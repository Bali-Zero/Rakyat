"""Guard: no log record on the /api/search path carries the user's query text.

Completes the router-level guard of #185: the query also flows through the keyword
translator, the Gemini query expander and the router's exception handler, which each
logged a slice of it (30-50 characters, or the whole query at DEBUG). Synthetic values
only; every test also asserts that the log line it covers was really emitted.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.search.keyword_translator import KeywordTranslator
from backend.services.search.query_expander import QueryExpander

knowledge_router = importlib.import_module("backend.app.modules.knowledge.router")

MARK = "uji-sintetis-kueri-7Q4ZK"
QUERY = f"{MARK} kueri tentang visa dan pajak untuk perusahaan"  # > 4 words, MARK in first 30 chars


def _text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))
    )


class _Gemini:
    def __init__(self, reply: str | None = None, boom: Exception | None = None) -> None:
        self.reply, self.boom = reply, boom

    async def generate_response(self, prompt: str) -> str | None:
        if self.boom:
            raise self.boom
        return self.reply


def _expander(**kw: Any) -> QueryExpander:
    e = QueryExpander()
    e._gemini = _Gemini(**kw)
    return e


@pytest.mark.asyncio
async def test_expand_multi_success_logs_no_query(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        out = await _expander(reply=f'["{MARK} alt satu", "alt dua"]').expand_multi(QUERY + " a")
    assert len(out) >= 2
    text = _text(caplog)
    assert "Multi-query expansion" in text, "success line never logged: vacuous"
    assert MARK not in text and "kueri tentang" not in text


@pytest.mark.asyncio
async def test_expand_multi_bad_json_logs_no_response_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        await _expander(reply=f"not json {MARK}").expand_multi(QUERY + " b")
    text = _text(caplog)
    assert "Failed to parse expansion JSON" in text, "parse-failure line never logged: vacuous"
    assert MARK not in text


@pytest.mark.asyncio
async def test_expand_multi_short_query_logs_no_query(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await _expander(reply="[]").expand_multi(f"{MARK} c")
    text = _text(caplog)
    assert "too short for LLM expansion" in text, "short-query line never logged: vacuous"
    assert MARK not in text


@pytest.mark.asyncio
async def test_gemini_failure_logs_type_not_message(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await _expander(boom=RuntimeError(f"quota for {MARK}")).expand_multi(QUERY + " d")
    text = _text(caplog)
    assert "Gemini query expansion failed: RuntimeError" in text
    assert MARK not in text


@pytest.mark.asyncio
async def test_expand_translation_logs_no_query(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await _expander(reply='{"english": "visa", "indonesian": "visa"}').expand(QUERY + " e")
    text = _text(caplog)
    assert "Query translated" in text, "translation line never logged: vacuous"
    assert MARK not in text


def test_keyword_translator_logs_no_query(caplog: pytest.LogCaptureFixture) -> None:
    translator = KeywordTranslator(term_map={"kueri": ("query", "pertanyaan")})
    with caplog.at_level(logging.DEBUG):
        translator.translate(f"{MARK} kueri f")
    text = _text(caplog)
    assert "Query expanded" in text, "expansion line never logged: vacuous"
    assert MARK not in text


class _Boom:
    async def search(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"backend rejected {kwargs['query']}")


def test_router_error_log_has_type_and_frames_but_not_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = FastAPI()
    app.include_router(knowledge_router.router)
    app.state.search_service = _Boom()
    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.DEBUG, logger=knowledge_router.logger.name):
        resp = client.post("/api/search", json={"query": QUERY, "level": 0, "limit": 3})
    assert resp.status_code == 500
    text = "\n".join(
        [r.getMessage() for r in caplog.records]
        + [str(r.exc_info[1]) for r in caplog.records if r.exc_info]
    )
    assert "Search error: RuntimeError" in text, "error line never logged: vacuous"
    assert "semantic_search" in text, "traceback frames lost: debugging would suffer"
    assert MARK not in text
