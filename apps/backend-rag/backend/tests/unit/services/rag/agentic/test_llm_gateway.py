"""
Unit tests for backend/services/rag/agentic/llm_gateway.py (Claude Console lane)

Covers: init, _available, set_gemini_tools, _get_model_for_tier, _get_fallback_chain,
        circuit breaker, create_chat_with_history, send_message, _send_with_fallback,
        history extraction, prompt building, tier constants.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthError, ClaudeOAuthNotAvailable
from backend.services.rag.agentic.llm_gateway import (
    TIER_FALLBACK,
    TIER_FLASH,
    TIER_LITE,
    TIER_PRO,
    LLMGateway,
    _build_claude_prompt,
    _extract_history_entries,
)

_MODULE = "backend.services.rag.agentic.llm_gateway"

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def gateway():
    """LLMGateway with a (mocked) Console seat."""
    gw = LLMGateway()
    gw._available = True
    return gw


@pytest.fixture
def gateway_no_seat():
    """LLMGateway with no Console seat configured."""
    with patch(f"{_MODULE}.collect_seats", return_value=[]):
        yield LLMGateway()


# ============================================================================
# Tier Constants
# ============================================================================


class TestTierConstants:
    def test_tier_values(self):
        assert TIER_FLASH == 0
        assert TIER_LITE == 1
        assert TIER_PRO == 2
        assert TIER_FALLBACK == 3


# ============================================================================
# Init
# ============================================================================


class TestInit:
    def test_init_default(self, gateway):
        assert gateway._gemini_tools == []
        assert gateway._circuit_breaker_threshold == 5
        assert gateway._max_fallback_depth == 3
        assert gateway._max_fallback_cost_usd == 0.10

    def test_no_gemini_or_openrouter_client_is_held(self, gateway):
        assert not hasattr(gateway, "_genai_client")
        assert not hasattr(gateway, "_openrouter_client")

    def test_init_with_tools(self):
        tools = [{"name": "tool1"}]
        gw = LLMGateway(gemini_tools=tools)
        assert gw._gemini_tools == tools


# ============================================================================
# _available property
# ============================================================================


class TestAvailable:
    def test_set_available(self, gateway):
        gateway._available = True
        assert gateway._available is True
        gateway._available = False
        assert gateway._available is False

    def test_available_without_override(self, gateway_no_seat):
        assert gateway_no_seat._available is False


# ============================================================================
# gemini_tools / set_gemini_tools
# ============================================================================


class TestGeminiTools:
    def test_get_gemini_tools(self, gateway):
        assert gateway.gemini_tools == []

    def test_set_gemini_tools(self, gateway):
        tools = [{"name": "test_tool"}]
        gateway.set_gemini_tools(tools)
        assert gateway.gemini_tools == tools

    def test_set_gemini_tools_none(self, gateway):
        gateway.set_gemini_tools(None)
        assert gateway.gemini_tools == []


# ============================================================================
# _get_model_for_tier
# ============================================================================


class TestGetModelForTier:
    def test_flash_tier(self, gateway):
        assert gateway._get_model_for_tier(TIER_FLASH) == gateway.model_name_flash

    def test_pro_tier(self, gateway):
        assert gateway._get_model_for_tier(TIER_PRO) == gateway.model_name_pro

    def test_lite_tier(self, gateway):
        assert gateway._get_model_for_tier(TIER_LITE) == gateway.model_name_fallback

    def test_fallback_tier(self, gateway):
        assert gateway._get_model_for_tier(TIER_FALLBACK) == gateway.model_name_fallback

    def test_unknown_tier_defaults_flash(self, gateway):
        assert gateway._get_model_for_tier(99) == gateway.model_name_flash

    def test_every_tier_is_a_claude_slug(self, gateway):
        for tier in (TIER_FLASH, TIER_LITE, TIER_PRO, TIER_FALLBACK):
            assert gateway._get_model_for_tier(tier).startswith("claude-")


# ============================================================================
# _get_fallback_chain
# ============================================================================


class TestGetFallbackChain:
    def test_flash_chain(self, gateway):
        chain = gateway._get_fallback_chain(TIER_FLASH)
        assert gateway.model_name_flash in chain

    def test_pro_chain(self, gateway):
        chain = gateway._get_fallback_chain(TIER_PRO)
        assert gateway.model_name_pro in chain

    def test_no_duplicates(self, gateway):
        chain = gateway._get_fallback_chain(TIER_FLASH)
        assert len(chain) == len(set(chain))

    def test_fallback_tier_chain(self, gateway):
        chain = gateway._get_fallback_chain(TIER_FALLBACK)
        assert gateway.model_name_fallback in chain


# ============================================================================
# Circuit Breaker
# ============================================================================


class TestCircuitBreaker:
    def test_get_circuit_breaker_creates(self, gateway):
        assert gateway._get_circuit_breaker("test_model") is not None

    def test_get_circuit_breaker_reuses(self, gateway):
        cb1 = gateway._get_circuit_breaker("test_model")
        cb2 = gateway._get_circuit_breaker("test_model")
        assert cb1 is cb2

    def test_is_circuit_open_default(self, gateway):
        assert gateway._is_circuit_open("test_model") is False

    def test_record_success(self, gateway):
        circuit = gateway._get_circuit_breaker("test_model")
        circuit.record_failure()  # seed a failure to prove the reset below
        assert circuit.failure_count == 1

        gateway._record_success("test_model")

        assert circuit.failure_count == 0

    @patch(f"{_MODULE}.ErrorClassifier")
    @patch(f"{_MODULE}.get_error_context")
    def test_record_failure(self, mock_ctx, mock_classifier, gateway):
        mock_classifier.classify_error.return_value = ("transient", "low")
        mock_ctx.return_value = {}

        gateway._record_failure("test_model", Exception("test error"))

        assert gateway._get_circuit_breaker("test_model").failure_count == 1
        mock_classifier.classify_error.assert_called_once()


# ============================================================================
# create_chat_with_history
# ============================================================================


class TestCreateChatWithHistory:
    def test_no_seat_returns_mock(self, gateway_no_seat):
        session = gateway_no_seat.create_chat_with_history(
            history_to_use=[{"role": "user", "content": "Hello"}],
        )
        assert session is not None
        assert session.history == [{"role": "user", "content": "Hello"}]

    def test_with_none_history(self, gateway_no_seat):
        assert gateway_no_seat.create_chat_with_history(history_to_use=None) is not None

    def test_with_empty_history(self, gateway_no_seat):
        assert gateway_no_seat.create_chat_with_history(history_to_use=[]) is not None

    def test_with_seat(self, gateway):
        session = gateway.create_chat_with_history(
            history_to_use=[{"role": "user", "content": "Hi"}],
            model_tier=TIER_FLASH,
            system_instruction="You are helpful.",
        )
        assert session.history == [{"role": "user", "parts": [{"text": "Hi"}]}]
        assert session.system_instruction == "You are helpful."


# ============================================================================
# history extraction + prompt building
# ============================================================================


class TestPromptBuilding:
    def test_history_comes_from_the_chat_session_in_either_shape(self):
        chat = MagicMock()
        chat.history = [
            {"role": "user", "parts": [{"text": "first"}]},
            {"role": "model", "content": "second"},
            {"role": "user", "parts": []},
        ]
        assert _extract_history_entries(chat, None, "now") == [
            ("User", "first"),
            ("Assistant", "second"),
        ]

    def test_without_a_chat_the_trailing_current_message_is_not_duplicated(self):
        messages = [
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "now"},
        ]
        assert _extract_history_entries(None, messages, "now") == [
            ("User", "earlier"),
            ("Assistant", "reply"),
        ]

    def test_the_message_is_the_last_section_verbatim(self):
        message = "question?\n\n[CRM NOTE] keep this exact suffix"
        prompt = _build_claude_prompt(
            message=message,
            system_prompt="sys",
            history_entries=[("User", "earlier")],
            tools=None,
        )
        assert prompt.endswith(message)
        assert prompt.startswith("<system_instructions>\nsys\n</system_instructions>")
        assert "<available_tools>" not in prompt

    def test_oversized_history_is_dropped_oldest_first(self):
        entries = [("User", "old " + "x" * 90_000), ("Assistant", "keep me " + "y" * 40_000)]
        prompt = _build_claude_prompt(
            message="now", system_prompt="", history_entries=entries, tools=None
        )
        assert len(prompt.encode("utf-8")) <= 120_000
        assert "keep me" in prompt
        assert "old " not in prompt
        assert prompt.endswith("now")


# ============================================================================
# send_message
# ============================================================================


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_message_calls_fallback(self, gateway):
        mock_usage = MagicMock()
        mock_usage.cost_usd = 0.001
        gateway._send_with_fallback = AsyncMock(
            return_value=("response text", "model-name", MagicMock(), mock_usage),
        )
        text, model, _obj, _usage = await gateway.send_message(
            chat=None,
            message="Hello",
            tier=TIER_FLASH,
        )
        assert text == "response text"
        assert model == "model-name"
        gateway._send_with_fallback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_message_all_fail(self, gateway):
        gateway._send_with_fallback = AsyncMock(side_effect=RuntimeError("All failed"))
        with pytest.raises(RuntimeError, match="All LLM models failed"):
            await gateway.send_message(chat=None, message="Hello")

    @pytest.mark.asyncio
    async def test_send_message_with_system_prompt(self, gateway):
        mock_usage = MagicMock()
        mock_usage.cost_usd = 0.0
        gateway._send_with_fallback = AsyncMock(
            return_value=("response", "model", None, mock_usage),
        )
        await gateway.send_message(
            chat=None,
            message="Hello",
            system_prompt="You are Zantara",
            tier=TIER_PRO,
        )
        call_kwargs = gateway._send_with_fallback.call_args[1]
        assert call_kwargs["system_prompt"] == "You are Zantara"
        assert call_kwargs["model_tier"] == TIER_PRO

    @pytest.mark.asyncio
    async def test_send_message_with_conversation(self, gateway):
        mock_usage = MagicMock()
        mock_usage.cost_usd = 0.0
        gateway._send_with_fallback = AsyncMock(
            return_value=("r", "m", None, mock_usage),
        )
        await gateway.send_message(
            chat=None,
            message="Hello",
            conversation_messages=[{"role": "user", "content": "prev"}],
        )
        call_kwargs = gateway._send_with_fallback.call_args[1]
        assert len(call_kwargs["conversation_messages"]) == 1


# ============================================================================
# send_message latency telemetry (2026-07-18)
# ============================================================================


class TestSendMessageTelemetry:
    @pytest.mark.asyncio
    async def test_send_message_logs_latency_on_success(self, gateway, caplog):
        mock_usage = MagicMock()
        mock_usage.cost_usd = 0.001
        mock_usage.prompt_tokens = 42
        mock_usage.completion_tokens = 7
        gateway._send_with_fallback = AsyncMock(
            return_value=("response text", "claude-sonnet-5-5", MagicMock(), mock_usage),
        )

        with caplog.at_level("INFO"):
            await gateway.send_message(chat=None, message="Hello", tier=TIER_FLASH)

        records = [r for r in caplog.records if r.message == "LLM gateway call"]
        assert len(records) == 1
        assert records[0].latency_ms >= 0
        assert records[0].model == "claude-sonnet-5-5"
        assert records[0].provider == "claude_console"
        assert records[0].prompt_tokens == 42
        assert records[0].completion_tokens == 7

    @pytest.mark.asyncio
    async def test_send_message_emits_metric_on_success(self, gateway):
        mock_usage = MagicMock()
        mock_usage.cost_usd = 0.0
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        gateway._send_with_fallback = AsyncMock(
            return_value=("response text", "claude-sonnet-5-5", None, mock_usage),
        )

        with patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock) as mock_emit:
            await gateway.send_message(chat=None, message="Hello", tier=TIER_FLASH)

        mock_emit.assert_awaited_once()
        assert mock_emit.call_args.kwargs["provider"] == "claude_console"
        assert mock_emit.call_args.kwargs["model"] == "claude-sonnet-5-5"
        assert mock_emit.call_args.kwargs["prompt_tokens"] == 10
        assert mock_emit.call_args.kwargs["completion_tokens"] == 5
        assert mock_emit.call_args.kwargs["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_send_message_logs_latency_on_total_failure(self, gateway, caplog):
        gateway._send_with_fallback = AsyncMock(side_effect=RuntimeError("All failed"))

        with caplog.at_level("ERROR"), pytest.raises(RuntimeError, match="All LLM models failed"):
            await gateway.send_message(chat=None, message="Hello")

        records = [r for r in caplog.records if r.message == "All LLM models failed"]
        assert len(records) == 1
        assert records[0].latency_ms >= 0


# ============================================================================
# _send_with_fallback — fail-closed cascade, no third-party route
# ============================================================================


class TestSendWithFallback:
    @pytest.mark.asyncio
    async def test_a_failing_slug_falls_through_to_the_next(self, gateway):
        gateway.model_name_flash = "claude-first"
        gateway.model_name_fallback = "claude-second"
        usage = MagicMock(cost_usd=0.0)
        gateway._call_model = AsyncMock(
            side_effect=[ClaudeOAuthError("seats failed"), ("ok", MagicMock(), usage)]
        )

        text, model, _obj, _usage = await gateway._send_with_fallback(
            chat=None,
            message="hi",
            system_prompt="",
            model_tier=TIER_FLASH,
            enable_function_calling=False,
            conversation_messages=[],
            query_cost_tracker={"cost": 0.0, "depth": 0},
        )

        assert (text, model) == ("ok", "claude-second")

    @pytest.mark.asyncio
    async def test_when_every_slug_fails_it_fails_closed(self, gateway):
        gateway._call_model = AsyncMock(side_effect=ClaudeOAuthNotAvailable("none"))

        with pytest.raises(RuntimeError, match="fallback chain failed"):
            await gateway._send_with_fallback(
                chat=None,
                message="hi",
                system_prompt="",
                model_tier=TIER_FLASH,
                enable_function_calling=False,
                conversation_messages=[],
                query_cost_tracker={"cost": 0.0, "depth": 0},
            )
