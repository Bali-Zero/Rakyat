"""Bonus reform C, PR3a-1: migration 399 and what it alone guarantees (real Postgres).

Same schema and pools as test_hr_bonus_payroll_locks (real 068 triggers, migrations 383-399). Synthetic ids
only. No test here calls the bonus-run service: every state a run would create (a run row, a reservation, a
hold bound to a run) is created by SQL, so the file proves the migration without the service.

Test-harness contract (SPEC-C-PR3a-SPLIT.md section 2): no wall-clock threshold proves a lock (T1: the runner is
given a lock_timeout far above the bound and the call is bounded by wait_for, so a refusal can only be NOWAIT or
a try-lock); every task is awaited on every exit path (T2); every extra connection is closed in `finally` and
the two-connection tests leave no advisory lock behind (T3).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr import delivery_bonus as bonus
from backend.app.services.hr import hr_service
from backend.db import migration_base as mb
from backend.db.migration_base import (
    BaseMigration,
    MigrationError,
    extract_dependencies,
    split_migration_sql,
)
from backend.db.migration_manager import MigrationManager
from backend.tests.migrations.test_bonus_run_month_lock_pg import (  # noqa: F401
    _BonusHold,
    _pay_salary,
    _roll_back_399,
    _row,
)
from backend.tests.migrations.test_delivery_bonus_events_pg import _freeze
from backend.tests.migrations.test_hr_bonus_payroll_locks import (  # noqa: F401
    SCHEMA,
    Env,
    _dsn,
    _open_followup,
    env,
)

pytestmark = pytest.mark.integration

MIG_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
FILE_399 = MIG_DIR / "399_bonus_run_payment.sql"
SEP = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)
NOV = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 10, 4, 0, tzinfo=timezone.utc)
NOV1, DEC1 = date(2026, 11, 1), date(2026, 12, 1)
# What the prologue's LOCK TABLE ... NOWAIT names (the relation name is not translated by lc_messages).
LOCKED_RELATIONS = (
    "hr_delivery_bonus_events",
    "hr_bonus_ledger",
    "hr_bonus_runs",
    "hr_payroll_periods",
    "practice_visa_followups",
    "hr_visa_bonus_hold_periods",
    "hr_bonus_deferral_causes",
    "hr_visa_bonus_obligations",
)
ALLOCATION_BUSY = "399: bonus payout allocation is busy"
BOUND = 15  # seconds: the whole bound of every call on another connection (T1, T3)


def _today(monkeypatch, iso: str) -> None:
    monkeypatch.setattr(hr_service, "business_today", lambda: date.fromisoformat(iso))


async def _emp_b(env: Env) -> int:
    return await env.pool.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-b'")


async def _insert(
    env: Env,
    status: str,
    awarded: datetime,
    *,
    employee_id: int | None = None,
    deferred_from: date | None = None,
    payroll_period_id: int | None = None,
) -> int:
    return await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason, deferred_from, payroll_period_id) VALUES ($1,'manual',100000,$2,$3,'manual',"
        "'why',$4,$5) RETURNING id",
        employee_id or env.emp_id,
        status,
        awarded,
        deferred_from,
        payroll_period_id,
    )


async def _insert_run(env: Env, month: date = NOV1) -> None:
    """A closed run, by SQL: what approving it would leave behind."""
    await env.pool.execute(
        "INSERT INTO hr_bonus_runs(bonus_month, status, approved_by) VALUES ($1,'approved','tm-admin')",
        month,
    )


async def _state(env: Env, bonus_id: int) -> dict:
    return dict(
        await env.pool.fetchrow(
            "SELECT status, bonus_run_id, deferred_from, payroll_period_id FROM hr_bonus_ledger "
            "WHERE id=$1",
            bonus_id,
        )
    )


async def _count(env: Env, table: str) -> int:
    return await env.pool.fetchval(f"SELECT count(*) FROM {table}")


async def _run_inputs(env: Env, month: date, as_of: str) -> dict[int, bool]:
    rows = await env.pool.fetch(
        "SELECT bonus_id, held FROM hr_bonus_run_inputs($1, $2)", month, date.fromisoformat(as_of)
    )
    return {r["bonus_id"]: r["held"] for r in rows}


async def _run_holds(env: Env, month: date, as_of: str) -> set[int]:
    rows = await env.pool.fetch(
        "SELECT practice_id FROM hr_bonus_run_holds($1, $2)", month, date.fromisoformat(as_of)
    )
    return {r["practice_id"] for r in rows}


async def _no_stray_advisory_locks(env: Env) -> None:
    """T3: no session of this database holds an advisory lock, bar the fixture's own session lock."""
    key = "r2-hr-bonus-locks-357"
    left = await asyncio.wait_for(
        env.pool.fetch(
            "SELECT l.pid, l.classid, l.objid FROM pg_locks l WHERE l.locktype='advisory' AND l.granted "
            "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
            "AND l.pid<>pg_backend_pid() AND NOT (l.classid::bigint=((hashtext($1)::bigint >> 32) & 4294967295) "
            "AND l.objid::bigint=(hashtext($1)::bigint & 4294967295))",
            key,
        ),
        BOUND,
    )
    assert not left, f"advisory locks left behind: {[dict(r) for r in left]}"


# ─── 1-2: R4, the salary carries and holds no bonus from the cycle on ──────────────────────────


@pytest.mark.asyncio
async def test_1_december_salary_carries_no_bonus_and_pph21_is_on_the_salary_alone(
    env: Env,
) -> None:
    await env.pool.execute(
        "UPDATE hr_employees SET base_salary_idr=20000000, ptkp_status='TK/0' WHERE id=$1",
        env.emp_id,
    )
    row = await _row(env, "approved", DEC)
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_payroll_bonus_inputs('2026-12-01', NULL, '2026-12-31')"
        )
        == 0
    )
    result = await env.svc.calculate_payroll(12, 2026, env.admin)
    slip = await env.pool.fetchrow(
        "SELECT id, bonus_total_idr FROM hr_payslips WHERE payroll_period_id=$1 AND employee_id=$2",
        result["period_id"],
        env.emp_id,
    )
    assert slip["bonus_total_idr"] == 0
    pph = await env.pool.fetchval(
        "SELECT amount_idr FROM hr_deductions WHERE payslip_id=$1 AND deduction_type='pph21'",
        slip["id"],
    )
    assert pph == hr_service.calculate_pph21_monthly(20_000_000, "TK/0") > 0
    await env.svc.approve_payroll(result["period_id"], env.admin)
    assert await _state(env, row) == {
        "status": "approved",
        "bonus_run_id": None,
        "deferred_from": None,
        "payroll_period_id": None,
    }
    await env.svc.mark_payroll_paid(result["period_id"])
    assert (await _state(env, row))["status"] == "approved"


@pytest.mark.asyncio
async def test_2_an_overdue_obligation_binds_nothing_to_the_december_salary(
    env: Env, monkeypatch
) -> None:
    _today(monkeypatch, "2026-12-20")
    await _open_followup(env.pool, env.emp_id, date(2026, 10, 1))
    row = await _row(env, "approved", DEC)
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_visa_holds_for_period('2026-12-01', NULL, '2026-12-20')"
        )
        == 0
    )
    result = await env.svc.calculate_payroll(12, 2026, env.admin)
    await env.svc.approve_payroll(result["period_id"], env.admin)
    assert await _count(env, "hr_visa_bonus_hold_periods") == 0
    assert await _count(env, "hr_bonus_deferral_causes") == 0
    assert (await _state(env, row))["deferred_from"] is None
    assert await env.pool.fetchval("SELECT bound_period_id FROM hr_visa_bonus_obligations") is None


# ─── 12: one payout per row ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_12_a_row_has_one_payout(env: Env) -> None:
    deferred = await _insert(env, "approved", SEP, deferred_from=date(2026, 9, 1))
    unreserved = await _row(env, "pending", DEC)  # not in the November run
    await _insert_run(env)
    await env.pool.execute("UPDATE hr_bonus_ledger SET bonus_run_id=$2 WHERE id=$1", deferred, NOV1)
    assert (await _state(env, deferred))["bonus_run_id"] == NOV1
    inputs = "SELECT bonus_id FROM hr_payroll_bonus_inputs('2026-10-01', NULL, '2026-12-31')"
    assert deferred not in {r["bonus_id"] for r in await env.pool.fetch(inputs)}
    period = (await env.svc.calculate_payroll(10, 2026, env.admin))["period_id"]
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="ck_hr_bonus_one_payout"):
        await env.pool.execute(
            "UPDATE hr_bonus_ledger SET payroll_period_id=$2 WHERE id=$1", deferred, period
        )
    await env.pool.execute("UPDATE hr_bonus_ledger SET status='approved' WHERE id=$1", unreserved)
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="ck_hr_bonus_paid_has_payout"):
        await env.pool.execute("UPDATE hr_bonus_ledger SET status='paid' WHERE id=$1", unreserved)


@pytest.mark.asyncio
async def test_a_hold_is_bound_to_exactly_one_of_a_salary_period_or_a_run(env: Env) -> None:
    practice = await _open_followup(env.pool, env.emp_id, date(2026, 10, 1))
    period = (await env.svc.calculate_payroll(10, 2026, env.admin))["period_id"]
    await _insert_run(env)
    for period_id, run in ((None, None), (period, NOV1)):
        with pytest.raises(
            asyncpg.exceptions.CheckViolationError, match="ck_hr_visa_hold_one_payout"
        ):
            await env.pool.execute(
                "INSERT INTO hr_visa_bonus_hold_periods(practice_id, payroll_period_id, bonus_run_id) "
                "VALUES ($1,$2,$3)",
                practice,
                period_id,
                run,
            )
    await env.pool.execute(
        "INSERT INTO hr_visa_bonus_hold_periods(practice_id, bonus_run_id) VALUES ($1,$2)",
        practice,
        NOV1,
    )
    assert await _count(env, "hr_visa_bonus_hold_periods") == 1


# ─── the views: hr_bonus_run_holds, hr_bonus_run_inputs, bound_run_month ───────────────────────


@pytest.mark.asyncio
async def test_the_run_holds_follow_the_follow_up_dates_and_the_run_binding(env: Env) -> None:
    emp_b = await _emp_b(env)
    pa = await _open_followup(
        env.pool, env.emp_id, date(2026, 10, 1)
    )  # due 10-31, payable from Nov
    pb = await _open_followup(
        env.pool, emp_b, date(2026, 11, 5), slot="itk2"
    )  # due 12-05, from Dec
    assert await _run_holds(env, NOV1, "2026-12-20") == {pa}  # pb is payable from December only
    assert await _run_holds(env, DEC1, "2027-01-02") == {pa, pb}
    assert await _run_holds(env, NOV1, "2026-10-15") == set()  # pa is not due yet
    assert await _run_holds(env, date(2026, 10, 1), "2026-12-20") == set()  # before the cycle
    await _insert_run(env)
    await env.pool.execute(
        "INSERT INTO hr_visa_bonus_hold_periods(practice_id, bonus_run_id) VALUES ($1,$2)", pa, NOV1
    )
    obligation = await env.pool.fetchrow(
        "SELECT bound_period_id, bound_month, bound_run_month FROM hr_visa_bonus_obligations "
        "WHERE practice_id=$1",
        pa,
    )
    assert (obligation["bound_period_id"], obligation["bound_month"]) == (None, NOV1)
    assert obligation["bound_run_month"] == NOV1
    assert await _run_holds(env, NOV1, "2026-12-20") == {pa}  # bound to this run: held in it
    assert await _run_holds(env, DEC1, "2027-01-02") == {
        pb
    }  # a run-bound hold holds its month only


@pytest.mark.asyncio
async def test_the_run_inputs_are_this_months_rows_the_reserved_and_the_deferred(env: Env) -> None:
    emp_b = await _emp_b(env)
    nov_period = (await env.svc.calculate_payroll(11, 2026, env.admin))["period_id"]
    await _open_followup(env.pool, emp_b, date(2026, 10, 1))  # holds emp_b's rows
    approved = await _insert(env, "approved", NOV)
    pending = await _insert(env, "pending", NOV)
    on_salary = await _insert(
        env, "approved", NOV, payroll_period_id=nov_period
    )  # a salary booking
    await _insert(env, "rejected", NOV)
    await _insert(env, "approved", OCT)  # not deferred: a pre-November row never enters a run
    carried = await _insert(env, "approved", OCT, deferred_from=date(2026, 10, 1))
    reserved = await _insert(env, "approved", SEP, deferred_from=date(2026, 9, 1))
    december = await _insert(env, "approved", DEC)
    held = await _insert(env, "approved", NOV, employee_id=emp_b)
    caused = await _insert(env, "approved", NOV)  # held by a deferral cause alone
    cause_practice = await _open_followup(env.pool, emp_b, date(2026, 10, 1), slot="itk3")
    await _insert_run(env)
    await env.pool.execute("UPDATE hr_bonus_ledger SET bonus_run_id=$2 WHERE id=$1", reserved, NOV1)
    await env.pool.execute(
        "INSERT INTO hr_visa_bonus_hold_periods(practice_id, bonus_run_id) VALUES ($1,$2)",
        cause_practice,
        NOV1,
    )
    await env.pool.execute(
        "INSERT INTO hr_bonus_deferral_causes(bonus_id, practice_id) VALUES ($1,$2)",
        caused,
        cause_practice,
    )
    nov = await _run_inputs(env, NOV1, "2026-12-20")
    assert nov == {
        approved: False,
        pending: False,
        carried: False,
        reserved: False,
        held: True,
        caused: True,
    }
    assert on_salary not in nov  # M6: a row a salary period reserved is no run input
    dec = await _run_inputs(env, DEC1, "2027-01-02")
    assert december in dec and reserved not in dec  # a reserved row is never reserved again
    assert await _run_inputs(env, date(2026, 10, 1), "2026-12-20") == {}  # before the cycle


@pytest.mark.asyncio
async def test_a_december_row_reserved_by_a_pre_399_salary_never_enters_a_run(env: Env) -> None:
    await _roll_back_399(env)  # the state before 399: the salary carries and reserves its bonus
    row = await _row(env, "approved", DEC)
    period = (await env.svc.calculate_payroll(12, 2026, env.admin))["period_id"]
    await env.svc.approve_payroll(period, env.admin)
    reserved = await env.pool.fetchval(
        "SELECT payroll_period_id FROM hr_bonus_ledger WHERE id=$1", row
    )
    assert reserved == period
    await env.pool.execute(_parts()[0])  # 399 arrives
    inputs = await env.pool.fetch(
        "SELECT bonus_id FROM hr_bonus_run_inputs('2026-12-01','2027-01-02')"
    )
    assert row not in {r["bonus_id"] for r in inputs}


@pytest.mark.asyncio
async def test_a_hold_bound_to_a_run_is_no_salary_hold_for_a_retroactive_month(
    env: Env, monkeypatch
) -> None:
    _today(monkeypatch, "2026-12-20")
    practice = await _open_followup(
        env.pool, env.emp_id, date(2026, 8, 1)
    )  # payable from September
    oct_row = await _insert(env, "approved", OCT)
    await _insert_run(env)  # the November run binds the hold to itself
    await env.pool.execute(
        "INSERT INTO hr_visa_bonus_hold_periods(practice_id, bonus_run_id) VALUES ($1,$2)",
        practice,
        NOV1,
    )
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_visa_holds_for_period('2026-10-01', NULL, '2026-12-20')"
        )
        == 0
    )
    held = await env.pool.fetchrow(
        "SELECT held FROM hr_payroll_bonus_inputs('2026-10-01', NULL, '2026-12-20') WHERE bonus_id=$1",
        oct_row,
    )
    assert held["held"] is False  # M4: the run-bound hold withholds nothing from the October salary
    result = await env.svc.calculate_payroll(10, 2026, env.admin)  # retroactive pre-November salary
    slip = await env.pool.fetchval(
        "SELECT bonus_total_idr FROM hr_payslips WHERE payroll_period_id=$1 AND employee_id=$2",
        result["period_id"],
        env.emp_id,
    )
    assert slip == 100_000
    await env.svc.approve_payroll(result["period_id"], env.admin)
    hold = await env.pool.fetchrow(
        "SELECT payroll_period_id, bonus_run_id FROM hr_visa_bonus_hold_periods WHERE practice_id=$1",
        practice,
    )
    assert (hold["payroll_period_id"], hold["bonus_run_id"]) == (None, NOV1)
    assert (await _state(env, oct_row))["payroll_period_id"] == result["period_id"]


# ─── 15: the 399 rollback ──────────────────────────────────────────────────────────────────────


def _parts() -> tuple[str, str]:
    forward, rollback = split_migration_sql(FILE_399.read_text(encoding="utf-8"))
    assert rollback
    return forward, rollback


async def _refused(env: Env, match: str) -> None:
    with pytest.raises(asyncpg.exceptions.RaiseError, match=match):
        await env.pool.execute(_parts()[1])
    assert await env.pool.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE column_name='bonus_run_id' "
        "AND table_schema=$1",
        SCHEMA,
    )


@pytest.mark.asyncio
async def test_15a_the_rollback_refuses_while_a_run_exists(env: Env) -> None:
    await _insert_run(env)
    await _refused(env, "Preserve closed bonus runs")


@pytest.mark.asyncio
async def test_15b_the_rollback_refuses_after_the_december_salary_is_approved(env: Env) -> None:
    await _row(env, "pending", DEC)
    period = (await env.svc.calculate_payroll(12, 2026, env.admin))["period_id"]
    await env.svc.approve_payroll(period, env.admin)
    await _refused(env, "from December 2026 on")


@pytest.mark.asyncio
async def test_15b2_the_rollback_refuses_a_december_salary_with_no_bonus_row_at_all(
    env: Env,
) -> None:
    period = (await env.svc.calculate_payroll(12, 2026, env.admin))["period_id"]
    await env.svc.approve_payroll(period, env.admin)
    await _refused(env, "from December 2026 on")


@pytest.mark.asyncio
async def test_15c_the_rollback_refuses_a_december_approval_that_straddles_the_migration(
    env: Env,
) -> None:
    """The approval stamps approved_at with its transaction's START, before 399's executed_at."""
    await _roll_back_399(env)
    await _row(env, "pending", DEC)
    period = (await env.svc.calculate_payroll(12, 2026, env.admin))["period_id"]
    forward = _parts()[0]
    async with env.pool.acquire() as t:  # T1/T3: every await on either connection is bounded
        tx = t.transaction()
        await asyncio.wait_for(tx.start(), BOUND)
        try:
            await asyncio.wait_for(t.fetchval("SELECT now()"), BOUND)
            # 399 applies and commits while T is open
            await asyncio.wait_for(env.pool.execute(forward), BOUND)
            await asyncio.wait_for(
                env.pool.execute(
                    "CREATE TABLE _schema_versions (id serial PRIMARY KEY, migration_name text UNIQUE, "
                    "migration_number int NOT NULL, executed_at timestamptz DEFAULT now())"
                ),
                BOUND,
            )
            await asyncio.wait_for(
                env.pool.execute(
                    "INSERT INTO _schema_versions(migration_name, migration_number) "
                    "VALUES ('399_bonus_run_payment', 399)"
                ),
                BOUND,
            )
            await asyncio.wait_for(
                t.execute("UPDATE hr_payroll_periods SET status='approved' WHERE id=$1", period),
                BOUND,
            )
            await asyncio.wait_for(tx.commit(), BOUND)
        finally:
            if t.is_in_transaction():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tx.rollback(), BOUND)
    assert await env.pool.fetchval(
        "SELECT approved_at < (SELECT executed_at FROM _schema_versions) FROM hr_payroll_periods"
    )
    await _refused(env, "from December 2026 on")


@pytest.mark.asyncio
async def test_15e_the_rollback_refuses_an_approved_row_under_the_paid_november_salary(
    env: Env,
) -> None:
    await _pay_salary(env, 11)
    await _row(env, "approved", NOV)
    await _refused(env, "paid only by a bonus run")


@pytest.mark.asyncio
async def test_15d_the_nov_only_state_rolls_back_to_393_and_the_forward_reapplies(
    env: Env,
) -> None:
    await env.pool.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='nuzantara_readonly') "
        "THEN CREATE ROLE nuzantara_readonly NOLOGIN; END IF; END $$"
    )
    await env.pool.execute("GRANT SELECT ON hr_visa_bonus_obligations TO nuzantara_readonly")
    await _pay_salary(env, 11)
    await _row(env, "pending", NOV)  # F4: November rows are only pending
    await env.svc.calculate_payroll(12, 2026, env.admin)  # a Dec period, still 'calculated'
    dec = await _row(env, "approved", DEC)
    forward, rollback = _parts()
    await env.pool.execute(rollback)
    assert not await env.pool.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE column_name='bonus_run_id' "
        "AND table_schema=$1",
        SCHEMA,
    )
    assert await env.pool.fetchval(
        "SELECT has_table_privilege('nuzantara_readonly','hr_visa_bonus_obligations','SELECT')"
    )
    inputs = await env.pool.fetch(
        "SELECT bonus_id FROM hr_payroll_bonus_inputs('2026-12-01',NULL,'2026-12-31')"
    )
    assert {r["bonus_id"] for r in inputs} == {dec}  # 393 again: the salary carries it
    assert await env.pool.fetchval("SELECT to_regclass('hr_bonus_runs')") is not None
    await env.pool.execute(forward)
    assert await env.pool.fetchval("SELECT to_regproc('hr_bonus_run_inputs')") is not None


# ─── 17-18: the real runner ────────────────────────────────────────────────────────────────────


def _schema_dsn() -> str:
    base = _dsn()
    return f"{base}{'&' if '?' in base else '?'}search_path={SCHEMA}"


def _migration() -> BaseMigration:
    return BaseMigration(
        399, FILE_399.name, "pr3a", dependencies=[393], rollback_sql=_parts()[1], _sql_dir=MIG_DIR
    )


async def _has_column(env: Env) -> bool:
    return bool(
        await env.pool.fetchval(
            "SELECT count(*) FROM information_schema.columns WHERE column_name='bonus_run_id' "
            "AND table_schema=$1 AND table_name='hr_bonus_ledger'",
            SCHEMA,
        )
    )


async def _seed_393_ledger(env: Env) -> None:
    """The runner's own ledgers in the fixture schema, with a 393 row so the dependency check passes.

    Never the minimal `_schema_versions` of `_mark_applied`: the runner's provenance probe looks at
    `public`, so the private copy needs the provenance columns its INSERT then names.
    """
    async with env.pool.acquire() as conn:
        await _migration()._ensure_migration_log(conn)
    await env.pool.execute(
        "ALTER TABLE _schema_versions ADD COLUMN applied_as text, ADD COLUMN applied_via text, "
        "ADD COLUMN runner_version text"
    )
    await env.pool.execute(
        "INSERT INTO schema_migrations(migration_name, migration_number, checksum) "
        "VALUES ('393_bonus_run_month_lock', 393, 'x')"
    )


def test_17_the_marker_stands_alone_and_the_forward_holds_none_of_the_rollback() -> None:
    forward, rollback = split_migration_sql(FILE_399.read_text(encoding="utf-8"))
    assert rollback, "the rollback marker must be alone on its line (migration_base.py:524-527)"
    assert extract_dependencies(FILE_399.read_text(encoding="utf-8")) == [393]
    for statement in (
        "DROP FUNCTION hr_bonus_run_inputs",
        "DROP VIEW",
        "Preserve closed bonus runs",
    ):
        assert statement in rollback and statement not in forward


@pytest.mark.asyncio
async def test_17_the_runner_finds_the_rollback_and_applies_and_rolls_back_399(env: Env) -> None:
    forward, rollback = _parts()
    text = FILE_399.read_text(encoding="utf-8")
    assert extract_dependencies(text) == [393]
    for statement in (
        "DROP FUNCTION hr_bonus_run_inputs",
        "DROP VIEW",
        "Preserve closed bonus runs",
    ):
        assert statement in rollback and statement not in forward
    await _roll_back_399(env)
    await _seed_393_ledger(env)
    assert await _migration().apply(database_url=_schema_dsn(), dedicated=False) is True
    assert await _has_column(env)
    stored = await env.pool.fetchval(
        "SELECT rollback_sql FROM _schema_versions WHERE migration_name='399_bonus_run_payment'"
    )
    assert stored == rollback
    manager = MigrationManager(_schema_dsn(), dedicated=False)
    try:
        assert await manager.rollback_migration("399_bonus_run_payment") is True
    finally:
        await manager.close()
    assert not await _has_column(env)
    for table in ("_schema_versions", "schema_migrations"):
        assert not await env.pool.fetchval(
            f"SELECT count(*) FROM {table} WHERE migration_number=399"
        )


async def _booking_setup(env: Env, monkeypatch) -> int:
    await env.pool.execute(
        "INSERT INTO hr_bonus_rates(practice_type_code, amount_idr, effective_from) "
        "VALUES ('visa_demo', 125000, '2020-01-01')"
    )
    _freeze(monkeypatch, "2026-11-29T02:00:00+00:00")
    return await env.pool.fetchval(
        "INSERT INTO practices(status) VALUES ('completed') RETURNING id"
    )


@contextlib.asynccontextmanager
async def _open_writer(dsn: str, practice: int) -> AsyncIterator[tuple[asyncpg.Connection, object]]:
    """A delivery writer holding its locks; T3: connection and transaction are released on EVERY exit path,
    including an error or a cancellation while it is still being opened. Every await is bounded."""
    conn = await asyncio.wait_for(asyncpg.connect(dsn), BOUND)
    tx = conn.transaction()
    try:
        await asyncio.wait_for(tx.start(), BOUND)
        await asyncio.wait_for(
            conn.fetchval("SELECT id FROM practices WHERE id=$1 FOR UPDATE", practice), BOUND
        )
        await asyncio.wait_for(bonus.get_delivery_bonus(conn, practice), BOUND)
        yield conn, tx
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tx.rollback(), BOUND)
        try:
            await asyncio.wait_for(conn.close(), BOUND)
        except BaseException:
            conn.terminate()
            raise


class _FirstRefusal:
    """Sets `seen` on the runner's first refused attempt: the retry loop asks for its backoff only after a
    55P03. A spy on the module function, so the signal holds whatever the logging configuration is."""

    def __init__(self, monkeypatch) -> None:  # noqa: ANN001
        self.seen = asyncio.Event()
        real = mb._lock_retry_backoff_seconds

        def spy(attempt: int) -> float:
            self.seen.set()
            return real(attempt)

        monkeypatch.setattr(mb, "_lock_retry_backoff_seconds", spy)


async def _until_the_migrator_met_the_lock(
    env: Env, refusal: _FirstRefusal, writer_pid: int
) -> None:
    """Return only on an OBSERVED signal: the runner logged its first 55P03 retry, or another backend (the
    migrator, whatever its application name) is seen waiting on a lock. A clock never lets the test advance:
    the caller bounds this with `wait_for`, and a timeout fails the test."""
    while not refusal.seen.is_set():
        waiting = await asyncio.wait_for(
            env.pool.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND datname=current_database() AND pid<>pg_backend_pid() AND pid<>$1",
                writer_pid,
            ),
            BOUND,
        )
        if waiting:
            return
        await asyncio.sleep(0.05)


async def _reap(task: asyncio.Task) -> None:
    """T2: a task of the test never outlives it, whatever the exit path."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


@pytest.mark.asyncio
async def test_18_a_concurrent_delivery_writer_never_meets_a_deadlock(
    env: Env, monkeypatch
) -> None:
    refusal = _FirstRefusal(monkeypatch)
    runner = None
    try:
        await _roll_back_399(env)
        await _seed_393_ledger(env)
        practice = await _booking_setup(env, monkeypatch)
        async with _open_writer(_schema_dsn(), practice) as (writer, tx):
            runner = asyncio.create_task(
                _migration().apply(database_url=_schema_dsn(), dedicated=False)
            )
            # The writer resumes only once the migrator has met its lock (or waits on it): with the
            # prologue it is refused at once; without it, it waits and the writer deadlocks. No clock.
            await asyncio.wait_for(
                _until_the_migrator_met_the_lock(env, refusal, writer.get_server_pid()), BOUND
            )
            booked = await asyncio.wait_for(
                bonus.record_delivery_bonus(
                    writer, practice, "visa_demo", "fake.a@example.com", "service_delivered"
                ),
                BOUND,
            )
            assert booked["status"] == "pending"
            await asyncio.wait_for(tx.commit(), BOUND)
            assert await asyncio.wait_for(asyncio.shield(runner), BOUND * 2) is True
    finally:
        if runner is not None:
            await _reap(runner)
    assert refusal.seen.is_set()
    assert await env.pool.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
    assert await _has_column(env)
    assert (
        await env.pool.fetchval("SELECT count(*) FROM _schema_versions WHERE migration_number=399")
        == 1
    )
    await _no_stray_advisory_locks(env)


async def _refused_by_a_lock_that_does_not_wait(coro, *, raised: str | None = None) -> None:
    """T1: the refusal is a try-lock or NOWAIT, never a wait that ended in a lock_timeout.

    The runner's lock_timeout is set far above the bound, so a body that waits hits the `wait_for` bound and
    the test fails on TimeoutError; correct code raises 55P03 at once. The origin is read from the error:
    the try-lock's own English RAISE text, or the NOWAIT's relation name (not translated; a lock_timeout
    message names no relation).
    """
    with pytest.raises(MigrationError) as busy:
        await asyncio.wait_for(coro, BOUND)
    cause = busy.value.__cause__
    assert isinstance(cause, asyncpg.exceptions.LockNotAvailableError)
    assert cause.sqlstate == "55P03"
    message = str(cause)
    if raised is not None:
        assert raised in message
    else:
        assert re.search(rf'"({"|".join(LOCKED_RELATIONS)})"', message), message


@pytest.mark.asyncio
async def test_18b_a_busy_writer_gives_a_55p03_migration_error_and_no_half_state(
    env: Env, monkeypatch
) -> None:
    monkeypatch.setattr(mb, "LOCK_RETRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(
        mb, "LOCK_RETRY_LOCK_TIMEOUT", "60s"
    )  # far above BOUND: only NOWAIT can refuse
    await _roll_back_399(env)
    await _seed_393_ledger(env)
    practice = await _booking_setup(env, monkeypatch)
    async with _open_writer(_schema_dsn(), practice):
        await _refused_by_a_lock_that_does_not_wait(
            _migration().apply(database_url=_schema_dsn(), dedicated=False)
        )
    async with _BonusHold(
        env.pool, "hr_bonus_payout_allocation", exclusive=False
    ):  # an open approve_bonus
        await _refused_by_a_lock_that_does_not_wait(
            _migration().apply(database_url=_schema_dsn(), dedicated=False), raised=ALLOCATION_BUSY
        )
    assert not await _has_column(env)
    assert not await env.pool.fetchval(
        "SELECT count(*) FROM _schema_versions WHERE migration_number=399"
    )
    assert (
        await asyncio.wait_for(
            _migration().apply(database_url=_schema_dsn(), dedicated=False), BOUND * 2
        )
        is True
    )
    manager = MigrationManager(_schema_dsn(), dedicated=False)
    try:
        async with _open_writer(_schema_dsn(), practice):  # a rollback behind a writer
            await _refused_by_a_lock_that_does_not_wait(
                manager.rollback_migration("399_bonus_run_payment")
            )
        async with _BonusHold(
            env.pool, "hr_bonus_payout_allocation", exclusive=False
        ):  # a rollback behind an open approve_bonus: the repeated prologue's try-lock
            await _refused_by_a_lock_that_does_not_wait(
                manager.rollback_migration("399_bonus_run_payment"), raised=ALLOCATION_BUSY
            )
    finally:
        await asyncio.wait_for(manager.close(), BOUND)
    assert await _has_column(env)
    await _no_stray_advisory_locks(env)


# ─── 19: B6 combined ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_19_a_run_cannot_change_month_or_approval_while_it_is_paid(env: Env) -> None:
    await env.pool.execute(
        "INSERT INTO hr_bonus_runs(bonus_month, status, approved_by) VALUES ('2026-11-01','approved','tm-x')"
    )
    for change in (
        "bonus_month='2026-12-01'",
        "approved_at=approved_at + interval '1 day'",
        "approved_by='tm-y'",
    ):
        with pytest.raises(asyncpg.exceptions.RaiseError):
            await env.pool.execute(f"UPDATE hr_bonus_runs SET status='paid', {change}")
    run = await env.pool.fetchrow("SELECT bonus_month, status, paid_at FROM hr_bonus_runs")
    assert (run["bonus_month"], run["status"], run["paid_at"]) == (NOV1, "approved", None)
    await env.pool.execute("UPDATE hr_bonus_runs SET status='paid', paid_by='tm-z'")
    assert await env.pool.fetchval("SELECT paid_by FROM hr_bonus_runs") == "tm-z"
