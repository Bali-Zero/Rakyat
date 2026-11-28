"""Bonus reform C, PR3a: a closed bonus month is approved and paid by its own run (real Postgres).

Same schema and pools as test_hr_bonus_payroll_locks (real 068 triggers, migrations 383-399). Synthetic ids only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr import delivery_bonus as bonus
from backend.app.services.hr import hr_service
from backend.app.services.hr.hr_service import (
    BonusPeriodLockedError,
    BonusRunRefusedError,
    bonus_run_ledger_totals,
)
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
OCT = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)
SEP = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
NOV = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 10, 4, 0, tzinfo=timezone.utc)
NOV1, DEC1 = date(2026, 11, 1), date(2026, 12, 1)


def _today(monkeypatch, iso: str) -> None:
    monkeypatch.setattr(hr_service, "business_today", lambda: date.fromisoformat(iso))


async def _emp_b(env: Env) -> int:
    return await env.pool.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-b'")


async def _figures(env: Env, month: date) -> dict:
    async with env.pool.acquire() as conn:
        return await hr_service._run_figures(conn, month, hr_service.business_today())


async def _run(env: Env, month: date = NOV1, *, svc=None, approved=None, pending=None) -> dict:
    """Approve a run with the figures the screen would show right now (or the given ones)."""
    live = await _figures(env, month)
    return await (svc or env.svc).approve_bonus_run(
        month,
        env.admin,
        expected_approved_idr=live["approved_idr"] if approved is None else approved,
        expected_pending=live["pending_count"] if pending is None else pending,
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


async def _off_payroll(env: Env, emp_id: int, off: bool) -> None:
    await env.pool.execute(
        "UPDATE hr_employees SET profile = $2::jsonb WHERE id = $1",
        emp_id,
        '{"on_payroll": false}' if off else "{}",
    )


async def _fulfil(env: Env, practice_id: int) -> None:
    await env.pool.execute(
        "UPDATE practice_visa_followups SET fulfilled_at=now() WHERE practice_id=$1", practice_id
    )


# ─── 1-3: the salary carries no bonus from the cycle on ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_1_december_salary_carries_no_bonus_and_pph21_is_on_the_salary_alone(
    env: Env,
) -> None:
    await env.pool.execute(
        "UPDATE hr_employees SET base_salary_idr=20000000, ptkp_status='TK/0' WHERE id=$1",
        env.emp_id,
    )
    row = await _row(env, "approved", DEC)
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
    result = await env.svc.calculate_payroll(12, 2026, env.admin)
    await env.svc.approve_payroll(result["period_id"], env.admin)
    assert await _count(env, "hr_visa_bonus_hold_periods") == 0
    assert await _count(env, "hr_bonus_deferral_causes") == 0
    assert (await _state(env, row))["deferred_from"] is None
    assert await env.pool.fetchval("SELECT bound_period_id FROM hr_visa_bonus_obligations") is None


@pytest.mark.asyncio
async def test_3_a_paid_november_salary_is_unaffected_by_the_november_run(
    env: Env, monkeypatch
) -> None:
    period = (await env.svc.calculate_payroll(11, 2026, env.admin))["period_id"]
    await env.svc.approve_payroll(period, env.admin)
    paid = await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason, payroll_period_id) VALUES ($1,'manual',100000,'paid',$2,'manual','why',$3)"
        " RETURNING id",
        env.emp_id,
        NOV,
        period,
    )
    await env.svc.mark_payroll_paid(period)
    slips = "SELECT employee_id, bonus_total_idr, net_salary_idr FROM hr_payslips ORDER BY id"
    before = [dict(r) for r in await env.pool.fetch(slips)]
    _today(monkeypatch, "2026-12-01")
    async with env.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_bonus_run_inputs('2026-11-01','2026-12-01') WHERE bonus_id=$1",
                paid,
            )
            == 0
        )
    await _run(env)
    await env.svc.mark_bonus_run_paid(NOV1, env.admin)
    assert (await _state(env, paid))["status"] == "paid"
    assert (await _state(env, paid))["payroll_period_id"] == period
    assert [dict(r) for r in await env.pool.fetch(slips)] == before
    with pytest.raises(asyncpg.exceptions.RaiseError, match="paid and immutable"):
        await env.svc.calculate_payroll(11, 2026, env.admin)


# ─── 4-7: due date, order, strict close, stale screen ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_4_a_month_is_closable_from_the_first_of_the_next_month_in_bali(
    env: Env, monkeypatch
) -> None:
    await _row(env, "approved", NOV)
    for today, month in (("2026-11-30", NOV1), ("2026-12-15", date(2026, 10, 1))):
        _today(monkeypatch, today)
        with pytest.raises(BonusRunRefusedError) as refused:
            await _run(env, month, approved=0, pending=0)
        assert refused.value.code == "bonus_run_not_due"
    _today(monkeypatch, "2026-12-15")
    with pytest.raises(BonusRunRefusedError) as refused:
        await _run(env, date(2026, 11, 15), approved=0, pending=0)
    assert refused.value.code == "bonus_run_not_due"
    assert await _count(env, "hr_bonus_runs") == 0
    _today(monkeypatch, "2026-12-01")
    assert (await _run(env))["run"]["status"] == "approved"


@pytest.mark.asyncio
async def test_5_runs_close_in_order(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2027-01-02")
    with pytest.raises(BonusRunRefusedError, match="Close November 2026 first") as refused:
        await _run(env, DEC1)
    assert refused.value.code == "bonus_run_out_of_order"
    await _run(env, NOV1)
    assert (await _run(env, DEC1))["run"]["bonus_month"] == DEC1


@pytest.mark.asyncio
async def test_6_strict_close_leaves_an_unheld_pending_row_unpayable(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    a, p = await _row(env, "approved", NOV), await _row(env, "pending", NOV)
    result = await _run(env)
    assert (await _state(env, a))["bonus_run_id"] == NOV1
    assert await _state(env, p) == {
        "status": "pending",
        "bonus_run_id": None,
        "deferred_from": None,
        "payroll_period_id": None,
    }
    assert (result["reserved_count"], result["reserved_idr"]) == (1, 100_000)
    assert (result["unpayable_pending_count"], result["unpayable_pending_idr"]) == (1, 100_000)
    with pytest.raises(BonusPeriodLockedError):
        await env.svc.approve_bonus(p, env.admin)
    with pytest.raises(BonusPeriodLockedError):
        await env.svc.create_manual_bonus(
            employee_id=env.emp_id,
            amount_idr=1,
            reason="x",
            practice_type_code=None,
            awarded_on=date(2026, 11, 20),
            created_by="fake.admin@example.com",
        )
    assert (await env.svc.reject_bonus(p, env.admin))["status"] == "rejected"
    dec = await env.svc.create_manual_bonus(
        employee_id=env.emp_id,
        amount_idr=1,
        reason="x",
        practice_type_code=None,
        awarded_on=date(2026, 12, 2),
        created_by="fake.admin@example.com",
    )
    assert dec["status"] == "pending"


@pytest.mark.asyncio
async def test_7_a_stale_screen_is_refused_and_writes_nothing(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    a = await _row(env, "approved", NOV)
    await _row(env, "pending", NOV)
    for kwargs in ({"approved": 1}, {"pending": 0}, {"approved": 0, "pending": 1}):
        with pytest.raises(BonusRunRefusedError) as refused:
            await _run(env, **kwargs)
        assert refused.value.code == "bonus_run_changed"
    assert await _count(env, "hr_bonus_runs") == 0
    assert (await _state(env, a))["bonus_run_id"] is None


# ─── 8-10: holds, one-month hold, off payroll ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_8_an_open_obligation_holds_the_row_and_the_december_run_reserves_it(
    env: Env, monkeypatch
) -> None:
    _today(monkeypatch, "2026-12-20")
    emp_b = await _emp_b(env)
    pa = await _open_followup(env.pool, env.emp_id, date(2026, 10, 1))
    pb = await _open_followup(env.pool, emp_b, date(2026, 10, 1), slot="itk2")
    a = await _row(env, "approved", NOV)
    p = await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason) VALUES ($1,'manual',100000,'pending',$2,'manual','why') RETURNING id",
        emp_b,
        NOV,
    )
    result = await _run(env)
    assert (result["reserved_count"], result["ever_deferred_count"]) == (0, 2)
    for row in (a, p):
        assert (await _state(env, row))["deferred_from"] == NOV1
        assert (await _state(env, row))["bonus_run_id"] is None
    assert await _count(env, "hr_bonus_deferral_causes") == 2
    assert await env.pool.fetch(
        "SELECT practice_id FROM hr_visa_bonus_hold_periods WHERE bonus_run_id=$1 "
        "AND payroll_period_id IS NULL",
        NOV1,
    )
    assert await _count(env, "hr_visa_bonus_hold_periods") == 2
    await _fulfil(env, pa)
    await _fulfil(env, pb)
    assert (await env.svc.approve_bonus(p, env.admin))["status"] == "approved"  # deferral exemption
    _today(monkeypatch, "2027-01-02")
    await _run(env, DEC1)
    assert (await _state(env, a))["bonus_run_id"] == DEC1
    assert (await _state(env, p))["bonus_run_id"] == DEC1


@pytest.mark.asyncio
async def test_8b_an_obligation_payable_from_december_holds_nothing_in_november(
    env: Env, monkeypatch
) -> None:
    _today(monkeypatch, "2026-12-20")
    await _open_followup(env.pool, env.emp_id, date(2026, 11, 5))  # due 12-05 <= today, payable Dec
    a, p = await _row(env, "approved", NOV), await _row(env, "pending", NOV)
    await _run(env)
    assert (await _state(env, a))["bonus_run_id"] == NOV1
    assert (await _state(env, p))["status"] == "pending"
    assert (await _state(env, p))["deferred_from"] is None
    for table in ("hr_visa_bonus_hold_periods", "hr_bonus_deferral_causes"):
        assert await _count(env, table) == 0
    assert (await _state(env, a))["deferred_from"] is None


@pytest.mark.asyncio
async def test_9_a_run_hold_holds_its_month_only(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-20")
    await _open_followup(env.pool, env.emp_id, date(2026, 10, 1))
    nov = await _row(env, "approved", NOV)
    await _run(env)
    dec = await _row(env, "approved", DEC)
    _today(monkeypatch, "2027-01-02")
    await _run(env, DEC1)
    assert (await _state(env, dec))["bonus_run_id"] == DEC1
    assert (await _state(env, nov))["bonus_run_id"] is None
    assert (await _state(env, nov))["deferred_from"] == NOV1


@pytest.mark.asyncio
async def test_10_an_off_payroll_row_is_carried_not_paid_until_the_card_is_back(
    env: Env, monkeypatch
) -> None:
    _today(monkeypatch, "2026-12-01")
    await _off_payroll(env, env.emp_id, True)
    row = await _row(env, "approved", NOV)
    result = await _run(env)
    assert (await _state(env, row))["bonus_run_id"] is None
    assert (await _state(env, row))["deferred_from"] == NOV1
    assert result["reserved_count"] == 0
    await _off_payroll(env, env.emp_id, False)
    _today(monkeypatch, "2027-01-02")
    await _run(env, DEC1)
    assert (await _state(env, row))["bonus_run_id"] == DEC1


# ─── 11-12: paying, one payout per row ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_11_paying_a_run_pays_exactly_its_reserved_rows(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    emp_b = await _emp_b(env)
    await _off_payroll(env, emp_b, True)
    reserved = await _row(env, "approved", NOV)
    carried = await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason) VALUES ($1,'manual',100000,'approved',$2,'manual','why') RETURNING id",
        emp_b,
        NOV,
    )
    pending = await _row(env, "pending", NOV)
    await _run(env)
    done = await env.svc.mark_bonus_run_paid(NOV1, "tm-payer")  # no payday gate: paid on 12-01
    assert (done["paid_count"], done["paid_idr"]) == (1, 100_000)
    run = await env.pool.fetchrow("SELECT status, paid_at, paid_by FROM hr_bonus_runs")
    assert (run["status"], run["paid_by"]) == ("paid", "tm-payer")
    assert run["paid_at"] is not None
    assert (await _state(env, reserved))["status"] == "paid"
    assert (await _state(env, carried))["status"] == "approved"
    assert (await _state(env, carried))["deferred_from"] == NOV1
    assert (await _state(env, pending))["status"] == "pending"
    with pytest.raises(BonusRunRefusedError) as refused:
        await env.svc.mark_bonus_run_paid(NOV1, "tm-payer")
    assert refused.value.code == "bonus_run_not_approved"
    with pytest.raises(LookupError):
        await env.svc.mark_bonus_run_paid(DEC1, "tm-payer")


@pytest.mark.asyncio
async def test_12_a_row_has_one_payout(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    deferred = await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason, deferred_from) VALUES ($1,'manual',100000,'approved',$2,'manual','why',$3)"
        " RETURNING id",
        env.emp_id,
        SEP,
        date(2026, 9, 1),
    )
    unreserved = await _row(env, "pending", DEC)  # not in the November run
    await _run(env)
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


# ─── notes of the round-3 review ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reserve_between_two_runs_a_reserved_row_is_never_reserved_again(
    env: Env, monkeypatch
) -> None:
    """NOTE (a): November reserves a deferred row and stays unpaid; December must not take it too."""
    _today(monkeypatch, "2026-12-01")
    deferred = await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,"
        " source, reason, deferred_from) VALUES ($1,'manual',100000,'approved',$2,'manual','why',$3)"
        " RETURNING id",
        env.emp_id,
        OCT,
        date(2026, 10, 1),
    )
    nov_row = await _row(env, "approved", NOV)
    await _run(env)
    assert (await _state(env, deferred))["bonus_run_id"] == NOV1
    dec_row = await _row(env, "approved", DEC)
    _today(monkeypatch, "2027-01-02")
    async with env.pool.acquire() as conn:
        inputs = await conn.fetch(
            "SELECT bonus_id FROM hr_bonus_run_inputs('2026-12-01','2027-01-02')"
        )
    assert {r["bonus_id"] for r in inputs} == {dec_row}
    await _run(env, DEC1)
    assert (await _state(env, deferred))["bonus_run_id"] == NOV1  # still November's
    assert (await _state(env, dec_row))["bonus_run_id"] == DEC1
    first = await env.svc.mark_bonus_run_paid(DEC1, "tm-payer")  # reverse order: December first
    assert (first["paid_count"], (await _state(env, deferred))["status"]) == (1, "approved")
    second = await env.svc.mark_bonus_run_paid(NOV1, "tm-payer")
    assert (second["paid_count"], second["paid_idr"]) == (2, 200_000)
    assert [(await _state(env, r))["status"] for r in (deferred, nov_row, dec_row)] == ["paid"] * 3
    assert await env.pool.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE status='paid'") == 3


@pytest.mark.asyncio
async def test_the_deferred_count_of_a_closed_run_is_historical(env: Env, monkeypatch) -> None:
    """NOTE (b): `ever_deferred_*` counts every row ever deferred out of the month, whoever pays it later."""
    _today(monkeypatch, "2026-12-20")
    practice = await _open_followup(env.pool, env.emp_id, date(2026, 10, 1))
    held = await _row(env, "approved", NOV)
    result = await _run(env)
    assert (result["ever_deferred_count"], result["ever_deferred_idr"]) == (1, 100_000)
    await _fulfil(env, practice)
    _today(monkeypatch, "2027-01-02")
    await _run(env, DEC1)
    await env.svc.mark_bonus_run_paid(DEC1, "tm-payer")
    state = await _state(env, held)
    assert (state["status"], state["bonus_run_id"], state["deferred_from"]) == ("paid", DEC1, NOV1)
    async with env.pool.acquire() as conn:
        nov = await bonus_run_ledger_totals(conn, NOV1)
        dec = await bonus_run_ledger_totals(conn, DEC1)
    assert (nov["reserved_count"], nov["ever_deferred_count"], nov["ever_deferred_idr"]) == (
        0,
        1,
        100_000,
    )
    assert (dec["reserved_count"], dec["ever_deferred_count"]) == (1, 0)


# ─── 13-14: locks and the race ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_13_approval_holds_the_bonus_month_exclusively(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    async with _BonusHold(env.pool, "hr_bonus_month:2026-11", exclusive=False):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await _run(env, svc=env.quick, approved=0, pending=0)
    async with _BonusHold(env.pool, "hr_payroll_month:2026-11"):
        assert (await _run(env, svc=env.quick, approved=0, pending=0))["run"][
            "status"
        ] == "approved"


@pytest.mark.asyncio
async def test_14b_a_reactivation_after_the_compare_cannot_enter_the_run(
    env: Env, monkeypatch
) -> None:
    """The run locks every employee row: putting an off-payroll person back waits for the run's commit."""
    _today(monkeypatch, "2026-12-01")
    await _off_payroll(env, env.emp_id, True)
    row = await _row(env, "approved", NOV)  # off payroll: the screen confirms 0
    paused, release = asyncio.Event(), asyncio.Event()
    real = hr_service._run_figures

    async def figures_then_pause(conn, month, as_of):
        out = await real(conn, month, as_of)
        paused.set()
        await release.wait()
        return out

    monkeypatch.setattr(hr_service, "_run_figures", figures_then_pause)
    run = asyncio.create_task(
        env.svc.approve_bonus_run(NOV1, env.admin, expected_approved_idr=0, expected_pending=0)
    )
    await asyncio.wait_for(paused.wait(), 5)
    try:
        with pytest.raises(
            asyncpg.exceptions.LockNotAvailableError
        ):  # quick pool: lock_timeout 200 ms
            await env.quick.db_pool.execute(
                "UPDATE hr_employees SET profile = '{}'::jsonb WHERE id = $1", env.emp_id
            )
    finally:
        release.set()
    result = await run
    assert result["reserved_count"] == 0
    assert (await _state(env, row))["bonus_run_id"] is None
    await _off_payroll(env, env.emp_id, False)  # after the commit the edit goes through


@pytest.mark.asyncio
async def test_14_two_concurrent_approvals_close_the_month_once(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    a = await _row(env, "approved", NOV)
    outcomes = await asyncio.gather(
        _run(env, approved=100_000, pending=0),
        _run(env, approved=100_000, pending=0),
        return_exceptions=True,
    )
    refused = [o for o in outcomes if isinstance(o, BonusRunRefusedError)]
    assert len(refused) == 1 and refused[0].code == "bonus_run_closed"
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert await _count(env, "hr_bonus_runs") == 1
    assert (await _state(env, a))["bonus_run_id"] == NOV1


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
async def test_15a_the_rollback_refuses_while_a_run_exists(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    await _run(env, approved=0, pending=0)
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
    async with env.pool.acquire() as t:
        tx = t.transaction()
        await tx.start()
        await t.fetchval("SELECT now()")
        await env.pool.execute(_parts()[0])  # 399 applies and commits while T is open
        await env.pool.execute(
            "CREATE TABLE _schema_versions (id serial PRIMARY KEY, migration_name text UNIQUE, "
            "migration_number int NOT NULL, executed_at timestamptz DEFAULT now())"
        )
        await env.pool.execute(
            "INSERT INTO _schema_versions(migration_name, migration_number) "
            "VALUES ('399_bonus_run_payment', 399)"
        )
        await t.execute("UPDATE hr_payroll_periods SET status='approved' WHERE id=$1", period)
        await tx.commit()
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


# ─── 16 lives in unit/app/routers/test_hr_payroll_routes.py ────────────────────────────────────
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


async def _open_writer(dsn: str, practice: int) -> tuple[asyncpg.Connection, object]:
    conn = await asyncpg.connect(dsn)
    tx = conn.transaction()
    await tx.start()
    await conn.fetchval("SELECT id FROM practices WHERE id=$1 FOR UPDATE", practice)
    await bonus.get_delivery_bonus(conn, practice)
    return conn, tx


async def _wait_for_migrator_lock(env: Env, cap: float = 1.5) -> None:
    """Resume the writer when another backend waits on a lock (the migrator), or after `cap` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cap
    while loop.time() < deadline:
        if await env.pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
            "AND datname=current_database() AND pid<>pg_backend_pid()"
        ):
            return
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_18_a_concurrent_delivery_writer_never_meets_a_deadlock(
    env: Env, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="backend.db.migration_base")
    await _roll_back_399(env)
    await _seed_393_ledger(env)
    practice = await _booking_setup(env, monkeypatch)
    writer, tx = await _open_writer(_schema_dsn(), practice)
    runner = asyncio.create_task(_migration().apply(database_url=_schema_dsn(), dedicated=False))
    try:
        await _wait_for_migrator_lock(
            env
        )  # with the prologue it never waits: the 1.5 s cap applies
        booked = await bonus.record_delivery_bonus(
            writer, practice, "visa_demo", "fake.a@example.com", "service_delivered"
        )
        assert booked["status"] == "pending"
        await asyncio.sleep(1.0)
        await tx.commit()
    finally:
        await writer.close()
    assert await runner is True
    assert any("lock timeout on attempt" in r.getMessage() for r in caplog.records)
    assert await env.pool.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
    assert await _has_column(env)
    assert (
        await env.pool.fetchval("SELECT count(*) FROM _schema_versions WHERE migration_number=399")
        == 1
    )


@pytest.mark.asyncio
async def test_18b_a_busy_writer_gives_a_55p03_migration_error_and_no_half_state(
    env: Env, monkeypatch
) -> None:
    monkeypatch.setattr(mb, "LOCK_RETRY_MAX_ATTEMPTS", 1)
    await _roll_back_399(env)
    await _seed_393_ledger(env)
    practice = await _booking_setup(env, monkeypatch)
    writer, tx = await _open_writer(_schema_dsn(), practice)
    try:
        with pytest.raises(MigrationError) as busy:
            await _migration().apply(database_url=_schema_dsn(), dedicated=False)
        assert isinstance(busy.value.__cause__, asyncpg.exceptions.LockNotAvailableError)
    finally:
        await tx.rollback()
        await writer.close()
    async with _BonusHold(
        env.pool, "hr_bonus_payout_allocation", exclusive=False
    ):  # an open approve_bonus
        with pytest.raises(MigrationError) as busy:
            await _migration().apply(database_url=_schema_dsn(), dedicated=False)
        assert isinstance(busy.value.__cause__, asyncpg.exceptions.LockNotAvailableError)
    assert not await _has_column(env)
    assert not await env.pool.fetchval(
        "SELECT count(*) FROM _schema_versions WHERE migration_number=399"
    )
    assert await _migration().apply(database_url=_schema_dsn(), dedicated=False) is True
    writer, tx = await _open_writer(_schema_dsn(), practice)  # a rollback behind a writer
    manager = MigrationManager(_schema_dsn(), dedicated=False)
    try:
        with pytest.raises(MigrationError) as busy:
            await manager.rollback_migration("399_bonus_run_payment")
        assert isinstance(busy.value.__cause__, asyncpg.exceptions.LockNotAvailableError)
    finally:
        await tx.rollback()
        await writer.close()
        await manager.close()
    assert await _has_column(env)


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
