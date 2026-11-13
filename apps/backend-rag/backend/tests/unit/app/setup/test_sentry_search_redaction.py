"""Typed search text never reaches Sentry: events are cleaned by FIELD, not by regex over text.

Rules under test (STRUCTURED FIELDS ONLY): query-string fields are replaced, URL-valued fields
keep their path only, the Referer / Link / Location request headers are dropped, dict params named
search / q lose their text value. Free text (exception and log messages, SQL, stack frames) is NOT
touched by any URL cutter. Guilt cases go through the real SDK with the repo's hooks and an
in-memory transport; the rest call the hook on synthetic events.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from backend.app.setup.sentry_config import (
    PII_REDACTION_PLACEHOLDER,
    _before_send,
    _before_send_transaction,
    _scrub,
)

VALUE = "zetaquorum-fixture-search"
TAIL = "zetaquorum-fixture-tail"

# Every shape a search value can take after the "?" or "#" of a URL.
HOSTILE_QUERIES = [
    f"search={VALUE}",
    f"q={VALUE}&page=2",
    f"se%61rch={VALUE}",
    f"%73EARCH={VALUE}",
    f"a=1;q={VALUE}",
    f"q=prefix;{TAIL}",
    f"q=prefix%3B{TAIL}",
    f"q=a%26b%3D{TAIL}",
    f"q=prefix {TAIL}",
    f'q=prefix"{TAIL}',
    f"x=1#q={VALUE}",
]
# URL ends the old cutters could not see.
HOSTILE_URLS = [f"https://kita.example/clients{end}?q={VALUE}" for end in ("", "]", ")", ":")] + [
    f"https://kita.example/clients?{q}" for q in HOSTILE_QUERIES
]


class MemoryTransport(Transport):
    def __init__(self, options: Any = None) -> None:
        super().__init__(options)
        self.payloads: list[str] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            self.payloads.append(item.payload.get_bytes().decode("utf-8", "replace"))


@pytest.fixture
def transport():
    t = MemoryTransport()
    sentry_sdk.init(
        dsn="https://public@o0.ingest.sentry.io/1",
        transport=t,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        include_local_variables=False,
        send_default_pii=False,
        traces_sample_rate=0.0,
        max_request_body_size="never",
    )
    yield t
    sentry_sdk.get_client().close()
    sentry_sdk.init()


def _failing_app() -> TestClient:
    app = FastAPI()

    @app.get("/api/crm/practices")
    async def practices(search: str | None = None, q: str | None = None):
        sentry_sdk.add_breadcrumb(
            category="httplib",
            data={
                "url": f"http://internal/api/crm/practices?month=2026-11&search={VALUE}",
                "http.query": f"month=2026-11&search={VALUE}",
                "http.fragment": "",
            },
        )
        raise RuntimeError("practices failed")

    return TestClient(app, raise_server_exceptions=False)


def _assert_clean(transport: MemoryTransport) -> str:
    sentry_sdk.flush()
    assert transport.payloads, "the failure must produce an event, or this proves nothing"
    blob = json.dumps(transport.payloads)
    assert VALUE not in blob and TAIL not in blob
    return blob


def test_a_failing_search_request_sends_no_search_value_to_sentry(transport):
    res = _failing_app().get("/api/crm/practices", params={"search": VALUE, "q": VALUE})
    assert res.status_code == 500
    blob = _assert_clean(transport)
    assert "practices failed" in blob
    assert "/api/crm/practices" in blob  # the path stays


@pytest.mark.parametrize("header", ["Referer", "Link", "Location"])
@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_url_headers_never_reach_the_event(transport, header, url):
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        raise RuntimeError("ping failed")

    res = TestClient(app, raise_server_exceptions=False).get("/api/ping", headers={header: url})
    assert res.status_code == 500
    blob = _assert_clean(transport)
    assert "ping failed" in blob


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_url_valued_fields_keep_their_path_only(url):
    event = {
        "request": {"url": url, "query_string": "q=x", "method": "GET"},
        "breadcrumbs": {"values": [{"category": "httplib", "data": {"url": url, "http.url": url}}]},
        "contexts": {"trace": {"data": {"url.full": url, "url.query": "q=x"}}},
        "spans": [
            {"op": "http.client", "description": f"GET {url}", "data": {"http.query": "q=x"}}
        ],
    }
    out = _before_send(event, {})
    blob = json.dumps(out)
    assert VALUE not in blob and TAIL not in blob
    assert "https://kita.example/clients" in blob
    assert out["request"]["query_string"] == PII_REDACTION_PLACEHOLDER
    assert out["spans"][0]["description"].startswith("GET https://kita.example/clients")


def test_headers_given_as_pairs_are_filtered_too():
    event = {
        "request": {
            "headers": [["Referer", f"https://x/p?q={VALUE}"], ["Accept", "text/html"]],
        }
    }
    assert _before_send(event, {})["request"]["headers"] == [["Accept", "text/html"]]


def test_an_unparsable_url_becomes_the_placeholder_and_never_raises():
    out = _before_send({"request": {"url": "http://[bad?q=" + VALUE}}, {})
    assert VALUE not in json.dumps(out)


def test_dict_params_named_search_are_redacted():
    out = _scrub({"query_params": {"search": VALUE, "q": VALUE, "month": "2026-11"}})
    assert out["query_params"] == {
        "search": PII_REDACTION_PLACEHOLDER,
        "q": PII_REDACTION_PLACEHOLDER,
        "month": "2026-11",
    }


@pytest.fixture
def traced_transport():
    """Same hooks as production, but with transaction sampling ON (the plain fixture turns it
    off). Guards the transaction hook: the SDK sends transactions only through it."""
    t = MemoryTransport()
    sentry_sdk.init(
        dsn="https://public@o0.ingest.sentry.io/1",
        transport=t,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        include_local_variables=False,
        send_default_pii=False,
        traces_sample_rate=1.0,
        max_request_body_size="never",
    )
    yield t
    sentry_sdk.get_client().close()
    sentry_sdk.init()


@pytest.mark.parametrize("header", ["Referer", "Link", "Location"])
def test_a_transaction_carries_no_search_value_and_no_url_header(traced_transport, header):
    app = FastAPI()

    @app.get("/api/crm/practices")
    async def ok(search: str | None = None):
        return {"ok": True}

    res = TestClient(app).get(
        "/api/crm/practices",
        params={"search": VALUE},
        headers={header: f"https://kita.example/clients?q=prefix;{TAIL}&search={VALUE}"},
    )
    assert res.status_code == 200
    sentry_sdk.flush()
    transactions = [p for p in traced_transport.payloads if '"type":"transaction"' in p]
    assert transactions, "a sampled request must send a transaction, or this proves nothing"
    blob = json.dumps(transactions)
    assert VALUE not in blob and TAIL not in blob
    assert "/api/crm/practices" in blob  # the path stays


def test_the_transaction_hook_scrubs_and_is_not_an_identity():
    event = {
        "type": "transaction",
        "transaction": "/api/crm/practices",
        "request": {
            "url": f"http://x/api/crm/practices?search={VALUE}",
            "query_string": f"search={VALUE}",
            "headers": {"Referer": f"https://kita.example/p?q={VALUE}", "Accept": "a"},
        },
    }
    out = _before_send_transaction(event, {})
    assert out is not None and out is not event
    assert VALUE not in json.dumps(out)
    assert out["request"]["headers"] == {"Accept": "a"}


# --- innocence: free text and diagnostics are NOT touched by any URL cutter ------------------


@pytest.mark.parametrize(
    "text",
    [
        "Foo#bar",
        "Ready? retry now",
        "SELECT metadata#>>'{a}' FROM t WHERE x = $1",
        "issue #123 and q-learning, search results",
        "worker restarted after 3 retries, state=ok",
        "https://x/api/crm/practices/12/documents",
    ],
)
def test_innocence_free_text_is_left_alone(text):
    event = {
        "message": text,
        "exception": {"values": [{"type": "ValueError", "value": text}]},
        "logentry": {"message": text},
    }
    out = _before_send(event, {})
    assert out["message"] == text
    assert out["exception"]["values"][0]["value"] == text
    assert out["logentry"]["message"] == text


def test_innocence_a_stack_trace_and_ordinary_fields_survive(transport):
    app = FastAPI()

    @app.get("/api/boom")
    async def boom():
        raise ValueError("Ready? retry now; Foo#bar")

    res = TestClient(app, raise_server_exceptions=False).get("/api/boom")
    assert res.status_code == 500
    sentry_sdk.flush()
    blob = json.dumps(transport.payloads)
    assert "Ready? retry now; Foo#bar" in blob
    assert "stacktrace" in blob


def test_innocence_a_log_message_is_not_rewritten(transport):
    logging.getLogger("zantara.backend").info("fetched /api/ping?ready=1 ok")
    try:
        raise RuntimeError("after breadcrumb")
    except RuntimeError:
        sentry_sdk.capture_exception()
    sentry_sdk.flush()
    assert "after breadcrumb" in json.dumps(transport.payloads)


def test_innocence_non_query_fields_named_like_urls_are_untouched():
    out = _scrub({"search_results": 3, "quota": "ok", "search": 3})
    assert out == {"search_results": 3, "quota": "ok", "search": 3}
