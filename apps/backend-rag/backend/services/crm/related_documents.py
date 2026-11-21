"""Registry of related subjects, slice 2: documents BY REFERENCE.

A registry document is a content hash, the Drive file ids where that content was asserted, and
links to the subjects it belongs to. Everything is a producer ASSERTION that this module never
verifies: it sees no bytes and calls no Drive. It writes no file anywhere, and it writes only
``related_documents``, ``related_document_sightings`` and ``related_subject_documents``, by
INSERT, never UPDATE or DELETE. First write wins: an existing row is never changed.

The service receives ONE connection and opens no other; an attempt is one READ COMMITTED
transaction on it. At most TWO attempts per request share ONE budget for the three causes of
abandonment (a lost insert race, a deadlock, a serialization error). It logs the subject id, the
outcome and counts only: never a hash, a Drive file id or an identifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import asyncpg

from backend.services.crm.related_subjects import (
    _OWNER_KEYS_SQL,
    _OWNER_ROW_SQL,
    _OWNERS_SQL,
    REGISTRY_ACTOR,
    IdentifierInput,
    InvalidSubject,
    RegistryUnavailable,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2

_DOCS_SQL = (
    "SELECT sha256, byte_size, mime_type, first_seen_on, first_channel "
    "FROM related_documents WHERE sha256 = ANY($1::text[])"
)
_SIGHTINGS_SQL = (
    "SELECT ref, sha256, seen_on FROM related_document_sightings "
    "WHERE kind = 'drive' AND (ref, sha256) IN (SELECT * FROM unnest($1::text[], $2::text[]))"
)
_LINKS_SQL = (
    "SELECT sha256, doc_type FROM related_subject_documents "
    "WHERE subject_id = $1 AND sha256 = ANY($2::text[])"
)
_INSERT_DOC_SQL = (
    "INSERT INTO related_documents "
    "(sha256, byte_size, mime_type, first_seen_on, first_channel, evidence, created_by) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) "
    "ON CONFLICT (sha256) DO NOTHING RETURNING sha256"
)
_INSERT_SIGHTING_SQL = (
    "INSERT INTO related_document_sightings (kind, ref, sha256, seen_on, created_by) "
    "VALUES ('drive', $1, $2, $3, $4) "
    "ON CONFLICT (kind, ref, sha256) DO NOTHING RETURNING sha256"
)
_INSERT_LINK_SQL = (
    "INSERT INTO related_subject_documents "
    "(subject_id, sha256, doc_type, evidence, created_by) "
    "VALUES ($1, $2, $3, $4::jsonb, $5) "
    "ON CONFLICT (subject_id, sha256) DO NOTHING RETURNING sha256"
)


class _Abandon(Exception):
    """An attempt lost an insert race: roll it back and start again from the subject."""


@dataclass(frozen=True)
class DriveSighting:
    file_id: str
    seen_on: date


@dataclass(frozen=True)
class DocumentInput:
    sha256: str
    byte_size: int
    mime_type: str
    doc_type: str
    first_seen_on: date
    first_channel: str
    drive: tuple[DriveSighting, ...] = ()


@dataclass(frozen=True)
class DocumentsInput:
    kind: str
    identifiers: tuple[IdentifierInput, ...]
    documents: tuple[DocumentInput, ...]
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def sightings(self) -> tuple[tuple[str, str, date], ...]:
        """Every (file id, sha256, seen_on), sorted by (file id, sha256)."""
        rows = [(d.file_id, doc.sha256, d.seen_on) for doc in self.documents for d in doc.drive]
        return tuple(sorted(rows, key=lambda r: (r[0], r[1])))


def prepare_documents(items: list[DocumentInput]) -> tuple[DocumentInput, ...]:
    """Step 1: refuse duplicates, sort documents by sha256 and each document's sightings by file id."""
    seen: set[str] = set()
    prepared = []
    for doc in items:
        if doc.sha256 in seen:
            raise InvalidSubject("duplicate_document")
        seen.add(doc.sha256)
        ids = [d.file_id for d in doc.drive]
        if len(ids) != len(set(ids)):
            raise InvalidSubject("duplicate_file_id")
        drive = tuple(sorted(doc.drive, key=lambda d: d.file_id))
        prepared.append(
            DocumentInput(
                doc.sha256,
                doc.byte_size,
                doc.mime_type,
                doc.doc_type,
                doc.first_seen_on,
                doc.first_channel,
                drive,
            )
        )
    return tuple(sorted(prepared, key=lambda d: d.sha256))


async def _resolve_subject(conn: asyncpg.Connection, request: DocumentsInput) -> dict[str, Any]:
    """Step 2: the subject owning exactly the submitted key set, or the reason there is none."""
    keys = [i.key for i in request.identifiers]
    rows = await conn.fetch(_OWNERS_SQL, [k[0] for k in keys], [k[1] for k in keys])
    owners = {r["subject_id"] for r in rows}
    if not owners:
        return {"reason": "none_owned"}
    if len(owners) > 1:
        return {"reason": "two_owners"}
    owner_id = next(iter(owners))
    submitted = set(keys)
    held = {(r["id_type"], r["id_norm"]) for r in await conn.fetch(_OWNER_KEYS_SQL, owner_id)}
    if submitted != held:
        reason = "set_differs" if submitted < held or submitted > held else "partial_overlap"
        return {"reason": reason}
    owner = await conn.fetchrow(_OWNER_ROW_SQL, owner_id)
    if owner["kind"] != request.kind:
        return {"reason": "kind_differs"}
    return {"subject_id": owner_id}


async def _attempt(
    conn: asyncpg.Connection, request: DocumentsInput, dry_run: bool
) -> dict[str, Any]:
    resolved = await _resolve_subject(conn, request)
    if "subject_id" not in resolved:
        return {"outcome": "subject_not_registered", "reason": resolved["reason"]}
    subject_id = resolved["subject_id"]

    hashes = [d.sha256 for d in request.documents]
    sightings = request.sightings
    docs = {r["sha256"]: r for r in await conn.fetch(_DOCS_SQL, hashes)}
    known_sightings = {
        (r["ref"], r["sha256"]): r
        for r in await conn.fetch(
            _SIGHTINGS_SQL, [s[0] for s in sightings], [s[1] for s in sightings]
        )
    }
    links = {r["sha256"]: r for r in await conn.fetch(_LINKS_SQL, subject_id, hashes)}

    for doc in request.documents:
        stored = docs.get(doc.sha256)
        if stored is not None and stored["byte_size"] != doc.byte_size:
            return {"outcome": "conflict", "reason": "document_differs"}

    differences = 0
    for doc in request.documents:
        stored = docs.get(doc.sha256)
        if stored is not None and (
            (stored["mime_type"], stored["first_seen_on"], stored["first_channel"])
            != (doc.mime_type, doc.first_seen_on, doc.first_channel)
        ):
            differences += 1
        link = links.get(doc.sha256)
        if link is not None and link["doc_type"] != doc.doc_type:
            differences += 1
    for ref, sha, seen_on in sightings:
        stored_sighting = known_sightings.get((ref, sha))
        if stored_sighting is not None and stored_sighting["seen_on"] != seen_on:
            differences += 1

    new_docs = [d for d in request.documents if d.sha256 not in docs]
    new_sightings = [s for s in sightings if (s[0], s[1]) not in known_sightings]
    new_links = [d for d in request.documents if d.sha256 not in links]
    counts = {
        "documents": len(new_docs),
        "sightings": len(new_sightings),
        "links": len(new_links),
        "descriptive_differences": differences,
    }
    if not (new_docs or new_sightings or new_links):
        return {"outcome": "already_linked", "subject_id": subject_id, "counts": counts}
    if dry_run:
        return {"outcome": "would_link", "subject_id": subject_id, "counts": counts}

    for doc in new_docs:
        won = await conn.fetchval(
            _INSERT_DOC_SQL,
            doc.sha256,
            doc.byte_size,
            doc.mime_type,
            doc.first_seen_on,
            doc.first_channel,
            {"plan_sha256": request.evidence["plan_sha256"]},
            REGISTRY_ACTOR,
        )
        if won is None:
            raise _Abandon
    for ref, sha, seen_on in new_sightings:
        won = await conn.fetchval(_INSERT_SIGHTING_SQL, ref, sha, seen_on, REGISTRY_ACTOR)
        if won is None:
            raise _Abandon
    link_evidence = {
        "plan_sha256": request.evidence["plan_sha256"],
        "subject_ref": request.evidence["subject_ref"],
    }
    for doc in new_links:
        won = await conn.fetchval(
            _INSERT_LINK_SQL, subject_id, doc.sha256, doc.doc_type, link_evidence, REGISTRY_ACTOR
        )
        if won is None:
            raise _Abandon
    return {"outcome": "linked", "subject_id": subject_id, "counts": counts}


async def register_documents(
    conn: asyncpg.Connection, request: DocumentsInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Link the submitted documents to the subject that owns the submitted key set.

    One budget of ``MAX_ATTEMPTS`` attempts for the three causes of abandonment (a lost insert
    race, a deadlock, a serialization error). Each abandoned attempt is rolled back completely
    and the next one repeats every read from the subject resolution. Running out of attempts
    raises ``RegistryUnavailable``. A result is returned only after the commit.
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            async with conn.transaction():
                result = await _attempt(conn, request, dry_run)
        except (_Abandon, asyncpg.DeadlockDetectedError, asyncpg.SerializationError):
            if attempts >= MAX_ATTEMPTS:
                raise RegistryUnavailable("registry_busy") from None
            continue
        result["dry_run"] = dry_run
        counts = result.get("counts") or {}
        logger.info(
            "related documents registry: outcome=%s subject_id=%s documents=%s sightings=%s "
            "links=%s differences=%s dry_run=%s",
            result["outcome"],
            result.get("subject_id"),
            counts.get("documents"),
            counts.get("sightings"),
            counts.get("links"),
            counts.get("descriptive_differences"),
            dry_run,
        )
        return result
