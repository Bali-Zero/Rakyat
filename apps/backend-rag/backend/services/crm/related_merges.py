"""Registry of related subjects, slice 4C: MERGES.

Two records of the registry that are the same person or company become one for every reader by a
POINTER: ``related_subjects.merged_into`` is set once, from NULL to the survivor's id, on the
merged-away record (the source). Nothing moves: identifiers, documents, names and links stay on the
rows that own them. The pointer is the single truth; ``related_subject_merges`` is insert-only
history (what was merged, when, on which basis) and no decision here reads it.

The service writes exactly two things in one transaction: one INSERT into
``related_subject_merges`` and the registry's one sanctioned UPDATE (``_UPDATE_POINTER_SQL``, one
row, one column). It never writes identifiers, documents, names, links or any CRM table.

The service receives ONE connection and opens no other; one request is one transaction on it. It
logs the outcome and the two subject ids only: never an identifier, a name or the request.
"""

from __future__ import annotations

import logging
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

MERGE_BASES = ("permit_stamp",)

_MERGE_ROW_SQL = "SELECT kind, merged_into FROM related_subjects WHERE id = $1"
_LOCK_SQL = "SELECT id, kind, merged_into FROM related_subjects WHERE id = $1 FOR UPDATE"
_SURVIVOR_SQL = "SELECT 1 FROM related_subjects WHERE merged_into = $1 LIMIT 1"
_TYPES_SQL = "SELECT subject_id, id_type FROM related_subject_identifiers WHERE subject_id = ANY($1::bigint[])"
_INSERT_MERGE_SQL = (
    "INSERT INTO related_subject_merges (source_id, target_id, basis, evidence, created_by) "
    "VALUES ($1, $2, $3, $4::jsonb, $5)"
)
_UPDATE_POINTER_SQL = (
    "UPDATE related_subjects SET merged_into = $2 WHERE id = $1 AND merged_into IS NULL "
    "RETURNING id"
)


class RegistryInconsistent(RuntimeError):
    """The pointer UPDATE touched no row under the lock: a defence, not a state."""


@dataclass(frozen=True)
class MergeSide:
    kind: str
    identifiers: tuple[IdentifierInput, ...]


@dataclass(frozen=True)
class MergeInput:
    source: MergeSide
    target: MergeSide
    basis: str
    evidence: dict[str, Any]


async def _resolve_owner(conn: asyncpg.Connection, side: MergeSide) -> dict[str, Any]:
    """The subject owning exactly the submitted key set, merged or not, or the reason there is none.

    The same three steps and reasons as ``related_names._resolve_subject`` (parity is pinned by a
    test), except that a merged owner is returned with its pointer instead of refused.
    """
    keys = [i.key for i in side.identifiers]
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
    owner = await conn.fetchrow(_MERGE_ROW_SQL, owner_id)
    if owner["kind"] != side.kind:
        return {"reason": "kind_differs"}
    return {"subject_id": owner_id, "kind": owner["kind"], "merged_into": owner["merged_into"]}


def _not_registered(side: str, reason: str) -> dict[str, Any]:
    return {"outcome": "subject_not_registered", "side": side, "reason": reason}


def _not_mergeable(reason: str, source_id: int, target_id: int) -> dict[str, Any]:
    return {
        "outcome": "not_mergeable",
        "reason": reason,
        "source_id": source_id,
        "target_id": target_id,
    }


def _merged(source_id: int, target_id: int, outcome: str, dry_run: bool) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "source_id": source_id,
        "target_id": target_id,
        "dry_run": dry_run,
    }


def _decide_on_rows(
    source_id: int, source: Any, target_id: int, target: Any, dry_run: bool
) -> dict[str, Any] | None:
    """Steps 2 to 5 of the decision order, on the rows as given. ``None`` = still mergeable."""
    if source["merged_into"] == target_id:
        return _merged(source_id, target_id, "already_merged", dry_run)
    if source["kind"] != target["kind"]:
        return _not_mergeable("kind_differs", source_id, target_id)
    if target["merged_into"] is not None:
        return _not_mergeable("target_is_merged", source_id, target_id)
    if source["merged_into"] is not None:
        return _not_mergeable("source_merged_elsewhere", source_id, target_id)
    return None


async def _decide_on_data(
    conn: asyncpg.Connection, source_id: int, target_id: int
) -> dict[str, Any] | None:
    """Steps 6 and 7: the source is no survivor; the basis holds. ``None`` = mergeable."""
    if await conn.fetchval(_SURVIVOR_SQL, source_id) is not None:
        return _not_mergeable("source_is_survivor", source_id, target_id)
    rows = await conn.fetch(_TYPES_SQL, [source_id, target_id])
    source_types = {r["id_type"] for r in rows if r["subject_id"] == source_id}
    target_types = {r["id_type"] for r in rows if r["subject_id"] == target_id}
    if source_types != {"permit"} or "passport" not in target_types:
        return _not_mergeable("basis_precondition", source_id, target_id)
    return None


async def record_merge(
    conn: asyncpg.Connection, request: MergeInput, *, dry_run: bool = True
) -> dict[str, Any]:
    """Record that the source subject is the target subject, or say why it cannot be.

    Resolution takes no lock (source first, then target). A dry run stops after the decision and
    writes nothing. An apply locks both rows in ascending id order, decides again from the LOCKED
    rows, then writes the history row and the pointer in the same transaction. The answer is built
    after the commit.
    """
    source_id: int | None = None
    target_id: int | None = None
    async with conn.transaction():
        source = await _resolve_owner(conn, request.source)
        if "subject_id" not in source:
            result = _not_registered("source", source["reason"])
        else:
            source_id = source["subject_id"]
            target = await _resolve_owner(conn, request.target)
            if "subject_id" not in target:
                result = _not_registered("target", target["reason"])
            else:
                target_id = target["subject_id"]
                result = await _judge(conn, request, source, target, dry_run)
    if result["outcome"] == "subject_not_registered":
        if result["side"] == "source":
            source_id = None
        else:
            target_id = None
    logger.info(
        "related merges registry: outcome=%s source_id=%s target_id=%s dry_run=%s",
        result["outcome"],
        source_id,
        target_id,
        dry_run,
    )
    return result


async def _judge(
    conn: asyncpg.Connection,
    request: MergeInput,
    source: dict[str, Any],
    target: dict[str, Any],
    dry_run: bool,
) -> dict[str, Any]:
    source_id, target_id = source["subject_id"], target["subject_id"]
    if source_id == target_id:
        return _not_mergeable("same_subject", source_id, target_id)
    if dry_run:
        decided = _decide_on_rows(
            source_id, source, target_id, target, dry_run
        ) or await _decide_on_data(conn, source_id, target_id)
        return decided or _merged(source_id, target_id, "merged", True)
    locked: dict[int, Any] = {}
    for subject_id in sorted((source_id, target_id)):
        locked[subject_id] = await conn.fetchrow(_LOCK_SQL, subject_id)
    if locked[source_id] is None:
        return _not_registered("source", "none_owned")
    if locked[target_id] is None:
        return _not_registered("target", "none_owned")
    decided = _decide_on_rows(
        source_id, locked[source_id], target_id, locked[target_id], False
    ) or await _decide_on_data(conn, source_id, target_id)
    if decided is not None:
        return decided
    await conn.execute(
        _INSERT_MERGE_SQL,
        source_id,
        target_id,
        request.basis,
        request.evidence,
        REGISTRY_ACTOR,
    )
    if await conn.fetchval(_UPDATE_POINTER_SQL, source_id, target_id) is None:
        raise RegistryInconsistent("pointer_not_set")
    return _merged(source_id, target_id, "merged", False)
