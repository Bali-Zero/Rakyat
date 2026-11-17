"""Tests for the CRM knowledge-graph maintenance jobs run by the drive worker."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from backend.workers import crm_kg_maintenance as kg
from backend.workers.crm_kg_maintenance import (
    KgMaintenanceConfig,
    load_kg_maintenance_config,
    run_due_jobs,
    run_kg_maintenance,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def make_config(**overrides: Any) -> KgMaintenanceConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "build_mediated_interval_seconds": 21_600,
        "garbage_collect_interval_seconds": 86_400,
        "timeout_seconds": 60,
    }
    values.update(overrides)
    return KgMaintenanceConfig(**values)


class FakeConn:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]], state: dict[str, Any]) -> None:
        self.log = log
        self.state = state

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.log.append((sql, args))
        if "pg_try_advisory_lock" in sql:
            return args[0] not in self.state.get("busy_locks", set())
        if "FROM system_settings" in sql:
            return self.state.get("settings", {}).get(args[0])
        return None

    async def execute(self, sql: str, *args: Any) -> None:
        self.log.append((sql, args))


class FakePool:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.state = state or {}
        self.released = 0
        self.closed = False

    async def acquire(self) -> FakeConn:
        return FakeConn(self.log, self.state)

    async def release(self, _conn: Any) -> None:
        self.released += 1

    async def close(self) -> None:
        self.closed = True

    def upserts(self) -> list[str]:
        return [args[0] for sql, args in self.log if "INSERT INTO system_settings" in sql]

    def unlocks(self) -> list[int]:
        return [args[0] for sql, args in self.log if "pg_advisory_unlock" in sql]


def patch_jobs(build: Any, gc: Any):
    return (
        patch(
            "backend.services.knowledge_graph.mediated_edges_builder.build_mediated_edges", build
        ),
        patch("backend.services.knowledge_graph.garbage_collector.garbage_collect", gc),
    )


async def run_with(pool: FakePool, build: Any, gc: Any, config: KgMaintenanceConfig | None = None):
    p1, p2 = patch_jobs(build, gc)
    with p1, p2:
        return await run_due_jobs(pool, config or make_config(), now=NOW)


@pytest.mark.asyncio
async def test_disabled_never_creates_pool() -> None:
    with patch("asyncpg.create_pool", new=AsyncMock()) as create_pool:
        result = await run_kg_maintenance("postgresql://x/db", make_config(enabled=False))
    assert result == {"status": "disabled"}
    create_pool.assert_not_called()


@pytest.mark.asyncio
async def test_lock_busy_skips_job() -> None:
    pool = FakePool({"busy_locks": {kg.BUILD_MEDIATED_LOCK_ID}})
    build = AsyncMock(return_value={"ok": True})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "locked"}
    build.assert_not_called()
    gc.assert_called_once()
    assert kg.BUILD_MEDIATED_LOCK_ID not in pool.unlocks()
    assert pool.unlocks() == [kg.GARBAGE_COLLECT_LOCK_ID]


@pytest.mark.asyncio
async def test_not_due_skips_job() -> None:
    recent = (NOW - timedelta(seconds=60)).isoformat()
    pool = FakePool(
        {
            "settings": {
                f"{kg.BUILD_MEDIATED_JOB}_last_run_at": recent,
                f"{kg.GARBAGE_COLLECT_JOB}_last_run_at": recent,
            }
        }
    )
    build = AsyncMock(return_value={"ok": True})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "not_due", "last_run_at": recent}
    build.assert_not_called()
    gc.assert_not_called()
    assert pool.upserts() == []
    assert len(pool.unlocks()) == 2


@pytest.mark.asyncio
async def test_due_stamps_before_run_and_records_result() -> None:
    pool = FakePool()
    order: list[str] = []

    async def build(_pool: Any) -> dict[str, Any]:
        order.append("job")
        order.append(f"upserts_at_job={len(pool.upserts())}")
        return {"ok": True, "edges": 3}

    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)

    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "ok"
    assert order == ["job", "upserts_at_job=1"]
    upserts = pool.upserts()
    assert upserts[0] == f"{kg.BUILD_MEDIATED_JOB}_last_run_at"
    assert upserts[1] == f"{kg.BUILD_MEDIATED_JOB}_last_result"
    stamp = next(
        args[1]
        for sql, args in pool.log
        if "INSERT INTO system_settings" in sql and args[0].endswith("_last_run_at")
    )
    assert stamp == NOW.isoformat()
    gc.assert_called_once()
    assert pool.unlocks() == [kg.BUILD_MEDIATED_LOCK_ID, kg.GARBAGE_COLLECT_LOCK_ID]
    assert pool.released == 2


@pytest.mark.asyncio
async def test_unparseable_last_run_is_due() -> None:
    pool = FakePool({"settings": {f"{kg.BUILD_MEDIATED_JOB}_last_run_at": "not-a-date"}})
    build = AsyncMock(return_value={"ok": True})
    gc = AsyncMock(return_value={"ok": True})
    await run_with(pool, build, gc)
    build.assert_called_once()


@pytest.mark.asyncio
async def test_job_timeout_reports_timeout_and_unlocks() -> None:
    pool = FakePool()

    async def slow(_pool: Any) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"ok": True}

    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, slow, gc, make_config(timeout_seconds=0.01))  # type: ignore[arg-type]
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "timeout"
    assert kg.BUILD_MEDIATED_LOCK_ID in pool.unlocks()
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_job_error_does_not_stop_second_job() -> None:
    pool = FakePool()
    build = AsyncMock(side_effect=RuntimeError("boom"))
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "error", "error": "boom"}
    assert results[kg.GARBAGE_COLLECT_JOB]["status"] == "ok"
    assert pool.unlocks() == [kg.BUILD_MEDIATED_LOCK_ID, kg.GARBAGE_COLLECT_LOCK_ID]
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_job_result_not_ok_is_error_status() -> None:
    pool = FakePool()
    build = AsyncMock(return_value={"ok": False})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "error"


@pytest.mark.asyncio
async def test_run_kg_maintenance_returns_error_when_pool_creation_fails() -> None:
    with patch("asyncpg.create_pool", new=AsyncMock(side_effect=OSError("db down"))):
        result = await run_kg_maintenance("postgresql://x/db", make_config())
    assert result == {"status": "error", "error": "db down"}


@pytest.mark.asyncio
async def test_run_kg_maintenance_runs_and_closes_pool() -> None:
    pool = FakePool()
    with (
        patch("asyncpg.create_pool", new=AsyncMock(return_value=pool)) as create_pool,
        patch.object(kg, "run_due_jobs", new=AsyncMock(return_value={"x": {"status": "ok"}})),
    ):
        result = await run_kg_maintenance("postgresql://x/db", make_config())
    assert result == {"status": "ran", "jobs": {"x": {"status": "ok"}}}
    assert create_pool.call_args.kwargs == {"min_size": 1, "max_size": 2}
    assert pool.closed is True


@pytest.mark.asyncio
async def test_run_forever_once_runs_kg_maintenance_after_poll() -> None:
    from backend.workers import drive_poll_worker as worker

    calls: list[str] = []

    async def fake_once(_config: Any) -> dict[str, Any]:
        calls.append("run_once")
        return {}

    async def fake_kg(_url: str, _config: Any) -> dict[str, Any]:
        calls.append("kg")
        return {"status": "disabled"}

    config = worker.DrivePollWorkerConfig(
        database_url="postgresql://x/db",
        interval_seconds=30,
        jitter_seconds=0,
        timeout_seconds=60,
        inline_ocr=False,
        once=True,
    )
    with (
        patch.object(worker, "run_once", new=fake_once),
        patch.object(worker, "run_kg_maintenance", new=fake_kg),
    ):
        assert await worker.run_forever(config) == 0
    assert calls == ["run_once", "kg"]


def test_env_defaults_and_clamping() -> None:
    with patch.dict("os.environ", {}, clear=True):
        config = load_kg_maintenance_config()
    assert config == KgMaintenanceConfig(True, 21_600, 86_400, 900)

    with patch.dict("os.environ", {"CRM_KG_MAINTENANCE_ENABLED": "false"}, clear=True):
        assert load_kg_maintenance_config().enabled is False

    env = {
        "CRM_KG_MAINTENANCE_ENABLED": "true",
        "CRM_KG_BUILD_MEDIATED_INTERVAL_SECONDS": "10",
        "CRM_KG_GARBAGE_COLLECT_INTERVAL_SECONDS": "99999999",
        "CRM_KG_MAINTENANCE_TIMEOUT_SECONDS": "5",
    }
    with patch.dict("os.environ", env, clear=True):
        config = load_kg_maintenance_config()
    assert config == KgMaintenanceConfig(True, 3600, 604_800, 60)

    with patch.dict("os.environ", {"CRM_KG_MAINTENANCE_TIMEOUT_SECONDS": "9999"}, clear=True):
        assert load_kg_maintenance_config().timeout_seconds == 3600
