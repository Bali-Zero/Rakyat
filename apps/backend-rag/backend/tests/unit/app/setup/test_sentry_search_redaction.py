"""Typed search text never reaches Sentry: query string, breadcrumb URLs, dict params.

Guilt: a failing `/api/crm/practices?search=<value>` request, captured by the real SDK with the
repo's hooks and an in-memory transport, must not carry the value anywhere in the envelope.
Innocence: other query params, other URLs and `q`-like words stay readable.
"""

from __future__ import annotations

import json
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
    _redact_query_string,
    _redact_string,
    _scrub,
)

VALUE = "zetaquorum-fixture-search"


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


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/api/crm/practices")
    async def practices(search: str | None = None, q: str | None = None, month: str | None = None):
        sentry_sdk.add_breadcrumb(
            category="httplib",
            data={"url": f"http://internal/api/crm/practices?month=2026-11&search={VALUE}"},
        )
        raise RuntimeError("practices failed")

    return TestClient(app, raise_server_exceptions=False)


def test_a_failing_search_request_sends_no_search_value_to_sentry(transport):
    res = _client().get("/api/crm/practices", params={"search": VALUE, "month": "2026-11"})
    assert res.status_code == 500
    sentry_sdk.flush()
    assert transport.payloads, "the failure must produce an event, or this proves nothing"
    blob = json.dumps(transport.payloads)
    assert "practices failed" in blob
    assert VALUE not in blob
    assert "month=2026-11" in blob or "2026-11" in blob  # other params stay readable


def test_the_q_param_is_redacted_the_same_way(transport):
    _client().get("/api/crm/practices", params={"q": VALUE})
    sentry_sdk.flush()
    assert VALUE not in json.dumps(transport.payloads)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"search={VALUE}&month=2026-11", f"search={PII_REDACTION_PLACEHOLDER}&month=2026-11"),
        (f"month=2026-11&Q={VALUE}", f"month=2026-11&Q={PII_REDACTION_PLACEHOLDER}"),
        (f"se%61rch={VALUE}", f"se%61rch={PII_REDACTION_PLACEHOLDER}"),
        (f" search ={VALUE}", f" search ={PII_REDACTION_PLACEHOLDER}"),
    ],
)
def test_query_string_values_are_redacted_by_key(raw, expected):
    assert _redact_query_string(raw) == expected


def test_urls_in_free_text_lose_the_search_value():
    url = f"GET http://x/api/crm/companies?limit=20&search={VALUE}&status=active done"
    out = _redact_string(url)
    assert VALUE not in out
    assert "limit=20" in out and "status=active" in out
    assert f"?q={VALUE}" not in _redact_string(f"http://x/p?q={VALUE}")


def test_dict_params_named_search_are_redacted():
    out = _scrub({"query_params": {"search": VALUE, "q": VALUE, "month": "2026-11"}})
    assert out["query_params"] == {
        "search": PII_REDACTION_PLACEHOLDER,
        "q": PII_REDACTION_PLACEHOLDER,
        "month": "2026-11",
    }


def test_innocence_look_alike_words_and_values_stay_readable():
    assert _redact_query_string("month=2026-11&status=active") == "month=2026-11&status=active"
    text = "researching https://x/y?research=1&frequency=2 and q-learning, search results"
    assert _redact_string(text) == text
    assert _scrub({"search_results": 3, "quota": "ok"}) == {"search_results": 3, "quota": "ok"}
    assert _scrub({"search": 3}) == {"search": 3}  # only text values are search text


def test_percent_encoded_and_mixed_case_keys_in_urls_are_redacted():
    for url in (
        f"https://kita.example/clients?se%61rch={VALUE}",
        f"https://kita.example/clients?%73EARCH={VALUE}&page=2",
        f"https://kita.example/p?a=1;q={VALUE}",
        f"https://kita.example/p?page=2&%51={VALUE}",
    ):
        assert VALUE not in _redact_string(url), url
    assert "page=2" in _redact_string(f"https://x/p?%51={VALUE}&page=2")


def test_a_referer_with_an_encoded_search_key_never_reaches_the_envelope(transport):
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        raise RuntimeError("ping failed")

    res = TestClient(app, raise_server_exceptions=False).get(
        "/api/ping",
        headers={"Referer": f"https://kita.example/clients?se%61rch={VALUE}"},
    )
    assert res.status_code == 500
    sentry_sdk.flush()
    blob = json.dumps(transport.payloads)
    assert "ping failed" in blob
    assert VALUE not in blob


def test_an_encoded_url_in_a_log_breadcrumb_is_redacted(transport):
    import logging

    logging.getLogger("zantara.backend").info(
        f"navigating to https://kita.example/p?%73earch={VALUE}&page=2"
    )
    try:
        raise RuntimeError("after breadcrumb")
    except RuntimeError:
        sentry_sdk.capture_exception()
    sentry_sdk.flush()
    blob = json.dumps(transport.payloads)
    assert "after breadcrumb" in blob
    assert VALUE not in blob
