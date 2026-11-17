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
    run_maintenance_loop,
)

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
BUILD_KEY = f"{kg.BUILD_MEDIATED_JOB}_last_run_at"
GC_KEY = f"{kg.GARBAGE_COLLECT_JOB}_last_run_at"


def make_config(**overrides: Any) -> KgMaintenanceConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "build_mediated_interval_seconds": 21_600,
        "garbage_collect_interval_seconds": 86_400,
        "timeout_seconds": 60,
        "check_seconds": 60,
    }
    values.update(overrides)
    return KgMaintenanceConfig(**values)


@pytest.fixture(autouse=True)
def tiny_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kg, "_STEP_TIMEOUT_SECONDS", 0.05)


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def fetchval(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        pool = self.pool
        pool.calls.append(("fetchval", kwargs))
        pool.statements.append((sql, args))
        key = args[0]
        if sql == kg._READ_SQL:
            return pool.store.get(key)
        if key in pool.state.get("claim_error_keys", set()):
            raise TimeoutError
        if key in pool.state.get("lose_race_keys", set()):
            return None
        if sql == kg._CLAIM_INSERT_SQL:
            if key in pool.store:
                return None
            pool.store[key] = args[1]
            return key
        if sql == kg._CLAIM_UPDATE_SQL:
            if pool.store.get(key) != args[2]:
                return None
            pool.store[key] = args[1]
            return key
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any, **kwargs: Any) -> None:
        self.pool.calls.append(("execute", kwargs))
        self.pool.writes.append(args)
        if args[0].endswith(tuple(self.pool.state.get("write_error_suffixes", ()))):
            raise TimeoutError


class FakePool:
    def __init__(
        self, store: dict[str, str] | None = None, state: dict[str, Any] | None = None
    ) -> None:
        self.store = store if store is not None else {}
        self.state = state or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.writes: list[tuple[Any, ...]] = []
        self.released = 0
        self.acquired = 0
        self.closed = False
        self.terminated = False

    async def acquire(self, **kwargs: Any) -> FakeConn:
        self.calls.append(("acquire", kwargs))
        self.acquired += 1
        return FakeConn(self)

    async def release(self, _conn: Any, **kwargs: Any) -> None:
        self.calls.append(("release", kwargs))
        self.released += 1

    async def close(self) -> None:
        if self.state.get("close_error"):
            raise OSError("close failed")
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


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
        return await run_due_jobs(pool, config or make_config())


def ok_job() -> AsyncMock:
    return AsyncMock(return_value={"ok": True})


# --- due decision ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "due"),
    [
        (None, True),
        ("garbage", True),
        ("2026-99-99T99:99:99+00:00", True),
        ("2026-11-18T05:00:00", True),  # naive counts as unparseable
        ((NOW - timedelta(seconds=60)).isoformat(), False),
        ((NOW - timedelta(seconds=3599)).isoformat(), False),
        ((NOW - timedelta(seconds=3600)).isoformat(), True),
        ((NOW - timedelta(days=3)).isoformat(), True),
    ],
)
def test_is_due(raw: str | None, due: bool) -> None:
    assert kg._is_due(raw, 3600, NOW) is due


# --- claim ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_row_uses_insert_claim_and_runs_job() -> None:
    pool = FakePool()
    build, gc = ok_job(), ok_job()
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "ok"
    build.assert_called_once_with(pool)
    gc.assert_called_once()
    claim_sqls = [sql for sql, _ in pool.statements if sql != kg._READ_SQL]
    assert claim_sqls == [kg._CLAIM_INSERT_SQL, kg._CLAIM_INSERT_SQL]
    assert [a[0] for a in pool.writes] == [
        f"{kg.BUILD_MEDIATED_JOB}_last_result",
        f"{kg.GARBAGE_COLLECT_JOB}_last_result",
    ]


@pytest.mark.asyncio
async def test_existing_due_row_uses_cas_update_on_the_exact_stored_text() -> None:
    old = (NOW - timedelta(days=2)).isoformat()
    pool = FakePool({BUILD_KEY: old})
    build, gc = ok_job(), ok_job()
    await run_with(pool, build, gc)
    updates = [args for sql, args in pool.statements if sql == kg._CLAIM_UPDATE_SQL]
    assert updates[0][0] == BUILD_KEY
    assert updates[0][2] == old
    assert pool.store[BUILD_KEY] != old
    build.assert_called_once()
    assert "WHERE key = $1 AND value = $3" in kg._CLAIM_UPDATE_SQL
    assert "ON CONFLICT (key) DO NOTHING" in kg._CLAIM_INSERT_SQL


@pytest.mark.asyncio
async def test_recent_stamp_is_not_due_and_job_not_called() -> None:
    recent = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    pool = FakePool({BUILD_KEY: recent, GC_KEY: recent})
    build, gc = ok_job(), ok_job()
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "not_due"}
    build.assert_not_called()
    gc.assert_not_called()
    assert pool.writes == []


@pytest.mark.asyncio
async def test_lost_race_is_not_due_and_job_not_called() -> None:
    pool = FakePool(state={"lose_race_keys": {BUILD_KEY}})
    build, gc = ok_job(), ok_job()
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "not_due"}
    build.assert_not_called()
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_each_job_claims_with_a_fresh_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([NOW, NOW + timedelta(minutes=20)])
    monkeypatch.setattr(kg, "_utcnow", lambda: next(ticks))
    pool = FakePool()
    await run_with(pool, ok_job(), ok_job())
    assert pool.store[BUILD_KEY] == NOW.isoformat()
    assert pool.store[GC_KEY] == (NOW + timedelta(minutes=20)).isoformat()


@pytest.mark.asyncio
async def test_claim_error_is_error_and_second_job_runs() -> None:
    pool = FakePool(state={"claim_error_keys": {BUILD_KEY}})
    build, gc = ok_job(), ok_job()
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "error"
    build.assert_not_called()
    gc.assert_called_once()
    assert pool.released == pool.acquired


# --- job outcomes and bounds -----------------------------------------------------


@pytest.mark.asyncio
async def test_job_timeout_reports_timeout() -> None:
    pool = FakePool()

    async def slow(_pool: Any) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"ok": True}

    gc = ok_job()
    results = await run_with(pool, slow, gc, make_config(timeout_seconds=0.01))  # type: ignore[arg-type]
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "timeout"
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_job_error_does_not_stop_second_job() -> None:
    pool = FakePool()
    build = AsyncMock(side_effect=RuntimeError("boom"))
    gc = ok_job()
    results = await run_with(pool, build, gc)
    assert results[kg.BUILD_MEDIATED_JOB] == {"status": "error", "error": "boom"}
    assert results[kg.GARBAGE_COLLECT_JOB]["status"] == "ok"
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_job_result_not_ok_is_error_status() -> None:
    pool = FakePool()
    results = await run_with(pool, AsyncMock(return_value={"ok": False}), ok_job())
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "error"


@pytest.mark.asyncio
async def test_result_write_failure_keeps_result_and_second_job_runs() -> None:
    pool = FakePool(state={"write_error_suffixes": ["_last_result"]})
    gc = ok_job()
    results = await run_with(pool, ok_job(), gc)
    assert results[kg.BUILD_MEDIATED_JOB]["status"] == "ok"
    gc.assert_called_once()


@pytest.mark.asyncio
async def test_every_housekeeping_call_carries_a_timeout() -> None:
    pool = FakePool()
    await run_with(pool, ok_job(), ok_job())
    ops = {op for op, _ in pool.calls}
    assert {"acquire", "release", "fetchval", "execute"} <= ops
    for op, kwargs in pool.calls:
        assert kwargs.get("timeout") == kg._STEP_TIMEOUT_SECONDS, op


# --- maintenance loop ------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_loop_returns_immediately_without_pool() -> None:
    create = AsyncMock()
    with patch.object(kg, "create_database_pool", new=create):
        await asyncio.wait_for(
            run_maintenance_loop("postgresql://x/db", make_config(enabled=False), asyncio.Event()),
            timeout=1,
        )
    create.assert_not_called()


@pytest.mark.asyncio
async def test_loop_creates_pool_once_with_policy_and_closes_on_stop() -> None:
    pool = FakePool()
    stop = asyncio.Event()
    runs = 0

    async def fake_due(_pool: Any, _config: Any) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 3:
            stop.set()
        return {}

    create = AsyncMock(return_value=pool)
    with (
        patch.object(kg, "create_database_pool", new=create),
        patch.object(kg, "run_due_jobs", new=fake_due),
    ):
        await asyncio.wait_for(
            run_maintenance_loop("postgresql://x/db", make_config(check_seconds=0.001), stop),
            timeout=1,
        )
    assert runs == 3
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["dsn"] == "postgresql://x/db"
    assert kwargs["normalize_dsn"] is False
    policy = kwargs["policy"]
    assert (policy.min_size, policy.max_size) == (1, 2)
    assert policy.command_timeout == 30
    assert policy.statement_timeout_seconds == 30
    assert pool.closed is True
    assert pool.terminated is False


@pytest.mark.asyncio
async def test_pool_creation_failure_retries_after_check_period() -> None:
    pool = FakePool()
    stop = asyncio.Event()
    attempts = 0

    async def flaky(**_kwargs: Any) -> FakePool:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("db down")
        return pool

    async def fake_due(_pool: Any, _config: Any) -> dict[str, Any]:
        stop.set()
        return {}

    with (
        patch.object(kg, "create_database_pool", new=flaky),
        patch.object(kg, "run_due_jobs", new=fake_due),
    ):
        await asyncio.wait_for(
            run_maintenance_loop("postgresql://x/db", make_config(check_seconds=0.001), stop),
            timeout=1,
        )
    assert attempts == 3
    assert pool.closed is True


@pytest.mark.asyncio
async def test_run_due_jobs_exception_does_not_end_loop() -> None:
    pool = FakePool()
    stop = asyncio.Event()
    runs = 0

    async def boom_then_stop(_pool: Any, _config: Any) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("boom")
        stop.set()
        return {}

    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)),
        patch.object(kg, "run_due_jobs", new=boom_then_stop),
    ):
        await asyncio.wait_for(
            run_maintenance_loop("postgresql://x/db", make_config(check_seconds=0.001), stop),
            timeout=1,
        )
    assert runs == 2
    assert pool.closed is True


@pytest.mark.asyncio
async def test_close_failure_terminates_pool() -> None:
    pool = FakePool(state={"close_error": True})
    stop = asyncio.Event()

    async def fake_due(_pool: Any, _config: Any) -> dict[str, Any]:
        stop.set()
        return {}

    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)),
        patch.object(kg, "run_due_jobs", new=fake_due),
    ):
        await asyncio.wait_for(run_maintenance_loop("postgresql://x/db", make_config(), stop), 1)
    assert pool.closed is False
    assert pool.terminated is True


@pytest.mark.asyncio
async def test_cancellation_closes_pool_and_propagates() -> None:
    pool = FakePool()
    stop = asyncio.Event()
    started = asyncio.Event()

    async def hang(_pool: Any, _config: Any) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {}

    with (
        patch.object(kg, "create_database_pool", new=AsyncMock(return_value=pool)),
        patch.object(kg, "run_due_jobs", new=hang),
    ):
        task = asyncio.create_task(run_maintenance_loop("postgresql://x/db", make_config(), stop))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert pool.closed is True


# --- config ---------------------------------------------------------------------


def test_env_defaults_and_clamping() -> None:
    with patch.dict("os.environ", {}, clear=True):
        config = load_kg_maintenance_config()
    assert config == KgMaintenanceConfig(True, 21_600, 86_400, 900, 300)

    with patch.dict("os.environ", {"CRM_KG_MAINTENANCE_ENABLED": "false"}, clear=True):
        assert load_kg_maintenance_config().enabled is False

    env = {
        "CRM_KG_MAINTENANCE_ENABLED": "true",
        "CRM_KG_BUILD_MEDIATED_INTERVAL_SECONDS": "10",
        "CRM_KG_GARBAGE_COLLECT_INTERVAL_SECONDS": "99999999",
        "CRM_KG_MAINTENANCE_TIMEOUT_SECONDS": "5",
        "CRM_KG_MAINTENANCE_CHECK_SECONDS": "1",
    }
    with patch.dict("os.environ", env, clear=True):
        config = load_kg_maintenance_config()
    assert config == KgMaintenanceConfig(True, 3600, 604_800, 60, 60)

    env = {
        "CRM_KG_MAINTENANCE_TIMEOUT_SECONDS": "9999",
        "CRM_KG_MAINTENANCE_CHECK_SECONDS": "99999",
    }
    with patch.dict("os.environ", env, clear=True):
        config = load_kg_maintenance_config()
    assert (config.timeout_seconds, config.check_seconds) == (1800, 3600)
    # an interval is always at least twice the job timeout
    assert config.build_mediated_interval_seconds >= 2 * config.timeout_seconds
