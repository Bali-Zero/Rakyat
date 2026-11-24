from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.ingestion.collection_manager import CollectionManager


def make_manager(upsert_documents: AsyncMock) -> CollectionManager:
    manager = CollectionManager(qdrant_url="http://qdrant.test")
    manager.collection_definitions = {"synthetic": {"priority": "high", "doc_count": 1}}
    client = MagicMock()
    client.upsert_documents = upsert_documents
    manager._collections_cache["synthetic"] = client
    return manager


@pytest.mark.asyncio
async def test_ingest_with_lock_does_not_stamp_freshness_for_reported_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failed_result = {"success": False, "documents_added": 0}
    manager = make_manager(AsyncMock(return_value=failed_result))

    with patch("backend.app.metrics.metrics_collector") as metrics_collector:
        result = await manager.ingest_with_lock(
            "synthetic",
            documents=["synthetic document"],
            embeddings=[[0.1]],
            metadatas=[{}],
            ids=["synthetic-id"],
        )

    assert result is failed_result
    assert "synthetic" not in manager._collection_last_updated
    metrics_collector.set_collection_last_updated.assert_not_called()
    assert "synthetic" in caplog.text
    assert "0" in caplog.text
    assert "synthetic document" not in caplog.text


@pytest.mark.asyncio
async def test_ingest_with_lock_stamps_success_and_releases_lock_after_each_result() -> None:
    successful_result = {"success": True, "documents_added": 1}
    failed_result = {"success": False, "documents_added": 0}
    manager = make_manager(
        AsyncMock(side_effect=[successful_result, failed_result, successful_result]),
    )
    manager._lock_timeout = 0.01

    with patch("backend.app.metrics.metrics_collector") as metrics_collector:
        first_result = await manager.ingest_with_lock(
            "synthetic",
            documents=["first synthetic document"],
            embeddings=[[0.1]],
            metadatas=[{}],
            ids=["synthetic-first"],
        )
        stamped_at = manager._collection_last_updated["synthetic"]
        failed_call_result = await manager.ingest_with_lock(
            "synthetic",
            documents=["second synthetic document"],
            embeddings=[[0.2]],
            metadatas=[{}],
            ids=["synthetic-second"],
        )
        final_result = await manager.ingest_with_lock(
            "synthetic",
            documents=["third synthetic document"],
            embeddings=[[0.3]],
            metadatas=[{}],
            ids=["synthetic-third"],
        )

    assert first_result is successful_result
    assert failed_call_result is failed_result
    assert final_result is successful_result
    assert manager._collection_last_updated["synthetic"] >= stamped_at
    assert metrics_collector.set_collection_last_updated.call_count == 2
