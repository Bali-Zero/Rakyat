from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.rag.agentic._tool_denial import ANONYMOUS_DENIAL_OBSERVATION
from backend.services.tools.definitions import AgentState, ToolCall


@pytest.fixture(autouse=True)
def _patch_stream_dependencies():
    with (
        patch("backend.services.rag.agentic.reasoning.trace_span") as trace_span,
        patch("backend.services.rag.agentic.reasoning.set_span_attribute"),
        patch("backend.services.rag.agentic.reasoning.set_span_status"),
        patch("backend.services.rag.agentic.reasoning.add_span_event"),
        patch("backend.services.rag.agentic.reasoning.emit_low_confidence_event", new_callable=AsyncMock),
        patch("backend.services.rag.agentic.reasoning.compute_evidence_score", return_value=0.0),
        patch("backend.services.rag.agentic.reasoning.is_critical_domain", return_value=False),
    ):
        trace_span.return_value.__enter__ = MagicMock()
        trace_span.return_value.__exit__ = MagicMock(return_value=False)
        yield


def _gateway():
    gateway = MagicMock()
    gateway.send_message = AsyncMock(
        return_value=("calling crm", "gemini-flash", MagicMock(candidates=[MagicMock()]), MagicMock()),
    )
    return gateway


async def _run_stream(result: str, max_steps: int = 2) -> tuple[AgentState, AsyncMock]:
    from backend.services.rag.agentic.reasoning import ReasoningEngine

    state = AgentState(query="synthetic CRM request", max_steps=max_steps, intent_type="simple")
    tool_call = ToolCall(tool_name="crm_query", arguments={"q": "synthetic"})
    execute_tool_mock = AsyncMock(return_value=(result, 0.01))
    engine = ReasoningEngine(tool_map={}, response_pipeline=None)

    with (
        patch(
            "backend.services.rag.agentic.reasoning.parse_tool_calls_from_response",
            return_value=([tool_call], "native"),
        ),
        patch("backend.services.rag.agentic.reasoning.execute_tool", execute_tool_mock),
        patch("backend.services.rag.agentic.reasoning.detect_query_language", return_value="ENGLISH"),
    ):
        async for _ in engine.execute_react_loop_stream(
            state=state,
            llm_gateway=_gateway(),
            chat=MagicMock(),
            initial_prompt="synthetic CRM request",
            system_prompt="sys",
            query="synthetic CRM request",
            user_id="synthetic-user",
            model_tier=1,
            tool_execution_counter={"count": 0},
        ):
            pass
    return state, execute_tool_mock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ANONYMOUS_DENIAL_OBSERVATION,
        "Error: connection refused while querying the CRM",
        '{"error": "CRM query failed: timeout"}',
    ],
)
async def test_crm_denials_and_failures_do_not_set_stream_trust(result):
    state, execute_tool_mock = await _run_stream(result)

    assert state.trusted_tools_used is False
    assert execute_tool_mock.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        '{"clients": [{"id": 1}], "count": 1}',
        "result: ok!",
    ],
)
async def test_real_crm_results_still_set_stream_trust_and_exit_early(result):
    state, execute_tool_mock = await _run_stream(result)

    assert state.trusted_tools_used is True
    assert execute_tool_mock.await_count == 1
