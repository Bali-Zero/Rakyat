"""Read-only access to the related-subjects registry (tables of migrations 361, 362, 365, 367, 369,
370 and 372, and ``clients``, SELECT only, to show the name of a linked client and to compute the
link suggestion).

Every statement here is a SELECT with bound parameters; no user text is interpolated into SQL.
Evidence columns (``evidence``, ``created_by``) are never selected, with ONE exception: the whole
NUMBER of files a client link rests on (``related_subject_links.evidence->'files'``), never a file
name, hash or reference. Nothing in this module logs.

A merged-away record points at its survivor (``related_subjects.merged_into``, one level, set by the
merge route and never recursed here). The "member set" of a subject S is S plus the rows whose
``merged_into`` equals S; a survivor shows the union of its members' identifiers, documents, names
and client links, each item carrying ``from_subject_id`` (None for the subject's own). A subject in
no merge gets exactly the answer it had before, plus empty new fields.

The name a reader sees is the EFFECTIVE name: the top candidate over the member set among the
registration names (``related_subjects.display_name``) and the recorded names, by source rank
(manual, mrz_corrected, registration, ocr_corroborated, ocr_single), then the survivor's OWN
candidates before merged-in ones, then newest ``created_at``, then highest candidate ``id`` and,
last, highest member id.
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

# The member set of the outer subject ``s``: ``s`` itself and the rows merged into it. UNION ALL of
# two plain selects (no CTE); the second branch is served by idx_related_subjects_merged_into.
_MEMBERS_FRAGMENT = (
    "(SELECT s.id AS id, s.display_name AS display_name, s.created_at AS created_at "
    "UNION ALL "
    "SELECT m.id, m.display_name, m.created_at FROM related_subjects m WHERE m.merged_into = s.id)"
)
_MEMBER_IDS_FRAGMENT = (
    "(SELECT s.id UNION ALL SELECT m.id FROM related_subjects m WHERE m.merged_into = s.id)"
)

# The effective name, selected in SQL so that ORDER BY and LIMIT see it. Each member's registration
# name is a candidate (rank 3, stamped with that member's creation, id 0); recorded rows follow
# their source rank. The tie-break puts the subject's own candidates (``foreign_`` false) before the
# merged-in ones. No CTE: one lateral subquery per subject.
_EFFECTIVE_SQL = (
    """
LEFT JOIN LATERAL (
    SELECT c.name, c.source
    FROM (
        SELECT mem.display_name AS name, 'registration'::text AS source, 3 AS rk,
               mem.created_at AS stamp, 0::bigint AS cid, mem.id AS mid,
               (mem.id <> s.id) AS foreign_
        FROM """
    + _MEMBERS_FRAGMENT
    + """ mem
        WHERE mem.display_name IS NOT NULL
        UNION ALL
        SELECT n.name, n.source,
               CASE n.source WHEN 'manual' THEN 1 WHEN 'mrz_corrected' THEN 2
                             WHEN 'ocr_corroborated' THEN 4 ELSE 5 END,
               n.created_at, n.id, n.subject_id, (n.subject_id <> s.id)
        FROM related_subject_names n
        WHERE n.subject_id IN """
    + _MEMBER_IDS_FRAGMENT
    + """
    ) c
    ORDER BY c.rk ASC, c.foreign_ ASC, c.stamp DESC, c.cid DESC, c.mid DESC
    LIMIT 1
) eff ON TRUE
"""
)

# A survivor matches a name search when the registration name or a recorded name of ANY member does
# (the effective name is one of them), or an identifier prefix of any member does. Merged-away
# records are never listed themselves.
_MATCH_SQL = (
    """
WHERE s.merged_into IS NULL
  AND ($1::text IS NULL OR s.kind = $1)
  AND (
    $2::text IS NULL
    OR EXISTS (
        SELECT 1
        FROM """
    + _MEMBERS_FRAGMENT
    + """ mem
        WHERE mem.display_name ILIKE '%' || $2 || '%' ESCAPE '\\'
           OR EXISTS (
               SELECT 1
               FROM related_subject_names n
               WHERE n.subject_id = mem.id AND n.name ILIKE '%' || $2 || '%' ESCAPE '\\'
           )
           OR EXISTS (
               SELECT 1
               FROM related_subject_identifiers i
               JOIN unnest($3::text[], $4::text[]) AS p(id_type, pattern)
                 ON i.id_type = p.id_type AND i.id_norm LIKE p.pattern ESCAPE '\\'
               WHERE i.subject_id = mem.id
           )
    )
  )
  AND (
    $5::boolean IS NULL
    OR $5::boolean = EXISTS (
        SELECT 1 FROM related_subject_links lk WHERE lk.subject_id IN """
    + _MEMBER_IDS_FRAGMENT
    + """
    )
  )
"""
)

_TOTAL_SQL = "SELECT count(*) FROM related_subjects s " + _MATCH_SQL

_PAGE_SQL = (
    "SELECT s.id, s.kind, eff.name AS display_name, eff.source AS name_source, "
    "s.date_of_birth, s.nationality, "
    "(SELECT count(DISTINCT (lc.client_id, lc.basis)) FROM related_subject_links lc "
    "WHERE lc.subject_id IN " + _MEMBER_IDS_FRAGMENT + ") AS links_count "
    "FROM related_subjects s "
    + _EFFECTIVE_SQL
    + _MATCH_SQL
    + " ORDER BY eff.name ASC NULLS LAST, s.id ASC LIMIT $6 OFFSET $7"
)

# $1 = the ids whose member sets are read (page survivors, or the one opened subject): a row of $1
# owns itself, any other row selected is a member through its pointer. Owner first, then member id.
_OWNER_FRAGMENT = "CASE WHEN s.id = ANY($1::bigint[]) THEN s.id ELSE s.merged_into END"

_IDENTIFIERS_SQL = (
    "SELECT o.owner_id, o.subject_id, o.id_type, o.id_raw FROM ("
    "SELECT " + _OWNER_FRAGMENT + " AS owner_id, i.subject_id, i.id_type, i.id_raw "
    "FROM related_subjects s JOIN related_subject_identifiers i ON i.subject_id = s.id "
    "WHERE s.id = ANY($1::bigint[]) OR s.merged_into = ANY($1::bigint[])"
    ") o ORDER BY o.owner_id, (o.subject_id <> o.owner_id), o.subject_id, o.id_type, o.id_raw"
)

_DOCUMENT_COUNTS_SQL = (
    "SELECT o.owner_id, count(DISTINCT o.sha256) AS n FROM ("
    "SELECT " + _OWNER_FRAGMENT + " AS owner_id, sd.sha256 "
    "FROM related_subjects s JOIN related_subject_documents sd ON sd.subject_id = s.id "
    "WHERE s.id = ANY($1::bigint[]) OR s.merged_into = ANY($1::bigint[])"
    ") o GROUP BY o.owner_id"
)

_SUBJECT_SQL = (
    "SELECT s.id, s.kind, eff.name AS display_name, eff.source AS name_source, "
    "s.display_name AS registration_name, s.date_of_birth, s.nationality, s.sex, s.created_at, "
    "s.merged_into, "
    "(SELECT count(*) FROM related_subjects mm WHERE mm.merged_into = s.id) AS merged_count "
    "FROM related_subjects s " + _EFFECTIVE_SQL + "WHERE s.id = $1"
)

# The records merged into $1 with the date and basis of the LATEST history row of that
# (source, target) pair; NULL when the pointer has no history row (an operator's statement).
_MERGES_SQL = """
SELECT m.id, m.display_name, m.created_at, mg.basis, mg.created_at AS merged_at
FROM related_subjects m
LEFT JOIN LATERAL (
    SELECT x.basis, x.created_at
    FROM related_subject_merges x
    WHERE x.source_id = m.id AND x.target_id = m.merged_into
    ORDER BY x.created_at DESC, x.id DESC
    LIMIT 1
) mg ON TRUE
WHERE m.merged_into = $1
ORDER BY merged_at NULLS LAST, m.id
"""

# $1 = the subject plus its members. Always fetches up to $2 rows with the total of ALL rows.
_NAMES_SQL = """
SELECT subject_id, name, source, created_at, count(*) OVER () AS n
FROM related_subject_names
WHERE subject_id = ANY($1::bigint[])
ORDER BY created_at DESC, id DESC
LIMIT $2
"""

# One link per (client, basis), the earliest kept; the total is of the de-duplicated set.
_LINKS_SQL = """
SELECT x.subject_id, x.client_id, x.client_name, x.client_live, x.basis, x.relation, x.created_at,
       x.files, count(*) OVER () AS n
FROM (
    SELECT l.subject_id, l.client_id,
           CASE WHEN c.id IS NOT NULL AND c.deleted_at IS NULL THEN c.full_name END AS client_name,
           (c.id IS NOT NULL AND c.deleted_at IS NULL) AS client_live,
           l.basis, l.relation, l.created_at, l.id,
           CASE WHEN jsonb_typeof(l.evidence->'files') = 'number'
                     AND (l.evidence->>'files') ~ '^[0-9]{1,9}$'
                THEN (l.evidence->>'files')::int END AS files,
           row_number() OVER (
               PARTITION BY l.client_id, l.basis ORDER BY l.created_at ASC, l.id ASC
           ) AS rn
    FROM related_subject_links l
    LEFT JOIN clients c ON c.id = l.client_id
    WHERE l.subject_id = ANY($1::bigint[])
) x
WHERE x.rn = 1
ORDER BY x.created_at ASC, x.id ASC
LIMIT $2
"""

# The effective review (latest (created_at, id)) of each client over the member set ($1).
_REVIEWS_SQL = """
SELECT DISTINCT ON (client_id) client_id, decision, relation, decided_by, created_at
FROM related_subject_link_reviews
WHERE subject_id = ANY($1::bigint[]) AND client_id = ANY($2::int[])
ORDER BY client_id, created_at DESC, id DESC
"""

# The suggestion for each live client of $3 over the member set $1 and the opened subject $2.
# Only clients whose name equals a name of the member set are returned, and only booleans leave
# SQL: the client's passport number, birth date and name are compared here and never selected.
# The NIK cast is guarded by a CASE so that no other value is ever cast.
_SUGGESTIONS_SQL = r"""
SELECT c.id AS client_id,
       EXISTS (
           SELECT 1 FROM related_subject_identifiers i
           WHERE i.subject_id = ANY($1::bigint[]) AND i.id_type = 'passport'
             AND p.num <> '' AND i.id_norm = p.num
       ) AS passport_equal,
       EXISTS (
           SELECT 1 FROM related_subject_identifiers i
           WHERE i.subject_id = ANY($1::bigint[]) AND i.id_type = 'passport'
             AND length(p.num) >= 6 AND length(i.id_norm) = length(p.num)
             AND (
                 SELECT count(*) FROM generate_series(1, length(p.num)) g
                 WHERE substr(i.id_norm, g, 1) <> substr(p.num, g, 1)
             ) = 1
       ) AS passport_one_char_apart,
       EXISTS (
           SELECT 1 FROM related_subject_identifiers i
           WHERE i.subject_id = ANY($1::bigint[]) AND i.id_type = 'nik'
             AND c.date_of_birth IS NOT NULL
             AND i.id_norm ~ '^[0-9]{16}$'
             AND substr(i.id_norm, 9, 4) = to_char(c.date_of_birth, 'MMYY')
             AND CASE WHEN i.id_norm ~ '^[0-9]{16}$' THEN substr(i.id_norm, 7, 2)::int END
                 IN (extract(day FROM c.date_of_birth)::int,
                     extract(day FROM c.date_of_birth)::int + 40)
       ) AS nik_encodes_client_birth_date,
       EXISTS (
           SELECT 1 FROM related_subjects own
           WHERE own.id = $2 AND own.date_of_birth IS NOT NULL
             AND own.date_of_birth = c.date_of_birth
       ) AS birth_date_equal
FROM clients c
CROSS JOIN LATERAL (
    SELECT upper(regexp_replace(coalesce(c.passport_number, ''), '[^A-Za-z0-9]', '', 'g')) AS num
) p
WHERE c.id = ANY($3::int[]) AND c.deleted_at IS NULL
  AND EXISTS (
      SELECT 1
      FROM (
          SELECT n.name AS name FROM related_subject_names n
          WHERE n.subject_id = ANY($1::bigint[])
          UNION ALL
          SELECT r.display_name FROM related_subjects r
          WHERE r.id = ANY($1::bigint[]) AND r.display_name IS NOT NULL
      ) nm
      WHERE upper(regexp_replace(btrim(nm.name), '\s+', ' ', 'g'))
          = upper(regexp_replace(btrim(c.full_name), '\s+', ' ', 'g'))
  )
"""

SUGGESTION_BASES = (
    "passport_equal",
    "passport_one_char_apart",
    "nik_encodes_client_birth_date",
    "birth_date_equal",
)

# One document per sha256: the opened subject's own link when it has one ($2), else the earliest.
_DOCUMENTS_SQL = """
SELECT x.subject_id, x.sha256, x.doc_type, d.mime_type, d.byte_size, d.first_seen_on
FROM (
    SELECT sd.subject_id, sd.sha256, sd.doc_type,
           row_number() OVER (
               PARTITION BY sd.sha256
               ORDER BY (sd.subject_id <> $2), sd.created_at ASC, sd.subject_id ASC
           ) AS rn
    FROM related_subject_documents sd
    WHERE sd.subject_id = ANY($1::bigint[])
) x
JOIN related_documents d ON d.sha256 = x.sha256
WHERE x.rn = 1
ORDER BY x.doc_type ASC NULLS LAST, x.sha256 ASC
LIMIT $3
"""

# Detachments touching the member set ($1): the split-off history of migration 370. Counts only;
# the owner key digest, the moved document hashes, evidence and created_by are never selected.
_DETACHMENTS_SQL = """
SELECT id, from_id, to_id, id_type, basis, created_at,
       cardinality(moved_documents) AS documents_moved
FROM related_subject_detachments
WHERE from_id = ANY($1::bigint[]) OR to_id = ANY($1::bigint[])
ORDER BY created_at, id
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
    """One page of unmerged subjects matching ``q``, ``kind`` and ``linked``, with the total.

    A survivor is found through any record merged into it and shows the union of their identifiers.
    """
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
    identifiers: dict[int, list[dict[str, Any]]] = defaultdict(list)
    counts: dict[int, int] = {}
    if ids:
        for r in await conn.fetch(_IDENTIFIERS_SQL, ids):
            owner = r["owner_id"]
            identifiers[owner].append(
                {
                    "type": r["id_type"],
                    "value": r["id_raw"],
                    "from_subject_id": None if r["subject_id"] == owner else r["subject_id"],
                }
            )
        for r in await conn.fetch(_DOCUMENT_COUNTS_SQL, ids):
            counts[r["owner_id"]] = r["n"]
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
    """The subject with the union of its own and its merged-in records' rows, or None when absent."""
    row = await conn.fetchrow(_SUBJECT_SQL, subject_id)
    if row is None:
        return None
    member_rows = await conn.fetch(_MERGES_SQL, subject_id) if row["merged_count"] > 0 else []
    ids = [subject_id] + [m["id"] for m in member_rows]

    def origin(owner_of_row: int) -> int | None:
        return None if owner_of_row == subject_id else owner_of_row

    identifiers = [
        {"type": r["id_type"], "value": r["id_raw"], "from_subject_id": origin(r["subject_id"])}
        for r in await conn.fetch(_IDENTIFIERS_SQL, [subject_id])
    ]
    doc_rows = await conn.fetch(_DOCUMENTS_SQL, ids, subject_id, MAX_DETAIL_DOCUMENTS)
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
            "from_subject_id": origin(d["subject_id"]),
        }
        for d in doc_rows
    ]
    registration = []
    if row["registration_name"] is not None:
        registration.append(
            {
                "name": row["registration_name"],
                "source": "registration",
                "created_at": row["created_at"],
                "from_subject_id": None,
            }
        )
    for m in member_rows:
        if m["display_name"] is not None:
            registration.append(
                {
                    "name": m["display_name"],
                    "source": "registration",
                    "created_at": m["created_at"],
                    "from_subject_id": m["id"],
                }
            )
    shown = registration[:MAX_DETAIL_NAMES]
    # the recorded-names statement is never given a smaller limit: with no slot left it must still
    # return a row, or its window total would read zero
    name_rows = await conn.fetch(_NAMES_SQL, ids, MAX_DETAIL_NAMES)
    names = shown + [
        {
            "name": r["name"],
            "source": r["source"],
            "created_at": r["created_at"],
            "from_subject_id": origin(r["subject_id"]),
        }
        for r in name_rows[: MAX_DETAIL_NAMES - len(shown)]
    ]
    names_total = len(registration) + (name_rows[0]["n"] if name_rows else 0)
    link_rows = await conn.fetch(_LINKS_SQL, ids, MAX_DETAIL_LINKS)
    links = [
        {
            "client_id": r["client_id"],
            "client_name": r["client_name"],
            "client_live": r["client_live"],
            "basis": r["basis"],
            "relation": r["relation"],
            "created_at": r["created_at"],
            "files": r["files"],
            "from_subject_id": origin(r["subject_id"]),
        }
        for r in link_rows
    ]
    client_ids = sorted({r["client_id"] for r in link_rows})
    reviews: dict[int, dict[str, Any]] = {}
    suggestions: dict[int, list[str]] = {}
    if client_ids:
        for r in await conn.fetch(_REVIEWS_SQL, ids, client_ids):
            reviews[r["client_id"]] = {
                "decision": r["decision"],
                "relation": r["relation"],
                "decided_by": r["decided_by"],
                "decided_at": r["created_at"],
            }
        for r in await conn.fetch(_SUGGESTIONS_SQL, ids, subject_id, client_ids):
            suggestions[r["client_id"]] = [b for b in SUGGESTION_BASES if r[b]]
    for link in links:
        basis_words = suggestions.get(link["client_id"], [])
        link["review"] = reviews.get(link["client_id"])
        link["suggested_relation"] = "self" if basis_words else None
        link["suggestion_basis"] = basis_words
    member_set = set(ids)
    detached_into: list[dict[str, Any]] = []
    detached_from: list[dict[str, Any]] = []
    for d in await conn.fetch(_DETACHMENTS_SQL, ids, MAX_DETAIL_LINKS):
        common = {
            "id_type": d["id_type"],
            "basis": d["basis"],
            "documents_moved": d["documents_moved"],
            "detached_at": d["created_at"],
        }
        if d["from_id"] in member_set:
            detached_into.append(
                {"subject_id": d["to_id"], **common, "from_subject_id": origin(d["from_id"])}
            )
        if d["to_id"] in member_set:
            detached_from.append(
                {"subject_id": d["from_id"], **common, "from_subject_id": origin(d["to_id"])}
            )
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
        "merged_from": [
            {"subject_id": m["id"], "basis": m["basis"], "merged_at": m["merged_at"]}
            for m in member_rows
        ],
        "merged_into": row["merged_into"],
        "detached_into": detached_into,
        "detached_from": detached_from,
    }
