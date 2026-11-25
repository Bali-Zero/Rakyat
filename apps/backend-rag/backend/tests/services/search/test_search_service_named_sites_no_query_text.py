"""Guard: each log line of SearchService changed to keep the query out of the logs still logs,
and carries no query text. Run WITHOUT the request-scoped backstop, so a reverted line is not
masked by it. Every test asserts that the line it covers was really emitted (non-vacuous).
Synthetic sentinel only."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any
from unittest.mock import MagicMock

import pytest

from backend.app.core.config import settings
from backend.services.routing.query_router_integration import QueryRouterIntegration
from backend.services.search.query_expander import QueryExpander
from backend.services.search.search_service import SearchService

SENT = "zq-sentinel-7f3a91c2"
QUERY = f"{SENT} kueri tentang visa dan pajak untuk perusahaan sintetis"


class _Gemini:
    def __init__(self, reply: str = "[]") -> None:
        self.reply = reply

    async def generate_response(self, prompt: str) -> str:
        if "EXACT JSON format" in prompt:  # the single-translation prompt wants an object
            return '{"english": "visa", "indonesian": "visa"}'
        return self.reply


class _Embedder:
    provider = "fake"

    def __init__(self, boom: Exception | None = None) -> None:
        self.boom = boom

    async def generate_query_embedding(self, query: str) -> list[float]:
        if self.boom:
            raise self.boom
        return [0.1, 0.2, 0.3]


class _VectorDB:
    def __init__(self, boom: Exception | None = None) -> None:
        self.boom = boom

    async def search(self, **_: Any) -> dict[str, Any]:
        if self.boom:
            raise self.boom
        return {"ids": ["d1"], "documents": ["doc text"], "distances": [0.2], "metadatas": [{}]}


class _Manager:
    def __init__(self, db: _VectorDB, missing: set[str] | None = None) -> None:
        self.db, self.missing = db, missing or set()

    def get_collection(self, name: str) -> _VectorDB | None:
        return None if name in self.missing else self.db


def _service(
    db: _VectorDB | None = None,
    embedder: _Embedder | None = None,
    manager: _Manager | None = None,
) -> SearchService:
    svc = SearchService.__new__(SearchService)
    svc.query_expander = QueryExpander()
    svc.query_expander._gemini = _Gemini()
    svc.embedder = embedder or _Embedder()
    svc.collection_manager = manager or _Manager(db or _VectorDB())
    svc.query_router = QueryRouterIntegration()
    svc.surface_router = None
    svc.health_monitor = MagicMock()
    svc._embedding_cache = OrderedDict()
    svc._embedding_cache_max = 8
    svc._bm25_enabled = False
    svc._bm25_vectorizer = None
    svc.conflict_stats = {"total_multi_collection_searches": 0}
    return svc


def _text(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.extend(str(v) for k, v in r.__dict__.items() if k not in ("args", "msg"))
        if r.exc_info and r.exc_info[1] is not None:
            parts.append(str(r.exc_info[1]))
        if r.exc_text:
            parts.append(r.exc_text)
    return "\n".join(parts)


def _text_of(record: logging.LogRecord) -> str:
    return (
        record.getMessage() + " " + str({k: v for k, v in record.__dict__.items() if k != "args"})
    )


def _only_search_service(caplog: pytest.LogCaptureFixture, needle: str) -> None:
    assert any(
        r.name.endswith("search_service") and needle in r.getMessage() for r in caplog.records
    ), f"{needle!r} was never logged by SearchService: the case would be vacuous"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    import backend.services.search.query_expander as qe

    fake = QueryExpander()
    fake._gemini = _Gemini()
    monkeypatch.setattr(qe, "_expander", fake)
    monkeypatch.setattr(settings, "enable_query_expansion", False, raising=False)
    from backend.core.cache import get_cache_service

    mem = getattr(get_cache_service(), "_memory_cache", None)
    if hasattr(mem, "clear"):
        mem.clear()


@pytest.mark.asyncio
async def test_surface_router_failure_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Surface:
        enabled = True

        async def adecide(self, query: str) -> Any:
            raise RuntimeError(f"surface refused {query}")

    svc = _service()
    svc.surface_router = _Surface()
    with caplog.at_level(logging.DEBUG):
        await svc._route_search_query(query=QUERY, collection_override=None, enable_fallbacks=False)
    _only_search_service(caplog, "SurfaceRouter routing failed; falling back")
    assert "RuntimeError" in _text(caplog)
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_expanded_query_debug_line_and_parameter_lines_carry_lengths_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = _service()
    with caplog.at_level(logging.DEBUG):
        out = await svc.search(QUERY, user_level=3, limit=3, collection_override=None)
    assert out.get("results")
    _only_search_service(caplog, "Query expanded for embedding: query_len=")
    _only_search_service(caplog, "Query: query_len=")
    _only_search_service(caplog, "Parameters: collection_override_set=False")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_unknown_collection_line_names_no_collection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coll = "koleksi-tak-dikenal"  # the router integration logs an override; only the named line is under test
    svc = _service(manager=_Manager(_VectorDB(), missing={coll}))
    with caplog.at_level(logging.DEBUG):
        await svc.search(QUERY, user_level=3, limit=3, collection_override=coll)
    _only_search_service(caplog, "Unknown collection, defaulting to legal_unified")
    assert SENT not in _text(caplog)
    named = [
        r
        for r in caplog.records
        if r.name.endswith("search_service")
        and ("Unknown collection" in r.getMessage() or "Parameters:" in r.getMessage())
    ]
    assert all(coll not in _text_of(r) for r in named)
    assert any("collection_override_set=True" in r.getMessage() for r in named)


@pytest.mark.asyncio
async def test_multi_expansion_failure_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "enable_query_expansion", True, raising=False)
    svc = _service()

    async def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"expansion rejected {kwargs['query']}")

    svc._search_with_multi_expansion = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        out = await svc.search(QUERY, user_level=3, limit=3)
    assert out.get("results")
    _only_search_service(caplog, "Multi-query expansion failed, falling back: RuntimeError")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_sub_query_failure_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _service()
    import backend.services.search.query_expander as qe

    qe._expander._gemini = _Gemini(f'["{SENT} alt satu", "{SENT} alt dua"]')

    async def boom(**kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"sub-query rejected {kwargs['query']}")

    svc._single_query_search = boom  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        await svc._search_with_multi_expansion(QUERY, user_level=3, limit=3)
    _only_search_service(caplog, "Multi-expansion sub-query failed: RuntimeError")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_dense_hybrid_failure_extra_has_length_and_no_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Hybrid(_VectorDB):
        async def hybrid_search(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError(f"sparse rejected {kwargs}")

    svc = _service(db=_Hybrid())
    svc._bm25_enabled = True

    class _Sparse:
        def generate_query_sparse_vector(self, query: str) -> dict[str, list[Any]]:
            return {"indices": [1], "values": [1.0]}

    svc._bm25_vectorizer = _Sparse()
    with caplog.at_level(logging.DEBUG):
        await svc.search(QUERY, user_level=3, limit=3, collection_override="koleksi-tak-dikenal")
    _only_search_service(caplog, "Hybrid search failed, falling back to dense-only: RuntimeError")
    rec = next(r for r in caplog.records if "Hybrid search failed" in r.getMessage())
    assert rec.query_len == len(QUERY)  # type: ignore[attr-defined]
    assert rec.collection == "<override>"  # type: ignore[attr-defined]
    assert "koleksi-tak-dikenal" not in _text_of(rec)
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_rerank_early_exit_line_has_no_query(caplog: pytest.LogCaptureFixture) -> None:
    svc = _service()

    async def fake_search(**kwargs: Any) -> dict[str, Any]:
        return {"results": [{"score": 0.99, "text": "t"}]}

    svc.search = fake_search  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        out = await svc.search_with_reranking(QUERY, user_level=3, limit=3)
    assert out["early_exit"] is True
    _only_search_service(caplog, "skipping reranking")
    assert SENT not in _text(caplog)


class _Reranker:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    async def rerank(self, query: str, docs: list[Any], top_k: int) -> list[Any]:
        return docs[:top_k]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "needle"),
    [(True, "Re-ranking 1 candidates"), (False, "ReRanker disabled - skipping rerank")],
    ids=["enabled", "disabled"],
)
async def test_rerank_branches_log_no_query(
    caplog: pytest.LogCaptureFixture, enabled: bool, needle: str
) -> None:
    svc = _service()

    async def fake_search(**kwargs: Any) -> dict[str, Any]:
        return {"results": [{"score": 0.5, "text": "t"}]}

    svc.search = fake_search  # type: ignore[method-assign]
    svc._init_reranker = lambda: _Reranker(enabled)  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        await svc.search_with_reranking(QUERY, user_level=3, limit=3)
    _only_search_service(caplog, needle)
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_hybrid_rerank_line_has_no_query(caplog: pytest.LogCaptureFixture) -> None:
    svc = _service()

    async def fake_hybrid(**kwargs: Any) -> dict[str, Any]:
        return {"results": [{"score": 0.5, "text": "t"}]}

    svc.hybrid_search = fake_hybrid  # type: ignore[method-assign]
    svc._init_reranker = lambda: _Reranker(True)  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        await svc.hybrid_search_with_reranking(QUERY, user_level=3, limit=3)
    _only_search_service(caplog, "Re-ranking 1 hybrid candidates")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_hybrid_search_error_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = _service(embedder=_Embedder(boom=RuntimeError(f"embedder rejected {QUERY}")))
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        await svc.hybrid_search(QUERY, user_level=3, limit=3, fallback_to_plain=False)
    _only_search_service(caplog, "Hybrid search error: RuntimeError")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_conflict_resolution_error_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = _service(embedder=_Embedder(boom=RuntimeError(f"embedder rejected {QUERY}")))
    with caplog.at_level(logging.DEBUG):
        try:
            await svc.search_with_conflict_resolution(QUERY, user_level=3, limit=3)
        except Exception:  # the fallback may raise too; the log line is what is under test
            pass
    _only_search_service(caplog, "Search with conflict resolution error: RuntimeError")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_collection_search_failure_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = _service(embedder=_Embedder(boom=ValueError(f"embedder rejected {QUERY}")))
    with caplog.at_level(logging.DEBUG):
        out = await svc.search_collection(QUERY, "legal_unified", limit=2)
    assert "error" in out
    _only_search_service(caplog, "Collection search failed: ValueError")
    assert SENT not in _text(caplog)
