"""Migration 394, effect 1: the bonuses the paid November salary already carried (rows 645-649).

Same real schema as test_hr_bonus_payroll_locks: migration_068's DDL and triggers (the paid period and
its payslips are immutable), then 357 and 383-393. The migration file is run as written.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.tests.migrations.test_hr_bonus_payroll_locks import (  # noqa: F401
    Env,
    env,
)

pytestmark = pytest.mark.integration

PATH = Path(__file__).resolve().parents[2] / "db/migrations_v2/394_book_november_bonus_receipts.sql"
FORWARD, ROLLBACK = split_migration_sql(PATH.read_text())
# The five measured rows: id, employee, amount. Awarded 2026-11-01 00:30 Bali (the old day-28 roll).
ROWS = [(645, 4, 100000), (646, 4, 100000), (647, 4, 100000), (648, 4, 150000), (649, 3, 200000)]
SLIPS = {4: 450000, 3: 200000}
AWARDED = datetime(2026, 10, 31, 16, 30, tzinfo=timezone.utc)


async def seed(
    pool: asyncpg.Pool,
    *,
    period_status: str = "paid",
    year: int = 2026,
    month: int = 11,
    slips: dict[int, int] | None = None,
    rows: list[tuple[int, int, int]] | None = None,
    extra=None,
) -> None:
    """Employees 3 and 4, period 44, slips, then the rows, then the final period status."""
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO team_members (id, full_name, email, role) VALUES "
            "('tm-3','Fake Three','fake.three@example.invalid','staff'),"
            "('tm-4','Fake Four','fake.four@example.invalid','staff')"
        )
        await c.execute(
            "INSERT INTO hr_employees (id, team_member_id, hire_date, base_salary_idr) VALUES "
            "(3,'tm-3','2025-01-01',5000000),(4,'tm-4','2025-01-01',5000000)"
        )
        await c.execute(
            "INSERT INTO hr_payroll_periods (id, payroll_month, payroll_year, period_start, period_end, status)"
            " VALUES (44, $1, $2, '2026-11-01', '2026-11-30', 'calculated')",
            month,
            year,
        )
        for emp, bonus in (slips or SLIPS).items():
            await c.execute(
                "INSERT INTO hr_payslips (payroll_period_id, employee_id, base_salary_idr, bonus_total_idr,"
                " net_salary_idr) VALUES (44, $1, 5000000, $2::bigint, 5000000 + $2::bigint)",
                emp,
                bonus,
            )
        for rid, emp, amount in rows if rows is not None else ROWS:
            await c.execute(
                "INSERT INTO hr_bonus_ledger (id, employee_id, practice_type_code, amount_idr, status,"
                " awarded_at, source, reason) VALUES ($1,$2,'manual',$3,'approved',$4,'manual','why')",
                rid,
                emp,
                amount,
                AWARDED,
            )
        if extra:
            await extra(c)
        if period_status != "calculated":
            await c.execute(
                "UPDATE hr_payroll_periods SET status = $1 WHERE id = 44", period_status
            )


async def state(pool: asyncpg.Pool) -> list[tuple]:
    return [
        tuple(r)
        for r in await pool.fetch(
            "SELECT id, status, payroll_period_id, amount_idr, employee_id, deferred_from, updated_at"
            " FROM hr_bonus_ledger ORDER BY id"
        )
    ]


async def paid_ids(pool: asyncpg.Pool) -> list[int]:
    return [
        r["id"]
        for r in await pool.fetch(
            "SELECT id FROM hr_bonus_ledger WHERE status='paid' AND payroll_period_id=44 ORDER BY id"
        )
    ]


@pytest.mark.asyncio
async def test_happy_path_records_exactly_the_five_rows_as_paid_on_period_44(env: Env):  # noqa: F811
    await seed(env.pool)
    await env.pool.execute(
        "INSERT INTO hr_bonus_ledger (id, employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason) VALUES (700, 4, 'manual', 100000, 'approved', $1, 'manual', 'why')",
        AWARDED,
    )
    other = await env.pool.fetchrow("SELECT * FROM hr_bonus_ledger WHERE id = 700")
    await env.pool.execute(FORWARD)
    assert await paid_ids(env.pool) == [645, 646, 647, 648, 649]
    assert dict(await env.pool.fetchrow("SELECT * FROM hr_bonus_ledger WHERE id = 700")) == dict(
        other
    )
    done = await env.pool.fetch(
        "SELECT employee_id, amount_idr, notes, approved_at FROM hr_bonus_ledger WHERE id < 650"
    )
    assert sum(r["amount_idr"] for r in done) == 650000
    assert await env.pool.fetchval("SELECT status FROM hr_payroll_periods WHERE id = 44") == "paid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "wrong_amount",
        "wrong_employee",
        "slip_total_mismatch",
        "slip_total_mismatch_other_employee",
        "period_not_paid",
        "period_wrong_month",
        "row_already_reserved",
        "row_already_paid_elsewhere",
        "row_not_approved",
        "row_deferred",
        "row_awarded_in_october",
        "other_row_reserved_to_period_44",
        "missing_row",
        "missing_slip",
    ],
)
async def test_each_failed_guard_changes_nothing(env: Env, case):  # noqa: F811
    kwargs: dict = {}
    after_seed: list[str] = []
    if case == "wrong_amount":
        kwargs["rows"] = [(645, 4, 99999)] + ROWS[1:]
    elif case == "wrong_employee":
        kwargs["rows"] = [(645, 3, 100000)] + ROWS[1:]
    elif case == "slip_total_mismatch":
        kwargs["slips"] = {4: 450001, 3: 200000}
    elif case == "slip_total_mismatch_other_employee":
        kwargs["slips"] = {4: 450000, 3: 100000}
    elif case == "missing_slip":
        kwargs["slips"] = {4: 450000}
    elif case == "period_not_paid":
        kwargs["period_status"] = "approved"
    elif case == "period_wrong_month":
        kwargs["month"] = 10
    elif case == "missing_row":
        kwargs["rows"] = ROWS[:4]
    elif case == "row_already_reserved":
        after_seed.append("UPDATE hr_bonus_ledger SET payroll_period_id = 44 WHERE id = 646")
    elif case == "row_already_paid_elsewhere":
        after_seed.append(
            "UPDATE hr_bonus_ledger SET status = 'paid', payroll_period_id = 44 WHERE id = 645"
        )
    elif case == "row_not_approved":
        after_seed.append("UPDATE hr_bonus_ledger SET status = 'pending' WHERE id = 647")
    elif case == "row_deferred":
        after_seed.append(
            "UPDATE hr_bonus_ledger SET deferred_from = DATE '2026-10-01' WHERE id = 648"
        )
    elif case == "row_awarded_in_october":
        after_seed.append(
            "UPDATE hr_bonus_ledger SET awarded_at = TIMESTAMPTZ '2026-10-31 15:30+00' WHERE id = 649"
        )
    elif case == "other_row_reserved_to_period_44":
        # Seeded while the period is still open so that the paid-period trigger lets it in.
        async def extra(c):
            await c.execute(
                "INSERT INTO hr_bonus_ledger (id, employee_id, practice_type_code, amount_idr, status,"
                " awarded_at, source, reason, payroll_period_id) VALUES (700, 4, 'manual', 5000,"
                " 'approved', $1, 'manual', 'why', 44)",
                AWARDED,
            )

        kwargs["extra"] = extra
    await seed(env.pool, **kwargs)
    for sql in after_seed:
        await env.pool.execute(sql)
    snapshot = await state(env.pool)
    await env.pool.execute(FORWARD)  # a NOTICE, never an error
    assert await state(env.pool) == snapshot


@pytest.mark.asyncio
async def test_a_second_run_changes_nothing(env: Env):  # noqa: F811
    await seed(env.pool)
    await env.pool.execute(FORWARD)
    snapshot = await state(env.pool)
    await env.pool.execute(FORWARD)
    assert await state(env.pool) == snapshot
    assert await paid_ids(env.pool) == [645, 646, 647, 648, 649]


@pytest.mark.asyncio
async def test_rollback_restores_the_rows_to_approved_and_unreserved(env: Env):  # noqa: F811
    await seed(env.pool)
    original = [r[:6] for r in await state(env.pool)]
    await env.pool.execute(FORWARD)
    await env.pool.execute(ROLLBACK)
    assert [r[:6] for r in await state(env.pool)] == original
    await env.pool.execute(ROLLBACK)  # already restored: nothing to do
    assert [r[:6] for r in await state(env.pool)] == original
    await env.pool.execute(FORWARD)  # and forward corrects them again
    assert await paid_ids(env.pool) == [645, 646, 647, 648, 649]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved_on",
    [
        "UPDATE hr_bonus_ledger SET status = 'reversed' WHERE id = 646",
        "UPDATE hr_bonus_ledger SET payroll_period_id = 45 WHERE id = 647",
        "DELETE FROM hr_bonus_ledger WHERE id = 649",
    ],
)
async def test_rollback_refuses_when_a_row_changed_since(env: Env, moved_on):  # noqa: F811
    await seed(env.pool)
    await env.pool.execute(FORWARD)
    await env.pool.execute(
        "INSERT INTO hr_payroll_periods (id, payroll_month, payroll_year, period_start, period_end)"
        " VALUES (45, 12, 2026, '2026-12-01', '2026-12-31')"
    )
    await env.pool.execute(moved_on)
    snapshot = await state(env.pool)
    with pytest.raises(asyncpg.RaiseError, match="Preserve bonus rows 645-649"):
        await env.pool.execute(ROLLBACK)
    assert await state(env.pool) == snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["row", "allocation", "month"])
async def test_it_never_waits_and_corrects_after_release(env: Env, held):  # noqa: F811
    await seed(env.pool)
    async with env.pool.acquire() as holder, env.pool.acquire() as migrator:
        tx = holder.transaction()
        await tx.start()
        if held == "row":
            await holder.execute("SELECT 1 FROM hr_bonus_ledger WHERE id = 647 FOR UPDATE")
        elif held == "allocation":  # approve_bonus and friends hold it shared
            await holder.execute(
                "SELECT pg_advisory_xact_lock_shared(hashtext('hr_bonus_payout_allocation'))"
            )
        else:  # the November salary month, held by whoever fixes its money
            await holder.execute(
                "SELECT pg_advisory_xact_lock(hashtext('hr_payroll_month:2026-11'))"
            )
        started = time.monotonic()
        with pytest.raises(asyncpg.LockNotAvailableError) as refused:
            await asyncio.wait_for(migrator.execute(FORWARD), 5)
        assert not isinstance(refused.value, asyncpg.DeadlockDetectedError)
        assert time.monotonic() - started < 1
        assert await paid_ids(env.pool) == []
        await tx.commit()
        await migrator.execute(FORWARD)
        assert await paid_ids(env.pool) == [645, 646, 647, 648, 649]


@pytest.mark.asyncio
async def test_an_approve_bonus_in_flight_is_not_waited_for(env: Env):  # noqa: F811
    # The writers' own order: allocation lock shared, then the row. A writer holding a row of the
    # five makes the migration refuse at once (55P03), never wait behind it.
    await seed(env.pool)
    async with env.pool.acquire() as writer, env.pool.acquire() as migrator:
        tx = writer.transaction()
        await tx.start()
        await writer.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext('hr_bonus_payout_allocation'))"
        )
        await writer.execute("SELECT 1 FROM hr_bonus_ledger WHERE id = 645 FOR UPDATE")
        with pytest.raises(asyncpg.LockNotAvailableError):
            await asyncio.wait_for(migrator.execute(FORWARD), 5)
        await tx.rollback()


@pytest.mark.asyncio
async def test_it_runs_clean_on_a_schema_with_no_payroll_tables(env: Env):  # noqa: F811
    async with env.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("CREATE SCHEMA empty_394b; SET LOCAL search_path TO empty_394b")
        await conn.execute(FORWARD)
        await conn.execute(ROLLBACK)
        await tx.rollback()


@pytest.mark.asyncio
async def test_it_runs_clean_where_the_payroll_tables_exist_but_hold_none_of_the_rows(env: Env):  # noqa: F811
    # The CI chain builds the tables empty: no period 44, no rows 645-649, nothing to correct.
    await env.pool.execute(FORWARD)
    await env.pool.execute(ROLLBACK)
    assert await state(env.pool) == []
