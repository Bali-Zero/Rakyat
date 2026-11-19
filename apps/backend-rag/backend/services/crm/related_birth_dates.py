"""Registry of related subjects, slice 4E: BIRTH DATES from an identity document.

A person record whose ``date_of_birth`` is NULL receives the date read from ONE identity document
(a passport data page, or an immigration document that copies it) already linked to that record,
and one history row names the document. The route never overwrites a date and never clears one.

The service writes exactly two statements in one transaction: the date (``SD``, the one column
``date_of_birth`` of one row, guarded by ``IS NULL``) and the history row (``IB``, insert-only). It
deletes nothing and writes no key, name, link, merge, document or CRM row.

Resolution is its own (the SQL texts of the registry are reused, no resolver of another slice is
called) and uses the PHYSICAL owners of the submitted key set, never the union a survivor shows on
the page. A dry run runs every statement in ONE repeatable-read, read-only transaction. An apply
runs in one READ COMMITTED transaction: the first statement only finds the candidate owners; with
exactly one candidate its row is locked ``FOR UPDATE`` before any other read, the owners are read
again under the lock and every answer but the candidate-read refusals comes from reads made under
that lock. No error is retried: a deadlock is the route's database error.

The service receives ONE connection and opens no other. It logs one fixed line per call that
returns, after its transaction: the outcome, the subject id and the mode. Never a key, a date, a
document hash or the request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import asyncpg

from backend.services.crm.related_subjects import (
    _OWNER_KEYS_SQL,
    _OWNERS_SQL,
    REGISTRY_ACTOR,
    IdentifierInput,
)

logger = logging.getLogger(__name__)

BIRTH_DATE_BASES = ("identity_document",)
BIRTH_DATE_DOCUMENT_KINDS = ("passport_data_page", "itas", "evisa")

_ROW_SQL = "SELECT id, kind, merged_into, date_of_birth FROM related_subjects WHERE id = $1"
_LOCK_SQL = _ROW_SQL + " FOR UPDATE"
_HISTORY_SQL = "SELECT 1 FROM related_subject_birth_dates WHERE subject_id = $1"
_OWNED_DOC_SQL = "SELECT 1 FROM related_subject_documents WHERE subject_id = $1 AND sha256 = $2"
_SET_DATE_SQL = (
    "UPDATE related_subjects SET date_of_birth = $2 WHERE id = $1 AND date_of_birth IS NULL "
    "RETURNING id"
)
_INSERT_BIRTH_DATE_SQL = (
    "INSERT INTO related_subject_birth_dates (subject_id, birth_date, document_sha256, "
    "document_kind, basis, evidence, created_by) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)"
)
_UNIQUE_SUBJECT = "related_subject_birth_dates_subject_id_key"


class BirthDateInconsistent(RuntimeError):
    """A defence of the service failed: a fixed 500, never a state. The message carries no value."""


@dataclass(frozen=True)
class BirthDateInput:
    kind: str
    identifiers: tuple[IdentifierInput, ...]  # K: canonical, sorted by (type, norm)
    birth_date: date  # b
    document: str  # x: content hash of the document the date was read from
    document_kind: str
    basis: str
    evidence: dict[str, Any]

    @property
    def keys(self) -> list[tuple[str, str]]:
        return [i.key for i in self.identifiers]


def _not_registered(reason: str) -> dict[str, Any]:
    return {"outcome": "subject_not_registered", "reason": reason}


def _refused(reason: str, subject_id: int) -> dict[str, Any]:
    return {"outcome": "not_recordable", "reason": reason, "subject_id": subject_id}


def _answer(outcome: str, subject_id: int, dry_run: bool) -> dict[str, Any]:
    return {"outcome": outcome, "subject_id": subject_id, "dry_run": dry_run}


def _set_reason(submitted: set[tuple[str, str]], held: set[tuple[str, str]]) -> str | None:
    """R3/R4: ``None`` when the owner holds exactly the submitted set."""
    if submitted == held:
        return None
    return "set_differs" if submitted < held or submitted > held else "partial_overlap"


async def _owners(conn: asyncpg.Connection, request: BirthDateInput) -> list[Any]:
    keys = request.keys
    return await conn.fetch(_OWNERS_SQL, [k[0] for k in keys], [k[1] for k in keys])


async def _held(conn: asyncpg.Connection, owner_id: int) -> set[tuple[str, str]]:
    return {(r["id_type"], r["id_norm"]) for r in await conn.fetch(_OWNER_KEYS_SQL, owner_id)}


async def _judge(
    conn: asyncpg.Connection, request: BirthDateInput, subject_id: int, row: Any, dry_run: bool
) -> dict[str, Any] | None:
    """R5, N1, W, N2, N3, N4 on the row as given (snapshot or locked). ``None`` = recordable."""
    if row["kind"] != "person":
        return _not_registered("kind_differs")
    if row["merged_into"] is not None:
        return _refused("subject_is_merged", subject_id)
    held = row["date_of_birth"]
    if held == request.birth_date:
        return _answer("already_recorded", subject_id, dry_run)
    if held is not None:
        return _refused("birth_date_differs", subject_id)
    if await conn.fetchrow(_HISTORY_SQL, subject_id) is not None:
        return _refused("history_exists", subject_id)
    if await conn.fetchrow(_OWNED_DOC_SQL, subject_id, request.document) is None:
        return _refused("document_not_on_subject", subject_id)
    return None


async def _dry_run(conn: asyncpg.Connection, request: BirthDateInput) -> dict[str, Any]:
    owners = {r["subject_id"] for r in await _owners(conn, request)}
    if not owners:
        return _not_registered("none_owned")
    if len(owners) > 1:
        return _not_registered("two_owners")
    (subject_id,) = owners
    reason = _set_reason(set(request.keys), await _held(conn, subject_id))
    if reason is not None:
        return _not_registered(reason)
    row = await conn.fetchrow(_ROW_SQL, subject_id)
    if row is None:
        raise BirthDateInconsistent("subject_row_missing")
    refused = await _judge(conn, request, subject_id, row, True)
    return refused or _answer("recorded", subject_id, True)


async def _apply(conn: asyncpg.Connection, request: BirthDateInput) -> dict[str, Any]:
    candidates = {r["subject_id"] for r in await _owners(conn, request)}
    if not candidates:
        return _not_registered("none_owned")
    if len(candidates) > 1:
        return _not_registered("two_owners")
    (candidate,) = candidates
    row = await conn.fetchrow(_LOCK_SQL, candidate)
    if row is None:
        return _not_registered("none_owned")
    # under the lock: the candidate read is discarded, every answer below comes from here
    owners = {r["subject_id"] for r in await _owners(conn, request)}
    if not owners:
        return _not_registered("none_owned")
    if len(owners) > 1:
        return _not_registered("two_owners")
    (owner_id,) = owners
    if owner_id != candidate:
        # a detach moved a key of K to another record: no chase, nothing written
        return _not_registered("set_differs")
    reason = _set_reason(set(request.keys), await _held(conn, candidate))
    if reason is not None:
        return _not_registered(reason)
    refused = await _judge(conn, request, candidate, row, False)
    if refused is not None:
        return refused
    if await conn.fetchval(_SET_DATE_SQL, candidate, request.birth_date) is None:
        raise BirthDateInconsistent("date_not_set")
    try:
        await conn.execute(
            _INSERT_BIRTH_DATE_SQL,
            candidate,
            request.birth_date,
            request.document,
            request.document_kind,
            request.basis,
            request.evidence,
            REGISTRY_ACTOR,
        )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == _UNIQUE_SUBJECT:
            raise BirthDateInconsistent("history_exists_at_insert") from None
        raise
    return _answer("recorded", candidate, False)


async def record_birth_date(
    conn: asyncpg.Connection, request: BirthDateInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Record a person's birth date from a linked identity document, or say why not.

    A dry run writes nothing; an apply writes the two statements or nothing. The answer is
    returned, and the outcome logged, only after the transaction ends.
    """
    if dry_run:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            result = await _dry_run(conn, request)
    else:
        async with conn.transaction():
            result = await _apply(conn, request)
    logger.info(
        "related birth date registry: outcome=%s subject_id=%s dry_run=%s",
        result["outcome"],
        result.get("subject_id"),
        dry_run,
    )
    return result
