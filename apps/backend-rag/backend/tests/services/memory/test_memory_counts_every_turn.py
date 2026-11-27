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
    def __init__(self, pool: object | None = object()) -> None:
        self.pool = pool
        self.add_fact = AsyncMock(return_value=True)
        self.increment_counter = AsyncMock()


class FakeEpisodicService:
    def __init__(self) -> None:
        self.extract_and_save_event = AsyncMock(return_value=None)


def _make_orchestrator(
    facts: list[dict[str, object]],
    memory_service: FakeMemoryService | None = None,
) -> tuple[MemoryOrchestrator, FakeMemoryService | None, FakeEpisodicService]:
    orchestrator = MemoryOrchestrator(db_pool=None, database_url=None)
    orchestrator._is_initialized = True
    orchestrator._status = MemoryServiceStatus.HEALTHY
    orchestrator._fact_extractor = FakeExtractor(facts)
    orchestrator._memory_service = memory_service or FakeMemoryService()
    episodic_service = FakeEpisodicService()
    orchestrator._episodic_memory = episodic_service
    return orchestrator, orchestrator._memory_service, episodic_service


@pytest.mark.asyncio
async def test_no_facts_still_updates_counter_and_extracts_episodic_event() -> None:
    orchestrator, memory_service, episodic_service = _make_orchestrator([])

    result = await orchestrator.process_conversation(
        user_email="no-facts@example.test",
        user_message="I have a meeting on 2040-01-02.",
        ai_response="Noted.",
    )

    assert result.facts_extracted == 0
    assert result.facts_saved == 0
    memory_service.increment_counter.assert_awaited_once_with(
        user_id="no-facts@example.test", counter_name="conversations"
    )
    episodic_service.extract_and_save_event.assert_awaited_once_with(
        user_id="no-facts@example.test",
        message="I have a meeting on 2040-01-02.",
        ai_response="Noted.",
    )


@pytest.mark.asyncio
async def test_fact_turn_saves_fact_and_runs_counter_and_timeline_once() -> None:
    orchestrator, memory_service, episodic_service = _make_orchestrator(
        [{"content": "Synthetic user prefers tea", "type": "preference"}]
    )

    result = await orchestrator.process_conversation(
        user_email="fact@example.test",
        user_message="I prefer tea.",
        ai_response="Noted.",
    )

    assert result.facts_extracted == 1
    assert result.facts_saved == 1
    memory_service.add_fact.assert_awaited_once()
    memory_service.increment_counter.assert_awaited_once()
    episodic_service.extract_and_save_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_memory_pool_skips_counter_but_runs_timeline() -> None:
    memory_service = FakeMemoryService(pool=None)
    orchestrator, _, episodic_service = _make_orchestrator([], memory_service)

    await orchestrator.process_conversation(
        user_email="no-pool@example.test",
        user_message="Meeting on 2040-01-02.",
        ai_response="Noted.",
    )

    memory_service.increment_counter.assert_not_awaited()
    episodic_service.extract_and_save_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_counter_failure_does_not_prevent_timeline_extraction() -> None:
    memory_service = FakeMemoryService()
    memory_service.increment_counter.side_effect = RuntimeError("synthetic counter failure")
    orchestrator, _, episodic_service = _make_orchestrator([], memory_service)

    await orchestrator.process_conversation(
        user_email="counter-error@example.test",
        user_message="Meeting on 2040-01-02.",
        ai_response="Noted.",
    )

    memory_service.increment_counter.assert_awaited_once()
    episodic_service.extract_and_save_event.assert_awaited_once()
