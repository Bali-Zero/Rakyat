"""Registry of related subjects, slice 4D: DETACHMENTS.

A person record of the registry can hold the national identity number (``nik``) of ANOTHER person,
proven by the birth date the number encodes. A detachment moves that one key, and the documents the
request lists, from the owner record to a NEW person record, in one transaction, and writes one
history row. The new record holds exactly that key for life: no route adds a key to an existing
record.

The service writes exactly four statements in one transaction: the new record (``IS``), the key
(``MK``, its ``subject_id`` only), the listed document links (``MD``, their ``subject_id`` only, run
even when the list is empty) and the history row (``IH``, insert-only). It deletes nothing and
writes no recorded name, client link, merge, document, sighting or CRM row.

Resolution is its own (the SQL texts of the registry are reused, no resolver of another slice is
called). A dry run runs every statement in ONE repeatable-read, read-only transaction, so its answer
is the answer of one committed instant. An apply runs in one READ COMMITTED transaction: the first
statement only finds the candidate owners of the submitted key set; with exactly one candidate its
row is locked ``FOR UPDATE`` before any other read, the owners are read again under the lock (the
candidate read is then discarded) and every answer but ``none_owned`` and the witness path's comes
from reads made under that lock. No error is retried: a deadlock is the route's database error.

The service receives ONE connection and opens no other. It logs one fixed line per call that
returns, after its transaction: the outcome and the two subject ids. Never a key, a fingerprint, a
date, a document hash or the request.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import asyncpg

from backend.services.crm.related_subjects import (
    _INSERT_SUBJECT_SQL,
    _OWNER_KEYS_SQL,
    _OWNERS_SQL,
    REGISTRY_ACTOR,
    IdentifierInput,
)

logger = logging.getLogger(__name__)

DETACH_BASES = ("nik_birth_date",)

_ROW_SQL = "SELECT id, kind, merged_into, date_of_birth FROM related_subjects WHERE id = $1"
_LOCK_SQL = _ROW_SQL + " FOR UPDATE"
_WITNESS_SQL = (
    "SELECT from_id, owner_keys_sha256, moved_documents FROM related_subject_detachments "
    "WHERE to_id = $1"
)
_OWNED_DOCS_SQL = (
    "SELECT sha256 FROM related_subject_documents WHERE subject_id = $1 "
    "AND sha256 = ANY($2::text[])"
)
_MOVE_KEY_SQL = (
    "UPDATE related_subject_identifiers SET subject_id = $4 "
    "WHERE id_type = $1 AND id_norm = $2 AND subject_id = $3 RETURNING id_type"
)
_MOVE_DOCS_SQL = (
    "UPDATE related_subject_documents SET subject_id = $2 "
    "WHERE subject_id = $1 AND sha256 = ANY($3::text[]) RETURNING sha256"
)
_INSERT_DETACH_SQL = (
    "INSERT INTO related_subject_detachments (from_id, to_id, id_type, owner_keys_sha256, "
    "moved_documents, basis, evidence, created_by) "
    "VALUES ($1, $2, $3, $4, $5::text[], $6, $7::jsonb, $8)"
)

_NIK = re.compile(r"[0-9]{16}")
_MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class DetachInconsistent(RuntimeError):
    """A defence of the service failed: a fixed 500, never a state. The message carries no value."""


@dataclass(frozen=True)
class DetachInput:
    kind: str
    identifiers: tuple[IdentifierInput, ...]  # K: canonical, sorted by (type, norm)
    detach: tuple[str, str]  # d: (type, canonical value), one of K
    documents: tuple[str, ...]  # L: sorted content hashes
    basis: str
    evidence: dict[str, Any]

    @property
    def keys(self) -> list[tuple[str, str]]:
        return [i.key for i in self.identifiers]


def keys_sha256(keys: Iterable[tuple[str, str]]) -> str:
    """Lowercase hex sha256 of the UTF-8 bytes of the canonical lines ``type:norm``, sorted by code
    point, joined by one newline, no final newline. A digest of key values: never logged."""
    lines = sorted(f"{id_type}:{norm}" for id_type, norm in keys)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def nik_date(norm: str) -> tuple[int, int, int] | None:
    """The (day, month, two-digit year) a canonical nik encodes in its digits 7 to 12, or None.

    Day = DD - 40 when DD > 40 (the women's encoding), else DD. Valid when 1 <= MM <= 12 and
    1 <= day <= the month's length, 29 February always (the century is unknown). Never raises on
    16 ASCII digits; any other string raises ``ValueError``.
    """
    if not isinstance(norm, str) or _NIK.fullmatch(norm) is None:
        raise ValueError("not a canonical nik")
    dd, mm, yy = int(norm[6:8]), int(norm[8:10]), int(norm[10:12])
    day = dd - 40 if dd > 40 else dd
    if not 1 <= mm <= 12 or not 1 <= day <= _MONTH_DAYS[mm - 1]:
        return None
    return day, mm, yy


def _not_registered(reason: str) -> dict[str, Any]:
    return {"outcome": "subject_not_registered", "reason": reason}


def _refused(reason: str, from_id: int, to_id: int | None = None) -> dict[str, Any]:
    return {"outcome": "not_detachable", "reason": reason, "from_id": from_id, "to_id": to_id}


def _answer(outcome: str, from_id: int, to_id: int | None, dry_run: bool) -> dict[str, Any]:
    return {"outcome": outcome, "from_id": from_id, "to_id": to_id, "dry_run": dry_run}


def _set_reason(submitted: set[tuple[str, str]], held: set[tuple[str, str]]) -> str | None:
    """R3/R4: ``None`` when the owner holds exactly the submitted set."""
    if submitted == held:
        return None
    return "set_differs" if submitted < held or submitted > held else "partial_overlap"


async def _owners(conn: asyncpg.Connection, request: DetachInput) -> list[Any]:
    keys = request.keys
    return await conn.fetch(_OWNERS_SQL, [k[0] for k in keys], [k[1] for k in keys])


async def _held(conn: asyncpg.Connection, owner_id: int) -> set[tuple[str, str]]:
    return {(r["id_type"], r["id_norm"]) for r in await conn.fetch(_OWNER_KEYS_SQL, owner_id)}


async def _witness(
    conn: asyncpg.Connection, request: DetachInput, rows: list[Any], dry_run: bool
) -> dict[str, Any]:
    """Two or more owners of K: the request was applied already (W), with other documents (W'),
    or K is held by two owners (R2). Read from the given owner rows and the history row only."""
    t = next(
        (r["subject_id"] for r in rows if (r["id_type"], r["id_norm"]) == request.detach), None
    )
    if t is None:
        return _not_registered("two_owners")
    h = await conn.fetchrow(_WITNESS_SQL, t)
    if h is None or h["owner_keys_sha256"] != keys_sha256(request.keys):
        return _not_registered("two_owners")
    if list(h["moved_documents"]) != list(request.documents):
        return _refused("detached_with_other_documents", h["from_id"], t)
    return _answer("already_detached", h["from_id"], t, dry_run)


async def _judge(
    conn: asyncpg.Connection, request: DetachInput, owner_id: int, row: Any
) -> dict[str, Any] | None:
    """R5, N1, N3 to N8 on the owner row as given (snapshot or locked). ``None`` = detachable."""
    if row["kind"] != request.kind:
        return _not_registered("kind_differs")
    if row["merged_into"] is not None:
        return _refused("owner_is_merged", owner_id)
    left = [k for k in request.keys if k != request.detach]
    if all(id_type == "permit" for id_type, _ in left):
        return _refused("owner_left_permit_only", owner_id)
    d_type, d_norm = request.detach
    if d_type != "nik" or _NIK.fullmatch(d_norm) is None:
        return _refused("identifier_not_nik", owner_id)
    dob = row["date_of_birth"]
    if dob is None:
        return _refused("owner_has_no_birth_date", owner_id)
    encoded = nik_date(d_norm)
    if encoded is None:
        return _refused("nik_date_invalid", owner_id)
    if encoded == (dob.day, dob.month, dob.year % 100):
        return _refused("nik_date_matches", owner_id)
    owned = await conn.fetch(_OWNED_DOCS_SQL, owner_id, list(request.documents))
    if len(owned) < len(request.documents):
        return _refused("document_not_on_owner", owner_id)
    return None


async def _dry_run(conn: asyncpg.Connection, request: DetachInput) -> dict[str, Any]:
    rows = await _owners(conn, request)
    owners = {r["subject_id"] for r in rows}
    if not owners:
        return _not_registered("none_owned")
    if len(owners) > 1:
        return await _witness(conn, request, rows, True)
    (owner_id,) = owners
    reason = _set_reason(set(request.keys), await _held(conn, owner_id))
    if reason is not None:
        return _not_registered(reason)
    row = await conn.fetchrow(_ROW_SQL, owner_id)
    if row is None:
        raise DetachInconsistent("owner_row_missing")
    refused = await _judge(conn, request, owner_id, row)
    return refused or _answer("detached", owner_id, None, True)


async def _apply(conn: asyncpg.Connection, request: DetachInput) -> dict[str, Any]:
    rows = await _owners(conn, request)
    candidates = {r["subject_id"] for r in rows}
    if not candidates:
        return _not_registered("none_owned")
    if len(candidates) > 1:
        return await _witness(conn, request, rows, False)
    (candidate,) = candidates
    row = await conn.fetchrow(_LOCK_SQL, candidate)
    if row is None:
        return _not_registered("none_owned")
    # under the lock: the candidate read is discarded, every answer below comes from here
    rows = await _owners(conn, request)
    owners = {r["subject_id"] for r in rows}
    if not owners:
        return _not_registered("none_owned")
    if len(owners) > 1:
        return await _witness(conn, request, rows, False)
    (owner_id,) = owners
    if owner_id != candidate:
        # X holds for life the one key of K moved to it: K cannot be its whole set
        if set(request.keys) == await _held(conn, owner_id):
            raise DetachInconsistent("key_set_of_another_owner")
        return _not_registered("set_differs")
    reason = _set_reason(set(request.keys), await _held(conn, candidate))
    if reason is not None:
        return _not_registered(reason)
    refused = await _judge(conn, request, candidate, row)
    if refused is not None:
        return refused
    d_type, d_norm = request.detach
    documents = list(request.documents)
    to_id = await conn.fetchval(
        _INSERT_SUBJECT_SQL,
        "person",
        None,
        None,
        None,
        None,
        {
            "detached_from": candidate,
            "basis": request.basis,
            "plan_sha256": request.evidence["plan_sha256"],
        },
        REGISTRY_ACTOR,
    )
    if await conn.fetchval(_MOVE_KEY_SQL, d_type, d_norm, candidate, to_id) is None:
        raise DetachInconsistent("key_not_moved")
    moved = await conn.fetch(_MOVE_DOCS_SQL, candidate, to_id, documents)
    if len(moved) != len(documents):
        raise DetachInconsistent("documents_not_moved")
    await conn.execute(
        _INSERT_DETACH_SQL,
        candidate,
        to_id,
        d_type,
        keys_sha256(request.keys),
        documents,
        request.basis,
        request.evidence,
        REGISTRY_ACTOR,
    )
    return _answer("detached", candidate, to_id, False)


async def detach_identifier(
    conn: asyncpg.Connection, request: DetachInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Detach one key and the listed documents of a person record into a new record, or say why not.

    A dry run writes nothing; an apply writes the four statements or nothing. The answer is
    returned, and the outcome logged, only after the transaction ends.
    """
    if dry_run:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            result = await _dry_run(conn, request)
    else:
        async with conn.transaction():
            result = await _apply(conn, request)
    logger.info(
        "related detach registry: outcome=%s from_id=%s to_id=%s dry_run=%s",
        result["outcome"],
        result.get("from_id"),
        result.get("to_id"),
        dry_run,
    )
    return result
