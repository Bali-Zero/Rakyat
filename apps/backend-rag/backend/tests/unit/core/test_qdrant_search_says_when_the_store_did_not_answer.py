"""The Qdrant client over its real HTTP path: refusals, outages and the scroll bound.

Every request goes through the real `QdrantClient` and an `httpx.MockTransport`
on its `_http_client`; only the retry wait is set to zero.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.core import qdrant_db
from backend.core.qdrant_db import QdrantClient, QdrantScrollBoundExceeded

_VECTOR = [0.1, 0.2, 0.3]
_HIT = {"id": "p-1", "score": 0.9, "payload": {"text": "body", "metadata": {"title": "T"}}}


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(qdrant_db._retry_with_backoff, "__defaults__", (qdrant_db.MAX_RETRIES, 0.0))


def _client(answers: list[httpx.Response], seen: list[dict]) -> QdrantClient:
    """A real client whose store answers `answers` in order (the last one repeats)."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content) if request.content else {})
        return answers[min(len(seen), len(answers)) - 1]

    client = QdrantClient(qdrant_url="http://qdrant.test", collection_name="news", api_key="k")
    client._http_client = httpx.AsyncClient(
        base_url="http://qdrant.test", transport=httpx.MockTransport(handler)
    )
    return client


_REFUSED = httpx.Response(400, json={"status": {"error": "Index required but not found"}})
_DOWN = httpx.Response(503, json={"status": {"error": "unavailable"}})
_NAMED = httpx.Response(400, text="Wrong input: Vector params for  are not specified in config")
_FOUND = httpx.Response(200, json={"result": [_HIT]})


@pytest.mark.asyncio
async def test_a_refusal_is_empty_by_default_and_raised_on_request():
    seen: list[dict] = []
    result = await _client([_REFUSED], seen).search(_VECTOR, limit=3)
    assert result["ids"] == [] and len(seen) == 1

    seen.clear()
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _client([_REFUSED], seen).search(_VECTOR, limit=3, raise_on_error=True)
    assert raised.value.response.status_code == 400
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_outage_is_empty_by_default_and_raised_on_request_after_the_retries():
    seen: list[dict] = []
    result = await _client([_DOWN], seen).search(_VECTOR, limit=3)
    assert result["ids"] == [] and len(seen) == qdrant_db.MAX_RETRIES + 1

    seen.clear()
    with pytest.raises(Exception, match="503"):
        await _client([_DOWN], seen).search(_VECTOR, limit=3, raise_on_error=True)
    assert len(seen) == qdrant_db.MAX_RETRIES + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_on_error", [False, True])
async def test_the_named_vector_fallback_still_answers_in_both_modes(raise_on_error):
    seen: list[dict] = []
    result = await _client([_NAMED, _FOUND], seen).search(
        _VECTOR, limit=3, raise_on_error=raise_on_error
    )
    assert result["ids"] == ["p-1"]
    assert seen[1]["vector"] == {"name": "dense", "vector": _VECTOR}


@pytest.mark.asyncio
async def test_a_failing_named_vector_fallback_raises_its_own_error_on_request():
    seen: list[dict] = []
    result = await _client([_NAMED, _DOWN], seen).search(_VECTOR, limit=3)
    assert result["ids"] == []

    seen.clear()
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _client([_NAMED, _DOWN], seen).search(_VECTOR, limit=3, raise_on_error=True)
    assert raised.value.response.status_code == 503


def _pages(seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        points = [{"id": f"p{len(seen)}-{i}", "payload": {}} for i in range(body["limit"])]
        return httpx.Response(
            200, json={"result": {"points": points, "next_page_offset": f"o{len(seen)}"}}
        )

    return handler


@pytest.mark.asyncio
async def test_a_strict_scroll_past_its_bound_raises_the_typed_bound_error():
    seen: list[dict] = []
    client = QdrantClient(qdrant_url="http://qdrant.test", collection_name="news", api_key="k")
    client._http_client = httpx.AsyncClient(
        base_url="http://qdrant.test", transport=httpx.MockTransport(_pages(seen))
    )

    with pytest.raises(QdrantScrollBoundExceeded) as raised:
        await client.scroll_strict(
            metadata_filter={"impact_level": "critical"}, page_size=2, max_points=3
        )

    assert isinstance(raised.value, RuntimeError)
    assert [body["limit"] for body in seen] == [2, 1]
    assert seen[1]["offset"] == "o1"
