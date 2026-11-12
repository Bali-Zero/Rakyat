"""Reads behind the People console: a bounded number of queries, whatever the roster size.

One query for the roster (staff LEFT JOIN hr_employees), then one aggregate per flag type over
exactly the employees on that roster. Staff = `role <> 'client'` and `active = true` (clients
share `team_members`). A scoped viewer's emails are a bound parameter, never interpolated.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from backend.app.services.hr.hr_service import business_today

DOC_SLOTS = ("ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk")  # + the contract file = 6 papers

# Who counts as a person in People (owner review 2026-11-12). Clients share `team_members`, and
# so do system, test and external accounts. A member WITH an hr_employees row is a person (any
# role but client); without one only a company address with a staff-like role is. Used by
# both SQLs below, over the aliases `tm` (team_members) and `e` (hr_employees, LEFT JOINed).
VISIBLE_PERSON_PREDICATE = """
      tm.active = true
      AND COALESCE(tm.role, '') <> 'client'
      AND (
        e.id IS NOT NULL
        OR (
          lower(tm.email) LIKE '%@balizero.com'
          AND COALESCE(tm.role, '') NOT IN ('client', 'member', 'monitoring')
        )
      )
"""

_ROSTER_SQL = f"""
    SELECT tm.id AS team_member_id, tm.full_name, tm.role, tm.department,
           e.id AS employee_id, e.hire_date, e.is_active AS employee_active, e.profile
    FROM team_members tm
    LEFT JOIN hr_employees e ON e.team_member_id = tm.id
    WHERE {VISIBLE_PERSON_PREDICATE}
      AND ($1::text[] IS NULL OR lower(tm.email) = ANY($1::text[]))
    ORDER BY lower(COALESCE(tm.department, '')), lower(COALESCE(tm.full_name, ''))
"""

_PENDING_BONUSES_SQL = """
    SELECT employee_id, COUNT(*) AS n
    FROM hr_bonus_ledger
    WHERE status = 'pending' AND employee_id = ANY($1::int[])
    GROUP BY employee_id
"""

_PENDING_LEAVE_SQL = """
    SELECT employee_id, COUNT(*) AS n
    FROM hr_leave_requests
    WHERE status = 'pending' AND employee_id = ANY($1::int[])
    GROUP BY employee_id
"""

_ON_LEAVE_SQL = """
    SELECT DISTINCT employee_id
    FROM hr_leave_requests
    WHERE status = 'approved' AND start_date <= $2 AND end_date >= $2
      AND employee_id = ANY($1::int[])
"""

_MEMBER_EMAIL_SQL = f"""
    SELECT tm.email
    FROM team_members tm
    LEFT JOIN hr_employees e ON e.team_member_id = tm.id
    WHERE tm.id = $1 AND {VISIBLE_PERSON_PREDICATE}
"""

_PULSE_SQL = """
    SELECT
      (SELECT COUNT(*) FROM hr_bonus_ledger WHERE status = 'pending') AS bonus_n,
      (SELECT COALESCE(SUM(amount_idr), 0) FROM hr_bonus_ledger WHERE status = 'pending')
          AS bonus_idr,
      (SELECT COUNT(*) FROM hr_leave_requests WHERE status = 'pending') AS leave_n,
      (SELECT status FROM hr_payroll_periods
         WHERE payroll_month = $1 AND payroll_year = $2 LIMIT 1) AS payroll_status
"""


def papers_missing(profile: Any) -> int:
    """How many of the six paper slots (contract + five documents) have no file on record."""
    p = profile if isinstance(profile, dict) else {}
    docs = p.get("docs") if isinstance(p.get("docs"), dict) else {}
    contract = p.get("contract") if isinstance(p.get("contract"), dict) else {}
    missing = sum(1 for slot in DOC_SLOTS if not (isinstance(docs.get(slot), str) and docs[slot]))
    contract_file = contract.get("file_id")
    return missing + (0 if isinstance(contract_file, str) and contract_file else 1)


class HRPeopleService:
    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    async def member_email(self, team_member_id: str) -> str | None:
        """The email of an active staff member by primary key; None if no such staff row."""
        async with self.db_pool.acquire() as conn:
            return await conn.fetchval(_MEMBER_EMAIL_SQL, team_member_id)

    async def roster(self, emails: list[str] | None) -> list[dict[str, Any]]:
        """Visible staff, each with its flags. `emails` None = everyone, else only those."""
        today = business_today()
        async with self.db_pool.acquire() as conn:
            rows = [dict(r) for r in await conn.fetch(_ROSTER_SQL, emails)]
            employee_ids = [r["employee_id"] for r in rows if r["employee_id"] is not None]
            bonuses = {
                r["employee_id"]: int(r["n"])
                for r in await conn.fetch(_PENDING_BONUSES_SQL, employee_ids)
            }
            leave = {
                r["employee_id"]: int(r["n"])
                for r in await conn.fetch(_PENDING_LEAVE_SQL, employee_ids)
            }
            on_leave = {
                r["employee_id"] for r in await conn.fetch(_ON_LEAVE_SQL, employee_ids, today)
            }
        out: list[dict[str, Any]] = []
        for r in rows:
            eid = r["employee_id"]
            profile = r["profile"] if isinstance(r["profile"], dict) else {}
            unit = profile.get("unit")
            has_record = eid is not None
            out.append(
                {
                    "team_member_id": str(r["team_member_id"]),
                    "employee_id": eid,
                    "has_record": has_record,
                    "name": r["full_name"],
                    "role": r["role"],
                    "department": r["department"],
                    "unit": unit if isinstance(unit, str) and unit else None,
                    "hire_date": r["hire_date"],
                    "active": has_record is False or r["employee_active"] is not False,
                    "flags": {
                        "papers_missing": papers_missing(profile) if has_record else None,
                        "bonuses_pending": bonuses.get(eid, 0),
                        "leave_pending": leave.get(eid, 0),
                        "on_leave_today": eid in on_leave,
                    },
                }
            )
        return out

    async def pulse(self, roster: list[dict[str, Any]]) -> dict[str, Any]:
        """The four pulse numbers plus this month's payroll status (WITA)."""
        today = business_today()
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(_PULSE_SQL, today.month, today.year)
        return {
            "active_people": sum(1 for p in roster if p["active"]),
            "people_without_record": sum(1 for p in roster if not p["has_record"]),
            "bonuses_pending_count": int(row["bonus_n"]),
            "bonuses_pending_idr": int(row["bonus_idr"]),
            "leave_pending_count": int(row["leave_n"]),
            "payroll": {"month": today.month, "year": today.year, "status": row["payroll_status"]},
        }
