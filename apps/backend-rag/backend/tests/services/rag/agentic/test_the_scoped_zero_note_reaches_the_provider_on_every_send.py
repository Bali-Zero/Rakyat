"""The scoped-zero note reaches the model provider on every send of a streamed turn.

When the CRM pre-call finds nothing within the signed-in user's scope, the turn carries
a note: any CRM fact in the answer must come from a tool result of this turn. The
streaming loop sends the model up to six different messages in a turn, and a provider
fallback is handed the current message alone, without the chat. So the note is appended
to the text of every one of them.

Asserted where the provider is called. The reasoning engine, the gateway's send and its
fallback chain, and the chat session are the real ones. Stood in: the Gemini SDK call
(down, or answering), the OpenRouter client's ``complete``, the tool body, the evidence
score that selects a branch, and the two telemetry writers that run after a provider
has answered.
"""

from __future__ import annotations

import ast
import copy
import inspect
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.rag.agentic import reasoning as reasoning_module
from backend.services.rag.agentic._reasoning_tier1 import (
    TRANSPARENCY_INSTRUCTION_FINAL,
    TRANSPARENCY_INSTRUCTION_NO_CONTEXT,
    build_tier1_prompt,
)
from backend.services.rag.agentic.llm_gateway import TIER_FLASH, LLMGateway
from backend.services.rag.agentic.reasoning import ReasoningEngine
from backend.services.tools.definitions import AgentState, ToolCall

Q = "how many clients do we have"
PROMPT = "Answer in English.\n\n" + Q
# What the turn can find elsewhere, and must not take a CRM fact from.
HISTORY = [
    {"role": "user", "content": "how many clients?"},
    {"role": "assistant", "content": "You have 57 clients; the newest is Client Zero."},
]
CHUNK = "Internal memo: the firm serves 99 clients, among them Client Nine."
CONSTRAINT = (
    "Any CRM client, practice or count in your answer must come from a crm_query result of "
    "this turn. Do not take one from earlier turns or from retrieved documents."
)
NOTES = {
    "practice_stats": "The CRM holds no practices within the signed-in user's scope. " + CONSTRAINT,
    "expiring_documents": (
        "The CRM holds no passport, visa or document expiring in the next 30 days within the "
        "signed-in user's scope. " + CONSTRAINT
    ),
    "search_clients": (
        "The automatic CRM lookup matched no client for this question as typed; that is not a "
        "search result. To look a client up, call crm_query with the name: results are limited "
        "to the signed-in user's scope. " + CONSTRAINT
    ),
}
# One turn per send of the loop. Every turn starts with the loop's first message; the
# last message of each is the send it is named after. ``answers`` is what the model
# says, in order ("CALL" asks for a retrieval); ``evidence`` selects the branch.
TURNS: dict[str, dict[str, Any]] = {
    "the loop's next step, after an observation": {
        "max_steps": 2,
        "answers": ["CALL", "Final Answer: fine"],
        "evidence": 0.9,
    },
    "the regeneration over an existing answer": {
        "max_steps": 2,
        "answers": ["CALL", "Final Answer: 57 clients", "regenerated"],
        "evidence": 0.0,
    },
    "the fallback with context and no answer": {
        "max_steps": 1,
        "answers": ["CALL", "fallback"],
        "evidence": 0.0,
    },
    "the answer from gathered context": {
        "max_steps": 1,
        "answers": ["CALL", "gathered"],
        "evidence": 0.9,
    },
    "the general-task answer": {
        "max_steps": 1,
        "answers": ["", "general"],
        "evidence": 0.0,
        "skip_rag": True,
    },
    "the answer from general knowledge, with no context": {
        "max_steps": 1,
        "answers": ["", "knowledge"],
        "evidence": 0.0,
    },
}


def _is_the_send_it_is_named_after(turn: str, message: str) -> bool:
    """The last message of a turn with no note, as main builds it."""
    if turn.startswith("the loop"):
        return message.startswith(f"Original user query: {Q}\n\nObservation: {CHUNK}\n\n")
    if turn.startswith("the regeneration"):
        return message == build_tier1_prompt(Q, [CHUNK])
    if turn.startswith("the fallback"):
        return message == build_tier1_prompt(
            Q, [CHUNK], transparency_instruction=TRANSPARENCY_INSTRUCTION_FINAL
        )
    if turn.startswith("the answer from gathered"):
        return (
            "Based on the information gathered:" in message
            and CHUNK in message
            and f"Provide a final, comprehensive answer to: {Q}" in message
        )
    if turn.startswith("the general-task"):
        return message == f"Please answer this request: {Q}"
    return message == build_tier1_prompt(
        Q,
        None,
        transparency_instruction=TRANSPARENCY_INSTRUCTION_NO_CONTEXT,
        include_context_section=False,
    )


class _OpenRouter:
    """The fallback provider's client: keeps the messages it is sent."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.received: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], **_: Any) -> SimpleNamespace:
        self.received.append(copy.deepcopy(messages))
        return SimpleNamespace(
            content=self._answers.pop(0),
            model_name="stand-in",
            model_used="stand-in",
            prompt_tokens=1,
            completion_tokens=1,
        )

    def user_messages(self) -> list[str]:
        assert all(
            len(m) == 2 and m[0] == {"role": "system", "content": "system"} for m in self.received
        )
        assert all(m[1]["role"] == "user" for m in self.received)
        return [m[1]["content"] for m in self.received]


class _Gemini:
    """The first provider's client: down when it has no answers, else keeps its input."""

    is_available = True

    def __init__(self, answers: list[str] | None) -> None:
        self._answers = answers
        self.received: list[list[dict[str, Any]]] = []
        models = SimpleNamespace(generate_content=self._generate)
        self._client = SimpleNamespace(aio=SimpleNamespace(models=models))

    def start_chat(self, **_: Any) -> object:
        # The gateway does not send through the SDK's own chat object: it rebuilds the
        # model's input from the session's history at every call.
        return object()

    async def _generate(self, *, model: str, contents: Any, config: Any) -> SimpleNamespace:
        self.received.append(copy.deepcopy(contents))
        if self._answers is None:
            raise RuntimeError("the provider is down")
        return SimpleNamespace(
            text=self._answers.pop(0), candidates=[], function_calls=None, usage_metadata=None
        )

    def user_messages(self) -> list[str]:
        assert all(c[-1]["role"] == "user" and len(c[-1]["parts"]) == 1 for c in self.received)
        return [c[-1]["parts"][0]["text"] for c in self.received]

    def earlier_turns(self) -> list[str]:
        return [
            " ".join(p["text"] for c in sent[:-1] for p in c["parts"]) for sent in self.received
        ]


def _retrieval(_response: Any, text: str) -> tuple[list[ToolCall], str]:
    if text != "CALL":
        return [], "none"
    return [ToolCall(tool_name="vector_search", arguments={"query": "clients"})], "native"


@pytest.fixture(autouse=True)
def _stage() -> Iterator[None]:
    with (
        patch("backend.services.rag.agentic.reasoning.trace_span") as trace_span,
        patch("backend.services.rag.agentic.reasoning.set_span_attribute"),
        patch("backend.services.rag.agentic.reasoning.set_span_status"),
        patch("backend.services.rag.agentic.reasoning.add_span_event"),
        patch(
            "backend.services.rag.agentic.reasoning.emit_low_confidence_event",
            new_callable=AsyncMock,
        ),
        patch("backend.services.rag.agentic.reasoning.is_critical_domain", return_value=False),
        patch(
            "backend.services.rag.agentic.reasoning.parse_tool_calls_from_response",
            side_effect=_retrieval,
        ),
        patch(
            "backend.services.rag.agentic.reasoning.execute_tool",
            AsyncMock(return_value=(CHUNK, 0.01)),
        ),
        # Telemetry written after a provider has answered; not on the message's way.
        patch("backend.services.rag.agentic.llm_gateway.emit_llm_metric", new_callable=AsyncMock),
        patch("backend.services.observability.record_llm_call", new_callable=AsyncMock),
    ):
        trace_span.return_value.__enter__ = MagicMock()
        trace_span.return_value.__exit__ = MagicMock(return_value=False)
        yield


async def _turn(turn: str, note: str = "", *, gemini_answers: bool = False) -> SimpleNamespace:
    """One streamed turn through the real engine and the real gateway."""
    script = TURNS[turn]
    answers = list(script["answers"])
    gemini = _Gemini(answers if gemini_answers else None)
    openrouter = _OpenRouter([] if gemini_answers else answers)
    gateway = LLMGateway()
    gateway._genai_client = gemini
    gateway._openrouter_client = openrouter
    chat = gateway.create_chat_with_history(
        history_to_use=HISTORY, model_tier=TIER_FLASH, system_instruction="system"
    )
    state = AgentState(query=Q, max_steps=script["max_steps"], intent_type="simple")
    state.skip_rag = script.get("skip_rag", False)
    state.crm_scope_note = note
    engine = ReasoningEngine(tool_map={}, response_pipeline=None)
    with patch(
        "backend.services.rag.agentic.reasoning.compute_evidence_score",
        return_value=script["evidence"],
    ):
        events = [
            event
            async for event in engine.execute_react_loop_stream(
                state=state,
                llm_gateway=gateway,
                chat=chat,
                initial_prompt=PROMPT,
                system_prompt="system",
                query=Q,
                user_id="member.one@example.test",
                model_tier=TIER_FLASH,
                tool_execution_counter={"count": 0},
            )
        ]
    assert not [e for e in events if e.get("type") == "error"]
    assert answers == []  # every scripted answer was asked for
    return SimpleNamespace(state=state, gemini=gemini, openrouter=openrouter)


def _suffix(note: str) -> str:
    return "\n\n[CRM NOTE] " + note


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", list(TURNS))
async def test_a_turn_with_no_note_sends_what_it_always_sent(turn: str) -> None:
    ran = await _turn(turn)

    sent = ran.openrouter.user_messages()
    assert len(sent) == len(TURNS[turn]["answers"])
    assert sent[0] == PROMPT
    assert _is_the_send_it_is_named_after(turn, sent[-1])
    assert ran.state.final_answer == TURNS[turn]["answers"][-1].removeprefix("Final Answer: ")
    assert not [m for m in sent if "CRM NOTE" in m]


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", list(NOTES))
@pytest.mark.parametrize("turn", list(TURNS))
async def test_with_the_first_provider_down_every_message_of_the_turn_ends_with_the_note(
    turn: str, query_type: str
) -> None:
    note = NOTES[query_type]
    plain = await _turn(turn)

    noted = await _turn(turn, note)

    assert noted.gemini.received  # the first provider was tried, and failed
    sent = noted.openrouter.user_messages()
    assert sent == [message + _suffix(note) for message in plain.openrouter.user_messages()]
    assert len(sent) == len(TURNS[turn]["answers"])
    assert all(message.endswith(note) for message in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", list(NOTES))
@pytest.mark.parametrize("turn", list(TURNS))
async def test_with_the_first_provider_answering_every_message_it_is_given_ends_with_the_note(
    turn: str, query_type: str
) -> None:
    note = NOTES[query_type]
    plain = await _turn(turn, gemini_answers=True)

    noted = await _turn(turn, note, gemini_answers=True)

    assert noted.openrouter.received == []
    sent = noted.gemini.user_messages()
    assert sent == [message + _suffix(note) for message in plain.gemini.user_messages()]
    assert len(sent) == len(TURNS[turn]["answers"])
    assert _is_the_send_it_is_named_after(turn, plain.gemini.user_messages()[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turn", ["the regeneration over an existing answer", "the fallback with context and no answer"]
)
async def test_the_note_is_in_the_message_whenever_a_conflicting_count_is(turn: str) -> None:
    note = NOTES["practice_stats"]

    down = await _turn(turn, note)
    last = down.openrouter.user_messages()[-1]
    assert "99 clients" in last  # the retrieved text is in the regenerated prompt
    assert last.endswith(_suffix(note))

    up = await _turn(turn, note, gemini_answers=True)
    # The first provider is also given the earlier turns, with their own count.
    assert all("57 clients" in earlier for earlier in up.gemini.earlier_turns())
    assert "99 clients" in up.gemini.user_messages()[-1]
    assert all(message.endswith(_suffix(note)) for message in up.gemini.user_messages())


@pytest.mark.asyncio
@pytest.mark.parametrize("turn", list(TURNS))
async def test_the_note_is_no_evidence_no_context_and_changes_no_branch(turn: str) -> None:
    plain = await _turn(turn)

    noted = await _turn(turn, NOTES["expiring_documents"])

    for name in ("evidence_score", "context_gathered", "trusted_tools_used", "skip_rag"):
        assert getattr(noted.state, name) == getattr(plain.state, name), name
    assert getattr(noted.state, "sources", None) == getattr(plain.state, "sources", None)
    assert noted.state.final_answer == plain.state.final_answer
    assert len(noted.state.steps) == len(plain.state.steps)
    assert not [c for c in noted.state.context_gathered if "CRM NOTE" in c or CONSTRAINT in c]


def test_every_send_of_the_streaming_loop_passes_its_message_through_the_note_helper() -> None:
    module = ast.parse(inspect.getsource(reasoning_module))
    (loop,) = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_react_loop_stream"
    ]
    sends = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
    ]
    assert len(sends) >= 6

    for send in sends:
        keywords = {k.arg: k.value for k in send.keywords}
        message = send.args[1] if len(send.args) > 1 else keywords.get("message")
        where = f"reasoning.py line {send.lineno}"
        assert isinstance(message, ast.Call), where
        assert isinstance(message.func, ast.Name), where
        assert message.func.id == "with_crm_scope_note", where
        assert isinstance(message.args[0], ast.Name) and message.args[0].id == "state", where
    assert reasoning_module.with_crm_scope_note.__module__.endswith("_crm_precall")
