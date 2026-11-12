"""Off-payroll staff (STAFF_CARD.md C12) against a real Postgres, own schema `hroffpay`.

Real DDL: migration_068 (payroll), migration 354 (card profile), migration 357 (manual bonus).
All names are placeholders.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr.hr_card_edit import CardEditBody
from backend.app.services.hr.hr_overview_compose import compose_member_overview
from backend.app.services.hr.hr_service import (
    HRCardCode,
    HRCardError,
    HRService,
    OffPayrollError,
    PayrollRosterChangedError,
    payroll_set_sql,
)
from backend.db.migration_base import split_migration_sql
from backend.migrations.migration_068_hr_payroll import UPGRADE_SQL
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

pytestmark = pytest.mark.integration

SCHEMA = "hroffpay"
MIGRATIONS = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
MONTH, YEAR = 11, 2026


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


@dataclass
class Env:
    svc: HRService
    pool: asyncpg.Pool
    on_id: int  # "Made Contoh", on payroll
    off_id: int  # "Wayan Contoh", card says on_payroll=false
    admin: str


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    admin = await asyncpg.connect(_dsn())
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('hr-off-payroll-test'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        await admin.execute(
            f"SET search_path TO {SCHEMA};"
            "CREATE TABLE team_members (id VARCHAR(36) PRIMARY KEY, full_name TEXT,"
            " email TEXT UNIQUE, role TEXT, department TEXT, active BOOLEAN NOT NULL DEFAULT TRUE);"
            "CREATE TABLE practices (id SERIAL PRIMARY KEY, status TEXT);"
        )
        await admin.execute(UPGRADE_SQL)
        for name in ("354_hr_employees_profile.sql", "357_hr_bonus_manual.sql"):
            sql = (MIGRATIONS / name).read_text(encoding="utf-8")
            await admin.execute(split_migration_sql(sql)[0])
        await admin.execute(
            "INSERT INTO team_members (id, full_name, email, role) VALUES "
            "('tm-admin','Fake Admin','fake.admin@example.com','admin'),"
            "('tm-on','Made Contoh','made.contoh@example.com','staff'),"
            "('tm-off','Wayan Contoh','wayan.contoh@example.com','staff');"
            "INSERT INTO hr_employees (team_member_id, hire_date, base_salary_idr, profile) VALUES "
            "('tm-on','2025-01-01',5000000,'{}'),"
            "('tm-off','2025-01-01',6000000,'{\"unit\":\"Marketing\",\"on_payroll\":false}')"
        )
        on_id = await admin.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-on'")
        off_id = await admin.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-off'")
        pool = await asyncpg.create_pool(
            _dsn(),
            init=init_prod_shaped_connection,
            server_settings={"search_path": SCHEMA},
            statement_cache_size=0,
            min_size=1,
            max_size=4,
        )
        yield Env(HRService(pool), pool, on_id, off_id, "tm-admin")
    finally:
        if pool:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await admin.execute("SELECT pg_advisory_unlock(hashtext('hr-off-payroll-test'))")
        await admin.close()


async def _slips(pool: asyncpg.Pool) -> set[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT employee_id FROM hr_payslips")
    return {r["employee_id"] for r in rows}


async def _set_flag(env: Env, member: str, email: str, value: bool | None) -> None:
    body = (
        CardEditBody.model_validate({"card": {"profile": {"on_payroll": value}}})
        if value is not None
        else CardEditBody.model_validate({"clear": ["on_payroll"]})
    )
    fields, profile, cleared, _ = body.to_write()
    assert await env.svc.edit_card(member, email, fields, profile, cleared) is not None


@pytest.mark.asyncio
async def test_calculate_excludes_the_off_payroll_person(env: Env) -> None:
    result = await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    assert result["employee_count"] == 1
    assert await _slips(env.pool) == {env.on_id}


@pytest.mark.asyncio
async def test_recalculate_deletes_a_stale_slip_in_a_calculated_period(env: Env) -> None:
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", None)  # on payroll again
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    assert await _slips(env.pool) == {env.on_id, env.off_id}
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", False)
    result = await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    assert result["employee_count"] == 1
    assert await _slips(env.pool) == {env.on_id}
    async with env.pool.acquire() as conn:  # no orphan deduction left behind
        orphans = await conn.fetchval(
            "SELECT COUNT(*) FROM hr_deductions d "
            "WHERE NOT EXISTS (SELECT 1 FROM hr_payslips p WHERE p.id = d.payslip_id)"
        )
    assert orphans == 0


@pytest.mark.asyncio
async def test_an_approved_period_keeps_its_slips(env: Env) -> None:
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", None)
    period = (await env.svc.calculate_payroll(MONTH, YEAR, env.admin))["period_id"]
    await env.svc.approve_payroll(period, env.admin)
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", False)
    with pytest.raises(
        asyncpg.RaiseError
    ):  # the period trigger refuses to reopen an approved month
        await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    assert await _slips(env.pool) == {env.on_id, env.off_id}  # approved: nothing deleted


@pytest.mark.asyncio
async def test_the_flag_is_set_and_cleared_through_the_card_edit(env: Env) -> None:
    async with env.pool.acquire() as conn:
        card = await conn.fetchval(
            "SELECT profile->'on_payroll' FROM hr_employees WHERE id=$1", env.on_id
        )
    assert card is None
    await _set_flag(env, "tm-on", "made.contoh@example.com", False)
    assert await env.svc.is_on_payroll(env.on_id) is False
    await _set_flag(env, "tm-on", "made.contoh@example.com", None)
    assert await env.svc.is_on_payroll(env.on_id) is True
    async with env.pool.acquire() as conn:
        has = await conn.fetchval(
            "SELECT profile ? 'on_payroll' FROM hr_employees WHERE id=$1", env.on_id
        )
    assert has is False  # key removed, not stored as null
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", True)
    assert await env.svc.is_on_payroll(env.off_id) is True


@pytest.mark.asyncio
async def test_unit_survives_the_flag(env: Env) -> None:
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", None)
    async with env.pool.acquire() as conn:
        unit = await conn.fetchval(
            "SELECT profile->>'unit' FROM hr_employees WHERE id=$1", env.off_id
        )
    assert unit == "Marketing"


@pytest.mark.asyncio
async def test_a_manual_bonus_for_an_off_payroll_person_is_refused(env: Env) -> None:
    kwargs = {
        "amount_idr": 100_000,
        "reason": "why",
        "practice_type_code": None,
        "awarded_on": date(2026, 11, 10),
        "created_by": "fake.admin@example.com",
    }
    with pytest.raises(OffPayrollError):
        await env.svc.create_manual_bonus(employee_id=env.off_id, **kwargs)
    assert (await env.svc.create_manual_bonus(employee_id=env.on_id, **kwargs)) is not None
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM hr_bonus_ledger") == 1


@pytest.mark.asyncio
async def test_list_employees_carries_the_flag(env: Env) -> None:
    rows = {r["id"]: r["on_payroll"] for r in await env.svc.list_employees()}
    assert rows == {env.on_id: True, env.off_id: False}


@pytest.mark.asyncio
async def test_my_dashboard_omits_payroll_for_off_payroll_and_keeps_it_for_the_rest(
    env: Env,
) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    off = await env.svc.get_my_dashboard(env.off_id)
    assert off["on_payroll"] is False
    assert "latest_payslip" not in off and "month_bonuses" not in off
    assert "leave_balances" in off
    on = await env.svc.get_my_dashboard(env.on_id)
    assert on["on_payroll"] is True and on["latest_payslip"] is not None


@pytest.mark.asyncio
async def test_my_overview_hides_payroll_for_the_person_but_not_for_hr(env: Env) -> None:
    # a stale slip and a bonus exist for the off-payroll person
    async with env.pool.acquire() as conn:
        period = await conn.fetchval(
            "INSERT INTO hr_payroll_periods (payroll_month,payroll_year,period_start,period_end,"
            "status,created_by) VALUES (11,2026,'2026-11-01','2026-11-30','calculated','tm-admin') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO hr_payslips (payroll_period_id, employee_id, base_salary_idr,"
            " bonus_total_idr, deduction_total_idr, net_salary_idr) VALUES ($1,$2,6000000,0,0,6000000)",
            period,
            env.off_id,
        )
    own = await compose_member_overview(env.svc, "tm-off", self_view=True)
    assert own is not None
    assert own.card.on_payroll is False and own.card.base_salary_idr is None
    assert own.payslips == [] and own.bonuses == []
    assert own.card.unit == "Marketing"
    hr_view = await compose_member_overview(env.svc, "tm-off")
    assert hr_view is not None and len(hr_view.payslips) == 1
    assert hr_view.card.base_salary_idr == 6000000 and hr_view.card.on_payroll is False
    on_own = await compose_member_overview(env.svc, "tm-on", self_view=True)
    assert on_own is not None and on_own.card.on_payroll is True
    assert on_own.card.base_salary_idr == 5000000


@pytest.mark.asyncio
async def test_an_inactive_login_with_an_active_card_gets_no_slip_and_loses_a_stale_one(
    env: Env,
) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)  # on-payroll person: slip
    async with env.pool.acquire() as conn:
        await conn.execute("UPDATE team_members SET active = FALSE WHERE id = 'tm-on'")
        assert await conn.fetchval("SELECT is_active FROM hr_employees WHERE id=$1", env.on_id)
    result = await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    assert result["employee_count"] == 0
    assert await _slips(env.pool) == set()  # stale slip deleted, none created


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["client", "monitoring", "partner"])
async def test_a_non_person_role_is_outside_the_payroll_set(env: Env, role: str) -> None:
    async with env.pool.acquire() as conn:
        await conn.execute("UPDATE team_members SET role = $1 WHERE id = 'tm-on'", role)
    assert (await env.svc.calculate_payroll(MONTH, YEAR, env.admin))["employee_count"] == 0


@pytest.mark.asyncio
async def test_an_active_login_with_an_active_card_keeps_its_slip(env: Env) -> None:
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)
    await env.svc.calculate_payroll(MONTH, YEAR, env.admin)  # recalculate: still there, once
    assert await _slips(env.pool) == {env.on_id}


@pytest.mark.asyncio
async def test_a_card_edit_waits_for_a_calculate_that_holds_the_roster(env: Env) -> None:
    """Guilt for the roster race: calculate read the set under FOR SHARE, so the flag flip
    cannot commit between the select and the slip insert."""
    async with env.pool.acquire() as holder, holder.transaction():
        await holder.fetch(
            f"SELECT e.id FROM hr_employees e JOIN team_members tm ON tm.id = e.team_member_id "
            f"WHERE {payroll_set_sql()} FOR SHARE OF e, tm"
        )  # exactly the lock calculate takes, held open
        quick = await asyncpg.create_pool(
            _dsn(),
            init=init_prod_shaped_connection,
            server_settings={"search_path": SCHEMA, "lock_timeout": "300ms"},
            statement_cache_size=0,
            min_size=1,
            max_size=2,
        )
        try:
            with pytest.raises(HRCardError) as err:
                await HRService(quick).edit_card(
                    "tm-on",
                    "made.contoh@example.com",
                    {},
                    {"on_payroll": False},
                    [],
                )
            assert err.value.code is HRCardCode.LOCK_TIMEOUT  # the edit waited, then timed out
        finally:
            await quick.close()
    # once calculate's transaction is over the same edit goes through
    await _set_flag(env, "tm-on", "made.contoh@example.com", False)
    assert await env.svc.is_on_payroll(env.on_id) is False


@pytest.mark.asyncio
async def test_approve_refuses_when_the_roster_changed_after_calculate(env: Env) -> None:
    period = (await env.svc.calculate_payroll(MONTH, YEAR, env.admin))["period_id"]
    await _set_flag(env, "tm-on", "made.contoh@example.com", False)  # now outside the set
    with pytest.raises(PayrollRosterChangedError):
        await env.svc.approve_payroll(period, env.admin)
    # someone joins the set with no slip: also refused
    await _set_flag(env, "tm-on", "made.contoh@example.com", None)
    await _set_flag(env, "tm-off", "wayan.contoh@example.com", None)
    with pytest.raises(PayrollRosterChangedError):
        await env.svc.approve_payroll(period, env.admin)
    period = (await env.svc.calculate_payroll(MONTH, YEAR, env.admin))["period_id"]
    assert (await env.svc.approve_payroll(period, env.admin))["status"] == "approved"


@pytest.mark.asyncio
async def test_approve_with_no_change_is_ok(env: Env) -> None:
    period = (await env.svc.calculate_payroll(MONTH, YEAR, env.admin))["period_id"]
    assert (await env.svc.approve_payroll(period, env.admin))["status"] == "approved"


async def _pending_bonus(env: Env, emp_id: int) -> int:
    async with env.pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, "
            "awarded_at, source, reason) VALUES ($1,'manual',1000,'pending',$2,'manual','why') "
            "RETURNING id",
            emp_id,
            datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_approve_bonus_refuses_an_off_payroll_person_and_keeps_the_rest(env: Env) -> None:
    off_bonus = await _pending_bonus(env, env.off_id)  # a pending bonus that already exists
    on_bonus = await _pending_bonus(env, env.on_id)
    with pytest.raises(OffPayrollError):
        await env.svc.approve_bonus(off_bonus, env.admin)
    assert (await env.svc.approve_bonus(on_bonus, env.admin))["status"] == "approved"
    async with env.pool.acquire() as conn:
        st = await conn.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", off_bonus)
    assert st == "pending"
