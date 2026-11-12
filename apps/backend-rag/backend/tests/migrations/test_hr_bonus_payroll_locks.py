"""Payroll-month locking of bonuses, against a real Postgres (own schema `hrlock357`).

The schema is the real one: migration_068's DDL and triggers, then migration 357.
Two pools: one normal, one with `lock_timeout = 200ms` to prove two sessions serialise
on the payroll period row instead of racing.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr.hr_service import (
    BonusPeriodLockedError,
    HRService,
    PayrollBonusesChangedError,
    payroll_month_lock_key,
)
from backend.db.migration_base import split_migration_sql
from backend.migrations.migration_068_hr_payroll import UPGRADE_SQL

pytestmark = pytest.mark.integration

SCHEMA = "hrlock357"
MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "357_hr_bonus_manual.sql"
MONTH, YEAR = 11, 2026
AWARDED = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


@dataclass
class Env:
    svc: HRService
    quick: HRService  # same database, lock_timeout 200ms
    pool: asyncpg.Pool
    emp_id: int
    admin: str


async def _pool(timeout: str | None) -> asyncpg.Pool:
    settings = {"search_path": SCHEMA}
    if timeout:
        settings["lock_timeout"] = timeout
    return await asyncpg.create_pool(
        _dsn(), server_settings=settings, statement_cache_size=0, min_size=1, max_size=6
    )


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    admin = await asyncpg.connect(_dsn())
    pools: list[asyncpg.Pool] = []
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('r2-hr-bonus-locks-357'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        await admin.execute(
            f"SET search_path TO {SCHEMA};"
            "CREATE TABLE team_members (id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT);"
            "CREATE TABLE practices (id SERIAL PRIMARY KEY, status TEXT);"
        )
        await admin.execute(UPGRADE_SQL)
        await admin.execute(split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[0])
        await admin.execute(
            "INSERT INTO team_members VALUES ('tm-admin','Fake Admin','fake.admin@example.com'),"
            "('tm-a','Fake A','fake.a@example.com'),('tm-b','Fake B','fake.b@example.com');"
            "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr) "
            "VALUES ('tm-a','2025-01-01',5000000),('tm-b','2025-01-01',5000000)"
        )
        emp_id = await admin.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-a'")
        normal, quick = await _pool(None), await _pool("200ms")
        pools += [normal, quick]
        yield Env(HRService(normal), HRService(quick), normal, emp_id, "tm-admin")
    finally:
        for p in pools:
            await p.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute("SELECT pg_advisory_unlock(hashtext('r2-hr-bonus-locks-357'))")
        await admin.close()


async def _bonus(pool: asyncpg.Pool, emp_id: int, status: str, amount: int = 100_000) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, "
            "awarded_at, source, reason) VALUES ($1,'manual',$2,$3,$4,'manual','why') "
            "RETURNING id",
            emp_id,
            amount,
            status,
            AWARDED,
        )


async def _period_id(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM hr_payroll_periods WHERE payroll_month=$1 AND payroll_year=$2",
            MONTH,
            YEAR,
        )


@pytest.mark.asyncio
async def test_an_approved_month_refuses_bonus_approval_and_manual_insert(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    pending = await _bonus(env.pool, env.emp_id, "pending")
    # still "calculated": approving is allowed here (the payroll drift check catches it later)
    await env.svc.approve_payroll(await _period_id(env.pool), env.admin)

    with pytest.raises(BonusPeriodLockedError):
        await env.svc.approve_bonus(pending, env.admin)
    with pytest.raises(BonusPeriodLockedError):
        await env.svc.create_manual_bonus(
            employee_id=env.emp_id,
            amount_idr=1,
            reason="late extra",
            practice_type_code=None,
            awarded_on=date(YEAR, MONTH, 12),
            created_by="fake.admin@example.com",
        )
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", pending)
            == "pending"
        )
    # a pending bonus in the locked month can still be rejected
    assert (await env.svc.reject_bonus(pending, env.admin))["status"] == "rejected"
    # a month with no period row is never locked
    other = await env.svc.create_manual_bonus(
        employee_id=env.emp_id,
        amount_idr=1,
        reason="other month",
        practice_type_code=None,
        awarded_on=date(YEAR, 12, 3),
        created_by="fake.admin@example.com",
    )
    assert other and other["status"] == "pending"


@pytest.mark.asyncio
async def test_payroll_approval_refuses_changed_bonuses_until_recalculated(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    pending = await _bonus(env.pool, env.emp_id, "pending")
    await env.svc.approve_bonus(pending, env.admin)  # month still open: allowed

    with pytest.raises(PayrollBonusesChangedError, match="recalculate"):
        await env.svc.approve_payroll(period_id, env.admin)
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM hr_payroll_periods WHERE id=$1", period_id)
            == "calculated"
        )

    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    row = await env.svc.approve_payroll(period_id, env.admin)
    assert row["status"] == "approved"
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT bonus_total_idr FROM hr_payslips WHERE employee_id=$1 AND payroll_period_id=$2",
                env.emp_id,
                period_id,
            )
            == 100_000
        )


@pytest.mark.asyncio
async def test_a_bonus_for_someone_without_a_payslip_is_a_mismatch(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    async with env.pool.acquire() as conn:
        await conn.execute("DELETE FROM hr_deductions")
        await conn.execute("DELETE FROM hr_payslips WHERE employee_id=$1", env.emp_id)
    await _bonus(env.pool, env.emp_id, "approved")
    with pytest.raises(PayrollBonusesChangedError):
        await env.svc.approve_payroll(period_id, env.admin)


def _manual(env: Env, day: int = 12) -> dict:
    return {
        "employee_id": env.emp_id,
        "amount_idr": 1,
        "reason": "blocked insert",
        "practice_type_code": None,
        "awarded_on": date(YEAR, MONTH, day),
        "created_by": "fake.admin@example.com",
    }


class _Hold:
    """An open transaction holding the month lock, as an in-flight request would."""

    def __init__(self, pool: asyncpg.Pool, *, exclusive: bool, period_row: int | None = None):
        self.pool, self.exclusive, self.period_row = pool, exclusive, period_row

    async def __aenter__(self) -> None:
        self.conn = await self.pool.acquire()
        self.tx = self.conn.transaction()
        await self.tx.start()
        fn = "pg_advisory_xact_lock" if self.exclusive else "pg_advisory_xact_lock_shared"
        # the key is built by the production function, so the test locks what the code locks
        await self.conn.execute(f"SELECT {fn}(hashtext($1))", payroll_month_lock_key(MONTH, YEAR))
        if self.period_row is not None:
            await self.conn.fetchrow(
                "SELECT * FROM hr_payroll_periods WHERE id=$1 FOR UPDATE", self.period_row
            )

    async def __aexit__(self, *exc: object) -> None:
        await self.tx.rollback()
        await self.pool.release(self.conn)


def test_the_lock_key_is_per_month() -> None:
    assert payroll_month_lock_key(3, 2026) == "hr_payroll_month:2026-03"
    assert payroll_month_lock_key(3, 2026) != payroll_month_lock_key(3, 2027)


@pytest.mark.asyncio
async def test_with_no_period_row_an_in_flight_bonus_blocks_calculate(env: Env) -> None:
    assert await _period_id(env.pool) is None
    async with _Hold(env.pool, exclusive=False):  # an approval/insert mid-transaction
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.calculate_payroll(MONTH, YEAR, env.admin)
    assert await _period_id(env.pool) is None  # nothing half-created
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)  # free again


@pytest.mark.asyncio
async def test_an_in_flight_bonus_blocks_payroll_approval_on_an_existing_period(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    async with _Hold(env.pool, exclusive=False):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.approve_payroll(period_id, env.admin)
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.calculate_payroll(MONTH, YEAR, env.admin)
    assert (await env.svc.approve_payroll(period_id, env.admin))["status"] == "approved"


@pytest.mark.asyncio
async def test_payroll_work_blocks_bonus_insert_and_approval_with_no_period_row(env: Env) -> None:
    pending = await _bonus(env.pool, env.emp_id, "pending")
    assert await _period_id(env.pool) is None
    async with _Hold(env.pool, exclusive=True):  # calculate/approve mid-transaction
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.approve_bonus(pending, env.admin)
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.create_manual_bonus(**_manual(env))
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", pending)
            == "pending"
        )
    assert (await env.svc.approve_bonus(pending, env.admin))["status"] == "approved"


@pytest.mark.asyncio
async def test_payroll_approval_holding_the_period_row_blocks_bonuses(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    pending = await _bonus(env.pool, env.emp_id, "pending")
    # exclusive month lock plus the period row: what approve_payroll holds mid-flight
    async with _Hold(env.pool, exclusive=True, period_row=period_id):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.create_manual_bonus(**_manual(env))
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.approve_bonus(pending, env.admin)


@pytest.mark.asyncio
async def test_no_initial_period_end_to_end_the_payslip_includes_the_bonus(env: Env) -> None:
    pending = await _bonus(env.pool, env.emp_id, "pending")
    await env.svc.approve_bonus(pending, env.admin)  # committed, no period row existed
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    assert (await env.svc.approve_payroll(period_id, env.admin))["status"] == "approved"
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT bonus_total_idr FROM hr_payslips WHERE employee_id=$1 AND payroll_period_id=$2",
                env.emp_id,
                period_id,
            )
            == 100_000
        )
    assert (await env.svc.mark_payroll_paid(period_id))["status"] == "paid"


@pytest.mark.asyncio
async def test_two_shared_holders_do_not_block_each_other(env: Env) -> None:
    first = await _bonus(env.pool, env.emp_id, "pending")
    second = await _bonus(env.pool, env.emp_id, "pending")
    async with _Hold(env.pool, exclusive=False):  # a third in-flight shared holder
        results = await asyncio.gather(
            env.quick.approve_bonus(first, env.admin),
            env.quick.approve_bonus(second, env.admin),
            env.quick.create_manual_bonus(**_manual(env)),
        )
    assert [r["status"] for r in results] == ["approved", "approved", "pending"]


@pytest.mark.asyncio
async def test_rate_upsert_without_effective_from_stores_today(env: Env) -> None:
    from backend.app.services.hr.hr_service import business_today

    async with env.pool.acquire() as conn:
        await conn.execute("DELETE FROM hr_bonus_rates WHERE practice_type_code='tax_fake'")
    rate = await env.svc.upsert_bonus_rate(
        {"practice_type_code": "tax_fake", "amount_idr": 5, "effective_from": None}
    )
    assert rate["effective_from"] == business_today()
    kept = await env.svc.upsert_bonus_rate(
        {
            "practice_type_code": "tax_fake",
            "amount_idr": 6,
            "effective_from": date(2026, 3, 1),
            "effective_to": date(2026, 12, 31),
        }
    )
    assert (kept["effective_from"], kept["effective_to"]) == (date(2026, 3, 1), date(2026, 12, 31))
