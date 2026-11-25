"""The visa OCR client sync carries the family-member guard in its own statement.

Behaviour on real tables is in
`tests/app/routers/test_visa_ocr_family_document_keeps_client_visa_pg.py`; this
unit test pins the statement shape where no database is available: the
`UPDATE clients` binds the document id and skips a family member's document.
Synthetic values only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_OCR = {
    "visa_type": "ITK",
    "visa_number": "SYN-0001",
    "expiry_date": "2027-12-31",
    "issue_date": "2026-11-01",
    "sponsor": "PT Synthetic Sponsor",
    "confidence": 0.9,
}


async def _client_sync_call(
    mock_db_pool: MagicMock, mock_db_conn: AsyncMock, doc_id: int | None
) -> Any:
    from backend.app.routers.crm_enhanced import _auto_ocr_visa

    mock_db_conn.execute = AsyncMock(return_value="UPDATE 1")
    with (
        patch(
            "backend.app.routers.crm_enhanced._download_drive_file",
            new=AsyncMock(return_value=(b"x", "image/jpeg")),
        ),
        patch("backend.app.routers.crm_enhanced._gemini_ocr", new=AsyncMock(return_value="{}")),
        patch(
            "backend.app.routers.crm_enhanced.extract_json_from_llm_response",
            return_value=dict(_OCR),
        ),
        patch("backend.app.routers.crm_enhanced.to_jsonb", return_value="{}"),
    ):
        result = await _auto_ocr_visa(mock_db_pool, 42, "synthetic-file", doc_id)
    assert result["success"] is True
    syncs = [c for c in mock_db_conn.execute.await_args_list if "UPDATE clients" in c.args[0]]
    assert len(syncs) == 1, "the client sync runs exactly one statement"
    return syncs[0]


@pytest.mark.asyncio
async def test_client_sync_skips_a_family_member_document(
    mock_db_pool: MagicMock, mock_db_conn: AsyncMock
) -> None:
    call = await _client_sync_call(mock_db_pool, mock_db_conn, doc_id=5)
    sql = " ".join(call.args[0].split())

    assert "NOT EXISTS (SELECT 1 FROM documents WHERE id = $" in sql
    assert "AND family_member_id IS NOT NULL" in sql
    # The document id is bound last, after the client id.
    assert call.args[-2:] == (42, 5)


@pytest.mark.asyncio
async def test_client_sync_still_writes_the_client_visa_fields(
    mock_db_pool: MagicMock, mock_db_conn: AsyncMock
) -> None:
    call = await _client_sync_call(mock_db_pool, mock_db_conn, doc_id=None)
    sql = call.args[0]

    for column in ("visa_expiry_date", "current_visa_type", "current_visa_sponsor"):
        assert f"{column} = $" in sql
    assert "ITK" in call.args and "PT Synthetic Sponsor" in call.args and 42 in call.args
