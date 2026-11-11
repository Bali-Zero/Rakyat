"""
HR/Payroll Service — Business logic for employees, bonuses, payroll, leave.

Pattern: asyncpg pool, same as CRM services.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

import asyncpg

from backend.app.utils.hr_utils import (
    calculate_bpjs,
    calculate_pph21_monthly,
)

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


class HRCardCode(str, Enum):
    """The closed set of card failure codes (STAFF_CARD.md)."""

    HIRE_DATE_REQUIRED = "hire_date_required"
    INVALID_INPUT = "invalid_input"
    PROFILE_NOT_OBJECT = "profile_not_object"
    DB_ERROR = "db_error"


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
            rows = await conn.fetch("""
                SELECT e.*, tm.full_name, tm.email, tm.role
                FROM hr_employees e
                JOIN team_members tm ON tm.id = e.team_member_id
                WHERE e.is_active = TRUE
                ORDER BY tm.full_name
            """)
            return [self._legacy_row(r) for r in rows]

    @staticmethod
    def _legacy_row(row: Any) -> dict[str, Any]:
        """Legacy employee shape: the card `profile` is served only by the card route."""
        data = dict(row)
        data.pop("profile", None)
        return data

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
            logger.info(f"HR employee upserted: team_member_id={data['team_member_id']}")
            return self._legacy_row(row)

    # ─── STAFF CARD (contract: STAFF_CARD.md) ───────────────────────

    # Explicit column list (no `e.*`, and no `notes`: HR free text is never on the card).
    _CARD_SELECT = """
        SELECT e.id, e.team_member_id, e.hire_date, e.is_active, e.ptkp_status,
               e.npwp, e.bpjs_kesehatan_number, e.bpjs_ketenagakerjaan_number,
               e.bank_name, e.bank_account, e.bank_account_holder, e.profile,
               e.base_salary_idr,
               tm.full_name, tm.role, tm.department
        FROM hr_employees e
        JOIN team_members tm ON tm.id = e.team_member_id
        WHERE tm.id = $1 AND COALESCE(tm.role, '') <> 'client'
    """

    # $11 is a Python dict bound to jsonb: the pool codec encodes it exactly once.
    _CARD_UPDATE_SQL = """
        UPDATE hr_employees SET
            hire_date = COALESCE($2, hire_date),
            npwp = COALESCE($3, npwp),
            ptkp_status = COALESCE($4, ptkp_status),
            bpjs_kesehatan_number = COALESCE($5, bpjs_kesehatan_number),
            bpjs_ketenagakerjaan_number = COALESCE($6, bpjs_ketenagakerjaan_number),
            bank_name = COALESCE($7, bank_name),
            bank_account = COALESCE($8, bank_account),
            bank_account_holder = COALESCE($9, bank_account_holder),
            is_active = COALESCE($10, is_active),
            profile = (CASE WHEN jsonb_typeof(profile) = 'object' THEN profile ELSE '{}'::jsonb END)
                || $11::jsonb
                || CASE WHEN $11::jsonb ? 'docs' THEN jsonb_build_object(
                    'docs',
                    (CASE WHEN jsonb_typeof(profile->'docs') = 'object' THEN profile->'docs'
                          ELSE '{}'::jsonb END) || ($11::jsonb->'docs'))
                    ELSE '{}'::jsonb END
                || CASE WHEN $11::jsonb ? 'contract' THEN jsonb_build_object(
                    'contract',
                    (CASE WHEN jsonb_typeof(profile->'contract') = 'object' THEN profile->'contract'
                          ELSE '{}'::jsonb END) || ($11::jsonb->'contract'))
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

    async def merge_card_on(
        self,
        conn: Any,
        team_member_id: str,
        fields: dict[str, Any],
        profile_patch: dict[str, Any],
    ) -> int | None:
        """Non-clobbering card write on a caller-owned connection (the import batches many).

        Returns the row id, or None when no row was written (no such member). A
        concurrent insert falls through to the update; a stored profile that is not
        an object after the merge fails and rolls this write back.
        """
        fields, profile_patch = self._checked_patch(fields, profile_patch)
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
                row = await conn.fetchrow(self._CARD_UPDATE_SQL, *args)
                if row is None:
                    if fields.get("hire_date") is None:
                        raise HRCardError(HRCardCode.HIRE_DATE_REQUIRED)
                    row = await conn.fetchrow(self._CARD_INSERT_SQL, *args)
                    if row is None:  # a concurrent writer inserted the row: update it
                        row = await conn.fetchrow(self._CARD_UPDATE_SQL, *args)
                if row is not None and row["profile_type"] != "object":
                    raise HRCardError(HRCardCode.PROFILE_NOT_OBJECT)
        except HRCardError:
            raise
        except Exception:
            raise HRCardError(HRCardCode.DB_ERROR) from None
        if row is None:
            return None
        logger.info(f"HR card merged: team_member_id={team_member_id}")
        return row["id"]

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
                data.get("effective_from", date.today()),
                data.get("effective_to"),
                data.get("is_active", True),
                data.get("notes"),
            )
            logger.info(
                f"Bonus rate upserted: {data['practice_type_code']} = {data['amount_idr']} IDR"
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
                       p.status as practice_status, c.full_name as client_name
                FROM hr_bonus_ledger bl
                JOIN hr_employees e ON e.id = bl.employee_id
                JOIN team_members tm ON tm.id = e.team_member_id
                JOIN practices p ON p.id = bl.practice_id
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
            return [dict(r) for r in rows]

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
        """Approve a pending bonus."""
        async with self.db_pool.acquire() as conn:
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
            if not row:
                msg = f"Bonus {bonus_id} not found or not pending"
                raise ValueError(msg)
            logger.info("Bonus %s approved by %s", bonus_id, approved_by)
            return dict(row)

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
        Creates period + payslips + deductions for all active employees.
        """
        period_start = date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        period_end = date(year, month, last_day)

        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
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

                # Get all active employees
                employees = await conn.fetch("""
                    SELECT e.*, tm.full_name, tm.email
                    FROM hr_employees e
                    JOIN team_members tm ON tm.id = e.team_member_id
                    WHERE e.is_active = TRUE
                """)

                payslips = []
                for emp in employees:
                    salary = emp["base_salary_idr"]
                    emp_id = emp["id"]
                    ptkp = emp["ptkp_status"]

                    # Sum approved bonuses for this month
                    bonus_row = await conn.fetchrow(
                        """
                        SELECT COALESCE(SUM(amount_idr), 0) as total
                        FROM hr_bonus_ledger
                        WHERE employee_id = $1
                          AND status IN ('approved', 'paid')
                          AND EXTRACT(MONTH FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $2
                          AND EXTRACT(YEAR FROM (awarded_at AT TIME ZONE 'Asia/Makassar')) = $3
                    """,
                        emp_id,
                        month,
                        year,
                    )
                    bonus_total = bonus_row["total"]

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
        """Approve a payroll period (locks it)."""
        async with self.db_pool.acquire() as conn:
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
            if not row:
                msg = f"Period {period_id} not found or not in 'calculated' status"
                raise ValueError(msg)
            logger.info("Payroll period %s approved by %s", period_id, approved_by)
            return dict(row)

    async def mark_payroll_paid(self, period_id: int) -> dict[str, Any]:
        """Mark payroll as paid."""
        async with self.db_pool.acquire() as conn:
            async with conn.transaction():
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

                logger.info("Leave request %s approved by %s", request_id, reviewed_by)
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

                logger.info("Leave request %s rejected by %s", request_id, reviewed_by)
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
        """Personal dashboard for a team member."""
        today = business_today()
        async with self.db_pool.acquire() as conn:
            # Current month bonuses
            bonuses = await conn.fetchrow(
                """
                SELECT COUNT(*) as count, COALESCE(SUM(amount_idr), 0) as total
                FROM hr_bonus_ledger
                WHERE employee_id = $1
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

            return {
                "month_bonuses": dict(bonuses),
                "latest_payslip": dict(latest_payslip) if latest_payslip else None,
                "leave_balances": [dict(r) for r in leave_balance],
            }
