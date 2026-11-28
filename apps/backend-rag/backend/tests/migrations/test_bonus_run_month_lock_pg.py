"""Bonus reform C, PR2a: the bonus month locks on its own, against a real Postgres.

Same schema and pools as test_hr_bonus_payroll_locks (real 068 triggers, migrations 383-393).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr import hr_service
from backend.app.services.hr.hr_service import (
    BONUS_RUN_FIRST_MONTH,
    BonusPeriodLockedError,
    bonus_month_lock_key,
)
from backend.db.migration_base import split_migration_sql
from backend.tests.migrations.test_hr_bonus_payroll_locks import (  # noqa: F401
    Env,
    env,
)

pytestmark = pytest.mark.integration

MIGRATION_393 = (
    Path(__file__).resolve().parents[2] / "db" / "migrations_v2" / "393_bonus_run_month_lock.sql"
)
NOV = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)


async def _row(
    env: Env, status: str, awarded: datetime, *, deferred_from: date | None = None
) -> int:
    return await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at, "
        "source, reason, deferred_from) VALUES ($1,'manual',100000,$2,$3,'manual','why',$4) RETURNING id",
        env.emp_id,
        status,
        awarded,
        deferred_from,
    )


def _manual(env: Env, month: int, day: int = 20) -> dict:
    return {
        "employee_id": env.emp_id,
        "amount_idr": 1,
        "reason": "month lock",
        "practice_type_code": None,
        "awarded_on": date(2026, month, day),
        "created_by": "fake.admin@example.com",
    }


async def _close_run(env: Env, month: str = "2026-11-01", status: str = "approved") -> None:
    await env.pool.execute(
        "INSERT INTO hr_bonus_runs(bonus_month, status) VALUES ($1, $2)",
        date.fromisoformat(month),
        status,
    )


async def _pay_salary(env: Env, month: int, year: int = 2026) -> int:
    result = await env.svc.calculate_payroll(month, year, env.admin)
    await env.svc.approve_payroll(result["period_id"], env.admin)
    await env.svc.mark_payroll_paid(result["period_id"])
    return result["period_id"]


class _BonusHold:
    """An open transaction holding a month key, as an in-flight request would."""

    def __init__(self, pool: asyncpg.Pool, key: str, *, exclusive: bool = True):
        self.pool, self.key, self.exclusive = pool, key, exclusive

    async def __aenter__(self) -> None:
        self.conn = await self.pool.acquire()
        self.tx = self.conn.transaction()
        await self.tx.start()
        fn = "pg_advisory_xact_lock" if self.exclusive else "pg_advisory_xact_lock_shared"
        await self.conn.execute(f"SELECT {fn}(hashtext($1))", self.key)

    async def __aexit__(self, *exc: object) -> None:
        await self.tx.rollback()
        await self.pool.release(self.conn)


@pytest.mark.asyncio
async def test_b1_a_paid_november_salary_does_not_close_the_november_bonus_month(env: Env) -> None:
    pending = await _row(env, "pending", NOV)
    await _pay_salary(env, 11)
    assert (await env.svc.approve_bonus(pending, env.admin))["status"] == "approved"
    manual = await env.svc.create_manual_bonus(**_manual(env, 11))
    assert manual["status"] == "pending"


@pytest.mark.asyncio
async def test_b2_a_closed_november_run_refuses_only_what_adds_money_to_november(env: Env) -> None:
    pending = await _row(env, "pending", NOV)
    rejected = await _row(env, "pending", NOV)
    deferred = await _row(env, "pending", NOV, deferred_from=date(2026, 10, 1))
    await _close_run(env)
    with pytest.raises(BonusPeriodLockedError, match="bonus run"):
        await env.svc.approve_bonus(pending, env.admin)
    with pytest.raises(BonusPeriodLockedError, match="bonus run"):
        await env.svc.create_manual_bonus(**_manual(env, 11))
    assert (await env.svc.create_manual_bonus(**_manual(env, 12)))["status"] == "pending"
    assert (await env.svc.reject_bonus(rejected, env.admin))["status"] == "rejected"
    assert (await env.svc.approve_bonus(deferred, env.admin))["status"] == "approved"


@pytest.mark.asyncio
async def test_b4_the_bonus_month_key_is_independent_of_the_salary_key(env: Env) -> None:
    """Proves key independence only: the salary key blocks no November bonus, the bonus key does."""
    pending = await _row(env, "pending", NOV)
    async with _BonusHold(env.pool, hr_service.payroll_month_lock_key(11, 2026)):
        assert (await env.quick.approve_bonus(pending, env.admin))["status"] == "approved"
    other = await _row(env, "pending", NOV)
    async with _BonusHold(env.pool, "hr_bonus_month:2026-11"):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.approve_bonus(other, env.admin)
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await env.quick.create_manual_bonus(**_manual(env, 11))
        assert (await env.quick.create_manual_bonus(**_manual(env, 12)))["status"] == "pending"
        assert (await env.quick.calculate_payroll(12, 2026, env.admin))["period_id"]


@pytest.mark.asyncio
async def test_b6_a_closed_run_never_reopens_and_its_month_is_a_first_of_november_on(env) -> None:
    await _close_run(env)
    run = "SELECT paid_at FROM hr_bonus_runs WHERE bonus_month='2026-11-01'"
    assert await env.pool.fetchval(run) is None
    await env.pool.execute("UPDATE hr_bonus_runs SET status='paid'")
    assert await env.pool.fetchval(run) is not None
    for sql in (
        "UPDATE hr_bonus_runs SET status='paid'",
        "UPDATE hr_bonus_runs SET status='approved', paid_at=NULL",
        "UPDATE hr_bonus_runs SET bonus_month='2026-12-01'",
        "DELETE FROM hr_bonus_runs",
    ):
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await env.pool.execute(sql)
    await env.pool.execute(
        "INSERT INTO hr_bonus_runs(bonus_month,status) VALUES ('2026-12-01','approved')"
    )
    with pytest.raises(asyncpg.exceptions.RaiseError):
        await env.pool.execute(
            "UPDATE hr_bonus_runs SET status='approved' WHERE bonus_month='2026-12-01'"
        )
    for bad in ("2026-10-01", "2026-11-15"):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await env.pool.execute(
                "INSERT INTO hr_bonus_runs(bonus_month,status) VALUES ($1,'approved')",
                date.fromisoformat(bad),
            )


@pytest.mark.asyncio
async def test_b7_the_sql_and_python_first_month_and_lock_keys_agree(env) -> None:
    assert await env.pool.fetchval("SELECT hr_bonus_run_first_month()") == BONUS_RUN_FIRST_MONTH
    assert bonus_month_lock_key(10, 2026) == "hr_payroll_month:2026-10"
    assert bonus_month_lock_key(11, 2026) == "hr_bonus_month:2026-11"
    assert bonus_month_lock_key(1, 2027) == "hr_bonus_month:2027-01"


@pytest.mark.asyncio
async def test_a_november_row_booked_after_the_november_salary_is_never_a_december_salary_input(
    env: Env,
) -> None:
    """One step of the double-pay guard: the December salary inputs skip a booked November row.

    It does not prove the later PR3 steps (run approval, payment).
    """
    await _pay_salary(env, 11)
    await _row(env, "approved", NOV)
    inputs = "SELECT count(*) FROM hr_payroll_bonus_inputs('2026-12-01', NULL, '2026-12-31')"
    assert await env.pool.fetchval(inputs) == 0
    assert await env.pool.fetchval("SELECT hr_bonus_month_closed('2026-11-01')") is False


async def _mark_applied(env: Env) -> None:
    """Write the _schema_versions row the runner writes for 393 (the fixture applies the file directly)."""
    await env.pool.execute(
        "CREATE TABLE _schema_versions (id serial PRIMARY KEY, migration_name text UNIQUE, "
        "migration_number int NOT NULL, executed_at timestamptz DEFAULT now())"
    )
    await env.pool.execute("SELECT pg_sleep(0.02)")
    await env.pool.execute(
        "INSERT INTO _schema_versions(migration_name, migration_number) "
        "VALUES ('393_bonus_run_month_lock', 393)"
    )
    await env.pool.execute("SELECT pg_sleep(0.02)")


async def _roll_back_399(env: Env) -> None:
    """399's FKs and ledger columns sit on 393's table: its rollback goes first (PR3a)."""
    path = MIGRATION_393.with_name("399_bonus_run_payment.sql")
    rollback = split_migration_sql(path.read_text(encoding="utf-8"))[1]
    assert rollback, "399 has no rollback marker the runner can find"
    await env.pool.execute(rollback)


def _split() -> tuple[str, str]:
    return split_migration_sql(MIGRATION_393.read_text(encoding="utf-8"))


async def _assert_rollback_refused(env: Env, match: str) -> None:
    _, rollback = _split()
    with pytest.raises(asyncpg.exceptions.RaiseError, match=match):
        await env.pool.execute(rollback)
    assert await env.pool.fetchval("SELECT to_regclass('hr_bonus_runs')") is not None
    assert await env.pool.fetchval("SELECT to_regproc('hr_bonus_month_closed')") is not None


@pytest.mark.asyncio
async def test_b8_the_rollback_refuses_with_a_run_and_otherwise_drops_the_three_objects(
    env,
) -> None:
    forward, rollback = _split()
    await _roll_back_399(env)
    async with env.pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute(
            "INSERT INTO hr_bonus_runs(bonus_month,status) VALUES ('2026-11-01','approved')"
        )
        with pytest.raises(asyncpg.exceptions.RaiseError, match="Preserve closed bonus months"):
            await conn.execute(rollback)
        await tx.rollback()
        await conn.execute(rollback)
        assert await conn.fetchval("SELECT to_regclass('hr_bonus_runs')") is None
        for fn in ("hr_bonus_month_closed", "hr_bonus_run_first_month", "hr_bonus_run_lock"):
            assert await conn.fetchval("SELECT to_regproc($1)", fn) is None
        await conn.execute(forward)
        assert await conn.fetchval("SELECT to_regclass('hr_bonus_runs')") is not None
        assert await conn.fetchval("SELECT hr_bonus_month_closed('2026-11-01')") is False


@pytest.mark.asyncio
async def test_b8a_the_rollback_refuses_after_a_november_receipt_booked_by_the_new_code(
    env: Env, monkeypatch
) -> None:
    from backend.app.services.hr import delivery_bonus as bonus
    from backend.tests.migrations.test_delivery_bonus_events_pg import _freeze

    await _mark_applied(env)
    await _roll_back_399(env)
    await _pay_salary(env, 11)
    await env.pool.execute(
        "INSERT INTO hr_bonus_rates(practice_type_code, amount_idr, effective_from) "
        "VALUES ('visa_demo', 125000, '2020-01-01')"
    )
    practice = await env.pool.fetchval(
        "INSERT INTO practices(status) VALUES ('completed') RETURNING id"
    )
    _freeze(monkeypatch, "2026-11-29T02:00:00+00:00")
    async with env.pool.acquire() as conn:
        async with conn.transaction():
            booked = await bonus.record_delivery_bonus(
                conn, practice, "visa_demo", "fake.a@example.com", "service_delivered"
            )
    assert booked["status"] == "pending"
    await _assert_rollback_refused(env, "roll forward")


@pytest.mark.asyncio
async def test_b8b_the_rollback_refuses_after_approving_a_pre_existing_november_row(
    env: Env,
) -> None:
    pending = await _row(env, "pending", NOV)
    await _mark_applied(env)
    await _roll_back_399(env)
    await _pay_salary(env, 11)
    assert (await env.svc.approve_bonus(pending, env.admin))["status"] == "approved"
    await _assert_rollback_refused(env, "roll forward")


@pytest.mark.asyncio
async def test_b8c_the_rollback_succeeds_when_only_pre_393_rows_exist_and_forward_reapplies(
    env: Env,
) -> None:
    forward, rollback = _split()
    await _row(env, "pending", NOV)
    await _row(env, "approved", NOV)
    await _mark_applied(env)
    await _roll_back_399(env)
    await env.pool.execute(rollback)
    assert await env.pool.fetchval("SELECT to_regclass('hr_bonus_runs')") is None
    await env.pool.execute(forward)
    assert await env.pool.fetchval("SELECT hr_bonus_month_closed('2026-11-01')") is False
