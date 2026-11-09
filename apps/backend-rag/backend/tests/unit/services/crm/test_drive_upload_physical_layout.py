"""Physical destination regressions shared by CRM and both portal Drive writers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.core.config import settings
from backend.services.crm.drive_taxonomy import physical_root_layout
from backend.services.portal._mixins.documents import PortalDocumentsMixin


def root(parent="individuals", **changes):
    return {
        "id": "root",
        "mimeType": "application/vnd.google-apps.folder",
        "trashed": False,
        "parents": [parent],
        **changes,
    }


@pytest.fixture(autouse=True)
def configured_roots(monkeypatch):
    monkeypatch.setattr(settings, "gdrive_individuals_folder_id", "individuals")
    monkeypatch.setattr(settings, "gdrive_companies_folder_id", "companies")


@pytest.mark.parametrize(
    "changes,kind",
    [
        ({"id": "different"}, "company"),
        ({"trashed": True}, "company"),
        ({"mimeType": "application/vnd.google-apps.shortcut"}, "company"),
        ({"parents": ["unknown"]}, "company"),
        ({"parents": ["companies"]}, "individual"),
        ({"parents": ["individuals", "companies"]}, "company"),
        ({}, "unknown"),
    ],
)
def test_root_layout_rejects_invalid_metadata(changes, kind):
    with pytest.raises(ValueError):
        physical_root_layout(
            root(**changes),
            "root",
            kind,
            individuals_parent="individuals",
            companies_parent="companies",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["crm", "portal-sa", "portal-oauth"])
@pytest.mark.parametrize(
    "parent,expected",
    [("individuals", "01_Profile"), ("companies", "99_Archive"), ("unknown", None)],
)
async def test_company_upload_uses_existing_physical_layout(writer, parent, expected):
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": 11,
        "full_name": "Synthetic Customer",
        "google_drive_folder_id": "root",
        "client_type": "company",
    }
    conn.fetchval.return_value = None
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    drive = MagicMock()
    drive.get_file_metadata = AsyncMock(return_value=root(parent))
    drive.get_file = AsyncMock(return_value=root(parent))
    drive.resolve_canonical_folder = AsyncMock(return_value="destination")
    drive.upload_file_to_folder = AsyncMock(return_value={"id": "uploaded"})
    portal = PortalDocumentsMixin()
    portal.pool = pool
    if writer == "portal-sa":
        if expected:
            assert (
                await portal._resolve_client_drive_folder(
                    conn, drive, 11, "Synthetic", "99_Archive", "personal"
                )
                == "destination"
            )
        else:
            with pytest.raises(ValueError):
                await portal._resolve_client_drive_folder(
                    conn, drive, 11, "Synthetic", "99_Archive", "personal"
                )
        resolver = drive.resolve_canonical_folder
        name_position = 0
    elif writer == "portal-oauth":
        drive.is_configured.return_value = True
        drive.get_valid_token = AsyncMock(return_value="synthetic")
        portal._resolve_oauth_folder = AsyncMock(return_value="destination")
        with patch(
            "backend.services.integrations.google_drive_service.GoogleDriveService",
            return_value=drive,
        ):
            result = await portal._upload_to_drive(
                conn, 11, "Synthetic", "passport", b"PDF", "scan.pdf"
            )
        assert result["success"] is bool(expected)
        resolver = portal._resolve_oauth_folder
        name_position = 1
    else:
        from backend.app.routers import crm_enhanced_documents as crm

        # Stop at the resolver boundary; other tests cover upload/DB lifecycle.
        drive.resolve_canonical_folder.side_effect = RuntimeError("synthetic resolver boundary")
        with (
            patch.object(crm, "ServiceAccountDriveService", return_value=drive),
            patch.object(crm, "_resolve_upload_practice", AsyncMock(return_value=None)),
            patch.object(crm, "_active_duplicate_document_id", AsyncMock(return_value=None)),
        ):
            with pytest.raises(HTTPException) as caught:
                await crm.upload_document_base64(
                    11,
                    crm.DocumentUploadBase64(
                        file="UERG",
                        file_name="scan.pdf",
                        document_type="passport",
                        document_category="personal",
                    ),
                    pool,
                    {"role": "admin"},
                    access_already_verified=True,
                )
        assert caught.value.status_code == 409
        resolver = drive.resolve_canonical_folder
        name_position = 0
    if expected:
        assert resolver.call_args.args[name_position] == expected
    else:
        resolver.assert_not_awaited()
        drive.upload_file_to_folder.assert_not_awaited()
    drive.create_folder.assert_not_called()
    assert conn.fetchrow.return_value["client_type"] == "company"
