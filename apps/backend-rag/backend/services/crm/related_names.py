"""Registry of related subjects, slice 4A: RECORDED NAMES.

A recorded name is a fact about a subject with its source: it is added, never overwritten. The
service writes ``related_subject_names`` by INSERT only (``ON CONFLICT DO NOTHING``), never
UPDATE or DELETE, and an existing row keeps its evidence. The producer's evidence numbers are an
assertion this module never verifies: it sees no OCR text.

The service receives ONE connection and opens no other; one request is one transaction on it. It
logs the subject id, the outcome and counts only: never a name and never an identifier.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from typing import Any

import asyncpg

from backend.services.crm.related_subjects import (
    _OWNER_KEYS_SQL,
    _OWNERS_SQL,
    REGISTRY_ACTOR,
    IdentifierInput,
)

logger = logging.getLogger(__name__)

MAX_RAW_NAME = 400
MAX_NAME = 200
NAME_SOURCES = ("ocr_corroborated", "ocr_single", "mrz_corrected")

# digits an OCR reading confuses with letters, and the letter each one stands for
_DIGIT_TO_LETTER = str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B"})

_OWNER_NAMES_SQL = "SELECT kind, display_name, merged_into FROM related_subjects WHERE id = $1"
_EXISTING_SQL = (
    "SELECT source, name FROM related_subject_names WHERE subject_id = $1 "
    "AND (source, name) IN (SELECT * FROM unnest($2::text[], $3::text[]))"
)
_INSERT_NAME_SQL = (
    "INSERT INTO related_subject_names (subject_id, name, source, evidence, created_by) "
    "VALUES ($1, $2, $3, $4::jsonb, $5) "
    "ON CONFLICT (subject_id, source, name) DO NOTHING RETURNING id"
)


class InvalidName(ValueError):
    """A name is not valid. The message is a fixed word and never carries the name."""


def normalise_name(raw: str) -> str:
    """The stored form of a submitted name (section 3.3 of the spec), or ``InvalidName``.

    NFC, white space collapsed as ``str.split()`` does, upper-case, NFC again; then 1 to 200
    characters, at least one letter (category L), no control or format character (Cc, Cf).
    """
    text = " ".join(unicodedata.normalize("NFC", raw).split())
    text = unicodedata.normalize("NFC", text.upper())
    if not 1 <= len(text) <= MAX_NAME:
        raise InvalidName("invalid_name")
    categories = [unicodedata.category(c) for c in text]
    if not any(c.startswith("L") for c in categories):
        raise InvalidName("invalid_name")
    if any(c in ("Cc", "Cf") for c in categories):
        raise InvalidName("invalid_name")
    return text


@dataclass(frozen=True)
class NameEntry:
    name: str  # already normalised
    source: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class NamesInput:
    kind: str
    identifiers: tuple[IdentifierInput, ...]
    names: tuple[NameEntry, ...]


async def _resolve_subject(conn: asyncpg.Connection, request: NamesInput) -> dict[str, Any]:
    """The subject owning exactly the submitted key set, or the reason there is none."""
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
    owner = await conn.fetchrow(_OWNER_NAMES_SQL, owner_id)
    if owner["kind"] != request.kind:
        return {"reason": "kind_differs"}
    if owner["merged_into"] is not None:
        return {"reason": "merged"}
    return {"subject_id": owner_id, "kind": owner["kind"], "display_name": owner["display_name"]}


def _correction_is_provable(
    kind: str, registration_name: str | None, entries: tuple[NameEntry, ...]
) -> bool:
    """Every ``mrz_corrected`` entry must be the registration name with its digits read as letters."""
    corrections = [e for e in entries if e.source == "mrz_corrected"]
    if not corrections:
        return True
    if kind != "person" or registration_name is None:
        return False
    try:
        registered = normalise_name(registration_name)
    except InvalidName:
        return False
    if not any(d in registered for d in "0158"):
        return False
    repaired = registered.translate(_DIGIT_TO_LETTER)
    return all(e.name != registered and e.name == repaired for e in corrections)


async def record_names(
    conn: asyncpg.Connection, request: NamesInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Record the submitted names on the subject that owns the submitted key set.

    A refusal of the subject or of a correction refuses the whole request: nothing is written.
    One transaction, one INSERT per entry, no retry. The answer is built after the commit.
    """
    entries = tuple(sorted(request.names, key=lambda e: (e.source, e.name)))
    async with conn.transaction():
        resolved = await _resolve_subject(conn, request)
        if "subject_id" not in resolved:
            result: dict[str, Any] = {
                "outcome": "subject_not_registered",
                "reason": resolved["reason"],
            }
        else:
            subject_id = resolved["subject_id"]
            if not _correction_is_provable(resolved["kind"], resolved["display_name"], entries):
                result = {"outcome": "correction_refused", "subject_id": subject_id}
            elif dry_run:
                existing = {
                    (r["source"], r["name"])
                    for r in await conn.fetch(
                        _EXISTING_SQL,
                        subject_id,
                        [e.source for e in entries],
                        [e.name for e in entries],
                    )
                }
                new = sum(1 for e in entries if (e.source, e.name) not in existing)
                result = _recorded(subject_id, new, len(entries) - new, dry_run=True)
            else:
                new = 0
                for e in entries:
                    won = await conn.fetchval(
                        _INSERT_NAME_SQL,
                        subject_id,
                        e.name,
                        e.source,
                        e.evidence,
                        REGISTRY_ACTOR,
                    )
                    if won is not None:
                        new += 1
                result = _recorded(subject_id, new, len(entries) - new, dry_run=False)
    logger.info(
        "related names registry: outcome=%s subject_id=%s recorded=%s already_recorded=%s "
        "dry_run=%s",
        result["outcome"],
        result.get("subject_id"),
        result.get("recorded"),
        result.get("already_recorded"),
        dry_run,
    )
    return result


def _recorded(subject_id: int, recorded: int, already: int, *, dry_run: bool) -> dict[str, Any]:
    return {
        "outcome": "recorded",
        "subject_id": subject_id,
        "recorded": recorded,
        "already_recorded": already,
        "dry_run": dry_run,
    }
