"""POST /api/crm/related-subjects/register: registry of related people and companies.

Auth: scoped ``X-CRM-Write-Key`` plus ``RELATED_SUBJECT_REGISTRY_ENABLED`` (default off, 503).

Privacy of the path. The body carries names and document numbers, so this route owns its whole
failure surface. The route class below wraps the COMPLETE handler, dependencies and body
parsing included: whatever fails inside answers with a fixed code, never with the input, a
library message or a caller-controlled location, and no exception reaches the middleware
(activity log, request tracing, error monitoring, Sentry). The route never sets
``request.state.request_body``. Failures inside a middleware itself are outside this guarantee.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from datetime import date
from http import HTTPStatus
from typing import Annotated, Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from backend.app.dependencies import get_database_pool
from backend.app.deps.crm_service_write import verify_related_subject_write
from backend.services.crm.related_identifiers import MAX_RAW_LENGTH, InvalidIdentifier, canonicalize
from backend.services.crm.related_subjects import (
    InvalidSubject,
    RegistryUnavailable,
    SubjectInput,
    prepare_identifiers,
    register_subject,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 65536

# The only field names a 422 may name; anything else a caller invents is reported as "_".
_REPORTABLE_LOC = frozenset(
    {
        "kind",
        "display_name",
        "date_of_birth",
        "nationality",
        "sex",
        "identifiers",
        "type",
        "value",
        "evidence",
        "plan_sha256",
        "subject_ref",
        "tier",
        "files",
        "independent_files",
        "strengths",
        "dry_run",
        "body",
    }
)
# The only error codes a 422 may carry (pydantic's own fixed vocabulary plus ours).
_REPORTABLE_CODE = frozenset(
    {
        "missing",
        "extra_forbidden",
        "string_type",
        "string_too_short",
        "string_too_long",
        "string_pattern_mismatch",
        "bool_type",
        "int_type",
        "greater_than_equal",
        "less_than_equal",
        "date_type",
        "date_from_datetime_inexact",
        "date_parsing",
        "literal_error",
        "list_type",
        "too_short",
        "too_long",
        "model_type",
        "dict_type",
        "json_invalid",
        "invalid_text",
        "invalid_identifier",
        "kind_type_mismatch",
        "duplicate_identifier",
    }
)
_SAFE_WORD = r"^[A-Za-z0-9_.:+-]+$"

_Word = Annotated[str, Field(min_length=1, max_length=64, pattern=_SAFE_WORD)]


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise PydanticCustomError("invalid_text", "invalid text")
    return text or None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _IdentifierEvidence(_Strict):
    files: int | None = Field(default=None, ge=0, le=1_000_000)
    independent_files: int | None = Field(default=None, ge=0, le=1_000_000)
    strengths: (
        list[Annotated[str, Field(min_length=1, max_length=32, pattern=_SAFE_WORD)]] | None
    ) = Field(default=None, max_length=16)


class _SubjectEvidence(_Strict):
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    subject_ref: _Word
    tier: Annotated[str, Field(min_length=1, max_length=16, pattern=_SAFE_WORD)]


class _IdentifierIn(_Strict):
    type: Literal["passport", "permit", "nik", "npwp", "nib", "ahu"]
    value: Annotated[str, Field(min_length=1, max_length=MAX_RAW_LENGTH)]
    evidence: _IdentifierEvidence | None = None

    @model_validator(mode="after")
    def _valid_value(self) -> _IdentifierIn:
        try:
            canonicalize(self.type, self.value)
        except InvalidIdentifier:
            raise PydanticCustomError("invalid_identifier", "invalid identifier") from None
        return self


class RelatedSubjectRegisterRequest(_Strict):
    kind: Literal["person", "company"]
    display_name: Annotated[str, Field(max_length=200)] | None = None
    date_of_birth: date | None = None
    nationality: Annotated[str, Field(max_length=64)] | None = None
    sex: Annotated[str, Field(max_length=8)] | None = None
    identifiers: Annotated[list[_IdentifierIn], Field(min_length=1, max_length=20)]
    evidence: _SubjectEvidence
    dry_run: StrictBool = True

    _text = field_validator("display_name", "nationality", "sex")(_clean_text)

    @model_validator(mode="after")
    def _identifiers_fit_kind(self) -> RelatedSubjectRegisterRequest:
        try:
            self._prepared()
        except InvalidSubject as exc:
            raise PydanticCustomError(exc.code, "invalid subject") from None
        return self

    def _prepared(self):
        return prepare_identifiers(
            self.kind,
            [
                (i.type, i.value, i.evidence.model_dump(exclude_none=True) if i.evidence else {})
                for i in self.identifiers
            ],
        )

    def to_subject(self) -> SubjectInput:
        return SubjectInput(
            kind=self.kind,
            identifiers=self._prepared(),
            evidence=self.evidence.model_dump(),
            display_name=self.display_name,
            date_of_birth=self.date_of_birth,
            nationality=self.nationality,
            sex=self.sex,
        )


def _fixed(status_code: int, detail: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _invalid(errors: list[dict[str, Any]]) -> JSONResponse:
    """422 body: field names and codes from fixed allowlists, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc = [
            part if isinstance(part, int) or part in _REPORTABLE_LOC else "_"
            for part in err.get("loc", ())
        ]
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _REPORTABLE_CODE else "invalid"})
    return _fixed(422, items)


def _failure_response(exc: Exception) -> JSONResponse:
    """The fixed answer for any failure inside the route. Logs the class name only."""
    if isinstance(exc, HTTPException):
        code = {401: "unauthorized", 503: "registry_unavailable"}.get(exc.status_code)
        if code is None:
            code = HTTPStatus(exc.status_code).phrase.lower().replace(" ", "_")
        return _fixed(exc.status_code, code)
    if isinstance(exc, RegistryUnavailable):
        logger.warning("related subject registry busy: %s", exc.code)
        return _fixed(503, exc.code)
    if isinstance(exc, (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)):
        logger.error("related subject registry database failure: class=%s", type(exc).__name__)
        return _fixed(503, "database_error")
    logger.error("related subject registry failure: class=%s", type(exc).__name__)
    return _fixed(500, "internal_error")


class _SealedRoute(APIRoute):
    """Route whose handler is sealed: dependencies, parsing and response included."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        inner = super().get_route_handler()

        async def sealed(request: Request) -> Response:
            try:
                return await inner(request)
            except Exception as exc:
                return _failure_response(exc)

        return sealed


router = APIRouter(
    prefix="/api/crm/related-subjects",
    tags=["crm-related-subjects"],
    route_class=_SealedRoute,
)


@router.post("/register", response_model=None)
async def register_related_subject(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Register one related subject (dry run by default). Answers carry fixed codes only."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedSubjectRegisterRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid(exc.errors(include_input=False, include_context=False, include_url=False))
    async with db_pool.acquire() as conn:
        result = await register_subject(conn, payload.to_subject(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)
