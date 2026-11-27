from unittest.mock import AsyncMock

import pytest

from backend.services.memory.orchestrator import (
    MemoryOrchestrator,
    MemoryServiceStatus,
)


class FakeExtractor:
    def __init__(self, facts: list[dict[str, object]]) -> None:
        self.facts = facts

    def extract_facts_from_conversation(
        self, user_message: str, ai_response: str, user_id: str
    ) -> list[dict[str, object]]:
        return self.facts


class FakeMemoryService:
    def __init__(self) -> None:
        self.pool = object()
        self.add_fact = AsyncMock(return_value=True)
        self.increment_counter = AsyncMock()


class FakeEpisodicService:
    def __init__(self) -> None:
        self.extract_and_save_event = AsyncMock(return_value=None)


def _make_orchestrator(
    facts: list[dict[str, object]],
) -> tuple[MemoryOrchestrator, FakeMemoryService, FakeEpisodicService]:
    orchestrator = MemoryOrchestrator(db_pool=None, database_url=None)
    orchestrator._is_initialized = True
    orchestrator._status = MemoryServiceStatus.HEALTHY
    orchestrator._fact_extractor = FakeExtractor(facts)
    memory_service = FakeMemoryService()
    orchestrator._memory_service = memory_service
    episodic_service = FakeEpisodicService()
    orchestrator._episodic_memory = episodic_service
    return orchestrator, memory_service, episodic_service


@pytest.mark.asyncio
async def test_only_user_sourced_facts_are_saved() -> None:
    orchestrator, memory_service, _ = _make_orchestrator(
        [
            {"content": "Synthetic user fact", "source": "user"},
            {"content": "Synthetic assistant fact one", "source": "ai"},
            {"content": "Synthetic assistant fact two", "source": "ai"},
        ]
    )

    result = await orchestrator.process_conversation(
        user_email="source-filter@example.test",
        user_message="Synthetic user message.",
        ai_response="Synthetic assistant response.",
    )

    assert result.facts_extracted == 3
    assert result.facts_saved == 1
    memory_service.add_fact.assert_awaited_once_with(
        user_id="source-filter@example.test",
        fact="Synthetic user fact",
        fact_type="general",
    )


@pytest.mark.asyncio
async def test_ai_only_facts_still_count_and_run_episodic_extraction() -> None:
    orchestrator, memory_service, episodic_service = _make_orchestrator(
        [{"content": "Synthetic assistant fact", "source": "ai"}]
    )

    result = await orchestrator.process_conversation(
        user_email="ai-only@example.test",
        user_message="Synthetic user message.",
        ai_response="Synthetic assistant response.",
    )

    assert result.facts_extracted == 1
    assert result.facts_saved == 0
    memory_service.add_fact.assert_not_awaited()
    memory_service.increment_counter.assert_awaited_once()
    episodic_service.extract_and_save_event.assert_awaited_once_with(
        user_id="ai-only@example.test",
        message="Synthetic user message.",
        ai_response="Synthetic assistant response.",
    )


@pytest.mark.asyncio
async def test_fact_without_source_is_saved_as_a_user_fact() -> None:
    orchestrator, memory_service, _ = _make_orchestrator(
        [{"content": "Synthetic default-source fact"}]
    )

    result = await orchestrator.process_conversation(
        user_email="default-source@example.test",
        user_message="Synthetic user message.",
        ai_response="Synthetic assistant response.",
    )

    assert result.facts_extracted == 1
    assert result.facts_saved == 1
    memory_service.add_fact.assert_awaited_once_with(
        user_id="default-source@example.test",
        fact="Synthetic default-source fact",
        fact_type="general",
    )
