"""Mark a practice's Required Documents slot as uploaded by the client.

One writer for both upload paths:
- CRM ``upload-client-document`` (client picks the slot explicitly)
- portal Vault upload (slot inferred from practice + document type)
"""

from typing import Any

from backend.app.utils.logging_utils import get_logger

logger = get_logger(__name__)

_MARK_UPLOADED_SQL = """
    UPDATE practice_required_documents
    SET uploaded_by_client = TRUE,
        uploaded_file_id = $1,
        uploaded_at = NOW(),
        client_notes = COALESCE($2, client_notes),
        status = 'uploaded',
        updated_at = NOW()
    WHERE id = $3
"""

# The Vault default type says nothing about which slot a file is for.
_UNTYPED = frozenset({"", "other"})


async def mark_required_slot_uploaded(
    conn: Any,
    *,
    slot_id: int,
    document_id: int,
    notes: str | None = None,
    only_if_pending: bool = False,
) -> bool:
    """Set the slot to 'uploaded' and link the document. Returns True if a row changed."""
    sql = _MARK_UPLOADED_SQL + (" AND status = 'pending'" if only_if_pending else "")
    result = await conn.execute(sql, document_id, notes, slot_id)
    return str(result).endswith(" 1")


async def fill_matching_pending_slot(
    conn: Any,
    *,
    practice_id: int,
    document_type: str | None,
    document_id: int,
) -> int | None:
    """Fill the one slot of ``practice_id`` whose type matches, if it is still pending.

    Fail-closed: no slot, more than one slot of that type, a slot that is not
    'pending', or an untyped upload leaves every slot untouched. The caller
    must already have checked that the practice belongs to the uploader.
    """
    doc_type = (document_type or "").strip().lower()
    if doc_type in _UNTYPED:
        return None
    slots = await conn.fetch(
        """
        SELECT id, status FROM practice_required_documents
        WHERE practice_id = $1 AND LOWER(document_type) = $2
        """,
        practice_id,
        doc_type,
    )
    if len(slots) != 1 or slots[0]["status"] != "pending":
        return None
    slot_id = slots[0]["id"]
    if await mark_required_slot_uploaded(
        conn, slot_id=slot_id, document_id=document_id, only_if_pending=True
    ):
        return slot_id
    return None
