from datetime import datetime, timezone

import asyncpg
import pytest

from backend.services.memory.memory_service_postgres import MemoryServicePostgres


class FakeConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.fail_reads = False
        self.execute_calls: list[tuple[object, ...]] = []

    async def fetch(self, *_args: object) -> list[dict[str, object]]:
        if self.fail_reads:
            raise asyncpg.InterfaceError("synthetic read failure")
        return []

    async def fetchrow(self, *_args: object) -> dict[str, object] | None:
        if self.fail_reads:
            raise asyncpg.InterfaceError("synthetic read failure")
        return self.row

    async def execute(self, *args: object) -> None:
        self.execute_calls.append(args)


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


def _service(connection: FakeConnection) -> MemoryServicePostgres:
    service = MemoryServicePostgres(database_url="postgresql://synthetic")
    service.pool = FakePool(connection)  # type: ignore[assignment]
    return service


@pytest.mark.asyncio
async def test_failed_refresh_refuses_to_overwrite_cached_stats() -> None:
    user_id = "user-900001"
    connection = FakeConnection(
        {
            "conversations_count": 41,
            "searches_count": 3,
            "summary": "kept",
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    )
    service = _service(connection)

    await service.get_memory(user_id, force_refresh=True)
    connection.fail_reads = True
    await service.get_memory(user_id, force_refresh=True)
    assert await service.increment_counter(user_id, "conversations") is False
    assert connection.execute_calls == []


@pytest.mark.asyncio
async def test_failed_uncached_read_refuses_counter_and_summary_saves() -> None:
    user_id = "user-900002"
    connection = FakeConnection(None)
    connection.fail_reads = True
    service = _service(connection)

    assert await service.increment_counter(user_id, "conversations") is False
    assert await service.update_summary(user_id, "made-up") is False
    assert connection.execute_calls == []


@pytest.mark.asyncio
async def test_failed_uncached_read_does_not_cache_empty_memory() -> None:
    user_id = "user-900003"
    connection = FakeConnection(None)
    connection.fail_reads = True
    service = _service(connection)

    await service.get_memory(user_id, force_refresh=True)

    assert user_id not in service.memory_cache


@pytest.mark.asyncio
async def test_missing_row_creates_cached_memory_and_saves_counter() -> None:
    user_id = "user-900004"
    connection = FakeConnection(None)
    service = _service(connection)

    memory = await service.get_memory(user_id, force_refresh=True)

    assert service.memory_cache[user_id] is memory
    assert await service.increment_counter(user_id, "conversations") is True
    assert connection.execute_calls[-1][1:4] == (user_id, 1, 0)


@pytest.mark.asyncio
async def test_memory_only_mode_still_caches_new_memory() -> None:
    service = MemoryServicePostgres(database_url="postgresql://synthetic")
    service.use_postgres = False
    user_id = "user-900005"

    memory = await service.get_memory(user_id)

    assert service.memory_cache[user_id] is memory


@pytest.mark.asyncio
async def test_successful_read_after_failure_allows_save() -> None:
    user_id = "user-900006"
    connection = FakeConnection(None)
    connection.fail_reads = True
    service = _service(connection)

    await service.get_memory(user_id, force_refresh=True)
    connection.fail_reads = False
    memory = await service.get_memory(user_id, force_refresh=True)

    assert await service.save_memory(memory) is True
    assert connection.execute_calls[-1][1] == user_id


@pytest.mark.asyncio
async def test_counter_after_a_transient_failure_continues_from_the_stored_values() -> None:
    user_id = "user-900007"
    connection = FakeConnection(
        {
            "conversations_count": 41,
            "searches_count": 3,
            "summary": "kept",
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
    )
    service = _service(connection)

    await service.get_memory(user_id, force_refresh=True)
    connection.fail_reads = True
    await service.get_memory(user_id, force_refresh=True)
    connection.fail_reads = False

    assert await service.increment_counter(user_id, "conversations") is True
    assert connection.execute_calls[-1][1:5] == (user_id, 42, 3, "kept")
