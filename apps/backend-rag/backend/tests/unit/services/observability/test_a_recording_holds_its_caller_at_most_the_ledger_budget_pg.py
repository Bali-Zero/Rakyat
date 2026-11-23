"""A recording holds the model call it records for at most the ledger's budget.

``record()`` awaited its Postgres write and then its JSONL write, with no limit of its own:
an opening of the pool, a statement behind a lock or a stuck filesystem held the answer of
the model call that was being recorded, already complete, for as long as they took.

Now one asynchronous wait over both writes ends at ``SINK_WAIT_BUDGET_S``. A write still
running then goes on, owned by the recorder, and its outcome is logged when it ends; at most
``MAX_OUTSTANDING_WRITES`` writes of one sink are outstanding on a loop, and past that the
sink is skipped; every log line about an event carries its key.

Postgres cases run on a real PostgreSQL in a private schema, with real pools; the stand-in
writes of the other cases end when the test says so. Invented rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import inspect
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

import backend.app.core.database as database
import backend.services.observability.llm_cost_recorder as recorder_module
from backend.services.observability.llm_cost_recorder import LLMCallEvent, LLMCostRecorder

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

# The table as migration 117 creates it.
_TABLE = """
CREATE TABLE llm_cost_events (
    id               BIGSERIAL PRIMARY KEY,
    ts_utc           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    provider         VARCHAR(32) NOT NULL,
    model            VARCHAR(128) NOT NULL,
    input_tokens     INT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens    INT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_hit_tokens INT NOT NULL DEFAULT 0 CHECK (cache_hit_tokens >= 0),
    cost_usd         NUMERIC(12, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    endpoint         VARCHAR(128),
    request_id       VARCHAR(64),
    success          BOOLEAN NOT NULL,
    error_class      VARCHAR(64),
    latency_ms       INT NOT NULL DEFAULT 0 CHECK (latency_ms >= 0)
);
"""

_POLICY = database.DatabasePoolPolicy(
    min_size=database._POOL_MIN_SIZE,
    max_size=database._POOL_MAX_SIZE,
    command_timeout=database._COMMAND_TIMEOUT,
    max_inactive_connection_lifetime=database._MAX_INACTIVE_CONN_LIFETIME,
)

_KEY = re.compile(r"event=([0-9a-f]{12})\b")
_WATCHDOG_S = 10.0


def _call(number: int = 1) -> LLMCallEvent:
    return LLMCallEvent(
        provider="synthetic",
        model="synthetic-model",
        input_tokens=100 + number,
        output_tokens=10 + number,
        cost_usd=0.000125,
        success=True,
        latency_ms=40,
        endpoint="synthetic_endpoint",
        request_id=f"request-{number}",
    )


class _Database:
    """Stands in for ``get_db_pool``: real pools in a private schema, counted."""

    def __init__(self, dsn: str, schema: str, monitor: asyncpg.Connection) -> None:
        self._dsn = dsn
        self._schema = schema
        self._monitor = monitor
        self._opened: list[tuple[asyncpg.Pool, asyncio.AbstractEventLoop]] = []
        self.asked = 0
        self.takes = 0.0

    async def open_pool(self) -> asyncpg.Pool:
        self.asked += 1
        if self.takes:
            await asyncio.sleep(self.takes)
        pool = await database.create_database_pool(
            dsn=self._dsn, policy=_POLICY, normalize_dsn=False
        )
        self._opened.append((pool, asyncio.get_running_loop()))
        return pool

    def pool(self) -> asyncpg.Pool:
        return self._opened[0][0]

    async def close_pools_of_this_loop(self) -> None:
        here = asyncio.get_running_loop()
        for pool, loop in self._opened:
            if loop is here:
                await pool.close()
        self._opened = [(pool, loop) for pool, loop in self._opened if loop is not here]

    async def requests(self) -> list[str]:
        rows = await self._monitor.fetch("SELECT request_id FROM llm_cost_events ORDER BY id")
        return [row["request_id"] for row in rows]

    async def has(self, count: int) -> bool:
        return len(await self.requests()) >= count

    async def inserts_waiting_on_a_lock(self) -> int:
        """Inserts waiting on the table of this test's schema, and on nothing else."""
        return await self._monitor.fetchval(
            "SELECT count(*) FROM pg_locks"
            " WHERE relation = 'llm_cost_events'::regclass AND NOT granted"
        )

    @contextlib.asynccontextmanager
    async def table_locked(self) -> AsyncGenerator[None, None]:
        """Every insert waits while this is open; the lock is held by a session of its own."""
        locker = await asyncpg.connect(_DB_URL, server_settings={"search_path": self._schema})
        holding = locker.transaction()
        await holding.start()
        try:
            await locker.execute("LOCK TABLE llm_cost_events IN ACCESS EXCLUSIVE MODE")
            yield
        finally:
            await holding.rollback()
            await locker.close()


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[_Database, None]:
    schema = "cost_budget_" + uuid4().hex
    monitor = await asyncpg.connect(_DB_URL, server_settings={"search_path": schema})
    await monitor.execute(f'CREATE SCHEMA "{schema}"')
    await monitor.execute(_TABLE)
    separator = "&" if "?" in (_DB_URL or "") else "?"
    dsn = f"{_DB_URL}{separator}search_path={schema}"
    stand_in = _Database(dsn, schema, monitor)
    monkeypatch.setattr(database, "get_db_pool", stand_in.open_pool)
    try:
        yield stand_in
    finally:
        await stand_in.close_pools_of_this_loop()
        await monitor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await monitor.close()


@pytest.fixture
def recorder(tmp_path: Path) -> LLMCostRecorder:
    return LLMCostRecorder(jsonl_root=tmp_path)


@pytest.fixture
def budget(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    def set_budget(seconds: float) -> None:
        monkeypatch.setattr(recorder_module, "SINK_WAIT_BUDGET_S", seconds)

    set_budget(0.2)
    return set_budget


class _HeldFsync:
    """The recorder's ``os``, whose ``fsync`` waits until the test lets it through."""

    def __init__(self) -> None:
        self.gate = threading.Event()

    def fsync(self, fd: int) -> None:
        if not self.gate.wait(_WATCHDOG_S):
            raise OSError("fsync held past the watchdog")
        os.fsync(fd)

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


@pytest.fixture
def held_fsync(monkeypatch: pytest.MonkeyPatch) -> Generator[_HeldFsync, None, None]:
    held = _HeldFsync()
    monkeypatch.setattr(recorder_module, "os", held)
    try:
        yield held
    finally:
        held.gate.set()


class _Write:
    """A stand-in sink write that ends when the test says so."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.error: BaseException | None = None

    async def __call__(self, event: LLMCallEvent) -> None:
        await self.release.wait()
        if self.error is not None:
            raise self.error

    def fail_now(self, error: BaseException) -> None:
        self.error = error
        self.release.set()


def _failing_prometheus(_event: LLMCallEvent) -> None:
    raise RuntimeError("synthetic prometheus failure")


def _lines(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.glob("llm_cost_log.*.jsonl")):
        found += [json.loads(line)["request_id"] for line in path.read_text().splitlines()]
    return found


def _writes(recorder: LLMCostRecorder, sink: str) -> set[asyncio.Task[None]]:
    return recorder._loops[id(asyncio.get_running_loop())].writes[sink]


async def _eventually(check: Callable[[], Any], what: str) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + _WATCHDOG_S
    while True:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if loop.time() > end:
            raise AssertionError(f"never happened: {what}")
        await asyncio.sleep(0.02)


async def _settled(recorder: LLMCostRecorder, sink: str) -> None:
    """The recorder no longer holds a write of ``sink``: its outcome has been read and logged."""
    await _eventually(lambda: not _writes(recorder, sink), f"the {sink} writes end")


@contextlib.contextmanager
def _loop_reports() -> Generator[list[str], None, None]:
    """What the running loop's exception handler is told while this is open."""
    loop = asyncio.get_running_loop()
    reports: list[str] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: reports.append(
            f"{context.get('message')} {context.get('exception')!r}"
        )
    )
    try:
        yield reports
    finally:
        loop.set_exception_handler(previous)


def _messages(caplog: pytest.LogCaptureFixture, level: int | None = None) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == recorder_module.logger.name and (level is None or r.levelno == level)
    ]


def _keys(messages: list[str]) -> set[str]:
    return {match.group(1) for message in messages for match in _KEY.finditer(message)}


async def _collected() -> None:
    gc.collect()
    await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)


# ── the caller is answered at the budget; the write goes on and lands ───────────────────


async def test_a_slow_pool_opening_answers_the_caller_and_the_row_lands_later(
    recorder: LLMCostRecorder,
    db: _Database,
    budget: Callable[[float], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    db.takes = 1.0
    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        assert await db.requests() == []
        assert result == {"prometheus": True, "postgres": False, "jsonl": True}
        [unconfirmed] = [m for m in _messages(caplog) if "not confirmed" in m]
        assert "postgres write" in unconfirmed and "jsonl" not in unconfirmed

        await _settled(recorder, "postgres")

    assert await db.requests() == ["request-1"]
    [late] = [m for m in _messages(caplog, logging.INFO) if "landed late" in m]
    assert late.startswith("postgres write")
    assert _keys([unconfirmed]) == _keys([late])
    db.takes = 0.0
    assert (await recorder.record(_call(2)))["postgres"] is True
    assert db.asked == 1


async def test_a_statement_behind_a_lock_answers_the_caller_and_lands_after_the_release(
    recorder: LLMCostRecorder, db: _Database, budget: Callable[[float], None]
) -> None:
    assert (await recorder.record(_call(0)))["postgres"] is True

    async with db.table_locked():
        result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        assert result == {"prometheus": True, "postgres": False, "jsonl": True}
        assert await db.inserts_waiting_on_a_lock() == 1

    await _settled(recorder, "postgres")
    assert await db.requests() == ["request-0", "request-1"]


async def test_a_write_that_gives_up_on_a_full_pool_after_the_answer_inserts_nothing(
    recorder: LLMCostRecorder,
    db: _Database,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder_module, "PG_ACQUIRE_TIMEOUT_S", 0.6)
    assert (await recorder.record(_call(0)))["postgres"] is True
    pool = db.pool()
    taken = [await pool.acquire() for _ in range(database._POOL_MAX_SIZE)]
    try:
        with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
            result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
            assert result["postgres"] is False
            await _settled(recorder, "postgres")
    finally:
        for connection in taken:
            await pool.release(connection)

    [late] = [m for m in _messages(caplog) if "failed late" in m]
    assert late.startswith("postgres write") and "no free connection" in late
    [unconfirmed] = [m for m in _messages(caplog) if "not confirmed" in m]
    assert _keys([late]) == _keys([unconfirmed])
    assert (await recorder.record(_call(2)))["postgres"] is True
    assert await db.requests() == ["request-0", "request-2"]


async def test_a_slow_first_preparation_of_the_jsonl_directory_does_not_hold_the_loop(
    recorder: LLMCostRecorder,
    tmp_path: Path,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget(0.4)
    prepare = LLMCostRecorder._prepare_jsonl_dir

    def slow_prepare(self: LLMCostRecorder) -> bool:
        time.sleep(1.0)
        return prepare(self)

    monkeypatch.setattr(LLMCostRecorder, "_prepare_jsonl_dir", slow_prepare)
    monkeypatch.setattr(recorder, "_write_postgres", AsyncMock())
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticking = asyncio.ensure_future(ticker())
    try:
        await asyncio.sleep(0.05)
        before = ticks
        result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        during = ticks - before
    finally:
        ticking.cancel()

    assert result == {"prometheus": True, "postgres": True, "jsonl": False}
    assert during >= 5
    await _settled(recorder, "jsonl")
    assert _lines(tmp_path) == ["request-1"]


async def test_a_held_jsonl_write_answers_the_caller_and_lands_in_order_after_the_release(
    recorder: LLMCostRecorder,
    tmp_path: Path,
    budget: Callable[[float], None],
    held_fsync: _HeldFsync,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recorder, "_write_postgres", AsyncMock())

    first = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
    second = await asyncio.wait_for(recorder.record(_call(2)), _WATCHDOG_S)

    assert first == second == {"prometheus": True, "postgres": True, "jsonl": False}
    assert len(_writes(recorder, "jsonl")) == 2
    held_fsync.gate.set()
    await _settled(recorder, "jsonl")
    assert _lines(tmp_path) == ["request-1", "request-2"]


async def test_fast_writes_answer_true_and_leave_nothing_behind(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    result = await recorder.record(_call(1))

    assert result == {"prometheus": True, "postgres": True, "jsonl": True}
    assert _writes(recorder, "postgres") == set()
    assert _writes(recorder, "jsonl") == set()


async def test_the_wait_ends_at_the_budget(
    recorder: LLMCostRecorder, budget: Callable[[float], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    budget(0.3)
    postgres, jsonl = _Write(), _Write()
    monkeypatch.setattr(recorder, "_write_postgres", postgres)
    monkeypatch.setattr(recorder, "_write_jsonl", jsonl)
    loop = asyncio.get_running_loop()

    started = loop.time()
    result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
    elapsed = loop.time() - started

    assert result == {"prometheus": True, "postgres": False, "jsonl": False}
    assert 0.29 <= elapsed < 1.0
    postgres.release.set()
    jsonl.release.set()
    await _settled(recorder, "postgres")
    await _settled(recorder, "jsonl")


async def test_what_ends_before_the_budget_is_reported_and_what_ends_after_is_late(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    budget(0.4)
    postgres, jsonl = _Write(), _Write()
    monkeypatch.setattr(recorder, "_write_postgres", postgres)
    monkeypatch.setattr(recorder, "_write_jsonl", jsonl)
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, postgres.release.set)
    loop.call_later(0.8, jsonl.release.set)

    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        assert not jsonl.release.is_set()
        await _settled(recorder, "jsonl")

    assert result == {"prometheus": True, "postgres": True, "jsonl": False}
    [late] = [m for m in _messages(caplog, logging.INFO) if "landed late" in m]
    assert late.startswith("jsonl write")


# ── what the logs say ─────────────────────────────────────────────────────────────────────


async def _record_with_held_writes(
    recorder: LLMCostRecorder, monkeypatch: pytest.MonkeyPatch
) -> tuple[_Write, _Write, dict[str, bool]]:
    postgres, jsonl = _Write(), _Write()
    monkeypatch.setattr(recorder, "_write_postgres", postgres)
    monkeypatch.setattr(recorder, "_write_jsonl", jsonl)
    monkeypatch.setattr(recorder, "_write_prometheus", _failing_prometheus)
    result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
    return postgres, jsonl, result


async def test_the_critical_line_waits_for_the_last_failure_and_comes_once(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        postgres, jsonl, result = await _record_with_held_writes(recorder, monkeypatch)
        assert result == {"prometheus": False, "postgres": False, "jsonl": False}
        assert _messages(caplog, logging.CRITICAL) == []
        assert [m for m in _messages(caplog, logging.WARNING) if "not confirmed" in m]

        postgres.fail_now(RuntimeError("synthetic postgres failure"))
        await _settled(recorder, "postgres")
        assert _messages(caplog, logging.CRITICAL) == []

        jsonl.fail_now(OSError("synthetic jsonl failure"))
        await _settled(recorder, "jsonl")

    [critical] = _messages(caplog, logging.CRITICAL)
    assert "ALL THREE llm_cost sinks failed" in critical
    assert len(_keys(_messages(caplog))) == 1


@pytest.mark.parametrize("succeeds", ["postgres", "jsonl"])
async def test_a_write_that_lands_late_rules_the_critical_line_out(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    succeeds: str,
) -> None:
    fails = "jsonl" if succeeds == "postgres" else "postgres"
    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        postgres, jsonl, _ = await _record_with_held_writes(recorder, monkeypatch)
        writes = {"postgres": postgres, "jsonl": jsonl}
        writes[succeeds].release.set()
        await _settled(recorder, succeeds)
        writes[fails].fail_now(RuntimeError("synthetic failure"))
        await _settled(recorder, fails)

    assert [m for m in _messages(caplog) if "failed late" in m]
    assert _messages(caplog, logging.CRITICAL) == []


async def test_all_three_failing_within_the_budget_is_critical_at_once(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder, "_write_prometheus", _failing_prometheus)
    monkeypatch.setattr(recorder, "_write_postgres", AsyncMock(side_effect=RuntimeError("db")))
    monkeypatch.setattr(recorder, "_write_jsonl", AsyncMock(side_effect=OSError("disk")))

    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        result = await recorder.record(_call(1))

    assert result == {"prometheus": False, "postgres": False, "jsonl": False}
    assert len(_messages(caplog, logging.CRITICAL)) == 1
    assert not [m for m in _messages(caplog) if "not confirmed" in m]


async def test_skipped_writes_count_as_failed_for_the_critical_line(
    recorder: LLMCostRecorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_OUTSTANDING_WRITES", 0)
    monkeypatch.setattr(recorder, "_write_prometheus", _failing_prometheus)

    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)

    assert result == {"prometheus": False, "postgres": False, "jsonl": False}
    assert len(_messages(caplog, logging.CRITICAL)) == 1
    skipped = [m for m in _messages(caplog) if "admission is saturated" in m]
    assert sorted(m.split(" ", 1)[0] for m in skipped) == ["jsonl", "postgres"]


async def test_every_line_about_an_event_carries_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    unwritable_root = tmp_path / "not-a-directory"
    unwritable_root.write_text("x")
    lines_root = tmp_path / "lines"
    lines_root.mkdir()
    event = _call(1)
    (lines_root / f"llm_cost_log.{event.ts_utc:%Y-%m-%d}.jsonl").mkdir()
    seen: dict[str, list[str]] = {}

    for name, root in (("root", unwritable_root), ("file", lines_root)):
        recorder = LLMCostRecorder(jsonl_root=root)
        monkeypatch.setattr(recorder, "_write_prometheus", _failing_prometheus)
        monkeypatch.setattr(recorder, "_write_postgres", AsyncMock(side_effect=RuntimeError("db")))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
            await asyncio.wait_for(recorder.record(event), _WATCHDOG_S)
        seen[name] = _messages(caplog)

    for lines in seen.values():
        assert len(lines) == 5, lines
        assert all(_KEY.search(line) for line in lines), lines
        assert len(_keys(lines)) == 1
    assert [line for line in seen["root"] if "is not writable" in line]
    assert [line for line in seen["file"] if "JSONL write failed" in line]


# ── ownership ─────────────────────────────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def _nothing() -> AsyncGenerator[None, None]:
    yield


@pytest.mark.parametrize("in_flight", ["postgres", "jsonl", "both"])
async def test_a_cancelled_caller_gets_the_answer_of_that_moment_and_both_writes_land(
    recorder: LLMCostRecorder,
    db: _Database,
    tmp_path: Path,
    budget: Callable[[float], None],
    held_fsync: _HeldFsync,
    in_flight: str,
) -> None:
    budget(_WATCHDOG_S)
    held_fsync.gate.set()
    assert (await recorder.record(_call(0)))["postgres"] is True
    if in_flight != "postgres":
        held_fsync.gate.clear()

    with _loop_reports() as reports:
        async with db.table_locked() if in_flight != "jsonl" else _nothing():
            caller = asyncio.ensure_future(recorder.record(_call(1)))
            # A write is held, or has ended, only once it exists: each wait starts from
            # something the write itself did.
            if in_flight == "postgres":
                await _eventually(lambda: len(_lines(tmp_path)) == 2, "the line lands")
                await _settled(recorder, "jsonl")
            else:
                await _eventually(lambda: _writes(recorder, "jsonl"), "the line is held")
            if in_flight == "jsonl":
                await _eventually(lambda: db.has(2), "the row lands")
                await _settled(recorder, "postgres")
            else:
                await _eventually(db.inserts_waiting_on_a_lock, "the insert is held")
            caller.cancel()
            result = await asyncio.wait_for(caller, _WATCHDOG_S)
            assert not caller.cancelled()
            assert result == {
                "prometheus": True,
                "postgres": in_flight == "jsonl",
                "jsonl": in_flight == "postgres",
            }
        held_fsync.gate.set()
        await _settled(recorder, "postgres")
        await _settled(recorder, "jsonl")
        await _collected()

    assert await db.requests() == ["request-0", "request-1"]
    assert _lines(tmp_path) == ["request-0", "request-1"]
    assert reports == []


async def test_a_write_that_fails_late_is_logged_once_and_reported_nowhere_else(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    postgres = _Write()
    monkeypatch.setattr(recorder, "_write_postgres", postgres)

    with (
        _loop_reports() as reports,
        caplog.at_level(logging.INFO, logger=recorder_module.logger.name),
    ):
        await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        postgres.fail_now(RuntimeError("synthetic late failure"))
        await _settled(recorder, "postgres")
        await _collected()

    [late] = [m for m in _messages(caplog) if "failed late" in m]
    assert late.startswith("postgres write failed late") and "synthetic late failure" in late
    assert _keys([late]) == _keys(_messages(caplog))
    assert reports == []


async def test_a_write_cancelled_from_outside_is_read_without_raising(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder, "_write_postgres", _Write())

    with (
        _loop_reports() as reports,
        caplog.at_level(logging.INFO, logger=recorder_module.logger.name),
    ):
        await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        [task] = _writes(recorder, "postgres")
        task.cancel()
        await _settled(recorder, "postgres")
        await _collected()

    [late] = [m for m in _messages(caplog) if "failed late" in m]
    assert late.startswith("postgres write failed late")
    assert reports == []


async def test_a_late_outcome_whose_line_cannot_be_logged_still_frees_its_write(
    recorder: LLMCostRecorder,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    postgres = _Write()
    monkeypatch.setattr(recorder, "_write_postgres", postgres)

    class _Refuses(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if "landed late" in record.getMessage():
                raise RuntimeError("synthetic logging failure")
            return True

    refuses = _Refuses()
    recorder_module.logger.addFilter(refuses)
    try:
        with (
            _loop_reports() as reports,
            caplog.at_level(logging.INFO, logger=recorder_module.logger.name),
        ):
            await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
            postgres.release.set()
            await _settled(recorder, "postgres")
            await _collected()
    finally:
        recorder_module.logger.removeFilter(refuses)

    assert reports == []
    [unlogged] = [m for m in _messages(caplog, logging.ERROR) if "could not be logged" in m]
    assert _KEY.search(unlogged)


async def test_a_write_that_cannot_be_started_leaves_nothing_unawaited(
    recorder: LLMCostRecorder, db: _Database, caplog: pytest.LogCaptureFixture
) -> None:
    loop = asyncio.get_running_loop()
    create_task = loop.create_task
    refused: list[Any] = []

    def refuses_the_jsonl_write(coroutine: Any, **kwargs: Any) -> asyncio.Task[Any]:
        if getattr(coroutine, "__qualname__", "").endswith("_write_jsonl"):
            refused.append(coroutine)
            raise RuntimeError("synthetic: the loop refuses new tasks")
        return create_task(coroutine, **kwargs)

    loop.create_task = refuses_the_jsonl_write  # type: ignore[method-assign]
    try:
        with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
            result = await asyncio.wait_for(recorder.record(_call(1)), _WATCHDOG_S)
        # A coroutine that is never run must be closed, or it is reported as never awaited.
        states = [inspect.getcoroutinestate(coroutine) for coroutine in refused]
    finally:
        del loop.create_task
        for coroutine in refused:
            coroutine.close()

    assert states == [inspect.CORO_CLOSED]
    assert result == {"prometheus": True, "postgres": True, "jsonl": False}
    [refusal] = [m for m in _messages(caplog) if "could not be started" in m]
    assert refusal.startswith("jsonl write") and _KEY.search(refusal)
    assert _messages(caplog, logging.CRITICAL) == []
    assert await db.requests() == ["request-1"]


# ── a finite number of outstanding writes ─────────────────────────────────────────────────


async def test_a_held_jsonl_sink_is_skipped_past_the_cap_and_admitted_again_after(
    recorder: LLMCostRecorder,
    db: _Database,
    tmp_path: Path,
    budget: Callable[[float], None],
    held_fsync: _HeldFsync,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_OUTSTANDING_WRITES", 3)
    results = []
    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        for number in range(1, 7):
            results.append(await asyncio.wait_for(recorder.record(_call(number)), _WATCHDOG_S))
            assert len(_writes(recorder, "jsonl")) <= 3
        skipped = [m for m in _messages(caplog) if "admission is saturated" in m]
        held_fsync.gate.set()
        await _settled(recorder, "jsonl")
        assert (await recorder.record(_call(7)))["jsonl"] is True

    assert [r["postgres"] for r in results] == [True] * 6
    assert [r["jsonl"] for r in results] == [False] * 6
    assert len(skipped) == 1 and skipped[0].startswith("jsonl write skipped")
    [again] = [m for m in _messages(caplog) if "admitted for" in m]
    assert again.startswith("jsonl write admitted") and "after 3 skipped" in again
    assert _lines(tmp_path) == ["request-1", "request-2", "request-3", "request-7"]
    assert len(await db.requests()) == 7


async def test_a_held_postgres_sink_is_skipped_past_the_cap_and_admitted_again_after(
    recorder: LLMCostRecorder,
    db: _Database,
    tmp_path: Path,
    budget: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder_module, "MAX_OUTSTANDING_WRITES", 3)
    assert (await recorder.record(_call(0)))["postgres"] is True
    results = []
    with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
        async with db.table_locked():
            for number in range(1, 7):
                results.append(await asyncio.wait_for(recorder.record(_call(number)), _WATCHDOG_S))
                assert len(_writes(recorder, "postgres")) <= 3
            assert await db.inserts_waiting_on_a_lock() == 3
        skipped = [m for m in _messages(caplog) if "admission is saturated" in m]
        await _settled(recorder, "postgres")
        assert (await recorder.record(_call(7)))["postgres"] is True

    assert [r["postgres"] for r in results] == [False] * 6
    assert [r["jsonl"] for r in results] == [True] * 6
    assert len(skipped) == 1 and skipped[0].startswith("postgres write skipped")
    [again] = [m for m in _messages(caplog) if "admitted for" in m]
    assert again.startswith("postgres write admitted") and "after 3 skipped" in again
    # The three held inserts take the lock in turn; their order is the database's.
    assert sorted(await db.requests()) == [
        "request-0",
        "request-1",
        "request-2",
        "request-3",
        "request-7",
    ]
    assert len(_lines(tmp_path)) == 8


# ── a loop of its own ─────────────────────────────────────────────────────────────────────


async def test_a_recording_on_a_loop_of_its_own_answers_at_the_budget_and_its_end_is_logged(
    recorder: LLMCostRecorder,
    tmp_path: Path,
    budget: Callable[[float], None],
    held_fsync: _HeldFsync,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder, "_write_postgres", AsyncMock())
    reports: list[str] = []

    async def call() -> dict[str, bool]:
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: reports.append(str(context.get("message")))
        )
        return await recorder.record(_call(1))

    release = threading.Timer(0.5, held_fsync.gate.set)
    release.start()
    try:
        with caplog.at_level(logging.INFO, logger=recorder_module.logger.name):
            result = await asyncio.wait_for(asyncio.to_thread(asyncio.run, call()), _WATCHDOG_S)
    finally:
        release.cancel()
        held_fsync.gate.set()

    assert result == {"prometheus": True, "postgres": True, "jsonl": False}
    assert reports == []
    [late] = [m for m in _messages(caplog) if "failed late" in m]
    assert late.startswith("jsonl write failed late")
    assert len(_lines(tmp_path)) <= 1
