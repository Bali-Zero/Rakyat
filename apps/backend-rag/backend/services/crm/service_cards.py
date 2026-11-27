"""Seed a practice's checklist from the service card of its service (spec BLOCK-1-VISA).

The card path is armed per service by the setting ``service_cards_seeding_codes`` (empty = off,
comma-separated practice type codes, ``*`` = every active card). It never overwrites or deletes
a slot: ``ON CONFLICT (practice_id, document_type) DO NOTHING``. Only the card's always-asked
documents become slots; the staff-only text (``condition_text``, ``source``) is never written
to a column the portal reads.
"""

from __future__ import annotations

import asyncpg

from backend.app.core.config import settings


def _listed(code: str | None, raw: str) -> bool:
    codes = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in codes or (code is not None and code in codes)


async def seed_required_documents_from_card(
    conn: asyncpg.Connection,
    practice_id: int,
    practice_type_id: int | None,
    created_by: str,
) -> int | None:
    """Seed ``practice_required_documents`` from the active card of the service.

    Returns None when the card path does not apply (setting empty, code not listed, no card,
    card inactive): the caller then uses the legacy ``required_documents`` array. Returns the
    number of slots it asked to insert (possibly 0) when an active, listed card exists; the
    legacy array is then not read. With the setting empty the connection is not touched.
    """
    raw = (getattr(settings, "service_cards_seeding_codes", "") or "").strip()
    if not raw or practice_type_id is None:
        return None
    card = await conn.fetchrow(
        """
        SELECT pt.code, sc.active
        FROM service_cards sc
        JOIN practice_types pt ON pt.id = sc.practice_type_id
        WHERE sc.practice_type_id = $1
        """,
        practice_type_id,
    )
    if not card or not card["active"] or not _listed(card["code"], raw):
        return None
    documents = await conn.fetch(
        """
        SELECT document_type, label, client_text, condition_text, active
        FROM service_card_documents
        WHERE practice_type_id = $1
        ORDER BY sort_order
        """,
        practice_type_id,
    )
    asked = [d for d in documents if d["active"] and d["condition_text"] is None]
    if not asked:
        return 0
    await conn.execute(
        """
        INSERT INTO practice_required_documents (
            practice_id, document_type, document_label, description, is_required, created_by
        )
        SELECT $1, t.doc_type, t.doc_label, t.doc_text, true, $5
        FROM UNNEST($2::varchar[], $3::varchar[], $4::text[]) WITH ORDINALITY
            AS t(doc_type, doc_label, doc_text, position)
        ORDER BY t.position
        ON CONFLICT (practice_id, document_type) DO NOTHING
        """,
        practice_id,
        [d["document_type"] for d in asked],
        [d["label"] for d in asked],
        [d["client_text"] for d in asked],
        created_by or "system",
    )
    return len(asked)
