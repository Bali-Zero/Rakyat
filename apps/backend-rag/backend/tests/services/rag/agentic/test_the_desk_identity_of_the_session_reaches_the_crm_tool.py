"""The desk identity of the signed-in session reaches the assistant's CRM tool.

The workspace route stamps the role of the session on a copy of the registry's
principal, one copy per request. The tool executor then hands the tool ``_desk_user``,
built from the e-mail of the request and that role, under a reserved name: whatever
the model supplies under such a name is dropped before the authorizer sees the
arguments and again before the trusted values are put in.

The route, the orchestrator's entry, the streaming core and its manager, the loop
preparation, both reasoning loops, the executor and the authorizer are the real ones here. The model, the
query-context preparation and the tool body are stood in. Invented addresses.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from backend.app.routers import agentic_rag as agentic
from backend.app.routers.agentic_rag import WorkspaceQueryRequest, _derive_wa_agent_role
from backend.services.agents import team_agent_config
from backend.services.agents.team_agent_config import ROLE_VISA_SPECIALIST, TEAM_AGENTS
from backend.services.agents.tool_authorizer import AuthResult, ToolAuthorizer
from backend.services.rag.agentic import tool_executor
from backend.services.rag.agentic.orchestrator import AgenticRAGOrchestrator
from backend.services.rag.agentic.orchestrator_core import OrchestratorCore
from backend.services.rag.agentic.orchestrator_streaming import OrchestratorStreamingManager
from backend.services.rag.agentic.orchestrator_streaming_core import OrchestratorStreamingCore
from backend.services.rag.agentic.reasoning import ReasoningEngine
from backend.services.rag.agentic.tool_executor import execute_tool
from backend.services.tools.definitions import AgentState, ToolCall

pytestmark = pytest.mark.asyncio

ONE = "member.one@example.test"
TWO = "member.two@example.test"
THREE = "member.three@example.test"
FORGED = {"email": "admin.seven@example.test", "role": "admin"}
QUESTION = "quanti clienti attivi abbiamo"
_COUNTS = json.dumps(
    {
        "clients": {"active_clients": 1, "leads": 0, "inactive": 0, "total": 1},
        "practices": {
            "on_process": 0,
            "completed": 0,
            "sending_invoice": 0,
            "waiting_documents": 0,
            "inquiry": 0,
            "cancelled": 0,
            "total": 0,
        },
    }
)


class _Tool:
    """Stands in for the CRM tool's body: keeps the arguments of every call."""

    name = "crm_query"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **arguments: Any) -> str:
        self.calls.append(arguments)
        return _COUNTS

    def desk_users(self) -> list[Any]:
        return [call.get("_desk_user") for call in self.calls]


class _Model:
    """Stands in for the model gateway: asks for the CRM tool once per chat, then answers.

    ``hold`` is how many turns must be waiting for their first answer before any gets
    one, so that requests really overlap.
    """

    gemini_tools: list[Any] = []

    def __init__(self, hold: int = 1) -> None:
        self._hold = hold
        self._waiting = 0
        self._all_in = asyncio.Event()
        self._seen: set[int] = set()
        self.most_waiting = 0

    def create_chat_with_history(self, **_: Any) -> object:
        return SimpleNamespace(history=[])

    async def send_message(self, chat: Any, *_: Any, **__: Any) -> tuple[str, str, Any, Any]:
        first = id(chat) not in self._seen
        self._seen.add(id(chat))
        if first:
            self._waiting += 1
            self.most_waiting = max(self.most_waiting, self._waiting)
            if self._waiting >= self._hold:
                self._all_in.set()
            await self._all_in.wait()
            self._waiting -= 1
        text = "calling crm" if first else "Final Answer: done"
        return text, "stand-in", MagicMock(candidates=[MagicMock()]), MagicMock()


def _asks_for_the_tool(_response: Any, text: str) -> tuple[list[ToolCall], str]:
    if text != "calling crm":
        return [], "none"
    # The model tries every reserved name; none of it may arrive.
    forged = {
        "query_type": "client_stats",
        "_desk_user": dict(FORGED),
        "_caller_profile": {"role": "creator"},
        "_user_id": FORGED["email"],
    }
    return [ToolCall(tool_name="crm_query", arguments=forged)], "native"


@pytest.fixture(autouse=True)
def _stage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Three invented addresses share ONE registry object, as colleagues of one role do.
    for address in (ONE, TWO, THREE):
        monkeypatch.setitem(TEAM_AGENTS, address, ROLE_VISA_SPECIALIST)
    # The executor's authorizer is a module global that other test files replace and
    # leave in place: these tests run against the real one, whatever ran before them.
    monkeypatch.setattr(tool_executor, "_authorizer", ToolAuthorizer())
    monkeypatch.setattr(tool_executor, "_confirmation_service", None)
    with (
        patch("backend.services.rag.agentic.reasoning.trace_span") as trace_span,
        patch("backend.services.rag.agentic.reasoning.set_span_attribute"),
        patch("backend.services.rag.agentic.reasoning.set_span_status"),
        patch("backend.services.rag.agentic.reasoning.add_span_event"),
        patch(
            "backend.services.rag.agentic.reasoning.emit_low_confidence_event",
            new_callable=AsyncMock,
        ),
        patch("backend.services.rag.agentic.reasoning.compute_evidence_score", return_value=0.9),
        patch("backend.services.rag.agentic.reasoning.is_critical_domain", return_value=False),
        patch(
            "backend.services.rag.agentic.reasoning.parse_tool_calls_from_response",
            side_effect=_asks_for_the_tool,
        ),
    ):
        trace_span.return_value.__enter__ = MagicMock()
        trace_span.return_value.__exit__ = MagicMock(return_value=False)
        yield


class _AfterTheLoop(Exception):
    """Raised by the first step after the non-streaming loop: the tool has been called."""


def _core(tool: _Tool, model: _Model) -> OrchestratorCore:
    with (
        patch("backend.services.rag.agentic.orchestrator_core.QueryPlanner"),
        patch("backend.services.rag.agentic.orchestrator_core.MultiAgentCoordinator"),
        patch(
            "backend.services.rag.agentic.orchestrator_core.requires_multi_agent",
            return_value=False,
        ),
        patch("backend.services.rag.agentic.orchestrator_core.KGAutoExpansion"),
    ):
        extractor = MagicMock()
        extractor.extract_entities = AsyncMock(return_value={"domain": "general"})
        core = OrchestratorCore(
            llm_gateway=model,
            reasoning_engine=ReasoningEngine(tool_map={"crm_query": tool}, response_pipeline=None),
            prompt_builder=MagicMock(),
            query_gates=MagicMock(),
            memory_handler=MagicMock(),
            context_window_manager=MagicMock(),
            entity_extractor=extractor,
            kg_retrieval=None,
            semantic_cache=None,
            faq_cache=None,
            db_pool=None,
        )
    core.context_manager.get_basic_context = AsyncMock(return_value={"profiles": [], "facts": []})
    core.retriever = None
    core.query_gates.run_all_gates = MagicMock(return_value=SimpleNamespace(triggered=False))
    core.check_faq_cache = AsyncMock(return_value=None)
    core.check_semantic_cache = AsyncMock(return_value=None)
    core.prompt_builder.build_system_prompt = MagicMock(return_value="system")

    async def a_fresh_state(query: str) -> tuple[str, bool, AgentState]:
        return "flash", False, AgentState(query=query, max_steps=2, intent_type="simple")

    core.routing_manager = SimpleNamespace(route_query=a_fresh_state)
    core._run_grading_gates = AsyncMock(side_effect=_AfterTheLoop)
    return core


def _desk(tool: _Tool, model: _Model) -> AgenticRAGOrchestrator:
    """The orchestrator the workspace route is given, over a real streaming core."""
    core = _core(tool, model)
    core.prepare_query_context = AsyncMock(return_value=({"user_id": "u"}, [], {}, "", None))
    core.check_gates_and_cache = AsyncMock(return_value=None)
    orchestrator = AgenticRAGOrchestrator.__new__(AgenticRAGOrchestrator)
    orchestrator.streaming_core = OrchestratorStreamingCore(
        core=core, streaming_manager=OrchestratorStreamingManager()
    )
    return orchestrator


def _http_request() -> MagicMock:
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.state.correlation_id = "corr"
    request.is_disconnected = AsyncMock(return_value=False)
    request.headers = {}
    return request


async def _ask_at_the_desk(
    orchestrator: AgenticRAGOrchestrator, tool: _Tool, email: str, role: str | None
) -> None:
    """One turn through the workspace route, read until the loop has called the tool."""
    session = {"email": email} if role is None else {"email": email, "role": role}
    response = await agentic.stream_workspace_agent(
        request_body=WorkspaceQueryRequest(query=QUESTION),
        http_request=_http_request(),
        current_user=session,
        orchestrator=orchestrator,
        db_pool=None,
    )
    body = response.body_iterator
    try:
        async for _chunk in body:
            mine = [u for u in tool.desk_users() if u and u["email"] == email]
            if len(mine) >= 2:
                break
    finally:
        await body.aclose()


# --- the executor ------------------------------------------------------------------


async def _run_tool(
    agent_role: Any, user_id: str | None, arguments: dict[str, Any] | None = None
) -> _Tool:
    tool = _Tool()
    await execute_tool(
        tool_map={"crm_query": tool},
        tool_name="crm_query",
        arguments={"query_type": "client_stats"} if arguments is None else arguments,
        user_id=user_id,
        tool_execution_counter={"count": 0},
        agent_role=agent_role,
    )
    return tool


async def test_a_stamped_principal_gives_the_tool_the_desk_user_of_the_request() -> None:
    tool = await _run_tool(replace(ROLE_VISA_SPECIALIST, desk_role="Consultant"), ONE)

    assert tool.calls == [
        {
            "query_type": "client_stats",
            "_user_id": ONE,
            "_desk_user": {"email": ONE, "role": "Consultant"},
        }
    ]


async def test_a_session_with_no_role_is_still_a_desk_session() -> None:
    tool = await _run_tool(replace(ROLE_VISA_SPECIALIST, desk_role=""), ONE)

    assert tool.desk_users() == [{"email": ONE, "role": ""}]


async def test_a_principal_that_no_desk_session_stamped_gives_no_desk_user() -> None:
    tool = await _run_tool(ROLE_VISA_SPECIALIST, ONE)

    assert tool.calls == [{"query_type": "client_stats", "_user_id": ONE}]


async def test_a_stamped_principal_with_no_user_gives_no_desk_user() -> None:
    tool = await _run_tool(replace(ROLE_VISA_SPECIALIST, desk_role="admin"), None)

    assert [c for c in tool.calls if "_desk_user" in c] == []


class _Seeing:
    """The real authorizer, keeping the arguments it is shown."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.shown: list[dict[str, Any]] = []

    async def authorize(self, user_email: Any, agent_role: Any, tool_name: str, args: Any) -> Any:
        self.shown.append(dict(args))
        return await self._real.authorize(user_email, agent_role, tool_name, args)


@pytest.mark.parametrize("desk_role", [None, "Consultant"])
async def test_what_the_model_puts_under_a_reserved_name_reaches_neither_authorizer_nor_tool(
    monkeypatch: pytest.MonkeyPatch, desk_role: str | None
) -> None:
    seeing = _Seeing(tool_executor._authorizer)
    monkeypatch.setattr(tool_executor, "_authorizer", seeing)
    forged = {
        "query_type": "client_stats",
        "_desk_user": dict(FORGED),
        "_caller_profile": {"role": "creator"},
        "_user_id": FORGED["email"],
    }

    tool = await _run_tool(replace(ROLE_VISA_SPECIALIST, desk_role=desk_role), ONE, forged)

    assert seeing.shown == [{"query_type": "client_stats"}]
    expected: dict[str, Any] = {"query_type": "client_stats", "_user_id": ONE}
    if desk_role is not None:
        expected["_desk_user"] = {"email": ONE, "role": desk_role}
    assert tool.calls == [expected]


class _Adding:
    """An authorizer that hands back arguments carrying a desk user of its own."""

    async def authorize(self, **_: Any) -> AuthResult:
        return AuthResult.allow(args={"query_type": "client_stats", "_desk_user": dict(FORGED)})


@pytest.mark.parametrize("desk_role", [None, "Consultant"])
async def test_a_desk_user_that_comes_back_from_the_authorizer_is_dropped_before_the_real_one(
    monkeypatch: pytest.MonkeyPatch, desk_role: str | None
) -> None:
    monkeypatch.setattr(tool_executor, "_authorizer", _Adding())

    tool = await _run_tool(replace(ROLE_VISA_SPECIALIST, desk_role=desk_role), ONE)

    expected = [] if desk_role is None else [{"email": ONE, "role": desk_role}]
    assert [u for u in tool.desk_users() if u is not None] == expected


# --- the desk: route, streaming core, loop -----------------------------------------


async def test_the_role_of_the_session_reaches_the_pre_call_and_the_loop_s_tool_call() -> None:
    tool, model = _Tool(), _Model()

    await _ask_at_the_desk(_desk(tool, model), tool, ONE, "Consultant")

    mine = {"email": ONE, "role": "Consultant"}
    assert tool.desk_users() == [mine, mine]
    pre_call, in_the_loop = tool.calls
    assert pre_call == {"query_type": "client_stats", "_desk_user": mine}
    # The loop's call went through the executor: the forged values are gone.
    assert in_the_loop == {"query_type": "client_stats", "_user_id": ONE, "_desk_user": mine}


async def test_a_session_with_no_role_claim_asks_as_a_desk_user_with_an_empty_role() -> None:
    tool, model = _Tool(), _Model()

    await _ask_at_the_desk(_desk(tool, model), tool, ONE, None)

    assert tool.desk_users() == [{"email": ONE, "role": ""}] * 2


async def test_requests_that_share_one_registry_role_each_ask_as_their_own_session() -> None:
    registry_before = dict(TEAM_AGENTS)
    tool, model = _Tool(), _Model(hold=2)
    desk = _desk(tool, model)

    # Two turns in flight together: neither gets its first answer before both wait.
    await asyncio.gather(
        _ask_at_the_desk(desk, tool, ONE, "Consultant"),
        _ask_at_the_desk(desk, tool, TWO, "admin"),
    )
    assert model.most_waiting == 2
    # Then one more, after them.
    await _ask_at_the_desk(desk, tool, THREE, "Tax Lead")

    seen = sorted((u["email"], u["role"]) for u in tool.desk_users())
    assert seen == sorted(
        [(ONE, "Consultant")] * 2 + [(TWO, "admin")] * 2 + [(THREE, "Tax Lead")] * 2
    )
    # The registry holds the same objects, and none of them carries a desk role.
    assert TEAM_AGENTS.keys() == registry_before.keys()
    assert all(TEAM_AGENTS[address] is role for address, role in registry_before.items())
    assert all(role.desk_role is None for role in TEAM_AGENTS.values())
    assert team_agent_config.get_agent_role(ONE) is ROLE_VISA_SPECIALIST
    # A use of the principal that no desk session stamped, after them, carries none.
    after = await _run_tool(team_agent_config.get_agent_role(ONE), ONE)
    assert after.desk_users() == [None]


# --- the non-streaming path --------------------------------------------------------


async def _ask_without_streaming(tool: _Tool, **principal: Any) -> None:
    core = _core(tool, _Model())
    with pytest.raises(_AfterTheLoop):
        await core.process_query_core(
            query=QUESTION,
            user_id=ONE,
            conversation_history=None,
            start_time=0.0,
            **principal,
        )


async def test_a_stamped_principal_reaches_the_tool_on_the_non_streaming_path() -> None:
    tool = _Tool()

    await _ask_without_streaming(
        tool, agent_role=replace(ROLE_VISA_SPECIALIST, desk_role="Consultant")
    )

    # No pre-call on this path: the one call is the loop's.
    assert tool.calls == [
        {
            "query_type": "client_stats",
            "_user_id": ONE,
            "_desk_user": {"email": ONE, "role": "Consultant"},
        }
    ]


async def test_the_whatsapp_entry_passes_the_registry_s_own_object_and_its_profile() -> None:
    profile = {"role": "team", "email": ONE}
    principal = _derive_wa_agent_role(profile)
    assert principal is ROLE_VISA_SPECIALIST
    assert principal.desk_role is None
    tool = _Tool()

    await _ask_without_streaming(tool, agent_role=principal, profile=profile)

    assert len(tool.calls) == 1
    assert "_desk_user" not in tool.calls[0]
    assert tool.calls[0]["_caller_profile"] == profile


async def test_a_query_with_no_principal_is_still_refused_the_crm_tool() -> None:
    tool = _Tool()

    await _ask_without_streaming(tool, agent_role=None)

    assert tool.calls == []


# --- what a trace of the principal can hold ----------------------------------------


async def test_the_stamp_adds_the_role_of_the_session_to_the_principal_and_nothing_else() -> None:
    stamped = replace(ROLE_VISA_SPECIALIST, desk_role="Consultant")

    before, after = asdict(ROLE_VISA_SPECIALIST), asdict(stamped)

    assert {name for name in after if after[name] != before[name]} == {"desk_role"}
    assert after["desk_role"] == "Consultant"
    assert before["desk_role"] is None
