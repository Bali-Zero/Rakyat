"""Registry of related subjects: people and companies that have documents but no CRM card.

A subject is the holder of its registered identifiers. The registry guarantees ONE OWNER
PER CANONICAL IDENTIFIER KEY and nothing more: it does not claim one subject per real
person, and a later client may carry the same number (the next call reports it).

The service receives ONE connection and opens no other. Everything runs in one
transaction on it (the connection's default READ COMMITTED isolation). It never
writes outside ``related_subjects`` and ``related_subject_identifiers``, and it logs
subject ids, outcomes and counts only: never a name and never an identifier value.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import asyncpg

from backend.services.crm.related_identifiers import (
    NORM_VERSION,
    InvalidIdentifier,
    canonicalize,
    kind_of,
    sql_canonical,
)

logger = logging.getLogger(__name__)

REGISTRY_ACTOR = "system:related-subject-registry"

_CLIENT_PASSPORT_SQL = (
    "SELECT 1 FROM clients WHERE deleted_at IS NULL AND "
    + sql_canonical("passport", "passport_number")
    + " = ANY($1::text[]) LIMIT 1"
)
_FAMILY_PASSPORT_SQL = (
    "SELECT 1 FROM client_family_members WHERE "
    + sql_canonical("passport", "passport_number")
    + " = ANY($1::text[]) LIMIT 1"
)
_COMPANY_SQL = (
    "SELECT 1 FROM companies WHERE "
    + sql_canonical("npwp", "npwp_company")
    + " = ANY($1::text[]) OR "
    + sql_canonical("nib", "nib")
    + " = ANY($2::text[]) LIMIT 1"
)
_OWNERS_SQL = (
    "SELECT subject_id, id_type, id_norm FROM related_subject_identifiers "
    "WHERE (id_type, id_norm) IN (SELECT * FROM unnest($1::text[], $2::text[]))"
)
_OWNER_KEYS_SQL = "SELECT id_type, id_norm FROM related_subject_identifiers WHERE subject_id = $1"
_OWNER_ROW_SQL = "SELECT kind, date_of_birth FROM related_subjects WHERE id = $1"
_INSERT_SUBJECT_SQL = (
    "INSERT INTO related_subjects "
    "(kind, display_name, date_of_birth, nationality, sex, evidence, created_by) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7) RETURNING id"
)
_INSERT_IDENTIFIER_SQL = (
    "INSERT INTO related_subject_identifiers "
    "(id_type, id_norm, id_raw, norm_version, subject_id, evidence) "
    "VALUES ($1, $2, $3, $4, $5, $6::jsonb) "
    "ON CONFLICT (id_type, id_norm) DO NOTHING RETURNING id_norm"
)


class InvalidSubject(ValueError):
    """A request subject is invalid. ``code`` is a fixed word; the message carries no value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RegistryUnavailable(RuntimeError):
    """The registry could not complete after its single retry. ``code`` is a fixed word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _LostRace(Exception):
    pass


@dataclass(frozen=True)
class IdentifierInput:
    id_type: str
    id_norm: str
    id_raw: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.id_type, self.id_norm)


@dataclass(frozen=True)
class SubjectInput:
    kind: str
    identifiers: tuple[IdentifierInput, ...]
    evidence: dict[str, Any]
    display_name: str | None = None
    date_of_birth: date | None = None
    nationality: str | None = None
    sex: str | None = None


def prepare_identifiers(
    kind: str, items: list[tuple[str, str, dict[str, Any]]]
) -> tuple[IdentifierInput, ...]:
    """Step 1: canonicalise, validate, refuse duplicates, sort by (id_type, id_norm)."""
    prepared: dict[tuple[str, str], IdentifierInput] = {}
    for id_type, value, evidence in items:
        try:
            if kind_of(id_type) != kind:
                raise InvalidSubject("kind_type_mismatch")
            norm = canonicalize(id_type, value)
        except InvalidIdentifier:
            raise InvalidSubject("invalid_identifier") from None
        key = (id_type, norm)
        if key in prepared:
            raise InvalidSubject("duplicate_identifier")
        prepared[key] = IdentifierInput(id_type, norm, value, dict(evidence))
    return tuple(prepared[key] for key in sorted(prepared))


async def _crm_match(conn: asyncpg.Connection, subject: SubjectInput) -> str | None:
    """Step 2: a live CRM record already holds one of the keys. Nothing is written."""
    passports = [i.id_norm for i in subject.identifiers if i.id_type == "passport"]
    if passports:
        if await conn.fetchval(_CLIENT_PASSPORT_SQL, passports):
            return "existing_client"
        if await conn.fetchval(_FAMILY_PASSPORT_SQL, passports):
            return "existing_family_member"
    npwps = [i.id_norm for i in subject.identifiers if i.id_type == "npwp"]
    nibs = [i.id_norm for i in subject.identifiers if i.id_type == "nib"]
    if (npwps or nibs) and await conn.fetchval(_COMPANY_SQL, npwps, nibs):
        return "existing_company"
    return None


async def _classify(conn: asyncpg.Connection, subject: SubjectInput) -> dict[str, Any]:
    """Step 3, read part: who owns the submitted keys.

    Returns ``{"outcome": "none_owned"}`` when no key is owned, else the final outcome.
    """
    keys = [i.key for i in subject.identifiers]
    rows = await conn.fetch(_OWNERS_SQL, [k[0] for k in keys], [k[1] for k in keys])
    owners = {r["subject_id"] for r in rows}
    if not owners:
        return {"outcome": "none_owned"}
    if len(owners) > 1:
        return {"outcome": "conflict", "reason": "two_owners"}
    owner_id = next(iter(owners))
    submitted = set(keys)
    held = {(r["id_type"], r["id_norm"]) for r in await conn.fetch(_OWNER_KEYS_SQL, owner_id)}
    if submitted != held:
        reason = "set_differs" if submitted < held or submitted > held else "partial_overlap"
        return {"outcome": "conflict", "reason": reason, "subject_id": owner_id}
    owner = await conn.fetchrow(_OWNER_ROW_SQL, owner_id)
    if owner["kind"] != subject.kind:
        return {"outcome": "conflict", "reason": "contradiction", "subject_id": owner_id}
    both_have_dob = owner["date_of_birth"] is not None and subject.date_of_birth is not None
    if both_have_dob and owner["date_of_birth"] != subject.date_of_birth:
        return {"outcome": "conflict", "reason": "contradiction", "subject_id": owner_id}
    basis = "identifiers_and_dob" if both_have_dob else "identifiers_only"
    return {"outcome": "already_registered", "subject_id": owner_id, "match_basis": basis}


async def _attempt(
    conn: asyncpg.Connection, subject: SubjectInput, dry_run: bool
) -> dict[str, Any]:
    crm = await _crm_match(conn, subject)
    if crm:
        return {"outcome": crm}
    state = await _classify(conn, subject)
    if state["outcome"] != "none_owned":
        return state
    if dry_run:
        return {"outcome": "would_create"}
    subject_id = await conn.fetchval(
        _INSERT_SUBJECT_SQL,
        subject.kind,
        subject.display_name,
        subject.date_of_birth,
        subject.nationality,
        subject.sex,
        json.dumps(subject.evidence),
        REGISTRY_ACTOR,
    )
    for ident in subject.identifiers:
        won = await conn.fetchval(
            _INSERT_IDENTIFIER_SQL,
            ident.id_type,
            ident.id_norm,
            ident.id_raw,
            NORM_VERSION,
            subject_id,
            json.dumps(ident.evidence),
        )
        if won is None:
            raise _LostRace
    return {"outcome": "created", "subject_id": subject_id}


async def register_subject(
    conn: asyncpg.Connection, subject: SubjectInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Register one subject on ``conn``; classify first, write only when nothing conflicts.

    A lost race rolls the whole transaction back (the subject row included) and the request
    is classified again, once. A deadlock or serialization failure is retried once. A second
    failure of either kind raises ``RegistryUnavailable``.
    """
    lost_race_seen = False
    db_error_seen = False
    while True:
        try:
            async with conn.transaction():
                result = await _attempt(conn, subject, dry_run)
        except _LostRace:
            if lost_race_seen:
                raise RegistryUnavailable("registry_busy") from None
            lost_race_seen = True
            continue
        except (asyncpg.DeadlockDetectedError, asyncpg.SerializationError):
            if db_error_seen:
                raise RegistryUnavailable("registry_busy") from None
            db_error_seen = True
            continue
        result["dry_run"] = dry_run
        logger.info(
            "related subject registry: outcome=%s subject_id=%s keys=%d dry_run=%s",
            result["outcome"],
            result.get("subject_id"),
            len(subject.identifiers),
            dry_run,
        )
        return result
