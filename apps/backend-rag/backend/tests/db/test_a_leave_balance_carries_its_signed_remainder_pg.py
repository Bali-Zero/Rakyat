"""Annual leave carries its signed remainder into the next year, and a balance may go negative."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.services.hr.hr_service import HRService, _open_year_balances

# The leave tables as migration_068_hr_payroll.py creates them, CHECKs included;
# migration 388 is then applied on top, exactly as it ships.
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
        await created.execute(_MIGRATION_388.read_text())
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
