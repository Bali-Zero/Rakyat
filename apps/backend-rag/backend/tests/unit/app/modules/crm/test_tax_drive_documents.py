"""Unit tests for client Tax Documents (Google Drive) CRUD and authorization.

Synthetic opaque fixtures only (no real PII or email literals).
Covers:
  - can_manage_client_tax_documents (human admin, assigned_to, tax_consultant,
    created_by, validated pool peers, unrelated staff, non-human/portal roles
    even with allowlisted admin email, machine auth_method rejection)
  - get_client_profile returning viewer_can_manage_tax_documents (including
    machine auth_method denial)
  - ServiceAccountDriveService.rename_file & update_file_content (supportsAllDrives=True)
  - POST /api/crm/clients/{id}/tax-documents/upload
  - PATCH /api/crm/clients/{id}/tax-documents/{file_id} + CRM DB sync
  - PUT /api/crm/clients/{id}/tax-documents/{file_id}/content + DB hash/OCR sync + reprocessing
  - DELETE /api/crm/clients/{id}/tax-documents/{file_id} + CRM soft-delete sync
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
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
from backend.app.utils import client_pools, crm_utils
from backend.app.utils.client_pools import can_manage_client_tax_documents
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService

A = "staff-setup-01"
B = "staff-peer-02"
TAX = "staff-tax-03"
CREATOR = "staff-creator-04"
OUTSIDER = "staff-outsider-05"
ALLOWLISTED_ADMIN = "staff-director-00"
ADMIN = {"email": ALLOWLISTED_ADMIN, "role": "admin"}

CLIENT_ROOT = "CLIENT_ROOT_FOLDER_01"
PERSONAL_TAX_FOLDER = "PERSONAL_TAX_FOLDER_02"
SUBFOLDER_2026 = "SUBFOLDER_2026_03"
COMPANY_ID = 9
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


def _user(
    email: str = TAX,
    role: str = "Tax Consultant",
    *,
    auth_method: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"email": email, "role": role}
    if auth_method is not None:
        out["auth_method"] = auth_method
    return out


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
    assert (
        can_manage_client_tax_documents(
            _user(ALLOWLISTED_ADMIN, "Founder", auth_method="jwt_cookie"),
            OUTSIDER,
            OUTSIDER,
            OUTSIDER,
        )
        is True
    )
    assert can_manage_client_tax_documents(_user(A, "Executive Consultant"), A, None, None) is True
    assert (
        can_manage_client_tax_documents(
            _user(TAX, "Tax Consultant", auth_method="jwt_header"),
            None,
            None,
            f" {TAX.upper()} ",
        )
        is True
    )
    assert (
        can_manage_client_tax_documents(_user(CREATOR, "Consultant"), None, CREATOR, None) is True
    )
    # Validated pool peer of assigned_to or tax_consultant
    assert can_manage_client_tax_documents(_user(B, "Senior Consultant"), A, None, None) is True
    assert can_manage_client_tax_documents(_user(B, "team"), None, None, A) is True


def test_can_manage_tax_documents_denies_outsider_nonhuman_admin_email_and_machine_auth(
    pool_ab: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crm_utils, "_crm_admin_emails", lambda: frozenset({ALLOWLISTED_ADMIN}))

    assert (
        can_manage_client_tax_documents(_user(OUTSIDER, "Tax Consultant"), A, CREATOR, TAX) is False
    )
    # Non-human / portal roles MUST be denied even if their email is on the admin allowlist
    for bad_role in ("monitoring", "internal", "service", "client", "partner", "unknown_role", ""):
        assert (
            can_manage_client_tax_documents(
                _user(ALLOWLISTED_ADMIN, bad_role), ALLOWLISTED_ADMIN, A, TAX
            )
            is False
        )
        assert can_manage_client_tax_documents(_user(A, bad_role), A, A, A) is False

    # Machine auth_method MUST be denied even with role="admin" and allowlisted email
    for bad_method in ("admin_key", "internal_key", "api_key", "public", "unknown", ""):
        assert (
            can_manage_client_tax_documents(
                _user(ALLOWLISTED_ADMIN, "admin", auth_method=bad_method),
                ALLOWLISTED_ADMIN,
                A,
                TAX,
            )
            is False
        )

    assert can_manage_client_tax_documents(_user(A, "Consultant"), None, "", "   ") is False

    monkeypatch.setattr(client_pools, "_VALIDATED", None)
    assert can_manage_client_tax_documents(_user(B, "Senior Consultant"), A, None, None) is False


@pytest.mark.asyncio
async def test_get_client_profile_returns_viewer_can_manage_tax_documents(
    pool_ab: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.routers.crm_enhanced import get_client_profile

    monkeypatch.setattr(crm_utils, "_crm_admin_emails", lambda: frozenset({ALLOWLISTED_ADMIN}))

    async def _fetch_profile(
        user: dict[str, Any],
        tax_consultant: str | None,
        request_auth_method: Any = None,
    ) -> dict[str, Any]:
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
            if request_auth_method is None:
                return await get_client_profile(client_id=7, pool=_Pool(), current_user=user)
            return await get_client_profile(
                client_id=7,
                pool=_Pool(),
                current_user=user,
                request_auth_method=request_auth_method,
            )

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

    # Human admin
    res_admin = await _fetch_profile(ADMIN, TAX, request_auth_method="jwt_cookie")
    assert res_admin["viewer_can_manage_tax_documents"] is True

    # Machine admin_key or non-human role with admin email -> False
    res_machine = await _fetch_profile(ADMIN, TAX, request_auth_method="admin_key")
    assert res_machine["viewer_can_manage_tax_documents"] is False

    res_probe = await _fetch_profile(
        _user(ALLOWLISTED_ADMIN, "monitoring"), TAX, request_auth_method="jwt_header"
    )
    assert res_probe["viewer_can_manage_tax_documents"] is False


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
    def __init__(self, on_drive_call: Any = None) -> None:
        self._on_drive_call = on_drive_call
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
                "webViewLink": f"https://drive.google.com/file/d/{FILE_PERSONAL}/view",
            },
            FILE_COMPANY: {
                "id": FILE_COMPANY,
                "name": "spt-company.pdf",
                "mimeType": "application/pdf",
                "parents": [COMPANY_TAX_DEPT],
                "trashed": False,
                "size": "240",
                "webViewLink": f"https://drive.google.com/file/d/{FILE_COMPANY}/view",
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

    def _tick(self) -> None:
        if self._on_drive_call is not None:
            self._on_drive_call()

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        self._tick()
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
        self._tick()
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
        self._tick()
        self.renamed.append((file_id, new_name))
        node = self.nodes[file_id]
        return {
            "id": file_id,
            "name": new_name,
            "mimeType": node["mimeType"],
            "size": node.get("size", "0"),
            "webViewLink": node.get("webViewLink"),
        }

    async def update_file_content(
        self,
        file_id: str,
        file_content: bytes,
        *,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> dict[str, Any]:
        self._tick()
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
            "webViewLink": node.get("webViewLink"),
        }

    async def trash_file(self, file_id: str) -> None:
        self._tick()
        self.trashed.append(file_id)


class _FakeConn:
    """Tracks SQL statements and serves client/company/document fixtures."""

    def __init__(
        self,
        *,
        stored_client: dict[str, Any] | None,
        stored_companies: list[dict[str, Any]],
        updated_doc_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stored_client = stored_client
        self.stored_companies = stored_companies
        self.updated_doc_rows = updated_doc_rows or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.fetched.append((sql, args))
        return self.stored_client

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched.append((sql, args))
        if "UPDATE documents" in sql:
            return list(self.updated_doc_rows)
        return list(self.stored_companies)

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn
        self.active_connections = 0
        self.max_active_during_drive = 0

    @asynccontextmanager
    async def acquire(self):
        self.active_connections += 1
        try:
            yield self.conn
        finally:
            self.active_connections -= 1


def _make_client(
    *,
    user: dict[str, Any],
    client_row: dict[str, Any] | None = None,
    company_rows: list[dict[str, Any]] | None = None,
    updated_doc_rows: list[dict[str, Any]] | None = None,
    drive: _FakeDrive | None = None,
    missing_client: bool = False,
    middleware_state_user: dict[str, Any] | None = None,
    middleware_auth_type: str | None = None,
) -> tuple[TestClient, _FakeDrive, MagicMock, _FakePool]:
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
                "company_id": COMPANY_ID,
                "google_drive_folder_id": COMPANY_ROOT,
                "tax_dept_folder_id": COMPANY_TAX_DEPT,
            }
        ]
    )

    conn = _FakeConn(
        stored_client=stored_client,
        stored_companies=stored_companies,
        updated_doc_rows=updated_doc_rows,
    )
    pool = _FakePool(conn)

    def _record_pool_usage() -> None:
        pool.max_active_during_drive = max(pool.max_active_during_drive, pool.active_connections)

    fake_drive = drive or _FakeDrive(on_drive_call=_record_pool_usage)
    drive_factory = MagicMock(return_value=fake_drive)

    app = FastAPI()
    if middleware_state_user is not None or middleware_auth_type is not None:

        @app.middleware("http")
        async def _inject_hybrid_auth_state(request: Request, call_next: Any) -> Any:
            if middleware_state_user is not None:
                request.state.user = dict(middleware_state_user)
            if middleware_auth_type is not None:
                request.state.auth_type = middleware_auth_type
            return await call_next(request)

    app.include_router(tax_drive_router, prefix="/api/crm")
    # When simulating HybridAuthMiddleware, let the real get_current_user run so
    # we prove get_tax_documents_user catches auth_method that get_current_user strips.
    if middleware_state_user is None:
        app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: pool
    app.dependency_overrides[get_tax_drive_service_factory] = lambda: drive_factory
    return TestClient(app), fake_drive, drive_factory, pool


# ============================================================================
# 4. Upload tests & machine/non-human middleware boundary tests
# ============================================================================


def test_upload_allowed_for_tax_consultant_into_nested_and_company_folders() -> None:
    client, fake_drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"))

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
    assert pool.max_active_during_drive == 0

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
        client, fake_drive, _, _ = _make_client(user=actor)
        r = client.post(
            "/api/crm/clients/7/tax-documents/upload",
            data={"folder_id": PERSONAL_TAX_FOLDER},
            files={"file": ("bpe.pdf", b"%PDF-1.4 ok", "application/pdf")},
        )
        assert r.status_code == 200
        assert len(fake_drive.uploaded) == 1


def test_machine_auth_and_nonhuman_admin_email_denied_through_real_get_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces HybridAuthMiddleware stripping auth_method via get_current_user."""
    monkeypatch.setattr(crm_utils, "_crm_admin_emails", lambda: frozenset({ALLOWLISTED_ADMIN}))

    # 1. X-Debug-Key (admin_key) + X-API-Key + X-Internal-Key + public -> 403 before DB or Drive
    for method, role in (
        ("admin_key", "admin"),
        ("api_key", "admin"),
        ("internal_key", "internal"),
        ("public", "public"),
    ):
        client, fake_drive, drive_factory, pool = _make_client(
            user={},
            middleware_state_user={
                "email": ALLOWLISTED_ADMIN,
                "role": role,
                "auth_method": method,
            },
            middleware_auth_type=method,
        )
        res = client.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}")
        assert res.status_code == 403
        drive_factory.assert_not_called()
        assert fake_drive.trashed == []
        assert pool.conn.fetched == []

    # 2. Non-human roles with JWT session AND allowlisted admin email -> 403 before DB or Drive
    for bad_role in ("monitoring", "internal", "client", "partner"):
        client, fake_drive, drive_factory, pool = _make_client(
            user={},
            middleware_state_user={
                "email": ALLOWLISTED_ADMIN,
                "role": bad_role,
                "auth_method": "jwt_header",
            },
            middleware_auth_type="jwt_header",
        )
        res = client.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}")
        assert res.status_code == 403
        drive_factory.assert_not_called()
        assert fake_drive.trashed == []
        assert pool.conn.fetched == []

    # 3. Genuine human JWT admin session -> 200
    c_human, d_human, _, _ = _make_client(
        user={},
        middleware_state_user={
            "email": ALLOWLISTED_ADMIN,
            "role": "Founder",
            "auth_method": "jwt_cookie",
        },
        middleware_auth_type="jwt_cookie",
    )
    res_ok = c_human.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}")
    assert res_ok.status_code == 200
    assert d_human.trashed == [FILE_PERSONAL]


def test_upload_denied_for_unrelated_staff_and_non_human_roles_before_drive() -> None:
    for actor in (
        _user(OUTSIDER, "Tax Consultant"),
        _user(TAX, "service"),
        _user(TAX, "client"),
        _user(TAX, "partner"),
    ):
        client, fake_drive, drive_factory, _ = _make_client(user=actor)
        r = client.post(
            "/api/crm/clients/7/tax-documents/upload",
            data={"folder_id": PERSONAL_TAX_FOLDER},
            files={"file": ("bpe.pdf", b"%PDF-1.4 ok", "application/pdf")},
        )
        assert r.status_code == 403
        drive_factory.assert_not_called()
        assert fake_drive.uploaded == []


def test_upload_rejects_foreign_folder_non_folder_empty_oversized_and_malformed_id() -> None:
    client, fake_drive, drive_factory, _ = _make_client(user=_user(TAX, "Tax Consultant"))

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
    c_missing, _, factory_m, _ = _make_client(
        user=_user(TAX, "Tax Consultant"), missing_client=True
    )
    r_missing = c_missing.delete(f"/api/crm/clients/999/tax-documents/{FILE_PERSONAL}")
    assert r_missing.status_code == 404
    factory_m.assert_not_called()

    c_no_root, _, factory_n, _ = _make_client(
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
# 5. Rename, Replace, and Delete (Trash) + CRM DB synchronization tests
# ============================================================================


def test_rename_syncs_registered_client_and_company_documents_and_guards_boundaries() -> None:
    client, fake_drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"))

    r_ok = client.patch(
        f"/api/crm/clients/7/tax-documents/{FILE_COMPANY}",
        json={"name": "SPT_Badan_2025.pdf"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["file"]["name"] == "SPT_Badan_2025.pdf"
    assert fake_drive.renamed == [(FILE_COMPANY, "SPT_Badan_2025.pdf")]
    assert pool.max_active_during_drive == 0

    # Both documents and company_documents received rename sync
    doc_renames = [(sql, args) for sql, args in pool.conn.executed if "UPDATE documents" in sql]
    comp_renames = [
        (sql, args) for sql, args in pool.conn.executed if "UPDATE company_documents" in sql
    ]
    assert len(doc_renames) == 1
    assert doc_renames[0][1] == (7, FILE_COMPANY, "SPT_Badan_2025.pdf")
    assert len(comp_renames) == 1
    assert comp_renames[0][1] == ([COMPANY_ID], FILE_COMPANY, "SPT_Badan_2025.pdf")

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


def test_replace_updates_db_hash_clears_ocr_and_enqueues_reprocessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_doc = {
        "id": 101,
        "document_type": "npwp",
        "subfolder": "03_Company_and_Tax",
        "file_name": "SPT_Personal_2025_Final.pdf",
    }
    client, fake_drive, _, pool = _make_client(
        user=_user(TAX, "Tax Consultant"),
        updated_doc_rows=[registered_doc],
    )
    ocr_mock = AsyncMock(return_value={"dispatched": True, "handler": "npwp"})
    monkeypatch.setattr(
        "backend.services.documents.ocr_dispatcher_service.dispatch_ocr_by_folder",
        ocr_mock,
    )

    payload_bytes = b"%PDF-1.4 final updated bytes"
    expected_hash = hashlib.md5(payload_bytes).hexdigest()

    # Replace content AND rename registered file
    r = client.put(
        f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}/content",
        data={"name": "SPT_Personal_2025_Final.pdf"},
        files={"file": ("revised.pdf", payload_bytes, "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["file"]["name"] == "SPT_Personal_2025_Final.pdf"
    assert fake_drive.replaced[-1]["file_id"] == FILE_PERSONAL
    assert fake_drive.replaced[-1]["file_name"] == "SPT_Personal_2025_Final.pdf"
    # DB connection must NEVER be held while talking to Drive
    assert pool.max_active_during_drive == 0

    # Registered `documents` row updated with new content_hash and invalidated OCR
    doc_updates = [(sql, args) for sql, args in pool.conn.fetched if "UPDATE documents" in sql]
    assert len(doc_updates) == 1
    doc_sql, doc_args = doc_updates[0]
    assert "content_hash = $6" in doc_sql
    assert "ocr_status = 'pending'" in doc_sql
    assert "ocr_completed_at = NULL" in doc_sql
    assert "ocr_extracted_data = NULL" in doc_sql
    assert doc_args[0] == 7
    assert doc_args[1] == FILE_PERSONAL
    assert doc_args[2] == "SPT_Personal_2025_Final.pdf"
    assert doc_args[4] == "application/pdf"
    assert doc_args[5] == expected_hash

    # Registered `company_documents` row also synced & OCR/verification cleared
    comp_updates = [
        (sql, args) for sql, args in pool.conn.executed if "UPDATE company_documents" in sql
    ]
    assert len(comp_updates) == 1
    comp_sql, comp_args = comp_updates[0]
    assert "content_hash = $6" in comp_sql
    assert "ocr_extracted_data = NULL" in comp_sql
    assert "is_verified = false" in comp_sql
    assert comp_args[0] == [COMPANY_ID]
    assert comp_args[1] == FILE_PERSONAL
    assert comp_args[5] == expected_hash

    # Background OCR reprocessing ran for the updated document row
    ocr_mock.assert_awaited_once_with(
        db_pool=pool,
        client_id=7,
        file_id=FILE_PERSONAL,
        folder_name="03_Company_and_Tax",
        filename="SPT_Personal_2025_Final.pdf",
        doc_id=101,
        document_type="npwp",
    )

    # All three dependent cache namespaces invalidated
    cache_mock = tax_drive_documents.invalidate_cache
    invalidated_keys = [call.args[0] for call in cache_mock.await_args_list]
    assert "zantara:team_drive:*" in invalidated_keys
    assert "zantara:crm_clients_documents:*" in invalidated_keys
    assert "zantara:crm_clients_stats:*" in invalidated_keys

    # Foreign file -> 403, Folder -> 400
    assert (
        client.put(
            f"/api/crm/clients/7/tax-documents/{FILE_FOREIGN}/content",
            files={"file": ("x.pdf", b"data", "application/pdf")},
        ).status_code
        == 403
    )
    assert (
        client.put(
            f"/api/crm/clients/7/tax-documents/{SUBFOLDER_2026}/content",
            files={"file": ("x.pdf", b"data", "application/pdf")},
        ).status_code
        == 400
    )


def test_replace_resets_pending_ocr_when_dispatcher_does_not_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered_doc = {
        "id": 202,
        "document_type": "spt_tahunan",
        "subfolder": "03_Tax",
        "file_name": "spt-2025.pdf",
    }
    client, _, _, pool = _make_client(
        user=_user(TAX, "Tax Consultant"),
        updated_doc_rows=[registered_doc],
    )
    monkeypatch.setattr(
        "backend.services.documents.ocr_dispatcher_service.dispatch_ocr_by_folder",
        AsyncMock(return_value={"dispatched": False}),
    )

    r = client.put(
        f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}/content",
        files={"file": ("spt-2025.pdf", b"%PDF-1.4 spt", "application/pdf")},
    )
    assert r.status_code == 200
    cleared = [
        (sql, args)
        for sql, args in pool.conn.executed
        if "SET ocr_status = NULL" in sql and args == (202,)
    ]
    assert len(cleared) == 1


def test_delete_trashes_drive_retires_crm_registrations_and_refuses_invalid_targets() -> None:
    client, fake_drive, _, pool = _make_client(user=_user(TAX, "Tax Consultant"))

    r_ok = client.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}")
    assert r_ok.status_code == 200
    assert r_ok.json() == {"success": True, "file_id": FILE_PERSONAL, "trashed": True}
    assert fake_drive.trashed == [FILE_PERSONAL]
    assert pool.max_active_during_drive == 0

    # Matching client & company CRM registrations marked deleted + archived (not portal deleted_at)
    doc_deletes = [(sql, args) for sql, args in pool.conn.executed if "UPDATE documents" in sql]
    comp_deletes = [
        (sql, args) for sql, args in pool.conn.executed if "UPDATE company_documents" in sql
    ]
    assert len(doc_deletes) == 1
    assert "status = 'deleted'" in doc_deletes[0][0]
    assert "is_archived = true" in doc_deletes[0][0]
    assert "deleted_at" not in doc_deletes[0][0]
    assert doc_deletes[0][1] == (7, FILE_PERSONAL)

    assert len(comp_deletes) == 1
    assert "status = 'deleted'" in comp_deletes[0][0]
    assert "is_archived = true" in comp_deletes[0][0]
    assert comp_deletes[0][1] == ([COMPANY_ID], FILE_PERSONAL)

    cache_mock = tax_drive_documents.invalidate_cache
    invalidated_keys = [call.args[0] for call in cache_mock.await_args_list]
    assert "zantara:team_drive:*" in invalidated_keys
    assert "zantara:crm_clients_documents:*" in invalidated_keys
    assert "zantara:crm_clients_stats:*" in invalidated_keys

    # Folder / root -> 400
    assert (
        client.delete(f"/api/crm/clients/7/tax-documents/{PERSONAL_TAX_FOLDER}").status_code == 400
    )
    assert client.delete(f"/api/crm/clients/7/tax-documents/{CLIENT_ROOT}").status_code == 400

    # Foreign file -> 403
    assert client.delete(f"/api/crm/clients/7/tax-documents/{FILE_FOREIGN}").status_code == 403

    # Unrelated staff -> 403 before Drive
    c_out, drive_out, factory_out, _ = _make_client(user=_user(OUTSIDER, "Tax Consultant"))
    assert c_out.delete(f"/api/crm/clients/7/tax-documents/{FILE_PERSONAL}").status_code == 403
    factory_out.assert_not_called()
    assert drive_out.trashed == []
