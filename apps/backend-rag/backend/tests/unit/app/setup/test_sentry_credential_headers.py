"""A request header that carries a credential never leaves the process with its value.

The hook that cleans an event dropped the URL-valued headers and passed every other request
header through. The SDK's own filter knows a handful of names; this backend reads one header
per integration. The rule under test reads the words of a header's name. It also covers the
cookies the SDK copies beside the headers and a key spelled like such a header anywhere in an
event. A credential VALUE that sits under another name or inside a text is outside this rule.
Most cases call the hook on an event of the SDK's shape; the last ones send a real request through a FastAPI app
with the SDK initialised on an in-memory transport, with and without `send_default_pii`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import sentry_sdk
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport
from starlette.websockets import WebSocketDisconnect

from backend.app.setup.sentry_config import (
    PII_REDACTION_PLACEHOLDER,
    _before_send,
    _before_send_transaction,
)

SENTINEL = "zz-sentinel-header-value"
PLAIN = "zz-plain-value"

# Names this backend reads a credential from, plus the standard ones.
CREDENTIAL_HEADERS = [
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-crm-write-key",
    "x-internal-key",
    "x-debug-key",
    "x-workspace-marketing-key",
    "x-wa-bot-profile-key",
    "x-bridge-auth",
    "x-bridge-skills-auth",
    "x-admin-secret",
    "x-producer-token",
    "x-asset-upload-token",
    "x-csrf-token",
    "x-callback-token",
    "x-hub-signature-256",
    "x-twitter-webhooks-signature",
    "x-session-id",
    # compact spellings: the same words with no separator between them
    "xapikey",
    "apikey",
    "bearer",
    "x-csrftoken",
    "csrftoken",
    "x-xsrf-token",
    "sessionid",
    "x-authtoken",
    "x-amz-security-token",
    # the WebSocket handshake header: this backend's /ws takes "bearer.<token>" as a subprotocol
    "sec-websocket-protocol",
]
# Names that hold no credential. Four of them hold a credential word INSIDE a longer word, and
# two are the named exceptions.
PLAIN_HEADERS = [
    "content-type",
    "content-length",
    "user-agent",
    "accept",
    "accept-encoding",
    "accept-language",
    "cache-control",
    "host",
    "origin",
    "retry-after",
    "if-none-match",
    "last-event-id",
    "traceparent",
    "baggage",
    "sentry-trace",
    "strict-transport-security",
    "www-authenticate",
    "x-forwarded-proto",
    "x-request-id",
    "x-correlation-id",
    "x-restli-id",
    "idempotency-key",
    "x-idempotency-key",
    "sec-websocket-key",
    "x-keyboard-layout",
    "x-tokenizer-version",
    "x-authorship",
]
SPELLINGS = {
    "as written": lambda name: name,
    "capitalised": lambda name: "-".join(part.capitalize() for part in name.split("-")),
    "upper case": lambda name: name.upper(),
    "underscores": lambda name: name.replace("-", "_"),
}


def _event(headers: Any) -> dict[str, Any]:
    return {
        "level": "error",
        "logentry": {"message": "a route failed"},
        "request": {
            "method": "POST",
            "url": "https://api.example.test/api/anything",
            "headers": headers,
        },
    }


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("name", CREDENTIAL_HEADERS)
def test_a_header_named_as_a_credential_keeps_its_name_and_loses_its_value(
    name: str, spelling: str
) -> None:
    written = SPELLINGS[spelling](name)

    cleaned = _before_send(_event({written: SENTINEL, "User-Agent": PLAIN}), {})

    assert cleaned is not None
    assert cleaned["request"]["headers"] == {
        written: PII_REDACTION_PLACEHOLDER,
        "User-Agent": PLAIN,
    }
    assert SENTINEL not in json.dumps(cleaned)


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("name", PLAIN_HEADERS)
def test_a_header_that_carries_no_credential_keeps_its_value(name: str, spelling: str) -> None:
    written = SPELLINGS[spelling](name)

    cleaned = _before_send(_event({written: PLAIN}), {})

    assert cleaned is not None
    assert cleaned["request"]["headers"] == {written: PLAIN}


@pytest.mark.parametrize("outer", [list, tuple], ids=["in a list", "in a tuple"])
@pytest.mark.parametrize("pair", [list, tuple], ids=["lists", "tuples"])
def test_headers_given_as_pairs_are_cleaned_the_same_way(pair: type, outer: type) -> None:
    headers = outer(
        [
            pair(["X-Crm-Write-Key", SENTINEL]),
            pair(["Referer", "https://kita.example.test/clients?q=zz"]),
            pair(["User-Agent", PLAIN]),
            "not a pair",
        ]
    )

    cleaned = _before_send(_event(headers), {})

    assert cleaned is not None
    assert cleaned["request"]["headers"] == outer(
        [
            pair(["X-Crm-Write-Key", PII_REDACTION_PLACEHOLDER]),
            pair(["User-Agent", PLAIN]),
            "not a pair",
        ]
    )


def test_headers_nested_anywhere_in_the_event_are_cleaned() -> None:
    event = _event({"User-Agent": PLAIN})
    event["extra"] = {"upstream": {"headers": {"X-Internal-Key": SENTINEL, "Accept": PLAIN}}}

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["extra"]["upstream"]["headers"] == {
        "X-Internal-Key": PII_REDACTION_PLACEHOLDER,
        "Accept": PLAIN,
    }


def test_a_transaction_is_cleaned_like_an_error_event() -> None:
    event = _event({"x-crm-write-key": SENTINEL, "accept": PLAIN})
    event["type"] = "transaction"
    event["transaction"] = "/api/anything"

    cleaned = _before_send_transaction(event, {})

    assert cleaned is not None
    assert cleaned["request"]["headers"] == {
        "x-crm-write-key": PII_REDACTION_PLACEHOLDER,
        "accept": PLAIN,
    }


# ── cookies ───────────────────────────────────────────────────────────────────

SECOND = "zz-second-sentinel-value"


def test_every_cookie_keeps_its_name_and_loses_its_value() -> None:
    event = _event({"User-Agent": PLAIN})
    event["request"]["cookies"] = {
        "garuda_session": SENTINEL,
        "garuda_result_session": SECOND,
        "nz_access_token": "zz-third-sentinel-value",
        "theme": "zz-dark-mode-value",
    }

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["request"]["cookies"] == dict.fromkeys(
        ["garuda_session", "garuda_result_session", "nz_access_token", "theme"],
        PII_REDACTION_PLACEHOLDER,
    )
    assert "sentinel" not in json.dumps(cleaned)
    assert "zz-dark-mode-value" not in json.dumps(cleaned)


@pytest.mark.parametrize(
    ("cookies", "expected"),
    [
        (f"garuda_session={SENTINEL}; theme=dark", PII_REDACTION_PLACEHOLDER),
        ([["garuda_session", SENTINEL]], [["garuda_session", PII_REDACTION_PLACEHOLDER]]),
        ([("garuda_session", SENTINEL)], [("garuda_session", PII_REDACTION_PLACEHOLDER)]),
        ({}, {}),
        (None, None),
        ("", ""),
        (5, PII_REDACTION_PLACEHOLDER),
        (["not a pair"], [PII_REDACTION_PLACEHOLDER]),
        ((("garuda_session", SENTINEL),), [("garuda_session", PII_REDACTION_PLACEHOLDER)]),
        (("garuda_session", SENTINEL), [PII_REDACTION_PLACEHOLDER, PII_REDACTION_PLACEHOLDER]),
    ],
    ids=[
        "a raw string",
        "lists",
        "tuples",
        "empty",
        "none",
        "blank",
        "a number",
        "odd",
        "a tuple of pairs",
        "one bare pair",
    ],
)
def test_cookies_in_any_shape_lose_their_values_and_never_break_the_hook(
    cookies: Any, expected: Any
) -> None:
    event = _event({"User-Agent": PLAIN})
    event["request"]["cookies"] = cookies

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["request"]["cookies"] == expected
    assert cleaned["request"]["headers"] == {"User-Agent": PLAIN}
    assert SENTINEL not in json.dumps(cleaned)


# ── the same name elsewhere in the event ─────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "X-Internal-Key",
        "X_API_KEY",
        "x_bridge_auth",
        "x_crm_write_key",
        "xapikey",
        "Authorization",
        "proxy_authorization",
        "Cookie",
        "set_cookie",
        "X-CSRFToken",
        "Sec-WebSocket-Protocol",
    ],
)
def test_a_key_spelled_like_a_credential_header_is_cleaned_anywhere(key: str) -> None:
    # No request header carries the value: only the name says what it is.
    event = _event({"User-Agent": PLAIN})
    event["request"]["data"] = {key: SENTINEL, "topic": PLAIN}
    event["extra"] = {key: SENTINEL, "nested": [{key: SENTINEL}]}

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["request"]["data"] == {key: PII_REDACTION_PLACEHOLDER, "topic": PLAIN}
    assert cleaned["extra"] == {
        key: PII_REDACTION_PLACEHOLDER,
        "nested": [{key: PII_REDACTION_PLACEHOLDER}],
    }
    assert SENTINEL not in json.dumps(cleaned)


def test_debug_keys_that_are_not_headers_keep_their_values() -> None:
    extra = {
        "token_count": 7,
        "max_tokens": 42,
        "input_tokens": 3,
        "tokenizer": "zz-tokenizer",
        "x-request-id": "zz-request",
        "x-keyboard-layout": "zz-layout",
        "key_count": 2,
        "cache_key": "zz-cache",
    }
    event = _event({"User-Agent": PLAIN})
    event["extra"] = dict(extra)

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["extra"] == extra


# ── the minimal event of a failed scrub ──────────────────────────────────────


def test_the_minimal_event_of_a_failed_scrub_is_cleaned_by_the_same_rules() -> None:
    # A direct call with a cyclic payload: the walk fails and the hook answers with its
    # minimal event, which keeps the exception. Through the SDK a cycle never arrives,
    # serialisation has already replaced it.
    cyclic: dict[str, Any] = {}
    cyclic["again"] = cyclic
    event = _event({"User-Agent": PLAIN})
    event["extra"] = cyclic
    event["exception"] = {
        "values": [
            {
                "type": "RuntimeError",
                "value": "zz boom",
                "stacktrace": {
                    "frames": [
                        {
                            "function": "zz_handler",
                            "vars": {
                                "headers": {"X-Internal-Key": SENTINEL, "User-Agent": PLAIN},
                                "cookies": {"garuda_session": SECOND},
                                "x_crm_write_key": "zz-third-sentinel-value",
                                "attempt": 3,
                            },
                        }
                    ]
                },
            }
        ]
    }

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["tags"] == {"sentry_hook_error": "true", "sentry_exception_dropped": "false"}
    assert "extra" not in cleaned
    assert "request" not in cleaned
    kept = cleaned["exception"]["values"][0]
    assert (kept["type"], kept["value"]) == ("RuntimeError", "zz boom")  # the exception survived
    assert kept["stacktrace"]["frames"][0]["vars"] == {
        "headers": {"X-Internal-Key": PII_REDACTION_PLACEHOLDER, "User-Agent": PLAIN},
        "cookies": {"garuda_session": PII_REDACTION_PLACEHOLDER},
        "x_crm_write_key": PII_REDACTION_PLACEHOLDER,
        "attempt": 3,
    }
    assert "sentinel" not in json.dumps(cleaned)


@pytest.mark.parametrize("outer", [list, tuple], ids=["in a list", "in a tuple"])
@pytest.mark.parametrize("pair", [list, tuple], ids=["lists", "tuples"])
def test_the_minimal_event_cleans_headers_given_as_pairs(pair: type, outer: type) -> None:
    cyclic: dict[str, Any] = {}
    cyclic["again"] = cyclic
    event = _event({"User-Agent": PLAIN})
    event["extra"] = cyclic
    event["exception"] = {
        "values": [
            {
                "type": "RuntimeError",
                "value": "zz boom",
                "stacktrace": {
                    "frames": [
                        {
                            "function": "zz_handler",
                            "vars": {
                                "headers": outer(
                                    [
                                        pair(["X-Internal-Key", SENTINEL]),
                                        pair(["User-Agent", PLAIN]),
                                    ]
                                ),
                                "cookies": outer([pair(["garuda_session", SECOND])]),
                            },
                        }
                    ]
                },
            }
        ]
    }

    cleaned = _before_send(event, {})

    assert cleaned is not None
    assert cleaned["tags"] == {"sentry_hook_error": "true", "sentry_exception_dropped": "false"}
    kept = cleaned["exception"]["values"][0]
    assert (kept["type"], kept["value"]) == ("RuntimeError", "zz boom")  # the exception survived
    assert kept["stacktrace"]["frames"][0]["vars"] == {
        "headers": outer(
            [pair(["X-Internal-Key", PII_REDACTION_PLACEHOLDER]), pair(["User-Agent", PLAIN])]
        ),
        "cookies": [pair(["garuda_session", PII_REDACTION_PLACEHOLDER])],
    }
    assert "sentinel" not in json.dumps(cleaned)


# ── through the SDK ───────────────────────────────────────────────────────────


class MemoryTransport(Transport):
    def __init__(self, options: Any = None) -> None:
        super().__init__(options)
        self.payloads: list[str] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            self.payloads.append(item.payload.get_bytes().decode("utf-8", "replace"))


@pytest.fixture(params=[False, True], ids=["pii off", "pii on"])
def transport(request: pytest.FixtureRequest):
    """The SDK with the production hooks, with `send_default_pii` off and on.

    Off, the SDK filters a few header names itself and attaches no cookies. On, a supported
    setting, it attaches the cookie jar and filters nothing: the hook must not rely on it.
    """
    t = MemoryTransport()
    sentry_sdk.init(
        dsn="https://public@o0.ingest.sentry.io/1",
        transport=t,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        include_local_variables=False,
        send_default_pii=request.param,
        traces_sample_rate=1.0,
    )
    yield t
    sentry_sdk.get_client().close()


def _client() -> TestClient:
    app = FastAPI()

    @app.post("/api/sealed/write")
    async def sealed() -> JSONResponse:
        # A route that refuses a call and says so at ERROR: the log record becomes an event,
        # and the integration attaches the request to it.
        logging.getLogger("zz.sealed").error("zz sealed route refused the call")
        return JSONResponse({"detail": "refused"}, status_code=503)

    @app.websocket("/ws")
    async def socket(websocket: WebSocket) -> None:
        # A handshake that goes wrong and says so at ERROR, then closes.
        await websocket.accept()
        logging.getLogger("zz.socket").error("zz websocket handshake refused")
        await websocket.close(code=4003)

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "name", ["X-CRM-Write-Key", "X-Internal-Key", "X-Bridge-Auth", "X-Producer-Token"]
)
def test_a_refused_call_sends_no_credential_header_value_through_the_sdk(
    name: str, transport: MemoryTransport
) -> None:
    response = _client().post(
        "/api/sealed/write", headers={name: SENTINEL, "X-Request-Id": "zz-request-mark"}
    )

    assert response.status_code == 503
    sentry_sdk.flush()
    blob = json.dumps(transport.payloads)
    assert "zz sealed route refused the call" in blob  # the event really was captured
    assert "zz-request-mark" in blob  # and it carries the request's headers
    assert SENTINEL not in blob


def test_a_refused_call_sends_no_cookie_value_through_the_sdk(
    transport: MemoryTransport,
) -> None:
    client = _client()
    client.cookies.set("garuda_session", SENTINEL)
    client.cookies.set("garuda_result_session", SECOND)

    response = client.post(
        "/api/sealed/write",
        headers={"Authorization": f"Bearer {SENTINEL}", "X-Request-Id": "zz-request-mark"},
    )

    assert response.status_code == 503
    sentry_sdk.flush()
    blob = json.dumps(transport.payloads)
    assert "zz sealed route refused the call" in blob
    assert "zz-request-mark" in blob
    assert SENTINEL not in blob
    assert SECOND not in blob


def test_a_websocket_handshake_sends_no_bearer_subprotocol_through_the_sdk(
    transport: MemoryTransport,
) -> None:
    with (
        _client().websocket_connect("/ws", subprotocols=[f"bearer.{SENTINEL}"]) as socket,
        pytest.raises(WebSocketDisconnect),
    ):
        socket.receive_text()

    sentry_sdk.flush()
    events = [
        json.loads(payload)
        for payload in transport.payloads
        if "zz websocket handshake refused" in payload
    ]
    assert events, "the error event of the handshake was not captured"
    headers = events[0]["request"]["headers"]
    assert headers["sec-websocket-protocol"] == PII_REDACTION_PLACEHOLDER
    assert headers["sec-websocket-key"] not in ("", PII_REDACTION_PLACEHOLDER)
    assert SENTINEL not in json.dumps(transport.payloads)
