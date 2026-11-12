"""
HR/Payroll utility functions.

RBAC helpers, Indonesian tax (PPh21 TER), BPJS calculations, THR.
Legal references:
- PPh21: UU HPP 7/2021, PP 58/2023 (TER method, effective 2024)
- BPJS Kesehatan: Perpres 64/2020
- BPJS Ketenagakerjaan: PP 44/2015, PP 46/2015
- THR: Permenaker 6/2016
- Leave: UU 13/2003 Pasal 79, 82, 93
- UMK Badung 2026: SK Gub Bali 1021/03-M/HK/2025 (Rp 3.791.003)
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Literal

from backend.app.core.config import settings

# ─── RBAC ────────────────────────────────────────────────────────────────

# HR-specific additions on top of the global admin allowlist
# (`settings.admin_emails_set`). Admins carry over automatically. HIGH-7 (audit 2026-04-18).
# Reorganisation 2026-11-09: tax@ no longer holds an HR role. Owner decision
# 2026-11-10: the Direktur is an HR admin (full HR access, including the pay
# fields of employee records). No tax or setup address is. Owner decision
# 2026-11-12: Ruslana is no longer an HR admin; she keeps her delegated leave approval
# (hr_leave_routing) and a read-only People view of four members (HR_SCOPED_VIEWERS).
HR_EXTRA_ADMIN_EMAILS: frozenset[str] = frozenset({"adit@balizero.com"})

# The closed set of HR writers (owner decision 2026-11-13): the owner and the accountant. HR
# card writes, HR document changes and payroll calculate/approve/mark-paid belong to them
# ONLY; being a global admin (`settings.admin_emails_set`, which also holds an IT account)
# grants none of it. All lower-case.
HR_WRITER_EMAILS: frozenset[str] = frozenset({"zero@balizero.com", "asya@balizero.com"})

# Read-only People viewers (owner decision 2026-11-12): viewer email -> the member emails
# that viewer may SEE. All lower-case. A scoped viewer never edits anything.
HR_SCOPED_VIEWERS: Mapping[str, frozenset[str]] = {
    "ruslana@balizero.com": frozenset(
        {
            "rina@balizero.com",
            "rio@balizero.com",
            "dea@balizero.com",
            "candra@balizero.com",
        }
    ),
}


def _hr_admin_emails() -> frozenset[str]:
    """Effective HR admin allowlist — union of global admins and HR extras."""
    return settings.admin_emails_set | HR_EXTRA_ADMIN_EMAILS


def is_hr_admin(user: dict[str, Any]) -> bool:
    """Check if user has HR admin privileges."""
    email = (user.get("email") or "").lower()
    return email in _hr_admin_emails() or user.get("role") == "admin"


def can_write_hr_card(user: dict[str, Any]) -> bool:
    """May this caller write HR data: any card, HR documents, payroll (STAFF_CARD.md C11)?

    Only the closed set `HR_WRITER_EMAILS` (the owner and the accountant). Being a global
    admin is not enough, nor an HR extra, a scoped viewer, a member or a client, and the
    token's `role` claim does not grant it: it is an email allow-list, nothing else.
    """
    if user.get("role") == "client":
        return False
    email = (user.get("email") or "").lower().strip()
    return email in HR_WRITER_EMAILS


def can_run_payroll(user: dict[str, Any]) -> bool:
    """May this caller calculate, approve or mark paid a payroll period? Same closed set."""
    return can_write_hr_card(user)


def hr_people_scope(user: dict[str, Any]) -> Literal["all"] | frozenset[str] | None:
    """Whose HR picture the caller may read in the People console.

    "all" for HR admins, the member-email set for a scoped viewer, None otherwise
    (a plain member, a client, or no identity at all).
    """
    if user.get("role") == "client":
        return None
    if is_hr_admin(user):
        return "all"
    email = (user.get("email") or "").lower().strip()
    return HR_SCOPED_VIEWERS.get(email)


def can_view_hr_member(user: dict[str, Any], member_email: str | None) -> bool:
    """True when the caller's People scope covers that member (case-insensitive)."""
    scope = hr_people_scope(user)
    if scope is None:
        return False
    if scope == "all":
        return True
    return (member_email or "").lower().strip() in scope


# ─── PTKP (Non-Taxable Income) — Annual ─────────────────────────────────
# UU HPP 7/2021, valid since 2016 (unchanged)

PTKP: dict[str, int] = {
    "TK/0": 54_000_000,
    "TK/1": 58_500_000,
    "TK/2": 63_000_000,
    "TK/3": 67_500_000,
    "K/0": 58_500_000,
    "K/1": 63_000_000,
    "K/2": 67_500_000,
    "K/3": 72_000_000,
}


# ─── PPh21 Progressive Brackets (Annual) ────────────────────────────────
# UU HPP 7/2021 Pasal 17 — used for year-end reconciliation

PPH21_BRACKETS: list[tuple[int, float]] = [
    (60_000_000, 0.05),
    (250_000_000, 0.15),
    (500_000_000, 0.25),
    (5_000_000_000, 0.30),
    (999_999_999_999_999, 0.35),
]


def calculate_pph21_annual(annual_gross: int, ptkp_status: str = "TK/0") -> int:
    """
    Calculate annual PPh21 using progressive brackets.
    Used for year-end reconciliation (bukan TER bulanan).

    Args:
        annual_gross: Total annual gross income in IDR
        ptkp_status: PTKP status code (e.g. 'TK/0', 'K/2')

    Returns:
        Annual PPh21 amount in IDR
    """
    ptkp = PTKP.get(ptkp_status, PTKP["TK/0"])
    taxable = max(0, annual_gross - ptkp)

    if taxable == 0:
        return 0

    tax = 0
    prev_limit = 0
    for limit, rate in PPH21_BRACKETS:
        if taxable <= 0:
            break
        bracket_amount = min(taxable, limit - prev_limit)
        tax += int(bracket_amount * rate)
        taxable -= bracket_amount
        prev_limit = limit

    return tax


def calculate_pph21_monthly(monthly_gross: int, ptkp_status: str = "TK/0") -> int:
    """
    Estimate monthly PPh21 (simplified TER method).
    PP 58/2023: actual TER uses 44-bracket lookup table.
    This is a simplified progressive calculation / 12.

    Args:
        monthly_gross: Monthly gross income in IDR
        ptkp_status: PTKP status code

    Returns:
        Monthly PPh21 estimate in IDR
    """
    annual_gross = monthly_gross * 12
    annual_tax = calculate_pph21_annual(annual_gross, ptkp_status)
    return annual_tax // 12


# ─── BPJS Calculations ──────────────────────────────────────────────────
# Rates effective 2024-2026

# BPJS Kesehatan (Perpres 64/2020)
BPJS_KES_EMPLOYER_RATE = 0.04  # 4%
BPJS_KES_EMPLOYEE_RATE = 0.01  # 1%
BPJS_KES_SALARY_CAP = 12_000_000  # Max salary for contribution calc

# BPJS Ketenagakerjaan
BPJS_JHT_EMPLOYER_RATE = 0.037  # 3.7%
BPJS_JHT_EMPLOYEE_RATE = 0.02  # 2%
BPJS_JKK_RATE = 0.0024  # 0.24% (risk class I — office/consulting)
BPJS_JKM_RATE = 0.003  # 0.30%
BPJS_JP_EMPLOYER_RATE = 0.02  # 2%
BPJS_JP_EMPLOYEE_RATE = 0.01  # 1%
BPJS_JP_SALARY_CAP = 10_042_300  # 2025 cap (updated annually by BPJS TK)


def calculate_bpjs(
    monthly_salary: int,
) -> dict[str, dict[str, int]]:
    """
    Calculate all BPJS contributions (employer + employee).

    Returns dict with keys: kesehatan, jht, jkk, jkm, jp.
    Each has: employer, employee, total.
    """
    # BPJS Kesehatan — capped at 12M
    kes_base = min(monthly_salary, BPJS_KES_SALARY_CAP)
    kes_employer = int(kes_base * BPJS_KES_EMPLOYER_RATE)
    kes_employee = int(kes_base * BPJS_KES_EMPLOYEE_RATE)

    # JHT — no cap
    jht_employer = int(monthly_salary * BPJS_JHT_EMPLOYER_RATE)
    jht_employee = int(monthly_salary * BPJS_JHT_EMPLOYEE_RATE)

    # JKK — employer only, no cap
    jkk = int(monthly_salary * BPJS_JKK_RATE)

    # JKM — employer only, no cap
    jkm = int(monthly_salary * BPJS_JKM_RATE)

    # JP — capped
    jp_base = min(monthly_salary, BPJS_JP_SALARY_CAP)
    jp_employer = int(jp_base * BPJS_JP_EMPLOYER_RATE)
    jp_employee = int(jp_base * BPJS_JP_EMPLOYEE_RATE)

    return {
        "kesehatan": {
            "employer": kes_employer,
            "employee": kes_employee,
            "total": kes_employer + kes_employee,
        },
        "jht": {
            "employer": jht_employer,
            "employee": jht_employee,
            "total": jht_employer + jht_employee,
        },
        "jkk": {"employer": jkk, "employee": 0, "total": jkk},
        "jkm": {"employer": jkm, "employee": 0, "total": jkm},
        "jp": {
            "employer": jp_employer,
            "employee": jp_employee,
            "total": jp_employer + jp_employee,
        },
    }


def total_employee_deductions(monthly_salary: int, ptkp_status: str = "TK/0") -> dict[str, int]:
    """
    Calculate total employee-side deductions from salary.

    Returns dict: bpjs_kes, bpjs_jht, bpjs_jp, pph21, total.
    """
    bpjs = calculate_bpjs(monthly_salary)
    pph21 = calculate_pph21_monthly(monthly_salary, ptkp_status)

    kes = bpjs["kesehatan"]["employee"]
    jht = bpjs["jht"]["employee"]
    jp = bpjs["jp"]["employee"]

    return {
        "bpjs_kes": kes,
        "bpjs_jht": jht,
        "bpjs_jp": jp,
        "pph21": pph21,
        "total": kes + jht + jp + pph21,
    }


# ─── THR (Tunjangan Hari Raya) ──────────────────────────────────────────
# Permenaker 6/2016


def calculate_thr(
    base_salary: int,
    hire_date: date,
    reference_date: date | None = None,
) -> int:
    """
    Calculate THR amount.

    - ≥12 months continuous: 1 month salary
    - <12 months: (months_worked / 12) × salary
    - <1 month: 0

    Args:
        base_salary: Monthly base salary in IDR
        hire_date: Employee hire date
        reference_date: Date of THR calculation (default: today)

    Returns:
        THR amount in IDR
    """
    if reference_date is None:
        reference_date = date.today()

    days_worked = (reference_date - hire_date).days
    if days_worked < 30:
        return 0

    months_worked = days_worked // 30
    if months_worked >= 12:
        return base_salary

    return int(base_salary * months_worked / 12)


# ─── UMK Reference ──────────────────────────────────────────────────────

UMK_BADUNG_2026 = 3_791_003  # SK Gub Bali 1021/03-M/HK/2025
UMP_BALI_2026 = 3_207_459
UMP_BALI_2025 = 2_996_561


def validate_minimum_wage(monthly_salary: int, year: int = 2026) -> bool:
    """Check if salary meets UMK Badung minimum."""
    if year >= 2026:
        return monthly_salary >= UMK_BADUNG_2026
    return monthly_salary >= UMP_BALI_2025
