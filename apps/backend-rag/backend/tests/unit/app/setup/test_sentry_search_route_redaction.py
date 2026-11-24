"""Sentry never receives the /api/search query text, through the logging integration or the
framework exception event, when the SDK is initialised by the repo's real initializer.

`_init_sentry_blocking` is the production initializer: its options are forwarded untouched and
only a memory transport is injected (synchronous). The framework event is the HTTPException the
route raises after the `except` blocks (fixed detail, no chain); the logging event and the
breadcrumbs come from log records created inside the request scope. Synthetic sentinels only.
HTTPX breadcrumbs of outbound requests are out of scope (spec amendment A1)."""

from __future__ import annotations

import importlib
import json
import logging
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from backend.app.setup import sentry_config
from backend.app.setup.exception_handlers import register_exception_handlers

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

SENT = "zq-sentinel-7f3a91c2"
COLL = "zq-sentinel-coll-5d2e"
FOREIGN = logging.getLogger("backend.fake.vendor_client")


class MemoryTransport(Transport):
    def __init__(self, options: Any = None) -> None:
        super().__init__(options)
        self.payloads: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            raw = item.payload.get_bytes().decode("utf-8", "replace")
            try:
                self.payloads.append(json.loads(raw))
            except ValueError:
                self.payloads.append({"raw": raw})


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    memory = MemoryTransport()
    real_init = sentry_sdk.init

    def init_with_memory_transport(**options: Any) -> Any:
        return real_init(transport=memory, **options)

    monkeypatch.setattr(sentry_sdk, "init", init_with_memory_transport)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SENTRY_TRACES_SAMPLE_RATE", raising=False)
    monkeypatch.delenv("SENTRY_SEND_DEFAULT_PII", raising=False)
    sentry_config._init_sentry_blocking("https://public@o0.ingest.sentry.io/1")
    sentry_sdk.get_isolation_scope().clear_breadcrumbs()
    sentry_sdk.get_current_scope().clear_breadcrumbs()
    yield memory
    sentry_sdk.get_client().close()
    sentry_sdk.get_isolation_scope().clear_breadcrumbs()
    sentry_sdk.get_current_scope().clear_breadcrumbs()


class _FailingService:
    async def search(self, **kwargs: Any) -> Any:
        try:
            raise ValueError(f"vendor quoted {kwargs['query']} / {kwargs['collection_override']}")
        except ValueError:
            FOREIGN.error("vendor call failed for %s", kwargs["query"], exc_info=True)
        raise RuntimeError(
            f"embedder rejected {kwargs['query']} in {kwargs['collection_override']}"
        )


def _client(service: Any) -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(knowledge_router.router)
    app.state.search_service = service
    return TestClient(app, raise_server_exceptions=False)


def _events(transport: MemoryTransport) -> list[dict[str, Any]]:
    sentry_sdk.flush()
    return [p for p in transport.payloads if "exception" in p or "logentry" in p or "message" in p]


def test_the_failing_route_sends_events_and_breadcrumbs_free_of_the_query(
    transport: MemoryTransport, caplog: pytest.LogCaptureFixture
) -> None:
    query = f"{SENT} kueri tentang visa dan pajak untuk perusahaan sintetis"
    with caplog.at_level(logging.DEBUG):
        resp = _client(_FailingService()).post(
            "/api/search", json={"query": query, "collection": COLL, "level": 3, "limit": 3}
        )
    assert resp.status_code == 500 and resp.json()["detail"] == "Search failed"
    events = _events(transport)
    assert events, "Sentry received nothing: the guard would be vacuous"

    framework = [
        e
        for e in events
        if any(v.get("type") == "HTTPException" for v in e.get("exception", {}).get("values", []))
    ]
    assert framework, "no framework exception event for the route's HTTPException"
    for event in framework:
        values = event["exception"]["values"]
        assert [v["type"] for v in values] == ["HTTPException"], "an exception chain was sent"
        assert values[0]["value"] == "Search failed" or "Search failed" in str(values[0]["value"])

    logging_events = [e for e in events if "vendor call failed" in json.dumps(e)]
    assert logging_events, "no logging-integration event for the in-scope ERROR record"
    breadcrumbs = [crumb for e in events for crumb in e.get("breadcrumbs", {}).get("values", [])]
    assert any("Received query: query_len=" in str(c.get("message")) for c in breadcrumbs), (
        "no logging breadcrumb from the request's INFO lines"
    )

    blob = json.dumps(transport.payloads)
    assert SENT not in blob and COLL not in blob


def test_a_422_sends_no_event_carrying_the_query(transport: MemoryTransport) -> None:
    query = f"{SENT} kueri dengan limit di luar batas"
    resp = _client(_FailingService()).post(
        "/api/search", json={"query": query, "level": 3, "limit": 999}
    )
    assert resp.status_code == 422
    sentry_sdk.flush()
    assert SENT not in json.dumps(transport.payloads)
