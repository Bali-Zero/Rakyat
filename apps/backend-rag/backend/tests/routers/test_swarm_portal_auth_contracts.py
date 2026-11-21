"""Contract tests for the Portal & Auth parity fixes (swarm Vertical 3).

Covers both source-level AST invariants and runtime behavioral contracts using
in-memory mocks only (no database, no network, zero PII).
"""

import ast
import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

APP = Path(__file__).resolve().parents[2] / "app"
SERVICES = Path(__file__).resolve().parents[2] / "services"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ============================================================================
# 1. Static / AST Contract Guards
# ============================================================================


def test_auth_bearer_is_optional_and_cookie_fallback_exists():
    """PORTAL-3A-04: HTTPBearer(auto_error=False) + nz_access_token fallback."""
    src = _src(APP / "routers" / "auth.py")
    assert "HTTPBearer(auto_error=False)" in src
    assert "_get_request_access_token" in src


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


# ============================================================================
# 2. Behavioral Runtime Contract Tests
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
    """PORTAL-3A-04: Bearer header takes precedence; nz_access_token cookie acts as fallback."""
    from backend.app.routers.auth import _extract_auth_token

    req_access = SimpleNamespace(
        headers={},
        cookies={"nz_access_token": "cookie-access"},
    )
    creds = SimpleNamespace(credentials="header-bearer")
    assert _extract_auth_token(req_access, creds) == "header-bearer"
    assert _extract_auth_token(req_access, None) == "cookie-access"

    req_empty = SimpleNamespace(headers={}, cookies={})
    assert _extract_auth_token(req_empty, None) is None


def test_revoke_all_emits_clear_cookie_headers():
    """PORTAL-3A-04 / N7: /revoke-all clears auth cookies on Response."""
    from fastapi import Response

    from backend.app.routers import auth as auth_mod

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = acquire_cm

    response = Response()
    req = SimpleNamespace(headers={}, cookies={}, client=SimpleNamespace(host="127.0.0.1"))
    from backend.services.security.token_revocation import TokenRevocationService

    with patch.object(
        TokenRevocationService,
        "revoke_all_user_tokens",
        new=AsyncMock(return_value=1),
    ):
        out = asyncio.run(
            auth_mod.revoke_all_sessions(
                request=req,
                response=response,
                current_user={"id": "u-1", "email": "staff_placeholder_op_01"},
                db_pool=pool,
            )
        )
    assert out["success"] is True
    set_cookies = [
        v.decode("latin-1")
        for k, v in response.raw_headers
        if k.lower() == b"set-cookie"
    ]
    assert any("nz_access_token=" in c for c in set_cookies)


def test_portal_upload_document_value_error_runtime_statuses():
    """PORTAL-3D-02 / N2: upload_document raises 404 / 429 / 400 / 500 safely without leaking internal messages."""
    from fastapi import HTTPException, UploadFile
    from starlette.datastructures import Headers

    from backend.app.routers.portal import upload_document

    png_bytes = _make_png_bytes()

    def _run_with_error(err: Exception) -> HTTPException:
        upload = UploadFile(
            file=BytesIO(png_bytes),
            filename="doc.png",
            headers=Headers({"content-type": "image/png"}),
        )
        svc = MagicMock()
        svc.upload_document = AsyncMock(side_effect=err)
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
        return exc_info.value

    assert _run_with_error(ValueError("Client 42 not found")).status_code == 404
    rate_exc = _run_with_error(ValueError("Upload rate limit exceeded for bucket"))
    assert rate_exc.status_code == 429
    val_exc = _run_with_error(ValueError("Internal scanner detail xyz"))
    assert val_exc.status_code == 400
    assert "xyz" not in str(val_exc.detail)
    # asyncpg.DataError subclasses ValueError in asyncpg — must stay 500, never 400
    db_exc = _run_with_error(asyncpg.DataError("invalid input syntax"))
    assert db_exc.status_code == 500
