"""Response models for the People console (`/api/hr/people`): the read side of HR.

Plain types, every field an explicit allow-list entry. The roster carries no pay or
identity numbers: those only exist in the per-person overview, behind the scope check.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

from backend.app.services.hr.hr_overview_models import MyHrOverview


class PersonFlags(BaseModel):
    papers_missing: int | None  # null: the person has no HR record yet
    bonuses_pending: int
    leave_pending: int
    on_leave_today: bool


class PersonRow(BaseModel):
    team_member_id: str
    employee_id: int | None
    has_record: bool
    name: str | None
    avatar_url: str | None = None  # /static/team/<slug>.jpg from team_members.avatar
    role: str | None
    department: str | None
    unit: str | None
    hire_date: date | None
    active: bool
    flags: PersonFlags


class PayrollMonth(BaseModel):
    month: int
    year: int
    status: str | None  # null: no payroll period opened for this month


class PeoplePulse(BaseModel):
    active_people: int
    people_without_record: int
    bonuses_pending_count: int
    bonuses_pending_idr: int
    leave_pending_count: int
    payroll: PayrollMonth


class PeopleRoster(BaseModel):
    viewer_team_member_id: str  # the bearer's sub: the page compares it
    scope: Literal["all", "scoped"]
    can_edit: bool
    people: list[PersonRow]
    pulse: PeoplePulse | None  # only for scope "all"


class PersonOverview(BaseModel):
    viewer_team_member_id: str
    person: MyHrOverview
    # DAI-153: the person's WhatsApp number, MASKED (`62•••••1234`), and only for an HR writer
    # (HR_WRITER_EMAILS); null for every other viewer, or when no number is stored.
    whatsapp_masked: str | None = None
