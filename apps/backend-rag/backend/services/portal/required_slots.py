"""Mark a practice's Required Documents slot as uploaded by the client.

One writer for both upload paths:
- CRM ``upload-client-document`` (client picks the slot explicitly)
- portal Vault upload (slot inferred from practice + document type)
"""

from typing import Any

from backend.app.utils.logging_utils import get_logger
from backend.services.crm.admin_document_visibility import admin_document_not_deleted_clause

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

# The file a slot was filled with is gone when its document row no longer exists (the foreign
# key then leaves NULL) or when the client or the team deleted it. A slot such a file left
# 'uploaded' is open again: no document backs it.
#
# The verdict is taken with the document row locked until the transaction ends. A restore of
# that document updates the same row, so it waits for the slot's fate and cannot commit between
# the verdict and the write. The document is locked BEFORE the slot: a hard delete reaches the
# slot from the document, through the foreign key, and the opposite order would deadlock with it.
_FILE_STILL_THERE_SQL = f"""
    SELECT ({admin_document_not_deleted_clause("d")}) AS there
    FROM documents d
    WHERE d.id = $1
    FOR SHARE
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


async def _file_is_gone(conn: Any, file_id: int) -> bool:
    """True when the document is deleted or no longer a row. Locks the row it finds."""
    row = await conn.fetchrow(_FILE_STILL_THERE_SQL, file_id)
    return row is None or not row["there"]


async def fill_matching_pending_slot(
    conn: Any,
    *,
    practice_id: int,
    document_type: str | None,
    document_id: int,
) -> int | None:
    """Fill the one slot of ``practice_id`` whose type matches, if it is still open.

    Open means 'pending', or 'uploaded' by a file that is gone: a document the client
    removed and uploads again must find its slot. Fail-closed: no slot, more than one
    slot of that type, a slot the team has judged ('verified', 'rejected'), a slot whose
    file is still there, or an untyped upload leaves every slot untouched. The caller
    must already have checked that the practice belongs to the uploader.

    Runs in a transaction of its own (a savepoint inside the caller's): the slot is
    written with its row locked, and with the row of the file it replaces locked too.
    """
    doc_type = (document_type or "").strip().lower()
    if doc_type in _UNTYPED:
        return None
    async with conn.transaction():
        seen = await conn.fetch(
            """
            SELECT id, status, uploaded_file_id FROM practice_required_documents
            WHERE practice_id = $1 AND LOWER(document_type) = $2
            """,
            practice_id,
            doc_type,
        )
        if len(seen) != 1:
            return None
        slot_id = seen[0]["id"]
        # Read without a lock first, only to learn which document to lock.
        seen_file = seen[0]["uploaded_file_id"] if seen[0]["status"] == "uploaded" else None
        seen_file_gone = seen_file is not None and await _file_is_gone(conn, seen_file)

        locked = await conn.fetch(
            """
            SELECT status, uploaded_file_id FROM practice_required_documents
            WHERE id = $1
            FOR UPDATE
            """,
            slot_id,
        )
        if not locked:
            return None
        slot = locked[0]
        if slot["status"] == "pending":
            filled = await mark_required_slot_uploaded(
                conn, slot_id=slot_id, document_id=document_id, only_if_pending=True
            )
        elif slot["status"] == "uploaded" and (
            slot["uploaded_file_id"] is None
            or (seen_file_gone and slot["uploaded_file_id"] == seen_file)
        ):
            result = await conn.execute(
                _MARK_UPLOADED_SQL + " AND status = 'uploaded'", document_id, None, slot_id
            )
            filled = str(result).endswith(" 1")
        else:
            filled = False
    return slot_id if filled else None
