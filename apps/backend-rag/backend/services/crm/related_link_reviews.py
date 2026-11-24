"""Human review of a proposed registry link (table of migration 372).

A CRM admin confirms (with a relation) or rejects the link between a registry subject and a client.
``record_review`` receives ONE connection and runs one transaction on it: the subject row is read
``FOR SHARE`` (a concurrent merge of it takes ``FOR UPDATE`` and so waits, and this transaction waits
for a merge already holding the row), the member set and the link are checked inside the same
transaction, and the row is inserted only when the effective review differs from the request.

Insert-only: the effective review of a subject and a client is the row with the greatest
``(created_at, id)`` over the member set (the subject and the records merged into it). Nothing in
this module logs and nothing here returns a client id, a client name or an input.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

DECISIONS = ("confirmed", "rejected")
RELATIONS = ("self", "spouse", "child", "employee", "other")

_LOCK_SQL = "SELECT merged_into FROM related_subjects WHERE id = $1 FOR SHARE"
_MEMBERS_SQL = "SELECT id FROM related_subjects WHERE merged_into = $1"
_LINK_SQL = (
    "SELECT 1 FROM related_subject_links WHERE subject_id = ANY($1::bigint[]) "
    "AND client_id = $2 LIMIT 1"
)
_EFFECTIVE_SQL = (
    "SELECT decision, relation, decided_by, created_at FROM related_subject_link_reviews "
    "WHERE subject_id = ANY($1::bigint[]) AND client_id = $2 "
    "ORDER BY created_at DESC, id DESC LIMIT 1"
)
_INSERT_SQL = (
    "INSERT INTO related_subject_link_reviews (subject_id, client_id, decision, relation, decided_by) "
    "VALUES ($1, $2, $3, $4, $5) RETURNING decision, relation, decided_by, created_at"
)


def review_dict(row: asyncpg.Record | dict[str, Any]) -> dict[str, Any]:
    """The answer shape of a review: closed words, the deciding admin and the timestamp."""
    decided_at: datetime = row["created_at"]
    return {
        "decision": row["decision"],
        "relation": row["relation"],
        "decided_by": row["decided_by"],
        "decided_at": decided_at,
    }


async def record_review(
    conn: asyncpg.Connection,
    *,
    subject_id: int,
    client_id: int,
    decision: str,
    relation: str | None,
    decided_by: str,
) -> dict[str, Any]:
    """Record one decision, or say why it cannot be. ``outcome`` is one of subject_not_found,
    subject_is_merged, link_not_found, unchanged, recorded; the last two carry ``review``."""
    async with conn.transaction():
        subject = await conn.fetchrow(_LOCK_SQL, subject_id)
        if subject is None:
            return {"outcome": "subject_not_found"}
        if subject["merged_into"] is not None:
            return {"outcome": "subject_is_merged"}
        members = [subject_id] + [r["id"] for r in await conn.fetch(_MEMBERS_SQL, subject_id)]
        if await conn.fetchval(_LINK_SQL, members, client_id) is None:
            return {"outcome": "link_not_found"}
        effective = await conn.fetchrow(_EFFECTIVE_SQL, members, client_id)
        if (
            effective is not None
            and effective["decision"] == decision
            and effective["relation"] == relation
        ):
            return {"outcome": "unchanged", "review": review_dict(effective)}
        row = await conn.fetchrow(
            _INSERT_SQL, subject_id, client_id, decision, relation, decided_by
        )
        return {"outcome": "recorded", "review": review_dict(row)}
