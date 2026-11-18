"""Read-only access to the related-subjects registry (tables of migrations 361, 362, 365 and 367,
and ``clients``, SELECT only, to show the name of a linked client).

Every statement here is a SELECT with bound parameters; no user text is interpolated into SQL.
Evidence columns (``evidence``, ``created_by``) are never selected. Nothing in this module logs.

The name a reader sees is the EFFECTIVE name: the top candidate among the registration name
(``related_subjects.display_name``) and the recorded names, by source rank (manual, mrz_corrected,
registration, ocr_corroborated, ocr_single), then newest ``created_at``, then highest ``id``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import asyncpg

from backend.services.crm.entity_search import escape_like
from backend.services.crm.related_identifiers import ID_TYPES, canonical_form

MIN_PREFIX = 3
MAX_DETAIL_DOCUMENTS = 200
MAX_DETAIL_NAMES = 20
MAX_DETAIL_LINKS = 20

# The effective name, selected in SQL so that ORDER BY and LIMIT see it. The registration name is
# one candidate (rank 3, stamped with the subject's creation, id 0); recorded rows follow their
# source rank. No CTE: one lateral subquery per subject.
_EFFECTIVE_SQL = """
LEFT JOIN LATERAL (
    SELECT c.name, c.source
    FROM (
        SELECT s.display_name AS name, 'registration'::text AS source, 3 AS rk,
               s.created_at AS stamp, 0::bigint AS cid
        WHERE s.display_name IS NOT NULL
        UNION ALL
        SELECT n.name, n.source,
               CASE n.source WHEN 'manual' THEN 1 WHEN 'mrz_corrected' THEN 2
                             WHEN 'ocr_corroborated' THEN 4 ELSE 5 END,
               n.created_at, n.id
        FROM related_subject_names n
        WHERE n.subject_id = s.id
    ) c
    ORDER BY c.rk ASC, c.stamp DESC, c.cid DESC
    LIMIT 1
) eff ON TRUE
"""

# A subject matches a name search when its registration name or any recorded name does (the
# effective name is one of them), or an identifier prefix does.
_MATCH_SQL = """
WHERE s.merged_into IS NULL
  AND ($1::text IS NULL OR s.kind = $1)
  AND (
    $2::text IS NULL
    OR s.display_name ILIKE '%' || $2 || '%' ESCAPE '\\'
    OR EXISTS (
        SELECT 1
        FROM related_subject_names n
        WHERE n.subject_id = s.id AND n.name ILIKE '%' || $2 || '%' ESCAPE '\\'
    )
    OR EXISTS (
        SELECT 1
        FROM related_subject_identifiers i
        JOIN unnest($3::text[], $4::text[]) AS p(id_type, pattern)
          ON i.id_type = p.id_type AND i.id_norm LIKE p.pattern ESCAPE '\\'
        WHERE i.subject_id = s.id
    )
  )
  AND (
    $5::boolean IS NULL
    OR $5::boolean = EXISTS (SELECT 1 FROM related_subject_links lk WHERE lk.subject_id = s.id)
  )
"""

_TOTAL_SQL = "SELECT count(*) FROM related_subjects s " + _MATCH_SQL

_PAGE_SQL = (
    "SELECT s.id, s.kind, eff.name AS display_name, eff.source AS name_source, "
    "s.date_of_birth, s.nationality, "
    "(SELECT count(*) FROM related_subject_links lc WHERE lc.subject_id = s.id) AS links_count "
    "FROM related_subjects s "
    + _EFFECTIVE_SQL
    + _MATCH_SQL
    + " ORDER BY eff.name ASC NULLS LAST, s.id ASC LIMIT $6 OFFSET $7"
)

_IDENTIFIERS_SQL = """
SELECT subject_id, id_type, id_raw
FROM related_subject_identifiers
WHERE subject_id = ANY($1::bigint[])
ORDER BY subject_id, id_type, id_raw
"""

_DOCUMENT_COUNTS_SQL = """
SELECT subject_id, count(*) AS n
FROM related_subject_documents
WHERE subject_id = ANY($1::bigint[])
GROUP BY subject_id
"""

_SUBJECT_SQL = (
    "SELECT s.id, s.kind, eff.name AS display_name, eff.source AS name_source, "
    "s.display_name AS registration_name, s.date_of_birth, s.nationality, s.sex, s.created_at "
    "FROM related_subjects s " + _EFFECTIVE_SQL + "WHERE s.id = $1"
)

_NAMES_SQL = """
SELECT name, source, created_at, count(*) OVER () AS n
FROM related_subject_names
WHERE subject_id = $1
ORDER BY created_at DESC, id DESC
LIMIT $2
"""

_LINKS_SQL = """
SELECT l.client_id,
       CASE WHEN c.id IS NOT NULL AND c.deleted_at IS NULL THEN c.full_name END AS client_name,
       (c.id IS NOT NULL AND c.deleted_at IS NULL) AS client_live,
       l.basis, l.relation, l.created_at, count(*) OVER () AS n
FROM related_subject_links l
LEFT JOIN clients c ON c.id = l.client_id
WHERE l.subject_id = $1
ORDER BY l.created_at ASC, l.id ASC
LIMIT $2
"""

_DOCUMENTS_SQL = """
SELECT d.sha256, sd.doc_type, d.mime_type, d.byte_size, d.first_seen_on
FROM related_subject_documents sd
JOIN related_documents d ON d.sha256 = sd.sha256
WHERE sd.subject_id = $1
ORDER BY sd.doc_type ASC NULLS LAST, d.sha256 ASC
LIMIT $2
"""

_SIGHTINGS_SQL = """
SELECT sha256, ref
FROM related_document_sightings
WHERE kind = 'drive' AND sha256 = ANY($1::text[])
ORDER BY sha256, ref
"""


def identifier_patterns(q: str) -> tuple[list[str], list[str]]:
    """LIKE patterns (type, prefix pattern) for the identifier types ``q`` can be a key prefix of."""
    types: list[str] = []
    patterns: list[str] = []
    for id_type in sorted(ID_TYPES):
        canonical = canonical_form(id_type, q)
        if len(canonical) >= MIN_PREFIX:
            types.append(id_type)
            patterns.append(escape_like(canonical) + "%")
    return types, patterns


async def list_subjects(
    conn: asyncpg.Connection,
    *,
    q: str | None,
    kind: str | None,
    limit: int,
    offset: int,
    linked: bool | None = None,
) -> dict[str, Any]:
    """One page of unmerged subjects matching ``q``, ``kind`` and ``linked``, with the total."""
    types: list[str] = []
    patterns: list[str] = []
    if q is not None:
        types, patterns = identifier_patterns(q)
        text_q: str | None = escape_like(q)
    else:
        text_q = None
    total = await conn.fetchval(_TOTAL_SQL, kind, text_q, types, patterns, linked)
    rows = await conn.fetch(_PAGE_SQL, kind, text_q, types, patterns, linked, limit, offset)
    ids = [r["id"] for r in rows]
    identifiers: dict[int, list[dict[str, str]]] = defaultdict(list)
    counts: dict[int, int] = {}
    if ids:
        for r in await conn.fetch(_IDENTIFIERS_SQL, ids):
            identifiers[r["subject_id"]].append({"type": r["id_type"], "value": r["id_raw"]})
        for r in await conn.fetch(_DOCUMENT_COUNTS_SQL, ids):
            counts[r["subject_id"]] = r["n"]
    items = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "display_name": r["display_name"],
            "name_source": r["name_source"],
            "date_of_birth": r["date_of_birth"],
            "nationality": r["nationality"],
            "identifiers": identifiers.get(r["id"], []),
            "document_count": counts.get(r["id"], 0),
            "links_count": r["links_count"],
        }
        for r in rows
    ]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def get_subject(conn: asyncpg.Connection, subject_id: int) -> dict[str, Any] | None:
    """The subject with its identifiers and documents, or None when the id does not exist."""
    row = await conn.fetchrow(_SUBJECT_SQL, subject_id)
    if row is None:
        return None
    identifiers = [
        {"type": r["id_type"], "value": r["id_raw"]}
        for r in await conn.fetch(_IDENTIFIERS_SQL, [subject_id])
    ]
    doc_rows = await conn.fetch(_DOCUMENTS_SQL, subject_id, MAX_DETAIL_DOCUMENTS)
    sightings: dict[str, list[str]] = defaultdict(list)
    if doc_rows:
        for r in await conn.fetch(_SIGHTINGS_SQL, [d["sha256"] for d in doc_rows]):
            sightings[r["sha256"]].append(r["ref"])
    documents = [
        {
            "sha256": d["sha256"],
            "doc_type": d["doc_type"],
            "mime_type": d["mime_type"],
            "byte_size": d["byte_size"],
            "first_seen_on": d["first_seen_on"],
            "drive_file_ids": sorted(sightings.get(d["sha256"], [])),
        }
        for d in doc_rows
    ]
    registered = row["registration_name"]
    name_rows = await conn.fetch(
        _NAMES_SQL, subject_id, MAX_DETAIL_NAMES - (1 if registered is not None else 0)
    )
    names = [
        {"name": r["name"], "source": r["source"], "created_at": r["created_at"]} for r in name_rows
    ]
    if registered is not None:
        names.insert(
            0, {"name": registered, "source": "registration", "created_at": row["created_at"]}
        )
    names_total = (name_rows[0]["n"] if name_rows else 0) + (1 if registered is not None else 0)
    link_rows = await conn.fetch(_LINKS_SQL, subject_id, MAX_DETAIL_LINKS)
    links = [
        {
            "client_id": r["client_id"],
            "client_name": r["client_name"],
            "client_live": r["client_live"],
            "basis": r["basis"],
            "relation": r["relation"],
            "created_at": r["created_at"],
        }
        for r in link_rows
    ]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "display_name": row["display_name"],
        "name_source": row["name_source"],
        "names": names,
        "names_total": names_total,
        "links": links,
        "links_total": link_rows[0]["n"] if link_rows else 0,
        "date_of_birth": row["date_of_birth"],
        "nationality": row["nationality"],
        "sex": row["sex"],
        "created_at": row["created_at"],
        "identifiers": identifiers,
        "documents": documents,
    }
