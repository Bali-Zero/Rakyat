"""Registry of related subjects, slice 4B: PROPOSED CLIENT LINKS.

A link is a proposal: a recorded fact about a subject and a CRM client with its basis and the
producer's evidence. It is added, never overwritten. The service writes ``related_subject_links``
by INSERT only (``ON CONFLICT DO NOTHING``), never UPDATE or DELETE, and an existing row keeps its
evidence and relation. The only other table it names is ``clients``, for one SELECT at write time.

The service receives ONE connection and opens no other; one request is one transaction on it. It
logs the subject id, the outcome and counts only: never a client id, a client name or an
identifier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from backend.services.crm.related_names import NamesInput, _resolve_subject
from backend.services.crm.related_subjects import REGISTRY_ACTOR, IdentifierInput

logger = logging.getLogger(__name__)

LINK_BASES = ("drive_folder", "whatsapp_phone", "identity")

_CLIENTS_SQL = "SELECT id FROM clients WHERE id = ANY($1::int[]) AND deleted_at IS NULL"
_EXISTING_SQL = (
    "SELECT client_id, basis FROM related_subject_links WHERE subject_id = $1 "
    "AND (client_id, basis) IN (SELECT * FROM unnest($2::int[], $3::text[]))"
)
_INSERT_LINK_SQL = (
    "INSERT INTO related_subject_links (subject_id, client_id, basis, evidence, created_by) "
    "VALUES ($1, $2, $3, $4::jsonb, $5) "
    "ON CONFLICT (subject_id, client_id, basis) DO NOTHING RETURNING id"
)


@dataclass(frozen=True)
class LinkEntry:
    client_id: int
    basis: str
    evidence: dict[str, Any]


@dataclass(frozen=True)
class LinksInput:
    kind: str
    identifiers: tuple[IdentifierInput, ...]
    links: tuple[LinkEntry, ...]


async def record_links(
    conn: asyncpg.Connection, request: LinksInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Record the submitted links on the subject that owns the submitted key set.

    A refusal of the subject or of a client refuses the whole request: nothing is written. The
    subject is judged before the clients. One transaction, one INSERT per entry, no retry. The
    answer is built after the commit.
    """
    submitted = request.links
    entries = tuple(sorted(submitted, key=lambda e: (e.client_id, e.basis)))
    missing = 0
    async with conn.transaction():
        resolved = await _resolve_subject(
            conn, NamesInput(kind=request.kind, identifiers=request.identifiers, names=())
        )
        if "subject_id" not in resolved:
            result: dict[str, Any] = {
                "outcome": "subject_not_registered",
                "reason": resolved["reason"],
            }
        else:
            subject_id = resolved["subject_id"]
            found = {
                r["id"]
                for r in await conn.fetch(_CLIENTS_SQL, sorted({e.client_id for e in entries}))
            }
            absent = [i for i, e in enumerate(submitted) if e.client_id not in found]
            if absent:
                missing = len(absent)
                result = {
                    "outcome": "client_not_found",
                    "subject_id": subject_id,
                    "indexes": absent,
                }
            elif dry_run:
                existing = {
                    (r["client_id"], r["basis"])
                    for r in await conn.fetch(
                        _EXISTING_SQL,
                        subject_id,
                        [e.client_id for e in entries],
                        [e.basis for e in entries],
                    )
                }
                new = sum(1 for e in entries if (e.client_id, e.basis) not in existing)
                result = _recorded(subject_id, new, len(entries) - new, dry_run=True)
            else:
                new = 0
                for e in entries:
                    won = await conn.fetchval(
                        _INSERT_LINK_SQL,
                        subject_id,
                        e.client_id,
                        e.basis,
                        e.evidence,
                        REGISTRY_ACTOR,
                    )
                    if won is not None:
                        new += 1
                result = _recorded(subject_id, new, len(entries) - new, dry_run=False)
    if result["outcome"] == "client_not_found":
        logger.info(
            "related links registry: outcome=%s subject_id=%s recorded=%s already_recorded=%s "
            "dry_run=%s missing=%d",
            result["outcome"],
            result.get("subject_id"),
            result.get("recorded"),
            result.get("already_recorded"),
            dry_run,
            missing,
        )
    else:
        logger.info(
            "related links registry: outcome=%s subject_id=%s recorded=%s already_recorded=%s "
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
