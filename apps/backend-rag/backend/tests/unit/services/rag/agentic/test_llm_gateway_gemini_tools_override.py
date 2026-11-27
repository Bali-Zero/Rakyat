"""W0 safety pre-arm — T-VIS: per-request tool-declaration override (Claude Console lane).

Context: `LLMGateway._gemini_tools` is set ONCE at orchestrator-construction
time (`orchestrator.py.__init__` -> `set_gemini_tools`) and the gateway instance
is SHARED across every concurrent request — mutating `self._gemini_tools` per
request would be a request-isolation bug (request A could leak its tool schema
into request B). The fix threads an OPTIONAL per-call `gemini_tools` override
through `send_message` -> `_send_with_fallback` -> `_call_model`, so a caller can
narrow the tool declarations offered to the model for THIS call only.

Since Wave 2 the declarations reach the model as an `<available_tools>` block of
the prompt plus a strict structured-turn schema, and the model's answer comes back
as `candidates[0].content.parts[*].function_call` so the ReAct loops read it as
`native`. Guilt/innocence pairs:

- GUILT: an override with a narrower tool set is what the prompt names — not the
  shared list.
- INNOCENCE: omitting the override (None) reproduces the shared list unchanged.
- Edge case: `[]` means "offer no tools" (no schema either), never a silent
  fallback to the shared list — `None` and `[]` are different (cicatrix family #3).
- A call to a tool OUTSIDE the offered set is dropped when the answer is parsed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.llm.claude_oauth_client import ClaudeOAuthResponse
from backend.services.rag.agentic._reasoning_loop_helpers import parse_tool_calls_from_response
from backend.services.rag.agentic.llm_gateway import _REACT_TURN_SCHEMA, LLMGateway

_MODULE = "backend.services.rag.agentic.llm_gateway"


def _decl(name: str) -> dict:
    return {
        "name": name,
        "description": f"{name} description",
        "parameters": {"type": "OBJECT", "properties": {}, "required": []},
    }


def _turn(structured: dict | None = None, text: str = "plain") -> ClaudeOAuthResponse:
    return ClaudeOAuthResponse(
        text=text, token_label="console_1", elapsed_s=0.1, attempts=1, structured=structured
    )


def _final(answer: str | None, calls: list | None = None) -> dict:
    return {"thought": "thinking", "tool_calls": calls or [], "final_answer": answer}


@pytest.fixture
def gateway_with_completion():
    """A gateway with a Console seat, a shared tool, and a stood-in completion call."""
    with patch(f"{_MODULE}.complete_async", new=AsyncMock(return_value=_turn())) as call:
        gw = LLMGateway()
        gw._available = True
        gw.set_gemini_tools([_decl("shared_tool")])
        yield gw, call


def _prompt(call) -> str:
    return call.call_args.args[0]


class TestCallModelToolsOverride:
    @pytest.mark.asyncio
    async def test_guilt_override_replaces_shared_tools(self, gateway_with_completion):
        gateway, call = gateway_with_completion

        await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=True,
            chat=None,
            message="hi",
            gemini_tools_override=[_decl("scoped_tool")],
        )

        assert "scoped_tool" in _prompt(call)
        assert "shared_tool" not in _prompt(call)
        assert call.call_args.kwargs["json_schema"] == _REACT_TURN_SCHEMA

    @pytest.mark.asyncio
    async def test_innocence_no_override_uses_shared_tools_unchanged(self, gateway_with_completion):
        gateway, call = gateway_with_completion

        await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=True,
            chat=None,
            message="hi",
            gemini_tools_override=None,
        )

        assert "shared_tool" in _prompt(call)
        assert call.call_args.kwargs["json_schema"] == _REACT_TURN_SCHEMA

    @pytest.mark.asyncio
    async def test_empty_override_offers_no_tools_does_not_fall_back(self, gateway_with_completion):
        """[] must mean 'offer nothing', never a silent fallback to the shared list."""
        gateway, call = gateway_with_completion

        await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=True,
            chat=None,
            message="hi",
            gemini_tools_override=[],
        )

        assert "<available_tools>" not in _prompt(call)
        assert "shared_tool" not in _prompt(call)
        assert call.call_args.kwargs["json_schema"] is None

    @pytest.mark.asyncio
    async def test_override_with_tools_false_offers_no_tools_either_way(
        self, gateway_with_completion
    ):
        """with_tools=False short-circuits the tools branch entirely, whatever the override."""
        gateway, call = gateway_with_completion

        await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=False,
            chat=None,
            message="hi",
            gemini_tools_override=[_decl("scoped_tool")],
        )

        assert "scoped_tool" not in _prompt(call)
        assert "<available_tools>" not in _prompt(call)
        assert call.call_args.kwargs["json_schema"] is None

    @pytest.mark.asyncio
    async def test_override_applies_on_the_chat_history_branch_too(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        chat = SimpleNamespace(history=[{"role": "user", "parts": [{"text": "earlier"}]}])

        await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=True,
            chat=chat,
            message="hi",
            gemini_tools_override=[_decl("scoped_tool")],
        )

        assert "scoped_tool" in _prompt(call)
        assert "shared_tool" not in _prompt(call)
        assert "User: earlier" in _prompt(call)


class TestStructuredTurnBecomesNativeFunctionCalls:
    @pytest.mark.asyncio
    async def test_a_structured_tool_call_is_read_as_a_native_call(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        call.return_value = _turn(
            _final(
                None,
                [{"name": "shared_tool", "arguments_json": '{"query": "kitas e33g"}'}],
            )
        )

        text, response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )
        tool_calls, mode = parse_tool_calls_from_response(response, text)

        assert mode == "native"
        assert [(t.tool_name, t.arguments) for t in tool_calls] == [
            ("shared_tool", {"query": "kitas e33g"})
        ]

    @pytest.mark.asyncio
    async def test_parallel_calls_all_come_through(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        gateway.set_gemini_tools([_decl("shared_tool"), _decl("other_tool")])
        call.return_value = _turn(
            _final(
                None,
                [
                    {"name": "shared_tool", "arguments_json": "{}"},
                    {"name": "other_tool", "arguments_json": ""},
                ],
            )
        )

        text, response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )
        tool_calls, mode = parse_tool_calls_from_response(response, text)

        assert mode == "native"
        assert [t.tool_name for t in tool_calls] == ["shared_tool", "other_tool"]

    @pytest.mark.asyncio
    async def test_a_tool_outside_the_offered_set_is_dropped(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        call.return_value = _turn(
            _final(None, [{"name": "team_my_clients", "arguments_json": "{}"}])
        )

        text, response, _usage = await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=True,
            chat=None,
            message="hi",
            gemini_tools_override=[_decl("scoped_tool")],
        )

        assert response.candidates == []
        assert parse_tool_calls_from_response(response, text)[0] == []

    @pytest.mark.asyncio
    async def test_a_malformed_arguments_string_is_skipped(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        call.return_value = _turn(
            _final(
                None,
                [
                    {"name": "shared_tool", "arguments_json": "{not json"},
                    {"name": "shared_tool", "arguments_json": "[1, 2]"},
                ],
            )
        )

        _text, response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )

        assert response.candidates == []

    @pytest.mark.asyncio
    async def test_a_final_answer_without_calls_is_marked_final(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        call.return_value = _turn(_final("The permit lasts one year."))

        text, response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )

        assert text == "Final Answer: The permit lasts one year."
        assert response.candidates == []

    @pytest.mark.asyncio
    async def test_an_answer_already_marked_final_is_not_marked_twice(
        self, gateway_with_completion
    ):
        gateway, call = gateway_with_completion
        call.return_value = _turn(_final("Final Answer: done"))

        text, _response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )

        assert text == "Final Answer: done"

    @pytest.mark.asyncio
    async def test_a_turn_with_neither_calls_nor_answer_is_just_a_thought(
        self, gateway_with_completion
    ):
        gateway, call = gateway_with_completion
        call.return_value = _turn(_final(None))

        text, _response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )

        assert text == "thinking"
        assert "Final Answer:" not in text

    @pytest.mark.asyncio
    async def test_a_json_answer_with_no_structured_output_is_still_understood(
        self, gateway_with_completion
    ):
        gateway, call = gateway_with_completion
        call.return_value = _turn(
            None,
            text='```json\n{"thought": "t", "tool_calls": [{"name": "shared_tool", '
            '"arguments_json": "{}"}], "final_answer": null}\n```',
        )

        text, response, _usage = await gateway._call_model(
            "claude-sonnet-5-5", with_tools=True, chat=None, message="hi"
        )

        assert parse_tool_calls_from_response(response, text)[1] == "native"

    @pytest.mark.asyncio
    async def test_the_next_step_sees_what_was_called(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        chat = SimpleNamespace(history=[])
        call.return_value = _turn(
            _final(None, [{"name": "shared_tool", "arguments_json": '{"query": "x"}'}])
        )

        await gateway._call_model("claude-sonnet-5-5", with_tools=True, chat=chat, message="hi")

        assert chat.history[0] == {"role": "user", "parts": [{"text": "hi"}]}
        assert chat.history[1]["role"] == "model"
        assert "shared_tool" in chat.history[1]["parts"][0]["text"]


class TestImagesNeverReachTheModel:
    @pytest.mark.asyncio
    async def test_attachments_are_ignored_and_not_serialised(self, gateway_with_completion):
        gateway, call = gateway_with_completion
        marker = "SU1BR0UtQllURVM"

        await gateway._call_model(
            "claude-sonnet-5-5",
            with_tools=False,
            chat=None,
            message="what is this?",
            images=[{"format": "base64", "data": marker, "filename": "a.png"}],
        )

        call.assert_awaited_once()
        assert marker not in _prompt(call)
        assert "a.png" not in _prompt(call)


class TestSendMessageForwardsToolsOverride:
    @pytest.mark.asyncio
    async def test_send_message_forwards_override_to_send_with_fallback(self):
        gateway = LLMGateway()

        from backend.services.llm_clients.pricing import create_token_usage

        override = [_decl("scoped_tool")]
        usage = create_token_usage(prompt_tokens=1, completion_tokens=1, model="m")

        with (
            patch.object(gateway, "_send_with_fallback") as mock_send,
            patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
        ):
            mock_send.return_value = ("r", "m", MagicMock(), usage)
            await gateway.send_message(chat=MagicMock(), message="hi", gemini_tools=override)

        assert mock_send.call_args.kwargs["gemini_tools_override"] == override

    @pytest.mark.asyncio
    async def test_send_message_default_forwards_none(self):
        gateway = LLMGateway()

        from backend.services.llm_clients.pricing import create_token_usage

        usage = create_token_usage(prompt_tokens=1, completion_tokens=1, model="m")

        with (
            patch.object(gateway, "_send_with_fallback") as mock_send,
            patch(f"{_MODULE}.emit_llm_metric", new_callable=AsyncMock),
        ):
            mock_send.return_value = ("r", "m", MagicMock(), usage)
            await gateway.send_message(chat=MagicMock(), message="hi")

        assert mock_send.call_args.kwargs["gemini_tools_override"] is None
