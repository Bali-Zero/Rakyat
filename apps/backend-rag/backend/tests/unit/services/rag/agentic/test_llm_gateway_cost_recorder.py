"""
The cost ledger is written by the completion client, not by the gateway.

Context: ``LLMGateway._call_model()`` used to call the raw Gemini SDK, so it had to
record ``llm_cost_events`` itself (and for a while it did not, undercounting the
ledger ~3x). It now answers through ``claude_oauth_client.complete_async``, which
records every call -- real envelope usage, provider ``claude_console`` -- under the
endpoint it is given. These tests pin the two halves of that contract on both
``_call_model`` branches (chat with a ``.history``, and no chat):

- the call is attributed to ``rag.gateway.chat``;
- the gateway writes NO second row of its own (that would double-count the spend).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthResponse
from backend.services.rag.agentic.llm_gateway import LLMGateway

_MODULE = "backend.services.rag.agentic.llm_gateway"


def _answer() -> ClaudeOAuthResponse:
    return ClaudeOAuthResponse(
        text="Hello from Claude", token_label="console_1", elapsed_s=0.1, attempts=1
    )


@pytest.fixture
def gateway():
    gw = LLMGateway()
    gw._available = True
    return gw


class TestCallModelCostLedger:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("chat", [None, SimpleNamespace(history=[])])
    async def test_both_branches_attribute_one_call_to_the_gateway_endpoint(self, gateway, chat):
        with (
            patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer())) as call,
            patch(
                "backend.services.observability.record_llm_call",
                new_callable=AsyncMock,
            ) as gateway_row,
        ):
            text, _response, usage = await gateway._call_model(
                "claude-sonnet-5-5",
                with_tools=False,
                chat=chat,
                message="hi",
                images=None,
            )

        assert text == "Hello from Claude"
        call.assert_awaited_once()
        assert call.call_args.kwargs["endpoint"] == "rag.gateway.chat"
        assert call.call_args.kwargs["model"] == "claude-sonnet-5-5"
        gateway_row.assert_not_awaited()  # one row per call: the client's, not a second one
        assert usage.prompt_tokens >= 1
        assert usage.completion_tokens >= 1

    @pytest.mark.asyncio
    async def test_a_chat_session_history_is_kept_current(self, gateway):
        chat = SimpleNamespace(history=[])

        with patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_answer())):
            await gateway._call_model(
                "claude-sonnet-5-5", with_tools=False, chat=chat, message="hi", images=None
            )

        assert chat.history == [
            {"role": "user", "parts": [{"text": "hi"}]},
            {"role": "model", "parts": [{"text": "Hello from Claude"}]},
        ]

    @pytest.mark.asyncio
    async def test_a_provider_failure_propagates_unchanged(self, gateway):
        boom = RuntimeError("seats down")

        with patch(f"{_MODULE}.complete_async", new=AsyncMock(side_effect=boom)):
            with pytest.raises(RuntimeError) as exc_info:
                await gateway._call_model(
                    "claude-sonnet-5-5", with_tools=False, chat=None, message="hi", images=None
                )

        assert exc_info.value is boom
