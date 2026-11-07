"""P3 - the CRM base64 upload takes an explicit optional ``client_visible``.

``documents.client_visible`` defaults to true in the database and no upload path set
it. The intake delivery of a ``recovery-v1`` row must create the Fly row STAFF-ONLY, so
the shared request model gains an optional field written in the SAME INSERT. Absent, the
statement is the legacy one (pinned below, whitespace aside): every existing caller keeps
the old database default.
"""

import pytest

from backend.app.routers.crm_enhanced_documents import DocumentUploadBase64

# Reuses the harness of the dedup tests (pool/drive doubles, the upload call).
from backend.tests.unit.routers.test_crm_upload_dedup import _drive, _pool, _upload

# The statement upload_document_base64 issued before this change.
LEGACY_INSERT = (
    "INSERT INTO documents ( client_id, document_type, document_category, file_name, file_id, "
    "file_url, google_drive_file_url, status, storage_type, notes, subfolder, expiry_date, "
    "content_hash, family_member_id, practice_id, uploaded_by, uploaded_source ) VALUES "
    "($1, $2, $3, $4, $5, $6, $7, 'active', 'google_drive', $8, $9, $10, $11, $12, $13, $14, $15) "
    "RETURNING id"
)


async def _insert(**model_fields) -> tuple[str, tuple]:
    pool, conn = _pool(dedup_hit=None)
    result = await _upload(pool, _drive(), **model_fields)
    assert result["document_id"] == 701
    inserts = [
        c.args for c in conn.fetchval.await_args_list if "INSERT INTO documents" in c.args[0]
    ]
    assert len(inserts) == 1, "exactly one documents INSERT: visibility is never a second write"
    return " ".join(inserts[0][0].split()), inserts[0][1:]


def test_the_field_is_optional_and_defaults_to_none():
    base = {"file": "ZmlsZQ==", "file_name": "scan.pdf", "document_type": "visa"}
    assert DocumentUploadBase64(**base).client_visible is None
    assert DocumentUploadBase64(**base, client_visible=False).client_visible is False


@pytest.mark.asyncio
async def test_absent_field_keeps_the_legacy_insert_and_its_fifteen_arguments():
    sql, args = await _insert()

    assert sql == LEGACY_INSERT
    assert len(args) == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_explicit_field_is_written_in_the_same_insert(value):
    _, legacy_args = await _insert()
    sql, args = await _insert(client_visible=value)

    assert sql == LEGACY_INSERT.replace(
        "uploaded_source )", "uploaded_source, client_visible )"
    ).replace("$15)", "$15, $16)")
    assert args[:15] == legacy_args  # the fifteen legacy arguments, unchanged and in order
    assert len(args) == 16 and args[15] is value  # a falsy False is written, not dropped
