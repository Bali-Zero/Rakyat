"""End to end through the real /api/search route: the real router, the production exception
handlers, the real SearchService; its embedder, vector client and the query expander's Gemini
client are fakes that LOG the input they receive (message, extras, and an exception they raise
and swallow). The observer sees every record at `handle`, non-propagating loggers included.
Synthetic sentinels only."""

from __future__ import annotations

import importlib
import logging
import types
from collections import OrderedDict
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.models import SearchQuery
from backend.app.setup.exception_handlers import register_exception_handlers
from backend.services.routing.query_router_integration import QueryRouterIntegration
from backend.services.search.query_expander import QueryExpander
from backend.services.search.search_service import SearchService

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

SENT = "zq-sentinel-7f3a91c2"
COLL = "zq-sentinel-coll-5d2e"
FAKE_LOG = logging.getLogger("backend.fake.vendor_client")


def _quote_and_swallow(label: str, text: str) -> None:
    FAKE_LOG.info("%s got %s", label, text, extra={"input": text, "payload": {"q": [text]}})
    try:
        raise ValueError(f"{label} choked on {text}")
    except ValueError:
        FAKE_LOG.warning("%s swallowed an error", label, exc_info=True)


class _Gemini:
    async def generate_response(self, prompt: str) -> str:
        _quote_and_swallow("gemini", prompt)
        if "EXACT JSON format" in prompt:
            return '{"english": "visa", "indonesian": "visa"}'
        # paraphrases: derived texts, not the registered text itself (the backstop matches the
        # full text; slices are the named sites' business)
        return '["alternatif satu tentang visa", "alternatif dua tentang pajak"]'


class _Embedder:
    provider = "fake"

    def __init__(self, boom: Exception | None = None) -> None:
        self.boom = boom

    async def generate_query_embedding(self, query: str) -> list[float]:
        _quote_and_swallow("embedder", query)
        if self.boom:
            raise self.boom
        return [0.1, 0.2, 0.3]


class _VectorDB:
    async def search(self, query_embedding: list[float], filter: Any, limit: int, **_: Any) -> Any:
        _quote_and_swallow("vector", f"filter={filter}")
        return {"ids": ["d1"], "documents": ["doc text"], "distances": [0.2], "metadatas": [{}]}


class _Manager:
    def get_collection(self, name: str) -> _VectorDB:
        return _VectorDB()


def _service(embedder: _Embedder | None = None) -> SearchService:
    svc = SearchService.__new__(SearchService)
    svc.query_expander = QueryExpander()
    svc.query_expander._gemini = _Gemini()
    svc.embedder = embedder or _Embedder()
    svc.collection_manager = _Manager()
    svc.query_router = QueryRouterIntegration()
    svc.surface_router = None
    svc.health_monitor = MagicMock()
    svc._embedding_cache = OrderedDict()
    svc._embedding_cache_max = 8
    svc._bm25_enabled = False
    svc._bm25_vectorizer = None
    return svc


def _client(service: Any) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(knowledge_router.router)
    app.state.search_service = service
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> Any:
    import backend.services.search.query_expander as qe

    fake = QueryExpander()
    fake._gemini = _Gemini()
    monkeypatch.setattr(qe, "_expander", fake)
    monkeypatch.setattr(settings, "enable_query_expansion", True, raising=False)
    from backend.core.cache import get_cache_service

    mem = getattr(get_cache_service(), "_memory_cache", None)
    if hasattr(mem, "clear"):
        mem.clear()
    with caplog.at_level(logging.DEBUG):
        yield


def _body(query: str, collection: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"query": query, "collection": collection, "level": 3, "limit": 3, **extra}


@pytest.mark.parametrize("with_collection", [False, True], ids=["multi-expansion", "collection"])
def test_a_successful_search_leaves_no_sentinel_in_any_record(
    observer: Any, with_collection: bool
) -> None:
    query = f"{SENT} kueri tentang visa dan pajak untuk perusahaan sintetis {with_collection}"
    resp = _client(_service()).post(
        "/api/search", json=_body(query, COLL if with_collection else None)
    )
    assert resp.status_code == 200 and resp.json()["results"], "the search did not run"
    assert observer.named("embedder got"), "the fake never logged its input: vacuous"
    assert with_collection or observer.named("gemini got"), "the expander's fake never ran"
    assert observer.named("embedder swallowed an error"), "the swallowed exception never logged"
    assert observer.named("Received query: query_len="), "request start line missing"
    assert observer.named("Search completed: query_len="), "request finish line missing"
    assert SENT not in observer.blob()
    assert not with_collection or COLL not in observer.blob()


def _error_query() -> str:
    return f"{SENT} kueri error tentang visa dan pajak untuk perusahaan sintetis"


def test_a_failing_search_answers_a_fixed_500_and_logs_no_sentinel(observer: Any) -> None:
    query = _error_query()
    service = _service(_Embedder(boom=RuntimeError(f"embedder rejected {query} in {COLL}")))
    resp = _client(service).post("/api/search", json=_body(query, COLL))
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Search failed"
    assert SENT not in resp.text and COLL not in resp.text
    assert observer.named("Search error: RuntimeError"), "the route's error line is missing"
    assert observer.named("HTTPException: 500"), "the HTTP exception handler never logged"
    assert SENT not in observer.blob() and COLL not in observer.blob()


async def _call_route(service: Any, query: SearchQuery) -> HTTPException:
    request = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(search_service=service)),
        method="POST",
        url=types.SimpleNamespace(path="/api/search"),
    )
    try:
        await knowledge_router.semantic_search(query, request)
    except HTTPException as exc:
        return exc
    raise AssertionError("the route did not raise")


@pytest.mark.asyncio
async def test_the_500_carries_no_exception_chain() -> None:
    query = _error_query()
    service = _service(_Embedder(boom=RuntimeError(f"embedder rejected {query}")))
    exc = await _call_route(service, SearchQuery(query=query, level=3, limit=3))
    assert exc.status_code == 500 and exc.detail == "Search failed"
    assert exc.__cause__ is None and exc.__context__ is None


class _RaisesHTTP:
    def __init__(self, status: int, detail: Any) -> None:
        self.status, self.detail = status, detail

    async def search(self, **kwargs: Any) -> Any:
        try:
            raise RuntimeError(f"low level quoted {kwargs['query']}")
        except RuntimeError as cause:
            raise HTTPException(status_code=self.status, detail=self.detail) from cause


@pytest.mark.asyncio
async def test_a_pass_through_http_exception_keeps_its_status_but_not_its_text() -> None:
    query = _error_query()
    service = _RaisesHTTP(404, f"not found: {query}")
    exc = await _call_route(service, SearchQuery(query=query, level=3, limit=3))
    assert exc.status_code == 404 and exc.detail == "Search failed"
    assert exc.__cause__ is None and exc.__context__ is None
    resp = _client(service).post("/api/search", json=_body(query))
    assert resp.status_code == 404 and resp.json()["detail"] == "Search failed"


@pytest.mark.asyncio
async def test_the_routes_own_400_keeps_its_fixed_text_without_a_chain() -> None:
    query = SearchQuery.model_construct(query=_error_query(), level=9, limit=3, collection=None)
    exc = await _call_route(_service(), query)
    assert exc.status_code == 400 and exc.detail == "Invalid access level. Must be 0-3."
    assert exc.__cause__ is None and exc.__context__ is None


def test_a_422_is_answered_without_logging_the_sentinel(observer: Any) -> None:
    query = f"{SENT} kueri dengan limit di luar batas"
    resp = _client(_service()).post("/api/search", json=_body(query, limit=999))
    assert resp.status_code == 422
    assert SENT not in observer.blob()
