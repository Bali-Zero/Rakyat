"""Employee bonus receipts. The CRM caller owns the transaction that records issuance or delivery."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import asyncpg

from backend.app.services.hr.hr_service import (
    BONUS_RUN_FIRST_MONTH,
    BUSINESS_TZ,
    BonusPeriodLockedError,
    _lock_bonus_month,
    _next_month,
    _refuse_if_bonus_month_closed,
    bonus_payday,
    paid_salary_periods,
    payroll_set_sql,
)
from backend.services.crm.delivery_catalog import delivery_policy

WITA = ZoneInfo(BUSINESS_TZ)


def payroll_award_date(earned_at: datetime) -> datetime:
    """A bonus belongs to the calendar month (Bali time) in which it is earned, up to its last day.

    Owner rule 2026-11-27: no roll-over after the 28th; an upload on the 29th-31st counts for that month.
    """
    return earned_at.astimezone(WITA)


async def get_delivery_bonus(conn: asyncpg.Connection, practice_id: int) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT e.event_kind, e.earned_at, e.recorded_at, e.evidence_issued_on, e.awarded_at, e.amount_idr,
               COALESCE(b.status, CASE WHEN e.payroll_ineligible THEN 'ineligible'
                                      WHEN e.forfeited THEN 'forfeited'
                                      WHEN e.amount_idr = 0 THEN 'no_bonus'
                                      WHEN e.employee_id IS NULL THEN 'awaiting_employee'
                                      WHEN e.amount_idr IS NULL THEN 'awaiting_rate'
                                      ELSE 'payroll_locked' END) AS status
        FROM hr_delivery_bonus_events e LEFT JOIN hr_bonus_ledger b ON b.id = e.ledger_id
        WHERE e.practice_id = $1
        """,
        practice_id,
    )
    return dict(row) if row else None


async def _write_ledger(
    conn: asyncpg.Connection, event: asyncpg.Record, awarded_at: datetime
) -> None:
    local = awarded_at.astimezone(WITA)
    await _lock_bonus_month(conn, local.month, local.year, exclusive=False)
    await _refuse_if_bonus_month_closed(conn, local.month, local.year)
    ledger = await conn.fetchval(
        """
        INSERT INTO hr_bonus_ledger
            (practice_id, employee_id, practice_type_code, bonus_rate_id, amount_idr, awarded_at, status)
        VALUES ($1, $2, $3, $4, $5, $6, 'pending') RETURNING id
        """,
        event["practice_id"],
        event["employee_id"],
        event["service_code"],
        event["bonus_rate_id"],
        event["amount_idr"],
        awarded_at,
    )
    await conn.execute(
        "UPDATE hr_delivery_bonus_events SET ledger_id = $2, awarded_at = $3 WHERE practice_id = $1",
        event["practice_id"],
        ledger,
        awarded_at,
    )


async def record_delivery_bonus(
    conn: asyncpg.Connection,
    practice_id: int,
    service_code: str,
    assigned_to: str | None,
    event_kind: str,
    *,
    issued_on: date | None = None,
    earned_at: datetime | None = None,
) -> dict | None:
    """Call inside the practice mutation's transaction after recording the qualifying output.

    The caller holds the practice row lock. A receipt that exists is returned as it is: no INSERT,
    no wait on HR's lock of that row. ``earned_at`` is the instant the proof first existed when
    that is earlier than now (it dates the receipt, its bonus month and its rate; recorded_at stays now).
    """
    previous = await get_delivery_bonus(conn, practice_id)
    if previous:
        return previous
    recorded = datetime.now(WITA)
    earned = earned_at or recorded
    if event_kind == "electronic_visa_issued":
        if issued_on is None or issued_on > recorded.date():
            raise ValueError(
                "An issued Electronic Visa needs its actual issue date, up to today in Bali"
            )
    # Existing payroll records win across upgrades, including already paid/reversed bonuses.
    # Resolve the email against all identities before evaluating the payroll set.
    # A missing HR card may leave the predicate NULL; an explicit exclusion is FALSE.
    # Zero matches or an ambiguous mix stays open for HR, never auto-credited.
    event = await conn.fetchrow(
        f"""
        INSERT INTO hr_delivery_bonus_events
            (practice_id, employee_id, service_code, event_kind, earned_at, awarded_at,
             bonus_rate_id, amount_idr, ledger_id, recorded_at, evidence_issued_on,
             original_team_member_id, payroll_ineligible, forfeited)
        SELECT $1, COALESCE(old.employee_id, employee.id), $2, $4, $5, COALESCE(old.awarded_at, $6),
               CASE WHEN old.id IS NOT NULL THEN old.bonus_rate_id WHEN NOT employee.ineligible THEN rate.id END,
               CASE WHEN old.id IS NOT NULL THEN old.amount_idr
                    WHEN employee.ineligible THEN NULL
                    WHEN rate.forfeit_below_idr IS NULL OR paid.below IS FALSE THEN rate.amount_idr END,
               old.id, $9, $8, CASE WHEN old.id IS NULL THEN employee.member_id END,
               old.id IS NULL AND employee.ineligible,
               old.id IS NULL AND NOT employee.ineligible AND COALESCE(paid.below, FALSE)
        FROM (SELECT 1) seed
        LEFT JOIN LATERAL (
            SELECT id, employee_id, awarded_at, bonus_rate_id, amount_idr FROM hr_bonus_ledger
             WHERE practice_id = $1 ORDER BY id LIMIT 1
        ) old ON TRUE
        LEFT JOIN LATERAL (
            SELECT CASE WHEN COUNT(*) = 1 AND bool_and(m.eligible) THEN MIN(m.employee_id) END AS id,
                   CASE WHEN COUNT(DISTINCT m.member_id) = 1 THEN MIN(m.member_id) END AS member_id,
                   COALESCE(bool_and(m.eligible IS FALSE), FALSE) AS ineligible
            FROM (
                SELECT tm.id AS member_id, e.id AS employee_id, {payroll_set_sql()} AS eligible
                FROM team_members tm LEFT JOIN hr_employees e ON e.team_member_id = tm.id
                WHERE lower(trim(tm.email)) = NULLIF(lower(trim($3::text)), '')
            ) m
        ) employee ON TRUE
        LEFT JOIN hr_bonus_rates rate ON rate.practice_type_code = $2 AND rate.is_active
            AND rate.effective_from <= $7 AND (rate.effective_to IS NULL OR rate.effective_to >= $7)
        -- A visa extension paid below its price forfeits the bonus; unknown payment goes to HR.
        LEFT JOIN LATERAL (SELECT hr_practice_paid_below($1, rate.forfeit_below_idr) AS below) paid ON TRUE
        ON CONFLICT (practice_id) DO NOTHING RETURNING *
        """,
        practice_id,
        service_code,
        assigned_to,
        event_kind,
        earned,
        payroll_award_date(earned),
        earned.astimezone(WITA).date(),
        issued_on,
        recorded,
    )
    if (
        event
        and event["employee_id"] is not None
        and event["amount_idr"]
        and event["ledger_id"] is None
    ):
        # A rate of 0 means the service carries no bonus: never a payroll line.
        try:
            await _write_ledger(conn, event, event["awarded_at"])
        except BonusPeriodLockedError:
            # Never alter a fixed payroll; HR can explicitly book this receipt in an open month.
            pass
    return await get_delivery_bonus(conn, practice_id)


async def resolve_delivery_bonus(
    conn: asyncpg.Connection,
    practice_id: int,
    amount_idr: int,
    reviewer: str,
    use_current_period: bool = False,
    *,
    employee_id: int | None = None,
) -> dict | None:
    """Record HR's valuation for the original beneficiary. The caller owns a transaction."""

    event = await conn.fetchrow(
        "SELECT * FROM hr_delivery_bonus_events WHERE practice_id=$1 FOR UPDATE",
        practice_id,
    )
    if not event:
        raise LookupError("Delivery bonus not found")
    if event["ledger_id"] is not None:
        return await get_delivery_bonus(conn, practice_id)
    if event["payroll_ineligible"]:
        raise ValueError("The original assignee was outside payroll at delivery")
    if event["forfeited"]:
        raise ValueError("The bonus was forfeited: the client paid less than the service price")
    if event["amount_idr"] is not None and amount_idr != event["amount_idr"]:
        raise ValueError("The recorded rate must be preserved")
    frozen_beneficiary = event["employee_id"]
    if frozen_beneficiary is not None and employee_id not in (None, frozen_beneficiary):
        raise ValueError("The original beneficiary must be preserved")
    beneficiary = frozen_beneficiary or employee_id
    if beneficiary is None:
        raise ValueError("Select the employee who earned this bonus")
    eligible = await conn.fetchval(
        f"""SELECT {payroll_set_sql()} FROM hr_employees e JOIN team_members tm ON tm.id=e.team_member_id
            WHERE e.id=$1 AND ($2::text IS NULL OR tm.id=$2) FOR SHARE OF e, tm""",
        beneficiary,
        event["original_team_member_id"],
    )
    if not eligible:
        raise ValueError(
            "The selected employee is not the original beneficiary or is no longer on payroll"
        )
    award_date = event["awarded_at"]
    if use_current_period:
        original = award_date.astimezone(WITA)
        await _lock_bonus_month(conn, original.month, original.year, exclusive=False)
        try:
            await _refuse_if_bonus_month_closed(conn, original.month, original.year)
        except BonusPeriodLockedError:
            award_date = datetime.now(WITA)
        else:
            raise ValueError("The original bonus month is still open")
    updated = await conn.fetchrow(
        "UPDATE hr_delivery_bonus_events SET amount_idr=$2, resolved_by=$3, employee_id=$4 WHERE practice_id=$1 RETURNING *",
        practice_id,
        amount_idr,
        reviewer,
        beneficiary,
    )
    if amount_idr:
        await _write_ledger(conn, updated, award_date)
    return await get_delivery_bonus(conn, practice_id)


async def payroll_member(conn: asyncpg.Connection, member_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""SELECT e.id, tm.email FROM hr_employees e JOIN team_members tm ON tm.id=e.team_member_id
            WHERE tm.id=$1 AND {payroll_set_sql()}""",
        member_id,
    )


async def _awaiting_payment(conn: asyncpg.Connection, emp_id: int, today: date) -> list[dict]:
    """Ended bonus months (Nov 2026 on) whose run is not paid yet and that hold money of this employee."""
    runs = {r["bonus_month"]: r["status"] for r in await conn.fetch("SELECT * FROM hr_bonus_runs")}
    periods = await paid_salary_periods(conn)
    waiting, month = [], BONUS_RUN_FIRST_MONTH
    while today >= _next_month(month):
        status = runs.get(month, "open")
        if status != "paid":
            if status == "open":
                row = await conn.fetchrow(
                    """SELECT COALESCE(sum(amount_idr) FILTER (WHERE status='approved' AND NOT held),0)::bigint AS amount,
                              COALESCE(sum(amount_idr) FILTER (WHERE status='pending' AND NOT held),0)::bigint AS pending
                       FROM hr_bonus_run_inputs($1::date, $2::date) WHERE employee_id=$3""",
                    month,
                    today,
                    emp_id,
                )
            else:
                row = await conn.fetchrow(
                    """SELECT COALESCE(sum(amount_idr) FILTER (WHERE bonus_run_id=$1),0)::bigint AS amount,
                              COALESCE(sum(amount_idr) FILTER (WHERE bonus_run_id IS NULL),0)::bigint AS pending
                       FROM hr_bonus_ledger WHERE employee_id=$2
                         AND ((bonus_run_id=$1 AND status='approved')
                              OR (status='pending' AND bonus_run_id IS NULL AND deferred_from IS NULL
                                  AND date_trunc('month', awarded_at AT TIME ZONE 'Asia/Makassar')::date=$1))""",
                    month,
                    emp_id,
                )
            if row["amount"] or row["pending"]:
                payday, waits_on = bonus_payday(month, periods)
                waiting.append(
                    {
                        "bonus_month": month,
                        "run_status": status,
                        "amount_idr": row["amount"],
                        "pending_idr": row["pending"],
                        "expected_payday": payday,
                        "payday_salary_month": waits_on,
                    }
                )
        month = _next_month(month)
    return waiting


async def delivery_desk(conn: asyncpg.Connection, member_id: str) -> dict:
    employee = await payroll_member(conn, member_id)
    today = datetime.now(WITA).date()
    month = today.replace(day=1)
    result = {"team_member_id": member_id, "month": month, "on_payroll": employee is not None}
    if not employee:
        return result
    emp_id = employee["id"]
    totals = await conn.fetchrow(
        """
        SELECT COALESCE(sum(b.amount_idr) FILTER (WHERE b.status='pending'),0)::bigint AS pending,
               COALESCE(sum(b.amount_idr) FILTER (WHERE b.status='approved'),0)::bigint AS approved,
               COALESCE(sum(b.amount_idr) FILTER (WHERE b.status='paid'),0)::bigint AS paid,
               count(*) FILTER (WHERE e.amount_idr IS NULL AND NOT e.forfeited) AS awaiting_rate,
               count(*) FILTER (WHERE e.ledger_id IS NULL AND e.amount_idr > 0
                                  AND NOT e.forfeited) AS payroll_locked
        FROM hr_delivery_bonus_events e LEFT JOIN hr_bonus_ledger b ON b.id=e.ledger_id
        WHERE e.employee_id=$1 AND date_trunc('month', e.awarded_at AT TIME ZONE 'Asia/Makassar')::date=$2
        """,
        emp_id,
        month,
    )
    milestones = await conn.fetchval(
        """SELECT count(*) FROM hr_delivery_bonus_events e WHERE e.employee_id=$1
             AND date_trunc('month', e.earned_at AT TIME ZONE 'Asia/Makassar')::date=$2""",
        emp_id,
        month,
    )
    target = await conn.fetchval(
        "SELECT target FROM hr_delivery_goals WHERE employee_id=$1 AND month=$2", emp_id, month
    )
    recent = await conn.fetch(
        """SELECT e.practice_id, COALESCE(pt.name,e.service_code) AS service,
                  e.event_kind, e.earned_at, e.awarded_at, e.amount_idr,
                  COALESCE(b.status, CASE WHEN e.forfeited THEN 'forfeited'
                                         WHEN e.amount_idr = 0 THEN 'no_bonus'
                                         WHEN e.amount_idr IS NULL THEN 'awaiting_rate'
                                         ELSE 'payroll_locked' END) AS status
           FROM hr_delivery_bonus_events e LEFT JOIN hr_bonus_ledger b ON b.id=e.ledger_id
           LEFT JOIN practice_types pt ON pt.code=e.service_code
           WHERE e.employee_id=$1 ORDER BY e.recorded_at DESC, e.practice_id DESC LIMIT 5""",
        emp_id,
    )
    next_case = await conn.fetchrow(
        """SELECT p.id AS practice_id, pt.name AS service, rate.amount_idr,
                  event.practice_id IS NOT NULL AS already_earned,
                  COALESCE(hr_practice_paid_below(p.id, rate.forfeit_below_idr), FALSE) AS forfeits
           FROM practices p JOIN practice_types pt ON pt.id=p.practice_type_id
           LEFT JOIN hr_delivery_bonus_events event ON event.practice_id=p.id
           LEFT JOIN hr_bonus_rates rate ON rate.practice_type_code=pt.code AND rate.is_active
                AND rate.effective_from<=$2 AND (rate.effective_to IS NULL OR rate.effective_to>=$2)
           WHERE lower(trim(p.assigned_to))=lower(trim($1)) AND p.status='on_process'
               AND p.payment_status='paid'
           ORDER BY COALESCE(p.start_date, p.created_at), p.id LIMIT 1""",
        employee["email"],
        today,
    )
    holds = await conn.fetch(
        """SELECT o.practice_id, o.service_code, COALESCE(pt.name,o.service_code) AS service, o.slot_key,
                  o.due_on, COALESCE(o.bound_month,o.payout_from_month) AS payout_from_month, o.email_state,
                  CASE WHEN $2::date < o.started_on + 60 THEN 'upload_permit'
                       WHEN o.email_state IN ('sending','uncertain') THEN 'verify_escalation_email'
                       WHEN o.email_state='sent' THEN 'upload_after_email'
                       ELSE 'send_escalation_email' END AS next_action
           FROM hr_visa_bonus_obligations o LEFT JOIN practice_types pt ON pt.code=o.service_code
           WHERE o.employee_id=$1 AND (o.bound_period_id IS NOT NULL OR o.bound_run_month IS NOT NULL
                 OR o.due_on <= $2)
           ORDER BY o.due_on, o.practice_id""",
        emp_id,
        today,
    )
    labeled_holds = []
    for row in holds:
        hold = dict(row)
        policy = delivery_policy(hold.pop("service_code"))
        hold["slot_label"] = (
            next(
                (slot["label"] for slot in policy["slots"] if slot["key"] == hold["slot_key"]),
                None,
            )
            if policy
            else None
        )
        labeled_holds.append(hold)
    return {
        **result,
        "awaiting_payment": await _awaiting_payment(conn, emp_id, today),
        "holds": labeled_holds,
        "totals": dict(totals),
        "milestones": milestones,
        "target": target,
        "recent": [dict(r) for r in recent],
        "next_case": dict(next_case) if next_case else None,
    }
