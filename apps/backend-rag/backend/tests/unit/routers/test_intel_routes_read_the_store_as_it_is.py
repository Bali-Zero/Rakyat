"""The intel routes over the real Qdrant client: what they ask the store, what they answer.

Store traffic goes through the real `QdrantClient`, whose `_http_client` is an
`httpx.MockTransport` that records every request; only the retry wait is zero and
the embedder is a stub. The collection map is the real one: nine categories select
the news collection, `visa` selects the visa collection.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from backend.app.core.constants import IntelConstants
from backend.app.routers import intel_analytics
from backend.app.routers.intel import IntelSearchRequest
from backend.core import qdrant_db
from backend.core.qdrant_db import QdrantClient

NEWS = IntelConstants.COLLECTIONS["news"]
VISA = IntelConstants.COLLECTIONS["visa"]

Answer = Callable[[dict[str, Any]], httpx.Response]


class _Store:
    """A Qdrant HTTP server double: records each request and answers per collection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.answers: dict[tuple[str, str], Answer] = {}

    def on(self, collection: str, op: str, answer: Answer) -> None:
        self.answers[(collection, op)] = answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        collection = parts[1]
        op = parts[3] if len(parts) > 3 else "info"
        body = json.loads(request.content) if request.content else {}
        self.calls.append((collection, op, body))
        answer = self.answers.get((collection, op))
        if answer is None:
            return httpx.Response(404, json={"status": {"error": "Not found"}})
        return answer(body)

    def collections(self, op: str) -> list[str]:
        return [collection for collection, kind, _ in self.calls if kind == op]

    def bodies(self, op: str) -> list[dict[str, Any]]:
        return [body for _, kind, body in self.calls if kind == op]


class _Embedder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def generate_single_embedding(self, text: str) -> list[float]:
        if self.error is not None:
            raise self.error
        return [0.1, 0.2, 0.3]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    double = _Store()

    def real_client(collection_name: str) -> QdrantClient:
        client = QdrantClient(
            qdrant_url="http://qdrant.test", collection_name=collection_name, api_key="k"
        )
        client._http_client = httpx.AsyncClient(
            base_url="http://qdrant.test", transport=httpx.MockTransport(double.handler)
        )
        return client

    monkeypatch.setattr(intel_analytics, "QdrantClient", real_client)
    monkeypatch.setattr(intel_analytics, "INTEL_COLLECTIONS", dict(IntelConstants.COLLECTIONS))
    monkeypatch.setattr(intel_analytics, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(qdrant_db._retry_with_backoff, "__defaults__", (qdrant_db.MAX_RETRIES, 0.0))
    return double


@pytest.fixture
def host_east_of_utc() -> Iterator[None]:
    """The host clock reads UTC+8, so a naive date read as host time would move by 8 hours."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = "WITA-8"
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


_NOW = datetime.now(tz=timezone.utc)


def _ago(days: float) -> datetime:
    return _NOW - timedelta(days=days)


def _naive(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


_RECENT = _naive(_ago(1))
_OLD = _naive(_ago(30))


def _hit(point_id: str, score: float = 0.9, **metadata: Any) -> dict[str, Any]:
    return {
        "id": point_id,
        "score": score,
        "payload": {"text": f"text {point_id}", "metadata": metadata},
    }


def _found(*hits: dict[str, Any]) -> Answer:
    """The store's nearest points, as many as the request asks for."""
    return lambda body: httpx.Response(200, json={"result": list(hits)[: body["limit"]]})


def _status(code: int) -> Answer:
    return lambda body: httpx.Response(code, json={"status": {"error": f"error {code}"}})


def _ids(answer: dict[str, Any]) -> list[str]:
    return [item["id"] for item in answer["results"]]


# ── search: failures ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_store_that_refuses_the_search_is_an_error_not_an_empty_list(store):
    store.on(NEWS, "search", _status(400))

    with pytest.raises(HTTPException) as raised:
        await intel_analytics.search_intel(IntelSearchRequest(query="q", category="news"))

    assert raised.value.status_code == 500
    assert raised.value.detail == "Intel search unavailable"


@pytest.mark.asyncio
async def test_a_store_still_down_after_its_retries_is_an_error(store):
    store.on(NEWS, "search", _status(503))

    with pytest.raises(HTTPException) as raised:
        await intel_analytics.search_intel(IntelSearchRequest(query="q", category="news"))

    assert raised.value.status_code == 500
    assert len(store.collections("search")) == qdrant_db.MAX_RETRIES + 1


@pytest.mark.asyncio
async def test_one_failing_collection_of_two_gives_the_others_results_marked_partial(store):
    store.on(NEWS, "search", _found(_hit("n1", published_at=_RECENT)))
    store.on(VISA, "search", _status(400))

    answer = await intel_analytics.search_intel(IntelSearchRequest(query="q"))

    assert _ids(answer) == ["n1"]
    assert answer["partial"] is True


@pytest.mark.asyncio
async def test_an_embedder_failure_is_500_without_its_message(store, monkeypatch):
    monkeypatch.setattr(
        intel_analytics,
        "get_embedder",
        lambda: _Embedder(RuntimeError("connect to qdrant.internal:6333 refused")),
    )

    with pytest.raises(HTTPException) as raised:
        await intel_analytics.search_intel(IntelSearchRequest(query="q"))

    assert raised.value.status_code == 500
    assert raised.value.detail == "Intel search unavailable"
    assert store.calls == []


@pytest.mark.asyncio
async def test_a_default_search_reads_each_collection_once_with_no_filter(store):
    store.on(NEWS, "search", _found(_hit("n1", published_at=_RECENT)))
    store.on(VISA, "search", _found(_hit("v1", published_at=_RECENT)))

    answer = await intel_analytics.search_intel(IntelSearchRequest(query="q"))

    assert sorted(store.collections("search")) == sorted([NEWS, VISA])
    assert all("filter" not in body for body in store.bodies("search"))
    assert sorted(_ids(answer)) == ["n1", "v1"]
    assert "partial" not in answer


# ── search: a narrowed tier, an impact level and a category ──────────────────


@pytest.mark.asyncio
async def test_a_narrowed_tier_is_matched_in_the_route_and_never_sent(store):
    store.on(
        NEWS,
        "search",
        _found(_hit("t1", tier="T1"), _hit("t2", tier="T2"), _hit("no-tier")),
    )

    answer = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", category="news", tier=["T1"], date_range="all")
    )

    assert _ids(answer) == ["t1"]
    assert all("filter" not in body for body in store.bodies("search"))


@pytest.mark.asyncio
async def test_an_impact_level_is_matched_in_the_route_and_never_sent(store):
    store.on(
        NEWS,
        "search",
        _found(_hit("high", impact_level="high"), _hit("low", impact_level="low"), _hit("none")),
    )

    answer = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", category="news", impact_level="high", date_range="all")
    )

    assert _ids(answer) == ["high"]
    assert all("filter" not in body for body in store.bodies("search"))


@pytest.mark.asyncio
async def test_a_category_selects_its_collection_and_does_not_filter_the_stored_category(store):
    store.on(NEWS, "search", _found(_hit("events-item", category="events")))
    store.on(VISA, "search", _found(_hit("visa-item")))

    news = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", category="immigration", date_range="all")
    )
    assert store.collections("search") == [NEWS]
    assert "filter" not in store.bodies("search")[0]
    assert _ids(news) == ["events-item"]

    store.calls.clear()
    visa = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", category="visa", date_range="all")
    )
    assert store.collections("search") == [VISA]
    assert _ids(visa) == ["visa-item"]


# ── search: the publication date ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_publication_date_rule_inside_and_outside_seven_days(store, host_east_of_utc):
    cutoff = _ago(7)
    store.on(
        NEWS,
        "search",
        _found(
            _hit("naive-inside", published_at=_naive(cutoff + timedelta(hours=1))),
            _hit("naive-outside", published_at=_naive(cutoff - timedelta(hours=1))),
            _hit(
                "offset-inside",
                published_at=_ago(2).astimezone(timezone(timedelta(hours=8))).isoformat(),
            ),
            _hit("zulu-inside", published_at=_naive(_ago(3)) + "Z"),
            _hit("old", published_at=_OLD),
            _hit("invalid", published_at="last tuesday"),
            _hit("missing"),
            _hit("legacy-inside", published_date=_RECENT),
            _hit("at-wins-inside", published_at=_RECENT, published_date=_OLD),
            _hit("at-wins-outside", published_at=_OLD, published_date=_RECENT),
            _hit("indexed-only", indexed_at=_RECENT),
        ),
    )

    answer = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", category="news", limit=20)
    )

    assert sorted(_ids(answer)) == sorted(
        ["naive-inside", "offset-inside", "zulu-inside", "legacy-inside", "at-wins-inside"]
    )
    by_id = {item["id"]: item for item in answer["results"]}
    assert by_id["at-wins-inside"]["published_date"] == _RECENT
    assert by_id["legacy-inside"]["published_date"] == _RECENT


@pytest.mark.asyncio
async def test_an_undated_search_keeps_the_undated_and_the_old(store):
    store.on(
        NEWS,
        "search",
        _found(
            _hit("missing"), _hit("invalid", published_at="n/a"), _hit("old", published_at=_OLD)
        ),
    )

    answer = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", category="news", date_range="all")
    )

    assert sorted(_ids(answer)) == ["invalid", "missing", "old"]


# ── search: bounds and the window ────────────────────────────────────────────


def _many(prefix: str, count: int, top: float, **metadata: Any) -> list[dict[str, Any]]:
    return [_hit(f"{prefix}{i}", top - i * 0.001, **metadata) for i in range(count)]


@pytest.mark.asyncio
async def test_a_huge_limit_asks_the_store_for_at_most_200_and_answers_at_most_50(store):
    store.on(NEWS, "search", _found(*_many("n", 250, 0.9, published_at=_RECENT)))
    store.on(VISA, "search", _found(*_many("v", 250, 0.8, published_at=_RECENT)))

    answer = await intel_analytics.search_intel(IntelSearchRequest(query="q", limit=1_000_000))

    assert [body["limit"] for body in store.bodies("search")] == [200, 200]
    assert answer["total"] == len(answer["results"]) == 50


@pytest.mark.asyncio
async def test_with_no_route_filter_each_collection_is_asked_for_the_results_wanted(store):
    store.on(NEWS, "search", _found())
    store.on(VISA, "search", _found())

    await intel_analytics.search_intel(IntelSearchRequest(query="q", limit=7, date_range="all"))

    assert [body["limit"] for body in store.bodies("search")] == [7, 7]


@pytest.mark.asyncio
async def test_with_a_route_filter_each_collection_is_asked_for_five_times_as_many(store):
    store.on(NEWS, "search", _found())
    store.on(VISA, "search", _found())

    await intel_analytics.search_intel(IntelSearchRequest(query="q", limit=7))

    assert [body["limit"] for body in store.bodies("search")] == [35, 35]


@pytest.mark.asyncio
async def test_two_collections_together_fill_the_fifty(store):
    store.on(NEWS, "search", _found(*_many("n", 30, 0.95)))
    store.on(VISA, "search", _found(*_many("v", 30, 0.949)))

    answer = await intel_analytics.search_intel(
        IntelSearchRequest(query="q", limit=50, date_range="all")
    )

    ids = _ids(answer)
    assert len(ids) == 50
    assert any(i.startswith("n") for i in ids) and any(i.startswith("v") for i in ids)
    scores = [item["similarity_score"] for item in answer["results"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_window_full_is_said_only_when_a_full_window_lost_points_to_the_filter(store):
    request = IntelSearchRequest(
        query="q", category="news", limit=2, impact_level="high", date_range="all"
    )

    store.on(NEWS, "search", _found(_hit("h", impact_level="high"), *_many("x", 20, 0.5)))
    full_and_dropped = await intel_analytics.search_intel(request)
    assert store.bodies("search")[-1]["limit"] == 10
    assert full_and_dropped["window_full"] is True
    assert _ids(full_and_dropped) == ["h"]

    store.on(NEWS, "search", _found(_hit("h", impact_level="high"), *_many("x", 3, 0.5)))
    assert "window_full" not in await intel_analytics.search_intel(request)

    store.on(NEWS, "search", _found(*_many("h", 20, 0.5, impact_level="high")))
    assert "window_full" not in await intel_analytics.search_intel(request)


# ── search: the result mapping ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stored_article_maps_to_its_point_id_source_and_url(store):
    store.on(
        NEWS,
        "search",
        _found(
            _hit(
                "5d1c7e0a-0000-4000-8000-000000000001",
                title="An article",
                source_name="Bali Zero",
                source_url="https://example.test/a",
                tier="T1",
                category="news",
                published_at=_RECENT,
            )
        ),
    )

    answer = await intel_analytics.search_intel(IntelSearchRequest(query="q", category="news"))

    [item] = answer["results"]
    assert item["id"] == "5d1c7e0a-0000-4000-8000-000000000001"
    assert item["source"] == "Bali Zero"
    assert item["url"] == "https://example.test/a"
    assert item["published_date"] == _RECENT
    assert item["category"] == NEWS


@pytest.mark.asyncio
async def test_a_legacy_flat_point_maps_its_source_and_url(store):
    flat = {
        "id": 42,
        "score": 0.8,
        "payload": {
            "text": "legacy",
            "title": "Legacy",
            "source": "Old feed",
            "url": "https://example.test/old",
            "published_at": _RECENT,
        },
    }
    store.on(VISA, "search", _found(flat))

    answer = await intel_analytics.search_intel(IntelSearchRequest(query="q", category="visa"))

    [item] = answer["results"]
    assert (item["id"], item["source"], item["url"]) == (
        "42",
        "Old feed",
        "https://example.test/old",
    )


# ── critical ─────────────────────────────────────────────────────────────────


def _point(point_id: str, impact: str = "critical", **metadata: Any) -> dict[str, Any]:
    return {
        "id": point_id,
        "payload": {"text": "body", "metadata": {"impact_level": impact, **metadata}},
    }


def _scrolled(*points: dict[str, Any]) -> Answer:
    return lambda body: httpx.Response(
        200, json={"result": {"points": list(points), "next_page_offset": None}}
    )


def _raises(error: Exception) -> Answer:
    def answer(body: dict[str, Any]) -> httpx.Response:
        raise error

    return answer


@pytest.mark.asyncio
async def test_with_no_classified_collection_the_store_is_not_read_and_the_answer_says_so(store):
    answer = await intel_analytics.get_critical_items()

    assert store.calls == []
    assert answer["count"] == 0 and answer["items"] == answer["alerts"] == []
    assert answer["classified"] is False
    assert sorted(answer["unclassified"]) == sorted([NEWS, VISA])

    visa_only = await intel_analytics.get_critical_items(category="visa")
    assert store.calls == []
    assert visa_only["unclassified"] == [VISA]


@pytest.mark.asyncio
async def test_a_classified_collection_answers_its_critical_items_in_range(store, monkeypatch):
    monkeypatch.setattr(intel_analytics, "_CLASSIFIED_COLLECTIONS", frozenset({NEWS}))
    store.on(
        NEWS,
        "scroll",
        _scrolled(
            _point("recent", published_at=_RECENT, source_name="Src", source_url="https://x.test"),
            _point("old", published_at=_OLD),
            _point("undated"),
            _point("low", impact="low", published_at=_RECENT),
        ),
    )

    answer = await intel_analytics.get_critical_items(days=7)

    assert [item["id"] for item in answer["items"]] == ["recent"]
    assert answer["items"][0]["source"] == "Src"
    assert answer["items"][0]["url"] == "https://x.test"
    assert answer["unclassified"] == [VISA]
    assert "partial" not in answer
    assert store.collections("scroll") == [NEWS]
    assert '"metadata.impact_level"' in json.dumps(store.bodies("scroll")[0]["filter"])


@pytest.mark.asyncio
async def test_reading_past_the_bound_is_partial_not_a_failure(store, monkeypatch):
    monkeypatch.setattr(intel_analytics, "_CLASSIFIED_COLLECTIONS", frozenset({NEWS}))
    monkeypatch.setattr(intel_analytics, "_CRITICAL_MAX_POINTS", 2)
    store.on(
        NEWS,
        "scroll",
        lambda body: httpx.Response(
            200,
            json={
                "result": {
                    "points": [_point(f"p{i}", published_at=_RECENT) for i in range(body["limit"])],
                    "next_page_offset": "more",
                }
            },
        ),
    )

    answer = await intel_analytics.get_critical_items(category="news")

    assert answer["partial"] is True
    assert answer["count"] == 0


@pytest.mark.asyncio
async def test_a_refused_scroll_of_the_only_classified_collection_is_500(store, monkeypatch):
    monkeypatch.setattr(intel_analytics, "_CLASSIFIED_COLLECTIONS", frozenset({NEWS}))
    store.on(NEWS, "scroll", _status(400))

    with pytest.raises(HTTPException) as raised:
        await intel_analytics.get_critical_items()

    assert raised.value.status_code == 500
    assert raised.value.detail == "Critical items unavailable"


@pytest.mark.asyncio
async def test_an_unrelated_runtime_error_is_a_failure_not_an_overflow(store, monkeypatch):
    monkeypatch.setattr(intel_analytics, "_CLASSIFIED_COLLECTIONS", frozenset({NEWS, VISA}))
    store.on(NEWS, "scroll", _raises(RuntimeError("event loop is closed")))
    store.on(VISA, "scroll", _raises(RuntimeError("event loop is closed")))

    with pytest.raises(HTTPException) as raised:
        await intel_analytics.get_critical_items()
    assert raised.value.status_code == 500

    store.on(VISA, "scroll", _scrolled(_point("v", published_at=_RECENT)))
    answer = await intel_analytics.get_critical_items()

    assert [item["id"] for item in answer["items"]] == ["v"]
    assert answer["partial"] is True
    assert "classified" not in answer


# ── trends ───────────────────────────────────────────────────────────────────


def _info(points: int) -> Answer:
    return lambda body: httpx.Response(200, json={"result": {"points_count": points}})


@pytest.mark.asyncio
async def test_trends_read_each_collection_once(store):
    store.on(NEWS, "info", _info(3513))
    store.on(VISA, "info", _info(90))

    answer = await intel_analytics.get_trends()

    assert sorted(store.collections("info")) == sorted([NEWS, VISA])
    assert sorted(t["total_items"] for t in answer["trends"]) == [90, 3513]


@pytest.mark.asyncio
async def test_trends_leave_out_a_failed_collection_and_fail_when_every_read_fails(store):
    store.on(NEWS, "info", _info(3513))
    store.on(VISA, "info", _status(503))

    answer = await intel_analytics.get_trends()
    assert [t["total_items"] for t in answer["trends"]] == [3513]

    store.on(NEWS, "info", _status(503))
    with pytest.raises(HTTPException) as raised:
        await intel_analytics.get_trends()
    assert raised.value.status_code == 500
    assert raised.value.detail == "Intel trends unavailable"
