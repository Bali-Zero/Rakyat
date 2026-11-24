"""The member's personal dashboard shows a payslip only once its payroll period is approved or paid.

`get_my_dashboard` (GET /api/hr/my-dashboard) returns the member's `latest_payslip`. A period in `draft`
or `calculated` can still be recalculated by the accountant, so its payslip is a provisional figure:
like the My HR overview, the dashboard returns the latest FINAL payslip, or None. Real Postgres, own
schema `hrdashvis`; real DDL from migration_068 (payroll), migration 354 and 357. All names and
amounts are placeholders.
"""

from __future__ import annotations

import calendar
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from backend.app.services.hr.hr_payslip_status import PAYSLIP_VISIBLE_STATUSES
from backend.app.services.hr.hr_service import HRService
from backend.db.migration_base import split_migration_sql
from backend.migrations.migration_068_hr_payroll import UPGRADE_SQL
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection
from backend.tests.migrations.hr_bonus_fixture import install_delivery_payroll_migrations

pytestmark = pytest.mark.integration

SCHEMA = "hrdashvis"
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
YEAR = 2026


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


@dataclass
class Env:
    svc: HRService
    pool: asyncpg.Pool
    member_id: int  # "Made Contoh", the member whose dashboard is read
    other_id: int  # "Wayan Contoh", another member on payroll


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    admin = await asyncpg.connect(_dsn())
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('hr-my-dashboard-visibility'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        await admin.execute(
            f"SET search_path TO {SCHEMA};"
            "CREATE TABLE team_members (id VARCHAR(36) PRIMARY KEY, full_name TEXT,"
            " email TEXT UNIQUE, role TEXT, department TEXT, active BOOLEAN NOT NULL DEFAULT TRUE,"
            " avatar TEXT);"
            "CREATE TABLE practices (id SERIAL PRIMARY KEY, status TEXT);"
        )
        await admin.execute(UPGRADE_SQL)
        for name in ("354_hr_employees_profile.sql", "357_hr_bonus_manual.sql"):
            sql = (MIGRATIONS / name).read_text(encoding="utf-8")
            await admin.execute(split_migration_sql(sql)[0])
        await install_delivery_payroll_migrations(admin)
        await admin.execute(
            "INSERT INTO team_members (id, full_name, email, role) VALUES "
            "('tm-admin','Fake Admin','fake.admin@example.com','admin'),"
            "('tm-member','Made Contoh','made.contoh@example.com','staff'),"
            "('tm-other','Wayan Contoh','wayan.contoh@example.com','staff');"
            "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, profile) VALUES "
            "('tm-member','2025-01-01',5000000,'{}'),"
            "('tm-other','2025-01-01',6000000,'{}')"
        )
        member_id = await admin.fetchval(
            "SELECT id FROM hr_employees WHERE team_member_id='tm-member'"
        )
        other_id = await admin.fetchval(
            "SELECT id FROM hr_employees WHERE team_member_id='tm-other'"
        )
        pool = await asyncpg.create_pool(
            _dsn(),
            init=init_prod_shaped_connection,
            server_settings={"search_path": SCHEMA},
            statement_cache_size=0,
            min_size=1,
            max_size=4,
        )
        yield Env(HRService(pool), pool, member_id, other_id)
    finally:
        if pool:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute("SELECT pg_advisory_unlock(hashtext('hr-my-dashboard-visibility'))")
        await admin.close()


async def _give_payslip(env: Env, employee_id: int, month: int, status: str, net: int = 1) -> int:
    """A payslip of `employee_id` in the 2026 period of `month`, which ends up in `status`.

    The payslip goes in while the period is still a draft (the schema refuses to touch the payslips
    of an approved or paid period); the status is set afterwards, as the payroll flow does.
    """
    async with env.pool.acquire() as conn:
        period = await conn.fetchval(
            "INSERT INTO hr_payroll_periods (payroll_month, payroll_year, period_start, period_end,"
            " status, created_by) VALUES ($1, $2, $3, $4, 'draft', 'tm-admin')"
            " ON CONFLICT (payroll_month, payroll_year) DO UPDATE SET status = 'draft'"
            " RETURNING id",
            month,
            YEAR,
            date(YEAR, month, 1),
            date(YEAR, month, calendar.monthrange(YEAR, month)[1]),
        )
        await conn.execute(
            "INSERT INTO hr_payslips (payroll_period_id, employee_id, base_salary_idr,"
            " bonus_total_idr, deduction_total_idr, net_salary_idr) VALUES ($1,$2,$3,0,0,$3)",
            period,
            employee_id,
            net,
        )
        await conn.execute(
            "UPDATE hr_payroll_periods SET status = $2 WHERE id = $1", period, status
        )
    return int(period)


def test_the_visible_statuses_are_the_final_ones() -> None:
    assert set(PAYSLIP_VISIBLE_STATUSES) == {"approved", "paid"}


@pytest.mark.asyncio
async def test_the_dashboard_skips_a_newer_draft_for_the_latest_approved_payslip(env: Env) -> None:
    approved = await _give_payslip(env, env.member_id, 9, "approved", net=111)
    await _give_payslip(env, env.member_id, 10, "draft", net=222)
    await _give_payslip(env, env.member_id, 11, "calculated", net=333)
    dash = await env.svc.get_my_dashboard(env.member_id)
    assert dash["on_payroll"] is True
    assert dash["latest_payslip"] is not None
    assert dash["latest_payslip"]["payroll_period_id"] == approved
    assert dash["latest_payslip"]["payroll_month"] == 9
    assert dash["latest_payslip"]["net_salary_idr"] == 111


@pytest.mark.asyncio
async def test_a_member_with_only_unapproved_periods_gets_no_latest_payslip(env: Env) -> None:
    await _give_payslip(env, env.member_id, 10, "draft")
    await _give_payslip(env, env.member_id, 11, "calculated")
    dash = await env.svc.get_my_dashboard(env.member_id)
    assert dash["on_payroll"] is True  # still a payroll member: leave and bonuses are unchanged
    assert dash["latest_payslip"] is None
    assert "leave_balances" in dash and "month_bonuses" in dash


@pytest.mark.asyncio
async def test_a_paid_payslip_is_shown_and_a_newer_approved_one_wins_over_an_older_paid_one(
    env: Env,
) -> None:
    paid = await _give_payslip(env, env.member_id, 8, "paid", net=88)
    assert (await env.svc.get_my_dashboard(env.member_id))["latest_payslip"][
        "payroll_period_id"
    ] == paid
    approved = await _give_payslip(env, env.member_id, 9, "approved", net=99)
    await _give_payslip(env, env.member_id, 10, "calculated", net=100)
    latest = (await env.svc.get_my_dashboard(env.member_id))["latest_payslip"]
    assert latest["payroll_period_id"] == approved
    assert latest["net_salary_idr"] == 99


@pytest.mark.asyncio
async def test_another_members_final_payslip_does_not_fill_in_for_a_provisional_one(
    env: Env,
) -> None:
    await _give_payslip(env, env.other_id, 9, "approved", net=999)
    await _give_payslip(env, env.member_id, 10, "draft", net=1)
    assert (await env.svc.get_my_dashboard(env.member_id))["latest_payslip"] is None
    other = (await env.svc.get_my_dashboard(env.other_id))["latest_payslip"]
    assert other is not None and other["net_salary_idr"] == 999
