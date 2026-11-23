"""What the streaming CRM pre-call hands the model: data, a scoped zero, or nothing.

Before the reasoning loop starts, the streaming core asks the CRM tool one question as
the signed-in desk user. Rows or counts of the shape that query returns are injected as
they always were. A valid empty list becomes a fixed note on the turn's state and
nothing else: no data block, no trust, retrieval not skipped. A refusal, an error
object or anything malformed tells the model nothing at all.

The streaming core and its manager are the real ones; the collaborators around them and
the tool's answer are stood in, and the loop is stopped as it starts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.services.rag.agentic._crm_precall import (
    DATA,
    NOTHING,
    SCOPED_ZERO,
    judge_crm_precall,
    with_crm_scope_note,
)
from backend.services.rag.agentic.orchestrator_streaming import OrchestratorStreamingManager
from backend.services.rag.agentic.orchestrator_streaming_core import OrchestratorStreamingCore
from backend.services.rag.agentic.query_helpers import wrap_query_with_language_instruction
from backend.services.rag.agentic.tools import CRMTool
from backend.services.tools.definitions import AgentState

ME = "member.one@example.test"
CONSTRAINT = (
    "Any CRM client, practice or count in your answer must come from a crm_query result of "
    "this turn. Do not take one from earlier turns or from retrieved documents."
)
# The notes, written out: these are the words the model is given.
NOTES = {
    "practice_stats": "The CRM holds no practices within the signed-in user's scope. " + CONSTRAINT,
    "expiring_documents": (
        "The CRM holds no visa or stay-permit practice in progress within the signed-in user's "
        "scope. " + CONSTRAINT
    ),
    "search_clients": (
        "The automatic CRM lookup matched no client for this question as typed; that is not a "
        "search result. To look a client up, call crm_query with the name: results are limited "
        "to the signed-in user's scope. " + CONSTRAINT
    ),
}
# One question per query type the pre-call can choose.
QUESTIONS = {
    "client_stats": "quanti clienti attivi abbiamo",
    "practice_stats": "mostra le pratiche in corso",
    "expiring_documents": "clienti con il visto in scadenza",
    "search_clients": "cerca tra i clienti Mario",
}
ROWS: dict[str, dict[str, Any]] = {
    "search_clients": {
        "id": 1,
        "full_name": "Client One",
        "email": "c1@example.test",
        "nationality": None,
        "status": "active",
        "phone": None,
    },
    "recent_clients": {
        "id": 1,
        "full_name": "Client One",
        "email": "c1@example.test",
        "nationality": None,
        "status": "active",
        "created_at": "2026-01-02 00:00:00+00:00",
    },
    "expiring_documents": {
        "full_name": "Client One",
        "email": "c1@example.test",
        "nationality": None,
        "practice_type_id": 1,
        "status": "on_process",
        "notes": None,
        "updated_at": "2026-03-02 00:00:00+00:00",
    },
    "practice_stats": {"service_type": "KITAS E33", "count": 3, "on_process": 1, "completed": 0},
}
COUNTS: dict[str, dict[str, int]] = {
    "clients": {"active_clients": 2, "leads": 1, "inactive": 0, "total": 3},
    "practices": {
        "on_process": 1,
        "completed": 0,
        "sending_invoice": 0,
        "waiting_documents": 0,
        "inquiry": 0,
        "cancelled": 0,
        "total": 1,
    },
}
REFUSAL = json.dumps({"error": "CRM access needs a signed-in team member"})


def _answer(query_type: str) -> str:
    """A valid, non-empty answer of that query type."""
    return json.dumps(COUNTS if query_type == "client_stats" else [ROWS[query_type]])


# --- the judgement ------------------------------------------------------------------


@pytest.mark.parametrize("query_type", [*ROWS, "client_stats"])
def test_a_valid_answer_of_its_own_query_type_is_data(query_type: str) -> None:
    assert judge_crm_precall(query_type, _answer(query_type)) == (DATA, "")


@pytest.mark.parametrize("query_type", list(NOTES))
def test_a_valid_empty_list_is_a_scoped_zero_with_that_type_s_note(query_type: str) -> None:
    assert judge_crm_precall(query_type, "[]") == (SCOPED_ZERO, NOTES[query_type])


def _not_rows(query_type: str) -> dict[str, Any]:
    row = ROWS[query_type]
    other = ROWS["practice_stats" if query_type != "practice_stats" else "search_clients"]
    first = next(iter(row))
    return {
        "an empty object": [{}],
        "an object with a null error": [{"error": None}],
        "a row with an error key added": [{**row, "error": "x"}],
        "a row of another query type": [other],
        "a row missing one field": [{k: v for k, v in row.items() if k != first}],
        "a valid row followed by a broken one": [row, {}],
        "a row that is a list": [list(row)],
        "an error object": {"error": "CRM query failed"},
        "an object that wraps the rows": {"rows": [row]},
        "null": None,
        "a number": 42,
        "a string": "no rows",
    }


@pytest.mark.parametrize("query_type", list(ROWS))
def test_anything_that_is_not_rows_of_that_query_type_is_nothing(query_type: str) -> None:
    for name, value in _not_rows(query_type).items():
        assert judge_crm_precall(query_type, json.dumps(value)) == (NOTHING, ""), name


@pytest.mark.parametrize("count", [True, False, -1, 1.0, "3", None])
@pytest.mark.parametrize("field", ["count", "on_process", "completed"])
def test_a_practice_group_whose_count_is_no_whole_number_is_nothing(field: str, count: Any) -> None:
    answer = json.dumps([{**ROWS["practice_stats"], field: count}])

    assert judge_crm_precall("practice_stats", answer) == (NOTHING, "")


def _not_counts() -> dict[str, Any]:
    clients, practices = COUNTS["clients"], COUNTS["practices"]
    return {
        "an empty list": [],
        "an empty object": {},
        "the clients alone": {"clients": clients},
        "a third block": {**COUNTS, "error": None},
        "a client count missing": {"clients": {"total": 3}, "practices": practices},
        "a client count added": {"clients": {**clients, "vip": 1}, "practices": practices},
        "a practice count missing": {"clients": clients, "practices": {"total": 1}},
        "a true count": {"clients": {**clients, "leads": True}, "practices": practices},
        "a negative count": {"clients": clients, "practices": {**practices, "total": -1}},
        "a fractional count": {"clients": {**clients, "total": 3.0}, "practices": practices},
        "a block that is a list": {"clients": [3], "practices": practices},
        "an error object": {"error": "CRM query failed"},
    }


def test_client_counts_that_are_not_the_two_complete_blocks_are_nothing() -> None:
    for name, value in _not_counts().items():
        assert judge_crm_precall("client_stats", json.dumps(value)) == (NOTHING, ""), name


@pytest.mark.parametrize("query_type", [*ROWS, "client_stats", "everything"])
def test_a_refusal_or_no_json_at_all_is_nothing(query_type: str) -> None:
    for answer in (REFUSAL, "Error: connection refused", "", None, 7, b"[]"):
        assert judge_crm_precall(query_type, answer) == (NOTHING, "")


def test_an_empty_list_is_no_scoped_zero_where_the_table_has_no_note() -> None:
    assert judge_crm_precall("recent_clients", "[]") == (NOTHING, "")
    assert judge_crm_precall("everything", "[]") == (NOTHING, "")


def test_the_note_is_appended_to_a_message_and_an_empty_note_changes_nothing() -> None:
    message = "Original user query: x"

    assert with_crm_scope_note(AgentState(query="x"), message) is message
    assert with_crm_scope_note(SimpleNamespace(), message) is message
    noted = AgentState(query="x")
    noted.crm_scope_note = NOTES["practice_stats"]
    assert with_crm_scope_note(noted, message) == (
        message + "\n\n[CRM NOTE] " + NOTES["practice_stats"]
    )
    assert AgentState(query="x").crm_scope_note == ""


# --- the pre-call, in the streaming core --------------------------------------------


class _TheLoopStarts(Exception):
    pass


class _Loop:
    """Where the reasoning engine is: holds the tool, keeps what the loop is started with."""

    def __init__(self, tool: Any) -> None:
        self.tool_map = {} if tool is None else {"crm_query": tool}
        self.started: list[dict[str, Any]] = []

    async def execute_react_loop_stream(self, **kwargs: Any) -> Any:
        self.started.append(kwargs)
        raise _TheLoopStarts
        yield  # pragma: no cover


class _Core:
    def __init__(self, tool: Any) -> None:
        self.reasoning_engine = _Loop(tool)
        self.llm_gateway = SimpleNamespace(create_chat_with_history=lambda **_: object())
        self.state: AgentState | None = None

    async def prepare_query_context(self, **_: Any) -> tuple[Any, ...]:
        return ({"user_id": "u"}, [], {}, "", None)

    async def check_gates_and_cache(self, **_: Any) -> None:
        return None

    async def prepare_react_execution(self, *, query: str, **_: Any) -> tuple[Any, ...]:
        self.state = AgentState(query=query)
        return ("flash", False, self.state, "system")


def _says(answer: Any) -> SimpleNamespace:
    return SimpleNamespace(execute=AsyncMock(return_value=answer))


async def _turn(
    tool: Any,
    question: str,
    *,
    user_id: str | None = ME,
    principal: Any = SimpleNamespace(role_id="visa_specialist", desk_role="Consultant"),
) -> tuple[AgentState, str]:
    """One streamed turn up to the start of the loop: its state and its first prompt."""
    core = _Core(tool)
    streaming = OrchestratorStreamingCore(
        core=core, streaming_manager=OrchestratorStreamingManager()
    )
    async for _event in streaming.stream_query_core(
        query=question,
        user_id=user_id,
        conversation_history=None,
        session_id=None,
        images=None,
        tool_execution_counter={"count": 0},
        correlation_id="corr",
        agent_role=principal,
    ):
        pass
    (started,) = core.reasoning_engine.started
    assert started["state"] is core.state
    return core.state, started["initial_prompt"]


def _told_nothing(state: AgentState, prompt: str, question: str) -> None:
    assert prompt == wrap_query_with_language_instruction(question)
    assert state.context_gathered == []
    assert state.trusted_tools_used is False
    assert state.skip_rag is False
    assert state.crm_scope_note == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", list(QUESTIONS))
async def test_the_pre_call_asks_as_the_desk_user_and_data_is_injected_as_before(
    query_type: str,
) -> None:
    question, answer = QUESTIONS[query_type], _answer(query_type)
    tool = _says(answer)

    state, prompt = await _turn(tool, question)

    asked: dict[str, Any] = {"query_type": query_type}
    if query_type == "search_clients":
        asked["search_term"] = question
    tool.execute.assert_awaited_once_with(**asked, _desk_user={"email": ME, "role": "Consultant"})
    block = f"\n\n[CRM DATABASE RESULTS — USE THESE NUMBERS IN YOUR ANSWER]\n{answer}\n"
    assert prompt == wrap_query_with_language_instruction(question) + block
    assert state.context_gathered == [block]
    assert state.trusted_tools_used is True
    assert state.skip_rag is True
    assert state.crm_scope_note == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", list(NOTES))
async def test_a_scoped_zero_puts_its_note_on_the_turn_and_nothing_anywhere_else(
    query_type: str,
) -> None:
    question = QUESTIONS[query_type]

    state, prompt = await _turn(_says("[]"), question)

    assert state.crm_scope_note == NOTES[query_type]
    # No data block and no note in the first prompt: the note rides on the messages.
    assert prompt == wrap_query_with_language_instruction(question)
    assert state.context_gathered == []
    assert state.trusted_tools_used is False
    assert state.skip_rag is False


@pytest.mark.asyncio
@pytest.mark.parametrize("query_type", list(QUESTIONS))
async def test_a_refusal_an_error_or_a_malformed_answer_tells_the_model_nothing(
    query_type: str,
) -> None:
    question = QUESTIONS[query_type]
    answers: list[Any] = [
        REFUSAL,
        json.dumps({"error": "CRM query failed: timeout"}),
        "Error: connection refused while querying the CRM",
        json.dumps([{"error": None}]),
        json.dumps([{}]),
        None,
    ]
    if query_type == "client_stats":
        answers += ["[]", json.dumps({"clients": COUNTS["clients"]})]
    else:
        answers.append(json.dumps([{**ROWS[query_type], "error": "x"}]))

    for answer in answers:
        state, prompt = await _turn(_says(answer), question)
        _told_nothing(state, prompt, question)


@pytest.mark.asyncio
async def test_a_tool_that_raises_tells_the_model_nothing() -> None:
    tool = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("down")))

    state, prompt = await _turn(tool, QUESTIONS["practice_stats"])

    _told_nothing(state, prompt, QUESTIONS["practice_stats"])


class _NeverTouched:
    """A database pool that fails the test if the tool goes near it."""

    def __bool__(self) -> bool:
        return True

    def acquire(self) -> Any:
        raise AssertionError("the tool reached for the database")


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", ["anonymous", "member.one", "whatsapp_628000000000"])
async def test_the_real_tool_refuses_a_user_that_is_no_address_and_the_model_is_told_nothing(
    user_id: str,
) -> None:
    question = QUESTIONS["client_stats"]

    state, prompt = await _turn(CRMTool(db_pool=_NeverTouched()), question, user_id=user_id)

    _told_nothing(state, prompt, question)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "principal"),
    [
        pytest.param(
            ME, SimpleNamespace(role_id="visa_specialist"), id="a principal with no desk role"
        ),
        pytest.param(ME, SimpleNamespace(role_id="x", desk_role=None), id="a desk role of None"),
        pytest.param(None, SimpleNamespace(role_id="x", desk_role="admin"), id="no user"),
        pytest.param("", SimpleNamespace(role_id="x", desk_role="admin"), id="an empty user"),
        pytest.param(ME, None, id="no principal"),
    ],
)
async def test_without_a_desk_session_the_pre_call_asks_nothing(
    user_id: str | None, principal: Any
) -> None:
    question = QUESTIONS["client_stats"]
    tool = _says(_answer("client_stats"))

    state, prompt = await _turn(tool, question, user_id=user_id, principal=principal)

    tool.execute.assert_not_awaited()
    _told_nothing(state, prompt, question)
