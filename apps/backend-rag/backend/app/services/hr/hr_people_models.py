"""Response models for the People console (`/api/hr/people`): the read side of HR.

Plain types, every field an explicit allow-list entry. The roster carries no pay or
identity numbers: those only exist in the per-person overview, behind the scope check.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.services.hr.hr_overview_models import MyHrOverview


class PersonFlags(BaseModel):
    papers_missing: int | None = None  # null: the person has no HR record yet
    bonuses_pending: int = 0
    leave_pending: int = 0
    on_leave_today: bool = False


class PersonRow(BaseModel):
    team_member_id: str
    employee_id: int | None = None
    has_record: bool = False
    name: str | None = None
    role: str | None = None
    department: str | None = None
    unit: str | None = None
    hire_date: date | None = None
    active: bool = True
    flags: PersonFlags = Field(default_factory=PersonFlags)


class PayrollMonth(BaseModel):
    month: int
    year: int
    status: str | None = None  # null: no payroll period opened for this month


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
    people: list[PersonRow] = Field(default_factory=list)
    pulse: PeoplePulse | None = None  # only for scope "all"


class PersonOverview(BaseModel):
    viewer_team_member_id: str
    person: MyHrOverview
