"""People console: `GET /api/hr/people`, `GET /api/hr/people/{id}/overview` and the one write,
`PATCH /api/hr/people/{id}/card` (STAFF_CARD.md C11) and the document door
`POST|DELETE /api/hr/people/{id}/documents/{slot}` (C13).

Scope is decided here from the session's email and the stored email of the member asked
about, never from anything the client sends. The only writes are the card edit and the
document door, for the global admins alone.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers.hr import _get_user_id
from backend.app.services.hr.hr_card_edit import CardEditBody, safe_validation_errors
from backend.app.services.hr.hr_card_models import DocumentChangeResult
from backend.app.services.hr.hr_documents import (
    ALL_SLOTS,
    MAX_BYTES,
    HRDocCode,
    HRDocError,
    HRDocumentService,
    detect_type,
    get_hr_drive_service,
)
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


def _field_422(field: str, code: str, message: str) -> JSONResponse:
    """A structured 422 as a RESPONSE: the app's global HTTPException handler flattens a
    list `detail` to a string, which would cost the page its per-field messages."""
    return JSONResponse(
        status_code=422, content={"detail": [{"field": field, "code": code, "message": message}]}
    )


def _card_error_response(code: HRCardCode) -> Response:
    if code is HRCardCode.HIRE_DATE_REQUIRED:
        return _field_422("hire_date", code.value, "Hire date is required")
    if code is HRCardCode.INVALID_INPUT:
        return _field_422("", code.value, "Invalid value")
    if code is HRCardCode.LOCK_TIMEOUT:
        return JSONResponse(status_code=409, content={"detail": "Card is busy, try again"})
    return JSONResponse(status_code=500, content={"detail": "Card operation failed"})


async def edit_person_card(
    team_member_id: uuid.UUID,
    body: CardEditBody,
    current_user: dict[str, Any] = Depends(require_card_writer),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> PersonOverview | Response:
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
        return _card_error_response(exc.code)
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


# ─── document door (C13) ───

_DOC_ERROR_STATUS = {
    HRDocCode.NOT_FOUND: (404, "No such person"),
    HRDocCode.HR_RECORD_REQUIRED: (
        409,
        {"code": "hr_record_required", "message": "Add this person to HR first"},
    ),
    HRDocCode.DRIVE_UNAVAILABLE: (
        502,
        {"code": "drive_unavailable", "message": "Document storage is unavailable, try again"},
    ),
    HRDocCode.LOCK_TIMEOUT: (409, "Card is busy, try again"),
    HRDocCode.DB_ERROR: (500, "Document operation failed"),
    HRDocCode.COMMIT_UNKNOWN: (500, "Document operation failed"),
}


def _doc_error_response(code: HRDocCode) -> Response:
    status, detail = _DOC_ERROR_STATUS[code]
    return JSONResponse(status_code=status, content={"detail": detail})


async def _document_context(
    team_member_id: uuid.UUID, slot: str, db_pool: asyncpg.Pool
) -> tuple[str, str] | Response:
    """(member id, email) of a visible person, or the refusal. The slot is checked first: an
    unknown slot never reaches a lookup."""
    if slot not in ALL_SLOTS:
        return _field_422("slot", "unknown_slot", "Unknown document slot")
    member_id = str(team_member_id)
    try:
        member_email = await _get_people_service(db_pool).member_email(member_id)
    except Exception as exc:
        logger.error("hr document read failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "Document operation failed"})
    if member_email is None:
        raise HTTPException(status_code=404, detail="No such person")
    return member_id, member_email


# The whole multipart body may exceed the file cap by its framing only.
_BODY_CAP = MAX_BYTES + 64 * 1024


def _too_large() -> Response:
    return JSONResponse(status_code=413, content={"detail": "The upload is too large"})


async def _read_capped_body(request: Request) -> bytes | None:
    """The request body, or None the moment it passes the cap (a declared Content-Length over it
    is refused before one byte is read; a chunked body is cut off as it grows)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > _BODY_CAP:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > _BODY_CAP:
            return None
    return bytes(buf)


_BOUNDARY = re.compile(r'boundary=(?:"([^"]+)"|([^;\s]+))', re.IGNORECASE)


def _boundary_of(content_type: str) -> bytes | None:
    """The multipart boundary, or None when absent or longer than RFC 2046's 70 characters."""
    found = _BOUNDARY.search(content_type)
    raw = (found.group(1) or found.group(2)) if found else ""
    if not raw or len(raw) > 70:
        return None
    try:
        return raw.encode("ascii")
    except UnicodeEncodeError:
        return None


async def _single_file(request: Request, body: bytes) -> bytes | Response:
    """The bytes of the ONE part named `file` in a buffered multipart body, else a fixed 422.

    Refused: no multipart content type, a boundary over 70 characters, a body that does not end in
    its closing boundary, any field at all, more or fewer than one file part, a file part under
    another name, and anything the parser itself rejects (every exception is caught)."""
    bad = _field_422("file", "invalid_upload", "Send exactly one file in the field 'file'")
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type.lower():
        return bad
    boundary = _boundary_of(content_type)
    if boundary is None or not body.rstrip(b"\r\n \t").endswith(b"--" + boundary + b"--"):
        return bad

    async def stream() -> Any:
        yield body
        yield b""

    form = None
    try:
        form = await MultiPartParser(
            request.headers, stream(), max_files=1, max_fields=0, max_part_size=_BODY_CAP
        ).parse()
        items = list(form.multi_items())
        if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
            return bad
        return await items[0][1].read()
    except (
        Exception
    ):  # MultiPartException, but also whatever a malformed body makes the parser raise
        return bad
    finally:
        if form is not None:
            try:
                await form.close()
            except Exception:
                pass


async def upload_person_document(
    request: Request,
    team_member_id: uuid.UUID,
    slot: str,
    current_user: dict[str, Any] = Depends(require_card_writer),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Upload or replace one HR paper (global admins only).

    No `UploadFile` parameter on purpose: FastAPI would parse the whole form before the writer
    check and the size check. Here the writer dependency has already run when the body is first
    touched; the body is read under a hard byte cap and only then parsed. The type is read from
    the magic bytes (PDF, JPEG, PNG); nothing the client names is trusted."""
    ctx = await _document_context(team_member_id, slot, db_pool)
    if isinstance(ctx, Response):
        return ctx
    member_id, member_email = ctx
    body = await _read_capped_body(request)
    if body is None:
        return _too_large()
    got = await _single_file(request, body)
    if isinstance(got, Response):
        return got
    data = got
    if not data:
        return _field_422("file", "empty_file", "The file is empty")
    if len(data) > MAX_BYTES:
        return _field_422("file", "file_too_large", "The file is larger than 4 MB")
    detected = detect_type(data)
    if detected is None:
        return _field_422("file", "unsupported_type", "Only PDF, JPEG or PNG files are accepted")
    actor_id = str(_get_user_id(current_user))
    actor_email = str(current_user.get("email") or "").lower().strip()
    try:
        drive = await get_hr_drive_service()
    except Exception as exc:
        logger.error("hr document drive init failed (%s)", type(exc).__name__)
        return _doc_error_response(HRDocCode.DRIVE_UNAVAILABLE)
    try:
        result = await HRDocumentService(db_pool, drive).upload(
            actor_email=actor_email,
            member_id=member_id,
            email=member_email,
            slot=slot,
            data=data,
            mime=detected[0],
            ext=detected[1],
        )
    except HRDocError as exc:
        logger.warning(
            "hr document upload refused actor=%s target=%s slot=%s code=%s",
            actor_id,
            member_id,
            slot,
            exc.code.value,
        )
        return _doc_error_response(exc.code)
    logger.info(
        "hr document upload actor=%s target=%s slot=%s",
        actor_id,
        member_id,
        slot,
    )
    return JSONResponse(status_code=200, content=result)


async def remove_person_document(
    team_member_id: uuid.UUID,
    slot: str,
    current_user: dict[str, Any] = Depends(require_card_writer),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Empty one HR paper's slot; the file goes to the Drive trash only when this door made it."""
    ctx = await _document_context(team_member_id, slot, db_pool)
    if isinstance(ctx, Response):
        return ctx
    member_id, member_email = ctx
    actor_id = str(_get_user_id(current_user))
    actor_email = str(current_user.get("email") or "").lower().strip()
    try:
        drive = await get_hr_drive_service()
    except Exception as exc:
        logger.error("hr document drive init failed (%s)", type(exc).__name__)
        return _doc_error_response(HRDocCode.DRIVE_UNAVAILABLE)
    try:
        result = await HRDocumentService(db_pool, drive).remove(
            actor_email=actor_email, member_id=member_id, email=member_email, slot=slot
        )
    except HRDocError as exc:
        logger.warning(
            "hr document remove refused actor=%s target=%s slot=%s code=%s",
            actor_id,
            member_id,
            slot,
            exc.code.value,
        )
        return _doc_error_response(exc.code)
    logger.info(
        "hr document remove actor=%s target=%s slot=%s",
        actor_id,
        member_id,
        slot,
    )
    return JSONResponse(status_code=200, content=result)


router.add_api_route(
    "/{team_member_id}/documents/{slot}",
    upload_person_document,
    methods=["POST"],
    response_model=DocumentChangeResult,
    route_class_override=_NoEchoValidationRoute,
    # The handler takes no body parameter (it reads the stream itself, after authz and under a
    # cap), so the multipart body is declared here for the generated clients.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                        "additionalProperties": False,
                    }
                }
            },
        }
    },
)
router.add_api_route(
    "/{team_member_id}/documents/{slot}",
    remove_person_document,
    methods=["DELETE"],
    response_model=DocumentChangeResult,
    route_class_override=_NoEchoValidationRoute,
)
