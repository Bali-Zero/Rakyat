"""Employee bonus receipts. The CRM caller owns the transaction that records issuance or delivery."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from backend.app.services.hr.hr_service import (
    BUSINESS_TZ,
    BonusPeriodLockedError,
    _lock_payroll_month,
    _refuse_if_payroll_closed,
    payroll_set_sql,
)

WITA = ZoneInfo(BUSINESS_TZ)


def payroll_award_date(earned_at: datetime) -> datetime:
    local = earned_at.astimezone(WITA)
    if local.day <= 28:
        return local
    return (
        local.replace(day=28, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=4)
    ).replace(day=1)


async def get_delivery_bonus(conn: asyncpg.Connection, practice_id: int) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT e.event_kind, e.earned_at, e.awarded_at, e.amount_idr,
               COALESCE(b.status, CASE WHEN e.amount_idr IS NULL THEN 'awaiting_rate'
                                      ELSE 'payroll_locked' END) AS status
        FROM hr_delivery_bonus_events e LEFT JOIN hr_bonus_ledger b ON b.id = e.ledger_id
        WHERE e.practice_id = $1 AND e.employee_id IS NOT NULL
        """,
        practice_id,
    )
    return dict(row) if row else None


async def _write_ledger(
    conn: asyncpg.Connection, event: asyncpg.Record, awarded_at: datetime
) -> None:
    local = awarded_at.astimezone(WITA)
    await _lock_payroll_month(conn, local.month, local.year, exclusive=False)
    await _refuse_if_payroll_closed(conn, local.month, local.year)
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
) -> dict | None:
    """Call inside the practice mutation's transaction after recording the qualifying output."""
    earned = datetime.now(WITA)
    # Existing payroll records win across upgrades, including already paid/reversed bonuses.
    # An ineligible assignee still claims the event to prevent awards after reassignment.
    event = await conn.fetchrow(
        f"""
        INSERT INTO hr_delivery_bonus_events
            (practice_id, employee_id, service_code, event_kind, earned_at, awarded_at,
             bonus_rate_id, amount_idr, ledger_id)
        SELECT $1, COALESCE(old.employee_id, employee.id), $2, $4, $5, COALESCE(old.awarded_at, $6),
               CASE WHEN old.id IS NOT NULL THEN old.bonus_rate_id ELSE rate.id END,
               CASE WHEN old.id IS NOT NULL THEN old.amount_idr ELSE rate.amount_idr END, old.id
        FROM (SELECT 1) seed
        LEFT JOIN LATERAL (
            SELECT id, employee_id, awarded_at, bonus_rate_id, amount_idr FROM hr_bonus_ledger
             WHERE practice_id = $1 ORDER BY id LIMIT 1
        ) old ON TRUE
        LEFT JOIN LATERAL (
            SELECT CASE WHEN COUNT(*) = 1 THEN MIN(e.id) END AS id
              FROM hr_employees e JOIN team_members tm ON tm.id = e.team_member_id
             WHERE lower(trim(tm.email)) = lower(trim($3::text)) AND {payroll_set_sql()}
        ) employee ON TRUE
        LEFT JOIN hr_bonus_rates rate ON rate.practice_type_code = $2 AND rate.is_active
            AND rate.effective_from <= $7 AND (rate.effective_to IS NULL OR rate.effective_to >= $7)
        ON CONFLICT (practice_id) DO NOTHING RETURNING *
        """,
        practice_id,
        service_code,
        assigned_to,
        event_kind,
        earned,
        payroll_award_date(earned),
        earned.date(),
    )
    if (
        event
        and event["employee_id"] is not None
        and event["amount_idr"] is not None
        and event["ledger_id"] is None
    ):
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
) -> dict | None:
    """Record HR's valuation for the original beneficiary. The caller owns a transaction."""

    event = await conn.fetchrow(
        "SELECT * FROM hr_delivery_bonus_events WHERE practice_id=$1 AND employee_id IS NOT NULL FOR UPDATE",
        practice_id,
    )
    if not event:
        raise LookupError("Delivery bonus not found")
    if event["ledger_id"] is not None:
        return await get_delivery_bonus(conn, practice_id)
    if event["amount_idr"] is not None and amount_idr != event["amount_idr"]:
        raise ValueError("The recorded rate must be preserved")
    eligible = await conn.fetchval(
        f"""SELECT {payroll_set_sql()} FROM hr_employees e JOIN team_members tm ON tm.id=e.team_member_id
            WHERE e.id=$1 FOR SHARE OF e, tm""",
        event["employee_id"],
    )
    if not eligible:
        raise ValueError("This employee is no longer on payroll")
    award_date = event["awarded_at"]
    if use_current_period:
        original = award_date.astimezone(WITA)
        await _lock_payroll_month(conn, original.month, original.year, exclusive=False)
        try:
            await _refuse_if_payroll_closed(conn, original.month, original.year)
        except BonusPeriodLockedError:
            award_date = datetime.now(WITA)
        else:
            raise ValueError("The original payroll month is still open")
    updated = await conn.fetchrow(
        "UPDATE hr_delivery_bonus_events SET amount_idr=$2, resolved_by=$3 WHERE practice_id=$1 RETURNING *",
        practice_id,
        amount_idr,
        reviewer,
    )
    await _write_ledger(conn, updated, award_date)
    return await get_delivery_bonus(conn, practice_id)


async def payroll_member(conn: asyncpg.Connection, member_id: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""SELECT e.id, tm.email FROM hr_employees e JOIN team_members tm ON tm.id=e.team_member_id
            WHERE tm.id=$1 AND {payroll_set_sql()}""",
        member_id,
    )


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
               count(*) FILTER (WHERE e.amount_idr IS NULL) AS awaiting_rate,
               count(*) FILTER (WHERE e.ledger_id IS NULL AND e.amount_idr IS NOT NULL) AS payroll_locked
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
                  COALESCE(b.status, CASE WHEN e.amount_idr IS NULL THEN 'awaiting_rate'
                                         ELSE 'payroll_locked' END) AS status
           FROM hr_delivery_bonus_events e LEFT JOIN hr_bonus_ledger b ON b.id=e.ledger_id
           LEFT JOIN practice_types pt ON pt.code=e.service_code
           WHERE e.employee_id=$1 ORDER BY e.earned_at DESC, e.practice_id DESC LIMIT 5""",
        emp_id,
    )
    next_case = await conn.fetchrow(
        """SELECT p.id AS practice_id, pt.name AS service, rate.amount_idr,
                  event.practice_id IS NOT NULL AS already_earned
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
    return {
        **result,
        "totals": dict(totals),
        "milestones": milestones,
        "target": target,
        "recent": [dict(r) for r in recent],
        "next_case": dict(next_case) if next_case else None,
    }
