"""Regression coverage for skipped response-pipeline verification."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.llm_clients.pricing import TokenUsage
from backend.services.rag.agentic.reasoning import ReasoningEngine
from backend.services.tools.definitions import AgentState


async def _run_loop(pipeline_result):
    pipeline = MagicMock()
    pipeline.process = AsyncMock(return_value=pipeline_result)
    engine = ReasoningEngine(tool_map={}, response_pipeline=pipeline)
    state = AgentState(query="synthetic question", max_steps=1)
    gateway = MagicMock()
    gateway._gemini_tools = []
    gateway.send_message = AsyncMock(
        return_value=(
            "Final Answer: Synthetic answer.",
            "synthetic-model",
            MagicMock(candidates=[]),
            TokenUsage(),
        )
    )

    with (
        patch("backend.services.rag.agentic.reasoning.detect_query_language", return_value="ENGLISH"),
        patch("backend.services.rag.agentic.reasoning.calculate_evidence_score", return_value=0.5),
        patch("backend.services.rag.agentic.reasoning.is_critical_domain", return_value=False),
        patch("backend.services.rag.agentic.reasoning.parse_tool_call", return_value=None),
        patch("backend.services.rag.agentic.reasoning.is_valid_tool_call", return_value=False),
        patch("backend.services.rag.agentic.reasoning.trace_span") as trace_span,
        patch("backend.services.rag.agentic.reasoning.set_span_attribute"),
        patch("backend.services.rag.agentic.reasoning.set_span_status"),
    ):
        trace_span.return_value.__enter__ = MagicMock()
        trace_span.return_value.__exit__ = MagicMock(return_value=False)
        return await engine.execute_react_loop(
            state=state,
            llm_gateway=gateway,
            chat=MagicMock(),
            initial_prompt="synthetic question",
            system_prompt="synthetic system",
            query="synthetic question",
            user_id="synthetic-user",
            model_tier=1,
            tool_execution_counter={"count": 0},
        )


@pytest.mark.asyncio
async def test_skipped_verification_does_not_persist_passing_score():
    """A skipped verifier must leave the response verification unchecked."""
    state, _, _, _ = await _run_loop(
        {
            "response": "Synthetic answer.",
            "verification_score": 1.0,
            "verification_status": "skipped",
            "verdict_available": True,
        }
    )

    assert getattr(state, "verification_score", 0.0) != 1.0


@pytest.mark.asyncio
async def test_completed_verification_persists_its_score():
    """A genuine verification verdict remains available to downstream code."""
    state, _, _, _ = await _run_loop(
        {
            "response": "Synthetic answer.",
            "verification_score": 0.84,
            "verification_status": "passed",
            "verdict_available": True,
        }
    )

    assert state.verification_score == 0.84
