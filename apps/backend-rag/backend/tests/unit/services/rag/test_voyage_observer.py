import asyncio
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from qdrant_client import QdrantClient

from backend.app.core.config import settings
from backend.services.rag import voyage_observer as observer
from backend.services.rag.agentic.tools import VectorSearchTool
from backend.services.rag.voyage_public import BASE_URL, digest_text
from backend.services.rag.voyage_shadow import PublicRelease, write_release
from backend.services.search.search_service import SearchService

QUESTION = "Which published passage covers a permit?"
PUBLIC = "Public article shared by both indexes"
SHADOW_ONLY = "Public article limited to the shadow collection"
BASELINE_ONLY = "An existing passage not approved for transmission"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    index = tmp_path / "index.json"
    manifest.write_text(
        json.dumps(
            {
                "public_documents": [PUBLIC, SHADOW_ONLY],
                "public_queries": [{"text": QUESTION, "rerank_model": "rerank-3-lite"}],
            }
        )
    )
    index.write_text(
        json.dumps(
            {
                "version": 1,
                "dimensions": 1024,
                "document_model": "voyage-4-large",
                "query_sha256": {digest_text(QUESTION): "rerank-3-lite"},
                "vectors": {
                    digest_text(PUBLIC): [1.0, 0.0] + [0.0] * 1022,
                    digest_text(SHADOW_ONLY): [0.0, 1.0] + [0.0] * 1022,
                },
            }
        )
    )
    release = PublicRelease.load(
        manifest,
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
        index,
        hashlib.sha256(index.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(settings, "voyage_public_manifest", str(manifest))
    monkeypatch.setattr(settings, "voyage_public_manifest_sha256", release.manifest_digest)
    monkeypatch.setattr(settings, "voyage_public_index_sha256", release.index_digest)
    monkeypatch.setattr(settings, "voyage_api_key", SecretStr("test-only"))
    db = QdrantClient(":memory:")
    monkeypatch.setattr(observer, "_reader", lambda _: nullcontext(db))
    yield db, release, index
    db.close()


def input_result(collection):
    return {
        "collection": collection,
        "results": [
            {
                "text": BASELINE_ONLY,
                "score": 0.99,
                "score_kind": "dense_formatted",
                "metadata": {"title": "Existing top result"},
            },
            {
                "text": PUBLIC,
                "score": 0.93,
                "score_kind": "dense_formatted",
                "metadata": {"title": "Current public result"},
            },
        ],
    }


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "unknown_query",
        "another_collection",
        "unready",
        "provider_failure",
        "wrong_provider_model",
        "time_limit",
        "empty",
    ],
)
async def test_observer_on_normal_tool_preserves_answer_and_readonly_boundaries(
    case, lab, monkeypatch
):
    db, release, index = lab
    if case != "unready":
        write_release(db, release)
    points_before = db.count(release.collection).count if case != "unready" else 0
    requests = []

    async def answer(req):
        body = json.loads(req.content)
        requests.append((req.url.path, body))
        assert BASELINE_ONLY not in req.content.decode()
        if case == "wrong_provider_model":
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0] * 1024}], "model": "other"}
            )
        if case == "provider_failure":
            return httpx.Response(429)
        if case == "time_limit":
            await asyncio.sleep(10)
        if req.url.path.endswith("/embeddings"):
            assert body["model"] == "voyage-4-lite" and body["input_type"] == "query"
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [0.0, 1.0] + [0.0] * 1022}]}
            )
        assert body["model"] == "rerank-3-lite" and set(body["documents"]) == {PUBLIC, SHADOW_ONLY}
        top = body["documents"].index(SHADOW_ONLY)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": top, "relevance_score": 0.99},
                    {"index": 1 - top, "relevance_score": 0.9},
                ]
            },
        )

    monkeypatch.setattr(
        observer,
        "_provider",
        lambda cfg: httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(answer)),
    )
    collection = "visa_oracle" if case == "another_collection" else "legal_unified"
    query = "unapproved question with private details" if case == "unknown_query" else QUESTION
    base = input_result(collection)
    if case == "empty":
        base["results"] = []
    svc = SearchService.__new__(SearchService)
    monkeypatch.setattr(svc, "hybrid_search", AsyncMock(side_effect=lambda **_: deepcopy(base)))
    monkeypatch.setattr(settings, "enable_hybrid_search", True)
    monkeypatch.setattr(settings, "voyage_shadow_observe", False)
    tool = VectorSearchTool(svc)
    before = json.loads(await tool.execute(query, collection=collection, top_k=2))
    assert requests == [] and "retrieval_observation" not in before
    monkeypatch.setattr(settings, "voyage_shadow_observe", True)
    from backend.services.rag.agentic.voyage_scope import canary_request

    with canary_request(
        query,
        enabled=True,
        current_user={"role": "Founder"},
        session_id="voyage-canary-" + "a" * 32,
    ) as witness:
        after = json.loads(await tool.execute(query, collection=collection, top_k=2))
        extra = witness or None
    assert after == before
    assert SHADOW_ONLY not in json.dumps(after)
    if case in {"unknown_query", "another_collection"}:
        assert extra is None and requests == []
    elif case in {"unready", "provider_failure", "wrong_provider_model", "time_limit"}:
        assert extra["mode"] == "shadow" and extra["status"] == "unavailable"
        assert len(requests) == (0 if case == "unready" else 1)
    else:
        assert (
            extra["mode"] == "shadow"
            and extra["status"] == "ok"
            and extra["index_sha256"] == release.index_digest
        )
        assert extra["served_legal"] == (0 if case == "empty" else 2)
        assert extra["shadow_count"] == 2 and extra["overlap"] == (0 if case == "empty" else 1)
        assert extra["top1_same"] is (None if case == "empty" else False)
        assert [path for path, _ in requests] == ["/v1/embeddings", "/v1/rerank"]
        assert SHADOW_ONLY not in json.dumps(extra)
    if case == "unready":
        assert not db.collection_exists(release.collection)
    else:
        assert db.count(release.collection).count == points_before


@pytest.mark.parametrize(
    "role,enabled,cache_hit,want_witness",
    [
        ("Founder", True, False, True),
        ("admin", True, False, True),
        (None, True, False, False),
        ("team", True, False, False),
        ("Founder", False, False, False),
        ("Founder", True, True, False),
    ],
)
async def test_shadow_reaches_only_fresh_operator_http_response(
    lab, monkeypatch, role, enabled, cache_hit, want_witness
):
    from unittest.mock import Mock

    from fastapi import FastAPI

    from backend.app.routers import agentic_rag as route
    from backend.services.rag.agentic.schema import CoreResult

    db, release, index = lab
    write_release(db, release)
    index.unlink()  # The serving machine needs only the approved manifest and the remote shadow.
    monkeypatch.setattr(settings, "voyage_shadow_observe", enabled)
    monkeypatch.setattr(settings, "enable_hybrid_search", True)
    monkeypatch.setenv("GIT_SHA", "a" * 40)
    calls = []

    async def provider(request):
        calls.append(request.url.path)
        if request.url.path.endswith("embeddings"):
            response = {"data": [{"index": 0, "embedding": [0.0, 1.0] + [0.0] * 1022}]}
        else:
            response = {
                "data": [{"index": 0, "relevance_score": 0.8}, {"index": 1, "relevance_score": 0.7}]
            }
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        observer,
        "_provider",
        lambda _: httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(provider)),
    )
    retriever = Mock()
    retriever.hybrid_search_with_reranking = AsyncMock(return_value=input_result("legal_unified"))
    tool = VectorSearchTool(retriever)

    class Runner:
        async def process_query(self, **args):
            tool_result = json.loads(
                await tool.execute(args["query"], collection="legal_unified", top_k=2)
            )
            assert "retrieval_observation" not in tool_result
            return CoreResult(
                answer="Baseline answer",
                model_used="fixture",
                route_used="fixture",
                cache_hit=cache_hit,
                sources=tool_result["sources"],
                tools_called=["vector_search"],
            )

    manager = Mock()
    manager.metrics_tracker.record_query_metrics = AsyncMock()
    manager.assign_variant.return_value = "control"
    manager.get_variant_config.return_value = {}
    monkeypatch.setattr(route, "get_ab_test_manager", lambda: manager)
    monkeypatch.setattr(route, "_lf_enabled", lambda: False)

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.get_current_user_optional] = lambda: (
        {"role": role, "user_id": "test"} if role else None
    )
    app.dependency_overrides[route.get_orchestrator] = lambda: Runner()
    app.dependency_overrides[route.get_optional_database_pool] = lambda: None
    app.dependency_overrides[route._verify_wa_inbox_bot_profile_key] = lambda: False
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as cli:
        body = {"query": QUESTION, "channel": "website", "session_id": "voyage-canary-" + "a" * 32}
        resp = await cli.post("/api/agentic-rag/query", json=body)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["answer"] == "Baseline answer" and len(result["sources"]) == 2
        assert SHADOW_ONLY not in resp.text
        receipt = result["debug_info"].get("voyage_shadow")
        assert bool(receipt) == want_witness
        if want_witness:
            assert receipt["worker_build_sha"] == "a" * 40
            assert receipt["index_sha256"] == release.index_digest
            assert receipt["query_sha256"] == digest_text(QUESTION)
            assert receipt["probe_nonce_sha256"] == digest_text(body["session_id"])
            assert receipt["shadow_count"] == 2 and receipt["status"] == "ok"
        if not enabled or role not in ("Founder", "admin"):
            assert not calls
        previous_calls = len(calls)
        second = await cli.post(
            "/api/agentic-rag/query", json={**body, "query": QUESTION + " extra"}
        )
        assert second.status_code == 200
        assert "voyage_shadow" not in second.json()["debug_info"]
        assert len(calls) == previous_calls


async def test_scope_is_per_request_and_reserves_one_exact_question():
    from backend.services.rag.agentic.voyage_scope import canary_request, reserve_observation

    async def request(question):
        with canary_request(
            question,
            enabled=True,
            current_user={"role": "admin"},
            session_id="voyage-canary-" + "a" * 32,
        ):
            await asyncio.sleep(0)
            assert not reserve_observation(question + " rewritten")
            assert reserve_observation(question)
            await asyncio.sleep(0)
            assert not reserve_observation(question)
        assert not reserve_observation(question)

    await asyncio.gather(request("first"), request("second"))


@pytest.mark.parametrize(
    "session", [None, "ordinary-session", "voyage-canary-123", "voyage-canary-" + "f" * 33]
)
def test_regular_operator_sessions_do_not_spend_provider_requests(session):
    from backend.services.rag.agentic.voyage_scope import canary_request, reserve_observation

    with canary_request(
        QUESTION, enabled=True, current_user={"role": "Founder"}, session_id=session
    ) as witness:
        assert witness is None and not reserve_observation(QUESTION)
