"""Annual leave balances open from the prior year's tracked balances."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.services.hr.hr_service import HRService, _open_year_balances

# The three leave tables mirror migration_068_hr_payroll.py (types, CHECKs,
# the unique key the rollover conflicts on); team_members and hr_employees
# keep only the columns the HR queries read.
_SCHEMA_SQL = """
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

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="isolated PG required")


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    admin = await asyncpg.connect(TEST_DSN)
    schema = "leaveyear_" + uuid4().hex
    created = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            TEST_DSN, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        await created.execute(_SCHEMA_SQL)
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _employee(pool: asyncpg.Pool, name: str, email: str) -> int:
    member_id = uuid4().hex[:36]
    await pool.execute(
        "INSERT INTO team_members (id, full_name, email) VALUES ($1, $2, $3)",
        member_id,
        name,
        email,
    )
    return await pool.fetchval(
        "INSERT INTO hr_employees (team_member_id) VALUES ($1) RETURNING id", member_id
    )


@pytest.mark.asyncio
async def test_request_opens_year_with_the_carry_and_reserves_balance(pool: asyncpg.Pool) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic One", "synthetic.one@example.test")
    annual_id = await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) VALUES ('annual', 'Annual', 12) RETURNING id"
    )
    await pool.execute(
        "INSERT INTO hr_leave_balances "
        "(employee_id, leave_type_id, balance_year, allocated_days, used_days) "
        "VALUES ($1, $2, 2026, 12, 5)",
        employee_id,
        annual_id,
    )
    accepted = {
        "employee_id": employee_id,
        "leave_type_id": annual_id,
        "start_date": date(2027, 1, 3),
        "end_date": date(2027, 1, 5),
        "total_days": 3,
    }
    result = await service.request_leave(accepted)
    balance = await pool.fetchrow(
        "SELECT allocated_days, carried_over, used_days, pending_days "
        "FROM hr_leave_balances WHERE employee_id = $1 AND leave_type_id = $2 "
        "AND balance_year = 2027",
        employee_id,
        annual_id,
    )
    assert result["status"] == "pending"
    assert dict(balance) == {
        "allocated_days": 12,
        "carried_over": 7,
        "used_days": 0,
        "pending_days": 3,
    }


async def _annual_2026(pool: asyncpg.Pool) -> int:
    employee_id = await _employee(pool, "Synthetic Reader", "reader@example.test")
    annual_id = await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) "
        "VALUES ('annual', 'Annual', 12) RETURNING id"
    )
    await pool.execute(
        "INSERT INTO hr_leave_balances "
        "(employee_id, leave_type_id, balance_year, allocated_days, used_days) "
        "VALUES ($1, $2, 2026, 12, 5)",
        employee_id,
        annual_id,
    )
    return employee_id


@pytest.mark.asyncio
async def test_my_balance_opens_the_year_it_reads(pool: asyncpg.Pool) -> None:
    employee_id = await _annual_2026(pool)

    balances = await HRService(pool).get_leave_balance(employee_id, 2027)

    assert [(row["balance_year"], row["allocated_days"], row["used_days"]) for row in balances] == [
        (2027, 12, 0)
    ]


@pytest.mark.asyncio
async def test_the_team_summary_opens_the_year_it_reads(pool: asyncpg.Pool) -> None:
    employee_id = await _annual_2026(pool)

    summary = await HRService(pool).get_team_leave_summary(2027)

    assert [(row["employee_id"], row["allocated_days"], row["used_days"]) for row in summary] == [
        (employee_id, 12, 0)
    ]


@pytest.mark.asyncio
async def test_open_year_preserves_existing_rows_and_only_copies_selected_employee(
    pool: asyncpg.Pool,
) -> None:
    first_employee = await _employee(pool, "Synthetic First", "first@example.test")
    other_employee = await _employee(pool, "Synthetic Other", "other@example.test")
    annual_id = await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) VALUES ('annual', 'Annual', 12) RETURNING id"
    )
    untracked_id = await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) VALUES ('untracked', 'Untracked', 0) RETURNING id"
    )
    await pool.executemany(
        "INSERT INTO hr_leave_balances "
        "(employee_id, leave_type_id, balance_year, allocated_days, used_days, pending_days) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        [
            (first_employee, annual_id, 2026, 12, 5, 2),
            (other_employee, annual_id, 2026, 15, 4, 1),
            (first_employee, annual_id, 2027, 20, 7, 4),
        ],
    )

    async with pool.acquire() as conn:
        await _open_year_balances(conn, 2027, first_employee)
        await _open_year_balances(conn, 2027, first_employee)

    rows = await pool.fetch(
        "SELECT employee_id, leave_type_id, allocated_days, carried_over, used_days, "
        "pending_days FROM hr_leave_balances WHERE balance_year = 2027 "
        "ORDER BY employee_id, leave_type_id"
    )
    # The row opened earlier keeps its own days and is re-carried from 2026: 12 - 5.
    assert [dict(row) for row in rows] == [
        {
            "employee_id": first_employee,
            "leave_type_id": annual_id,
            "allocated_days": 20,
            "carried_over": 7,
            "used_days": 7,
            "pending_days": 4,
        }
    ]
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM hr_leave_balances WHERE employee_id = $1 "
            "AND leave_type_id = $2 AND balance_year = 2027",
            first_employee,
            untracked_id,
        )
        == 0
    )
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM hr_leave_balances WHERE employee_id = $1 "
            "AND leave_type_id = $2 AND balance_year = 2027",
            other_employee,
            annual_id,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_a_type_never_tracked_stays_untracked_in_the_new_year(
    pool: asyncpg.Pool,
) -> None:
    service = HRService(pool)
    employee_id = await _employee(pool, "Synthetic Sick", "sick@example.test")
    sick_id = await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) "
        "VALUES ('sick', 'Sick', 0) RETURNING id"
    )

    result = await service.request_leave(
        {
            "employee_id": employee_id,
            "leave_type_id": sick_id,
            "start_date": date(2027, 2, 1),
            "end_date": date(2027, 2, 2),
            "total_days": 2,
        }
    )

    assert result["status"] == "pending"
    assert await pool.fetchval("SELECT count(*) FROM hr_leave_balances") == 0


@pytest.mark.asyncio
async def test_only_the_year_just_before_opens_a_year(pool: asyncpg.Pool) -> None:
    employee_id = await _employee(pool, "Synthetic Gap", "gap@example.test")
    annual_id = await pool.fetchval(
        "INSERT INTO hr_leave_types (code, name, default_days) "
        "VALUES ('annual', 'Annual', 12) RETURNING id"
    )
    await pool.execute(
        "INSERT INTO hr_leave_balances "
        "(employee_id, leave_type_id, balance_year, allocated_days) "
        "VALUES ($1, $2, 2025, 12)",
        employee_id,
        annual_id,
    )

    async with pool.acquire() as conn:
        await _open_year_balances(conn, 2027)

    assert (
        await pool.fetchval("SELECT count(*) FROM hr_leave_balances WHERE balance_year = 2027") == 0
    )
