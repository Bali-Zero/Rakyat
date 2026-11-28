"""Bonus reform C, PR3a-2: a closed bonus month is approved and paid by its own run (real Postgres).

Same schema and pools as test_hr_bonus_payroll_locks (real 068 triggers, migrations 383-399). Synthetic ids
only. What migration 399 alone guarantees (R4 on the salary, the views, the rollback guards, the runner, the
lock trigger) is proved in test_migration_399_bonus_run_payment_pg.py; this file proves the service.

Test-harness contract (SPEC-C-PR3a-SPLIT.md section 2): every pause is released and every task is awaited by
`_approval_paused_after_compare` on any exit path (T2); every await on another connection is bounded by
`wait_for` (T1, T3) and the two-connection tests leave no advisory lock behind (T3).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import date, datetime

import asyncpg
import pytest

from backend.app.services.hr import hr_service
from backend.app.services.hr.hr_service import (
    BonusPeriodLockedError,
    BonusRunRefusedError,
    bonus_run_ledger_totals,
)
from backend.tests.migrations.test_bonus_run_month_lock_pg import (  # noqa: F401
    _BonusHold,
    _row,
)
from backend.tests.migrations.test_hr_bonus_payroll_locks import (  # noqa: F401
    Env,
    _open_followup,
    env,
)
from backend.tests.migrations.test_migration_399_bonus_run_payment_pg import (
    BOUND,
    DEC,
    DEC1,
    NOV,
    NOV1,
    OCT,
    _count,
    _emp_b,
    _insert,
    _no_stray_advisory_locks,
    _state,
    _today,
)

pytestmark = pytest.mark.integration


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


@contextlib.asynccontextmanager
async def _approval_paused_after_compare(
    env: Env, monkeypatch, *, approved: int, pending: int = 0
) -> AsyncIterator[tuple[asyncio.Task, asyncio.Event]]:
    """A November approval stopped right after the figures compare; yields (task, release).

    T2: on ANY exit path, including a timeout waiting for the pause BEFORE the yield, the pause is released,
    the task is cancelled if it is not done and it is awaited, so no transaction or pooled connection outlives
    the test. A test that wants the approval's result releases and awaits it inside the block.
    """
    paused, release = asyncio.Event(), asyncio.Event()
    real = hr_service._run_figures

    async def figures_then_pause(conn, month, as_of):
        out = await real(conn, month, as_of)
        paused.set()
        await release.wait()
        return out

    monkeypatch.setattr(hr_service, "_run_figures", figures_then_pause)
    task = asyncio.create_task(
        env.svc.approve_bonus_run(
            NOV1, env.admin, expected_approved_idr=approved, expected_pending=pending
        )
    )
    try:
        await asyncio.wait_for(paused.wait(), BOUND)
        yield task, release
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ─── 3: the paid November salary ───────────────────────────────────────────────────────────────


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
async def test_4b_the_first_of_the_month_is_the_balinese_one_not_the_utc_one(
    env: Env, monkeypatch
) -> None:
    """The real WITA conversion, on a pinned clock: 23:59 WITA on 11-30 is not due, 00:00 WITA on 12-01 is."""
    await _row(env, "approved", NOV)

    def freeze(iso: str) -> None:
        fixed = datetime.fromisoformat(iso)

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed

        monkeypatch.setattr(hr_service, "datetime", Frozen)

    freeze("2026-11-30T15:59:00+00:00")  # 23:59 WITA on 11-30
    with pytest.raises(BonusRunRefusedError) as refused:
        await _run(env, approved=100_000, pending=0)
    assert refused.value.code == "bonus_run_not_due"
    freeze("2026-11-30T16:00:00+00:00")  # 00:00 WITA on 12-01, still 11-30 in UTC
    assert (await _run(env, approved=100_000, pending=0))["run"]["status"] == "approved"


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
    p = await _insert(env, "pending", NOV, employee_id=emp_b)
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


# ─── 11: paying ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_11_paying_a_run_pays_exactly_its_reserved_rows(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    emp_b = await _emp_b(env)
    await _off_payroll(env, emp_b, True)
    reserved = await _row(env, "approved", NOV)
    carried = await _insert(env, "approved", NOV, employee_id=emp_b)
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


# ─── notes of the round-3 review ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reserve_between_two_runs_a_reserved_row_is_never_reserved_again(
    env: Env, monkeypatch
) -> None:
    """NOTE (a): November reserves a deferred row and stays unpaid; December must not take it too."""
    _today(monkeypatch, "2026-12-01")
    deferred = await _insert(env, "approved", OCT, deferred_from=date(2026, 10, 1))
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


# ─── 13-14: locks and the race (two connections: bounded, released, nothing left behind) ───────


@pytest.mark.asyncio
async def test_13_approval_holds_the_bonus_month_exclusively(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    async with _BonusHold(env.pool, "hr_bonus_month:2026-11", exclusive=False):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await asyncio.wait_for(_run(env, svc=env.quick, approved=0, pending=0), BOUND)
    async with _BonusHold(env.pool, "hr_payroll_month:2026-11"):
        done = await asyncio.wait_for(_run(env, svc=env.quick, approved=0, pending=0), BOUND)
        assert done["run"]["status"] == "approved"
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
async def test_13b_approval_takes_the_allocation_lock_before_the_month_lock(
    env: Env, monkeypatch
) -> None:
    """The HR writers' order: an approval waiting on the allocation key holds no month key yet.

    Observed, not timed: the approval is seen waiting on the allocation key in pg_locks, and the month key is
    still free to a third session's EXCLUSIVE try-lock, which fails against either mode (taken and released in
    one statement)."""
    _today(monkeypatch, "2026-12-01")
    task = None
    try:
        async with _BonusHold(env.pool, "hr_bonus_payout_allocation"):
            task = asyncio.create_task(_run(env, approved=0, pending=0))
            await asyncio.wait_for(_until_the_allocation_key_has_a_waiter(env), BOUND)
            assert await asyncio.wait_for(
                env.pool.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext('hr_bonus_month:2026-11'))"
                ),
                BOUND,
            ), "the approval took the month key before the allocation key"
        done = await asyncio.wait_for(task, BOUND)
        assert done["run"]["status"] == "approved"
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    await _no_stray_advisory_locks(env)


async def _until_the_allocation_key_has_a_waiter(env: Env) -> None:
    """Return once a backend of this database waits for the allocation key itself (a one-bigint advisory key
    shows in pg_locks as classid = high 32 bits, objid = low 32 bits, objsubid = 1); the caller bounds it."""
    while not await asyncio.wait_for(
        env.pool.fetchval(
            """SELECT count(*) FROM pg_locks l, (SELECT hashtext('hr_bonus_payout_allocation')::bigint AS k) key
               WHERE l.locktype = 'advisory' AND NOT l.granted AND l.objsubid = 1
                 AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
                 AND l.classid = ((key.k >> 32) & 4294967295)::oid
                 AND l.objid = (key.k & 4294967295)::oid"""
        ),
        BOUND,
    ):
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_14b_a_reactivation_after_the_compare_cannot_enter_the_run(
    env: Env, monkeypatch
) -> None:
    """The run locks every employee row: putting an off-payroll person back waits for the run's commit."""
    _today(monkeypatch, "2026-12-01")
    await _off_payroll(env, env.emp_id, True)
    row = await _row(env, "approved", NOV)  # off payroll: the screen confirms 0
    async with _approval_paused_after_compare(env, monkeypatch, approved=0) as (task, release):
        with pytest.raises(
            asyncpg.exceptions.LockNotAvailableError
        ):  # quick pool: lock_timeout 200 ms
            await asyncio.wait_for(
                env.quick.db_pool.execute(
                    "UPDATE hr_employees SET profile = '{}'::jsonb WHERE id = $1", env.emp_id
                ),
                BOUND,
            )
        release.set()
        result = await asyncio.wait_for(task, BOUND)
    assert result["reserved_count"] == 0
    assert (await _state(env, row))["bonus_run_id"] is None
    await _off_payroll(env, env.emp_id, False)  # after the commit the edit goes through
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
async def test_14_two_concurrent_approvals_close_the_month_once(env: Env, monkeypatch) -> None:
    _today(monkeypatch, "2026-12-01")
    a = await _row(env, "approved", NOV)
    outcomes = await asyncio.wait_for(
        asyncio.gather(
            _run(env, approved=100_000, pending=0),
            _run(env, approved=100_000, pending=0),
            return_exceptions=True,
        ),
        BOUND,
    )
    refused = [o for o in outcomes if isinstance(o, BonusRunRefusedError)]
    assert len(refused) == 1 and refused[0].code == "bonus_run_closed"
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert await _count(env, "hr_bonus_runs") == 1
    assert (await _state(env, a))["bonus_run_id"] == NOV1
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
async def test_14c_the_run_holds_the_allocation_lock_against_a_concurrent_approval(
    env: Env, monkeypatch
) -> None:
    """An earlier-month deferred row approved mid-run would change what the run was confirmed on."""
    _today(monkeypatch, "2026-12-01")
    deferred = await _insert(env, "pending", OCT, deferred_from=date(2026, 10, 1))
    async with _approval_paused_after_compare(env, monkeypatch, approved=0, pending=1) as (
        task,
        release,
    ):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await asyncio.wait_for(env.quick.approve_bonus(deferred, env.admin), BOUND)
        release.set()
        result = await asyncio.wait_for(task, BOUND)
    assert result["reserved_count"] == 0
    assert (await _state(env, deferred))["status"] == "pending"
    approved = await asyncio.wait_for(env.quick.approve_bonus(deferred, env.admin), BOUND)
    assert approved["status"] == "approved"
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
async def test_14d_the_run_locks_the_followups_it_read(env: Env, monkeypatch) -> None:
    """Fulfilling or postponing a follow-up mid-run would change who is held after the compare."""
    _today(monkeypatch, "2026-12-01")
    practice = await _open_followup(env.pool, env.emp_id, date(2026, 10, 1))
    row = await _row(env, "approved", NOV)  # held by the open follow-up: the screen confirms 0
    async with _approval_paused_after_compare(env, monkeypatch, approved=0) as (task, release):
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await asyncio.wait_for(
                env.quick.db_pool.execute(
                    "UPDATE practice_visa_followups SET fulfilled_at=now() WHERE practice_id=$1",
                    practice,
                ),
                BOUND,
            )
        release.set()
        result = await asyncio.wait_for(task, BOUND)
    assert result["reserved_count"] == 0
    assert (await _state(env, row))["deferred_from"] == NOV1
    await _fulfil(env, practice)  # after the commit the edit goes through
    await _no_stray_advisory_locks(env)
