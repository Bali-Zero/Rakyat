"""Annual leave carries its signed remainder into the next year, and a balance may go negative."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.services.hr import hr_service
from backend.app.services.hr.hr_service import (
    HRService,
    _lock_leave_balances,
    _lock_leave_balances_a_year_opens_from,
    _open_year_balances,
)
from backend.db.migration_base import split_migration_sql

# The leave tables as migration_068_hr_payroll.py creates them, CHECKs included;
# the forward part of migration 388 is then applied on top, exactly as it ships.
_SCHEMA_068_SQL = """
CREATE TABLE team_members (
    id VARCHAR(36) PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL
);
CREATE TABLE hr_employees (
    id SERIAL PRIMARY KEY,
    team_member_id VARCHAR(36) NOT NULL REFERENCES team_members(id),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE hr_leave_types (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    default_days INTEGER NOT NULL CHECK (default_days >= 0),
    is_paid BOOLEAN NOT NULL DEFAULT TRUE,
    requires_document BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE hr_leave_balances (
    id SERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE CASCADE,
    leave_type_id INTEGER NOT NULL REFERENCES hr_leave_types(id) ON DELETE RESTRICT,
    balance_year SMALLINT NOT NULL CHECK (balance_year BETWEEN 2000 AND 2100),
    allocated_days INTEGER NOT NULL DEFAULT 0 CHECK (allocated_days >= 0),
    carried_over INTEGER NOT NULL DEFAULT 0 CHECK (carried_over >= 0),
    used_days INTEGER NOT NULL DEFAULT 0 CHECK (used_days >= 0),
    pending_days INTEGER NOT NULL DEFAULT 0 CHECK (pending_days >= 0),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_hr_leave_bal UNIQUE (employee_id, leave_type_id, balance_year),
    CONSTRAINT ck_hr_leave_bal_nonneg CHECK (
        (allocated_days + carried_over) >= (used_days + pending_days)
    )
);
CREATE TABLE hr_leave_requests (
    id BIGSERIAL PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES hr_employees(id) ON DELETE CASCADE,
    leave_type_id INTEGER NOT NULL REFERENCES hr_leave_types(id) ON DELETE RESTRICT,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    total_days INTEGER NOT NULL CHECK (total_days > 0),
    reason TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by VARCHAR(36) REFERENCES team_members(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMPTZ,
    rejection_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_hr_leave_req_dates CHECK (end_date >= start_date),
    CONSTRAINT ck_hr_leave_req_status CHECK (
        status IN ('pending', 'approved', 'rejected', 'cancelled')
    )
);
"""

_MIGRATION_388 = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "388_hr_leave_balance_may_go_negative.sql"
)

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="isolated PG required")


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(TEST_DSN)
    schema = "leavecarry_" + uuid4().hex
    created = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        await created.execute(_SCHEMA_068_SQL)
        await created.execute(split_migration_sql(_MIGRATION_388.read_text())[0])
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _member(pool: asyncpg.Pool, name: str) -> str:
    member_id = uuid4().hex[:36]
    await pool.execute(
        "INSERT INTO team_members (id, full_name, email) VALUES ($1, $2, $3)",
        member_id,
        name,
        f"{member_id}@example.test",
    )
    return member_id


async def _employee(pool: asyncpg.Pool, name: str) -> int:
    member_id = await _member(pool, name)
    return await pool.fetchval(
        "INSERT INTO hr_employees (team_member_id) VALUES ($1) RETURNING id", member_id
    )


async def _leave_type(pool: asyncpg.Pool, code: str, days: int) -> int:
    return await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) VALUES ($1, $1, $2) RETURNING id",
        code,
        days,
    )


async def _balance(
    pool: asyncpg.Pool, employee_id: int, type_id: int, year: int, allocated: int, used: int
) -> None:
    await pool.execute(
        "INSERT INTO hr_leave_balances "
        "(employee_id, leave_type_id, balance_year, allocated_days, used_days) "
        "VALUES ($1, $2, $3, $4, $5)",
        employee_id,
        type_id,
        year,
        allocated,
        used,
    )


async def _row(pool: asyncpg.Pool, employee_id: int, type_id: int, year: int) -> dict:
    row = await pool.fetchrow(
        "SELECT allocated_days, carried_over, used_days, pending_days, "
        "allocated_days + carried_over - used_days - pending_days AS remaining "
        "FROM hr_leave_balances WHERE employee_id = $1 AND leave_type_id = $2 "
        "AND balance_year = $3",
        employee_id,
        type_id,
        year,
    )
    return dict(row) if row else {}


async def _open(pool: asyncpg.Pool, year: int, employee_id: int | None = None) -> None:
    async with pool.acquire() as conn:
        await _open_year_balances(conn, year, employee_id)


@pytest.mark.asyncio
async def test_days_left_in_a_year_open_the_next_one(pool: asyncpg.Pool) -> None:
    employee_id = await _employee(pool, "Synthetic Saver")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 5)

    await _open(pool, 2027, employee_id)
    await _open(pool, 2027, employee_id)

    assert await _row(pool, employee_id, annual_id, 2027) == {
        "allocated_days": 12,
        "carried_over": 7,
        "used_days": 0,
        "pending_days": 0,
        "remaining": 19,
    }


@pytest.mark.asyncio
async def test_a_year_that_ended_in_debt_opens_the_next_one_short(pool: asyncpg.Pool) -> None:
    employee_id = await _employee(pool, "Synthetic Borrower")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 13)

    await _open(pool, 2027)

    assert await _row(pool, employee_id, annual_id, 2027) == {
        "allocated_days": 12,
        "carried_over": -1,
        "used_days": 0,
        "pending_days": 0,
        "remaining": 11,
    }


@pytest.mark.asyncio
async def test_a_request_past_the_allowance_is_taken_and_approved_as_debt(
    pool: asyncpg.Pool,
) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic Early")
    reviewer = await _member(pool, "Synthetic Reviewer")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 10)

    request = await service.request_leave(
        {
            "employee_id": employee_id,
            "leave_type_id": annual_id,
            "start_date": date(2026, 12, 21),
            "end_date": date(2026, 12, 24),
            "total_days": 4,
        }
    )
    assert request["status"] == "pending"
    assert (await _row(pool, employee_id, annual_id, 2026))["remaining"] == -2

    approved = await service.approve_leave(request["id"], reviewer)
    assert approved["status"] == "approved"
    assert await _row(pool, employee_id, annual_id, 2026) == {
        "allocated_days": 12,
        "carried_over": 0,
        "used_days": 14,
        "pending_days": 0,
        "remaining": -2,
    }

    await _open(pool, 2027, employee_id)
    assert (await _row(pool, employee_id, annual_id, 2027))["carried_over"] == -2


@pytest.mark.asyncio
async def test_another_tracked_type_still_stops_at_its_allowance(pool: asyncpg.Pool) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic Groom")
    marriage_id = await _leave_type(pool, "marriage", 3)
    await _balance(pool, employee_id, marriage_id, 2026, 3, 0)

    with pytest.raises(ValueError, match="Insufficient leave balance"):
        await service.request_leave(
            {
                "employee_id": employee_id,
                "leave_type_id": marriage_id,
                "start_date": date(2026, 12, 7),
                "end_date": date(2026, 12, 10),
                "total_days": 4,
            }
        )
    assert (await _row(pool, employee_id, marriage_id, 2026))["pending_days"] == 0


@pytest.mark.asyncio
async def test_only_annual_leave_carries_a_remainder(pool: asyncpg.Pool) -> None:
    employee_id = await _employee(pool, "Synthetic Parent")
    paternity_id = await _leave_type(pool, "paternity", 2)
    await _balance(pool, employee_id, paternity_id, 2026, 2, 0)

    await _open(pool, 2027, employee_id)

    assert (await _row(pool, employee_id, paternity_id, 2027))["carried_over"] == 0


@pytest.mark.asyncio
async def test_approving_last_years_leave_re_carries_the_years_already_opened(
    pool: asyncpg.Pool,
) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic Planner")
    reviewer = await _member(pool, "Synthetic Approver")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 5)

    december = await service.request_leave(
        {
            "employee_id": employee_id,
            "leave_type_id": annual_id,
            "start_date": date(2026, 12, 28),
            "end_date": date(2026, 12, 29),
            "total_days": 2,
        }
    )
    # January leave asked for in December opens 2027 while 2026 is still moving.
    await service.request_leave(
        {
            "employee_id": employee_id,
            "leave_type_id": annual_id,
            "start_date": date(2027, 1, 4),
            "end_date": date(2027, 1, 4),
            "total_days": 1,
        }
    )
    await _open(pool, 2028, employee_id)
    assert (await _row(pool, employee_id, annual_id, 2027))["carried_over"] == 7
    assert (await _row(pool, employee_id, annual_id, 2028))["carried_over"] == 19

    await service.approve_leave(december["id"], reviewer)

    assert (await _row(pool, employee_id, annual_id, 2027))["carried_over"] == 5
    assert (await _row(pool, employee_id, annual_id, 2028))["carried_over"] == 17


@pytest.mark.asyncio
async def test_rejecting_last_years_request_leaves_the_carry_as_it_was(
    pool: asyncpg.Pool,
) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic Refused")
    reviewer = await _member(pool, "Synthetic Head")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 5)
    december = await service.request_leave(
        {
            "employee_id": employee_id,
            "leave_type_id": annual_id,
            "start_date": date(2026, 12, 28),
            "end_date": date(2026, 12, 29),
            "total_days": 2,
        }
    )
    await _open(pool, 2027, employee_id)

    await service.reject_leave(december["id"], reviewer, "synthetic reason")

    assert (await _row(pool, employee_id, annual_id, 2027))["carried_over"] == 7


@pytest.mark.asyncio
async def test_only_the_two_debt_checks_are_gone(pool: asyncpg.Pool) -> None:
    employee_id = await _employee(pool, "Synthetic Guard")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 0)

    for column in ("allocated_days", "used_days", "pending_days"):
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute(
                f"UPDATE hr_leave_balances SET {column} = -1 WHERE employee_id = $1",
                employee_id,
            )
    await pool.execute(
        "UPDATE hr_leave_balances SET carried_over = -3, used_days = 20 WHERE employee_id = $1",
        employee_id,
    )


@pytest.mark.asyncio
async def test_the_rollback_restores_both_checks_without_failing_on_debt(
    pool: asyncpg.Pool,
) -> None:
    employee_id = await _employee(pool, "Synthetic Rollback")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 14)
    _, rollback = split_migration_sql(_MIGRATION_388.read_text())
    assert rollback

    await pool.execute(rollback)

    checks = await pool.fetch(
        "SELECT conname, convalidated FROM pg_constraint "
        "WHERE conrelid = 'hr_leave_balances'::regclass "
        "AND conname IN ('ck_hr_leave_bal_nonneg', 'hr_leave_balances_carried_over_check') "
        "ORDER BY conname"
    )
    assert [tuple(row) for row in checks] == [
        ("ck_hr_leave_bal_nonneg", False),
        ("hr_leave_balances_carried_over_check", False),
    ]
    other = await _employee(pool, "Synthetic After Rollback")
    with pytest.raises(asyncpg.CheckViolationError):
        await _balance(pool, other, annual_id, 2026, 12, 13)


@pytest.mark.asyncio
async def test_forward_again_after_a_rollback_drops_both_checks(pool: asyncpg.Pool) -> None:
    forward, rollback = split_migration_sql(_MIGRATION_388.read_text())
    await pool.execute(rollback)

    await pool.execute(forward)

    left = await pool.fetch(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'hr_leave_balances'::regclass "
        "AND conname IN ('ck_hr_leave_bal_nonneg', 'hr_leave_balances_carried_over_check')"
    )
    assert left == []
    employee_id = await _employee(pool, "Synthetic Forward Again")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 14)
    await pool.execute(
        "UPDATE hr_leave_balances SET carried_over = -2 WHERE employee_id = $1", employee_id
    )
    assert (await _row(pool, employee_id, annual_id, 2026))["remaining"] == -4


@pytest.mark.asyncio
async def test_the_leave_lock_is_a_two_key_lock_in_its_own_namespace(pool: asyncpg.Pool) -> None:
    namespace = getattr(hr_service, "HR_LEAVE_LOCK_NS", None)
    assert namespace is not None, "no leave lock namespace"
    employee_id = await _employee(pool, "Synthetic Lock Shape")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 0)

    async with pool.acquire() as holder, pool.acquire() as observer:
        async with holder.transaction():
            await _lock_leave_balances(holder, employee_id)
            await _lock_leave_balances_a_year_opens_from(holder, 2027)
            held = await observer.fetch(
                "SELECT classid::bigint AS ns, objid::bigint AS key, objsubid "
                "FROM pg_locks WHERE locktype = 'advisory' AND pid = $1",
                await holder.fetchval("SELECT pg_backend_pid()"),
            )

    assert [tuple(row) for row in held] == [(namespace, employee_id, 2)]


class _HeldConnection:
    """A connection that runs `reached` right after the first statement containing `marker`."""

    def __init__(
        self, conn: asyncpg.Connection, marker: str, reached: Callable[[], Awaitable[None]]
    ) -> None:
        self._conn = conn
        self._marker = marker
        self._reached = reached
        self._held = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def _after(self, query: str) -> None:
        if not self._held and self._marker in query:
            self._held = True
            await self._reached()

    async def fetchrow(self, query: str, *args: Any) -> Any:
        row = await self._conn.fetchrow(query, *args)
        await self._after(query)
        return row

    async def fetchval(self, query: str, *args: Any) -> Any:
        value = await self._conn.fetchval(query, *args)
        await self._after(query)
        return value


class _HeldAlsoOnFetchAndExecute(_HeldConnection):
    """Also pauses after a fetch or an execute, whichever form the statement under test takes."""

    async def fetch(self, query: str, *args: Any) -> Any:
        rows = await self._conn.fetch(query, *args)
        await self._after(query)
        return rows

    async def execute(self, query: str, *args: Any) -> Any:
        status = await self._conn.execute(query, *args)
        await self._after(query)
        return status


class _HoldingPool:
    """Hands HRService connections that pause at a chosen statement."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        marker: str,
        reached: Callable[[], Awaitable[None]],
        connection_class: type[_HeldConnection] = _HeldConnection,
    ) -> None:
        self._pool = pool
        self._marker = marker
        self._reached = reached
        self._connection_class = connection_class

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[_HeldConnection]:
        async with self._pool.acquire() as conn:
            yield self._connection_class(conn, self._marker, self._reached)


async def _wait_briefly(event: asyncio.Event) -> None:
    # Without the employee lock the other side always arrives; with it, it cannot until this
    # transaction ends, so the wait gives up and lets this side finish first.
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), 1.0)


@pytest.mark.asyncio
async def test_two_requests_at_once_cannot_both_spend_the_same_days(pool: asyncpg.Pool) -> None:
    employee_id = await _employee(pool, "Synthetic Double Click")
    marriage_id = await _leave_type(pool, "marriage", 3)
    await _balance(pool, employee_id, marriage_id, 2026, 3, 0)
    both_read = asyncio.Event()
    readers = 0

    async def read_the_balance() -> None:
        nonlocal readers
        readers += 1
        if readers == 2:
            both_read.set()
        await _wait_briefly(both_read)

    service = HRService(_HoldingPool(pool, "SELECT * FROM hr_leave_balances", read_the_balance))
    request = {
        "employee_id": employee_id,
        "leave_type_id": marriage_id,
        "start_date": date(2026, 12, 14),
        "end_date": date(2026, 12, 16),
        "total_days": 3,
    }

    results = await asyncio.gather(
        service.request_leave(dict(request)),
        service.request_leave(dict(request)),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, ValueError)]
    assert len(refused) == 1
    assert "Insufficient leave balance" in str(refused[0])
    assert (await _row(pool, employee_id, marriage_id, 2026))["pending_days"] == 3
    assert await pool.fetchval("SELECT count(*) FROM hr_leave_requests") == 1


@pytest.mark.asyncio
async def test_an_approval_and_the_first_opening_of_next_year_do_not_cross(
    pool: asyncpg.Pool,
) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic Crossing")
    reviewer = await _member(pool, "Synthetic Crossing Reviewer")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, employee_id, annual_id, 2026, 12, 5)
    december = await service.request_leave(
        {
            "employee_id": employee_id,
            "leave_type_id": annual_id,
            "start_date": date(2026, 12, 28),
            "end_date": date(2026, 12, 29),
            "total_days": 2,
        }
    )
    approval_held = asyncio.Event()
    january_done = asyncio.Event()

    async def hold_the_approval() -> None:
        approval_held.set()
        await _wait_briefly(january_done)

    approver = HRService(_HoldingPool(pool, "SELECT MAX(balance_year)", hold_the_approval))
    reached_the_year_lookup = True

    async def ask_for_january() -> None:
        nonlocal reached_the_year_lookup
        try:
            await asyncio.wait_for(approval_held.wait(), 5.0)
        except asyncio.TimeoutError:
            # A service that never looks up the later years opened never pauses here.
            reached_the_year_lookup = False
            january_done.set()
            return
        try:
            await service.request_leave(
                {
                    "employee_id": employee_id,
                    "leave_type_id": annual_id,
                    "start_date": date(2027, 1, 4),
                    "end_date": date(2027, 1, 4),
                    "total_days": 1,
                }
            )
        finally:
            january_done.set()

    await asyncio.gather(approver.approve_leave(december["id"], reviewer), ask_for_january())

    assert reached_the_year_lookup, "the approval never reached the year lookup"
    assert (await _row(pool, employee_id, annual_id, 2026))["used_days"] == 7
    assert (await _row(pool, employee_id, annual_id, 2027))["carried_over"] == 5


async def _wait_bounded(event: asyncio.Event, seconds: float = 10.0) -> bool:
    """Wait for an event for at most `seconds`; say whether it was set. Never waits unbounded."""
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        return False
    return True


@pytest.mark.asyncio
async def test_a_team_opening_writes_only_for_the_employees_it_locked(pool: asyncpg.Pool) -> None:
    service = HRService(pool)
    locked_employee = await _employee(pool, "Synthetic Already Open")
    late_employee = await _employee(pool, "Synthetic Late Arrival")
    reviewer = await _member(pool, "Synthetic Late Reviewer")
    annual_id = await _leave_type(pool, "annual", 12)
    await _balance(pool, locked_employee, annual_id, 2027, 12, 0)
    await _balance(pool, late_employee, annual_id, 2026, 12, 0)
    summary_locked = asyncio.Event()
    approval_held = asyncio.Event()
    summary_opened = asyncio.Event()
    seen = {"summary": False, "approval": False}

    async def after_the_summary_locks() -> None:
        # The late employee has no 2027 row yet, so the summary has not locked it.
        seen["summary"] = True
        summary_locked.set()
        await _wait_bounded(approval_held)

    async def hold_the_approval() -> None:
        seen["approval"] = True
        approval_held.set()
        await _wait_bounded(summary_opened)

    summarizer = HRService(
        _HoldingPool(
            pool,
            "pg_advisory_xact_lock($2::int, prior.employee_id)",
            after_the_summary_locks,
            _HeldAlsoOnFetchAndExecute,
        )
    )
    approver = HRService(_HoldingPool(pool, "SELECT MAX(balance_year)", hold_the_approval))

    async def summarize() -> None:
        try:
            await summarizer.get_team_leave_summary(2028)
        finally:
            summary_opened.set()

    async def request_then_approve() -> None:
        if not await _wait_bounded(summary_locked):
            return
        january = await service.request_leave(
            {
                "employee_id": late_employee,
                "leave_type_id": annual_id,
                "start_date": date(2027, 1, 11),
                "end_date": date(2027, 1, 12),
                "total_days": 2,
            }
        )
        await approver.approve_leave(january["id"], reviewer)

    await asyncio.gather(summarize(), request_then_approve())

    assert seen == {"summary": True, "approval": True}, "the interleaving was not reached"
    assert (await _row(pool, late_employee, annual_id, 2027))["used_days"] == 2
    # 12 allocated + 12 carried from 2026 - 2 used: what 2028 must carry after the approval.
    row = await _row(pool, late_employee, annual_id, 2028)
    if not row:
        await service.get_leave_balance(late_employee, 2028)
        row = await _row(pool, late_employee, annual_id, 2028)
    assert row["carried_over"] == 22
    assert (await _row(pool, locked_employee, annual_id, 2028))["carried_over"] == 12
