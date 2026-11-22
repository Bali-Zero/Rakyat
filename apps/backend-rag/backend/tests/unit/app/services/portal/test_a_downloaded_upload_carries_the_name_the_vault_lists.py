"""A document the client uploaded is downloaded under the name the vault lists.

Both upload paths store the file in Drive as ``<YYYYMMDD_HHMMSS>_<name>``, while the row,
and so the vault, keeps ``<name>``. The download took the Drive name first, so a client who
uploaded ``passport.pdf`` and saw ``passport.pdf`` in the vault got back
``20261121_101500_passport.pdf``.

The download now gives the listed name when the Drive name is exactly that stamp followed by
the listed name. Any other Drive name is kept: a file the team renamed in Drive is still
downloaded under the team's name, as an existing test requires.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.portal.portal_service import PortalService

DRIVE = "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService"
STAMP = "20261121_101500_"
LISTED = "passport.pdf"


def _service(row: dict[str, Any] | None = None) -> PortalService:
    conn = AsyncMock()
    conn.fetchrow.return_value = row
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return PortalService(pool)


async def _downloaded_name(listed: str | None, in_drive: str | None) -> str:
    """The name the real download gives for a row listed as ``listed`` whose Drive object is
    named ``in_drive``."""
    row = {
        "id": 10,
        "file_name": listed,
        "file_id": "drive_file_10",
        "file_url": None,
        "mime_type": "application/pdf",
        "status": "received",
    }
    drive = MagicMock()
    drive.get_file_metadata = AsyncMock(
        return_value={"name": in_drive, "mimeType": "application/pdf"}
    )
    drive.download_file_content = AsyncMock(return_value=b"PDF_BYTES")
    with patch(DRIVE, return_value=drive):
        result = await _service(row).download_document(
            client_id=1,
            document_id=10,
            current_user={"client_id": 1, "email": "client@example.com"},
        )
    assert result is not None
    assert result["content"] == b"PDF_BYTES"
    assert result["mime_type"] == "application/pdf"
    return result["file_name"]


async def _name_the_upload_gives_drive(file_name: str) -> str:
    """Run the real service-account upload and return the name it sends to Drive."""
    conn = AsyncMock()
    conn.fetchval.return_value = "individual"
    drive = MagicMock()
    drive.get_file_metadata = AsyncMock(return_value={"name": "Personal"})
    drive.upload_file_to_folder = AsyncMock(return_value={"id": "drive_file_10"})
    with (
        patch(DRIVE, return_value=drive),
        patch.object(
            PortalService, "_resolve_client_drive_folder", AsyncMock(return_value="folder_1")
        ),
    ):
        result = await _service()._upload_with_service_account(
            conn=conn,
            client_id=1,
            client_name="Placeholder Client",
            document_type="passport",
            file_content=b"PDF_BYTES",
            file_name=file_name,
            mime_type="application/pdf",
            result={},
        )
    assert result.get("success") is True, result
    return drive.upload_file_to_folder.await_args.kwargs["file_name"]


# ---------------------------------------------------------------- the stamp is ours

NAMES_A_CLIENT_UPLOADS = {
    "a plain name": "passport.pdf",
    "spaces and brackets": "Akta Pendirian (final) v2.pdf",
    "no extension": "scan",
    "letters that are not ASCII": "paspor_ibu_É.pdf",
    "a name that already starts with a stamp": "20250101_000000_passport.pdf",
    "underscores only": "___",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listed", NAMES_A_CLIENT_UPLOADS.values(), ids=NAMES_A_CLIENT_UPLOADS.keys()
)
async def test_what_the_upload_stores_in_drive_is_downloaded_under_the_listed_name(
    listed: str,
) -> None:
    """Tied to the writer: the Drive name comes from the real upload, not from this file."""
    in_drive = await _name_the_upload_gives_drive(listed)
    assert in_drive != listed and in_drive.endswith("_" + listed)

    assert await _downloaded_name(listed, in_drive) == listed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listed", NAMES_A_CLIENT_UPLOADS.values(), ids=NAMES_A_CLIENT_UPLOADS.keys()
)
async def test_a_stamp_before_the_listed_name_is_not_part_of_the_download_name(listed: str) -> None:
    assert await _downloaded_name(listed, STAMP + listed) == listed


# ---------------------------------------------------------------- every other name is kept

DRIVE_NAMES_THAT_ARE_KEPT = {
    "the team renamed the file": "passport-renamed.pdf",
    "the same name, no stamp": LISTED,
    "a stamp before another name": STAMP + "passport (1).pdf",
    "a stamp before a longer name that ends alike": STAMP + "old_" + LISTED,
    "a stamp and nothing else": STAMP,
    "a stamp with seven digits in the date": "2026112_101500_" + LISTED,
    "a stamp with a dash in the middle": "20261121-101500_" + LISTED,
    "a stamp with no underscore after it": "20261121_101500" + LISTED,
    "something before the stamp": "x" + STAMP + LISTED,
    "a stamp in digits that are not ASCII": "٢٠٢٦١١٢١_١٠١٥٠٠_" + LISTED,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "in_drive", DRIVE_NAMES_THAT_ARE_KEPT.values(), ids=DRIVE_NAMES_THAT_ARE_KEPT.keys()
)
async def test_any_other_drive_name_is_kept_as_it_is(in_drive: str) -> None:
    assert await _downloaded_name(LISTED, in_drive) == in_drive


@pytest.mark.asyncio
async def test_a_listed_name_that_is_only_the_tail_of_the_drive_name_changes_nothing() -> None:
    """Exactness, not "ends with": ``port.pdf`` is not what follows the stamp."""
    assert await _downloaded_name("port.pdf", STAMP + LISTED) == STAMP + LISTED


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [None, ""], ids=["no listed name", "an empty listed name"])
async def test_a_row_with_no_name_gets_the_drive_name(listed: str | None) -> None:
    assert await _downloaded_name(listed, STAMP + LISTED) == STAMP + LISTED


@pytest.mark.asyncio
@pytest.mark.parametrize("in_drive", [None, ""], ids=["no Drive name", "an empty Drive name"])
async def test_a_drive_object_with_no_name_gets_the_listed_name_as_before(
    in_drive: str | None,
) -> None:
    assert await _downloaded_name(LISTED, in_drive) == LISTED
