"""
Unit tests for LLMGateway (Claude Console lane)
Target: public surface — init, tools, send_message, health_check, images.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthResponse
from backend.services.rag.agentic.llm_gateway import TIER_FLASH, LLMGateway

_MODULE = "backend.services.rag.agentic.llm_gateway"


def _answer(text: str = "ok") -> ClaudeOAuthResponse:
    return ClaudeOAuthResponse(text=text, token_label="console_1", elapsed_s=0.1, attempts=1)


@pytest.fixture
def llm_gateway():
    """Create LLM gateway instance with a Console seat available."""
    gateway = LLMGateway(gemini_tools=[])
    gateway._available = True
    return gateway


class TestLLMGateway:
    """Tests for LLMGateway"""

    def test_init(self):
        """Test initialization"""
        gateway = LLMGateway(gemini_tools=[])
        assert gateway is not None

    def test_set_gemini_tools(self, llm_gateway):
        """Test setting tool declarations"""
        tools = [{"name": "test_tool"}]
        llm_gateway.set_gemini_tools(tools)
        assert llm_gateway.gemini_tools == tools

    @pytest.mark.asyncio
    async def test_send_message_flash_tier(self, llm_gateway):
        """A plain completion goes out once, on the Console door."""
        with (
            patch(
                f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer("response"))
            ) as call,
            patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
        ):
            response, model, _obj, usage = await llm_gateway.send_message(
                chat=MagicMock(history=[]),
                message="test",
                tier=TIER_FLASH,
                enable_function_calling=False,
            )

        assert response == "response"
        assert model == llm_gateway.model_name_flash
        assert usage.total_tokens >= 2
        call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_message_all_models_fail_is_a_runtime_error(self, llm_gateway):
        """No third-party fallback: a dead lane fails closed."""
        with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=RuntimeError("down"))):
            with pytest.raises(RuntimeError, match="All LLM models failed"):
                await llm_gateway.send_message(chat=None, message="test")

    @pytest.mark.asyncio
    async def test_send_message_without_a_seat_fails_closed(self):
        gateway = LLMGateway(gemini_tools=[])
        with patch(f"{_MODULE}.collect_seats", return_value=[]):
            with pytest.raises(RuntimeError, match="All LLM models failed"):
                await gateway.send_message(chat=None, message="test")

    @pytest.mark.asyncio
    async def test_send_message_with_tools_but_no_function_calling_sends_none(self, llm_gateway):
        llm_gateway.set_gemini_tools([{"name": "test_tool", "description": "d"}])

        with (
            patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer())) as call,
            patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
        ):
            await llm_gateway.send_message(
                chat=MagicMock(history=[]), message="test", enable_function_calling=False
            )

        assert "test_tool" not in call.call_args.args[0]
        assert call.call_args.kwargs["json_schema"] is None

    @pytest.mark.asyncio
    async def test_images_are_never_forwarded(self, llm_gateway):
        """Cloud vision is disabled: attachments must not reach the provider."""
        marker = "QUJDRA-IMAGE-PAYLOAD"
        with (
            patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer())) as call,
            patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
        ):
            await llm_gateway.send_message(
                chat=None,
                message="What is this?",
                enable_function_calling=False,
                images=[{"base64": f"data:image/png;base64,{marker}", "name": "a.png"}],
            )

        assert marker not in call.call_args.args[0]
        call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_health_check(self, llm_gateway):
        """Test health check"""
        with patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer("OK"))):
            health = await llm_gateway.health_check()
        assert health["claude_console"] is True

    @pytest.mark.asyncio
    async def test_send_message_returns_a_four_tuple(self, llm_gateway):
        with (
            patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer("fine"))),
            patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
        ):
            result = await llm_gateway.send_message(
                chat=None, message="test", tier=TIER_FLASH, enable_function_calling=False
            )
        assert len(result) == 4  # (response, model, obj, usage)
