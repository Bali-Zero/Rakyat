"""Typed search text never reaches Sentry: the query string and fragment of every URL are dropped.

Rule under test: in an event, a URL-shaped string keeps its PATH only (the query and the fragment
are cut, not parsed), structured query-string fields are dropped whole, and dict params named
search / q lose their text value. Guilt cases go through the real SDK with the repo's hooks and an
in-memory transport; innocence cases show that paths and non-URL fields are untouched.
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
    _redact_string,
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
    f"page=2&%51={VALUE}",
    f"x=1#q={VALUE}",
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


def _app() -> TestClient:
    app = FastAPI()

    @app.get("/api/crm/practices")
    async def practices(search: str | None = None, q: str | None = None):
        sentry_sdk.add_breadcrumb(
            category="httplib",
            data={"url": f"http://internal/api/crm/practices?month=2026-11&search={VALUE}"},
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
    res = _app().get("/api/crm/practices", params={"search": VALUE, "q": VALUE})
    assert res.status_code == 500
    blob = _assert_clean(transport)
    assert "practices failed" in blob
    assert "/api/crm/practices" in blob  # the path stays


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
def test_a_referer_header_keeps_only_its_path(transport, query):
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        raise RuntimeError("ping failed")

    res = TestClient(app, raise_server_exceptions=False).get(
        "/api/ping", headers={"Referer": f"https://kita.example/clients?{query}"}
    )
    assert res.status_code == 500
    blob = _assert_clean(transport)
    assert "ping failed" in blob
    assert "https://kita.example/clients" in blob


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
def test_a_url_in_a_log_breadcrumb_keeps_only_its_path(transport, query):
    logging.getLogger("zantara.backend").info(f"navigating to https://kita.example/p?{query}")
    try:
        raise RuntimeError("after breadcrumb")
    except RuntimeError:
        sentry_sdk.capture_exception()
    blob = _assert_clean(transport)
    assert "after breadcrumb" in blob
    assert "https://kita.example/p" in blob


@pytest.mark.parametrize("query", HOSTILE_QUERIES)
def test_urls_in_any_string_lose_query_and_fragment(query):
    out = _redact_string(f"GET http://x/api/crm/companies?{query} -> 200")
    assert VALUE not in out and TAIL not in out
    assert out.startswith("GET http://x/api/crm/companies")
    assert "200" in out
    assert _redact_string(f"/relative/path?{query}") == "/relative/path"


def test_structured_query_fields_are_dropped_whole():
    out = _scrub(
        {
            "request": {"query_string": f"q=prefix;{TAIL}&month=2026-11"},
            "data": {
                "http.query": f"search={VALUE}",
                "url.query": f"q={VALUE}",
                "http.fragment": "",
            },
        }
    )
    assert out["request"]["query_string"] == PII_REDACTION_PLACEHOLDER
    assert out["data"]["http.query"] == PII_REDACTION_PLACEHOLDER
    assert out["data"]["url.query"] == PII_REDACTION_PLACEHOLDER


def test_dict_params_named_search_are_redacted():
    out = _scrub({"query_params": {"search": VALUE, "q": VALUE, "month": "2026-11"}})
    assert out["query_params"] == {
        "search": PII_REDACTION_PLACEHOLDER,
        "q": PII_REDACTION_PLACEHOLDER,
        "month": "2026-11",
    }


def test_innocence_paths_and_non_url_fields_are_untouched():
    assert _redact_string("https://x/api/crm/practices/12/documents") == (
        "https://x/api/crm/practices/12/documents"
    )
    assert _redact_string("worker restarted after 3 retries, state=ok") == (
        "worker restarted after 3 retries, state=ok"
    )
    assert _redact_string("issue #123 and q-learning, search results") == (
        "issue #123 and q-learning, search results"
    )
    assert _scrub({"search_results": 3, "quota": "ok", "search": 3}) == {
        "search_results": 3,
        "quota": "ok",
        "search": 3,
    }
