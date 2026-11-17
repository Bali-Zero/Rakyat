"""Contract tests for the Portal & Auth parity fixes (swarm Vertical 3).

Covers both source-level AST invariants and runtime behavioral contracts using
in-memory mocks only (no database, no network, zero PII).
"""

import ast
import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
SERVICES = Path(__file__).resolve().parents[2] / "services"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ============================================================================
# 1. Static / AST Contract Guards (run under plain python3 & pytest)
# ============================================================================


def test_payment_proof_route_is_implemented():
    """PORTAL-3A-01: POST /{invoice_id}/payment-proof exists and returns the TS envelope."""
    src = _src(APP / "routers" / "portal_billing.py")
    assert '"/{invoice_id}/payment-proof"' in src
    assert "Payment proof uploaded" in src
    assert "portal_service.upload_document" in src
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "upload_payment_proof" in names


def test_auth_bearer_is_optional_and_cookie_fallback_exists():
    """PORTAL-3A-04: HTTPBearer(auto_error=False) + nz_access_token / nz_refresh_token fallback."""
    src = _src(APP / "routers" / "auth.py")
    assert "HTTPBearer(auto_error=False)" in src
    assert "nz_access_token" in src
    assert "nz_refresh_token" in src


def test_revoke_all_clears_cookies():
    """PORTAL-3A-04: revoke-all clears auth cookies."""
    tree = ast.parse(_src(APP / "routers" / "auth.py"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "revoke_all_sessions"
    )
    called = {
        c.func.id
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
    }
    assert "clear_auth_cookies" in called


def test_portal_upload_value_error_mapping():
    """PORTAL-3D-02: upload_document maps ValueError to 404 / 429 / 400 (literal codes)."""
    tree = ast.parse(_src(APP / "routers" / "portal.py"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "upload_document"
    )
    codes = set()
    for handler in (h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)):
        if isinstance(handler.type, ast.Name) and handler.type.id == "ValueError":
            for call in ast.walk(handler):
                if isinstance(call, ast.Call):
                    for kw in call.keywords:
                        if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                            codes.add(kw.value.value)
    assert {400, 404, 429} <= codes


def test_invite_complete_redirects_to_existing_page():
    """PORTAL-3C-01: redirect_to must point to /portal/login-upgraded."""
    src = _src(APP / "routers" / "portal_invite.py")
    assert 'redirect_to="/portal/login-upgraded"' in src


def test_dashboard_parses_string_missing_documents():
    """PORTAL-3C-07: TEXT missing_documents is parsed before slicing."""
    src = _src(SERVICES / "portal" / "_mixins" / "dashboard.py")
    assert "missing_documents" in src
    assert "json" in src
    assert "isinstance" in src


# ============================================================================
# 2. Behavioral Runtime Contract Tests (run under pytest on backend venv)
# ============================================================================


def test_dashboard_action_items_never_slice_string_into_chars():
    """PORTAL-3C-07: _build_action_items parses JSON array strings and CSV strings."""
    from backend.services.portal._mixins.dashboard import PortalDashboardMixin

    mixin = PortalDashboardMixin()
    visa_data = {"status": "active", "daysRemaining": 120}
    rows = [
        {
            "id": 11,
            "practice_name": "KITAS Renewal",
            "status": "waiting_documents",
            "missing_documents": '["doc_alpha", "doc_beta", "doc_gamma", "doc_delta"]',
        },
        {
            "id": 12,
            "practice_name": "Company Setup",
            "status": "on_process",
            "missing_documents": "doc_one, doc_two",
        },
    ]
    actions = mixin._build_action_items(rows, visa_data)
    assert len(actions) == 2
    assert actions[0]["description"] == "Please upload: doc_alpha, doc_beta, doc_gamma..."
    assert actions[1]["description"] == "Please upload: doc_one, doc_two"


def _make_png_bytes() -> bytes:
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (1, 1), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_extract_auth_token_precedence_and_cookie_fallback():
    """PORTAL-3A-04: Bearer header takes precedence; cookies act as fallback."""
    from backend.app.routers.auth import _extract_auth_token

    req_access = SimpleNamespace(
        headers={},
        cookies={"nz_access_token": "cookie-access", "nz_refresh_token": "cookie-refresh"},
    )
    creds = SimpleNamespace(credentials="header-bearer")
    assert _extract_auth_token(req_access, creds) == "header-bearer"
    assert _extract_auth_token(req_access, None) == "cookie-access"

    req_refresh_only = SimpleNamespace(
        headers={},
        cookies={"nz_refresh_token": "cookie-refresh"},
    )
    assert _extract_auth_token(req_refresh_only, None, allow_refresh_cookie=False) is None
    assert (
        _extract_auth_token(req_refresh_only, None, allow_refresh_cookie=True)
        == "cookie-refresh"
    )


def test_upload_payment_proof_happy_path_and_ownership_guard():
    """PORTAL-3A-01: upload_payment_proof verifies invoice ownership and returns TS shape."""
    from fastapi import HTTPException, UploadFile
    from starlette.datastructures import Headers

    from backend.app.routers.portal_billing import upload_payment_proof
    from backend.services.portal.upload_validation import DuplicateDocumentError

    png_bytes = _make_png_bytes()

    def _make_upload(payload: bytes = png_bytes, filename: str = "receipt.png") -> UploadFile:
        return UploadFile(
            file=BytesIO(payload),
            filename=filename,
            headers=Headers({"content-type": "image/png"}),
        )

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"id": 501, "invoice_number": "INV-2026-501", "practice_id": 77}
    )
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    portal_service = MagicMock()
    portal_service.pool = pool
    portal_service.upload_document = AsyncMock(
        return_value={"id": 9001, "created_at": "2026-11-16T08:00:00+00:00"}
    )

    client_ctx = {"client_id": 42, "user_id": 7}

    result = asyncio.run(
        upload_payment_proof(
            invoice_id=501,
            file=_make_upload(),
            client=client_ctx,
            portal_service=portal_service,
        )
    )
    assert result == {
        "success": True,
        "message": "Payment proof uploaded",
        "data": {
            "id": 9001,
            "status": "received",
            "note": None,
            "created_at": "2026-11-16T08:00:00+00:00",
        },
    }
    portal_service.upload_document.assert_awaited_once()

    # Unowned invoice -> 404
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc_unowned:
        asyncio.run(
            upload_payment_proof(
                invoice_id=999,
                file=_make_upload(),
                client=client_ctx,
                portal_service=portal_service,
            )
        )
    assert exc_unowned.value.status_code == 404

    # Duplicate upload -> 409
    conn.fetchrow = AsyncMock(
        return_value={"id": 501, "invoice_number": "INV-2026-501", "practice_id": 77}
    )
    portal_service.upload_document = AsyncMock(side_effect=DuplicateDocumentError("dup"))
    with pytest.raises(HTTPException) as exc_dup:
        asyncio.run(
            upload_payment_proof(
                invoice_id=501,
                file=_make_upload(),
                client=client_ctx,
                portal_service=portal_service,
            )
        )
    assert exc_dup.value.status_code == 409


def test_portal_upload_document_value_error_runtime_statuses():
    """PORTAL-3D-02: upload_document raises 404 / 429 / 400 according to ValueError cause."""
    from fastapi import HTTPException, UploadFile
    from starlette.datastructures import Headers

    from backend.app.routers.portal import upload_document

    png_bytes = _make_png_bytes()

    def _run_with_error(err_msg: str) -> int:
        upload = UploadFile(
            file=BytesIO(png_bytes),
            filename="doc.png",
            headers=Headers({"content-type": "image/png"}),
        )
        svc = MagicMock()
        svc.upload_document = AsyncMock(side_effect=ValueError(err_msg))
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                upload_document(
                    file=upload,
                    document_type="tax_report",
                    practice_id=None,
                    document_purpose=None,
                    client={"client_id": 42, "user_id": 7},
                    portal_service=svc,
                )
            )
        return exc_info.value.status_code

    assert _run_with_error("Client 42 not found") == 404
    assert _run_with_error("Upload rate limit exceeded") == 429
    assert _run_with_error("Invalid document metadata") == 400
