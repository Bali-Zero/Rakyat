"""A cached result can be refused by a predicate: an error answer is not kept."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.cache import CacheService, cached
from backend.core.exceptions import QdrantError
from backend.services.rag import hybrid_search as hs


def _no_error(result):
    return not (isinstance(result, dict) and result.get("error"))


def _make_counter(results, **decorator_kwargs):
    calls = {"n": 0}

    prefix = "sc_" + uuid.uuid4().hex
    decorate = cached(ttl=60, prefix=prefix, cache_service=CacheService(), **decorator_kwargs)

    @decorate
    async def fn(x):
        i = calls["n"]
        calls["n"] += 1
        return results[i]

    return fn, calls


@pytest.mark.asyncio
async def test_error_answer_is_not_kept_and_next_call_runs():
    fn, calls = _make_counter([{"error": "x"}, {"ok": 1}], should_cache=_no_error)
    first = await fn(1)
    second = await fn(1)
    assert first == {"error": "x"}
    assert second == {"ok": 1}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_good_answer_after_error_is_then_cached():
    fn, calls = _make_counter([{"error": "x"}, {"ok": 1}, {"ok": 2}], should_cache=_no_error)
    await fn(1)
    assert await fn(1) == {"ok": 1}
    assert await fn(1) == {"ok": 1}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_normal_dict_and_empty_list_are_cached_with_predicate():
    for value in ({"results": [1]}, []):
        fn, calls = _make_counter([value, "later"], should_cache=_no_error)
        assert await fn(1) == value
        assert await fn(1) == value
        assert calls["n"] == 1


@pytest.mark.asyncio
async def test_without_predicate_error_payload_is_cached_as_before():
    fn, calls = _make_counter([{"error": "x"}, {"ok": 1}])
    assert await fn(1) == {"error": "x"}
    assert await fn(1) == {"error": "x"}
    assert calls["n"] == 1


def test_hybrid_search_predicate_refuses_only_error_payloads():
    assert hs._is_cacheable_search_result({"search_type": "error", "error": "boom"}) is False
    assert hs._is_cacheable_search_result({"search_type": "dense_only", "results": []}) is True
    assert hs._is_cacheable_search_result([]) is True


@pytest.mark.asyncio
async def test_hybrid_search_error_is_not_served_to_the_next_caller(monkeypatch):
    monkeypatch.setattr(hs.settings, "enable_bm25", False, raising=False)
    embedder = MagicMock()
    embedder.generate_query_embedding = AsyncMock(side_effect=[QdrantError("blip"), [0.1, 0.2]])
    vector_db = MagicMock(spec=["search"])
    vector_db.search = AsyncMock(
        return_value={
            "ids": ["d1"],
            "documents": ["doc"],
            "metadatas": [{}],
            "scores": [0.9],
        }
    )
    manager = MagicMock()
    manager.get_collection.return_value = vector_db
    service = hs.HybridSearchService(collection_manager=manager, embedder=embedder)

    query = "synthetic query " + uuid.uuid4().hex
    first = await service.search_hybrid(query, "plain_collection", limit=3)
    second = await service.search_hybrid(query, "plain_collection", limit=3)

    assert first["search_type"] == "error"
    assert second["search_type"] != "error"
    assert second["total_results"] == 1
    assert embedder.generate_query_embedding.await_count == 2
