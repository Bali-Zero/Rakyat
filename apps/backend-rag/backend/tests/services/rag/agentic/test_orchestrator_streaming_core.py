import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.rag.agentic import orchestrator_streaming_core
from backend.services.rag.agentic.orchestrator_core import OrchestratorCore
from backend.services.rag.agentic.orchestrator_streaming_core import OrchestratorStreamingCore
from backend.services.rag.agentic.schema import (
    AnalyticsReceiptStatus,
    CoreResult,
    EvidenceProvenance,
    FinalizationStatus,
    ProducerOrigin,
    TrustedBypassReason,
)
from backend.services.tools.definitions import AgentState


class FakeStreamingManager:
    def create_done_event(self, *, execution_time: float, route_used: str, **kwargs) -> dict:
        return {
            "type": "done",
            "data": {"execution_time": execution_time, "route_used": route_used, **kwargs},
        }

    def create_initial_status_event(self, correlation_id: str) -> dict:
        return {"type": "status", "data": {"correlation_id": correlation_id}}

    def create_error_event(
        self,
        *,
        error_type: str,
        message: str,
        correlation_id: str,
    ) -> dict:
        return {
            "type": "error",
            "data": {
                "error_type": error_type,
                "message": message,
                "correlation_id": correlation_id,
            },
        }

    async def process_event_stream(self, *, event_generator, **_kwargs):
        async for event in event_generator:
            yield event


@pytest.mark.asyncio
async def test_stream_core_result_yields_metadata_tokens_sources_and_done(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(orchestrator_streaming_core.asyncio, "sleep", no_sleep)
    result = SimpleNamespace(
        answer="Hello world",
        sources=[{"title": "source"}],
        model_used="greeting-gate",
        timings={"total": 0.25},
    )
    core = OrchestratorStreamingCore(core=object(), streaming_manager=FakeStreamingManager())

    events = [event async for event in core._stream_core_result(result, route_used="gate")]

    assert events[0]["type"] == "metadata"
    assert events[0]["data"] == {
        "status": "greeting",
        "route": "gate",
        "model_used": "greeting-gate",
    }
    assert events[1:3] == [
        {"type": "token", "data": "Hello "},
        {"type": "token", "data": "world "},
    ]
    assert events[3] == {"type": "sources", "data": [{"title": "source"}]}
    assert events[4] == {
        "type": "done",
        "data": {"execution_time": 0.25, "route_used": "gate"},
    }


@pytest.mark.asyncio
async def test_stream_core_result_uses_verification_status_for_non_gate() -> None:
    result = SimpleNamespace(
        answer="Answer",
        sources=[],
        model_used="gemini",
        verification_status="unchecked",
        timings={"total": 0.0},
    )
    core = OrchestratorStreamingCore(core=object(), streaming_manager=FakeStreamingManager())

    events = [event async for event in core._stream_core_result(result, route_used="agentic")]

    assert events[0]["data"]["status"] == "unchecked"
    assert events[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_single_event_generator_yields_original_event() -> None:
    core = OrchestratorStreamingCore(core=object(), streaming_manager=FakeStreamingManager())
    event = {"type": "token", "data": "hello"}

    assert [item async for item in core._single_event_generator(event)] == [event]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected_origin", "expected_provenance", "expected_trust"),
    [
        (
            CoreResult(answer="hello", model_used="greeting-gate"),
            ProducerOrigin.QUERY_GATE,
            None,
            TrustedBypassReason.DETERMINISTIC_QUERY_GATE,
        ),
        (
            CoreResult(
                answer="cached",
                sources=[{"source": "cache-source"}],
                model_used="cache",
                cache_hit=True,
            ),
            ProducerOrigin.SEMANTIC_CACHE,
            EvidenceProvenance.SEMANTIC_CACHE,
            None,
        ),
    ],
)
async def test_gate_and_cache_stream_cross_finalization_before_first_token(
    monkeypatch,
    result: CoreResult,
    expected_origin: ProducerOrigin,
    expected_provenance: EvidenceProvenance | None,
    expected_trust: TrustedBypassReason | None,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(orchestrator_streaming_core.asyncio, "sleep", no_sleep)
    real_core = OrchestratorCore.__new__(OrchestratorCore)
    real_core.db_pool = None
    real_core.prepare_query_context = AsyncMock(return_value=({}, [], {}, "", None))
    real_core.check_gates_and_cache = AsyncMock(return_value=result)
    streaming_core = OrchestratorStreamingCore(
        core=real_core,
        streaming_manager=FakeStreamingManager(),
    )

    with patch.object(streaming_core, "_mark_react_stream_unfinalized") as marker:
        events = [
            event
            async for event in streaming_core.stream_query_core(
                query="stream contract",
                user_id="contract-user",
                conversation_history=None,
                session_id=None,
                images=None,
                tool_execution_counter={"count": 0},
                correlation_id="contract-correlation",
            )
        ]

    marker.assert_not_called()

    first_token_index = next(
        index for index, event in enumerate(events) if event["type"] == "token"
    )
    finalization_metadata = next(
        event
        for event in events[:first_token_index]
        if event["type"] == "metadata" and "finalization_status" in event["data"]
    )
    assert (
        finalization_metadata["data"]["finalization_status"] is FinalizationStatus.SHADOW_RECORDED
    )
    assert finalization_metadata["data"]["producer_origin"] is expected_origin
    assert finalization_metadata["data"]["evidence_provenance"] is expected_provenance
    assert finalization_metadata["data"]["trusted_bypass_reason"] is expected_trust
    assert finalization_metadata["data"]["analytics_receipt"] is AnalyticsReceiptStatus.SKIPPED
    assert result.answer in {"hello", "cached"}


def test_react_stream_gap_is_fail_visible_and_cannot_claim_complete() -> None:
    core = OrchestratorStreamingCore(core=object(), streaming_manager=FakeStreamingManager())

    with (
        patch.object(orchestrator_streaming_core, "add_span_event") as span_event,
        patch.object(orchestrator_streaming_core.logger, "warning") as warning,
    ):
        core._mark_react_stream_unfinalized()

    assert orchestrator_streaming_core.REACT_STREAM_FINALIZATION_COMPLETE is False
    span_event.assert_called_once_with(
        "finalization.stream_react_unfinalized",
        {
            "coverage_complete": False,
            "reason": "tokens_emitted_before_confidence_and_analytics",
        },
    )
    warning.assert_called_once()


class _RawReactReasoningEngine:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.tool_map: dict = {}

    async def execute_react_loop_stream(self, **_kwargs):
        yield {"type": "token", "data": "raw-token"}
        if self.outcome == "error":
            raise RuntimeError("raw stream failed")
        if self.outcome == "cancel":
            raise asyncio.CancelledError


class _RawReactCore:
    def __init__(self, outcome: str) -> None:
        self.reasoning_engine = _RawReactReasoningEngine(outcome)
        self.llm_gateway = SimpleNamespace(
            create_chat_with_history=lambda **_kwargs: object(),
        )
        self.metrics_manager = SimpleNamespace(
            extract_collections_from_state=lambda _state: set(),
            extract_sources_from_state=lambda _state: [],
        )
        self.db_pool = None

    async def prepare_query_context(self, **_kwargs):
        return ({"profile": None}, [], {}, "", None)

    async def check_gates_and_cache(self, **_kwargs):
        return None

    async def prepare_react_execution(self, **_kwargs):
        state = AgentState(query="raw stream contract", intent_type="business_complex")
        state.evidence_score = 0.8
        state.trusted_tools_used = True
        return ("gemini-parent", False, state, "system prompt")


class _ToolCallReactReasoningEngine:
    def __init__(self, raw_event: dict) -> None:
        self.raw_event = raw_event
        self.tool_map: dict = {}

    async def execute_react_loop_stream(self, **_kwargs):
        yield self.raw_event


class _ToolCallReactCore(_RawReactCore):
    def __init__(self, raw_event: dict) -> None:
        super().__init__("complete")
        self.reasoning_engine = _ToolCallReactReasoningEngine(raw_event)


@pytest.mark.asyncio
async def test_stream_query_core_names_tool_from_producer_tool_event() -> None:
    streaming_core = OrchestratorStreamingCore(
        core=_ToolCallReactCore(
            {"type": "tool_call", "data": {"tool": "vector_search", "args": {}}}
        ),
        streaming_manager=FakeStreamingManager(),
    )

    events = [
        event
        async for event in streaming_core.stream_query_core(
            query="raw stream contract",
            user_id="contract-user",
            conversation_history=None,
            session_id=None,
            images=None,
            tool_execution_counter={"count": 0},
            correlation_id="contract-correlation",
        )
    ]

    tool_thinking_event = next(
        event
        for event in events
        if event["type"] == "thinking" and event["phase"] == "tool_calling"
    )
    assert "vector_search" in tool_thinking_event["data"]
    assert "unknown tool" not in tool_thinking_event["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected_tool_name"),
    [
        ({"tool_name": "legacy_vector_search", "args": {}}, "legacy_vector_search"),
        ({"args": {}}, "unknown tool"),
    ],
)
async def test_stream_query_core_keeps_tool_name_fallbacks(
    data: dict,
    expected_tool_name: str,
) -> None:
    streaming_core = OrchestratorStreamingCore(
        core=_ToolCallReactCore({"type": "tool_call", "data": data}),
        streaming_manager=FakeStreamingManager(),
    )

    events = [
        event
        async for event in streaming_core.stream_query_core(
            query="raw stream contract",
            user_id="contract-user",
            conversation_history=None,
            session_id=None,
            images=None,
            tool_execution_counter={"count": 0},
            correlation_id="contract-correlation",
        )
    ]

    assert any(
        event["type"] == "thinking"
        and event["phase"] == "tool_calling"
        and expected_tool_name in event["data"]
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expect_error_event", "expect_cancel"),
    [
        ("complete", False, False),
        ("error", True, False),
        ("cancel", False, True),
    ],
)
async def test_real_raw_react_stream_marks_every_terminal_path_exactly_once(
    outcome: str,
    expect_error_event: bool,
    expect_cancel: bool,
) -> None:
    streaming_core = OrchestratorStreamingCore(
        core=_RawReactCore(outcome),
        streaming_manager=FakeStreamingManager(),
    )

    with patch.object(streaming_core, "_mark_react_stream_unfinalized") as marker:
        if expect_cancel:
            with pytest.raises(asyncio.CancelledError):
                async for _event in streaming_core.stream_query_core(
                    query="raw stream contract",
                    user_id="contract-user",
                    conversation_history=None,
                    session_id=None,
                    images=None,
                    tool_execution_counter={"count": 0},
                    correlation_id="contract-correlation",
                ):
                    pass
            events = []
        else:
            events = [
                event
                async for event in streaming_core.stream_query_core(
                    query="raw stream contract",
                    user_id="contract-user",
                    conversation_history=None,
                    session_id=None,
                    images=None,
                    tool_execution_counter={"count": 0},
                    correlation_id="contract-correlation",
                )
            ]

    marker.assert_called_once_with()
    assert any(event.get("data") == "raw-token" for event in events) is not expect_cancel
    assert any(event["type"] == "error" for event in events) is expect_error_event


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_read", [1, 2, 3, 4])
async def test_w8_a_failing_analytics_clock_costs_the_timings_never_the_stream(
    failing_read: int,
) -> None:
    """Stage-timing spec W8 (gate 9198 r1): the reasoning clock is analytics-only.

    A failing read (parametrized over each of the four reads: loop start, before and
    after the one yield, loop end) must leave the user's stream intact (done, no
    error event) and the analytics row written, without timings.
    """
    core = _RawReactCore("complete")
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": "00000000-0000-0000-0000-0000000000b8"})
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    core.db_pool = SimpleNamespace(acquire=lambda: ctx)
    extract = AsyncMock()
    core.metrics_manager.extract_timings_from_state = extract
    reads = {"n": 0}

    def clock() -> float:
        reads["n"] += 1
        if reads["n"] == failing_read:
            raise OSError("clock unavailable")
        return 100.0

    streaming_core = OrchestratorStreamingCore(
        core=core,
        streaming_manager=FakeStreamingManager(),
    )
    with (
        patch.object(orchestrator_streaming_core, "_ANALYTICS_CLOCK", clock),
        patch.object(streaming_core, "_mark_react_stream_unfinalized"),
    ):
        events = [
            event
            async for event in streaming_core.stream_query_core(
                query="raw stream contract",
                user_id="contract-user",
                conversation_history=None,
                session_id=None,
                images=None,
                tool_execution_counter={"count": 0},
                correlation_id="contract-correlation",
            )
        ]

    assert reads["n"] == 4  # start, before+after the one yield (M2), end
    assert not any(event["type"] == "error" for event in events)
    assert any(event["type"] == "done" for event in events)
    extract.assert_not_called()
    assert conn.fetchrow.await_count == 1
    metadata = conn.fetchrow.call_args[0][1:]
    assert not any(isinstance(arg, dict) and "timings" in arg for arg in metadata)


# ─────────────────────────────────────────────────────────────────────────
# TOURNIQUET (2026-07-21) — CRM pre-call must be gated to authenticated
# staff. The prefetch calls crm_tool.execute() DIRECTLY (bypasses
# tool_executor/ToolAuthorizer entirely), so Part 1's SENSITIVE_TOOLS deny
# in tool_authorizer.py does not cover this path on its own. See memory
# `discovery_crm_pii_public_exposure_blog_ask_timesheet_2026_07_21`.
# ─────────────────────────────────────────────────────────────────────────


class _StopAfterCrmPrefetch(Exception):
    """Sentinel raised right after the CRM pre-call block completes, so the
    test never has to mock the real ReAct loop machinery downstream."""


class FakeReasoningEngine:
    def __init__(self, tool_map: dict) -> None:
        self.tool_map = tool_map


class FakeCoreForCrmPrefetch:
    """Minimal OrchestratorCore stand-in: just enough surface to drive
    stream_query_core through context-prep + gate-check + the CRM pre-call
    block, then bail out via prepare_react_execution before touching the
    real ReAct loop."""

    def __init__(self, tool_map: dict) -> None:
        self.reasoning_engine = FakeReasoningEngine(tool_map)

    async def prepare_query_context(self, **_kwargs):
        return ({"user_id": "u"}, [], {}, "", None)

    async def check_gates_and_cache(self, **_kwargs):
        return None

    async def prepare_react_execution(self, **_kwargs):
        raise _StopAfterCrmPrefetch


async def _drain_until_crm_prefetch(core: OrchestratorStreamingCore, **kwargs) -> None:
    with pytest.raises(_StopAfterCrmPrefetch):
        async for _event in core.stream_query_core(**kwargs):
            pass


@pytest.mark.asyncio
async def test_crm_prefetch_skipped_for_no_principal_streaming_caller() -> None:
    """GUILT: agent_role=None (public /stream — blog/IG/anon web-chat) must
    never call crm_tool.execute() directly, even when the query matches a
    CRM keyword."""
    crm_tool = SimpleNamespace(execute=AsyncMock(return_value="42 active clients"))
    fake_core = FakeCoreForCrmPrefetch(tool_map={"crm_query": crm_tool})
    streaming_core = OrchestratorStreamingCore(
        core=fake_core, streaming_manager=FakeStreamingManager()
    )

    await _drain_until_crm_prefetch(
        streaming_core,
        query="quanti clienti attivi abbiamo",
        user_id="anonymous",
        conversation_history=None,
        session_id=None,
        images=None,
        tool_execution_counter={"count": 0},
        correlation_id="cid-no-principal",
        agent_role=None,
    )

    crm_tool.execute.assert_not_called()


@pytest.mark.asyncio
async def test_crm_prefetch_fires_for_authenticated_staff_caller() -> None:
    """INNOCENCE: staff callers (agent_role set, /workspace-stream) keep the
    existing CRM pre-call UX — no regression from the tourniquet."""
    crm_tool = SimpleNamespace(execute=AsyncMock(return_value="42 active clients online"))
    fake_core = FakeCoreForCrmPrefetch(tool_map={"crm_query": crm_tool})
    streaming_core = OrchestratorStreamingCore(
        core=fake_core, streaming_manager=FakeStreamingManager()
    )

    await _drain_until_crm_prefetch(
        streaming_core,
        query="quanti clienti attivi abbiamo",
        user_id="damar@balizero.com",
        conversation_history=None,
        session_id=None,
        images=None,
        tool_execution_counter={"count": 0},
        correlation_id="cid-staff",
        agent_role=SimpleNamespace(role_id="visa_specialist"),
    )

    crm_tool.execute.assert_called_once_with(query_type="client_stats")
