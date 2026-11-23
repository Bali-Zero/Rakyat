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
import pytest_asyncio

from backend.app.services.hr.hr_service import (
    BonusPeriodLockedError,
    HRService,
    PayrollBonusesChangedError,
    payroll_month_lock_key,
)
from backend.db.migration_base import split_migration_sql
from backend.migrations.migration_068_hr_payroll import UPGRADE_SQL
from backend.tests.migrations.hr_bonus_fixture import install_delivery_payroll_migrations

pytestmark = pytest.mark.integration

SCHEMA = "hrlock357"
MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "357_hr_bonus_manual.sql"
MIGRATION_354 = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "354_hr_employees_profile.sql"
)
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


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    admin = await asyncpg.connect(_dsn())
    pools: list[asyncpg.Pool] = []
    try:
        await admin.execute("SELECT pg_advisory_lock(hashtext('r2-hr-bonus-locks-357'))")
        await admin.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA};")
        await admin.execute(
            f"SET search_path TO {SCHEMA};"
            "CREATE TABLE team_members (id VARCHAR(36) PRIMARY KEY, full_name TEXT, email TEXT,"
            " role TEXT, active BOOLEAN NOT NULL DEFAULT TRUE, avatar TEXT);"
            "CREATE TABLE practices (id SERIAL PRIMARY KEY, status TEXT);"
        )
        await admin.execute(UPGRADE_SQL)
        await admin.execute(split_migration_sql(MIGRATION_354.read_text(encoding="utf-8"))[0])
        await admin.execute(split_migration_sql(MIGRATION.read_text(encoding="utf-8"))[0])
        await install_delivery_payroll_migrations(admin)
        await admin.execute(
            "INSERT INTO team_members (id, full_name, email, role) VALUES "
            "('tm-admin','Fake Admin','fake.admin@example.com','admin'),"
            "('tm-a','Fake A','fake.a@example.com','staff'),"
            "('tm-b','Fake B','fake.b@example.com','staff');"
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


async def _open_followup(pool, emp_id, started_on, code="visa_demo", slot="itk"):
    async with pool.acquire() as conn:
        async with conn.transaction():
            pid = await conn.fetchval(
                "INSERT INTO practices(status) VALUES('on_process') RETURNING id"
            )
            await conn.execute(
                """INSERT INTO hr_delivery_bonus_events
                   (practice_id,employee_id,service_code,event_kind,earned_at,awarded_at)
                   VALUES($1,$2,$3,'electronic_visa_issued',now(),now())""",
                pid,
                emp_id,
                code,
            )
            await conn.execute(
                """INSERT INTO practice_visa_followups
                     (practice_id,service_code,slot_key,started_on,due_on,allow_postpone)
                   VALUES($1,$2,$3,$4,$4::date+30,true)""",
                pid,
                code,
                slot,
                started_on,
            )
    return pid


async def _award(pool, emp_id, month, amount, status="approved"):
    return await pool.fetchval(
        """INSERT INTO hr_bonus_ledger(employee_id,practice_type_code,amount_idr,status,source,reason,awarded_at)
           VALUES ($1,'manual',$2,$3,'manual','fixture',$4) RETURNING id""",
        emp_id,
        amount,
        status,
        datetime(2025, month, 5, 10, tzinfo=timezone.utc),
    )


async def _run_month(env, month):
    result = await env.svc.calculate_payroll(month, 2025, env.admin)
    await env.svc.approve_payroll(result["period_id"], env.admin)
    await env.svc.mark_payroll_paid(result["period_id"])
    return {r["employee_name"]: r for r in result["payslips"]}


@pytest.mark.asyncio
async def test_one_overdue_case_holds_one_month_and_carries_each_bonus_once(env, monkeypatch):
    from datetime import date

    from backend.app.services.hr import hr_service

    consultant = env.emp_id
    colleague = await env.pool.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-b'")
    pid = await _open_followup(env.pool, consultant, date(2025, 1, 1))
    await _award(env.pool, consultant, 1, 100_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 1, 31))
    assert (await _run_month(env, 1))["Fake A"]["bonus_total"] == 100_000
    assert not await env.pool.fetchval("SELECT count(*) FROM hr_visa_bonus_hold_periods")
    feb = await _award(env.pool, consultant, 2, 155_000)
    feb_pending = await _award(env.pool, consultant, 2, 45_000, "pending")
    await _award(env.pool, colleague, 2, 75_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 2, 28))
    february = await _run_month(env, 2)
    assert february["Fake A"]["base_salary"] == 5_000_000 and february["Fake A"]["bonus_total"] == 0
    assert february["Fake B"]["bonus_total"] == 75_000
    assert (
        await env.pool.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", feb) == "approved"
    )
    assert await env.pool.fetchval("SELECT count(*) FROM hr_bonus_deferral_causes") == 2
    await env.svc.approve_bonus(feb_pending, env.admin)
    # Even confirmed receipt at day 60 leaves February's deferred payment outstanding.
    await env.pool.execute(
        """UPDATE practice_visa_followups SET email_state='sent', email_sent_at=now(),
               email_receipt_confirmed_at=now(),email_receipt_confirmed_by=$2,email_receipt_reference=$3
           WHERE practice_id=$1""",
        pid,
        env.admin,
        "fixture-mail-receipt",
    )
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_visa_bonus_obligations WHERE practice_id=$1", pid
        )
        == 1
    )
    await _award(env.pool, consultant, 3, 95_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 3, 31))
    assert (await _run_month(env, 3))["Fake A"]["bonus_total"] == 95_000
    assert (
        await env.pool.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", feb) == "approved"
    )
    await env.pool.execute(
        "UPDATE practice_visa_followups SET fulfilled_at=now() WHERE practice_id=$1", pid
    )
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 4, 30))
    assert (await _run_month(env, 4))["Fake A"]["bonus_total"] == 200_000
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE id=ANY($1::bigint[]) AND status='paid'",
            [feb, feb_pending],
        )
        == 2
    )
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 5, 31))
    assert (await _run_month(env, 5))["Fake A"]["bonus_total"] == 0


@pytest.mark.asyncio
async def test_overlapping_holds_release_only_when_all_contributing_cases_clear(env, monkeypatch):
    from datetime import date

    from backend.app.services.hr import hr_service

    first = await _open_followup(env.pool, env.emp_id, date(2025, 1, 6))
    second = await _open_followup(env.pool, env.emp_id, date(2025, 1, 8))
    await _award(env.pool, env.emp_id, 2, 230_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 2, 28))
    assert (await _run_month(env, 2))["Fake A"]["bonus_total"] == 0
    await env.pool.execute(
        "UPDATE practice_visa_followups SET fulfilled_at=now() WHERE practice_id=$1", first
    )
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 3, 31))
    assert (await _run_month(env, 3))["Fake A"]["bonus_total"] == 0
    await env.pool.execute(
        "UPDATE practice_visa_followups SET fulfilled_at=now() WHERE practice_id=$1", second
    )
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 4, 30))
    assert (await _run_month(env, 4))["Fake A"]["bonus_total"] == 230_000


@pytest.mark.asyncio
async def test_due_between_calculation_and_approval_requires_recalculation(env, monkeypatch):
    from datetime import date

    from backend.app.services.hr import hr_service

    await _open_followup(env.pool, env.emp_id, date(2025, 1, 25))
    await _award(env.pool, env.emp_id, 2, 111_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 2, 23))
    period = (await env.svc.calculate_payroll(2, 2025, env.admin))["period_id"]
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 2, 24))
    with pytest.raises(PayrollBonusesChangedError):
        await env.svc.approve_payroll(period, env.admin)
    assert (await _run_month(env, 2))["Fake A"]["bonus_total"] == 0


@pytest.mark.asyncio
async def test_postponement_moves_the_deadline_and_empty_held_month_is_consumed(env, monkeypatch):
    from datetime import date

    from backend.app.services.hr import hr_service

    pid = await _open_followup(env.pool, env.emp_id, date(2025, 1, 7))
    await env.pool.execute(
        """UPDATE practice_visa_followups SET due_on=started_on+60,
                  postponed_at=(started_on+31)::timestamp AT TIME ZONE 'Asia/Makassar',
                  postponed_by='test-staff',postponement_note='No arrival'
           WHERE practice_id=$1""",
        pid,
    )
    await _award(env.pool, env.emp_id, 2, 70_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 2, 28))
    assert (await _run_month(env, 2))["Fake A"]["bonus_total"] == 70_000
    assert not await env.pool.fetchval("SELECT count(*) FROM hr_visa_bonus_hold_periods")
    # The extended deadline expires during March when the consultant has no award.
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 3, 31))
    assert (await _run_month(env, 3))["Fake A"]["bonus_total"] == 0
    assert await env.pool.fetchval("SELECT count(*) FROM hr_visa_bonus_hold_periods") == 1
    await _award(env.pool, env.emp_id, 4, 125_000)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 4, 30))
    assert (await _run_month(env, 4))["Fake A"]["bonus_total"] == 125_000


@pytest.mark.asyncio
async def test_archiving_a_fulfilled_itk_cannot_reopen_a_hold_during_payroll_approval(
    env, monkeypatch
):
    """Pause after the real drift read, then attempt a real archive through the document trigger."""
    import asyncio
    from contextlib import asynccontextmanager

    from backend.app.services.hr import hr_service

    monkeypatch.setattr(hr_service, "business_today", lambda: date(2025, 2, 28))
    pid = await _open_followup(env.pool, env.emp_id, date(2025, 1, 1))
    await env.pool.execute("UPDATE practices SET client_id=76 WHERE id=$1", pid)
    doc = await env.pool.fetchval(
        "INSERT INTO documents(practice_id,client_id,file_id) VALUES($1,76,'example-itk') RETURNING id",
        pid,
    )
    await env.pool.execute(
        """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by)
           VALUES($1,'visa_demo','itk',$2,'test-staff')""",
        pid,
        doc,
    )
    assert await env.pool.fetchval(
        "SELECT fulfilled_at IS NOT NULL FROM practice_visa_followups WHERE practice_id=$1", pid
    )
    award = await _award(env.pool, env.emp_id, 2, 75_000)
    period = (await env.svc.calculate_payroll(2, 2025, env.admin))["period_id"]
    drift_checked, resume = asyncio.Event(), asyncio.Event()

    class PausedConnection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, attr):
            return getattr(self.conn, attr)

        async def fetchval(self, query, *args):
            result = await self.conn.fetchval(query, *args)
            if "COALESCE(ps.bonus_total_idr" in query:
                assert result is None
                drift_checked.set()
                await resume.wait()
            return result

    class PausedPool:
        @asynccontextmanager
        async def acquire(self):
            async with env.pool.acquire() as conn:
                yield PausedConnection(conn)

    approving = asyncio.create_task(HRService(PausedPool()).approve_payroll(period, env.admin))
    try:
        await asyncio.wait_for(drift_checked.wait(), timeout=5)
        # env.quick uses a distinct Postgres connection with a 200 ms lock timeout.
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.db_pool.execute(
                "UPDATE documents SET is_archived=true WHERE id=$1", doc
            )
    finally:
        resume.set()
        await asyncio.wait_for(approving, timeout=5)

    assert (
        await env.pool.fetchval("SELECT payroll_period_id FROM hr_bonus_ledger WHERE id=$1", award)
        == period
    )
    await env.pool.execute("UPDATE documents SET is_archived=true WHERE id=$1", doc)
    assert not await env.pool.fetchval(
        "SELECT fulfilled_at IS NOT NULL FROM practice_visa_followups WHERE practice_id=$1", pid
    )
    assert (await env.svc.mark_payroll_paid(period))[
        "status"
    ] == "paid"  # the approved snapshot stays fixed
