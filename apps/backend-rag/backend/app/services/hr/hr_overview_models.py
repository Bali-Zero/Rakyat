"""Response models for `GET /api/hr/me/overview`: the member's own HR picture in one read.

Plain types, no validators. Every field is an explicit allow-list entry: nothing from a
row reaches the response unless it is declared here (no HR free-text notes, no reviewer ids).
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from backend.app.services.hr.hr_card_models import StaffCard


class PayslipSummary(BaseModel):
    id: int
    payroll_year: int
    payroll_month: int
    net_salary_idr: int
    base_salary_idr: int
    bonus_total_idr: int
    allowance_total_idr: int
    deduction_total_idr: int
    thr_idr: int


class BonusEntry(BaseModel):
    id: int
    practice_type_code: str
    awarded_at: datetime
    status: str
    amount_idr: int


class BonusTotals(BaseModel):
    """Server-side totals; rejected, reversed and cancelled bonuses are never counted."""

    month_total_idr: int = 0
    month_count: int = 0
    year_total_idr: int = 0


class LeaveBalanceRow(BaseModel):
    id: int
    leave_type_id: int
    code: str
    leave_type_name: str
    is_paid: bool
    balance_year: int
    allocated_days: int
    carried_over: int
    used_days: int
    pending_days: int


class LeaveRequestRow(BaseModel):
    id: int
    leave_type_name: str
    start_date: date
    end_date: date
    total_days: int
    status: str
    requested_at: datetime | None = None


class LeaveBlock(BaseModel):
    balances: list[LeaveBalanceRow] = Field(default_factory=list)
    requests: list[LeaveRequestRow] = Field(default_factory=list)


class AttendanceMonth(BaseModel):
    """One Bali-time calendar month of the member's own clock-ins.

    `hours` and `avg_clock_in` are null when no clocked day carries them;
    `late_incidents` is null when the late-incident record could not be read.
    """

    month: str  # "YYYY-MM"
    days_present: int
    hours: float | None = None
    avg_clock_in: str | None = None  # "HH:MM", Bali time
    late_incidents: int | None = None


class MyHrOverview(BaseModel):
    team_member_id: str  # the owner: equals the token's `sub`; the page compares it
    card: StaffCard
    payslips: list[PayslipSummary] = Field(default_factory=list)
    bonuses: list[BonusEntry] = Field(default_factory=list)
    bonus_totals: BonusTotals = Field(default_factory=BonusTotals)
    leave: LeaveBlock = Field(default_factory=LeaveBlock)
    attendance: list[AttendanceMonth] = Field(default_factory=list)
