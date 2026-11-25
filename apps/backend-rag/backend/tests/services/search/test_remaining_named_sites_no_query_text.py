"""Guard: the log lines of KnowledgeService, QueryRouter and SurfaceRouter that used to carry
the query (or a slice of it, or the free-form collection) log lengths and flags only. Run
WITHOUT the request-scoped backstop. Each case asserts its line was emitted. Synthetic only."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.modules.knowledge.service import KnowledgeService
from backend.services.routing import keyword_matcher
from backend.services.routing.query_router import QueryRouter
from backend.services.routing.surface_router import SurfaceRouter

SENT = "zq-sentinel-7f3a91c2"
QUERY = f"{SENT} kueri tentang visa dan pajak untuk perusahaan sintetis"
OVERRIDE = "koleksi-tak-dikenal"


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


def _logged(caplog: pytest.LogCaptureFixture, logger_suffix: str, needle: str) -> logging.LogRecord:
    for r in caplog.records:
        if r.name.endswith(logger_suffix) and needle in r.getMessage():
            return r
    raise AssertionError(f"{needle!r} was never logged by {logger_suffix}: the case is vacuous")


def _knowledge_service(embedder_boom: Exception | None = None) -> KnowledgeService:
    client = MagicMock()
    client.search = AsyncMock(
        return_value={"documents": ["doc"], "ids": ["d1"], "distances": [0.1], "metadatas": [{}]}
    )
    router = MagicMock()
    router.route = MagicMock(return_value="visa_oracle")
    with (
        patch("backend.app.modules.knowledge.service.QdrantClient", return_value=client),
        patch("backend.core.embeddings.create_embeddings_generator") as make_embedder,
        patch("backend.app.modules.knowledge.service.QueryRouter", return_value=router),
    ):
        embedder = MagicMock()
        embedder.provider = "fake"
        embedder.dimensions = 3
        if embedder_boom:
            embedder.generate_query_embedding = MagicMock(side_effect=embedder_boom)
        else:
            embedder.generate_query_embedding = MagicMock(return_value=[0.1, 0.2, 0.3])
        make_embedder.return_value = embedder
        service = KnowledgeService()
    for key in service.collections:
        service.collections[key] = client
    return service


@pytest.mark.asyncio
async def test_knowledge_service_search_lines_carry_lengths_and_flags_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _knowledge_service()
    with caplog.at_level(logging.DEBUG):
        await service.search(QUERY, user_level=3, limit=3, collection_override=OVERRIDE)
    suffix = "knowledge.service"
    assert f"query_len={len(QUERY)}" in _logged(caplog, suffix, "Query: query_len=").getMessage()
    params = _logged(caplog, suffix, "Parameters: collection_override_set=True")
    _logged(caplog, suffix, "Using override collection")
    unknown = _logged(caplog, suffix, "Unknown collection, defaulting to visa_oracle")
    assert OVERRIDE not in params.getMessage() + unknown.getMessage()
    assert OVERRIDE not in "".join(
        r.getMessage()
        for r in caplog.records
        if r.name.endswith(suffix) and "override" in r.getMessage()
    )
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_knowledge_service_error_logs_type_and_frames_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _knowledge_service(embedder_boom=RuntimeError(f"embedder rejected {QUERY}"))
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError):
        await service.search(QUERY, user_level=3, limit=3)
    _logged(caplog, "knowledge.service", "Search error: RuntimeError")
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
async def test_knowledge_service_rerank_line_carries_no_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _knowledge_service()

    async def fake_search(**kwargs: object) -> dict[str, object]:
        return {"results": [{"text": "t"}]}

    service.search = fake_search  # type: ignore[method-assign]
    service.reranker = MagicMock(enabled=True, rerank=AsyncMock(return_value=[{"text": "t"}]))
    with caplog.at_level(logging.DEBUG):
        await service.search_with_reranking(QUERY, user_level=3, limit=3)
    _logged(caplog, "knowledge.service", "Re-ranking 1 candidates")
    assert SENT not in _text(caplog)


def test_query_router_logs_length_and_only_vocabulary_keywords(
    caplog: pytest.LogCaptureFixture,
) -> None:
    router = QueryRouter()
    with caplog.at_level(logging.DEBUG):
        router.route(QUERY)
    _logged(caplog, "query_router", f"Query: query_len={len(QUERY)}")
    detected = [r for r in caplog.records if "Detected keywords" in r.getMessage()]
    assert detected, "the matched-keywords line was never logged: the case is vacuous"
    vocabulary = {
        kw
        for name, value in vars(keyword_matcher).items()
        if name.endswith("_KEYWORDS") and isinstance(value, list)
        for kw in value
    }
    matched = detected[0].args  # a lone dict argument is stored unwrapped by LogRecord
    assert isinstance(matched, dict)
    assert matched, "no keyword matched: the vocabulary assertion would be vacuous"
    for words in matched.values():
        assert set(words) <= vocabulary
    assert SENT not in _text(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boom", "needle"),
    [
        (TimeoutError(f"slow on {QUERY}"), f"Haiku timeout (query_len={len(QUERY)})"),
        (RuntimeError(f"cli rejected {QUERY}"), "Haiku error: RuntimeError"),
    ],
    ids=["timeout", "error"],
)
async def test_surface_router_haiku_failures_log_no_query(
    caplog: pytest.LogCaptureFixture, boom: Exception, needle: str
) -> None:
    router = SurfaceRouter(enabled=True)
    with (
        patch("backend.llm.claude_oauth_client.complete_async", AsyncMock(side_effect=boom)),
        caplog.at_level(logging.DEBUG),
    ):
        if isinstance(boom, TimeoutError):
            with pytest.raises(TimeoutError):
                await router._classify_with_haiku(QUERY)
        else:
            await router._classify_with_haiku(QUERY)
    _logged(caplog, "surface_router", needle)
    assert SENT not in _text(caplog)
