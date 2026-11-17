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
BUILD_RUN_KEY = f"{kg.BUILD_MEDIATED_JOB}_last_run_at"
GC_RUN_KEY = f"{kg.GARBAGE_COLLECT_JOB}_last_run_at"


def make_config(**overrides: Any) -> KgMaintenanceConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "build_mediated_interval_seconds": 21_600,
        "garbage_collect_interval_seconds": 86_400,
        "timeout_seconds": 60,
    }
    values.update(overrides)
    return KgMaintenanceConfig(**values)


@pytest.fixture(autouse=True)
def tiny_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kg, "_STEP_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(kg, "_total_budget_seconds", lambda _config: 0.2)


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def fetchval(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        self.pool.calls.append(("fetchval", kwargs))
        self.pool.claims.append((sql, args, kwargs))
        key = args[0]
        if key in self.pool.state.get("claim_error_keys", set()):
            raise TimeoutError
        if key in self.pool.state.get("not_claimed_keys", set()):
            return None
        return key

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> None:
        self.pool.calls.append(("execute", kwargs))
        self.pool.writes.append((sql, args))
        if args[0].endswith(tuple(self.pool.state.get("write_error_suffixes", ()))):
            raise TimeoutError


class FakePool:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.claims: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self.state = state or {}
        self.released = 0
        self.closed = False
        self.terminated = False

    async def acquire(self, **kwargs: Any) -> FakeConn:
        self.calls.append(("acquire", kwargs))
        return FakeConn(self)

    async def release(self, _conn: Any, **kwargs: Any) -> None:
        self.calls.append(("release", kwargs))
        self.released += 1

    async def close(self, **kwargs: Any) -> None:
        self.calls.append(("close", kwargs))
        if self.state.get("close_hangs"):
            await asyncio.Event().wait()
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True

    def result_keys(self) -> list[str]:
        return [args[0] for _sql, args in self.writes]


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
async def test_claimed_runs_job_and_writes_result() -> None:
    pool = FakePool()
    build = AsyncMock(return_value={"ok": True, "edges": 3})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)

    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "ok"
    build.assert_called_once_with(pool)
    gc.assert_called_once()
    assert pool.result_keys() == [
        f"{kg.BUILD_MEDIATED_JOB}_last_result",
        f"{kg.GARBAGE_COLLECT_JOB}_last_result",
    ]
    assert pool.released == pool.calls.count(("acquire", {"timeout": 0.05}))


@pytest.mark.asyncio
async def test_not_claimed_skips_job() -> None:
    pool = FakePool({"not_claimed_keys": {BUILD_RUN_KEY}})
    build = AsyncMock(return_value={"ok": True})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "not_due"}
    build.assert_not_called()
    gc.assert_called_once()
    assert pool.result_keys() == [f"{kg.GARBAGE_COLLECT_JOB}_last_result"]


@pytest.mark.asyncio
async def test_claim_call_carries_timeout_and_atomic_sql() -> None:
    pool = FakePool()
    await run_with(pool, AsyncMock(return_value={}), AsyncMock(return_value={}))
    sql, args, kwargs = pool.claims[0]
    assert kwargs == {"timeout": kg._STEP_TIMEOUT_SECONDS}
    assert "ON CONFLICT" in sql
    assert "RETURNING" in sql
    assert "pg_advisory" not in sql
    assert args[0] == BUILD_RUN_KEY
    assert args[1] == NOW.isoformat()
    assert args[2] == NOW - timedelta(seconds=21_600)


@pytest.mark.asyncio
async def test_claim_error_is_error_and_second_job_runs() -> None:
    pool = FakePool({"claim_error_keys": {BUILD_RUN_KEY}})
    build = AsyncMock(return_value={"ok": True})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "error"
    build.assert_not_called()
    gc.assert_called_once()
    assert pool.released == 3


@pytest.mark.asyncio
async def test_job_timeout_reports_timeout() -> None:
    pool = FakePool()

    async def slow(_pool: Any) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"ok": True}

    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, slow, gc, make_config(timeout_seconds=0.01))  # type: ignore[arg-type]
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "timeout"
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_job_error_does_not_stop_second_job() -> None:
    pool = FakePool()
    build = AsyncMock(side_effect=RuntimeError("boom"))
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "error", "error": "boom"}
    assert results[kg.GARBAGE_COLLECT_JOB]["status"] == "ok"
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_job_result_not_ok_is_error_status() -> None:
    pool = FakePool()
    build = AsyncMock(return_value={"ok": False})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "error"


@pytest.mark.asyncio
async def test_result_write_failure_keeps_result_and_second_job_runs() -> None:
    pool = FakePool({"write_error_suffixes": ["_last_result"]})
    build = AsyncMock(return_value={"ok": True})
    gc = AsyncMock(return_value={"ok": True})
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "ok"
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_every_housekeeping_call_carries_a_timeout() -> None:
    pool = FakePool()
    await run_with(pool, AsyncMock(return_value={"ok": True}), AsyncMock(return_value={"ok": True}))
    ops = {op for op, _ in pool.calls}
    assert {"acquire", "release", "fetchval", "execute"} <= ops
    # 2 jobs x (acquire, claim, release, acquire, result write, release)
    assert len(pool.calls) == 12
    for op, kwargs in pool.calls:
        assert kwargs.get("timeout") == kg._STEP_TIMEOUT_SECONDS, op


@pytest.mark.asyncio
async def test_run_kg_maintenance_returns_error_when_pool_creation_fails() -> None:
    with patch.object(kg, "create_database_pool", new=AsyncMock(side_effect=OSError("db down"))):
        result = await run_kg_maintenance("postgresql://x/db", make_config())
    assert result == {"status": "error", "error": "db down"}


@pytest.mark.asyncio
async def test_run_kg_maintenance_runs_and_closes_pool() -> None:
    pool = FakePool()
    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)) as create_pool,
        patch.object(kg, "run_due_jobs", new=AsyncMock(return_value={"x": {"status": "ok"}})),
    ):
        result = await run_kg_maintenance("postgresql://x/db", make_config())
    assert result == {"status": "ran", "jobs": {"x": {"status": "ok"}}}
    kwargs = create_pool.call_args.kwargs
    assert kwargs["dsn"] == "postgresql://x/db"
    assert kwargs["normalize_dsn"] is False
    policy = kwargs["policy"]
    assert (policy.min_size, policy.max_size) == (1, 2)
    assert policy.command_timeout == 30
    assert policy.statement_timeout_seconds == 30
    assert policy.max_inactive_connection_lifetime == 300
    assert pool.closed is True
    assert pool.terminated is False


@pytest.mark.asyncio
async def test_run_due_jobs_never_returning_hits_total_budget() -> None:
    pool = FakePool()

    async def hang(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)),
        patch.object(kg, "run_due_jobs", new=hang),
    ):
        result = await asyncio.wait_for(
            run_kg_maintenance("postgresql://x/db", make_config()), timeout=1
        )
    assert result == {"status": "timeout", "timeout_seconds": 0.2}
    assert pool.closed or pool.terminated


@pytest.mark.asyncio
async def test_pool_close_hang_terminates_pool_and_returns() -> None:
    pool = FakePool({"close_hangs": True})
    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)),
        patch.object(kg, "run_due_jobs", new=AsyncMock(return_value={})),
    ):
        result = await asyncio.wait_for(
            run_kg_maintenance("postgresql://x/db", make_config()), timeout=1
        )
    assert result == {"status": "ran", "jobs": {}}
    assert pool.terminated is True


@pytest.mark.asyncio
async def test_pool_creation_hang_returns_timeout_dict() -> None:
    async def hang(**_kwargs: Any) -> Any:
        await asyncio.Event().wait()

    with patch.object(kg, "create_database_pool", new=hang):
        result = await asyncio.wait_for(
            run_kg_maintenance("postgresql://x/db", make_config()), timeout=1
        )
    assert result["status"] == "timeout"


@pytest.mark.asyncio
async def test_outer_cancellation_is_not_swallowed() -> None:
    pool = FakePool()
    started = asyncio.Event()

    async def hang(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {}

    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)),
        patch.object(kg, "run_due_jobs", new=hang),
        patch.object(kg, "_total_budget_seconds", lambda _config: 30),
    ):
        task = asyncio.create_task(run_kg_maintenance("postgresql://x/db", make_config()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pool.closed or pool.terminated


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
