"""Stream-path logs and span events carry no identifier, event text or exception text."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from backend.app.routers import agentic_rag as agentic
from backend.app.routers.agentic_rag import AgenticQueryRequest, WorkspaceQueryRequest
from backend.services.rag.agentic import orchestrator_streaming_core as core_module
from backend.services.rag.agentic.orchestrator_streaming import OrchestratorStreamingManager
from backend.services.rag.agentic.orchestrator_streaming_core import OrchestratorStreamingCore
from backend.services.tools.definitions import AgentState

CANARY_USER = "canary-user-000000"
CANARY_EMAIL = "canary.person@example.test"
CANARY_TEXT = "canary-body-text-000000"


def _dump(caplog) -> str:
    return caplog.text + repr([r.__dict__ for r in caplog.records])


# --- orchestrator_streaming_core ----------------------------------------------


class _Manager:
    def create_initial_status_event(self, correlation_id):
        return {"type": "status", "data": {}}

    def create_done_event(self, **kwargs):
        return {"type": "done", "data": kwargs}

    def create_error_event(self, **kwargs):
        return {"type": "error", "data": kwargs}

    async def process_event_stream(self, *, event_generator, **_kwargs):
        async for event in event_generator:
            yield event


class _Engine:
    tool_map: dict = {}

    async def execute_react_loop_stream(self, **_kwargs):
        yield {"type": "token", "data": "t"}


class _Core:
    def __init__(self):
        self.reasoning_engine = _Engine()
        self.llm_gateway = SimpleNamespace(create_chat_with_history=lambda **_k: object())
        self.metrics_manager = SimpleNamespace(
            extract_collections_from_state=lambda _s: set(),
            extract_sources_from_state=lambda _s: [],
        )
        self.db_pool = None

    async def prepare_query_context(self, **_k):
        return ({"profile": None}, [], {}, "", None)

    async def check_gates_and_cache(self, **_k):
        return None

    async def prepare_react_execution(self, **_k):
        state = AgentState(query="q", intent_type="business_complex")
        state.evidence_score = 0.8
        state.trusted_tools_used = True
        return ("tier", False, state, "system prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [CANARY_USER, CANARY_EMAIL])
async def test_react_stream_start_log_and_span_event_redact_user_id(caplog, user_id) -> None:
    streaming_core = OrchestratorStreamingCore(core=_Core(), streaming_manager=_Manager())
    with (
        caplog.at_level(logging.INFO),
        patch.object(core_module, "add_span_event") as span_event,
        patch.object(streaming_core, "_mark_react_stream_unfinalized"),
    ):
        async for _ in streaming_core.stream_query_core(
            query="q",
            user_id=user_id,
            conversation_history=None,
            session_id=None,
            images=None,
            tool_execution_counter={"count": 0},
            correlation_id="corr",
        ):
            pass
    assert "Processing query with ReAct loop for user id:" in caplog.text
    start = [c for c in span_event.call_args_list if c.args[0] == "react.stream.start"]
    assert len(start) == 1
    assert start[0].args[1]["user_id"].startswith("id:")
    assert start[0].args[1]["model_tier"] == "tier"
    assert user_id not in _dump(caplog) + repr(span_event.call_args_list)


# --- orchestrator_streaming ---------------------------------------------------


def test_event_validation_failure_logs_shape_not_content(caplog) -> None:
    manager = OrchestratorStreamingManager()
    with caplog.at_level(logging.INFO):
        out = manager.validate_event({"type": ["bad"], "data": CANARY_TEXT}, "corr-1")
    assert out is None
    record = next(r for r in caplog.records if "Event validation failed" in r.getMessage())
    assert record.getMessage().endswith("ValidationError")
    assert record.correlation_id == "corr-1"
    assert record.event_keys == ["data", "type"]
    assert record.raw_event_length > 0
    assert record.validation_errors == [{"loc": ["type"], "type": "string_type"}]
    assert CANARY_TEXT not in _dump(caplog)


def test_event_validation_failure_reports_a_string_event_type(caplog) -> None:
    manager = OrchestratorStreamingManager()
    with caplog.at_level(logging.INFO):
        manager.validate_event({"type": "token"}, "corr-1")
    record = next(r for r in caplog.records if "Event validation failed" in r.getMessage())
    assert record.event_type == "token"
    assert record.validation_errors == [{"loc": ["data"], "type": "missing"}]


@pytest.mark.asyncio
async def test_unexpected_event_error_logs_type_frames_and_redacted_user(caplog) -> None:
    manager = OrchestratorStreamingManager(max_event_errors=1)

    async def events():
        yield {"type": "token", "data": "x"}

    with (
        caplog.at_level(logging.INFO),
        patch.object(
            manager, "validate_event", side_effect=RuntimeError(f"bad {CANARY_TEXT} {CANARY_EMAIL}")
        ),
    ):
        out = [e async for e in manager.process_event_stream(events(), "corr-2", CANARY_EMAIL)]
    record = next(
        r for r in caplog.records if "Unexpected error processing event" in r.getMessage()
    )
    assert "RuntimeError" in record.getMessage()
    assert "File " in record.getMessage()
    assert "orchestrator_streaming.py" in record.getMessage()
    assert record.user_id.startswith("id:")
    assert record.correlation_id == "corr-2"
    assert not hasattr(record, "error_message")
    assert CANARY_TEXT not in _dump(caplog)
    assert CANARY_EMAIL not in _dump(caplog)
    # What the client receives is unchanged.
    assert out[-1]["type"] == "error"
    assert CANARY_TEXT in repr(out[-1])


# --- agentic_rag stream handlers ----------------------------------------------


def _http_request():
    req = MagicMock(spec=Request)
    req.state = MagicMock()
    req.state.correlation_id = "corr-3"
    req.is_disconnected = AsyncMock(return_value=False)
    req.headers = {}
    return req


async def _body(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(chunks)


async def _run_stream(events_factory):
    orchestrator = AsyncMock()
    orchestrator.stream_query = MagicMock(return_value=events_factory())
    response = await agentic.stream_agentic_rag(
        request_body=AgenticQueryRequest(query="q"),
        http_request=_http_request(),
        current_user={"email": CANARY_EMAIL, "user_id": CANARY_USER},
        orchestrator=orchestrator,
        db_pool=None,
    )
    return await _body(response)


@pytest.mark.asyncio
async def test_non_serialisable_event_no_longer_raises_attribute_error(caplog) -> None:
    async def events():
        yield {"type": "token", "data": object()}
        yield {"type": "done", "data": "[DONE]"}

    with caplog.at_level(logging.INFO):
        body = await _run_stream(events)
    assert "serialization_error" in body
    assert '"status": "completed"' in body
    assert "JSON serialization failed: TypeError (correlation_id=corr-3)" in caplog.text
    assert "Fatal error in stream" not in caplog.text


@pytest.mark.asyncio
async def test_a_foreign_type_error_is_not_reported_as_serialisation(caplog) -> None:
    async def events():
        yield {"type": "token", "data": "x"}

    with (
        caplog.at_level(logging.INFO),
        patch.object(
            agentic, "clean_image_generation_response", side_effect=TypeError(CANARY_TEXT)
        ),
    ):
        body = await _run_stream(events)
    assert "serialization_error" not in body
    assert "processing_error" in body
    assert "Error processing stream event: TypeError (correlation_id=corr-3)" in caplog.text


@pytest.mark.asyncio
async def test_stream_handlers_log_type_correlation_and_frames_not_the_message(caplog) -> None:
    async def events():
        yield {"type": "token", "data": "x"}
        raise RuntimeError(f"fatal {CANARY_TEXT} {CANARY_EMAIL}")

    with (
        caplog.at_level(logging.INFO),
        patch.object(
            agentic, "clean_image_generation_response", side_effect=ValueError(CANARY_TEXT)
        ),
    ):
        body = await _run_stream(events)
    per_event = next(r for r in caplog.records if "Error processing stream event" in r.getMessage())
    fatal = next(r for r in caplog.records if "Fatal error in stream" in r.getMessage())
    for record, name in ((per_event, "ValueError"), (fatal, "RuntimeError")):
        assert f": {name} (correlation_id=corr-3)" in record.getMessage()
        assert "File " in record.getMessage()
        assert "agentic_rag.py" in record.getMessage()
        assert record.exc_info is None
    assert CANARY_TEXT not in _dump(caplog)
    assert CANARY_EMAIL not in _dump(caplog)
    # What the client receives is unchanged.
    assert "processing_error" in body
    assert "fatal_error" in body
    assert CANARY_TEXT in body


@pytest.mark.asyncio
async def test_workspace_stream_fatal_logs_type_correlation_and_frames(caplog) -> None:
    async def events():
        yield {"type": "token", "data": "x"}
        raise RuntimeError(f"fatal {CANARY_TEXT} {CANARY_EMAIL}")
        yield  # pragma: no cover

    orchestrator = AsyncMock()
    orchestrator.stream_query = MagicMock(return_value=events())
    role = SimpleNamespace(role_id="synthetic_role", client_scope="synthetic_scope")
    with (
        caplog.at_level(logging.INFO),
        patch.object(agentic, "get_agent_role", return_value=role),
        patch.object(agentic, "build_agent_context", return_value={"ctx": 1}),
        patch.object(agentic, "_inject_agent_context_prefix", return_value="q"),
    ):
        response = await agentic.stream_workspace_agent(
            request_body=WorkspaceQueryRequest(query="q"),
            http_request=_http_request(),
            current_user={"email": CANARY_EMAIL},
            orchestrator=orchestrator,
            db_pool=None,
        )
        body = await _body(response)
    fatal = next(r for r in caplog.records if "workspace-stream fatal error" in r.getMessage())
    assert ": RuntimeError (correlation_id=corr-3)" in fatal.getMessage()
    assert "File " in fatal.getMessage()
    assert "agentic_rag.py" in fatal.getMessage()
    assert fatal.exc_info is None
    assert CANARY_TEXT not in _dump(caplog)
    assert CANARY_EMAIL not in _dump(caplog)
    assert "fatal_error" in body
