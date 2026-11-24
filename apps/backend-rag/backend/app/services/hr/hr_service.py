"""
HR/Payroll Service — Business logic for employees, bonuses, payroll, leave.

Pattern: asyncpg pool, same as CRM services.
"""

from __future__ import annotations

import calendar
import logging
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from backend.app.services.hr.hr_leave_routing import SETUP_TEAM_EMAILS, TAX_TEAM_EMAILS
from backend.app.services.hr.hr_overview_logic import aggregate_attendance, unambiguous_email
from backend.app.utils.hr_utils import (
    calculate_bpjs,
    calculate_pph21_monthly,
)
from backend.app.utils.service_accounts import GROUP_STAFF_ROLES_SQL

logger = logging.getLogger(__name__)

# Bali Zero runs on WITA (UTC+8). `awarded_at` is a timestamptz and the Fly
# Postgres session is UTC, so extracting the month straight off the column cuts
# it at 08:00 WITA — a bonus awarded at 00:30 on the 1st is billed to the
# PREVIOUS month. Every month/year bucket over `awarded_at` must therefore pass
# through `AT TIME ZONE 'Asia/Makassar'` first, so payroll, the bonus summary,
# the dashboard and the /hr/bonuses page all agree on which month a bonus
# belongs to. The conversion is written out literally at every call site (never
# interpolated) so a static scan can see it — see
# test_bonus_month_bucketing_is_wita.py.
BUSINESS_TZ = "Asia/Makassar"


def business_today() -> date:
    """Today's date in WITA — the server clock is UTC, the business is not."""
    return (datetime.now(timezone.utc) + timedelta(hours=8)).date()


#: What a manual bonus stores in practice_type_code when no service is named
#: (the column is NOT NULL and has no foreign key).
MANUAL_BONUS_CODE = "manual"


class BonusPeriodLockedError(Exception):
    """The payroll month of a bonus is already approved or paid."""


class PayrollBonusesChangedError(Exception):
    """Approved bonuses no longer match the payslips the period was calculated with."""


class PayrollRosterChangedError(Exception):
    """The payroll set no longer matches the owners of the period's payslips."""


class OffPayrollError(Exception):
    """The employee's card says `on_payroll = false`: no payroll item may be created for them."""


class SelfApprovalError(ValueError):
    """The approver is the person the bonus is for: a bonus needs a second pair of eyes."""


#: SQL predicate over `hr_employees AS e`: true unless the card holds the JSON boolean
#: `false` at `profile.on_payroll` (STAFF_CARD.md C12). Works on a non-object profile too.
ON_PAYROLL_SQL = "((e.profile -> 'on_payroll') IS DISTINCT FROM 'false'::jsonb)"


def payroll_set_sql() -> str:
    """SQL predicate over `hr_employees e JOIN team_members tm`: who is on the monthly payroll.

    Exactly: an active card, a login that is active and not a client/monitoring/partner (the
    People rule, `VISIBLE_PERSON_PREDICATE`, whose card clause is already true on this join),
    and a card that does not say `on_payroll = false`. Imported at call time: hr_people_service
    imports this module.
    """
    from backend.app.services.hr.hr_people_service import VISIBLE_PERSON_PREDICATE

    # Group staff hold an HR card but are never paid through this payroll, whatever
    # `on_payroll` says (their rows are inactive and only the People predicate opts them in).
    return (
        f"(e.is_active = TRUE AND {ON_PAYROLL_SQL} AND ({VISIBLE_PERSON_PREDICATE})"
        f" AND lower(trim(COALESCE(tm.role, ''))) NOT IN ({GROUP_STAFF_ROLES_SQL}))"
    )


async def _lock_bonus_allocations(conn: Any, *, exclusive: bool) -> None:
    """Take before any period or ledger lock. Opening an earlier payroll can move carry between months."""
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await conn.execute(f"SELECT {fn}(hashtext('hr_bonus_payout_allocation'))")


def payroll_month_lock_key(month: int, year: int) -> str:
    """The advisory-lock key of a payroll month (also what the tests lock on)."""
    return f"hr_payroll_month:{year:04d}-{month:02d}"


async def _lock_payroll_month(conn: Any, month: int, year: int, *, exclusive: bool) -> None:
    """Take the transaction-scoped advisory lock of a payroll month.

    It exists before any period row does, which a row lock cannot: whoever ADDS money to
    a month (bonus insert, bonus approval) holds it SHARED, whoever FIXES the month's
    money (calculate, approve, mark paid) holds it EXCLUSIVE, so the two never interleave
    even when no hr_payroll_periods row exists yet. Must run inside a transaction (it is
    released at commit/rollback). Lock order everywhere: bonus row, month lock, period row.
    """
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await conn.execute(f"SELECT {fn}(hashtext($1))", payroll_month_lock_key(month, year))


async def _refuse_if_payroll_closed(conn: Any, month: int, year: int) -> None:
    """Raise BonusPeriodLockedError when the month's payroll is approved or paid.

    Only meaningful under the month lock, which makes this plain read race-free.
    """
    status = await conn.fetchval(
        "SELECT status FROM hr_payroll_periods WHERE payroll_month = $1 AND payroll_year = $2",
        month,
        year,
    )
    if status in ("approved", "paid"):
        msg = "That month's payroll is already approved"
        raise BonusPeriodLockedError(msg)


def department_of(email: str | None, stored: str | None = None) -> str:
    """The department chip of a person: 'tax', 'setup' or 'office'.

    The org chart in hr_leave_routing is authoritative (the stored column is free
    text); the stored value is only a fallback for addresses outside both sets.
    """
    mail = (email or "").lower().strip()
    if mail in TAX_TEAM_EMAILS:
        return "tax"
    if mail in SETUP_TEAM_EMAILS:
        return "setup"
    guess = (stored or "").lower().strip()
    return guess if guess in ("tax", "setup") else "office"


def practice_group(code: str, category: str | None = None) -> str:
    """Settings dropdown group of a practice type: visa | company | tax | other."""
    low = (code or "").lower()
    cat = (category or "").lower()
    if low.startswith("tax") or cat == "tax":
        return "tax"
    if low.startswith("company") or cat in ("company", "business"):
        return "company"
    if low.startswith(("visa", "kitas", "kitap", "voa", "extension")) or cat in (
        "visa",
        "kitas",
        "kitap",
    ):
        return "visa"
    return "other"


_LOCK_ERRORS = (
    asyncpg.exceptions.LockNotAvailableError,
    asyncpg.exceptions.DeadlockDetectedError,
)


def _lock_visible_person_sql() -> str:
    # Imported at call time: hr_people_service imports this module (business_today).
    from backend.app.services.hr.hr_people_service import VISIBLE_PERSON_PREDICATE

    return f"""
        SELECT tm.id
        FROM team_members tm
        LEFT JOIN hr_employees e ON e.team_member_id = tm.id
        WHERE tm.id = $1 AND tm.email = $2 AND {VISIBLE_PERSON_PREDICATE}
        FOR SHARE OF tm
    """


CARD_EDIT_LOCK_TIMEOUT_MS = 5000  # the import's bound (C8): wait at most this for a member lock


class HRCardCode(str, Enum):
    """The closed set of card failure codes (STAFF_CARD.md)."""

    HIRE_DATE_REQUIRED = "hire_date_required"
    INVALID_INPUT = "invalid_input"
    PROFILE_NOT_OBJECT = "profile_not_object"
    DB_ERROR = "db_error"
    LOCK_TIMEOUT = "lock_timeout"


class HRCardError(Exception):
    """A card operation failed. Carries a code from HRCardCode only: a driver or
    pydantic message can quote the personal value being handled, so none propagates."""

    def __init__(self, code: HRCardCode) -> None:
        super().__init__(code.value)
        self.code = code


class HRService:
    """HR/Payroll business logic service."""

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    # ─── EMPLOYEES ───────────────────────────────────────────────────

    async def list_employees(self) -> list[dict[str, Any]]:
        """List all HR employees with team member info."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT e.*, {payroll_set_sql()} AS on_payroll,
                       tm.full_name, tm.email, tm.role, tm.department
                FROM hr_employees e
                JOIN team_members tm ON tm.id = e.team_member_id
                WHERE e.is_active = TRUE
                ORDER BY tm.full_name
            """)
            out = []
            for r in rows:
                row = self._legacy_row(r)
                row["department"] = department_of(row.get("email"), row.get("department"))
                out.append(row)
            return out

    @staticmethod
    def _legacy_row(row: Any) -> dict[str, Any]:
        """Legacy employee shape: the card `profile` is served only by the card route."""
        data = dict(row)
        data.pop("profile", None)
        return data

    async def is_on_payroll(self, employee_id: int) -> bool:
        """False only when the employee's card says `profile.on_payroll = false` (C12)."""
        async with self.db_pool.acquire() as conn:
            value = await conn.fetchval(
                f"SELECT {ON_PAYROLL_SQL} FROM hr_employees e WHERE e.id = $1", employee_id
            )
        return value is not False

    async def get_employee(self, employee_id: int) -> dict[str, Any] | None:
        """Get employee by HR employee ID."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT e.*, tm.full_name, tm.email, tm.role
                FROM hr_employees e
                JOIN team_members tm ON tm.id = e.team_member_id
                WHERE e.id = $1
            """,
                employee_id,
            )
            return self._legacy_row(row) if row else None

    async def get_employee_by_email(self, email: str) -> dict[str, Any] | None:
        """Get employee by team member email."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT e.*, tm.full_name, tm.email, tm.role
                FROM hr_employees e
                JOIN team_members tm ON tm.id = e.team_member_id
                WHERE tm.email = $1
            """,
                email,
            )
            return self._legacy_row(row) if row else None

    async def upsert_employee(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create or update HR employee record."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO hr_employees (
                    team_member_id, hire_date, base_salary_idr,
                    bank_name, bank_account, bank_account_holder,
                    npwp, ptkp_status,
                    bpjs_kesehatan_number, bpjs_ketenagakerjaan_number,
                    notes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (team_member_id) DO UPDATE SET
                    hire_date = EXCLUDED.hire_date,
                    base_salary_idr = EXCLUDED.base_salary_idr,
                    bank_name = EXCLUDED.bank_name,
                    bank_account = EXCLUDED.bank_account,
                    bank_account_holder = EXCLUDED.bank_account_holder,
                    npwp = EXCLUDED.npwp,
                    ptkp_status = EXCLUDED.ptkp_status,
                    bpjs_kesehatan_number = EXCLUDED.bpjs_kesehatan_number,
                    bpjs_ketenagakerjaan_number = EXCLUDED.bpjs_ketenagakerjaan_number,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
                RETURNING *
            """,
                data["team_member_id"],
                data["hire_date"],
                data["base_salary_idr"],
                data.get("bank_name"),
                data.get("bank_account"),
                data.get("bank_account_holder"),
                data.get("npwp"),
                data.get("ptkp_status", "TK/0"),
                data.get("bpjs_kesehatan_number"),
                data.get("bpjs_ketenagakerjaan_number"),
                data.get("notes"),
            )
            logger.info(f"HR employee upserted: employee_id={row['id']}")
            return self._legacy_row(row)

    # ─── STAFF CARD (contract: STAFF_CARD.md) ───────────────────────

    # Explicit column list (no `e.*`, and no `notes`: HR free text is never on the card).
    _CARD_SELECT = f"""
        SELECT e.id, e.team_member_id, e.hire_date, e.is_active, e.ptkp_status,
               e.npwp, e.bpjs_kesehatan_number, e.bpjs_ketenagakerjaan_number,
               e.bank_name, e.bank_account, e.bank_account_holder, e.profile,
               e.base_salary_idr,
               tm.full_name, tm.role, tm.department, tm.avatar
        FROM hr_employees e
        JOIN team_members tm ON tm.id = e.team_member_id
        WHERE tm.id = $1 AND COALESCE(tm.role, '') <> 'client'
          AND (tm.active = true OR lower(trim(COALESCE(tm.role, ''))) IN ({GROUP_STAFF_ROLES_SQL}))
    """

    # $11 is a Python dict bound to jsonb: the pool codec encodes it exactly once. $12 is the
    # text[] of cleared keys (C11): a cleared column becomes NULL, a cleared profile key is
    # removed, `contract_number` removes `profile.contract.number`; empty for the import.
    _CARD_UPDATE_SQL = """
        UPDATE hr_employees SET
            hire_date = COALESCE($2, hire_date),
            npwp = CASE WHEN 'npwp' = ANY($12::text[]) THEN NULL ELSE COALESCE($3, npwp) END,
            ptkp_status = COALESCE($4, ptkp_status),
            bpjs_kesehatan_number = CASE WHEN 'bpjs_kesehatan_number' = ANY($12::text[]) THEN NULL ELSE COALESCE($5, bpjs_kesehatan_number) END,
            bpjs_ketenagakerjaan_number = CASE WHEN 'bpjs_ketenagakerjaan_number' = ANY($12::text[]) THEN NULL ELSE COALESCE($6, bpjs_ketenagakerjaan_number) END,
            bank_name = CASE WHEN 'bank_name' = ANY($12::text[]) THEN NULL ELSE COALESCE($7, bank_name) END,
            bank_account = CASE WHEN 'bank_account' = ANY($12::text[]) THEN NULL ELSE COALESCE($8, bank_account) END,
            bank_account_holder = CASE WHEN 'bank_account_holder' = ANY($12::text[]) THEN NULL ELSE COALESCE($9, bank_account_holder) END,
            is_active = COALESCE($10, is_active),
            profile = ((CASE WHEN jsonb_typeof(profile) = 'object' THEN profile ELSE '{}'::jsonb END)
                    - $12::text[])
                || $11::jsonb
                || CASE WHEN $11::jsonb ? 'docs' THEN jsonb_build_object(
                    'docs',
                    (CASE WHEN jsonb_typeof(profile->'docs') = 'object' THEN profile->'docs'
                          ELSE '{}'::jsonb END) || ($11::jsonb->'docs'))
                    ELSE '{}'::jsonb END
                || CASE WHEN $11::jsonb ? 'contract' OR ('contract_number' = ANY($12::text[])
                                          AND jsonb_typeof(profile->'contract') = 'object')
                    THEN jsonb_build_object(
                    'contract',
                    ((CASE WHEN jsonb_typeof(profile->'contract') = 'object' THEN profile->'contract'
                           ELSE '{}'::jsonb END)
                        - ARRAY(SELECT 'number' WHERE 'contract_number' = ANY($12::text[])))
                    || COALESCE($11::jsonb->'contract', '{}'::jsonb))
                    ELSE '{}'::jsonb END,
            updated_at = NOW()
        WHERE team_member_id = $1
        RETURNING id, jsonb_typeof(profile) AS profile_type
    """

    _CARD_INSERT_SQL = """
        INSERT INTO hr_employees (
            team_member_id, hire_date, base_salary_idr, npwp, ptkp_status,
            bpjs_kesehatan_number, bpjs_ketenagakerjaan_number,
            bank_name, bank_account, bank_account_holder, is_active, profile
        )
        SELECT tm.id, $2, 0, $3, COALESCE($4, 'TK/0'), $5, $6, $7, $8, $9,
               COALESCE($10, TRUE), $11::jsonb
        FROM team_members tm
        WHERE tm.id = $1
          AND NOT EXISTS (SELECT 1 FROM hr_employees x WHERE x.team_member_id = $1)
        ON CONFLICT (team_member_id) DO NOTHING
        RETURNING id, jsonb_typeof(profile) AS profile_type
    """

    async def get_card_for_member(self, team_member_id: str) -> dict[str, Any] | None:
        """The staff member's HR card, keyed by the team_members PRIMARY KEY (never an email).

        None when there is no hr_employees row (or the key is not a staff row).
        """
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(self._CARD_SELECT, team_member_id)
        except Exception:
            raise HRCardError(HRCardCode.DB_ERROR) from None
        return dict(row) if row else None

    @staticmethod
    def _checked_patch(
        fields: dict[str, Any], profile_patch: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Input checks that never put a value in an error: a non-object is rejected as is."""
        if not isinstance(fields, dict) or not isinstance(profile_patch, dict):
            raise HRCardError(HRCardCode.PROFILE_NOT_OBJECT)
        for key in ("docs", "contract"):
            if key in profile_patch and not isinstance(profile_patch[key], dict):
                raise HRCardError(HRCardCode.PROFILE_NOT_OBJECT)
        hire = fields.get("hire_date")
        if isinstance(hire, str):  # DATE parameters take date objects only
            try:
                fields = {**fields, "hire_date": date.fromisoformat(hire)}
            except ValueError:
                raise HRCardError(HRCardCode.INVALID_INPUT) from None
        elif hire is not None and not isinstance(hire, date):
            raise HRCardError(HRCardCode.INVALID_INPUT)
        return fields, profile_patch

    async def lock_staff_member(self, conn: Any, team_member_id: str, email: str) -> bool:
        """Lock the team_members row FOR SHARE, re-checking exact email and staff in the same
        statement. False when the row no longer matches; a lock timeout or deadlock is
        HRCardError(LOCK_TIMEOUT). Runs in a savepoint so a failed lock aborts nothing else."""
        try:
            async with conn.transaction():
                found = await conn.fetchval(
                    "SELECT id FROM team_members WHERE id = $1 AND email = $2 "
                    "AND COALESCE(role, '') <> 'client' FOR SHARE",
                    team_member_id,
                    email,
                )
        except _LOCK_ERRORS:
            raise HRCardError(HRCardCode.LOCK_TIMEOUT) from None
        except Exception:
            raise HRCardError(HRCardCode.DB_ERROR) from None
        return found is not None

    async def merge_card_on(
        self,
        conn: Any,
        team_member_id: str,
        fields: dict[str, Any],
        profile_patch: dict[str, Any],
        clear: Sequence[str] = (),
    ) -> int | None:
        """Non-clobbering card write on a caller-owned connection (the import batches many).

        `clear` (C11, the HTTP edit only) names columns to set NULL and profile keys to remove
        in the same statement; an insert has nothing to clear and ignores it.

        Returns the row id, or None when no row was written (no such member). A
        concurrent insert falls through to the update; a stored profile that is not
        an object after the merge fails and rolls this write back.
        """
        fields, profile_patch = self._checked_patch(fields, profile_patch)
        cleared = sorted({str(k) for k in clear})
        args = (
            team_member_id,
            fields.get("hire_date"),
            fields.get("npwp"),
            fields.get("ptkp_status"),
            fields.get("bpjs_kesehatan_number"),
            fields.get("bpjs_ketenagakerjaan_number"),
            fields.get("bank_name"),
            fields.get("bank_account"),
            fields.get("bank_account_holder"),
            fields.get("is_active"),
            profile_patch,  # a dict: the pool codec encodes it once
        )
        try:
            async with conn.transaction():
                row = await conn.fetchrow(self._CARD_UPDATE_SQL, *args, cleared)
                if row is None:
                    if fields.get("hire_date") is None:
                        raise HRCardError(HRCardCode.HIRE_DATE_REQUIRED)
                    row = await conn.fetchrow(self._CARD_INSERT_SQL, *args)
                    if row is None:  # a concurrent writer inserted the row: update it
                        row = await conn.fetchrow(self._CARD_UPDATE_SQL, *args, cleared)
                if row is not None and row["profile_type"] != "object":
                    raise HRCardError(HRCardCode.PROFILE_NOT_OBJECT)
        except HRCardError:
            raise
        except _LOCK_ERRORS:
            raise HRCardError(HRCardCode.LOCK_TIMEOUT) from None
        except Exception:
            raise HRCardError(HRCardCode.DB_ERROR) from None
        if row is None:
            return None
        logger.info(f"HR card merged: employee_id={row['id']}")
        return row["id"]

    async def lock_visible_person(self, conn: Any, team_member_id: str, email: str) -> bool:
        """Lock the team_members row FOR SHARE, re-checking in the SAME statement that the
        member is still a person the People console shows (the full `VISIBLE_PERSON_PREDICATE`:
        active, not client/monitoring/partner, with an HR row or a named staff role) and that the
        stored email is the one the route looked up. False when the row no longer qualifies.
        A savepoint keeps a failed lock from aborting the caller's transaction."""
        try:
            async with conn.transaction():
                found = await conn.fetchval(_lock_visible_person_sql(), team_member_id, email)
        except _LOCK_ERRORS:
            raise HRCardError(HRCardCode.LOCK_TIMEOUT) from None
        except Exception:
            raise HRCardError(HRCardCode.DB_ERROR) from None
        return found is not None

    async def edit_card(
        self,
        team_member_id: str,
        email: str,
        fields: dict[str, Any],
        profile_patch: dict[str, Any],
        clear: Sequence[str],
    ) -> int | None:
        """One HR admin edit of one card (C11): lock the member, merge and clear, one transaction.

        Same `lock_timeout` as the import (C8), same merge as C5. The member lock re-checks the
        People visibility predicate, so a member deactivated or turned monitoring/partner after
        the route's lookup is not written. Returns the row id, or None when the person no longer
        qualifies. Failures are HRCardError codes; no value reaches any message or log.
        """
        try:
            async with self.db_pool.acquire() as conn, conn.transaction():
                await conn.execute(f"SET LOCAL lock_timeout = {int(CARD_EDIT_LOCK_TIMEOUT_MS)}")
                # One lock order with the HR document door (STAFF_CARD.md C13): the per-member
                # advisory lock first, then the row lock, so a unit change cannot land between the
                # door's unit read and its folder move.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", team_member_id
                )
                if not await self.lock_visible_person(conn, team_member_id, email):
                    return None
                return await self.merge_card_on(conn, team_member_id, fields, profile_patch, clear)
        except HRCardError:
            raise
        except _LOCK_ERRORS:
            raise HRCardError(HRCardCode.LOCK_TIMEOUT) from None
        except Exception:
            raise HRCardError(HRCardCode.DB_ERROR) from None

    # ─── BONUS RATES ─────────────────────────────────────────────────

    async def list_bonus_rates(self) -> list[dict[str, Any]]:
        """List all bonus rates."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT br.*, pt.name as practice_type_name
                FROM hr_bonus_rates br
                LEFT JOIN practice_types pt ON pt.code = br.practice_type_code
                ORDER BY br.practice_type_code
            """)
            return [dict(r) for r in rows]

    async def upsert_bonus_rate(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create or update a bonus rate."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO hr_bonus_rates (
                    practice_type_code, amount_idr, effective_from,
                    effective_to, is_active, notes
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (practice_type_code) DO UPDATE SET
                    amount_idr = EXCLUDED.amount_idr,
                    effective_from = EXCLUDED.effective_from,
                    effective_to = EXCLUDED.effective_to,
                    is_active = EXCLUDED.is_active,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
                RETURNING *
            """,
                data["practice_type_code"],
                data["amount_idr"],
                data.get("effective_from") or business_today(),
                data.get("effective_to"),
                data.get("is_active", True),
                data.get("notes"),
            )
            logger.info(
                "Bonus rate upserted: rate_id=%s",
                row["id"],
            )
            return dict(row)

    # ─── BONUSES ─────────────────────────────────────────────────────

    async def list_bonuses(
        self,
        employee_id: int | None = None,
        status: str | None = None,
        month: int | None = None,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        """List bonus ledger entries with filters."""
        async with self.db_pool.acquire() as conn:
            query = """
                SELECT bl.*, tm.full_name as employee_name, tm.email as employee_email,
                       tm.department as employee_department,
                       p.status as practice_status, c.full_name as client_name
                FROM hr_bonus_ledger bl
                JOIN hr_employees e ON e.id = bl.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                LEFT JOIN practices p ON p.id = bl.practice_id
                LEFT JOIN clients c ON c.id = p.client_id
                WHERE 1=1
            """
            params: list[Any] = []
            idx = 1

            if employee_id is not None:
                query += f" AND bl.employee_id = ${idx}"
                params.append(employee_id)
                idx += 1

            if status is not None:
                query += f" AND bl.status = ${idx}"
                params.append(status)
                idx += 1

            if month is not None and year is not None:
                query += (
                    " AND EXTRACT(MONTH FROM"
                    f" (bl.awarded_at AT TIME ZONE 'Asia/Makassar')) = ${idx}"
                )
                params.append(month)
                idx += 1
                query += (
                    f" AND EXTRACT(YEAR FROM (bl.awarded_at AT TIME ZONE 'Asia/Makassar')) = ${idx}"
                )
                params.append(year)
                idx += 1

            query += " ORDER BY bl.awarded_at DESC"
            rows = await conn.fetch(query, *params)
            out = []
            for r in rows:
                row = dict(r)
                row["employee_department"] = department_of(
                    row.get("employee_email"), row.get("employee_department")
                )
                out.append(row)
            return out

    async def list_bonus_historical(
        self,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        """List the pre-system bonus recaps imported from PDF (migration 099).

        These rows are an accounting record of what was paid BEFORE the bonus
        ledger existed. They are a SEPARATE source from ``hr_bonus_ledger`` and
        the two overlap on some months with different totals — callers must
        present them side by side for reconciliation and must NEVER sum them.
        """
        async with self.db_pool.acquire() as conn:
            query = """
                SELECT h.id, h.employee_name, h.employee_id,
                       h.bonus_month, h.bonus_year, h.total_amount_idr,
                       h.task_count, h.source_pdf, h.accounting_total_data,
                       h.accounting_not_paid, h.accounting_paid,
                       h.imported_at, h.notes
                FROM hr_bonus_historical h
            """
            params: list[Any] = []
            if year is not None:
                query += " WHERE h.bonus_year = $1"
                params.append(year)

            query += " ORDER BY h.bonus_year DESC, h.bonus_month DESC, h.employee_name"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def approve_bonus(self, bonus_id: int, approved_by: str) -> dict[str, Any]:
        """Approve a pending bonus, unless its payroll month is already approved or paid, its
        employee is outside the payroll set (OffPayrollError) or the approver is the employee
        it is for (SelfApprovalError, a ValueError)."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await _lock_bonus_allocations(conn, exclusive=False)
                cur = await conn.fetchrow(
                    """
                    SELECT status, employee_id, deferred_from,
                           (SELECT b.team_member_id FROM hr_employees b
                             WHERE b.id = hr_bonus_ledger.employee_id) AS beneficiary,
                           EXTRACT(MONTH FROM (awarded_at AT TIME ZONE 'Asia/Makassar'))::int AS m,
                           EXTRACT(YEAR FROM (awarded_at AT TIME ZONE 'Asia/Makassar'))::int AS y
                    FROM hr_bonus_ledger WHERE id = $1 FOR UPDATE
                    """,
                    bonus_id,
                )
                if not cur or cur["status"] != "pending":
                    msg = f"Bonus {bonus_id} not found or not pending"
                    raise ValueError(msg)
                await _lock_payroll_month(conn, cur["m"], cur["y"], exclusive=False)
                if not cur.get("deferred_from"):
                    await _refuse_if_payroll_closed(conn, cur["m"], cur["y"])
                in_set = await conn.fetchval(
                    f"SELECT {payroll_set_sql()} FROM hr_employees e "
                    "JOIN team_members tm ON tm.id = e.team_member_id "
                    "WHERE e.id = $1 FOR SHARE OF e, tm",
                    cur["employee_id"],
                )
                if in_set is not True:
                    msg = "This person is not on payroll"
                    raise OffPayrollError(msg)
                beneficiary = cur.get("beneficiary")  # the login the bonus is for, if it has a card
                if beneficiary is not None and str(beneficiary) == str(approved_by):
                    msg = "A bonus needs an approver other than the person it is for"
                    raise SelfApprovalError(msg)
                row = await conn.fetchrow(
                    """
                    UPDATE hr_bonus_ledger
                    SET status = 'approved', approved_by = $2, approved_at = NOW()
                    WHERE id = $1 AND status = 'pending'
                    RETURNING *
                    """,
                    bonus_id,
                    approved_by,
                )
            logger.info("Bonus %s approved", bonus_id)
            return dict(row)

    # ─── MANUAL BONUSES ──────────────────────────────────────────────

    async def list_bonus_practice_types(self) -> list[dict[str, Any]]:
        """Active practice types for the rate / manual-bonus dropdowns, tax included."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT code, name, category
                FROM practice_types
                WHERE is_active = TRUE
                ORDER BY code
            """)
        out = [
            {
                "code": r["code"],
                "name": r["name"],
                "category": r["category"],
                "group": practice_group(r["code"], r["category"]),
            }
            for r in rows
        ]
        order = {"visa": 0, "company": 1, "tax": 2, "other": 3}
        out.sort(key=lambda t: (order[t["group"]], str(t["name"] or "").lower()))
        return out

    async def create_manual_bonus(
        self,
        *,
        employee_id: int,
        amount_idr: int,
        reason: str,
        practice_type_code: str | None,
        awarded_on: date,
        created_by: str,
    ) -> dict[str, Any] | None:
        """Insert a pending manual bonus. None when the employee is unknown or inactive.

        Raises ValueError for an unknown service code, OffPayrollError when the employee's
        card says they are not on payroll, and BonusPeriodLockedError when the payroll month
        of `awarded_on` is already approved or paid.
        """
        awarded_at = datetime.combine(awarded_on, time(12, 0), tzinfo=ZoneInfo(BUSINESS_TZ))
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await _lock_bonus_allocations(conn, exclusive=False)
                emp = await conn.fetchrow(
                    f"SELECT e.id, {payroll_set_sql()} AS on_payroll "
                    "FROM hr_employees e JOIN team_members tm ON tm.id = e.team_member_id "
                    "WHERE e.id = $1 AND e.is_active = TRUE",
                    employee_id,
                )
                if not emp:
                    return None
                if not emp["on_payroll"]:
                    msg = "This person is not on payroll"
                    raise OffPayrollError(msg)
                if practice_type_code:
                    known = await conn.fetchval(
                        "SELECT 1 FROM practice_types WHERE code = $1 AND is_active = TRUE",
                        practice_type_code,
                    )
                    if not known:
                        msg = "Unknown or inactive service"
                        raise ValueError(msg)
                await _lock_payroll_month(conn, awarded_on.month, awarded_on.year, exclusive=False)
                await _refuse_if_payroll_closed(conn, awarded_on.month, awarded_on.year)
                row = await conn.fetchrow(
                    """
                    INSERT INTO hr_bonus_ledger (
                        employee_id, practice_type_code, amount_idr, status,
                        awarded_at, source, reason, created_by
                    ) VALUES ($1, $2, $3, 'pending', $4, 'manual', $5, $6)
                    RETURNING *
                    """,
                    employee_id,
                    practice_type_code or MANUAL_BONUS_CODE,
                    amount_idr,
                    awarded_at,
                    reason.strip(),
                    created_by,
                )
        logger.info("Manual bonus %s created for employee %s", row["id"], employee_id)
        return dict(row)

    async def reject_bonus(
        self, bonus_id: int, rejected_by: str, note: str | None = None
    ) -> dict[str, Any]:
        """Reject a pending bonus (practice or manual).

        One statement decides: LookupError when no bonus has that id, ValueError when it
        exists but is not pending. The existence probe shares the UPDATE's snapshot.
        """
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await _lock_bonus_allocations(conn, exclusive=False)
                row = await conn.fetchrow(
                    """
                    WITH upd AS (
                        UPDATE hr_bonus_ledger
                        SET status = 'rejected', approved_by = $2, approved_at = NOW(),
                            notes = COALESCE($3, notes)
                        WHERE id = $1 AND status = 'pending'
                        RETURNING *
                    )
                    SELECT upd.*, EXISTS (SELECT 1 FROM hr_bonus_ledger WHERE id = $1) AS _existed
                    FROM (SELECT 1) AS one LEFT JOIN upd ON TRUE
                    """,
                    bonus_id,
                    rejected_by,
                    note,
                )
            if row is None or row["id"] is None:
                if row is not None and row["_existed"]:
                    msg = f"Bonus {bonus_id} is not pending"
                    raise ValueError(msg)
                msg = f"Bonus {bonus_id} not found"
                raise LookupError(msg)
            logger.info("Bonus %s rejected", bonus_id)
            out = dict(row)
            out.pop("_existed", None)
            return out

    async def delete_manual_bonus(self, bonus_id: int) -> str:
        """Delete a pending manual bonus: 'deleted', 'missing' (no such id) or 'conflict'
        (exists but is not a pending manual bonus). One statement, one snapshot."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await _lock_bonus_allocations(conn, exclusive=False)
                row = await conn.fetchrow(
                    """
                    WITH del AS (
                        DELETE FROM hr_bonus_ledger
                        WHERE id = $1 AND source = 'manual' AND status = 'pending'
                        RETURNING id
                    )
                    SELECT (SELECT id FROM del) AS deleted_id,
                           EXISTS (SELECT 1 FROM hr_bonus_ledger WHERE id = $1) AS existed
                    """,
                    bonus_id,
                )
        if row["deleted_id"] is not None:
            return "deleted"
        return "conflict" if row["existed"] else "missing"

    async def get_bonus_summary(
        self,
        employee_id: int,
        month: int,
        year: int,
    ) -> dict[str, Any]:
        """Get bonus summary for an employee in a given month."""
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as bonus_count,
                    COALESCE(SUM(amount_idr) FILTER (WHERE status IN ('approved','paid')), 0) as approved_total,
                    COALESCE(SUM(amount_idr) FILTER (WHERE status = 'pending'), 0) as pending_total,
                    COALESCE(SUM(amount_idr), 0) as grand_total
                FROM hr_bonus_ledger
                WHERE employee_id = $1
                  AND status NOT IN ('rejected', 'reversed', 'cancelled')
                  AND EXTRACT(MONTH FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $2
                  AND EXTRACT(YEAR FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $3
            """,
                employee_id,
                month,
                year,
            )
            return dict(row)

    # ─── PAYROLL ─────────────────────────────────────────────────────

    async def calculate_payroll(
        self,
        month: int,
        year: int,
        created_by: str,
    ) -> dict[str, Any]:
        """
        Calculate payroll for a given month.
        Creates period + payslips + deductions for all active employees that are on payroll
        (card `profile.on_payroll` is not false). Payslips of this period that belong to
        anyone outside that set (inactive login, client/monitoring/partner role, inactive card or
        off-payroll card) are deleted in the same transaction, unless the period was
        already approved or paid when this call found it.
        """
        period_start = date(year, month, 1)
        as_of = business_today()
        last_day = calendar.monthrange(year, month)[1]
        period_end = date(year, month, last_day)

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await _lock_bonus_allocations(conn, exclusive=True)
                await _lock_payroll_month(conn, month, year, exclusive=True)
                prior_status = await conn.fetchval(
                    "SELECT status FROM hr_payroll_periods "
                    "WHERE payroll_month = $1 AND payroll_year = $2",
                    month,
                    year,
                )
                # Create or get period
                period = await conn.fetchrow(
                    """
                    INSERT INTO hr_payroll_periods (
                        payroll_month, payroll_year, period_start, period_end,
                        status, created_by
                    ) VALUES ($1, $2, $3, $4, 'calculated', $5)
                    ON CONFLICT (payroll_month, payroll_year) DO UPDATE SET
                        status = 'calculated', updated_at = NOW()
                    RETURNING *
                """,
                    month,
                    year,
                    period_start,
                    period_end,
                    created_by,
                )
                period_id = period["id"]

                # Get all active employees that are on payroll
                employees = await conn.fetch(f"""
                    SELECT e.*, tm.full_name, tm.email
                    FROM hr_employees e
                    JOIN team_members tm ON tm.id = e.team_member_id
                    WHERE {payroll_set_sql()}
                    FOR SHARE OF e, tm
                """)  # share locks: a concurrent card or login edit waits for this commit

                payslips = []
                for emp in employees:
                    salary = emp["base_salary_idr"]
                    emp_id = emp["id"]
                    ptkp = emp["ptkp_status"]

                    bonus_row = await conn.fetchrow(
                        """SELECT COALESCE(SUM(amount_idr), 0) AS total
                           FROM hr_payroll_bonus_inputs($2, $3, $4)
                           WHERE employee_id=$1 AND status='approved' AND NOT held""",
                        emp_id,
                        period_start,
                        period_id,
                        as_of,
                    )
                    # SUM(bigint) is numeric: asyncpg returns Decimal, which the float tax maths rejects.
                    bonus_total = int(bonus_row["total"])

                    # Calculate BPJS
                    bpjs = calculate_bpjs(salary)

                    # Employee deductions
                    ded_kes = bpjs["kesehatan"]["employee"]
                    ded_jht = bpjs["jht"]["employee"]
                    ded_jp = bpjs["jp"]["employee"]
                    pph21 = calculate_pph21_monthly(salary + bonus_total, ptkp)
                    deduction_total = ded_kes + ded_jht + ded_jp + pph21

                    # Net salary
                    net = salary + bonus_total - deduction_total

                    # Delete existing payslip for recalculation
                    await conn.execute(
                        """
                        DELETE FROM hr_deductions WHERE payslip_id IN (
                            SELECT id FROM hr_payslips
                            WHERE employee_id = $1 AND payroll_period_id = $2
                        )
                    """,
                        emp_id,
                        period_id,
                    )
                    await conn.execute(
                        """
                        DELETE FROM hr_payslips
                        WHERE employee_id = $1 AND payroll_period_id = $2
                    """,
                        emp_id,
                        period_id,
                    )

                    # Insert payslip
                    payslip = await conn.fetchrow(
                        """
                        INSERT INTO hr_payslips (
                            payroll_period_id, employee_id, base_salary_idr,
                            bonus_total_idr, deduction_total_idr, net_salary_idr
                        ) VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING *
                    """,
                        period_id,
                        emp_id,
                        salary,
                        bonus_total,
                        deduction_total,
                        net,
                    )
                    payslip_id = payslip["id"]

                    # Insert deduction items
                    deduction_items = [
                        ("bpjs_kes_employee", "BPJS Kesehatan (1%)", ded_kes, False),
                        (
                            "bpjs_kes_employer",
                            "BPJS Kesehatan (4%)",
                            bpjs["kesehatan"]["employer"],
                            True,
                        ),
                        ("bpjs_jht_employee", "BPJS JHT (2%)", ded_jht, False),
                        ("bpjs_jht_employer", "BPJS JHT (3.7%)", bpjs["jht"]["employer"], True),
                        ("bpjs_jkk", "BPJS JKK (0.24%)", bpjs["jkk"]["employer"], True),
                        ("bpjs_jkm", "BPJS JKM (0.30%)", bpjs["jkm"]["employer"], True),
                        ("bpjs_jp_employee", "BPJS JP (1%)", ded_jp, False),
                        ("bpjs_jp_employer", "BPJS JP (2%)", bpjs["jp"]["employer"], True),
                        ("pph21", "PPh21", pph21, False),
                    ]
                    for dtype, label, amount, is_employer in deduction_items:
                        if amount > 0:
                            await conn.execute(
                                """
                                INSERT INTO hr_deductions (
                                    payslip_id, deduction_type, label, amount_idr, is_employer
                                ) VALUES ($1, $2, $3, $4, $5)
                            """,
                                payslip_id,
                                dtype,
                                label,
                                amount,
                                is_employer,
                            )

                    payslips.append(
                        {
                            "employee_name": emp["full_name"],
                            "base_salary": salary,
                            "bonus_total": bonus_total,
                            "deduction_total": deduction_total,
                            "net_salary": net,
                        }
                    )

                if prior_status not in ("approved", "paid"):
                    kept = [emp["id"] for emp in employees]
                    await conn.execute(
                        """
                        DELETE FROM hr_deductions WHERE payslip_id IN (
                            SELECT id FROM hr_payslips
                            WHERE payroll_period_id = $1 AND employee_id <> ALL($2::int[])
                        )
                    """,
                        period_id,
                        kept,
                    )
                    await conn.execute(
                        """
                        DELETE FROM hr_payslips
                        WHERE payroll_period_id = $1 AND employee_id <> ALL($2::int[])
                    """,
                        period_id,
                        kept,
                    )

                logger.info(f"Payroll calculated: {month}/{year}, {len(payslips)} employees")
                return {
                    "period_id": period_id,
                    "month": month,
                    "year": year,
                    "employee_count": len(payslips),
                    "payslips": payslips,
                }

    async def list_payroll_periods(self) -> list[dict[str, Any]]:
        """List all payroll periods."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT pp.*,
                    COUNT(ps.id) as payslip_count,
                    COALESCE(SUM(ps.net_salary_idr), 0) as total_net
                FROM hr_payroll_periods pp
                LEFT JOIN hr_payslips ps ON ps.payroll_period_id = pp.id
                GROUP BY pp.id
                ORDER BY pp.payroll_year DESC, pp.payroll_month DESC
            """)
            return [dict(r) for r in rows]

    async def get_payslips(
        self,
        employee_id: int | None = None,
        period_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get payslips with filters."""
        async with self.db_pool.acquire() as conn:
            query = """
                SELECT ps.*, tm.full_name as employee_name, tm.email as employee_email,
                       pp.payroll_month, pp.payroll_year, pp.status as period_status
                FROM hr_payslips ps
                JOIN hr_employees e ON e.id = ps.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                JOIN hr_payroll_periods pp ON pp.id = ps.payroll_period_id
                WHERE 1=1
            """
            params: list[Any] = []
            idx = 1

            if employee_id is not None:
                query += f" AND ps.employee_id = ${idx}"
                params.append(employee_id)
                idx += 1
            if period_id is not None:
                query += f" AND ps.payroll_period_id = ${idx}"
                params.append(period_id)
                idx += 1

            query += " ORDER BY pp.payroll_year DESC, pp.payroll_month DESC"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def get_payslip_detail(self, payslip_id: int) -> dict[str, Any] | None:
        """Get full payslip with deductions."""
        async with self.db_pool.acquire() as conn:
            payslip = await conn.fetchrow(
                """
                SELECT ps.*, tm.full_name, tm.email,
                       pp.payroll_month, pp.payroll_year, pp.status as period_status
                FROM hr_payslips ps
                JOIN hr_employees e ON e.id = ps.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                JOIN hr_payroll_periods pp ON pp.id = ps.payroll_period_id
                WHERE ps.id = $1
            """,
                payslip_id,
            )
            if not payslip:
                return None

            deductions = await conn.fetch(
                """
                SELECT * FROM hr_deductions WHERE payslip_id = $1
                ORDER BY is_employer, deduction_type
            """,
                payslip_id,
            )

            result = dict(payslip)
            result["deductions"] = [dict(d) for d in deductions]
            return result

    async def approve_payroll(self, period_id: int, approved_by: str) -> dict[str, Any]:
        """Approve a payroll period (locks it).

        Reserve payable bonuses and date each deferral on approval. If a rate, hold or
        roster changed since calculation, the operator recalculates the open period.
        """
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                # Lock order: carry allocation, due obligations in practice-id order, month,
                # then period. Archiving a permit can reopen a previously fulfilled obligation,
                # so protect both directions through the drift check and payout allocation.
                as_of = business_today()
                await _lock_bonus_allocations(conn, exclusive=True)
                await conn.execute(
                    """SELECT f.practice_id FROM practice_visa_followups f
                       JOIN hr_delivery_bonus_events e ON e.practice_id=f.practice_id
                       WHERE f.due_on <= $1 OR EXISTS (
                           SELECT 1 FROM hr_visa_bonus_hold_periods h WHERE h.practice_id=f.practice_id
                       )
                       ORDER BY f.practice_id FOR SHARE OF f, e""",
                    as_of,
                )
                seen = await conn.fetchrow(
                    "SELECT payroll_month, payroll_year FROM hr_payroll_periods WHERE id = $1",
                    period_id,
                )
                if not seen:
                    msg = f"Period {period_id} not found or not in 'calculated' status"
                    raise ValueError(msg)
                await _lock_payroll_month(
                    conn, seen["payroll_month"], seen["payroll_year"], exclusive=True
                )
                period = await conn.fetchrow(
                    "SELECT * FROM hr_payroll_periods WHERE id = $1 FOR UPDATE", period_id
                )
                if (
                    not period
                    or period["status"] != "calculated"
                    or (period["payroll_month"], period["payroll_year"])
                    != (seen["payroll_month"], seen["payroll_year"])
                ):
                    msg = f"Period {period_id} not found or not in 'calculated' status"
                    raise ValueError(msg)
                target_month = date(period["payroll_year"], period["payroll_month"], 1)
                drift = await conn.fetchval(
                    f"""
                    SELECT e.id FROM hr_employees e
                    JOIN team_members tm ON tm.id = e.team_member_id
                    LEFT JOIN hr_payslips ps
                           ON ps.employee_id = e.id AND ps.payroll_period_id = $1
                    WHERE {payroll_set_sql()}
                      AND COALESCE(ps.bonus_total_idr, 0) <> COALESCE((
                            SELECT SUM(b.amount_idr) FROM hr_payroll_bonus_inputs($2,$1,$3) b
                            WHERE b.employee_id=e.id AND b.status='approved' AND NOT b.held
                          ), 0)
                    LIMIT 1
                    """,
                    period_id,
                    target_month,
                    as_of,
                )
                if drift is not None:
                    msg = (
                        "Bonuses changed since this payroll was calculated; "
                        "recalculate before approving"
                    )
                    raise PayrollBonusesChangedError(msg)
                roster = await conn.fetch(
                    f"""
                    SELECT e.id, {payroll_set_sql()} AS in_set
                    FROM hr_employees e JOIN team_members tm ON tm.id = e.team_member_id
                    FOR SHARE OF e, tm
                    """
                )
                owners = await conn.fetch(
                    "SELECT employee_id FROM hr_payslips WHERE payroll_period_id = $1", period_id
                )
                if {r["id"] for r in roster if r["in_set"]} != {r["employee_id"] for r in owners}:
                    msg = "The team changed since this payroll was calculated: calculate again"
                    raise PayrollRosterChangedError(msg)
                await conn.execute(
                    """INSERT INTO hr_visa_bonus_hold_periods(practice_id,payroll_period_id)
                       SELECT h.practice_id,$1 FROM hr_visa_holds_for_period($2,$1,$3) h
                       JOIN hr_payslips ps ON ps.payroll_period_id=$1 AND ps.employee_id=h.employee_id
                       ON CONFLICT (practice_id) DO NOTHING""",
                    period_id,
                    target_month,
                    as_of,
                )
                await conn.execute(
                    """INSERT INTO hr_bonus_deferral_causes(bonus_id,practice_id)
                       SELECT i.bonus_id,h.practice_id
                       FROM hr_payroll_bonus_inputs($2,$1,$3) i
                       JOIN hr_visa_holds_for_period($2,$1,$3) h USING(employee_id)
                       JOIN hr_payslips ps ON ps.payroll_period_id=$1 AND ps.employee_id=i.employee_id
                       WHERE NOT EXISTS (
                           SELECT 1 FROM hr_bonus_deferral_causes c
                           JOIN hr_visa_bonus_obligations o USING(practice_id)
                           WHERE c.bonus_id=i.bonus_id AND o.bound_period_id <> $1
                       ) ON CONFLICT DO NOTHING""",
                    period_id,
                    target_month,
                    as_of,
                )
                # Reserve exactly the approved rows behind the slip. A held pending/approved
                # row has no payout period and retains its original award date for HR reporting.
                await conn.execute(
                    """UPDATE hr_bonus_ledger b
                       SET payroll_period_id=CASE WHEN i.held THEN NULL ELSE $1 END,
                           deferred_from=CASE WHEN i.held THEN COALESCE(b.deferred_from,$2)
                                              ELSE b.deferred_from END
                       FROM hr_payroll_bonus_inputs($2,$1,$3) i
                       JOIN hr_payslips ps ON ps.payroll_period_id=$1 AND ps.employee_id=i.employee_id
                       WHERE b.id=i.bonus_id AND (i.held OR i.status='approved')""",
                    period_id,
                    target_month,
                    as_of,
                )
                row = await conn.fetchrow(
                    """
                    UPDATE hr_payroll_periods
                    SET status = 'approved', approved_by = $2
                    WHERE id = $1 AND status = 'calculated'
                    RETURNING *
                    """,
                    period_id,
                    approved_by,
                )
            logger.info("Payroll period %s approved", period_id)
            return dict(row)

    async def mark_payroll_paid(self, period_id: int) -> dict[str, Any]:
        """Mark payroll as paid."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                await _lock_bonus_allocations(conn, exclusive=True)
                seen = await conn.fetchrow(
                    "SELECT payroll_month, payroll_year FROM hr_payroll_periods WHERE id = $1",
                    period_id,
                )
                if seen:
                    await _lock_payroll_month(
                        conn, seen["payroll_month"], seen["payroll_year"], exclusive=True
                    )
                row = await conn.fetchrow(
                    """
                    UPDATE hr_payroll_periods
                    SET status = 'paid'
                    WHERE id = $1 AND status = 'approved'
                    RETURNING *
                """,
                    period_id,
                )
                if not row:
                    msg = f"Period {period_id} not found or not approved"
                    raise ValueError(msg)

                # Mark all bonuses in this period as paid
                await conn.execute(
                    """
                    UPDATE hr_bonus_ledger
                    SET status = 'paid'
                    WHERE payroll_period_id = $1 AND status = 'approved'
                """,
                    period_id,
                )

                logger.info("Payroll period %s marked as paid", period_id)
                return dict(row)

    # ─── LEAVE ───────────────────────────────────────────────────────

    async def request_leave(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a leave request."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                # Validate leave_type_id exists and is active (server-side guard
                # against clients bypassing the dropdown filter).
                leave_type = await conn.fetchrow(
                    """
                    SELECT id FROM hr_leave_types
                    WHERE id = $1 AND is_active = TRUE
                """,
                    data["leave_type_id"],
                )
                if not leave_type:
                    msg = f"Leave type {data['leave_type_id']} not found or inactive"
                    raise ValueError(msg)

                # Check balance
                balance = await conn.fetchrow(
                    """
                    SELECT * FROM hr_leave_balances
                    WHERE employee_id = $1
                      AND leave_type_id = $2
                      AND balance_year = $3
                """,
                    data["employee_id"],
                    data["leave_type_id"],
                    data["start_date"].year,
                )

                if balance:
                    available = (
                        balance["allocated_days"]
                        + balance["carried_over"]
                        - balance["used_days"]
                        - balance["pending_days"]
                    )
                    if data["total_days"] > available:
                        msg = f"Insufficient leave balance: {available} days available, {data['total_days']} requested"
                        raise ValueError(msg)

                    # Reserve pending days
                    await conn.execute(
                        """
                        UPDATE hr_leave_balances
                        SET pending_days = pending_days + $1
                        WHERE id = $2
                    """,
                        data["total_days"],
                        balance["id"],
                    )

                row = await conn.fetchrow(
                    """
                    INSERT INTO hr_leave_requests (
                        employee_id, leave_type_id, start_date, end_date,
                        total_days, reason
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING *
                """,
                    data["employee_id"],
                    data["leave_type_id"],
                    data["start_date"],
                    data["end_date"],
                    data["total_days"],
                    data.get("reason"),
                )
                logger.info(
                    f"Leave request created: employee={data['employee_id']}, days={data['total_days']}"
                )
                return dict(row)

    async def approve_leave(self, request_id: int, reviewed_by: str) -> dict[str, Any]:
        """Approve a leave request."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                req = await conn.fetchrow(
                    """
                    UPDATE hr_leave_requests
                    SET status = 'approved', reviewed_by = $2, reviewed_at = NOW()
                    WHERE id = $1 AND status = 'pending'
                    RETURNING *
                """,
                    request_id,
                    reviewed_by,
                )
                if not req:
                    msg = f"Leave request {request_id} not found or not pending"
                    raise ValueError(msg)

                # Move from pending to used
                await conn.execute(
                    """
                    UPDATE hr_leave_balances
                    SET pending_days = pending_days - $1,
                        used_days = used_days + $1
                    WHERE employee_id = $2
                      AND leave_type_id = $3
                      AND balance_year = $4
                """,
                    req["total_days"],
                    req["employee_id"],
                    req["leave_type_id"],
                    req["start_date"].year,
                )

                logger.info("Leave request %s approved", request_id)
                return dict(req)

    async def reject_leave(
        self,
        request_id: int,
        reviewed_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """Reject a leave request."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
                req = await conn.fetchrow(
                    """
                    UPDATE hr_leave_requests
                    SET status = 'rejected', reviewed_by = $2,
                        reviewed_at = NOW(), rejection_reason = $3
                    WHERE id = $1 AND status = 'pending'
                    RETURNING *
                """,
                    request_id,
                    reviewed_by,
                    reason,
                )
                if not req:
                    msg = f"Leave request {request_id} not found or not pending"
                    raise ValueError(msg)

                # Release pending days
                await conn.execute(
                    """
                    UPDATE hr_leave_balances
                    SET pending_days = GREATEST(0, pending_days - $1)
                    WHERE employee_id = $2
                      AND leave_type_id = $3
                      AND balance_year = $4
                """,
                    req["total_days"],
                    req["employee_id"],
                    req["leave_type_id"],
                    req["start_date"].year,
                )

                logger.info("Leave request %s rejected", request_id)
                return dict(req)

    async def get_leave_request(
        self,
        request_id: int,
    ) -> dict[str, Any] | None:
        """Fetch a single leave request with requester email + leave type.

        Returns None if the request does not exist. Used by the router to
        perform delegated RBAC checks before approve/reject.
        """
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT lr.*,
                       tm.email AS requester_email,
                       tm.full_name AS requester_name,
                       lt.name AS leave_type_name
                FROM hr_leave_requests lr
                JOIN hr_employees e ON e.id = lr.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                JOIN hr_leave_types lt ON lt.id = lr.leave_type_id
                WHERE lr.id = $1
            """,
                request_id,
            )
        return dict(row) if row else None

    async def get_leave_type_name(self, type_id: int) -> str:
        """Return the human-readable name of a leave type.

        Falls back to "Leave type #N" if the id is not found (defensive:
        should not happen given FK constraints, but keeps the notifier
        independent of strict row presence).
        """
        async with self.db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name FROM hr_leave_types WHERE id = $1",
                type_id,
            )
        return row["name"] if row else f"Leave type #{type_id}"

    async def get_leave_balance(
        self,
        employee_id: int,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get leave balances for an employee."""
        if year is None:
            year = date.today().year
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT lb.*, lt.code, lt.name as leave_type_name, lt.is_paid
                FROM hr_leave_balances lb
                JOIN hr_leave_types lt ON lt.id = lb.leave_type_id
                WHERE lb.employee_id = $1 AND lb.balance_year = $2
                ORDER BY lt.code
            """,
                employee_id,
                year,
            )
            return [dict(r) for r in rows]

    async def get_team_leave_summary(
        self,
        year: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get leave balance summary for all active employees."""
        if year is None:
            year = date.today().year
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tm.full_name as employee_name, tm.email,
                       he.id as employee_id,
                       lt.code as leave_type, lt.name as leave_type_name,
                       lb.allocated_days, lb.used_days, lb.pending_days,
                       lb.carried_over,
                       (lb.allocated_days + COALESCE(lb.carried_over, 0)
                        - lb.used_days - lb.pending_days) AS remaining_days
                FROM hr_leave_balances lb
                JOIN hr_employees he ON he.id = lb.employee_id
                JOIN team_members tm ON tm.id = he.team_member_id
                JOIN hr_leave_types lt ON lt.id = lb.leave_type_id
                WHERE he.is_active = TRUE AND lb.balance_year = $1
                ORDER BY tm.full_name, lt.code
            """,
                year,
            )
            return [dict(r) for r in rows]

    async def list_leave_requests(
        self,
        employee_id: int | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List leave requests with filters."""
        async with self.db_pool.acquire() as conn:
            query = """
                SELECT lr.*, tm.full_name as employee_name, tm.email as employee_email,
                       lt.name as leave_type_name
                FROM hr_leave_requests lr
                JOIN hr_employees e ON e.id = lr.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                JOIN hr_leave_types lt ON lt.id = lr.leave_type_id
                WHERE 1=1
            """
            params: list[Any] = []
            idx = 1

            if employee_id is not None:
                query += f" AND lr.employee_id = ${idx}"
                params.append(employee_id)
                idx += 1
            if status is not None:
                query += f" AND lr.status = ${idx}"
                params.append(status)
                idx += 1

            query += " ORDER BY lr.requested_at DESC"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    async def list_leave_requests_scoped(
        self,
        own_employee_id: int | None,
        requester_emails: list[str],
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Leave requests of one employee plus those of the given requester emails (an approver's view).

        Same leave-only columns as `list_leave_requests`; no payroll or employee-finance column is joined.
        """
        async with self.db_pool.acquire() as conn:
            query = """
                SELECT lr.*, tm.full_name as employee_name, tm.email as employee_email,
                       lt.name as leave_type_name
                FROM hr_leave_requests lr
                JOIN hr_employees e ON e.id = lr.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                JOIN hr_leave_types lt ON lt.id = lr.leave_type_id
                WHERE (lr.employee_id = $1 OR lower(tm.email) = ANY($2::text[]))
            """
            params: list[Any] = [own_employee_id, [e.lower() for e in requester_emails]]
            if status is not None:
                query += " AND lr.status = $3"
                params.append(status)
            query += " ORDER BY lr.requested_at DESC"
            rows = await conn.fetch(query, *params)
            return [dict(r) for r in rows]

    # ─── MY OVERVIEW (GET /api/hr/me/overview) ───────────────────────

    _MY_BONUSES_SQL = """
        SELECT id, practice_type_code, awarded_at, status, amount_idr
        FROM hr_bonus_ledger
        WHERE employee_id = $1
          AND EXTRACT(YEAR FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $2
        ORDER BY awarded_at DESC
    """

    # team_timesheet.user_id is str(team_members.id) = the token's `sub`
    # (team_activity._resolve_actor_identity), so the attendance read is keyed by it.
    _MY_CLOCKINS_SQL = """
        SELECT work_date, clock_in_bali, hours_worked
        FROM daily_work_hours
        WHERE user_id = $1 AND work_date >= $2 AND work_date < $3
    """

    # attendance_late_incidents has NO member id column (092): it is keyed by email only, and
    # team_members.email is unique case-SENSITIVELY, so two members could differ only by case.
    # The member's own email is read by primary key together with how many team_members rows
    # share it case-insensitively; the incident read happens only when that is exactly one.
    _MY_EMAIL_SQL = """
        SELECT tm.email,
               (SELECT COUNT(*) FROM team_members o
                 WHERE LOWER(o.email) = LOWER(tm.email)) AS same_email_members
        FROM team_members tm
        WHERE tm.id = $1
    """

    _MY_LATE_SQL = """
        SELECT late_date
        FROM attendance_late_incidents
        WHERE LOWER(email) = LOWER($1) AND late_date >= $2 AND late_date < $3
    """

    async def get_my_overview_rows(self, employee_id: int, team_member_id: str) -> dict[str, Any]:
        """The raw rows behind the "My HR" page, for ONE employee / team member.

        The caller passes the employee row resolved from the token's `sub`; nothing here
        reads another member. Payslips, leave balances and leave requests reuse the very
        methods the existing own-data routes call, so the visibility rule is one rule.
        """
        today = business_today()
        payslips = (await self.get_payslips(employee_id))[:12]
        balances = await self.get_leave_balance(employee_id, today.year)
        requests = (await self.list_leave_requests(employee_id))[:20]
        async with self.db_pool.acquire() as conn:
            bonuses = [
                dict(r) for r in await conn.fetch(self._MY_BONUSES_SQL, employee_id, today.year)
            ]
        return {
            "payslips": payslips,
            "bonuses": bonuses,
            "balances": balances,
            "requests": requests,
            "attendance": await self._my_attendance(team_member_id, today),
        }

    async def _my_attendance(self, team_member_id: str, today: date) -> list[dict[str, Any]]:
        """Last three Bali calendar months (current first), aggregated from the clock-in view.

        Each source degrades on its own: an unreadable clock-in view gives no months
        (the page shows its empty state), an unreadable incident table gives
        `late_incidents = None`. Neither takes the payslips down with it.
        """
        months: list[tuple[int, int]] = []
        y, m = today.year, today.month
        for _ in range(3):
            months.append((y, m))
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        start = date(months[-1][0], months[-1][1], 1)
        end = date(today.year + (today.month == 12), today.month % 12 + 1, 1)

        try:
            async with self.db_pool.acquire() as conn:
                clock_rows = [
                    dict(r)
                    for r in await conn.fetch(self._MY_CLOCKINS_SQL, team_member_id, start, end)
                ]
        except Exception as exc:
            logger.warning("my overview: clock-in view unreadable (%s)", type(exc).__name__)
            return []
        late_rows: list[dict[str, Any]] | None
        try:
            async with self.db_pool.acquire() as conn:
                owner = await conn.fetchrow(self._MY_EMAIL_SQL, team_member_id)
                email = unambiguous_email(dict(owner) if owner else None)
                # Ambiguous or unknown email: never attribute someone else's incidents.
                late_rows = (
                    None
                    if email is None
                    else [dict(r) for r in await conn.fetch(self._MY_LATE_SQL, email, start, end)]
                )
        except Exception as exc:
            logger.warning("my overview: late-incident table unreadable (%s)", type(exc).__name__)
            late_rows = None
        return aggregate_attendance(months, clock_rows, late_rows)

    # ─── DASHBOARD ───────────────────────────────────────────────────

    async def get_admin_dashboard(self) -> dict[str, Any]:
        """Admin dashboard summary."""
        today = business_today()
        async with self.db_pool.acquire() as conn:
            emp_count = await conn.fetchval(
                "SELECT COUNT(*) FROM hr_employees WHERE is_active = TRUE"
            )
            pending_bonuses = await conn.fetchrow("""
                SELECT COUNT(*) as count, COALESCE(SUM(amount_idr), 0) as total
                FROM hr_bonus_ledger WHERE status = 'pending'
            """)
            pending_leave = await conn.fetchval(
                "SELECT COUNT(*) FROM hr_leave_requests WHERE status = 'pending'"
            )
            current_period = await conn.fetchrow(
                """
                SELECT * FROM hr_payroll_periods
                WHERE payroll_month = $1 AND payroll_year = $2
            """,
                today.month,
                today.year,
            )

            return {
                "employee_count": emp_count,
                "pending_bonuses": dict(pending_bonuses),
                "pending_leave_requests": pending_leave,
                "current_period": dict(current_period) if current_period else None,
                "month": today.month,
                "year": today.year,
            }

    async def get_my_dashboard(self, employee_id: int) -> dict[str, Any]:
        """Personal dashboard for a team member.

        A person whose card says `on_payroll = false` gets `on_payroll: false` and no payroll
        data at all (no payslip, no bonus totals); their leave balances stay.
        """
        today = business_today()
        async with self.db_pool.acquire() as conn:
            on_payroll = await conn.fetchval(
                f"SELECT {ON_PAYROLL_SQL} FROM hr_employees e WHERE e.id = $1", employee_id
            )
            # Current month bonuses
            bonuses = await conn.fetchrow(
                """
                SELECT COUNT(*) as count, COALESCE(SUM(amount_idr), 0) as total
                FROM hr_bonus_ledger
                WHERE employee_id = $1
                  AND status NOT IN ('rejected', 'reversed', 'cancelled')
                  AND EXTRACT(MONTH FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $2
                  AND EXTRACT(YEAR FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $3
            """,
                employee_id,
                today.month,
                today.year,
            )

            # Latest payslip
            latest_payslip = await conn.fetchrow(
                """
                SELECT ps.*, pp.payroll_month, pp.payroll_year
                FROM hr_payslips ps
                JOIN hr_payroll_periods pp ON pp.id = ps.payroll_period_id
                WHERE ps.employee_id = $1
                ORDER BY pp.payroll_year DESC, pp.payroll_month DESC
                LIMIT 1
            """,
                employee_id,
            )

            # Leave balance
            # NOTE: shape must match get_leave_balance() above —
            # both endpoints feed the same LeaveBalance type on the frontend.
            leave_balance = await conn.fetch(
                """
                SELECT lb.*, lt.code, lt.name AS leave_type_name, lt.is_paid
                FROM hr_leave_balances lb
                JOIN hr_leave_types lt ON lt.id = lb.leave_type_id
                WHERE lb.employee_id = $1 AND lb.balance_year = $2
                ORDER BY lt.code
            """,
                employee_id,
                today.year,
            )

            if on_payroll is False:
                return {
                    "on_payroll": False,
                    "leave_balances": [dict(r) for r in leave_balance],
                }
            return {
                "on_payroll": True,
                "month_bonuses": dict(bonuses),
                "latest_payslip": dict(latest_payslip) if latest_payslip else None,
                "leave_balances": [dict(r) for r in leave_balance],
            }
