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
    def __init__(self, httpx_crumb: bool = False) -> None:
        self.httpx_crumb = httpx_crumb

    async def search(self, **kwargs: Any) -> Any:
        if self.httpx_crumb:
            # An HTTPX-kind breadcrumb carrying the sentinel (the vector store's request path).
            # Spec amendment A1 puts breadcrumbs of non-logging integrations OUT of scope: the
            # test below must neither depend on them nor assert anything about them.
            sentry_sdk.add_breadcrumb(
                type="http",
                category="httplib",
                data={
                    "url": f"http://vector.invalid/collections/{kwargs['collection_override']}/points/search"
                },
            )
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


def _is_out_crumb(crumb: dict[str, Any]) -> bool:
    """Spec amendment A3.1: a breadcrumb is OUT if and only if its type is not "log" (the
    marker of the logging integration). Never by category, logger name or message text."""
    return crumb.get("type") != "log"


def query_leaks(events: list[dict[str, Any]], sentinels: list[str]) -> list[str]:
    """The oracle (A3.2). Reports where a sentinel shows up in what the spec promises:
    1. the COMPLETE payload of every logging-integration event (one carrying `logentry`),
       minus the OUT breadcrumbs only;
    2. a framework exception event's exception values, `message` and `logentry` only;
    3. every `type == "log"` breadcrumb, in any event."""

    def hit(value: Any) -> bool:
        blob = json.dumps(value, default=str)
        return any(sentinel in blob for sentinel in sentinels)

    found: list[str] = []
    for event in events:
        crumbs = event.get("breadcrumbs", {}).get("values", [])
        if "logentry" in event:
            kept = {k: v for k, v in event.items() if k != "breadcrumbs"}
            kept["breadcrumbs"] = [c for c in crumbs if not _is_out_crumb(c)]
            if hit(kept):
                found.append("logging event")
        elif any(
            v.get("type") == "HTTPException" for v in event.get("exception", {}).get("values", [])
        ):
            for field in ("exception", "message", "logentry"):
                if field in event and hit(event[field]):
                    found.append(f"framework event {field}")
        found += ["log breadcrumb" for c in crumbs if not _is_out_crumb(c) and hit(c)]
    return found


# --- the oracle on planted payloads (A3.3), one case per bullet --------------------------------

PLANT = "zq-planted-sentinel"


def _logging_event(**fields: Any) -> dict[str, Any]:
    return {"logentry": {"message": "clean"}, "logger": "app", **fields}


def _crumb(type_: str, category: str) -> dict[str, Any]:
    return {"type": type_, "category": category, "message": f"carries {PLANT}"}


def _with_crumb(crumb: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    return {**event, "breadcrumbs": {"values": [crumb]}}


def test_oracle_reports_a_log_breadcrumb_even_with_an_httpx_category() -> None:
    event = _with_crumb(_crumb("log", "httpx"), {"exception": {"values": []}, "message": "x"})
    assert query_leaks([event], [PLANT]) == ["log breadcrumb"]


def test_oracle_reports_the_sentinel_nested_in_a_logging_events_extra() -> None:
    event = _logging_event(extra={"a": [{"b": PLANT}]})
    assert query_leaks([event], [PLANT]) == ["logging event"]


def test_oracle_reports_the_sentinel_in_a_logging_events_logger() -> None:
    assert query_leaks([_logging_event(logger=f"vendor.{PLANT}")], [PLANT]) == ["logging event"]


def test_oracle_ignores_an_http_breadcrumb_even_inside_a_logging_event() -> None:
    plain = {"exception": {"values": []}, "message": "x"}
    assert query_leaks([_with_crumb(_crumb("http", "httplib"), plain)], [PLANT]) == []
    assert query_leaks([_with_crumb(_crumb("http", "httplib"), _logging_event())], [PLANT]) == []


def test_oracle_ignores_a_framework_event_field_other_than_exception_message_logentry() -> None:
    framework = {
        "exception": {"values": [{"type": "HTTPException", "value": "Search failed"}]},
        "message": "Search failed",
        "request": {"url": f"http://x/{PLANT}"},
        "tags": {"t": PLANT},
    }
    assert query_leaks([framework], [PLANT]) == []
    framework["message"] = PLANT
    assert query_leaks([framework], [PLANT]) == ["framework event message"]


def test_oracle_reports_nothing_for_a_clean_payload() -> None:
    clean = _with_crumb(_crumb("log", "app"), _logging_event())
    clean["breadcrumbs"]["values"][0]["message"] = "clean"
    assert query_leaks([clean], [PLANT]) == []


@pytest.mark.parametrize(
    "httpx_crumb", [False, True], ids=["plain", "httpx-breadcrumb-out-of-scope"]
)
def test_the_failing_route_sends_events_and_breadcrumbs_free_of_the_query(
    transport: MemoryTransport, caplog: pytest.LogCaptureFixture, httpx_crumb: bool
) -> None:
    query = f"{SENT} kueri tentang visa dan pajak untuk perusahaan sintetis"
    with caplog.at_level(logging.DEBUG):
        resp = _client(_FailingService(httpx_crumb)).post(
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
        assert "Search failed" in str(values[0]["value"])

    logging_events = [e for e in events if "logentry" in e]
    vendor = [
        e
        for e in logging_events
        if e.get("logger") == FOREIGN.name and "vendor call failed" in json.dumps(e["logentry"])
    ]
    assert vendor, "no genuine logging-integration event (logentry) for the in-scope ERROR record"
    for event in vendor:
        assert "[query]" in json.dumps(event["logentry"]), "the redaction placeholder is missing"
    crumbs = [c for e in events for c in e.get("breadcrumbs", {}).get("values", [])]
    assert any(
        not _is_out_crumb(c) and "Received query: query_len=" in str(c.get("message"))
        for c in crumbs
    ), "no logging breadcrumb from the request's INFO lines"

    assert query_leaks(events, [SENT, COLL]) == []


def test_a_422_sends_no_event_carrying_the_query(transport: MemoryTransport) -> None:
    query = f"{SENT} kueri dengan limit di luar batas"
    resp = _client(_FailingService()).post(
        "/api/search", json={"query": query, "level": 3, "limit": 999}
    )
    assert resp.status_code == 422
    sentry_sdk.flush()
    assert SENT not in json.dumps(transport.payloads)
