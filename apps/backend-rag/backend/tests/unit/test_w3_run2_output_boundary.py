"""Synthetic RUN-2 output regressions through the reachable W3 functions."""
from __future__ import annotations

import inspect
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

USER = "RUN2_CANARY_USER@example.invalid"
CONTENT = "RUN2_CANARY_CONTENT"
FAULT = "RUN2_CANARY_EXCEPTION"
TITLE = "RUN2_CANARY_TITLE"


@pytest.fixture(autouse=True)
def safe_diagnostics(caplog, monkeypatch):
    monkeypatch.setenv("LOG_PII_HMAC_SALT", "synthetic-run2-log-salt")
    caplog.set_level(logging.DEBUG)
    yield
    records = [record for phase in ("setup", "call", "teardown") for record in caplog.get_records(phase)]
    for record in records:
        # Check both rendered output and structured diagnostics, not message templates only.
        output = record.getMessage() + repr(record.__dict__)
        assert not any(value in output for value in (USER, CONTENT, FAULT, TITLE))
        assert record.exc_info is None or record.exc_info is False


def pool_with(conn):
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool


@pytest.mark.parametrize(
    ("error_type", "status", "retry"),
    [
        (asyncpg.UniqueViolationError, 400, None),
        (asyncpg.ForeignKeyViolationError, 400, None),
        (asyncpg.CheckViolationError, 400, None),
        (asyncpg.PostgresError, 503, None),
        (asyncpg.InterfaceError, 503, "3"),
        (RuntimeError, 500, None),
    ],
)
def test_database_faults_preserve_http_contract(error_type, status, retry, caplog):
    from backend.app.utils.error_handlers import handle_database_error

    response = handle_database_error(error_type(FAULT))
    assert response.status_code == status
    assert (response.headers or {}).get("Retry-After") == retry
    assert FAULT not in response.detail
    assert error_type.__name__ in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncpg.PostgresError, RuntimeError])
async def test_identity_faults_preserve_unknown_role(error_type):
    from backend.services.whatsapp_identity import resolve_sender_identity

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=error_type(FAULT))
    result = await resolve_sender_identity("628123456000", pool_with(conn))
    assert result == {"role": "unknown"}
    conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "get", "memory"])
async def test_response_error_is_fixed_while_identity_and_shape_survive(operation):
    from backend.app.routers import conversations as routes

    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=ValueError(FAULT))
    pool = pool_with(conn)
    user = {"email": USER}
    if operation == "list":
        response = await routes.list_conversations(limit=20, offset=0, current_user=user, db_pool=pool)
    elif operation == "get":
        response = await routes.get_conversation(17, current_user=user, db_pool=pool)
    else:
        with patch.object(routes, "get_memory_orchestrator", AsyncMock(side_effect=ValueError(FAULT))):
            response = await routes.get_user_memory_context(current_user=user, db_pool=pool)
    assert response.success is False
    assert response.error == "Request could not be completed."
    if operation != "memory":
        conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "gemini"])
@pytest.mark.parametrize("fails", [False, True])
async def test_title_provider_result_is_preserved_without_output(provider, fails):
    from backend.services.crm import enrichment

    generate = AsyncMock(side_effect=RuntimeError(FAULT) if fails else None)
    generate.return_value = TITLE if provider == "ollama" else {"text": TITLE}
    if provider == "ollama":
        with patch("backend.llm.ollama_client.ollama_chat", generate):
            result = await enrichment._generate_title_via_ollama("17", CONTENT, 80)
    else:
        client = MagicMock(is_available=True)
        client.generate_content = generate
        with patch("backend.llm.genai_client.get_genai_client", return_value=client):
            result = await enrichment._generate_title_via_gemini("17", CONTENT, 80)
    assert result == (None if fails else TITLE)
    generate.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboarding_payload_is_returned_without_log_transcription():
    from backend.services.whatsapp_onboarding_detector import OnboardingIntentDetector

    result = await OnboardingIntentDetector()._trigger_onboarding_chain(
        name=CONTENT, email=USER, nationality=CONTENT,
        business_description=CONTENT, phone=CONTENT,
    )
    assert result["status"] == "queued"
    assert result["payload"] == {
        "name": CONTENT, "email": USER, "nationality": CONTENT,
        "business_description": CONTENT, "phone": CONTENT,
    }


@pytest.mark.asyncio
async def test_memory_fact_and_duplicate_preserve_content_without_logging_it():
    from backend.services.memory.memory_service_postgres import MemoryServicePostgres

    service = MemoryServicePostgres.__new__(MemoryServicePostgres)
    service.pool = None
    service.use_postgres = False
    service.memory_cache = {}
    assert await service.add_fact(USER, CONTENT) is True
    assert await service.add_fact(USER, CONTENT) is False
    assert service.memory_cache[USER].profile_facts == [CONTENT]


@pytest.mark.asyncio
async def test_fact_extraction_retains_facts_without_content_output():
    from backend.services.memory.memory_fact_extractor import MemoryFactExtractor

    extractor = MemoryFactExtractor()
    fact = {"type": "personal", "content": CONTENT, "confidence": 0.9}
    with patch.object(extractor, "_extract_from_text", side_effect=[[fact], []]):
        result = extractor.extract_facts_from_conversation(CONTENT, CONTENT, USER)
        if inspect.isawaitable(result):
            result = await result
    assert result == [fact]


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["kg", "collective"])
async def test_context_failure_keeps_empty_fallback_and_one_attempt(component):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=ValueError(FAULT))
    pool = pool_with(conn)
    if component == "kg":
        from backend.agents.services.kg_repository import KnowledgeGraphRepository
        result = await KnowledgeGraphRepository(pool).get_entity_context_for_query(CONTENT)
    else:
        from backend.services.memory.collective_memory_service import CollectiveMemoryService
        result = await CollectiveMemoryService(pool).get_collective_context()
    assert result == []
    conn.fetch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("critical", [False, True])
async def test_initialization_alerts_only_receive_safe_failure_diagnostics(critical):
    from backend.services.memory import orchestrator as module

    memory = MagicMock()
    memory.get_memory = AsyncMock(
        side_effect=ValueError(FAULT) if critical else None, return_value=object()
    )
    alert = MagicMock(send_alert=AsyncMock())
    with (
        patch.object(module, "MemoryServicePostgres", return_value=memory),
        patch.object(module, "MemoryFactExtractor", side_effect=ValueError(FAULT) if not critical else None),
        patch.object(module, "CollectiveMemoryService"),
        patch.object(module, "EpisodicMemoryService"),
        patch.object(module, "KnowledgeGraphRepository"),
        patch("backend.services.monitoring.alert_service.get_alert_service", return_value=alert),
    ):
        orch = module.MemoryOrchestrator(db_pool=MagicMock())
        if critical:
            with pytest.raises(RuntimeError) as caught:
                await orch.initialize()
            assert FAULT not in str(caught.value)
        else:
            await orch.initialize()
            assert orch._is_initialized is True
    alert.send_alert.assert_awaited_once()
    args = repr(alert.send_alert.await_args)
    assert FAULT not in args
    assert "ValueError" in args


def test_background_task_failure_is_observed_once_without_traceback():
    from backend.services.rag.agentic.memory_handler import MemoryHandler

    handler = MemoryHandler()
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = ValueError(FAULT)
    handler._inflight_tasks.add(task)
    handler._on_save_task_done(task)
    assert task not in handler._inflight_tasks
    task.exception.assert_called_once_with()


@pytest.mark.asyncio
async def test_faq_skip_does_not_log_query_and_does_not_touch_cache():
    from backend.services.rag.agentic.orchestrator_core import OrchestratorCore

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.faq_cache = MagicMock()
    result = await core.check_faq_cache(CONTENT, {}, time.time())
    assert result is None
    core.faq_cache.get.assert_not_called()


@pytest.mark.asyncio
async def test_episodic_insert_preserves_title_and_query_arguments_without_output():
    from datetime import datetime, timezone

    from backend.services.memory.episodic_memory_service import EpisodicMemoryService

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 17, "created_at": datetime.now(timezone.utc)})
    result = await EpisodicMemoryService(pool_with(conn)).add_event(
        USER, TITLE, description=CONTENT
    )
    assert result["status"] == "created"
    assert result["title"] == TITLE
    assert conn.fetchrow.await_args.args[1:4] == (USER, TITLE, CONTENT)


@pytest.mark.asyncio
async def test_rag_eager_initialization_keeps_degraded_completion_without_exception_text():
    from backend.services.rag.agentic.orchestrator import AgenticRAGOrchestrator

    orch = AgenticRAGOrchestrator.__new__(AgenticRAGOrchestrator)
    orch._initialized = False
    orch.memory_handler = MagicMock()
    orch.memory_handler.get_memory_orchestrator = AsyncMock(side_effect=ValueError(FAULT))
    orch.kg_langgraph_orchestrator = MagicMock()
    orch.kg_langgraph_orchestrator.initialize = AsyncMock(side_effect=ValueError(FAULT))
    await orch.initialize()
    assert orch._initialized is True
    orch.memory_handler.get_memory_orchestrator.assert_awaited_once()
    orch.kg_langgraph_orchestrator.initialize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_subject", [None, "RUN2_CANARY_SUBJECT"], ids=["user-fallback", "explicit-subject"])
async def test_process_query_dispatch_preserves_result_and_safe_runtime_log(memory_subject, caplog):
    from contextlib import nullcontext

    from backend.services.rag.agentic import orchestrator as module

    orch = module.AgenticRAGOrchestrator.__new__(module.AgenticRAGOrchestrator)
    orch._initialized = True
    result = MagicMock(answer=CONTENT)
    orch.core = MagicMock(process_query_core=AsyncMock(return_value=result))
    orch.memory_handler = MagicMock()
    with patch.object(module, "trace_span", return_value=nullcontext()):
        actual = await orch.process_query(
            query=CONTENT, user_id=USER, session_id="synthetic-session",
            memory_subject=memory_subject, channel="whatsapp",
        )
    assert actual is result
    orch.core.process_query_core.assert_awaited_once()
    orch.memory_handler.create_save_task.assert_called_once_with(
        user_id=USER, query=CONTENT, answer=CONTENT, session_id="synthetic-session",
        metrics_collector=module.metrics_collector, memory_subject=memory_subject,
    )
    warnings = [
        record for record in caplog.records
        if record.name == module.__name__ and record.levelno >= logging.WARNING
    ]
    assert warnings == []
    assert "Dispatched memory save task" in caplog.text
    assert "NameError" not in caplog.text
    assert "RUN2_CANARY_SUBJECT" not in caplog.text


def test_settings_remain_real_under_rag_and_router_collection():
    from backend.app.core import config
    from backend.app.routers import whatsapp_chat
    from backend.services.rag.agentic import orchestrator

    values = config.settings.model_dump()
    assert isinstance(values, dict)
    assert type(values["meta_webhook_require_signature"]) is bool
    assert whatsapp_chat.settings is config.settings
    assert orchestrator.settings is config.settings


@pytest.fixture
def real_span_recorder(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from backend.app.utils import tracing

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("synthetic-run2")
    monkeypatch.setattr(tracing, "OTEL_AVAILABLE", True)
    monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)
    yield exporter
    provider.shutdown()


def _span_payload(exporter):
    return repr([
        (dict(span.attributes or {}), span.status.description,
         [(event.name, dict(event.attributes or {})) for event in span.events])
        for span in exporter.get_finished_spans()
    ])


def _assert_safe_spans(exporter):
    payload = _span_payload(exporter)
    assert not any(marker in payload for marker in (USER, CONTENT, FAULT, TITLE))


def test_legacy_trace_counterfactual_is_detected_by_real_exporter(real_span_recorder):
    from backend.app.utils.tracing import trace_span

    error = ValueError(FAULT)
    with pytest.raises(ValueError) as caught:
        with trace_span("synthetic.legacy", {"user_id": USER}):
            raise error
    assert caught.value is error
    spans = real_span_recorder.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["user_id"] == USER
    assert FAULT in _span_payload(real_span_recorder)
    assert any(event.name == "exception" for event in spans[0].events)
    with pytest.raises(AssertionError):
        _assert_safe_spans(real_span_recorder)


def test_opted_in_trace_preserves_healthy_technical_attributes(real_span_recorder):
    from backend.app.utils.tracing import trace_span

    with trace_span("synthetic.safe", {"count": 2, "mode": "synthetic"}, safe_diagnostics=True):
        pass
    span = real_span_recorder.get_finished_spans()[0]
    assert dict(span.attributes) == {"count": "2", "mode": "synthetic"}
    assert not span.events
    _assert_safe_spans(real_span_recorder)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "entities", "kg", "workflow"])
async def test_nested_parallel_outputs_preserve_returns_and_safe_spans(fault, real_span_recorder):
    from types import SimpleNamespace

    from backend.services.rag.agentic import orchestrator_core as module

    core = module.OrchestratorCore.__new__(module.OrchestratorCore)
    entities = {"person": [CONTENT]}
    workflow = {"type": CONTENT, "source": CONTENT, "steps": []}
    core.entity_extractor = SimpleNamespace(extract_entities=AsyncMock(
        return_value=entities, side_effect=ValueError(FAULT) if fault == "entities" else None
    ))
    kg = SimpleNamespace(graph_summary=CONTENT, entities_found=[CONTENT], relationships=[])
    core.kg_retrieval = SimpleNamespace(get_context_for_query=AsyncMock(
        return_value=kg, side_effect=ValueError(FAULT) if fault == "kg" else None
    ))
    core.kg_langgraph_orchestrator = SimpleNamespace(
        app=object(), initialize=AsyncMock(),
        query=AsyncMock(return_value={"workflow": workflow},
                        side_effect=ValueError(FAULT) if fault == "workflow" else None),
    )
    core._format_workflow_for_prompt = MagicMock(return_value=CONTENT)
    core._track_workflow = AsyncMock()

    def close_test_coroutine(coroutine, **_kwargs):
        coroutine.close()

    with patch.object(module, "spawn", side_effect=close_test_coroutine) as spawned:
        result_entities, prompt, result_workflow = await core.extract_entities_and_kg_context(
            CONTENT, {"user_id": USER}
        )
    assert result_entities == ({} if fault == "entities" else entities)
    assert result_workflow is (None if fault == "workflow" else workflow)
    assert CONTENT in prompt  # business content remains available to the caller
    core.entity_extractor.extract_entities.assert_awaited_once_with(CONTENT)
    core.kg_retrieval.get_context_for_query.assert_awaited_once_with(CONTENT, max_depth=1)
    core.kg_langgraph_orchestrator.query.assert_awaited_once_with(
        query=CONTENT, user_context={"user_id": USER}
    )
    assert spawned.call_count == (0 if fault == "workflow" else 1)
    assert len(real_span_recorder.get_finished_spans()) == 2
    _assert_safe_spans(real_span_recorder)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [False, True])
async def test_semantic_cache_status_has_no_error_payload(fault, real_span_recorder):
    from types import SimpleNamespace

    from backend.services.rag.agentic.orchestrator_core import OrchestratorCore

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.retriever = None
    core.semantic_cache = SimpleNamespace(get_cached_result=AsyncMock(
        return_value={"cache_hit": "exact", "result": {"answer": CONTENT, "sources": []}},
        side_effect=ValueError(FAULT) if fault else None,
    ))
    result = await core.check_semantic_cache(CONTENT, {}, time.time())
    assert (result is None) if fault else (result.answer == CONTENT and result.cache_hit)
    core.semantic_cache.get_cached_result.assert_awaited_once_with(CONTENT, None)
    assert len(real_span_recorder.get_finished_spans()) == 1
    _assert_safe_spans(real_span_recorder)


@pytest.mark.asyncio
async def test_process_query_span_redacts_both_identity_attributes(real_span_recorder):
    from backend.services.rag.agentic import orchestrator as module

    orch = module.AgenticRAGOrchestrator.__new__(module.AgenticRAGOrchestrator)
    orch._initialized = True
    result = MagicMock(answer=CONTENT)
    orch.core = MagicMock(process_query_core=AsyncMock(return_value=result))
    orch.memory_handler = MagicMock()
    actual = await orch.process_query(CONTENT, user_id=USER, session_id=CONTENT)
    assert actual is result
    orch.core.process_query_core.assert_awaited_once()
    orch.memory_handler.create_save_task.assert_called_once_with(
        user_id=USER, query=CONTENT, answer=CONTENT, session_id=CONTENT,
        metrics_collector=module.metrics_collector, memory_subject=None,
    )
    span = real_span_recorder.get_finished_spans()[0]
    assert span.attributes["user_id"] != USER
    assert span.attributes["session_id"] != CONTENT
    _assert_safe_spans(real_span_recorder)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["known", "unexpected", "cancel"])
async def test_react_fault_identity_cause_and_no_exported_error(fault, real_span_recorder):
    import asyncio
    from types import SimpleNamespace

    from backend.services.rag.agentic.orchestrator_core import OrchestratorCore

    error = ValueError(FAULT) if fault == "known" else KeyError(FAULT) if fault == "unexpected" else asyncio.CancelledError(FAULT)
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.llm_gateway = MagicMock()
    core.reasoning_engine = SimpleNamespace(execute_react_loop=AsyncMock(side_effect=error))
    state = SimpleNamespace(steps=[])
    expected = RuntimeError if fault == "unexpected" else type(error)
    with pytest.raises(expected) as caught:
        await core.execute_react_loop(
            state=state, chat=object(), system_prompt=CONTENT, query=CONTENT,
            user_id=USER, model_tier="flash", tool_execution_counter={"count": 0},
        )
    if fault == "unexpected":
        assert str(caught.value) == "ReAct loop failed."
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    core.reasoning_engine.execute_react_loop.assert_awaited_once()
    assert len(real_span_recorder.get_finished_spans()) == 1
    assert not real_span_recorder.get_finished_spans()[0].events
    _assert_safe_spans(real_span_recorder)


@pytest.mark.asyncio
async def test_react_success_keeps_result_tuple_and_safe_spans(real_span_recorder):
    from types import SimpleNamespace

    from backend.services.rag.agentic.orchestrator_core import OrchestratorCore

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.llm_gateway = MagicMock()
    state = SimpleNamespace(steps=[object()])
    usage = {"total": 7}
    core.reasoning_engine = SimpleNamespace(
        execute_react_loop=AsyncMock(return_value=(state, "synthetic-model", [], usage))
    )
    result = await core.execute_react_loop(
        state=state, chat=object(), system_prompt=CONTENT, query=CONTENT,
        user_id=USER, model_tier="flash", tool_execution_counter={"count": 0},
    )
    assert result[0] is state and result[1] == "synthetic-model" and result[2] is usage
    assert result[3] >= 0
    core.reasoning_engine.execute_react_loop.assert_awaited_once()
    _assert_safe_spans(real_span_recorder)


@pytest.mark.asyncio
async def test_timeout_metric_emits_only_redacted_label_and_one_increment():
    from backend.app import metrics
    from backend.security.pii_log_identifier import redact_identifier_for_log
    from backend.services.rag.agentic.memory_handler import MemoryHandler

    handler = MemoryHandler()
    lock = MagicMock()
    lock.acquire = AsyncMock(side_effect=TimeoutError())
    handler._memory_locks[USER + "::__nosession__"] = lock
    handler.get_memory_orchestrator = AsyncMock()
    label = redact_identifier_for_log(USER)
    counter = metrics.memory_lock_timeout_total
    child = counter.labels(user_id=label)
    before = child._value.get()
    try:
        await handler.save_conversation_memory(
            USER, CONTENT, CONTENT, metrics_collector=metrics.metrics_collector
        )
        assert child._value.get() == before + 1
        samples = [sample for family in counter.collect() for sample in family.samples
                   if sample.name.endswith("_total") and sample.labels.get("user_id") == label]
        assert len(samples) == 1 and samples[0].labels["user_id"] != USER
        handler.get_memory_orchestrator.assert_not_awaited()
        lock.release.assert_not_called()
    finally:
        counter.remove(label)
