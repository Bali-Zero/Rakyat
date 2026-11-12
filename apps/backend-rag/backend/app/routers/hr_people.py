"""People console, read side: `GET /api/hr/people` and `GET /api/hr/people/{id}/overview`.

Scope is decided here from the session's email and the stored email of the member asked
about, never from anything the client sends. Write actions are not in this router.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers.hr import _get_user_id
from backend.app.services.hr.hr_overview_compose import (
    OverviewReadError,
    compose_member_overview,
)
from backend.app.services.hr.hr_people_models import PeopleRoster, PersonOverview
from backend.app.services.hr.hr_people_service import HRPeopleService
from backend.app.services.hr.hr_service import HRCardError, HRService
from backend.app.utils.hr_utils import can_view_hr_member, hr_people_scope, is_hr_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hr/people", tags=["hr-people"])


def _get_hr_service(db_pool: asyncpg.Pool) -> HRService:
    return HRService(db_pool)


def _get_people_service(db_pool: asyncpg.Pool) -> HRPeopleService:
    return HRPeopleService(db_pool)


@router.get("", response_model=PeopleRoster)
async def list_people(
    current_user: dict[str, Any] = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> PeopleRoster:
    """Every staff member the caller may see, with the flags that need attention.

    HR admins see all staff (people without an HR record included) plus the pulse; a
    scoped viewer sees only their members, no pulse.
    """
    scope = hr_people_scope(current_user)
    if scope is None:
        raise HTTPException(status_code=403, detail="People access required")
    viewer_id = _get_user_id(current_user)
    service = _get_people_service(db_pool)
    try:
        people = await service.roster(None if scope == "all" else sorted(scope))
        pulse = await service.pulse(people) if scope == "all" else None
    except Exception as exc:  # a fixed message: a driver error can quote a value
        logger.error("hr people read failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="People read failed") from None
    return PeopleRoster(
        viewer_team_member_id=str(viewer_id),
        scope="all" if scope == "all" else "scoped",
        can_edit=is_hr_admin(current_user),
        people=people,
        pulse=pulse,
    )


@router.get("/{team_member_id}/overview", response_model=PersonOverview)
async def get_person_overview(
    team_member_id: uuid.UUID,
    current_user: dict[str, Any] = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> PersonOverview:
    """One person's HR picture: exactly the shape `/me/overview` gives that member."""
    if hr_people_scope(current_user) is None:
        raise HTTPException(status_code=403, detail="People access required")
    viewer_id = _get_user_id(current_user)
    member_id = str(team_member_id)
    try:
        member_email = await _get_people_service(db_pool).member_email(member_id)
    except Exception as exc:
        logger.error("hr people read failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="People read failed") from None
    if member_email is None:
        raise HTTPException(status_code=404, detail="No HR employee record for this person")
    if not can_view_hr_member(current_user, member_email):
        raise HTTPException(status_code=403, detail="Not allowed to view this person")
    try:
        person = await compose_member_overview(_get_hr_service(db_pool), member_id)
    except HRCardError:  # a code, never a driver message
        raise HTTPException(status_code=500, detail="Card operation failed") from None
    except OverviewReadError:  # the class was logged where it happened
        raise HTTPException(status_code=500, detail="Overview read failed") from None
    if person is None:
        raise HTTPException(status_code=404, detail="No HR employee record for this person")
    return PersonOverview(viewer_team_member_id=str(viewer_id), person=person)
