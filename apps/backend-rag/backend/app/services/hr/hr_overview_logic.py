"""Pure aggregation for `GET /api/hr/me/overview` (no I/O, no database).

Definitions are the ones the admin side already uses, restated here only where
the SQL cannot do it:

- a day is PRESENT when `daily_work_hours` has a row for that `work_date`
  (the view `team_timesheet_service.get_daily_hours` / the CSV export read);
  hours are that view's `hours_worked`; `clock_in_bali` is Bali wall time
  (`get_daily_hours` prints it with a bare `strftime("%H:%M")`);
- a LATE day is an `attendance_late_incidents` row (opened by
  `attendance_monitor.check_late_checkin` at clock-in >= 09:40 Bali time,
  `LATE_INCIDENT_HOUR/MINUTE`), counted by `late_date`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

WITA = ZoneInfo("Asia/Makassar")  # == hr_service.BUSINESS_TZ (asserted in tests)

# Bonus states that never count towards what the member earned.
EXCLUDED_BONUS_STATUSES = frozenset({"rejected", "reversed", "cancelled"})


def bonus_totals(bonuses: list[dict[str, Any]], now: datetime) -> dict[str, int]:
    """Month and year totals in WITA, excluding rejected / reversed / cancelled."""
    here = now.astimezone(WITA)
    month_total = month_count = year_total = 0
    for b in bonuses:
        if str(b.get("status") or "").lower() in EXCLUDED_BONUS_STATUSES:
            continue
        at = b["awarded_at"].astimezone(WITA)
        if at.year != here.year:
            continue
        amount = int(b.get("amount_idr") or 0)
        year_total += amount
        if at.month == here.month:
            month_total += amount
            month_count += 1
    return {
        "month_total_idr": month_total,
        "month_count": month_count,
        "year_total_idr": year_total,
    }


def unambiguous_email(owner: dict[str, Any] | None) -> str | None:
    """The member's email, only if no other team_members row shares it ignoring case.

    `attendance_late_incidents` is keyed by email alone, so an email that two members
    share (differing only by case) cannot be attributed to either: None, and the caller
    reports `late_incidents = null` rather than another member's lateness.
    """
    if not owner:
        return None
    email = owner.get("email")
    if not isinstance(email, str) or not email.strip():
        return None
    return email if owner.get("same_email_members") == 1 else None


def _bali_wall_time(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.astimezone(WITA) if value.tzinfo is not None else value


def aggregate_attendance(
    months: list[tuple[int, int]],
    clock_rows: list[dict[str, Any]],
    late_rows: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """One entry per requested (year, month), in the order given (newest first).

    `late_rows is None` means the incident record could not be read: every
    month then reports `late_incidents = None` rather than a false zero.
    """
    out: list[dict[str, Any]] = []
    for year, month in months:
        days: set[Any] = set()
        hours: list[float] = []
        minutes: list[int] = []
        for r in clock_rows:
            d = r.get("work_date")
            if d is None or (d.year, d.month) != (year, month):
                continue
            days.add(d)
            if r.get("hours_worked") is not None:
                hours.append(float(r["hours_worked"]))
            ci = _bali_wall_time(r.get("clock_in_bali"))
            if ci is not None:
                minutes.append(ci.hour * 60 + ci.minute)
        avg = round(sum(minutes) / len(minutes)) if minutes else None
        late = (
            None
            if late_rows is None
            else len(
                {
                    r["late_date"]
                    for r in late_rows
                    if r.get("late_date") is not None
                    and (r["late_date"].year, r["late_date"].month) == (year, month)
                }
            )
        )
        out.append(
            {
                "month": f"{year:04d}-{month:02d}",
                "days_present": len(days),
                "hours": round(sum(hours), 2) if hours else None,
                "avg_clock_in": f"{avg // 60:02d}:{avg % 60:02d}" if avg is not None else None,
                "late_incidents": late,
            }
        )
    return out
