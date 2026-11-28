"""Bonus reform C, PR3b: the bonus run list, its payday and the desk (real Postgres, synthetic ids only)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.app.services.hr import delivery_bonus as bonus
from backend.app.services.hr.hr_service import bonus_payday, list_bonus_runs
from backend.tests.migrations.test_bonus_run_month_lock_pg import _row
from backend.tests.migrations.test_bonus_run_payment_pg import _off_payroll, _run
from backend.tests.migrations.test_delivery_bonus_events_pg import _freeze
from backend.tests.migrations.test_hr_bonus_payroll_locks import _open_followup, env  # noqa: F401
from backend.tests.migrations.test_migration_399_bonus_run_payment_pg import (
    DEC1,
    NOV,
    NOV1,
    _emp_b,
    _today,
)

pytestmark = pytest.mark.integration

NOV_SALARY = {
    "payroll_month": 11,
    "payroll_year": 2026,
    "status": "paid",
    "paid_at": datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc),
}


async def _desk_schema(env) -> None:  # noqa: ANN001
    """The bits of practices and practice_types the desk's next_case and recent queries read."""
    await env.pool.execute(
        "ALTER TABLE practices ADD COLUMN IF NOT EXISTS assigned_to text,"
        " ADD COLUMN IF NOT EXISTS start_date date,"
        " ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now(),"
        " ADD COLUMN IF NOT EXISTS payment_status text"
    )


async def _pay_nov_salary(env) -> None:  # noqa: ANN001
    """The November salary, paid on a fixed day (the clock the trigger would stamp is the real one)."""
    result = await env.svc.calculate_payroll(11, 2026, env.admin)
    await env.svc.approve_payroll(result["period_id"], env.admin)
    await env.pool.execute(
        "UPDATE hr_payroll_periods SET status='paid', paid_at=$2 WHERE id=$1",
        result["period_id"],
        NOV_SALARY["paid_at"],
    )


JAN1 = date(2027, 1, 1)


def _dec_salary(paid_at: datetime) -> dict:
    return {"payroll_month": 12, "payroll_year": 2026, "status": "paid", "paid_at": paid_at}


def test_1_the_payday_is_the_wita_paid_date_of_the_first_salary_after_the_month_plus_seven() -> (
    None
):
    assert bonus_payday(NOV1, [NOV_SALARY]) == (None, DEC1)
    dec = _dec_salary(datetime(2026, 12, 18, 2, 0, tzinfo=timezone.utc))
    assert bonus_payday(NOV1, [NOV_SALARY, dec])[0] == date(2026, 12, 25)
    # 23:59:59 WITA on 30 Nov is still November; 00:00:00 WITA on 1 Dec is not.
    before = _dec_salary(datetime(2026, 11, 30, 15, 59, 59, tzinfo=timezone.utc))
    after = _dec_salary(datetime(2026, 11, 30, 16, 0, 0, tzinfo=timezone.utc))
    assert bonus_payday(NOV1, [before])[0] is None
    assert bonus_payday(NOV1, [after])[0] == date(2026, 12, 8)
    unpaid = {**dec, "status": "approved", "paid_at": None}
    assert bonus_payday(NOV1, [NOV_SALARY, unpaid]) == (None, DEC1)


@pytest.mark.asyncio
async def test_2_the_list_opens_a_month_when_it_ends_and_closes_in_order(
    env,
    monkeypatch,  # noqa: F811
) -> None:
    await _pay_nov_salary(env)
    await _row(env, "approved", NOV)
    await _row(env, "approved", NOV)
    await _row(env, "pending", NOV)
    _today(monkeypatch, "2026-11-28")
    async with env.pool.acquire() as conn:
        (nov,) = await list_bonus_runs(conn, date(2026, 11, 28))
    assert (nov["bonus_month"], nov["status"], nov["ended"], nov["can_approve"]) == (
        NOV1,
        "open",
        False,
        False,
    )
    assert (nov["expected_payday"], nov["payday_salary_month"]) == (None, DEC1)
    async with env.pool.acquire() as conn:
        nov, dec = await list_bonus_runs(conn, date(2026, 12, 1))
    assert (nov["can_approve"], dec["can_approve"], dec["ended"]) == (True, False, False)
    assert (nov["approved_count"], nov["approved_total_idr"]) == (2, 200_000)
    assert (nov["pending_count"], nov["pending_total_idr"]) == (1, 100_000)
    # Dec has ended too, but Nov has no run: Dec is not approvable before it.
    async with env.pool.acquire() as conn:
        nov, dec, jan = await list_bonus_runs(conn, date(2027, 1, 2))
    assert (nov["can_approve"], dec["can_approve"], jan["can_approve"]) == (True, False, False)
    _today(monkeypatch, "2026-12-01")
    await _run(env)
    async with env.pool.acquire() as conn:
        nov, _dec = await list_bonus_runs(conn, date(2026, 12, 1))
    assert (nov["status"], nov["can_approve"], nov["approved_by"]) == (
        "approved",
        False,
        "tm-admin",
    )
    assert nov["approved_at"] is not None and nov["paid_at"] is None
    assert (nov["approved_total_idr"], nov["pending_count"], nov["held_count"]) == (200_000, 1, 0)
    await env.svc.mark_bonus_run_paid(NOV1, "tm-admin")
    async with env.pool.acquire() as conn:
        nov, dec, _jan = await list_bonus_runs(conn, date(2027, 1, 2))
    assert (nov["status"], nov["approved_total_idr"]) == ("paid", 200_000)
    assert nov["paid_at"] is not None
    assert dec["can_approve"] is True  # Nov has its run now


@pytest.mark.asyncio
async def test_3_the_desk_lists_an_ended_unpaid_month_and_its_run_bound_hold(
    env,
    monkeypatch,  # noqa: F811
) -> None:
    await _desk_schema(env)
    await _pay_nov_salary(env)
    await _row(env, "approved", NOV)
    _today(monkeypatch, "2026-12-03")
    _freeze(monkeypatch, "2026-12-03T04:00:00+00:00")
    desk = await bonus.delivery_desk(env.pool, "tm-a")
    (waiting,) = desk["awaiting_payment"]
    assert (waiting["bonus_month"], waiting["run_status"], waiting["amount_idr"]) == (
        NOV1,
        "open",
        100_000,
    )
    assert (waiting["pending_idr"], waiting["expected_payday"]) == (0, None)
    assert waiting["payday_salary_month"] == DEC1
    await _run(env)
    (waiting,) = (await bonus.delivery_desk(env.pool, "tm-a"))["awaiting_payment"]
    assert (waiting["run_status"], waiting["amount_idr"]) == ("approved", 100_000)
    await env.svc.mark_bonus_run_paid(NOV1, "tm-admin")
    assert (await bonus.delivery_desk(env.pool, "tm-a"))["awaiting_payment"] == []


@pytest.mark.asyncio
async def test_3b_a_postponed_run_bound_hold_stays_listed(env, monkeypatch) -> None:  # noqa: F811
    await _desk_schema(env)
    _today(monkeypatch, "2026-12-03")
    _freeze(monkeypatch, "2026-12-03T04:00:00+00:00")
    practice = await _open_followup(env.pool, env.emp_id, date(2026, 10, 20))  # due 11-19, overdue
    await _row(env, "approved", NOV)
    await _run(env)
    assert (
        await env.pool.fetchval(
            "SELECT bonus_run_id FROM hr_visa_bonus_hold_periods WHERE practice_id=$1", practice
        )
        == NOV1
    )
    await env.pool.execute(
        "UPDATE practice_visa_followups SET postponed_at = $2, postponed_by = 'tm-admin',"
        " postponement_note = 'fake', due_on = started_on + 60 WHERE practice_id = $1",
        practice,
        datetime(2026, 11, 25, 4, 0, tzinfo=timezone.utc),
    )  # due 12-19 is after the desk date: only the run binding keeps the hold listed
    desk = await bonus.delivery_desk(env.pool, "tm-a")
    assert [h["practice_id"] for h in desk["holds"]] == [practice]
    assert [
        w["run_status"] for w in desk["awaiting_payment"]
    ] == []  # the held row is carried, not owed


def test_1b_a_month_whose_own_salary_is_unpaid_waits_on_that_salary() -> None:
    assert bonus_payday(NOV1, []) == (None, NOV1)
    late = {**NOV_SALARY, "paid_at": datetime(2026, 12, 2, 1, 0, tzinfo=timezone.utc)}
    assert bonus_payday(NOV1, [late]) == (date(2026, 12, 9), DEC1)  # paid after Nov ends: it counts


@pytest.mark.asyncio
async def test_2b_january_follows_december_and_a_closed_month_counts_its_carried_rows(
    env,
    monkeypatch,  # noqa: F811
) -> None:
    await _row(env, "approved", NOV)
    emp_b = await _emp_b(env)
    await env.pool.execute(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at, "
        "source, reason) VALUES ($1,'manual',70000,'approved',$2,'manual','why')",
        emp_b,
        NOV,
    )
    await _off_payroll(env, emp_b, True)
    _today(monkeypatch, "2026-12-02")
    await _run(env)
    async with env.pool.acquire() as conn:
        nov, _dec = await list_bonus_runs(conn, date(2026, 12, 2))
    # the off-payroll row was deferred from November: the closed month's held figure is historical
    assert (nov["approved_total_idr"], nov["held_count"], nov["held_total_idr"]) == (
        100_000,
        1,
        70_000,
    )
    _today(monkeypatch, "2027-01-02")
    await _run(env, DEC1)
    async with env.pool.acquire() as conn:
        _nov, dec, jan, feb = await list_bonus_runs(conn, date(2027, 2, 1))
    assert (dec["status"], jan["can_approve"], feb["can_approve"]) == ("approved", True, False)


@pytest.mark.asyncio
async def test_4_the_desk_counts_a_held_row_nowhere_and_a_running_month_not_at_all(
    env,
    monkeypatch,  # noqa: F811
) -> None:
    await _desk_schema(env)
    await _row(env, "approved", NOV)
    _freeze(monkeypatch, "2026-11-28T04:00:00+00:00")
    assert (await bonus.delivery_desk(env.pool, "tm-a"))["awaiting_payment"] == []  # Nov still runs
    await _open_followup(env.pool, env.emp_id, date(2026, 10, 20))  # due 11-19: the row is held
    _freeze(monkeypatch, "2026-12-03T04:00:00+00:00")
    assert (await bonus.delivery_desk(env.pool, "tm-a"))["awaiting_payment"] == []
    async with env.pool.acquire() as conn:
        (nov, _dec) = await list_bonus_runs(conn, date(2026, 12, 3))
    assert (nov["held_count"], nov["held_total_idr"], nov["approved_total_idr"]) == (1, 100_000, 0)


@pytest.mark.asyncio
async def test_5_a_carried_row_is_owed_once_on_the_desk_however_many_months_stay_open(
    env,
    monkeypatch,  # noqa: F811
) -> None:
    """Held at the November run, its hold lifts; December and January both end unapproved."""
    await _desk_schema(env)
    _today(monkeypatch, "2026-12-03")
    practice = await _open_followup(env.pool, env.emp_id, date(2026, 10, 20))
    carried = await _row(env, "approved", NOV)
    await _run(env)
    assert (
        await env.pool.fetchval("SELECT deferred_from FROM hr_bonus_ledger WHERE id=$1", carried)
        == NOV1
    )
    await env.pool.execute(
        "UPDATE practice_visa_followups SET fulfilled_at = now() WHERE practice_id = $1", practice
    )
    _freeze(monkeypatch, "2027-02-02T04:00:00+00:00")
    awaiting = (await bonus.delivery_desk(env.pool, "tm-a"))["awaiting_payment"]
    assert [(w["bonus_month"], w["run_status"], w["amount_idr"]) for w in awaiting] == [
        (DEC1, "open", 100_000)
    ]
    own_jan = await _row(env, "approved", datetime(2027, 1, 10, 4, 0, tzinfo=timezone.utc))
    awaiting = (await bonus.delivery_desk(env.pool, "tm-a"))["awaiting_payment"]
    assert [(w["bonus_month"], w["amount_idr"]) for w in awaiting] == [
        (DEC1, 100_000),
        (JAN1, 100_000),
    ]  # January's own row is January's; the carried one stays December's
    assert own_jan != carried
