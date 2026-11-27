"""Tests for ZantaraAIClient on the Claude Console lane.

Nothing here reaches a model or the network: ``complete_async`` and the seat
lookup are stood in. Fixtures are synthetic markers, never client data.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from backend.llm.claude_oauth_client import (
    ClaudeOAuthError,
    ClaudeOAuthNotAvailable,
    ClaudeOAuthResponse,
)
from backend.llm.fallback_messages import get_fallback_message
from backend.llm.zantara_ai_client import (
    _MAX_PROMPT_BYTES,
    ZantaraAIClient,
    _build_prompt,
    _is_terminal_auth_or_quota_error,
    _resolve_model,
)

_MODULE = "backend.llm.zantara_ai_client"
_SENTINEL = "SYNTHETIC_CLIENT_SENTINEL"


def _resp(text: str) -> ClaudeOAuthResponse:
    return ClaudeOAuthResponse(text=text, token_label="console_1", elapsed_s=0.1, attempts=1)


def _seat_error(tail: str) -> ClaudeOAuthError:
    return ClaudeOAuthError(
        f"Claude Console seats failed after 3 attempt(s). Last error class: {tail}"
    )


@pytest.fixture(autouse=True)
def _no_model_env(monkeypatch):
    monkeypatch.delenv("CHANNEL_MODEL_NAME", raising=False)


@pytest.fixture
def lane():
    """A Console seat is configured and the completion call is stood in."""
    with (
        patch(f"{_MODULE}.collect_seats", return_value=[object()]),
        patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_resp("answer"))) as call,
        patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()),
    ):
        yield call


async def _collect(client: ZantaraAIClient, **kwargs) -> str:
    chunks = []
    async for chunk in client.stream(**kwargs):
        chunks.append(chunk)
    return "".join(chunks)


def _words(text: str) -> str:
    return "".join(word + " " for word in text.split())


# ============================================================================
# Initialization
# ============================================================================
def test_init_with_a_seat_uses_the_console_lane(lane):
    client = ZantaraAIClient(api_key="ignored-legacy-argument")
    assert client.mock_mode is False
    assert client.get_model_info()["provider"] == "claude_console"
    assert client.is_available() is True


def test_init_without_a_seat_in_development_is_mock_mode():
    with (
        patch(f"{_MODULE}.collect_seats", return_value=[]),
        patch("backend.app.core.config.settings.environment", "development"),
    ):
        client = ZantaraAIClient()
    assert client.mock_mode is True
    assert client.get_model_info()["provider"] == "mock"
    assert client.is_available() is True


def test_init_without_a_seat_in_production_fails_closed():
    with (
        patch(f"{_MODULE}.collect_seats", return_value=[]),
        patch("backend.app.core.config.settings.environment", "production"),
        pytest.raises(ValueError, match="No Claude Console seats configured in production"),
    ):
        ZantaraAIClient()


def test_a_claude_model_argument_is_honoured(lane):
    assert ZantaraAIClient(model="claude-haiku-5-5").model == "claude-haiku-5-5"


def test_legacy_non_claude_slugs_are_ignored(lane, monkeypatch):
    legacy = "gemini-" + "2.5-flash"
    default = _resolve_model(None)
    assert default.startswith("claude-")
    assert ZantaraAIClient(model=legacy).model == default
    monkeypatch.setenv("CHANNEL_MODEL_NAME", legacy)
    assert _resolve_model(None) == default
    monkeypatch.setenv("CHANNEL_MODEL_NAME", "claude-sonnet-5-5")
    assert _resolve_model(None) == "claude-sonnet-5-5"


# ============================================================================
# Helpers
# ============================================================================
def test_validate_inputs(lane):
    client = ZantaraAIClient()
    client._validate_inputs(
        max_tokens=100, temperature=0.5, messages=[{"role": "user", "content": "hi"}]
    )
    for bad in (
        {"max_tokens": 0},
        {"max_tokens": 10000},
        {"temperature": -1},
        {"temperature": 2.1},
    ):
        with pytest.raises(ValueError):
            client._validate_inputs(**bad)
    for messages in ([], "not a list", [{"role": "user"}], ["x"], [{"role": "user", "content": 5}]):
        with pytest.raises(ValueError):
            client._validate_inputs(messages=messages)


def test_estimate_tokens(lane):
    client = ZantaraAIClient()
    client.token_estimator = type(
        "E", (), {"estimate_messages_tokens": lambda *_: 10, "estimate_tokens": lambda *_: 20}
    )()
    assert client._estimate_tokens([], "response") == {"input": 10, "output": 20}


@pytest.mark.parametrize(
    ("tail", "terminal"),
    [
        ("console_1: authentication_error", True),
        ("console_1: model_access_denied", True),
        ("console_1: quota_exhausted", True),
        ("console_1: rate limited", False),
        ("console_1: cli_exit_1", False),
    ],
)
def test_terminal_classification_reads_the_structured_tail(tail, terminal):
    assert _is_terminal_auth_or_quota_error(_seat_error(tail)) is terminal
    assert _is_terminal_auth_or_quota_error(RuntimeError("quota authentication")) is False


# ============================================================================
# _build_prompt
# ============================================================================
def test_prompt_orders_system_history_then_the_last_message_verbatim():
    prompt = _build_prompt(
        "sys",
        [
            {"role": "system", "content": "skipped"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "question\n\n[note] keep"},
        ],
    )
    assert prompt.startswith("<system_instructions>\nsys\n</system_instructions>")
    assert "User: first\nAssistant: reply" in prompt
    assert "skipped" not in prompt
    assert prompt.endswith("question\n\n[note] keep")


def test_oldest_history_is_trimmed_first_and_the_message_is_kept():
    message = "question\n\n[note] keep"
    prompt = _build_prompt(
        "sys",
        [
            {"role": "user", "content": "h" * 125_000},
            {"role": "assistant", "content": "recent"},
            {"role": "user", "content": message},
        ],
    )
    assert prompt.endswith(message)
    assert "h" * 1000 not in prompt
    assert len(prompt.encode("utf-8")) <= _MAX_PROMPT_BYTES


@pytest.mark.parametrize(
    ("messages", "system", "reason"),
    [
        ([{"role": "user", "content": "x" * 140_000}], "", "exceeds"),
        ([{"role": "user", "content": "hello\x00world"}], "", "NUL"),
        ([{"role": "user", "content": "bad\ud800text"}], "", "surrogates"),
        ([{"role": "user", "content": "   "}], "", "non-empty"),
        ([{"role": "system", "content": "only system"}], "sys", "non-system"),
    ],
    ids=["oversize", "nul", "surrogate", "blank", "system_only"],
)
def test_a_prompt_the_cli_can_never_accept_is_refused(messages, system, reason):
    with pytest.raises(ValueError, match=reason):
        _build_prompt(system, messages)


# ============================================================================
# chat_async
# ============================================================================
@pytest.mark.asyncio
async def test_chat_async_runs_on_the_console_lane(lane):
    client = ZantaraAIClient()

    result = await client.chat_async(messages=[{"role": "user", "content": "hi"}])

    assert result["text"] == "answer"
    assert result["provider"] == "claude_console"
    assert result["cost"] == 0.0
    lane.assert_awaited_once()
    assert lane.await_args.kwargs["endpoint"] == "zantara_ai.chat"
    assert lane.await_args.kwargs["timeout_s"] == 60.0
    assert lane.await_args.kwargs["total_timeout_s"] == 120.0
    assert lane.await_args.kwargs["model"] == client.model


@pytest.mark.asyncio
async def test_chat_async_mock_mode():
    with (
        patch(f"{_MODULE}.collect_seats", return_value=[]),
        patch("backend.app.core.config.settings.environment", "development"),
    ):
        client = ZantaraAIClient()
        result = await client.chat_async(messages=[{"role": "user", "content": "hi"}])
    assert result["provider"] == "mock"
    assert "MOCK response" in result["text"]


@pytest.mark.asyncio
async def test_chat_async_without_a_seat_raises_not_available(lane):
    client = ZantaraAIClient()
    with patch(f"{_MODULE}.collect_seats", return_value=[]):
        with pytest.raises(ClaudeOAuthNotAvailable):
            await client.chat_async(messages=[{"role": "user", "content": "hi"}])
    lane.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_async_propagates_a_seat_failure(lane):
    client = ZantaraAIClient()
    lane.side_effect = _seat_error("console_1: rate limited")
    with pytest.raises(ClaudeOAuthError):
        await client.chat_async(messages=[{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    ["hello\x00world", "x" * 140_000, "bad\ud800text", "   "],
    ids=["nul", "big", "sur", "blank"],
)
async def test_chat_async_refuses_an_invalid_prompt_before_the_cli(lane, bad):
    client = ZantaraAIClient()
    with pytest.raises(ValueError):
        await client.chat_async(messages=[{"role": "user", "content": bad}])
    lane.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_async_logs_never_carry_the_prompt_or_the_answer(lane, caplog):
    caplog.set_level(logging.DEBUG)
    client = ZantaraAIClient()
    lane.return_value = _resp(f"{_SENTINEL} answer")

    await client.chat_async(messages=[{"role": "user", "content": f"{_SENTINEL} question"}])
    lane.side_effect = _seat_error("console_1: rate limited")
    with pytest.raises(ClaudeOAuthError):
        await client.chat_async(messages=[{"role": "user", "content": f"{_SENTINEL} question"}])

    assert _SENTINEL not in caplog.text


# ============================================================================
# stream
# ============================================================================
@pytest.mark.asyncio
async def test_stream_yields_the_answer(lane):
    client = ZantaraAIClient()
    text = await _collect(client, message="hi", user_id="user1")
    assert text == "answer"
    assert lane.await_args.kwargs["endpoint"] == "zantara_ai.chat"


@pytest.mark.asyncio
async def test_stream_mock_mode():
    with (
        patch(f"{_MODULE}.collect_seats", return_value=[]),
        patch("backend.app.core.config.settings.environment", "development"),
        patch(f"{_MODULE}.asyncio.sleep", new=AsyncMock()),
    ):
        client = ZantaraAIClient()
        text = await _collect(client, message="hi", user_id="user1")
    assert "MOCK" in text


@pytest.mark.asyncio
async def test_stream_without_a_seat_yields_the_unavailable_message(lane):
    client = ZantaraAIClient()
    with patch(f"{_MODULE}.collect_seats", return_value=[]):
        text = await _collect(client, message="hi", user_id="user1", language="en")
    assert text == _words(get_fallback_message("service_unavailable", "en"))
    lane.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_not_available_error_yields_the_unavailable_message(lane):
    client = ZantaraAIClient()
    lane.side_effect = ClaudeOAuthNotAvailable("no door")
    text = await _collect(client, message="hi", user_id="user1")
    assert text == _words(get_fallback_message("service_unavailable", "en"))
    assert lane.await_count == 1


@pytest.mark.asyncio
async def test_stream_empty_output_ends_in_the_connection_message(lane):
    client = ZantaraAIClient()
    lane.return_value = _resp("   ")
    text = await _collect(client, message="hi", user_id="user1")
    assert text == _words(get_fallback_message("connection_error", "en"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["console_1: authentication_error", "console_1: quota_exhausted"])
async def test_stream_terminal_seat_failure_answers_at_once_without_retry(lane, tail):
    client = ZantaraAIClient()
    lane.side_effect = _seat_error(tail)
    text = await _collect(client, message="hi", user_id="user1")
    assert text == _words(get_fallback_message("api_key_error", "en"))
    assert lane.await_count == 1


@pytest.mark.asyncio
async def test_stream_a_rate_limit_is_retried_and_then_answers(lane):
    client = ZantaraAIClient()
    lane.side_effect = [_seat_error("console_1: rate limited"), _resp("recovered")]
    text = await _collect(client, message="hi", user_id="user1")
    assert text == "recovered"
    assert lane.await_count == 2


@pytest.mark.asyncio
async def test_stream_exhausted_retries_end_in_the_connection_message(lane):
    client = ZantaraAIClient()
    lane.side_effect = _seat_error("console_1: rate limited")
    text = await _collect(client, message="hi", user_id="user1")
    assert text == _words(get_fallback_message("connection_error", "en"))
    assert lane.await_count == client.retry_handler.max_retries


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["hello\x00world", "x" * 140_000, "bad\ud800text"])
async def test_stream_refuses_an_invalid_prompt_before_the_cli(lane, bad):
    client = ZantaraAIClient()
    with pytest.raises(ValueError):
        await _collect(client, message=bad, user_id="user1")
    lane.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_logs_never_carry_the_prompt_or_the_answer(lane, caplog):
    caplog.set_level(logging.DEBUG)
    client = ZantaraAIClient()
    lane.return_value = _resp(f"{_SENTINEL} answer")
    await _collect(client, message=f"{_SENTINEL} question", user_id="user1")
    lane.side_effect = _seat_error("console_1: rate limited")
    await _collect(client, message=f"{_SENTINEL} question", user_id="user1")
    assert _SENTINEL not in caplog.text


# ============================================================================
# conversational / conversational_with_tools / close
# ============================================================================
@pytest.mark.asyncio
async def test_conversational(lane):
    client = ZantaraAIClient()
    result = await client.conversational("hi", "user1")
    assert result["text"] == "answer"
    assert result["ai_used"] == "zantara-ai"
    assert result["provider"] == "claude_console"


@pytest.mark.asyncio
async def test_conversational_with_tools_falls_back_to_plain_conversation(lane):
    client = ZantaraAIClient()
    result = await client.conversational_with_tools("hi", "user1", tools=[{}])
    assert result["text"] == "answer"
    assert result["used_tools"] is False
    assert result["tools_called"] == []


@pytest.mark.asyncio
async def test_close_is_a_no_op(lane):
    await ZantaraAIClient().close()
