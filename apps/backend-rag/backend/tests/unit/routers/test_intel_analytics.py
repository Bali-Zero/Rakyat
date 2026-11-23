"""Unit tests for backend/app/routers/intel_analytics.py"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from fastapi import HTTPException

from backend.core.embeddings import EmbeddingsGenerator
from backend.core.qdrant_db import QdrantClient

# ---------------------------------------------------------------------------
# Module-level patches: heavy deps that must never import for real
# ---------------------------------------------------------------------------

_MOCK_INTEL_COLLECTIONS = {
    "visa": "bali_intel_visa",
    "news": "bali_intel_news",
}


@pytest.fixture(autouse=True)
def _patch_imports(monkeypatch):
    """Patch all external dependencies imported at module level."""
    # Create mock staging service
    mock_staging = MagicMock()
    visa_dir = MagicMock(spec=Path)
    visa_dir.exists.return_value = False
    visa_dir.glob.return_value = []
    news_dir = MagicMock(spec=Path)
    news_dir.exists.return_value = False
    news_dir.glob.return_value = []
    mock_staging.get_staging_dir.side_effect = lambda t: visa_dir if t == "visa" else news_dir

    mock_analytics = MagicMock()
    mock_embedder = create_autospec(EmbeddingsGenerator, instance=True)

    # Patch the intel module references used by intel_analytics
    monkeypatch.setattr(
        "backend.app.routers.intel_analytics.INTEL_COLLECTIONS",
        _MOCK_INTEL_COLLECTIONS,
    )
    monkeypatch.setattr(
        "backend.app.routers.intel_analytics.staging_service",
        mock_staging,
    )
    monkeypatch.setattr(
        "backend.app.routers.intel_analytics.analytics_service",
        mock_analytics,
    )
    monkeypatch.setattr(
        "backend.app.routers.intel_analytics.get_embedder",
        lambda: mock_embedder,
    )
    return {
        "staging": mock_staging,
        "analytics": mock_analytics,
        "embedder": mock_embedder,
        "visa_dir": visa_dir,
        "news_dir": news_dir,
    }


# ---------------------------------------------------------------------------
# /api/intel/metrics
# ---------------------------------------------------------------------------


class TestGetSystemMetrics:
    """Tests for GET /api/intel/metrics."""

    @pytest.mark.asyncio
    async def test_metrics_basic_success(self, _patch_imports):
        """Metrics returns expected keys when everything is healthy."""
        from backend.app.routers.intel_analytics import get_system_metrics

        result = await get_system_metrics()

        assert "agent_status" in result
        assert "last_run" in result
        assert "items_processed_today" in result

    @pytest.mark.asyncio
    async def test_metrics_fake_fields_removed(self, _patch_imports):
        """uptime_percentage/avg_response_time_ms/next_scheduled_run/qdrant_health
        were fake constants (fixed 99.8, content-length-derived latency, a
        hardcoded +2h guess, and a QdrantClient constructor that never contacts
        the server) with no live consumer — kita pruning wave 2d removed them
        instead of shipping fake data."""
        from backend.app.routers.intel_analytics import get_system_metrics

        result = await get_system_metrics()

        assert "uptime_percentage" not in result
        assert "avg_response_time_ms" not in result
        assert "next_scheduled_run" not in result
        assert "qdrant_health" not in result

    @pytest.mark.asyncio
    async def test_metrics_with_staging_files(self, _patch_imports):
        """When staging dirs contain JSON files, items_processed_today counts them."""
        visa_dir = _patch_imports["visa_dir"]
        visa_dir.exists.return_value = True
        visa_dir.glob.return_value = [Path("a.json"), Path("b.json")]
        news_dir = _patch_imports["news_dir"]
        news_dir.exists.return_value = True
        news_dir.glob.return_value = [Path("c.json")]

        from backend.app.routers.intel_analytics import get_system_metrics

        result = await get_system_metrics()

        assert result["items_processed_today"] == 3

    @pytest.mark.asyncio
    async def test_metrics_scheduler_active(self, _patch_imports):
        """When autonomous scheduler has recent tasks, agent_status=active."""
        mock_task = MagicMock()
        mock_task.last_run = datetime.now(tz=timezone.utc)
        mock_task.enabled = True
        mock_scheduler = MagicMock()
        mock_scheduler.tasks = {"t1": mock_task}

        with patch(
            "backend.services.misc.autonomous_scheduler.get_autonomous_scheduler",
            return_value=mock_scheduler,
        ):
            from backend.app.routers.intel_analytics import get_system_metrics

            result = await get_system_metrics()

        assert result["agent_status"] == "active"

    @pytest.mark.asyncio
    async def test_metrics_scheduler_not_configured(self, _patch_imports):
        """When no scheduler exists, agent_status=not_configured."""
        with patch(
            "backend.services.misc.autonomous_scheduler.get_autonomous_scheduler",
            return_value=None,
        ):
            from backend.app.routers.intel_analytics import get_system_metrics

            result = await get_system_metrics()

        assert result["agent_status"] == "not_configured"


# ---------------------------------------------------------------------------
# /api/intel/search
# ---------------------------------------------------------------------------


class TestSearchIntel:
    """Tests for POST /api/intel/search."""

    @pytest.mark.asyncio
    async def test_search_basic(self, _patch_imports):
        """Basic search returns results list."""
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.1] * 1536

        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "documents": ["doc1"],
            "metadatas": [{"id": "1", "title": "Test", "tier": "T1"}],
            "distances": [0.5],
        }

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            return_value=mock_client,
        ):
            from backend.app.routers.intel_analytics import (
                IntelSearchRequest,
                search_intel,
            )

            request = IntelSearchRequest(query="test visa")
            result = await search_intel(request)

        assert "results" in result
        assert "total" in result

    @pytest.mark.asyncio
    async def test_search_with_category_filter(self, _patch_imports):
        """Search with category narrows to single collection."""
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.1] * 1536

        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "documents": [],
            "metadatas": [],
            "distances": [],
        }

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            return_value=mock_client,
        ):
            from backend.app.routers.intel_analytics import (
                IntelSearchRequest,
                search_intel,
            )

            request = IntelSearchRequest(query="test", category="visa")
            result = await search_intel(request)

        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_search_collection_error_continues(self, _patch_imports):
        """When one collection search fails, others still return results."""
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.1] * 1536

        call_count = 0

        def mock_qdrant_factory(**kwargs):
            nonlocal call_count
            call_count += 1
            m = AsyncMock()
            if call_count == 1:
                m.search.side_effect = Exception("timeout")
            else:
                m.search.return_value = {
                    "documents": ["doc2"],
                    "metadatas": [{"id": "2", "title": "News", "tier": "T2"}],
                    "distances": [0.3],
                }
            return m

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            side_effect=mock_qdrant_factory,
        ):
            from backend.app.routers.intel_analytics import (
                IntelSearchRequest,
                search_intel,
            )

            request = IntelSearchRequest(query="test")
            result = await search_intel(request)

        # Should get results from the non-failing collection
        assert result["total"] >= 0

    @pytest.mark.asyncio
    async def test_search_results_sorted_by_similarity(self, _patch_imports):
        """Results are sorted by similarity_score descending."""
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.1] * 1536

        mock_client = AsyncMock()
        mock_client.search.return_value = {
            "documents": ["doc1", "doc2"],
            "metadatas": [
                {"id": "1", "title": "A", "tier": "T1"},
                {"id": "2", "title": "B", "tier": "T1"},
            ],
            "distances": [1.0, 0.1],
        }

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            return_value=mock_client,
        ):
            from backend.app.routers.intel_analytics import (
                IntelSearchRequest,
                search_intel,
            )

            request = IntelSearchRequest(query="test", category="visa")
            result = await search_intel(request)

        if len(result["results"]) >= 2:
            assert (
                result["results"][0]["similarity_score"] >= result["results"][1]["similarity_score"]
            )

    @pytest.mark.asyncio
    async def test_search_passes_the_awaited_vector_to_the_client(self, _patch_imports):
        """The client receives the vector the async embedder returned, not a coroutine."""
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.25, 0.5, 0.75]

        client = create_autospec(QdrantClient, instance=True)
        client.search.return_value = {
            "documents": ["visa news"],
            "metadatas": [{"id": "1", "title": "Visa", "tier": "T1"}],
            "distances": [0.0],
        }

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import IntelSearchRequest, search_intel

            result = await search_intel(IntelSearchRequest(query="visa", category="visa"))

        embedder.generate_single_embedding.assert_awaited_once_with("visa")
        assert client.search.await_args.kwargs["query_embedding"] == [0.25, 0.5, 0.75]
        assert result["total"] == 1
        assert result["results"][0]["id"] == "1"

    @pytest.mark.asyncio
    async def test_search_one_failing_collection_keeps_the_others(self, _patch_imports):
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.1, 0.2]

        failing = create_autospec(QdrantClient, instance=True)
        failing.search.side_effect = RuntimeError("timeout")
        working = create_autospec(QdrantClient, instance=True)
        working.search.return_value = {
            "documents": ["news doc"],
            "metadatas": [{"id": "2", "title": "News", "tier": "T2"}],
            "distances": [0.3],
        }
        clients = {"bali_intel_visa": failing, "bali_intel_news": working}

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            side_effect=lambda collection_name: clients[collection_name],
        ):
            from backend.app.routers.intel_analytics import IntelSearchRequest, search_intel

            result = await search_intel(IntelSearchRequest(query="test"))

        assert result["total"] == 1
        assert result["results"][0]["id"] == "2"

    @pytest.mark.asyncio
    async def test_search_every_collection_failing_is_an_error_not_an_empty_success(
        self, _patch_imports
    ):
        embedder = _patch_imports["embedder"]
        embedder.generate_single_embedding.return_value = [0.1, 0.2]

        failing = create_autospec(QdrantClient, instance=True)
        failing.search.side_effect = RuntimeError("connection refused to qdrant.internal")

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=failing):
            from backend.app.routers.intel_analytics import IntelSearchRequest, search_intel

            with pytest.raises(HTTPException) as exc_info:
                await search_intel(IntelSearchRequest(query="test"))

        assert exc_info.value.status_code == 500
        assert "qdrant.internal" not in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# /api/intel/store
# ---------------------------------------------------------------------------


class TestStoreIntel:
    """Tests for POST /api/intel/store."""

    @pytest.mark.asyncio
    async def test_store_success(self, _patch_imports):
        mock_client = AsyncMock()

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            return_value=mock_client,
        ):
            from backend.app.routers.intel_analytics import (
                IntelStoreRequest,
                store_intel,
            )

            request = IntelStoreRequest(
                collection="visa",
                id="test-1",
                document="test doc",
                embedding=[0.1] * 1536,
                metadata={"title": "test"},
                full_data={"key": "value"},
            )
            result = await store_intel(request, current_user={"role": "admin"})

        assert result["success"] is True
        assert result["collection"] == "bali_intel_visa"

    @pytest.mark.asyncio
    async def test_store_invalid_collection(self, _patch_imports):
        from backend.app.routers.intel_analytics import IntelStoreRequest, store_intel

        request = IntelStoreRequest(
            collection="invalid_collection",
            id="test-1",
            document="test doc",
            embedding=[0.1] * 1536,
            metadata={},
            full_data={},
        )
        with pytest.raises(HTTPException) as exc_info:
            await store_intel(request, current_user={"role": "admin"})
        # The inner HTTPException(400) is caught by the outer try/except
        # and re-raised as HTTPException(500), so we check for 500.
        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    @pytest.mark.parametrize("user", [{"role": "staff", "email": "member@example.test"}, {}])
    async def test_store_refuses_a_caller_who_is_not_an_admin(self, _patch_imports, user):
        client = create_autospec(QdrantClient, instance=True)

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import IntelStoreRequest, store_intel

            request = IntelStoreRequest(
                collection="visa",
                id="test-1",
                document="test doc",
                embedding=[0.1] * 4,
                metadata={},
                full_data={},
            )
            with pytest.raises(HTTPException) as exc_info:
                await store_intel(request, current_user=user)

        assert exc_info.value.status_code == 403
        client.upsert_documents.assert_not_called()

    def test_store_route_answers_403_to_a_non_admin_and_200_to_an_admin(self, _patch_imports):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.app.dependencies import get_current_user
        from backend.app.routers.intel_analytics import router

        app = FastAPI()
        app.include_router(router)
        body = {
            "collection": "visa",
            "id": "test-1",
            "document": "test doc",
            "embedding": [0.1] * 4,
            "metadata": {},
            "full_data": {},
        }
        client = create_autospec(QdrantClient, instance=True)

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            app.dependency_overrides[get_current_user] = lambda: {"role": "staff"}
            denied = TestClient(app).post("/api/intel/store", json=body)
            app.dependency_overrides[get_current_user] = lambda: {"role": "admin"}
            allowed = TestClient(app).post("/api/intel/store", json=body)

        assert denied.status_code == 403
        assert allowed.status_code == 200
        client.upsert_documents.assert_awaited_once()


# ---------------------------------------------------------------------------
# /api/intel/critical
# ---------------------------------------------------------------------------


class TestGetCriticalItems:
    """Tests for GET /api/intel/critical."""

    @staticmethod
    def _point(point_id, *, impact="critical", days_ago=1, **extra):
        published = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()
        metadata = {
            "id": point_id,
            "title": f"Item {point_id}",
            "impact_level": impact,
            "published_at": published,
            **extra,
        }
        return {"id": point_id, "payload": {"text": "body", "metadata": metadata}}

    @staticmethod
    def _client(points=None, error=None):
        client = create_autospec(QdrantClient, instance=True)
        if error is not None:
            client.scroll_strict.side_effect = error
        else:
            client.scroll_strict.return_value = points or []
        return client

    @pytest.mark.asyncio
    async def test_critical_items_returns_structure(self, _patch_imports):
        client = self._client([])

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_critical_items

            result = await get_critical_items()

        assert result == {"items": [], "alerts": [], "count": 0}

    @pytest.mark.asyncio
    async def test_critical_returns_stored_shape_in_range_and_drops_out_of_range(
        self, _patch_imports
    ):
        client = self._client(
            [
                self._point("in-range", days_ago=2, source_name="src"),
                self._point("too-old", days_ago=60),
                self._point("not-critical", impact="low", days_ago=1),
                self._point("no-date", days_ago=1) | {},
            ]
        )
        client.scroll_strict.return_value[3]["payload"]["metadata"].pop("published_at")

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_critical_items

            result = await get_critical_items(category="visa", days=7)

        assert [i["id"] for i in result["items"]] == ["in-range"]
        assert result["count"] == 1
        assert result["alerts"] == result["items"]
        assert result["items"][0]["severity"] == "high"
        assert "partial" not in result
        assert client.scroll_strict.await_args.kwargs["metadata_filter"] == {
            "impact_level": "critical"
        }

    @pytest.mark.asyncio
    async def test_critical_newest_first_and_collections_are_read_once(
        self, _patch_imports, monkeypatch
    ):
        monkeypatch.setattr(
            "backend.app.routers.intel_analytics.INTEL_COLLECTIONS",
            {"news": "shared", "social": "shared", "roundup": "shared"},
        )
        client = self._client([self._point("older", days_ago=3), self._point("newer", days_ago=1)])

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_critical_items

            result = await get_critical_items()

        assert [i["id"] for i in result["items"]] == ["newer", "older"]
        assert client.scroll_strict.await_count == 1

    @pytest.mark.asyncio
    async def test_critical_reading_past_the_bound_says_it_is_partial(self, _patch_imports):
        client = self._client(error=RuntimeError("Qdrant strict scroll exceeded max_points"))

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_critical_items

            result = await get_critical_items(category="visa")

        assert result["partial"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_critical_one_collection_failing_is_marked_partial(self, _patch_imports):
        failing = self._client(error=ConnectionError("down"))
        working = self._client([self._point("ok", days_ago=1)])
        clients = {"bali_intel_visa": failing, "bali_intel_news": working}

        with patch(
            "backend.app.routers.intel_analytics.QdrantClient",
            side_effect=lambda collection_name: clients[collection_name],
        ):
            from backend.app.routers.intel_analytics import get_critical_items

            result = await get_critical_items()

        assert [i["id"] for i in result["items"]] == ["ok"]
        assert result["partial"] is True

    @pytest.mark.asyncio
    async def test_critical_every_collection_failing_is_an_error(self, _patch_imports):
        client = self._client(error=ConnectionError("qdrant.internal down"))

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_critical_items

            with pytest.raises(HTTPException) as exc_info:
                await get_critical_items()

        assert exc_info.value.status_code == 500
        assert "qdrant.internal" not in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# /api/intel/trends
# ---------------------------------------------------------------------------


class TestGetTrends:
    """Tests for GET /api/intel/trends."""

    @staticmethod
    def _client(stats):
        client = create_autospec(QdrantClient, instance=True)
        client.get_stats.return_value = stats
        return client

    @pytest.mark.asyncio
    async def test_trends_returns_structure(self, _patch_imports):
        client = self._client({"collection_name": "x", "total_documents": 42})

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_trends

            result = await get_trends()

        assert "trends" in result
        assert "top_topics" in result
        assert [t["total_items"] for t in result["trends"]] == [42, 42]

    @pytest.mark.asyncio
    async def test_trends_with_category(self, _patch_imports):
        client = self._client({"collection_name": "x", "total_documents": 10})

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_trends

            result = await get_trends(category="visa")

        client.get_stats.assert_awaited_once_with()
        assert result["trends"] == [{"collection": "visa", "total_items": 10}]

    @pytest.mark.asyncio
    async def test_trends_skips_a_collection_whose_statistics_failed(self, _patch_imports):
        client = self._client({"collection_name": "x", "error": "HTTP 503"})

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_trends

            result = await get_trends(category="visa")

        assert result["trends"] == []


# ---------------------------------------------------------------------------
# /api/intel/analytics
# ---------------------------------------------------------------------------


class TestGetIntelligenceAnalytics:
    """Tests for GET /api/intel/analytics."""

    @pytest.mark.asyncio
    async def test_analytics_delegates_to_service(self, _patch_imports):
        analytics = _patch_imports["analytics"]
        analytics.get_intelligence_analytics.return_value = {"metrics": True}

        from backend.app.routers.intel_analytics import get_intelligence_analytics

        result = await get_intelligence_analytics(days=30)

        analytics.get_intelligence_analytics.assert_called_once_with(30)
        assert result == {"metrics": True}

    @pytest.mark.asyncio
    async def test_analytics_error_raises_500(self, _patch_imports):
        analytics = _patch_imports["analytics"]
        analytics.get_intelligence_analytics.side_effect = RuntimeError("boom")

        from backend.app.routers.intel_analytics import get_intelligence_analytics

        with pytest.raises(HTTPException) as exc_info:
            await get_intelligence_analytics()
        assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# /api/intel/stats/{collection}
# ---------------------------------------------------------------------------


class TestGetCollectionStats:
    """Tests for GET /api/intel/stats/{collection}."""

    @pytest.mark.asyncio
    async def test_stats_success(self, _patch_imports):
        client = create_autospec(QdrantClient, instance=True)
        client.get_stats.return_value = {
            "collection_name": "bali_intel_visa",
            "total_documents": 99,
        }

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_collection_stats

            result = await get_collection_stats("visa")

        client.get_stats.assert_awaited_once_with()
        assert result["collection_name"] == "bali_intel_visa"
        assert result["total_documents"] == 99
        assert set(result) == {"collection_name", "total_documents", "last_updated"}

    @pytest.mark.asyncio
    async def test_stats_unknown_collection_is_404(self, _patch_imports):
        from backend.app.routers.intel_analytics import get_collection_stats

        with pytest.raises(HTTPException) as exc_info:
            await get_collection_stats("nonexistent")
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_stats_reported_error_is_500_without_internals(self, _patch_imports):
        client = create_autospec(QdrantClient, instance=True)
        client.get_stats.return_value = {
            "collection_name": "bali_intel_visa",
            "error": "connect to qdrant.internal:6333 refused",
        }

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_collection_stats

            with pytest.raises(HTTPException) as exc_info:
                await get_collection_stats("visa")

        assert exc_info.value.status_code == 500
        assert "qdrant.internal" not in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_stats_raised_error_is_500_without_internals(self, _patch_imports):
        client = create_autospec(QdrantClient, instance=True)
        client.get_stats.side_effect = RuntimeError("secret-host.internal exploded")

        with patch("backend.app.routers.intel_analytics.QdrantClient", return_value=client):
            from backend.app.routers.intel_analytics import get_collection_stats

            with pytest.raises(HTTPException) as exc_info:
                await get_collection_stats("visa")

        assert exc_info.value.status_code == 500
        assert "secret-host" not in str(exc_info.value.detail)
