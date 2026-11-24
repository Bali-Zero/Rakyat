"""Guard: SearchService.search() leaves no query text in any log record, end to end.

Drives the real SearchService.search() (real QueryExpander, KeywordTranslator and QueryRouter;
fake embedder, vector DB and Gemini) with a canary query and asserts the canary appears in no
record: not in the message, not in its args, not in `extra`, not in the exception text.
Covers the multi-query expansion path, the single-query path, and the failure paths.
"""

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

CANARY = "uji-sintetis-kueri-9Q7ZK"
QUERY = f"{CANARY} kueri tentang visa dan pajak untuk perusahaan sintetis"


class _Gemini:
    async def generate_response(self, prompt: str) -> str:
        if "english" in prompt.lower() and "indonesian" in prompt.lower():
            return f'{{"english": "visa {CANARY}", "indonesian": "visa {CANARY}"}}'
        return f'["{CANARY} alt satu", "{CANARY} alt dua"]'


class _Embedder:
    provider = "fake"

    async def generate_query_embedding(self, query: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _VectorDB:
    def __init__(self, boom: Exception | None = None, hybrid_boom: Exception | None = None) -> None:
        self.boom, self.hybrid_boom = boom, hybrid_boom
        if hybrid_boom is not None:
            self.hybrid_search = self._hybrid

    async def _hybrid(self, **kwargs: Any) -> dict[str, Any]:
        raise self.hybrid_boom  # type: ignore[misc]

    async def search(
        self, query_embedding: list[float], filter: Any, limit: int, **_: Any
    ) -> dict[str, Any]:
        if self.boom:
            raise self.boom
        return {"ids": ["d1"], "documents": ["doc text"], "distances": [0.2], "metadatas": [{}]}


class _Manager:
    def __init__(self, db: _VectorDB) -> None:
        self.db = db

    def get_collection(self, name: str) -> _VectorDB:
        return self.db


class _Sparse:
    def generate_query_sparse_vector(self, query: str) -> dict[str, list[Any]]:
        return {"indices": [1], "values": [1.0]}


def _service(db: _VectorDB, bm25: bool = False) -> SearchService:
    svc = SearchService.__new__(SearchService)
    svc.query_expander = QueryExpander()
    svc.query_expander._gemini = _Gemini()
    svc.embedder = _Embedder()
    svc.collection_manager = _Manager(db)
    svc.query_router = QueryRouterIntegration()
    svc.surface_router = None
    svc.health_monitor = MagicMock()
    svc._embedding_cache = OrderedDict()
    svc._embedding_cache_max = 8
    svc._bm25_enabled = bm25
    svc._bm25_vectorizer = _Sparse() if bm25 else None
    return svc


def _everything(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.append(str(r.args))
        parts.extend(str(v) for k, v in r.__dict__.items() if k not in ("args", "msg"))
        if r.exc_info and r.exc_info[1] is not None:
            parts.append(str(r.exc_info[1]))
        if r.exc_text:
            parts.append(r.exc_text)
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def _no_real_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """The multi-expansion path asks the module singleton: point it at a fake, never the network."""
    import backend.services.search.query_expander as qe

    fake = QueryExpander()
    fake._gemini = _Gemini()
    monkeypatch.setattr(qe, "_expander", fake)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    from backend.core.cache import get_cache_service

    cache = get_cache_service()
    mem = getattr(cache, "_memory_cache", None)
    if hasattr(mem, "clear"):
        mem.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("expansion", [True, False], ids=["multi-expansion", "single-query"])
async def test_search_success_logs_no_canary(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, expansion: bool
) -> None:
    monkeypatch.setattr(settings, "enable_query_expansion", expansion, raising=False)
    svc = _service(_VectorDB())
    with caplog.at_level(logging.DEBUG):
        out = await svc.search(QUERY + (" m" if expansion else " s"), user_level=3, limit=3)
    assert out.get("results"), "search returned nothing: the path under test did not run"
    text = _everything(caplog)
    assert "query_len=" in text, "no query-shape line was logged: vacuous"
    assert CANARY not in text


@pytest.mark.asyncio
async def test_search_dense_failure_logs_no_canary(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "enable_query_expansion", False, raising=False)
    svc = _service(_VectorDB(boom=ValueError(f"backend rejected {QUERY}")))
    with caplog.at_level(logging.DEBUG):
        out = await svc.search(QUERY + " f", user_level=3, limit=3)
    assert out.get("error"), "the failure path did not run"
    text = _everything(caplog)
    assert "Search failed completely" in text, "failure line never logged: vacuous"
    assert CANARY not in text


@pytest.mark.asyncio
async def test_search_hybrid_failure_logs_no_canary(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "enable_query_expansion", False, raising=False)
    svc = _service(_VectorDB(hybrid_boom=RuntimeError(f"sparse failed on {QUERY}")), bm25=True)
    with caplog.at_level(logging.DEBUG):
        await svc.search(QUERY + " h", user_level=3, limit=3)
    text = _everything(caplog)
    assert "Hybrid search failed" in text, "hybrid-failure line never logged: vacuous"
    assert CANARY not in text
