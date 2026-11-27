"""LLMGateway on the Claude Console lane: construction, routing, health.

The gateway used to call Gemini (with an OpenRouter fallback). Since Wave 2 it has
exactly one door, ``claude_oauth_client``. Stood in here: the Console seats
(``collect_seats``) and the completion call (``complete_async``); the gateway's
own routing, circuit breaker and session handling are the real ones.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthResponse
from backend.services.llm_clients.pricing import TokenUsage
from backend.services.rag.agentic.chat_session import ChatSession
from backend.services.rag.agentic.llm_gateway import (
    TIER_FALLBACK,
    TIER_FLASH,
    TIER_LITE,
    TIER_PRO,
    LLMGateway,
    MockChatSession,
)

_MODULE = "backend.services.rag.agentic.llm_gateway"


def _answer(text: str = "pong") -> ClaudeOAuthResponse:
    return ClaudeOAuthResponse(text=text, token_label="console_1", elapsed_s=0.1, attempts=1)


@pytest.fixture
def seats():
    with patch(f"{_MODULE}.collect_seats", return_value=[object()]):
        yield


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("PRIMARY_MODEL_NAME", "FALLBACK_MODEL_NAME", "NUZANTARA_CLAUDE_DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_gemini_tools_can_be_read_and_replaced() -> None:
    gateway = LLMGateway(gemini_tools=[{"name": "vector_search"}])

    assert gateway.gemini_tools == [{"name": "vector_search"}]

    gateway.set_gemini_tools([{"name": "calculator"}])
    assert gateway.gemini_tools == [{"name": "calculator"}]

    gateway.set_gemini_tools(None)
    assert gateway.gemini_tools == []


def test_model_tier_and_fallback_chain_are_deterministic() -> None:
    gateway = LLMGateway()
    gateway.model_name_pro = "claude-pro-slug"
    gateway.model_name_flash = "claude-flash-slug"
    gateway.model_name_fallback = "claude-fallback-slug"

    assert gateway._get_model_for_tier(TIER_PRO) == "claude-pro-slug"
    assert gateway._get_model_for_tier(TIER_FLASH) == "claude-flash-slug"
    assert gateway._get_model_for_tier(TIER_LITE) == "claude-fallback-slug"
    assert gateway._get_model_for_tier(TIER_FALLBACK) == "claude-fallback-slug"
    assert gateway._get_fallback_chain(TIER_PRO) == [
        "claude-pro-slug",
        "claude-flash-slug",
        "claude-fallback-slug",
    ]
    assert gateway._get_fallback_chain(TIER_FLASH) == ["claude-flash-slug", "claude-fallback-slug"]


def test_a_legacy_gemini_slug_in_the_environment_is_ignored(monkeypatch) -> None:
    """Fly still carries PRIMARY_MODEL_NAME=gemini-2.5-flash until Stage 3."""
    monkeypatch.setenv("PRIMARY_MODEL_NAME", "gemini-2.5-flash")
    monkeypatch.setenv("FALLBACK_MODEL_NAME", "gemini-2.0-flash")

    gateway = LLMGateway()

    assert gateway.model_name_flash.startswith("claude-")
    assert gateway.model_name_pro == gateway.model_name_flash
    assert gateway.model_name_fallback == gateway.model_name_flash


def test_a_claude_slug_in_the_environment_is_honoured(monkeypatch) -> None:
    monkeypatch.setenv("PRIMARY_MODEL_NAME", "claude-haiku-4-5")

    gateway = LLMGateway()

    assert gateway.model_name_flash == "claude-haiku-4-5"
    assert gateway.model_name_fallback == "claude-haiku-4-5"


def test_available_follows_the_console_seats() -> None:
    gateway = LLMGateway()
    with patch(f"{_MODULE}.collect_seats", return_value=[]):
        assert gateway._available is False
    with patch(f"{_MODULE}.collect_seats", return_value=[object()]):
        assert gateway._available is True
    gateway._available = False
    with patch(f"{_MODULE}.collect_seats", return_value=[object()]):
        assert gateway._available is False


def test_create_chat_with_history_returns_mock_when_no_seat() -> None:
    gateway = LLMGateway()
    with patch(f"{_MODULE}.collect_seats", return_value=[]):
        chat = gateway.create_chat_with_history(
            history_to_use=[{"role": "user", "content": "hello"}],
            model_tier=TIER_FLASH,
        )

    assert isinstance(chat, MockChatSession)
    assert chat.history == [{"role": "user", "content": "hello"}]


def test_create_chat_with_history_is_an_in_memory_session_when_a_seat_exists(seats) -> None:
    gateway = LLMGateway()

    chat = gateway.create_chat_with_history(
        history_to_use=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            "not a dict",
        ],
        model_tier=TIER_FLASH,
        system_instruction="be brief",
    )

    assert isinstance(chat, ChatSession)
    assert chat.client is None
    assert chat.system_instruction == "be brief"
    assert chat.history == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hi"}]},
    ]


@pytest.mark.asyncio
async def test_send_message_delegates_to_fallback_runner(monkeypatch) -> None:
    gateway = LLMGateway()
    seen: dict[str, object] = {}

    async def fake_send_with_fallback(**kwargs):
        seen.update(kwargs)
        return (
            "answer",
            "claude-fake",
            SimpleNamespace(text="answer"),
            TokenUsage(prompt_tokens=1, completion_tokens=2, cost_usd=0.0),
        )

    monkeypatch.setattr(gateway, "_send_with_fallback", fake_send_with_fallback)

    with patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock) as emitted:
        text, model, response, usage = await gateway.send_message(
            chat=None,
            message="What is KITAS?",
            system_prompt="system",
            tier=TIER_PRO,
            conversation_messages=[{"role": "user", "content": "What is KITAS?"}],
        )

    assert text == "answer"
    assert model == "claude-fake"
    assert response.text == "answer"
    assert usage.total_tokens == 3
    assert seen["model_tier"] == TIER_PRO
    assert seen["message"] == "What is KITAS?"
    assert seen["system_prompt"] == "system"
    assert seen["conversation_messages"] == [{"role": "user", "content": "What is KITAS?"}]
    assert seen["query_cost_tracker"] == {"cost": 0.0, "depth": 0}
    assert emitted.call_args.kwargs["provider"] == "claude_console"


@pytest.mark.asyncio
async def test_send_message_is_answered_through_the_console_lane(seats) -> None:
    gateway = LLMGateway()

    with (
        patch(
            f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer("KITAS is a permit"))
        ) as call,
        patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
    ):
        text, model, _response, usage = await gateway.send_message(
            chat=None, message="What is KITAS?", enable_function_calling=False
        )

    assert text == "KITAS is a permit"
    assert model == gateway.model_name_flash
    assert usage.prompt_tokens >= 1
    call.assert_awaited_once()
    assert call.call_args.kwargs["endpoint"] == "rag.gateway.chat"
    assert call.call_args.kwargs["model"] == gateway.model_name_flash
    assert call.call_args.kwargs["json_schema"] is None


@pytest.mark.asyncio
async def test_health_check_reports_the_lane_healthy(seats) -> None:
    gateway = LLMGateway()

    with patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer("OK"))) as call:
        status = await gateway.health_check()

    assert status == {"claude_console": True, "primary": True, "fallback": True}
    assert call.call_args.kwargs["endpoint"] == "rag.gateway.chat"


@pytest.mark.asyncio
async def test_health_check_without_a_seat_spawns_nothing() -> None:
    gateway = LLMGateway()

    with (
        patch(f"{_MODULE}.collect_seats", return_value=[]),
        patch(f"{_MODULE}.complete_async", new=AsyncMock()) as call,
    ):
        status = await gateway.health_check()

    assert status == {"claude_console": False, "primary": False, "fallback": False}
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_check_is_false_when_the_cli_fails(seats) -> None:
    gateway = LLMGateway()

    with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=RuntimeError("down"))):
        status = await gateway.health_check()

    assert status == {"claude_console": False, "primary": False, "fallback": False}
