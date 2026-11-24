"""The request-scoped redaction backstop: whatever a client library logs while a /api/search
request is in flight, the user's query (and free-form collection) is replaced in the record."""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.search.log_safety import (
    install_query_log_redaction,
    query_log_scope,
    redact_query,
)

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

install_query_log_redaction()
CANARY = "uji-sintetis-kueri-3WK8Q"
FOREIGN = logging.getLogger("backend.core.qdrant_db")


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    parts: list[str] = []
    for r in caplog.records:
        parts += [r.getMessage(), str(r.args), r.exc_text or ""]
        parts += [str(v) for k, v in r.__dict__.items() if k not in ("args", "msg", "exc_info")]
        if r.exc_info and r.exc_info[1] is not None:
            parts.append(str(r.exc_info[1]))
    return "\n".join(parts)


def test_a_foreign_logger_inside_the_scope_is_redacted_everywhere(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG), query_log_scope(f"{CANARY} kueri panjang"):
        try:
            raise RuntimeError(f"HTTP 400 for {CANARY} kueri panjang")
        except RuntimeError:
            FOREIGN.error(
                "qdrant failed on %s",
                f"{CANARY} kueri panjang",
                extra={"error": f"body quoted {CANARY} kueri panjang"},
                exc_info=True,
            )
        FOREIGN.debug(f"f-string leak {CANARY} kueri panjang")
    text = _all_text(caplog)
    assert "qdrant failed on" in text and "f-string leak" in text, "records never emitted: vacuous"
    assert CANARY not in text
    assert "[query]" in text


def test_outside_the_scope_nothing_is_touched(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        FOREIGN.error("plain %s", CANARY)
    assert CANARY in _all_text(caplog)


def test_short_texts_are_ignored_and_numeric_args_survive(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG), query_log_scope("ab", f"{CANARY} lengkap"):
        FOREIGN.info("ab stays, %d results in %.1fms for %s", 3, 1.5, f"{CANARY} lengkap")
    text = _all_text(caplog)
    assert "ab stays, 3 results in 1.5ms for [query]" in text


def test_redact_query_handles_none_and_whitespace_variants() -> None:
    assert redact_query("x", None) == "x"
    assert (
        redact_query("a  b   c here", ("a b c here",)) == "a  b   c here"
    )  # not collapsed in text
    assert redact_query("see a b c here now", ("a  b c here",)) == "see [query] now"


class _Boom:
    async def search(self, **kwargs: Any) -> dict[str, Any]:
        try:
            raise RuntimeError(
                f"upstream rejected {kwargs['query']} / {kwargs['collection_override']}"
            )
        except RuntimeError:
            FOREIGN.error(
                "upstream error for %s",
                kwargs["query"],
                extra={"coll": kwargs["collection_override"]},
                exc_info=True,
            )
            raise


def test_end_to_end_through_the_route_nothing_reaches_a_log_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.state.search_service = _Boom()
    client = TestClient(app, raise_server_exceptions=False)
    collection = "koleksi-sintetis-5ZP2"
    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/api/search",
            json={"query": f"{CANARY} kueri", "collection": collection, "level": 0, "limit": 2},
        )
    assert resp.status_code == 500
    text = "\n".join(_all_text(caplog).splitlines())
    assert "upstream error for" in text, "the foreign record never reached the handler: vacuous"
    assert CANARY not in text and collection not in text
