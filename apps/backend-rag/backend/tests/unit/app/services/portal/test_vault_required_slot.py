"""Vault upload fills the matching pending Required Documents slot.

Regression for practice 1324 / client_id 12666: a passport uploaded through
the portal Vault for a practice stayed out of that practice's Required
Documents card until staff flipped the slot by hand.
"""

import inspect
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.portal.portal_service import PortalService

CLIENT_ID = 900001
PRACTICE_ID = 700001
DOC_ID = 880001
USER = {"client_id": CLIENT_ID, "email": "client-fiktif@example.test"}


class _AsyncCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _physical_drive_roots(monkeypatch):
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "gdrive_individuals_folder_id", "individuals")
    monkeypatch.setattr(settings, "gdrive_companies_folder_id", "companies")


def _close_spawn(awaitable: object) -> None:
    if inspect.iscoroutine(awaitable):
        awaitable.close()


def _service(
    *, practice_owned: bool, slots: list[dict[str, Any]]
) -> tuple[PortalService, AsyncMock]:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncCtx())
    conn.fetchrow.side_effect = [
        None,  # duplicate check
        {"id": PRACTICE_ID} if practice_owned else None,  # practice ownership
        {"email": USER["email"], "full_name": "Client Fiktif", "assigned_to": None},
        {
            "id": DOC_ID,
            "document_type": "passport",
            "file_name": "passport.pdf",
            "status": "received",
            "created_at": datetime(2026, 10, 21, tzinfo=timezone.utc),
            "expiry_date": None,
        },
    ]
    conn.fetch.return_value = slots
    conn.execute.return_value = "UPDATE 1"
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    service = PortalService(pool)
    service._upload_to_drive = AsyncMock(
        return_value={"success": True, "file_id": "drive_file_fiktif", "file_url": None}
    )
    return service, conn


async def _upload(service: PortalService, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "client_id": CLIENT_ID,
        "file_content": b"%PDF-1.4 placeholder",
        "file_name": "passport.pdf",
        "document_type": "passport",
        "mime_type": "application/pdf",
        "practice_id": PRACTICE_ID,
        "current_user": USER,
    }
    kwargs.update(overrides)
    with (
        patch(
            "backend.services.portal._mixins.documents.DocumentOCR.extract_text",
            new=AsyncMock(return_value={"success": False, "text": "", "pages": 0}),
        ),
        patch(
            "backend.services.portal._mixins.documents.ExpiryDetector.detect_expiry",
            return_value={"expiry_date": None, "confidence": 0.0},
        ),
        patch("backend.services.portal._mixins.documents.spawn", side_effect=_close_spawn),
    ):
        return await service.upload_document(**kwargs)


def _slot_updates(conn: AsyncMock) -> list[tuple]:
    return [
        c.args
        for c in conn.execute.await_args_list
        if "UPDATE practice_required_documents" in c.args[0]
    ]


# --- GUILT -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_guilt_vault_passport_fills_the_one_pending_passport_slot() -> None:
    service, conn = _service(practice_owned=True, slots=[{"id": 11, "status": "pending"}])

    result = await _upload(service)

    assert result["id"] == DOC_ID
    updates = _slot_updates(conn)
    assert len(updates) == 1
    sql, document_id, notes, slot_id = updates[0]
    assert "status = 'uploaded'" in sql
    assert "uploaded_by_client = TRUE" in sql
    assert "uploaded_at = NOW()" in sql
    assert "status = 'pending'" in sql  # never overwrites a slot that moved on
    assert (document_id, notes, slot_id) == (DOC_ID, None, 11)


# --- INNOCENCE -------------------------------------------------------------


@pytest.mark.asyncio
async def test_innocence_two_matching_slots_leave_every_slot_untouched() -> None:
    service, conn = _service(
        practice_owned=True,
        slots=[{"id": 11, "status": "pending"}, {"id": 12, "status": "pending"}],
    )

    result = await _upload(service)

    assert result["id"] == DOC_ID
    assert _slot_updates(conn) == []


@pytest.mark.asyncio
async def test_innocence_verified_slot_is_not_touched() -> None:
    service, conn = _service(practice_owned=True, slots=[{"id": 11, "status": "verified"}])

    result = await _upload(service)

    assert result["id"] == DOC_ID
    assert _slot_updates(conn) == []


@pytest.mark.asyncio
async def test_innocence_other_clients_practice_is_rejected_and_nothing_written() -> None:
    service, conn = _service(practice_owned=False, slots=[{"id": 11, "status": "pending"}])

    with pytest.raises(ValueError, match="Practice not found or not accessible"):
        await _upload(service)

    assert _slot_updates(conn) == []
    conn.fetch.assert_not_awaited()
    service._upload_to_drive.assert_not_awaited()
    assert not any("INSERT INTO documents" in c.args[0] for c in conn.fetchrow.await_args_list)


@pytest.mark.asyncio
async def test_innocence_no_practice_id_keeps_old_behaviour() -> None:
    service, conn = _service(practice_owned=True, slots=[{"id": 11, "status": "pending"}])
    # Without practice_id there is no ownership lookup: drop that fetchrow.
    side = list(conn.fetchrow.side_effect)
    conn.fetchrow.side_effect = [side[0], side[2], side[3]]

    result = await _upload(service, practice_id=None)

    assert result["id"] == DOC_ID
    assert _slot_updates(conn) == []
    conn.fetch.assert_not_awaited()
    insert = conn.fetchrow.await_args_list[2]
    assert "INSERT INTO documents" in insert.args[0]
    assert insert.args[2] is None  # practice_id stays NULL


@pytest.mark.asyncio
async def test_innocence_untyped_vault_upload_does_not_guess_a_slot() -> None:
    service, conn = _service(practice_owned=True, slots=[{"id": 11, "status": "pending"}])

    result = await _upload(service, document_type="other")

    assert result["id"] == DOC_ID
    assert _slot_updates(conn) == []
    conn.fetch.assert_not_awaited()
