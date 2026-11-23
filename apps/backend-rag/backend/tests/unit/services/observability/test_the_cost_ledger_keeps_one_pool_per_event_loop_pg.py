"""The LLM cost ledger keeps one database pool per event loop.

Every recorded model call used to open a pool of its own and leave it behind: two
connections that stayed open until the pool's inactivity limit closed them, five minutes
later. A burst of calls held twice as many connections as there were calls.

These tests run the recorder on a real PostgreSQL with real pools. The opener that stands
in for ``get_db_pool`` builds each pool with the application's own factory and policy, in a
private schema, and counts how many it was asked for; the connections are counted on the
server. Invented rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
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

# The policy ``get_db_pool`` gives its pools.
_POLICY = database.DatabasePoolPolicy(
    min_size=database._POOL_MIN_SIZE,
    max_size=database._POOL_MAX_SIZE,
    command_timeout=database._COMMAND_TIMEOUT,
    max_inactive_connection_lifetime=database._MAX_INACTIVE_CONN_LIFETIME,
)


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
    """Stands in for ``get_db_pool``: real pools, counted; and the server's own counts."""

    def __init__(
        self, dsn: str, application: str, schema: str, monitor: asyncpg.Connection
    ) -> None:
        self._dsn = dsn
        self._application = application
        self._schema = schema
        self._monitor = monitor
        self._opened: list[tuple[asyncpg.Pool, asyncio.AbstractEventLoop]] = []
        self.asked = 0
        self.takes = 0.0
        self.fails = 0
        self.gives_nothing = 0

    async def open_pool(self) -> asyncpg.Pool | None:
        self.asked += 1
        if self.takes:
            await asyncio.sleep(self.takes)
        if self.fails:
            self.fails -= 1
            raise OSError("the database cannot be reached")
        if self.gives_nothing:
            self.gives_nothing -= 1
            return None
        pool = await database.create_database_pool(
            dsn=self._dsn, policy=_POLICY, normalize_dsn=False
        )
        self._opened.append((pool, asyncio.get_running_loop()))
        return pool

    async def close_pools_of_this_loop(self) -> None:
        here = asyncio.get_running_loop()
        for pool, loop in self._opened:
            if loop is here:
                await pool.close()
        self._opened = [(pool, loop) for pool, loop in self._opened if loop is not here]

    async def connections(self) -> int:
        return await self._monitor.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = $1",
            self._application,
        )

    async def rows(self) -> list[asyncpg.Record]:
        return await self._monitor.fetch("SELECT * FROM llm_cost_events ORDER BY id")

    async def inserts_waiting_on_a_lock(self) -> int:
        return await self._monitor.fetchval(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE application_name = $1 AND wait_event_type = 'Lock'",
            self._application,
        )

    @contextlib.asynccontextmanager
    async def table_locked_by_another_session(self) -> AsyncIterator[None]:
        """Every insert waits for as long as this is open.

        The lock is held by a session of its own: the counts above are read outside any
        transaction, because PostgreSQL keeps one view of the sessions for a whole
        transaction and a count taken inside the locking one would never move.
        """
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
    schema = "cost_ledger_" + uuid4().hex
    application = "cost_ledger_test_" + uuid4().hex[:12]
    monitor = await asyncpg.connect(_DB_URL, server_settings={"search_path": schema})
    await monitor.execute(f'CREATE SCHEMA "{schema}"')
    await monitor.execute(_TABLE)
    separator = "&" if "?" in (_DB_URL or "") else "?"
    dsn = f"{_DB_URL}{separator}application_name={application}&search_path={schema}"
    stand_in = _Database(dsn, application, schema, monitor)
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


def _on_a_loop_of_its_own(recorder: LLMCostRecorder, db: _Database, event: LLMCallEvent) -> Any:
    """Record as a synchronous entry would: ``asyncio.run`` for this one call.

    The helper closes that loop's pool before the loop ends. That is hygiene of this
    test file only: nothing in the application closes a pool on a loop that
    ``asyncio.run`` ends, so these tests say nothing about what such a loop leaves behind.
    """

    async def call() -> dict[str, bool]:
        try:
            return await recorder.record(event)
        finally:
            await db.close_pools_of_this_loop()

    return asyncio.to_thread(asyncio.run, call())


async def test_recorded_calls_one_after_another_share_one_pool(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    for number in range(12):
        assert (await recorder.record(_call(number)))["postgres"] is True

    assert db.asked == 1
    assert 1 <= await db.connections() <= database._POOL_MAX_SIZE
    assert [row["request_id"] for row in await db.rows()] == [f"request-{n}" for n in range(12)]


async def test_recorded_calls_at_the_same_moment_open_one_pool(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    db.takes = 0.05

    results = await asyncio.gather(*(recorder.record(_call(n)) for n in range(12)))

    assert [result["postgres"] for result in results] == [True] * 12
    assert db.asked == 1
    assert 1 <= await db.connections() <= database._POOL_MAX_SIZE
    assert sorted(row["request_id"] for row in await db.rows()) == sorted(
        f"request-{n}" for n in range(12)
    )


async def test_the_row_written_is_the_call(recorder: LLMCostRecorder, db: _Database) -> None:
    event = _call(7)

    await recorder.record(event)

    (row,) = await db.rows()
    assert {name: row[name] for name in row.keys() if name != "id"} == {
        "ts_utc": event.ts_utc,
        "provider": "synthetic",
        "model": "synthetic-model",
        "input_tokens": 107,
        "output_tokens": 17,
        "cache_hit_tokens": 0,
        "cost_usd": Decimal("0.000125"),
        "endpoint": "synthetic_endpoint",
        "request_id": "request-7",
        "success": True,
        "error_class": None,
        "latency_ms": 40,
    }


async def test_a_call_on_a_loop_of_its_own_gets_a_pool_of_that_loop_and_its_row_lands(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    # A pool works only on the loop that opened it: the pool this loop already holds
    # is of no use to the two calls below.
    assert (await recorder.record(_call(1)))["postgres"] is True

    second = await _on_a_loop_of_its_own(recorder, db, _call(2))
    third = await _on_a_loop_of_its_own(recorder, db, _call(3))

    assert second["postgres"] is True
    assert third["postgres"] is True
    assert db.asked == 3
    assert [row["request_id"] for row in await db.rows()] == [
        "request-1",
        "request-2",
        "request-3",
    ]
    # And this loop still has its own.
    assert (await recorder.record(_call(4)))["postgres"] is True
    assert db.asked == 3


async def test_loops_that_have_closed_are_forgotten(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    for number in range(4):
        await _on_a_loop_of_its_own(recorder, db, _call(number))

    await recorder.record(_call(9))

    # Not an interface: the one place the recorder could grow with every such call.
    assert len(recorder._loops) == 1


async def test_a_failed_opening_is_shared_by_the_calls_waiting_on_it_and_tried_again_later(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    db.takes = 0.05
    db.fails = 1

    results = await asyncio.gather(*(recorder.record(_call(n)) for n in range(8)))

    # One attempt, one outcome for all eight: none of them queued up an attempt of its own.
    assert db.asked == 1
    assert [result["postgres"] for result in results] == [False] * 8
    assert [result["jsonl"] for result in results] == [True] * 8
    assert await db.rows() == []

    assert (await recorder.record(_call(20)))["postgres"] is True
    assert db.asked == 2
    assert [row["request_id"] for row in await db.rows()] == ["request-20"]


async def test_an_opener_that_gives_no_pool_is_asked_again(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    db.gives_nothing = 1

    assert (await recorder.record(_call(1)))["postgres"] is False
    assert (await recorder.record(_call(2)))["postgres"] is True

    assert db.asked == 2
    assert [row["request_id"] for row in await db.rows()] == ["request-2"]


async def test_a_call_cancelled_while_the_pool_opens_does_not_cancel_the_opening(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    db.takes = 0.05
    first = asyncio.ensure_future(recorder.record(_call(1)))
    await asyncio.sleep(0.01)
    assert db.asked == 1

    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    await asyncio.sleep(0.2)
    db.takes = 0.0

    assert (await recorder.record(_call(2)))["postgres"] is True
    assert db.asked == 1
    # The cancelled call's write is not cancelled with it: it waited on the same opening
    # and lands too.
    assert sorted(row["request_id"] for row in await db.rows()) == ["request-1", "request-2"]


async def test_an_opening_that_fails_after_its_only_caller_left_is_not_reported_as_lost(
    recorder: LLMCostRecorder, db: _Database
) -> None:
    reports: list[str] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reports.append(str(context.get("message"))))
    try:
        db.takes = 0.05
        db.fails = 1
        first = asyncio.ensure_future(recorder.record(_call(1)))
        await asyncio.sleep(0.01)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        await asyncio.sleep(0.2)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert db.asked == 1
    assert reports == []


async def test_a_pool_held_full_by_blocked_inserts_does_not_hold_a_recording_back(
    recorder: LLMCostRecorder,
    db: _Database,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(recorder_module, "PG_ACQUIRE_TIMEOUT_S", 0.3)
    assert (await recorder.record(_call(0)))["postgres"] is True
    held = database._POOL_MAX_SIZE
    blocked: list[asyncio.Future[dict[str, bool]]] = []
    loop = asyncio.get_running_loop()

    try:
        async with db.table_locked_by_another_session():
            # Each of these inserts waits on the lock and keeps its connection, until the
            # pool has none left.
            blocked = [asyncio.ensure_future(recorder.record(_call(n))) for n in range(1, held + 1)]
            for _ in range(200):
                if await db.inserts_waiting_on_a_lock() == held:
                    break
                await asyncio.sleep(0.02)
            assert await db.inserts_waiting_on_a_lock() == held
            assert not any(task.done() for task in blocked)

            started = loop.time()
            with caplog.at_level(logging.WARNING, logger=recorder_module.logger.name):
                late = await asyncio.wait_for(
                    asyncio.gather(*(recorder.record(_call(100 + n)) for n in range(3))), 5
                )
            waited = loop.time() - started

            assert [result["postgres"] for result in late] == [False] * 3
            assert [result["jsonl"] for result in late] == [True] * 3
            assert [result["prometheus"] for result in late] == [True] * 3
            assert 0.25 <= waited < 2.0
            refused = [
                record.getMessage()
                for record in caplog.records
                if "no free connection" in record.getMessage()
            ]
            assert len(refused) == 3
            assert not any(task.done() for task in blocked)
    finally:
        done = await asyncio.wait_for(asyncio.gather(*blocked), 10)

    # The inserts that were only waiting land once the lock is gone; nothing else does.
    assert [result["postgres"] for result in done] == [True] * held
    assert sorted(row["request_id"] for row in await db.rows()) == sorted(
        f"request-{n}" for n in range(held + 1)
    )
    assert db.asked == 1


async def test_a_pool_held_full_by_anyone_does_not_hold_a_recording_back(
    recorder: LLMCostRecorder, db: _Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder_module, "PG_ACQUIRE_TIMEOUT_S", 0.2)
    assert (await recorder.record(_call(0)))["postgres"] is True
    pool = db._opened[0][0]
    taken = [await pool.acquire() for _ in range(database._POOL_MAX_SIZE)]
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        result = await asyncio.wait_for(recorder.record(_call(1)), 5)
    finally:
        waited = loop.time() - started
        for connection in taken:
            await pool.release(connection)

    assert result == {"prometheus": True, "postgres": False, "jsonl": True}
    assert 0.15 <= waited < 2.0
    assert (await recorder.record(_call(2)))["postgres"] is True
    assert [row["request_id"] for row in await db.rows()] == ["request-0", "request-2"]


class _PoolWhoseInsertTimesOut:
    """Gives a connection at once; its statement then runs into the command timeout."""

    def acquire(self, *, timeout: float) -> _PoolWhoseInsertTimesOut:
        assert timeout == recorder_module.PG_ACQUIRE_TIMEOUT_S
        return self

    async def __aenter__(self) -> _PoolWhoseInsertTimesOut:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object) -> None:
        raise asyncio.TimeoutError


async def test_a_statement_that_times_out_is_not_called_a_missing_connection(
    recorder: LLMCostRecorder,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def gives_the_pool() -> _PoolWhoseInsertTimesOut:
        return _PoolWhoseInsertTimesOut()

    monkeypatch.setattr(database, "get_db_pool", gives_the_pool)

    with caplog.at_level(logging.WARNING, logger=recorder_module.logger.name):
        result = await recorder.record(_call(1))

    assert result["postgres"] is False
    failures = [r.getMessage() for r in caplog.records if "postgres write failed" in r.getMessage()]
    assert len(failures) == 1
    assert "no free connection" not in failures[0]


async def test_two_recorders_do_not_share_a_pool(tmp_path: Path, db: _Database) -> None:
    one = LLMCostRecorder(jsonl_root=tmp_path / "one")
    other = LLMCostRecorder(jsonl_root=tmp_path / "other")

    assert (await one.record(_call(1)))["postgres"] is True
    assert (await other.record(_call(2)))["postgres"] is True

    assert db.asked == 2
