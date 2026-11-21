"""Registry of related people and companies: POST /api/crm/related-subjects/register (slice 1),
POST /api/crm/related-subjects/documents/register (slice 2, documents by reference) and
POST /api/crm/related-subjects/names/register (slice 4A, recorded names) and
POST /api/crm/related-subjects/links/register (slice 4B, proposed client links) and
POST /api/crm/related-subjects/merges/register (slice 4C, merges of two subjects) and
POST /api/crm/related-subjects/identifiers/detach (slice 4D, one key moved to a new record) and
POST /api/crm/related-subjects/birth-date/register (slice 4E, a birth date from an identity document).

Auth: scoped ``X-CRM-Write-Key`` plus ``RELATED_SUBJECT_REGISTRY_ENABLED`` (default off, 503).

Privacy of the path. The body carries names and document numbers, so this route owns its whole
failure surface. The route class below wraps the COMPLETE handler, dependencies and body
parsing included: whatever fails inside answers with a fixed code, never with the input, a
library message or a caller-controlled location, and no exception reaches the middleware
(activity log, request tracing, error monitoring, Sentry). The route never sets
``request.state.request_body``. Failures inside a middleware itself are outside this guarantee.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Coroutine
from datetime import date, datetime, timezone
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
from backend.services.crm.related_birth_dates import BirthDateInput, record_birth_date
from backend.services.crm.related_detach import DetachInput, detach_identifier
from backend.services.crm.related_documents import (
    DocumentInput,
    DocumentsInput,
    DriveSighting,
    prepare_documents,
    register_documents,
)
from backend.services.crm.related_identifiers import MAX_RAW_LENGTH, InvalidIdentifier, canonicalize
from backend.services.crm.related_links import LinkEntry, LinksInput, record_links
from backend.services.crm.related_merges import MergeInput, MergeSide, record_merge
from backend.services.crm.related_names import (
    MAX_RAW_NAME,
    InvalidName,
    NameEntry,
    NamesInput,
    normalise_name,
    record_names,
)
from backend.services.crm.related_subjects import (
    IdentifierInput,
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


# ── slice 2: documents by reference ───────────────────────────────────────────

MAX_DOCUMENTS = 40
MAX_DRIVE_IDS = 8
MAX_KEYS = 20
MAX_BYTE_SIZE = 9_223_372_036_854_775_807

# Slice 2 names more fields and has two more service codes. Its lists are its own: the slice-1
# lists above are not widened, so the slice-1 route answers exactly as it did.
_DOCUMENTS_LOC = _REPORTABLE_LOC | frozenset(
    {
        "subject",
        "documents",
        "sha256",
        "byte_size",
        "mime_type",
        "doc_type",
        "first_seen_on",
        "first_channel",
        "drive",
        "file_id",
        "seen_on",
    }
)
_DOCUMENTS_CODE = _REPORTABLE_CODE | frozenset({"duplicate_document", "duplicate_file_id"})
# A list index is an integer below the limit of the list it indexes, and nothing else.
_INDEX_LIMIT = {"identifiers": MAX_KEYS, "documents": MAX_DOCUMENTS, "drive": MAX_DRIVE_IDS}


class _KeyIn(_Strict):
    type: Literal["passport", "permit", "nik", "npwp", "nib", "ahu"]
    value: Annotated[str, Field(min_length=1, max_length=MAX_RAW_LENGTH)]

    @model_validator(mode="after")
    def _valid_value(self) -> _KeyIn:
        try:
            canonicalize(self.type, self.value)
        except InvalidIdentifier:
            raise PydanticCustomError("invalid_identifier", "invalid identifier") from None
        return self


class _SubjectKeys(_Strict):
    kind: Literal["person", "company"]
    identifiers: Annotated[list[_KeyIn], Field(min_length=1, max_length=MAX_KEYS)]

    @model_validator(mode="after")
    def _identifiers_fit_kind(self) -> _SubjectKeys:
        try:
            self.prepared()
        except InvalidSubject as exc:
            raise PydanticCustomError(exc.code, "invalid subject") from None
        return self

    def prepared(self) -> tuple[IdentifierInput, ...]:
        return prepare_identifiers(self.kind, [(i.type, i.value, {}) for i in self.identifiers])


class _DriveIn(_Strict):
    file_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{20,80}$")]
    seen_on: date


class _DocumentIn(_Strict):
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    byte_size: Annotated[int, Field(ge=1, le=MAX_BYTE_SIZE)]
    mime_type: Annotated[
        str, Field(pattern=r"^[a-z0-9][a-z0-9.+-]{0,60}/[a-z0-9][a-z0-9.+-]{0,90}$")
    ]
    doc_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
    first_seen_on: date
    first_channel: Literal["drive", "whatsapp"]
    drive: Annotated[list[_DriveIn], Field(max_length=MAX_DRIVE_IDS)]


class _DocumentsEvidence(_Strict):
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    subject_ref: _Word


class RelatedDocumentsRegisterRequest(_Strict):
    subject: _SubjectKeys
    documents: Annotated[list[_DocumentIn], Field(min_length=1, max_length=MAX_DOCUMENTS)]
    evidence: _DocumentsEvidence
    dry_run: StrictBool = True

    @model_validator(mode="after")
    def _documents_are_distinct(self) -> RelatedDocumentsRegisterRequest:
        try:
            self._prepared()
        except InvalidSubject as exc:
            raise PydanticCustomError(exc.code, "invalid documents") from None
        return self

    def _prepared(self):
        return prepare_documents(
            [
                DocumentInput(
                    d.sha256,
                    d.byte_size,
                    d.mime_type,
                    d.doc_type,
                    d.first_seen_on,
                    d.first_channel,
                    tuple(DriveSighting(x.file_id, x.seen_on) for x in d.drive),
                )
                for d in self.documents
            ]
        )

    def to_input(self) -> DocumentsInput:
        return DocumentsInput(
            kind=self.subject.kind,
            identifiers=self.subject.prepared(),
            documents=self._prepared(),
            evidence=self.evidence.model_dump(),
        )


# ── slice 4A: recorded names ──────────────────────────────────────────────────

MAX_NAMES = 5
MAX_COUNT = 10000

# Slice 4A has closed lists of its own; the lists above are not widened.
_NAMES_LOC = frozenset(
    {
        "body",
        "subject",
        "kind",
        "identifiers",
        "type",
        "value",
        "names",
        "name",
        "source",
        "evidence",
        "plan_sha256",
        "files",
        "blobs",
        "mrz_valid",
        "dry_run",
    }
)
_NAMES_CODE = frozenset(
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
        "literal_error",
        "list_type",
        "too_short",
        "too_long",
        "model_type",
        "dict_type",
        "json_invalid",
        "invalid_identifier",
        "kind_type_mismatch",
        "duplicate_identifier",
        "invalid_name",
        "blobs_above_files",
        "evidence_source_mismatch",
        "duplicate_name",
    }
)
_NAMES_INDEX_LIMIT = {"identifiers": MAX_KEYS, "names": MAX_NAMES}


class _NameEvidence(_Strict):
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    files: Annotated[int, Field(ge=1, le=MAX_COUNT)]
    blobs: Annotated[int, Field(ge=1, le=MAX_COUNT)]
    mrz_valid: StrictBool

    @model_validator(mode="after")
    def _blobs_fit_files(self) -> _NameEvidence:
        if self.blobs > self.files:
            raise PydanticCustomError("blobs_above_files", "blobs above files")
        return self


class _NameIn(_Strict):
    name: Annotated[str, Field(min_length=1, max_length=MAX_RAW_NAME)]
    source: Literal["ocr_corroborated", "ocr_single", "mrz_corrected"]
    evidence: _NameEvidence

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        try:
            return normalise_name(value)
        except InvalidName:
            raise PydanticCustomError("invalid_name", "invalid name") from None

    @model_validator(mode="after")
    def _evidence_fits_source(self) -> _NameIn:
        ev = self.evidence
        if self.source == "ocr_corroborated":
            fits = ev.blobs >= 2 or ev.mrz_valid
        elif self.source == "ocr_single":
            fits = ev.blobs == 1 and not ev.mrz_valid
        else:
            fits = ev.files >= 1
        if not fits:
            raise PydanticCustomError("evidence_source_mismatch", "evidence does not fit source")
        return self


class RelatedNamesRegisterRequest(_Strict):
    subject: _SubjectKeys
    names: Annotated[list[_NameIn], Field(min_length=1, max_length=MAX_NAMES)]
    dry_run: StrictBool = True

    @model_validator(mode="after")
    def _names_are_distinct(self) -> RelatedNamesRegisterRequest:
        keys = [(n.source, n.name) for n in self.names]
        if len(keys) != len(set(keys)):
            raise PydanticCustomError("duplicate_name", "duplicate name")
        return self

    def to_input(self) -> NamesInput:
        return NamesInput(
            kind=self.subject.kind,
            identifiers=self.subject.prepared(),
            names=tuple(NameEntry(n.name, n.source, n.evidence.model_dump()) for n in self.names),
        )


# ── slice 4B: proposed client links ───────────────────────────────────────────

MAX_LINKS = 5
MAX_CLIENT_ID = 2147483647

# Slice 4B has closed lists of its own; the lists above are not widened.
_LINKS_LOC = frozenset(
    {
        "body",
        "subject",
        "kind",
        "identifiers",
        "type",
        "value",
        "links",
        "client_id",
        "basis",
        "evidence",
        "plan_sha256",
        "files",
        "dry_run",
    }
)
_LINKS_CODE = _NAMES_CODE - {
    "invalid_name",
    "blobs_above_files",
    "evidence_source_mismatch",
    "duplicate_name",
} | {"duplicate_link"}
_LINKS_INDEX_LIMIT = {"identifiers": MAX_KEYS, "links": MAX_LINKS}


class _LinkEvidence(_Strict):
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    files: Annotated[int, Field(ge=1, le=MAX_COUNT)]


class _LinkIn(_Strict):
    client_id: Annotated[int, Field(ge=1, le=MAX_CLIENT_ID)]
    basis: Literal["drive_folder", "whatsapp_phone", "identity"]
    evidence: _LinkEvidence


class RelatedLinksRegisterRequest(_Strict):
    subject: _SubjectKeys
    links: Annotated[list[_LinkIn], Field(min_length=1, max_length=MAX_LINKS)]
    dry_run: StrictBool = True

    @model_validator(mode="after")
    def _links_are_distinct(self) -> RelatedLinksRegisterRequest:
        keys = [(n.client_id, n.basis) for n in self.links]
        if len(keys) != len(set(keys)):
            raise PydanticCustomError("duplicate_link", "duplicate link")
        return self

    def to_input(self) -> LinksInput:
        return LinksInput(
            kind=self.subject.kind,
            identifiers=self.subject.prepared(),
            links=tuple(
                LinkEntry(n.client_id, n.basis, n.evidence.model_dump()) for n in self.links
            ),
        )


# ── slice 4C: merges ──────────────────────────────────────────────────────────

# Slice 4C has closed lists of its own; the lists above are not widened.
_MERGES_LOC = frozenset(
    {
        "body",
        "source",
        "target",
        "kind",
        "identifiers",
        "type",
        "value",
        "basis",
        "evidence",
        "plan_sha256",
        "files",
        "dry_run",
    }
)
_MERGES_CODE = _NAMES_CODE - {
    "invalid_name",
    "blobs_above_files",
    "evidence_source_mismatch",
    "duplicate_name",
}
_MERGES_INDEX_LIMIT = {"identifiers": MAX_KEYS}


class _MergeEvidence(_Strict):
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    files: Annotated[int, Field(ge=1, le=MAX_COUNT)]


class RelatedMergesRegisterRequest(_Strict):
    source: _SubjectKeys
    target: _SubjectKeys
    basis: Literal["permit_stamp"]
    evidence: _MergeEvidence
    dry_run: StrictBool = True

    def to_input(self) -> MergeInput:
        return MergeInput(
            source=MergeSide(self.source.kind, self.source.prepared()),
            target=MergeSide(self.target.kind, self.target.prepared()),
            basis=self.basis,
            evidence=self.evidence.model_dump(),
        )


# ── slice 4D: detachments ─────────────────────────────────────────────────────

# Slice 4D has closed lists of its own; the lists above are not widened.
_DETACH_LOC = frozenset(
    {
        "body",
        "owner",
        "detach",
        "kind",
        "identifiers",
        "type",
        "value",
        "documents",
        "basis",
        "evidence",
        "plan_sha256",
        "files",
        "dry_run",
    }
)
_DETACH_CODE = _MERGES_CODE | {
    "duplicate_document",
    "detach_not_in_owner",
    "owner_keeps_no_identifier",
}
_DETACH_INDEX_LIMIT = {"identifiers": MAX_KEYS, "documents": MAX_DOCUMENTS}

_ContentHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RelatedDetachRequest(_Strict):
    owner: _SubjectKeys
    detach: _KeyIn
    documents: Annotated[list[_ContentHash], Field(max_length=MAX_DOCUMENTS)]
    basis: Literal["nik_birth_date"]
    evidence: _MergeEvidence
    dry_run: StrictBool = True

    @model_validator(mode="after")
    def _detach_fits_owner(self) -> RelatedDetachRequest:
        if len(set(self.documents)) != len(self.documents):
            raise PydanticCustomError("duplicate_document", "duplicate document")
        keys = {i.key for i in self.owner.prepared()}
        if self._detach_key() not in keys:
            raise PydanticCustomError("detach_not_in_owner", "detach not in owner")
        if len(keys) == 1:
            raise PydanticCustomError("owner_keeps_no_identifier", "owner keeps no identifier")
        return self

    def _detach_key(self) -> tuple[str, str]:
        return (self.detach.type, canonicalize(self.detach.type, self.detach.value))

    def to_input(self) -> DetachInput:
        return DetachInput(
            kind=self.owner.kind,
            identifiers=self.owner.prepared(),
            detach=self._detach_key(),
            documents=tuple(sorted(self.documents)),
            basis=self.basis,
            evidence=self.evidence.model_dump(),
        )


# ── slice 4E: birth dates ─────────────────────────────────────────────────────

# Slice 4E has closed lists of its own; the lists above are not widened.
_BIRTH_DATE_LOC = frozenset(
    {
        "body",
        "subject",
        "kind",
        "identifiers",
        "type",
        "value",
        "birth_date",
        "document",
        "document_kind",
        "basis",
        "evidence",
        "plan_sha256",
        "files",
        "dry_run",
    }
)
# Every code the model can raise; nothing else reaches an answer.
_BIRTH_DATE_CODE = frozenset(
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
        "literal_error",
        "list_type",
        "too_short",
        "too_long",
        "model_type",
        "dict_type",
        "json_invalid",
        "invalid_identifier",
        "kind_type_mismatch",
        "duplicate_identifier",
        "birth_date_invalid",
        "birth_date_out_of_range",
    }
)
_BIRTH_DATE_INDEX_LIMIT = {"identifiers": MAX_KEYS}
_EARLIEST_BIRTH_DATE = date(1900, 1, 1)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class _PersonKeys(_SubjectKeys):
    """The complete key set of a PERSON: ``kind`` is the one literal, a pydantic literal error at
    ``subject.kind`` for anything else. ``_SubjectKeys`` itself is not narrowed."""

    kind: Literal["person"]


class RelatedBirthDateRequest(_Strict):
    subject: _PersonKeys
    birth_date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    document: _ContentHash
    document_kind: Literal["passport_data_page", "itas", "evisa"]
    basis: Literal["identity_document"]
    evidence: _MergeEvidence
    dry_run: StrictBool = True

    @field_validator("birth_date")
    @classmethod
    def _birth_date_is_a_plausible_calendar_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise PydanticCustomError("birth_date_invalid", "invalid birth date") from None
        if parsed < _EARLIEST_BIRTH_DATE or parsed > _utc_today():
            raise PydanticCustomError("birth_date_out_of_range", "birth date out of range")
        return value

    def to_input(self) -> BirthDateInput:
        return BirthDateInput(
            kind=self.subject.kind,
            identifiers=self.subject.prepared(),
            birth_date=date.fromisoformat(self.birth_date),
            document=self.document,
            document_kind=self.document_kind,
            basis=self.basis,
            evidence=self.evidence.model_dump(),
        )


# Fixed answers: the whole failure surface is these constants. No status, code or text taken
# from an exception or from the request ever reaches an answer.
_ANSWER_BODIES: dict[str, bytes] = {
    code: json.dumps({"detail": code}).encode()
    for code in (
        "unauthorized",
        "registry_unavailable",
        "registry_busy",
        "database_error",
        "internal_error",
    )
}
_ANSWER_STATUS = {
    "unauthorized": 401,
    "registry_unavailable": 503,
    "registry_busy": 503,
    "database_error": 503,
    "internal_error": 500,
}


def _answer(code: str) -> Response:
    return Response(
        content=_ANSWER_BODIES[code],
        status_code=_ANSWER_STATUS[code],
        media_type="application/json",
    )


def _fixed(status_code: int, detail: Any) -> Response:
    return Response(
        content=json.dumps({"detail": detail}).encode(),
        status_code=status_code,
        media_type="application/json",
    )


def _invalid(errors: list[dict[str, Any]]) -> Response:
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


def _invalid_documents(errors: list[dict[str, Any]]) -> Response:
    """422 body of the documents route: closed lists of its own, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc: list[Any] = []
        for part in err.get("loc", ()):
            limit = _INDEX_LIMIT.get(loc[-1]) if loc else None
            if isinstance(part, int) and not isinstance(part, bool):
                loc.append(part if limit is not None and 0 <= part < limit else "_")
            else:
                loc.append(part if part in _DOCUMENTS_LOC else "_")
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _DOCUMENTS_CODE else "invalid"})
    return _fixed(422, items)


def _invalid_names(errors: list[dict[str, Any]]) -> Response:
    """422 body of the names route: closed lists of its own, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc: list[Any] = []
        for part in err.get("loc", ()):
            limit = _NAMES_INDEX_LIMIT.get(loc[-1]) if loc else None
            if isinstance(part, int) and not isinstance(part, bool):
                loc.append(part if limit is not None and 0 <= part < limit else "_")
            else:
                loc.append(part if part in _NAMES_LOC else "_")
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _NAMES_CODE else "invalid"})
    return _fixed(422, items)


def _invalid_links(errors: list[dict[str, Any]]) -> Response:
    """422 body of the links route: closed lists of its own, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc: list[Any] = []
        for part in err.get("loc", ()):
            limit = _LINKS_INDEX_LIMIT.get(loc[-1]) if loc else None
            if isinstance(part, int) and not isinstance(part, bool):
                loc.append(part if limit is not None and 0 <= part < limit else "_")
            else:
                loc.append(part if part in _LINKS_LOC else "_")
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _LINKS_CODE else "invalid"})
    return _fixed(422, items)


def _invalid_merges(errors: list[dict[str, Any]]) -> Response:
    """422 body of the merges route: closed lists of its own, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc: list[Any] = []
        for part in err.get("loc", ()):
            limit = _MERGES_INDEX_LIMIT.get(loc[-1]) if loc else None
            if isinstance(part, int) and not isinstance(part, bool):
                loc.append(part if limit is not None and 0 <= part < limit else "_")
            else:
                loc.append(part if part in _MERGES_LOC else "_")
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _MERGES_CODE else "invalid"})
    return _fixed(422, items)


def _invalid_detach(errors: list[dict[str, Any]]) -> Response:
    """422 body of the detach route: closed lists of its own, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc: list[Any] = []
        for part in err.get("loc", ()):
            limit = _DETACH_INDEX_LIMIT.get(loc[-1]) if loc else None
            if isinstance(part, int) and not isinstance(part, bool):
                loc.append(part if limit is not None and 0 <= part < limit else "_")
            else:
                loc.append(part if part in _DETACH_LOC else "_")
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _DETACH_CODE else "invalid"})
    return _fixed(422, items)


def _invalid_birth_date(errors: list[dict[str, Any]]) -> Response:
    """422 body of the birth-date route: closed lists of its own, never an input or a message."""
    items = []
    for err in errors[:50]:
        loc: list[Any] = []
        for part in err.get("loc", ()):
            limit = _BIRTH_DATE_INDEX_LIMIT.get(loc[-1]) if loc else None
            if isinstance(part, int) and not isinstance(part, bool):
                loc.append(part if limit is not None and 0 <= part < limit else "_")
            else:
                loc.append(part if part in _BIRTH_DATE_LOC else "_")
        code = err.get("type")
        items.append({"loc": loc, "code": code if code in _BIRTH_DATE_CODE else "invalid"})
    return _fixed(422, items)


def _failure_code(exc: BaseException) -> str:
    """The fixed code for a failure, chosen by class only (never by anything the failure carries)."""
    if isinstance(exc, HTTPException):
        if exc.status_code == 401:
            return "unauthorized"
        if exc.status_code == 503:
            return "registry_unavailable"
        return "internal_error"
    if isinstance(exc, RegistryUnavailable):
        return "registry_busy"
    if isinstance(exc, (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)):
        return "database_error"
    return "internal_error"


def _failure_response(exc: Exception) -> Response:
    """The fixed answer for any failure inside the route. It cannot raise.

    Anything that goes wrong while classifying or logging (an exception whose class lies about
    itself, a broken logger) falls back to the fixed 500. The log line names the code only.
    """
    try:
        code = _failure_code(exc)
        try:
            logger.error("related subject registry failure: code=%s", code)
        except Exception:
            pass
        return _answer(code)
    except Exception:
        return _answer("internal_error")


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


@router.post("/documents/register", response_model=None)
async def register_related_subject_documents(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Register documents by reference for one registered subject (dry run by default)."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid_documents([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedDocumentsRegisterRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid_documents(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    async with db_pool.acquire() as conn:
        result = await register_documents(conn, payload.to_input(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)


@router.post("/names/register", response_model=None)
async def register_related_subject_names(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Record names for one registered subject (dry run by default). Answers carry no input."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid_names([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedNamesRegisterRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid_names(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    async with db_pool.acquire() as conn:
        result = await record_names(conn, payload.to_input(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)


@router.post("/links/register", response_model=None)
async def register_related_subject_links(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Record proposed client links for one registered subject (dry run by default)."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid_links([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedLinksRegisterRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid_links(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    async with db_pool.acquire() as conn:
        result = await record_links(conn, payload.to_input(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)


@router.post("/merges/register", response_model=None)
async def register_related_subject_merge(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Record that one registered subject is another (dry run by default). Answers carry ids only."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid_merges([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedMergesRegisterRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid_merges(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    async with db_pool.acquire() as conn:
        result = await record_merge(conn, payload.to_input(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)


@router.post("/identifiers/detach", response_model=None)
async def detach_related_subject_identifier(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Move one key and the listed documents of a person record to a new record (dry run by
    default). Answers carry ids only."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid_detach([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedDetachRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid_detach(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    async with db_pool.acquire() as conn:
        result = await detach_identifier(conn, payload.to_input(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)


@router.post("/birth-date/register", response_model=None)
async def register_related_subject_birth_date(
    request: Request,
    _actor: str = Depends(verify_related_subject_write),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> Response:
    """Record the birth date of one person record from a linked identity document (dry run by
    default). Answers carry ids only."""
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _invalid_birth_date([{"loc": ("body",), "type": "too_long"}])
    try:
        payload = RelatedBirthDateRequest.model_validate_json(raw)
    except ValidationError as exc:
        return _invalid_birth_date(
            exc.errors(include_input=False, include_context=False, include_url=False)
        )
    async with db_pool.acquire() as conn:
        result = await record_birth_date(conn, payload.to_input(), dry_run=payload.dry_run)
    return JSONResponse(status_code=200, content=result)
