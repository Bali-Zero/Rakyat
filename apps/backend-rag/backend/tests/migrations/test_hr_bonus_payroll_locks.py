"""Payroll-month locking of bonuses, against a real Postgres (own schema `hrlock357`).

The schema is the real one: migration_068's DDL and triggers, then migration 357.
Two pools: one normal, one with `lock_timeout = 200ms` to prove two sessions serialise
on the payroll period row instead of racing.
"""

from __future__ import annotations

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
    _lock_payroll_month,
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


@pytest.mark.asyncio
async def test_payroll_approval_waits_for_a_session_holding_the_month(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    async with env.pool.acquire() as a:
        tx = a.transaction()
        await tx.start()
        try:
            await _lock_payroll_month(a, MONTH, YEAR)  # tx A holds FOR SHARE
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await env.quick.approve_payroll(period_id, env.admin)
        finally:
            await tx.rollback()
    assert (await env.svc.approve_payroll(period_id, env.admin))["status"] == "approved"


@pytest.mark.asyncio
async def test_bonus_insert_and_approval_wait_for_payroll_approval(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    period_id = await _period_id(env.pool)
    pending = await _bonus(env.pool, env.emp_id, "pending")
    async with env.pool.acquire() as a:
        tx = a.transaction()
        await tx.start()
        try:
            await a.fetchrow("SELECT * FROM hr_payroll_periods WHERE id=$1 FOR UPDATE", period_id)
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await env.quick.create_manual_bonus(
                    employee_id=env.emp_id,
                    amount_idr=1,
                    reason="blocked insert",
                    practice_type_code=None,
                    awarded_on=date(YEAR, MONTH, 12),
                    created_by="fake.admin@example.com",
                )
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await env.quick.approve_bonus(pending, env.admin)
        finally:
            await tx.rollback()
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", pending)
            == "pending"
        )
