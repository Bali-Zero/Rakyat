"""Unit tests for client Tax Documents (Google Drive) CRUD and authorization.

Synthetic opaque fixtures only (no real PII or email literals).
Covers:
  - can_manage_client_tax_documents (admin, assigned_to, tax_consultant,
    created_by, validated pool peers, unrelated staff, machine/portal roles)
  - get_client_profile returning viewer_can_manage_tax_documents
  - ServiceAccountDriveService.rename_file & update_file_content (supportsAllDrives=True)
  - POST /api/crm/clients/{id}/tax-documents/upload
  - PATCH /api/crm/clients/{id}/tax-documents/{file_id}
  - PUT /api/crm/clients/{id}/tax-documents/{file_id}/content
  - DELETE /api/crm/clients/{id}/tax-documents/{file_id}
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.modules.crm import tax_drive_documents
from backend.app.modules.crm.tax_drive_documents import (
    TAX_DOC_MAX_BYTES,
    get_tax_drive_service_factory,
)
from backend.app.modules.crm.tax_drive_documents import (
    router as tax_drive_router,
)
from backend.app.utils import client_pools
from backend.app.utils.client_pools import can_manage_client_tax_documents
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService

A = "staff-setup-01"
B = "staff-peer-02"
TAX = "staff-tax-03"
CREATOR = "staff-creator-04"
OUTSIDER = "staff-outsider-05"
ADMIN = {"email": "staff-director-00", "role": "admin"}

CLIENT_ROOT = "CLIENT_ROOT_FOLDER_01"
PERSONAL_TAX_FOLDER = "PERSONAL_TAX_FOLDER_02"
SUBFOLDER_2026 = "SUBFOLDER_2026_03"
COMPANY_ROOT = "COMPANY_ROOT_FOLDER_04"
COMPANY_TAX_DEPT = "COMPANY_TAX_DEPT_05"
FOREIGN_FOLDER = "FOREIGN_CLIENT_FOLDER_99"

FILE_PERSONAL = "FILE_PERSONAL_SPT_01"
FILE_COMPANY = "FILE_COMPANY_SPT_02"
FILE_FOREIGN = "FILE_FOREIGN_DOC_99"
FILE_TRASHED = "FILE_TRASHED_DOC_88"
FILE_SHORTCUT = "FILE_SHORTCUT_DOC_77"

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


def _user(email: str = TAX, role: str = "Tax Consultant") -> dict[str, str]:
    return {"email": email, "role": role}


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "Not Found" if status == 404 else "Forbidden"
    return HttpError(resp, b"error")


@pytest.fixture(autouse=True)
def _reset_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_pools, "_VALIDATED", ())
    monkeypatch.setattr(client_pools, "_LAST_ATTEMPT", None)
    monkeypatch.setattr(tax_drive_documents, "invalidate_cache", AsyncMock())


@pytest.fixture
def pool_ab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_pools, "_VALIDATED", (frozenset({A, B}),))


# ============================================================================
# 1. Pure authorization predicate
# ============================================================================


def test_can_manage_tax_documents_allows_admin_and_associated_staff(pool_ab: None) -> None:
    assert can_manage_client_tax_documents(ADMIN, OUTSIDER, OUTSIDER, OUTSIDER) is True
    assert can_manage_client_tax_documents(_user(A, "Executive Consultant"), A, None, None) is True
    assert (
        can_manage_client_tax_documents(
            _user(TAX, "Tax Consultant"), None, None, f" {TAX.upper()} "
        )
        is True
    )
    assert (
        can_manage_client_tax_documents(_user(CREATOR, "Consultant"), None, CREATOR, None) is True
    )
    # Validated pool peer of assigned_to or tax_consultant
    assert can_manage_client_tax_documents(_user(B, "Senior Consultant"), A, None, None) is True
    assert can_manage_client_tax_documents(_user(B, "team"), None, None, A) is True


def test_can_manage_tax_documents_denies_outsider_machine_portal_and_unvalidated_pool(
    pool_ab: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        can_manage_client_tax_documents(_user(OUTSIDER, "Tax Consultant"), A, CREATOR, TAX) is False
    )
    assert can_manage_client_tax_documents(_user(A, "internal"), A, A, A) is False
    assert can_manage_client_tax_documents(_user(A, "service"), A, A, A) is False
    assert can_manage_client_tax_documents(_user(A, "client"), A, A, A) is False
    assert can_manage_client_tax_documents(_user(A, "partner"), A, A, A) is False
    assert can_manage_client_tax_documents(_user(A, "unknown_role"), A, A, A) is False
    assert can_manage_client_tax_documents(_user(A, "Consultant"), None, "", "   ") is False

    monkeypatch.setattr(client_pools, "_VALIDATED", None)
    assert can_manage_client_tax_documents(_user(B, "Senior Consultant"), A, None, None) is False


@pytest.mark.asyncio
async def test_get_client_profile_returns_viewer_can_manage_tax_documents(pool_ab: None) -> None:
    from backend.app.routers.crm_enhanced import get_client_profile

    async def _fetch_profile(user: dict[str, str], tax_consultant: str | None) -> dict[str, Any]:
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 7,
                "full_name": "Fixture Client",
                "assigned_to": A,
                "created_by": CREATOR,
                "tax_consultant": tax_consultant,
            }
        )
        conn.fetch = AsyncMock(return_value=[])

        class _Pool:
            @asynccontextmanager
            async def acquire(self):
                yield conn

        with patch("backend.app.routers.crm_enhanced.verify_client_access", new=AsyncMock()):
            return await get_client_profile(client_id=7, pool=_Pool(), current_user=user)

    # Tax consultant (NOT assigned_to): cannot write client record, CAN manage tax documents
    res_tax = await _fetch_profile(_user(TAX, "Tax Consultant"), TAX)
    assert res_tax["viewer_can_write"] is False
    assert res_tax["viewer_can_manage_tax_documents"] is True

    # Setup peer in pool
    res_peer = await _fetch_profile(_user(B, "Executive Consultant"), TAX)
    assert res_peer["viewer_can_manage_tax_documents"] is True

    # Unrelated staff
    res_out = await _fetch_profile(_user(OUTSIDER, "Tax Consultant"), TAX)
    assert res_out["viewer_can_manage_tax_documents"] is False

    # Admin
    res_admin = await _fetch_profile(ADMIN, TAX)
    assert res_admin["viewer_can_manage_tax_documents"] is True


# ============================================================================
# 2. ServiceAccountDriveService Shared Drive mutation methods
# ============================================================================


@pytest.mark.asyncio
async def test_service_account_drive_rename_and_replace_use_supports_all_drives() -> None:
    svc = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    files_api = MagicMock()
    update_req = MagicMock()
    update_req.execute = MagicMock(
        return_value={
            "id": FILE_PERSONAL,
            "name": "SPT_2025.pdf",
            "mimeType": "application/pdf",
            "size": "42",
        }
    )
    files_api.update = MagicMock(return_value=update_req)
    svc.service = MagicMock()
    svc.service.files = MagicMock(return_value=files_api)

    renamed = await svc.rename_file(FILE_PERSONAL, "SPT_2025.pdf")
    assert renamed["name"] == "SPT_2025.pdf"
    _, rename_kwargs = files_api.update.call_args
    assert rename_kwargs["fileId"] == FILE_PERSONAL
    assert rename_kwargs["body"] == {"name": "SPT_2025.pdf"}
    assert rename_kwargs["supportsAllDrives"] is True

    files_api.update.reset_mock()
    replaced = await svc.update_file_content(
        FILE_PERSONAL,
        b"%PDF-1.4 new",
        file_name="SPT_2025_v2.pdf",
        mime_type="application/pdf",
    )
    assert replaced["id"] == FILE_PERSONAL
    _, replace_kwargs = files_api.update.call_args
    assert replace_kwargs["fileId"] == FILE_PERSONAL
    assert replace_kwargs["body"] == {"name": "SPT_2025_v2.pdf"}
    assert replace_kwargs["media_body"] is not None
    assert replace_kwargs["supportsAllDrives"] is True


# ============================================================================
# 3. Endpoint test harness (fake DB + fake Drive tree)
# ============================================================================


class _FakeDrive:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {
            CLIENT_ROOT: {
                "id": CLIENT_ROOT,
                "name": "7_Client",
                "mimeType": FOLDER_MIME,
                "parents": ["INDIVIDUALS_ROOT"],
                "trashed": False,
            },
            PERSONAL_TAX_FOLDER: {
                "id": PERSONAL_TAX_FOLDER,
                "name": "03_Company_and_Tax",
                "mimeType": FOLDER_MIME,
                "parents": [CLIENT_ROOT],
                "trashed": False,
            },
            SUBFOLDER_2026: {
                "id": SUBFOLDER_2026,
                "name": "2026",
                "mimeType": FOLDER_MIME,
                "parents": [PERSONAL_TAX_FOLDER],
                "trashed": False,
            },
            COMPANY_ROOT: {
                "id": COMPANY_ROOT,
                "name": "PT_Example",
                "mimeType": FOLDER_MIME,
                "parents": ["COMPANIES_ROOT"],
                "trashed": False,
            },
            COMPANY_TAX_DEPT: {
                "id": COMPANY_TAX_DEPT,
                "name": "Tax_Dept",
                "mimeType": FOLDER_MIME,
                "parents": ["TAX_SHARED_ROOT"],
                "trashed": False,
            },
            FOREIGN_FOLDER: {
                "id": FOREIGN_FOLDER,
                "name": "Other_Client",
                "mimeType": FOLDER_MIME,
                "parents": ["INDIVIDUALS_ROOT"],
                "trashed": False,
            },
            FILE_PERSONAL: {
                "id": FILE_PERSONAL,
                "name": "spt-personal.pdf",
                "mimeType": "application/pdf",
                "parents": [SUBFOLDER_2026],
                "trashed": False,
                "size": "120",
            },
            FILE_COMPANY: {
                "id": FILE_COMPANY,
                "name": "spt-company.pdf",
                "mimeType": "application/pdf",
                "parents": [COMPANY_TAX_DEPT],
                "trashed": False,
                "size": "240",
            },
            FILE_FOREIGN: {
                "id": FILE_FOREIGN,
                "name": "secret.pdf",
                "mimeType": "application/pdf",
                "parents": [FOREIGN_FOLDER],
                "trashed": False,
                "size": "100",
            },
            FILE_TRASHED: {
                "id": FILE_TRASHED,
                "name": "old.pdf",
                "mimeType": "application/pdf",
                "parents": [PERSONAL_TAX_FOLDER],
                "trashed": True,
                "size": "50",
            },
            FILE_SHORTCUT: {
                "id": FILE_SHORTCUT,
                "name": "link",
                "mimeType": SHORTCUT_MIME,
                "parents": [PERSONAL_TAX_FOLDER],
                "trashed": False,
            },
        }
        self.uploaded: list[dict[str, Any]] = []
        self.renamed: list[tuple[str, str]] = []
        self.replaced: list[dict[str, Any]] = []
        self.trashed: list[str] = []

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        node = self.nodes.get(file_id)
        if node is None:
            raise _http_error(404)
        return dict(node)

    async def upload_file_to_folder(
        self,
        folder_id: str,
        file_content: bytes,
        file_name: str,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self.uploaded.append(
            {
                "folder_id": folder_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "size": len(file_content),
            }
        )
        return {
            "id": "UPLOADED_FILE_ID_01",
            "name": file_name,
            "mimeType": mime_type or "application/pdf",
            "size": str(len(file_content)),
            "webViewLink": "https://drive.google.com/file/d/UPLOADED_FILE_ID_01/view",
        }

    async def rename_file(self, file_id: str, new_name: str) -> dict[str, Any]:
        self.renamed.append((file_id, new_name))
        node = self.nodes[file_id]
        return {
            "id": file_id,
            "name": new_name,
            "mimeType": node["mimeType"],
            "size": node.get("size", "0"),
        }

    async def update_file_content(
        self,
        file_id: str,
        file_content: bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self.replaced.append(
            {
                "file_id": file_id,
                "file_name": file_name,
                "mime_type": mime_type,
                "size": len(file_content),
            }
        )
        node = self.nodes[file_id]
        return {
            "id": file_id,
            "name": file_name or node["name"],
            "mimeType": mime_type or node["mimeType"],
            "size": str(len(file_content)),
        }

    async def trash_file(self, file_id: str) -> None:
        self.trashed.append(file_id)


def _make_client(
    *,
    user: dict[str, Any],
    client_row: dict[str, Any] | None = None,
    company_rows: list[dict[str, Any]] | None = None,
    drive: _FakeDrive | None = None,
    missing_client: bool = False,
) -> tuple[TestClient, _FakeDrive, MagicMock]:
    fake_drive = drive or _FakeDrive()
    drive_factory = MagicMock(return_value=fake_drive)

    stored_client = (
        None
        if missing_client
        else (
            client_row
            if client_row is not None
            else {
                "id": 7,
                "assigned_to": A,
                "created_by": CREATOR,
                "tax_consultant": TAX,
                "google_drive_folder_id": CLIENT_ROOT,
                "drive_folder_id": None,
            }
        )
    )
    stored_companies = (
        company_rows
        if company_rows is not None
        else [
            {
                "google_drive_folder_id": COMPANY_ROOT,
                "tax_dept_folder_id": COMPANY_TAX_DEPT,
            }
        ]
    )

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=stored_client)
    conn.fetch = AsyncMock(return_value=stored_companies)

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    app = FastAPI()
    app.include_router(tax_drive_router, prefix="/api/crm")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: _Pool()
    app.dependency_overrides[get_tax_drive_service_factory] = lambda: drive_factory
    return TestClient(app), fake_drive, drive_factory


# ============================================================================
# 4. Upload tests
# ============================================================================


def test_upload_allowed_for_tax_consultant_into_nested_and_company_folders() -> None:
    client, fake_drive, _ = _make_client(user=_user(TAX, "Tax Consultant"))

    # Nested personal tax subfolder
    r1 = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": SUBFOLDER_2026},
        files={"file": ("C:\\fakepath\\SPT_2025.pdf", b"%PDF-1.4 body", "application/pdf")},
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["success"] is True
    assert body["file"]["name"] == "SPT_2025.pdf"
    assert fake_drive.uploaded[-1]["folder_id"] == SUBFOLDER_2026

    # Linked company tax_dept_folder_id (direct root match)
    r2 = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": COMPANY_TAX_DEPT},
        files={"file": ("coretax.xml", b"<efaktur/>", "application/xml")},
    )
    assert r2.status_code == 200
    assert fake_drive.uploaded[-1]["folder_id"] == COMPANY_TAX_DEPT
    assert fake_drive.uploaded[-1]["file_name"] == "coretax.xml"


def test_upload_allowed_for_setup_pool_peer_and_admin(pool_ab: None) -> None:
    for actor in (_user(A, "Executive Consultant"), _user(B, "Consultant"), ADMIN):
        client, fake_drive, _ = _make_client(user=actor)
        r = client.post(
            "/api/crm/clients/7/tax-documents/upload",
            data={"folder_id": PERSONAL_TAX_FOLDER},
            files={"file": ("bpe.pdf", b"%PDF-1.4 ok", "application/pdf")},
        )
        assert r.status_code == 200
        assert len(fake_drive.uploaded) == 1


def test_upload_denied_for_unrelated_staff_and_non_human_roles_before_drive() -> None:
    for actor in (
        _user(OUTSIDER, "Tax Consultant"),
        _user(TAX, "service"),
        _user(TAX, "client"),
        _user(TAX, "partner"),
    ):
        client, fake_drive, drive_factory = _make_client(user=actor)
        r = client.post(
            "/api/crm/clients/7/tax-documents/upload",
            data={"folder_id": PERSONAL_TAX_FOLDER},
            files={"file": ("bpe.pdf", b"%PDF-1.4 ok", "application/pdf")},
        )
        assert r.status_code == 403
        drive_factory.assert_not_called()
        assert fake_drive.uploaded == []


def test_upload_rejects_foreign_folder_non_folder_empty_oversized_and_malformed_id() -> None:
    client, fake_drive, drive_factory = _make_client(user=_user(TAX, "Tax Consultant"))

    # Foreign folder -> 403
    r_foreign = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": FOREIGN_FOLDER},
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert r_foreign.status_code == 403

    # Unknown Drive folder -> 403 (no existence oracle)
    r_unknown = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": "DOES_NOT_EXIST_99999"},
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert r_unknown.status_code == 403

    # File ID (not a folder) -> 400
    r_file = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": FILE_PERSONAL},
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert r_file.status_code == 400

    drive_factory.reset_mock()
    # Malformed folder_id -> 400 before Drive
    r_bad_id = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": "../bad"},
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert r_bad_id.status_code == 400
    drive_factory.assert_not_called()

    # Empty file -> 400 before Drive
    r_empty = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": PERSONAL_TAX_FOLDER},
        files={"file": ("empty.pdf", b"", "application/pdf")},
    )
    assert r_empty.status_code == 400
    drive_factory.assert_not_called()

    # Oversized (> 4 MB) -> 413 before Drive
    r_big = client.post(
        "/api/crm/clients/7/tax-documents/upload",
        data={"folder_id": PERSONAL_TAX_FOLDER},
        files={"file": ("big.pdf", b"x" * (TAX_DOC_MAX_BYTES + 1), "application/pdf")},
    )
    assert r_big.status_code == 413
    drive_factory.assert_not_called()
    assert fake_drive.uploaded == []


def test_missing_client_and_unlinked_drive_folder_errors() -> None:
    c_missing, _, factory_m = _make_client(user=_user(TAX, "Tax Consultant"), missing_client=True)
    r_missing = c_missing.delete(f"/api/crm/clients/999/tax-documents/{FILE_PERSONAL}")
    assert r_missing.status_code == 404
    factory_m.assert_not_called()

    c_no_root, _, factory_n = _make_client(
        user=_user(TAX, "Tax Consultant"),
        client_row={
            "id": 7,
            "assigned_to": A,
            "created_by": CREATOR,
            "tax_consultant": TAX,
            "google_drive_folder_id": None,
            "drive_folder_id": None,
        },
        company_rows=[],
    )
    r_no_root = c_no_root.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}")
    assert r_no_root.status_code == 403
    factory_n.assert_not_called()


# ============================================================================
# 5. Rename, Replace, and Delete (Trash) tests
# ============================================================================


def test_rename_succeeds_for_client_and_company_files_and_guards_boundaries() -> None:
    client, fake_drive, _ = _make_client(user=_user(TAX, "Tax Consultant"))

    r_ok = client.patch(
        f"/api/crm/clients/7/tax-documents/{FILE_COMPANY}",
        json={"name": "SPT_Badan_2025.pdf"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["file"]["name"] == "SPT_Badan_2025.pdf"
    assert fake_drive.renamed == [(FILE_COMPANY, "SPT_Badan_2025.pdf")]

    # Slash in name -> 400
    assert (
        client.patch(
            f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}",
            json={"name": "bad/name.pdf"},
        ).status_code
        == 400
    )
    # Folder / root / shortcut -> 400
    assert (
        client.patch(
            f"/api/crm/clients/7/tax-documents/{PERSONAL_TAX_FOLDER}",
            json={"name": "renamed-folder"},
        ).status_code
        == 400
    )
    assert (
        client.patch(
            f"/api/crm/clients/7/tax-documents/{CLIENT_ROOT}",
            json={"name": "renamed-root"},
        ).status_code
        == 400
    )
    assert (
        client.patch(
            f"/api/crm/clients/7/tax-documents/{FILE_SHORTCUT}",
            json={"name": "renamed-shortcut"},
        ).status_code
        == 400
    )
    # Foreign file -> 403
    assert (
        client.patch(
            f"/api/crm/clients/7/tax-documents/{FILE_FOREIGN}",
            json={"name": "hijack.pdf"},
        ).status_code
        == 403
    )
    # Trashed file -> 404
    assert (
        client.patch(
            f"/api/crm/clients/7/tax-documents/{FILE_TRASHED}",
            json={"name": "revive.pdf"},
        ).status_code
        == 404
    )


def test_replace_updates_content_in_place_with_optional_name() -> None:
    client, fake_drive, _ = _make_client(user=_user(TAX, "Tax Consultant"))

    # Replace content without renaming
    r1 = client.put(
        f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}/content",
        files={"file": ("revised.pdf", b"%PDF-1.4 revised", "application/pdf")},
    )
    assert r1.status_code == 200
    assert fake_drive.replaced[-1]["file_id"] == FILE_PERSONAL
    assert fake_drive.replaced[-1]["file_name"] is None

    # Replace content AND rename
    r2 = client.put(
        f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}/content",
        data={"name": "SPT_Personal_2025_Final.pdf"},
        files={"file": ("revised.pdf", b"%PDF-1.4 final", "application/pdf")},
    )
    assert r2.status_code == 200
    assert r2.json()["file"]["name"] == "SPT_Personal_2025_Final.pdf"
    assert fake_drive.replaced[-1]["file_name"] == "SPT_Personal_2025_Final.pdf"

    # Foreign file -> 403
    r_foreign = client.put(
        f"/api/crm/clients/7/tax-documents/{FILE_FOREIGN}/content",
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert r_foreign.status_code == 403

    # Folder -> 400
    r_folder = client.put(
        f"/api/crm/clients/7/tax-documents/{SUBFOLDER_2026}/content",
        files={"file": ("x.pdf", b"data", "application/pdf")},
    )
    assert r_folder.status_code == 400


def test_delete_trashes_owned_file_and_refuses_folders_foreign_and_outsiders() -> None:
    client, fake_drive, _ = _make_client(user=_user(TAX, "Tax Consultant"))

    r_ok = client.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}")
    assert r_ok.status_code == 200
    assert r_ok.json() == {"success": True, "file_id": FILE_PERSONAL, "trashed": True}
    assert fake_drive.trashed == [FILE_PERSONAL]

    # Folder / root -> 400
    assert (
        client.delete(f"/api/crm/clients/7/tax-documents/{PERSONAL_TAX_FOLDER}").status_code == 400
    )
    assert client.delete(f"/api/crm/clients/7/tax-documents/{CLIENT_ROOT}").status_code == 400

    # Foreign file -> 403
    assert client.delete(f"/api/crm/clients/7/tax-documents/{FILE_FOREIGN}").status_code == 403

    # Unrelated staff -> 403 before Drive
    c_out, drive_out, factory_out = _make_client(user=_user(OUTSIDER, "Tax Consultant"))
    assert c_out.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}").status_code == 403
    factory_out.assert_not_called()
    assert drive_out.trashed == []
