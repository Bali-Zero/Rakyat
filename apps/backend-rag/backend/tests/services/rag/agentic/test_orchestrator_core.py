from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.prompts import channel_overlays
from backend.services.llm_clients.pricing import TokenUsage
from backend.services.rag.agentic import orchestrator_core
from backend.services.rag.agentic.orchestrator_core import OrchestratorCore
from backend.services.rag.agentic.schema import AnalyticsReceiptStatus, CoreResult, ProducerOrigin
from backend.services.tools.definitions import AgentState


class FakeEmbedder:
    async def generate_query_embedding(self, query: str) -> list[float]:
        assert query == "cached query"
        return [0.1, 0.2, 0.3]


class FakeSemanticCache:
    def __init__(self, cached: dict | None) -> None:
        self.cached = cached
        self.calls: list[tuple[str, object]] = []

    async def get_cached_result(self, query: str, query_embedding) -> dict | None:
        self.calls.append((query, query_embedding))
        return self.cached


def make_core() -> OrchestratorCore:
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.semantic_cache = None
    core.retriever = None
    core.entity_extractor = None
    core.kg_retrieval = None
    core.kg_langgraph_orchestrator = None
    core.db_pool = None
    core.reasoning_engine = None
    core.llm_gateway = object()
    core.context_manager = None
    core.query_gates = None
    core.prompt_builder = None
    core.routing_manager = None
    return core


def make_fast_path_core() -> OrchestratorCore:
    core = make_core()
    core._query_planner = None
    core._multi_agent_coordinator = None
    core._specialized_router = None
    core._kg_auto_expansion = None
    core.db_pool = object()
    core.reasoning_engine = SimpleNamespace(tool_map={"vector_search": object()})
    core.prepare_query_context = AsyncMock(return_value=({"profile": None}, [], {}, "", None))
    core.query_gates = SimpleNamespace(
        run_all_gates=lambda **_kwargs: SimpleNamespace(triggered=False)
    )
    core.check_faq_cache = AsyncMock(return_value=None)
    core.check_semantic_cache = AsyncMock(return_value=None)
    core._inject_curated_qa_grounding = AsyncMock(return_value="")
    core._try_kg_fast_path = AsyncMock(return_value=None)
    core._log_query_analytics = AsyncMock(return_value=AnalyticsReceiptStatus.WRITTEN)
    return core


@pytest.mark.asyncio
async def test_process_query_core_fast_answer_logs_once_and_skips_react() -> None:
    core = make_fast_path_core()
    core.routing_manager = SimpleNamespace(route_query=AsyncMock())
    fast_result = CoreResult(
        answer="Synthetic fast answer",
        sources=[{"collection": "legal_test", "title": "Synthetic source"}],
        model_used="claude-sonnet-5-5",
        route_used="fast_answer",
        context_used=80,
        timings={"total": 0.2},
    )

    with (
        patch.object(orchestrator_core, "try_fast_answer", AsyncMock(return_value=fast_result)),
        patch.object(orchestrator_core, "spawn") as spawn_mock,
    ):
        result = await core.process_query_core(
            query="Synthetic legal question",
            user_id=None,
            conversation_history=None,
            start_time=0.0,
        )

    assert result is fast_result
    assert result.producer_origin is ProducerOrigin.FAST_ANSWER
    assert core._log_query_analytics.await_count == 1
    core.routing_manager.route_query.assert_not_awaited()
    spawn_mock.assert_not_called()


@pytest.mark.asyncio
async def test_process_query_core_fast_answer_none_runs_react() -> None:
    core = make_fast_path_core()
    state = AgentState(query="Synthetic fallback question")
    state.evidence_score = 0.8
    core.routing_manager = SimpleNamespace(
        route_query=AsyncMock(return_value=("flash", False, state))
    )
    core.prompt_builder = SimpleNamespace(build_system_prompt=lambda **_kwargs: "Synthetic prompt")
    core.llm_gateway = SimpleNamespace(create_chat_with_history=lambda **_kwargs: object())
    core.execute_react_loop = AsyncMock(return_value=(state, "gemini-test", TokenUsage(), 0.1))
    core._run_grading_gates = AsyncMock()
    core.metrics_manager = SimpleNamespace(
        extract_timings_from_state=lambda **_kwargs: {"total": 0.3},
        extract_collections_from_state=lambda _state: set(),
        extract_sources_from_state=lambda _state: [],
        calculate_context_used=lambda _state: 0,
        record_rag_metrics=MagicMock(),
        record_token_usage=MagicMock(),
        log_query_completion=MagicMock(),
    )
    core.response_builder = SimpleNamespace(
        build_core_result=lambda **_kwargs: CoreResult(
            answer="Synthetic ReAct answer", route_used="react"
        )
    )

    with patch.object(orchestrator_core, "try_fast_answer", AsyncMock(return_value=None)):
        result = await core.process_query_core(
            query="Synthetic fallback question",
            user_id=None,
            conversation_history=None,
            start_time=0.0,
        )

    assert result.answer == "Synthetic ReAct answer"
    core.execute_react_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_semantic_cache_returns_core_result_with_embedding(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator_core.time, "time", lambda: 12.0)
    core = make_core()
    core.retriever = SimpleNamespace(embedder=FakeEmbedder())
    core.semantic_cache = FakeSemanticCache(
        {
            "result": {
                "answer": "cached answer",
                "sources": [{"title": "source"}],
            },
        },
    )

    result = await core.check_semantic_cache(
        query="cached query",
        extracted_entities={"visa": ["KITAS"]},
        start_time=10.0,
    )

    assert result is not None
    assert result.answer == "cached answer"
    assert result.cache_hit is True
    assert result.entities == {"visa": ["KITAS"]}
    assert result.timings == {"total": 2.0}
    assert core.semantic_cache.calls[0][0] == "cached query"
    assert core.semantic_cache.calls[0][1].tolist() == pytest.approx([0.1, 0.2, 0.3])


@pytest.mark.asyncio
async def test_extract_entities_and_kg_context_merges_entity_and_graph_summary() -> None:
    core = make_core()
    core.entity_extractor = SimpleNamespace(
        extract_entities=AsyncMock(return_value={"visa_types": ["KITAS"]}),
    )
    core.kg_retrieval = SimpleNamespace(
        get_context_for_query=AsyncMock(
            return_value=SimpleNamespace(
                graph_summary="KG summary",
                entities_found=["KITAS"],
                relationships=["REQUIRES"],
            ),
        ),
    )

    entities, prompt_context, workflow = await core.extract_entities_and_kg_context("KITAS docs")

    assert entities == {"visa_types": ["KITAS"]}
    assert "KNOWN ENTITIES" in prompt_context
    assert "KG summary" in prompt_context
    assert workflow is None


@pytest.mark.asyncio
async def test_execute_react_loop_wraps_reasoning_engine_result() -> None:
    state = AgentState(query="question")
    usage = TokenUsage(prompt_tokens=3, completion_tokens=4, cost_usd=0.0)

    async def fake_execute_react_loop(**kwargs):
        assert kwargs["query"] == "question"
        assert kwargs["user_id"] == "user-1"
        assert kwargs["initial_prompt"] != "question"
        return state, "gemini-test", [{"role": "user"}], usage

    core = make_core()
    core.reasoning_engine = SimpleNamespace(execute_react_loop=fake_execute_react_loop)

    result_state, model, token_usage, duration = await core.execute_react_loop(
        state=state,
        chat=object(),
        system_prompt="system",
        query="question",
        user_id="user-1",
        model_tier="flash",
        tool_execution_counter={"count": 0},
    )

    assert result_state is state
    assert model == "gemini-test"
    assert token_usage is usage
    assert duration >= 0


@pytest.mark.asyncio
async def test_prepare_query_context_falls_back_when_context_loading_fails() -> None:
    core = make_core()
    core.context_manager = SimpleNamespace(
        get_full_context=AsyncMock(side_effect=RuntimeError("context down")),
    )
    core.extract_entities_and_kg_context = AsyncMock(
        return_value=({"entities": []}, "kg context", None),
    )

    user_context, history, entities, kg_context, workflow = await core.prepare_query_context(
        query="question",
        user_id="user-1",
        conversation_history=[{"role": "user"}],
    )

    assert user_context == {}
    assert history == []
    assert entities == {"entities": []}
    assert kg_context == "kg context"
    assert workflow is None


@pytest.mark.asyncio
async def test_check_gates_and_cache_returns_gate_before_cache() -> None:
    gate_result = SimpleNamespace(triggered=True)
    core_result = CoreResult(answer="gate", model_used="identity-gate")
    core = make_core()
    core.query_gates = SimpleNamespace(
        run_all_gates=lambda **kwargs: gate_result,
        gate_result_to_core_result=lambda **kwargs: core_result,
    )
    core.check_semantic_cache = AsyncMock()

    result = await core.check_gates_and_cache(
        query="who are you",
        user_context={},
        history=[],
        extracted_entities={},
        start_time=0.0,
    )

    assert result is core_result
    core.check_semantic_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_query_core_merges_caller_profile_into_user_context() -> None:
    """WA team-assistant V1: a caller-supplied `profile` override must land
    in user_context["profile"] (merged on top of whatever the DB-keyed
    context lookup found) BEFORE gates/cache/ReAct ever see it — the gate
    call is the earliest observation point, so short-circuit there."""
    core = make_core()
    core._query_planner = None
    core.prepare_query_context = AsyncMock(
        return_value=(
            {"profile": {"id": "db-id", "email": "stale@example.com"}, "facts": []},
            [],
            {},
            "",
            None,
        ),
    )
    seen_user_context: dict = {}

    def _capture_gates(**kwargs):
        seen_user_context.update(kwargs["user_context"])
        return SimpleNamespace(triggered=True)

    sentinel = CoreResult(answer="gated", model_used="gate")
    core.query_gates = SimpleNamespace(
        run_all_gates=_capture_gates,
        gate_result_to_core_result=lambda *a, **kw: sentinel,
    )

    result = await core.process_query_core(
        query="hello",
        user_id="whatsapp_628400010000",
        conversation_history=None,
        start_time=0.0,
        profile={"role": "team", "name": "Test Member Alpha"},
    )

    assert result is sentinel
    # Caller-supplied fields win; fields the DB lookup found (and the
    # caller didn't override, e.g. "id") survive the merge.
    assert seen_user_context["profile"] == {
        "id": "db-id",
        "email": "stale@example.com",
        "role": "team",
        "name": "Test Member Alpha",
    }


@pytest.mark.asyncio
async def test_process_query_core_no_profile_is_a_no_op() -> None:
    """Every existing caller passes profile=None — user_context["profile"]
    must be exactly what prepare_query_context returned, untouched."""
    core = make_core()
    core._query_planner = None
    core.prepare_query_context = AsyncMock(
        return_value=({"profile": None, "facts": []}, [], {}, "", None),
    )
    seen_user_context: dict = {}

    def _capture_gates(**kwargs):
        seen_user_context.update(kwargs["user_context"])
        return SimpleNamespace(triggered=True)

    sentinel = CoreResult(answer="gated", model_used="gate")
    core.query_gates = SimpleNamespace(
        run_all_gates=_capture_gates,
        gate_result_to_core_result=lambda *a, **kw: sentinel,
    )

    await core.process_query_core(
        query="hello",
        user_id="anonymous",
        conversation_history=None,
        start_time=0.0,
    )

    assert seen_user_context["profile"] is None


@pytest.mark.asyncio
async def test_prepare_react_execution_stamps_agent_role_and_channel(monkeypatch) -> None:
    state = AgentState(query="question")
    core = make_core()
    core.routing_manager = SimpleNamespace(
        route_query=AsyncMock(return_value=("flash", False, state)),
    )
    core.prompt_builder = SimpleNamespace(
        build_system_prompt=lambda **kwargs: f"PROMPT:{kwargs['additional_context']}",
    )
    # Patched at its DEFINITION (`channel_overlays`), not at an import alias in
    # `orchestrator_core`: since 2026-08-11 this path calls the shared
    # `apply_channel_overlay` helper, so the real append logic runs underneath
    # this stub. The expected prompt below is byte-identical to what this test
    # asserted when the three lines were inline here — that identity is the
    # point: the streaming path's output must not move when the non-streaming
    # twin gains the same overlay.
    monkeypatch.setattr(channel_overlays, "build_channel_context", lambda channel: f"{channel}:ctx")

    model_tier, deep_think, prepared_state, prompt = await core.prepare_react_execution(
        query="question",
        user_context={"profile": {"id": "user-1"}},
        history=[],
        extracted_entities={},
        kg_context_str="KG",
        channel="whatsapp",
        agent_role="ops",
    )

    assert model_tier == "flash"
    assert deep_think is False
    assert prepared_state is state
    assert prepared_state.agent_role == "ops"
    assert prompt == "PROMPT:KG\n\nwhatsapp:ctx"


@pytest.mark.asyncio
async def test_prepare_react_execution_defaults_an_absent_channel_to_webapp(monkeypatch) -> None:
    """The STREAMING path's `or "webapp"` default, pinned from its own side.

    Its non-streaming twin deliberately has NO such default (see
    test_channel_overlay_nonstreaming.py). The asymmetry is the whole design
    decision of 2026-08-11 — this path has always defaulted, that one has
    never applied any overlay — so both halves need a test, or "tidying them
    into agreement" only breaks whichever side happens to be unpinned.
    """
    state = AgentState(query="question")
    core = make_core()
    core.routing_manager = SimpleNamespace(
        route_query=AsyncMock(return_value=("flash", False, state)),
    )
    core.prompt_builder = SimpleNamespace(
        build_system_prompt=lambda **kwargs: f"PROMPT:{kwargs['additional_context']}",
    )
    monkeypatch.setattr(channel_overlays, "build_channel_context", lambda channel: f"{channel}:ctx")

    _, _, _, prompt = await core.prepare_react_execution(
        query="question",
        user_context={"profile": None},
        history=[],
        extracted_entities={},
        kg_context_str="KG",
        channel=None,
        agent_role=None,
    )

    assert prompt == "PROMPT:KG\n\nwebapp:ctx"
