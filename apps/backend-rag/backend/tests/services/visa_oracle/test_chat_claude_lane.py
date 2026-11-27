"""visa_oracle.chat answers through the Claude Console door and degrades without it."""

from __future__ import annotations

import asyncio
import logging
import os

os.environ.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_for_testing_only_min_32_chars_long")
os.environ.setdefault("API_KEYS", "test_api_key_1,test_api_key_2")
os.environ.setdefault("ENVIRONMENT", "test")

import pytest
from starlette.requests import Request

from backend.llm.claude_oauth_client import (
    ClaudeOAuthError,
    ClaudeOAuthNotAvailable,
    ClaudeOAuthResponse,
)


class _StrongSearch:
    async def search_hybrid(self, **_kw):
        return {"results": [{"content": "C1 is the tourist visa.", "score": 0.9, "source": "imigrasi"}]}


def _request() -> Request:
    return Request({"type": "http", "headers": []})


@pytest.fixture
def mod(monkeypatch):
    from backend.app.routers import visa_oracle

    monkeypatch.setattr("backend.services.rag.hybrid_search.HybridSearchService", _StrongSearch)
    return visa_oracle


@pytest.mark.asyncio
async def test_the_answer_comes_from_the_claude_lane_with_its_bounds(mod, monkeypatch):
    seen: dict = {}

    async def _complete(prompt, **kwargs):
        seen["prompt"], seen["kwargs"] = prompt, kwargs
        return ClaudeOAuthResponse(
            text="Stay up to 60 days.", token_label="console_1", elapsed_s=0.2, attempts=1
        )

    monkeypatch.setattr("backend.llm.claude_oauth_client.complete_async", _complete)
    body = mod.ChatRequest(session_id="s1", message="How long can I stay on a C1?")
    resp = await mod.chat(_request(), body, db_pool=None)

    assert resp.answer == "Stay up to 60 days."
    assert resp.confidence == mod.CONFIDENCE_NORMAL
    assert seen["kwargs"]["endpoint"] == "visa_oracle.chat"
    assert seen["kwargs"]["max_output_tokens"] == 1024
    assert "C1 is the tourist visa." in seen["prompt"]


@pytest.mark.parametrize(
    "failure",
    [
        ClaudeOAuthError("every seat failed"),
        ClaudeOAuthNotAvailable("no Console seat configured"),
        asyncio.TimeoutError(),
    ],
)
@pytest.mark.asyncio
async def test_a_dead_lane_degrades_to_the_localized_fallback(mod, monkeypatch, caplog, failure):
    async def _complete(prompt, **_kw):
        raise failure

    monkeypatch.setattr("backend.llm.claude_oauth_client.complete_async", _complete)
    body = mod.ChatRequest(session_id="s1", message="How long can I stay on a C1?", language="it")
    with caplog.at_level(logging.DEBUG):
        resp = await mod.chat(_request(), body, db_pool=None)

    assert resp.success is True
    assert resp.answer == mod.TIMEOUT_FALLBACK_BY_LANG["it"]
    assert resp.confidence == mod.CONFIDENCE_CAUTIOUS
    assert "every seat failed" not in caplog.text


@pytest.mark.asyncio
async def test_an_empty_answer_is_the_fallback_not_a_blank_reply(mod, monkeypatch):
    async def _complete(prompt, **_kw):
        return ClaudeOAuthResponse(text="  ", token_label="console_1", elapsed_s=0.1, attempts=1)

    monkeypatch.setattr("backend.llm.claude_oauth_client.complete_async", _complete)
    body = mod.ChatRequest(session_id="s1", message="How long can I stay on a C1?")
    resp = await mod.chat(_request(), body, db_pool=None)

    assert resp.answer == mod.TIMEOUT_FALLBACK_BY_LANG["en"]
