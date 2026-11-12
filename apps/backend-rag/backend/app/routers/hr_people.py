"""People console: `GET /api/hr/people`, `GET /api/hr/people/{id}/overview` and the one write,
`PATCH /api/hr/people/{id}/card` (STAFF_CARD.md C11).

Scope is decided here from the session's email and the stored email of the member asked
about, never from anything the client sends. The only write is the card edit, for the
global admins alone.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers.hr import _get_user_id
from backend.app.services.hr.hr_card_edit import CardEditBody, safe_validation_errors
from backend.app.services.hr.hr_overview_compose import (
    OverviewReadError,
    compose_member_overview,
)
from backend.app.services.hr.hr_people_models import PeopleRoster, PersonOverview
from backend.app.services.hr.hr_people_service import HRPeopleService
from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService
from backend.app.utils.hr_utils import (
    can_view_hr_member,
    can_write_hr_card,
    hr_people_scope,
    is_hr_admin,
)

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


# ─── card edit (C11) ───


class _NoEchoValidationRoute(APIRoute):
    """FastAPI's default 422 carries `input`; on this route a validation error is reduced to
    `{field, code, message}` first (a card value must never come back in a response)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422, content={"detail": safe_validation_errors(exc.errors())}
                )

        return handler


async def require_card_writer(
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    if not can_write_hr_card(current_user):
        raise HTTPException(status_code=403, detail="Card write access required")
    return current_user


def _card_error_status(code: HRCardCode) -> HTTPException:
    if code is HRCardCode.HIRE_DATE_REQUIRED:
        return HTTPException(
            status_code=422,
            detail=[{"field": "hire_date", "code": code.value, "message": "Hire date is required"}],
        )
    if code is HRCardCode.INVALID_INPUT:
        return HTTPException(
            status_code=422, detail=[{"field": "", "code": code.value, "message": "Invalid value"}]
        )
    if code is HRCardCode.LOCK_TIMEOUT:
        return HTTPException(status_code=409, detail="Card is busy, try again")
    return HTTPException(status_code=500, detail="Card operation failed")


async def edit_person_card(
    team_member_id: uuid.UUID,
    body: CardEditBody,
    current_user: dict[str, Any] = Depends(require_card_writer),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> PersonOverview:
    """Edit, clear or create one person's HR card; answer with the person's refreshed overview.

    Only a global admin (403 otherwise). The person must be a visible one (the People
    predicate), so a client, monitoring or partner account never gets a card. Every error is a
    fixed code; the log names the actor, the target and the KEY NAMES touched, never a value.
    """
    actor_id = _get_user_id(current_user)
    member_id = str(team_member_id)
    try:
        member_email = await _get_people_service(db_pool).member_email(member_id)
    except Exception as exc:
        logger.error("hr card edit failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Card operation failed") from None
    if member_email is None:
        raise HTTPException(status_code=404, detail="No such person")
    fields, profile, cleared, set_keys = body.to_write()
    service = _get_hr_service(db_pool)
    try:
        row_id = await service.edit_card(member_id, member_email, fields, profile, cleared)
    except HRCardError as exc:
        logger.warning(
            "hr card edit refused actor=%s target=%s code=%s", actor_id, member_id, exc.code.value
        )
        raise _card_error_status(exc.code) from None
    if row_id is None:  # the member changed under us (renamed, deactivated, now a client)
        raise HTTPException(status_code=404, detail="No such person")
    logger.info(
        "hr card edit actor=%s target=%s set=%s cleared=%s", actor_id, member_id, set_keys, cleared
    )
    try:
        person = await compose_member_overview(service, member_id)
    except (HRCardError, OverviewReadError):  # the write is committed; the page may re-read
        raise HTTPException(status_code=500, detail="Overview read failed") from None
    if person is None:
        raise HTTPException(status_code=404, detail="No HR employee record for this person")
    return PersonOverview(viewer_team_member_id=str(actor_id), person=person)


router.add_api_route(
    "/{team_member_id}/card",
    edit_person_card,
    methods=["PATCH"],
    response_model=PersonOverview,
    route_class_override=_NoEchoValidationRoute,
)
