"""Regression suite for RT-03 and RT-04 OAuth state CSRF, admin auth, and XSS fixes."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import admin_drive_auth, admin_zoho_auth, google_drive, zoho_email
from backend.services.integrations.zoho_oauth_service import ZohoOAuthService

SYNTHETIC_ADMIN_SECRET = "syn-admin-secret-rt03-rt04"  # pragma: allowlist secret
SUPER_ADMIN_EMAIL = "superadmin@example.test"
STAFF_EMAIL = "staff-member@example.test"


class FakeAsyncRedis:
    """Minimal in-memory async Redis double supporting TTL and atomic GETDEL."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float]] = {}
        self.last_ttl: dict[str, int | None] = {}

    def expire_key(self, key: str) -> None:
        if key in self.store:
            self.store[key] = (self.store[key][0], time.monotonic() - 1.0)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.store[key] = (value, time.monotonic() + (ex if ex is not None else 600))
        self.last_ttl[key] = ex
        return True

    async def getdel(self, key: str) -> str | None:
        entry = self.store.pop(key, None)
        if entry is None:
            return None
        val, expires_at = entry
        return val if time.monotonic() < expires_at else None


class FailingAsyncRedis:
    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        raise ConnectionError("redis unreachable")

    async def getdel(self, key: str) -> str | None:
        raise ConnectionError("redis unreachable")


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncRedis:
    redis_double = FakeAsyncRedis()
    fake_mgr = MagicMock()
    fake_mgr.get_async_client.return_value = redis_double
    monkeypatch.setattr(google_drive.RedisManager, "get_instance", classmethod(lambda cls: fake_mgr))
    google_drive._NONPROD_OAUTH_STATES.clear()
    return redis_double


@pytest.fixture
def mock_drive_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    svc = MagicMock()
    svc.SYSTEM_USER_ID = "SYSTEM"
    svc.root_folder_id = "folder-root-syn"
    svc.is_configured.return_value = True
    svc.is_connected = AsyncMock(return_value=True)
    svc.get_valid_token = AsyncMock(return_value=None)
    svc.get_authorization_url.side_effect = lambda s: f"https://accounts.google.com/o/oauth2/v2/auth?state={s}"
    svc.exchange_code = AsyncMock(return_value={"access_token": "tok-syn", "expires_in": 3600})
    svc.disconnect = AsyncMock(return_value=True)
    svc.list_files = AsyncMock(return_value={"files": [], "next_page_token": None})
    svc.get_file = AsyncMock(return_value={"id": "f1", "name": "doc.pdf", "mimeType": "application/pdf"})
    svc.search_files = AsyncMock(return_value=[])
    mock_cls = MagicMock(return_value=svc)
    mock_cls.SYSTEM_USER_ID = "SYSTEM"
    monkeypatch.setattr(google_drive, "GoogleDriveService", mock_cls)
    monkeypatch.setattr(admin_drive_auth, "GoogleDriveService", mock_cls)
    return svc


@pytest.fixture
def mock_zoho_oauth(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    svc = MagicMock()
    svc.get_authorization_url.side_effect = lambda s: f"https://accounts.zoho.com/oauth/v2/auth?state={s}"
    svc.exchange_code = AsyncMock(return_value={"email": "mailbox@example.test"})
    mock_cls = MagicMock(return_value=svc)
    mock_cls.SCOPES = list(ZohoOAuthService.SCOPES)
    monkeypatch.setattr(zoho_email, "_get_oauth_service", lambda _pool: svc)
    monkeypatch.setattr(admin_zoho_auth, "ZohoOAuthService", mock_cls)
    return svc


@pytest.fixture
def app_and_pool(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, MagicMock]:
    monkeypatch.setenv("ADMIN_SECRET_KEY", SYNTHETIC_ADMIN_SECRET)
    monkeypatch.setattr(google_drive.settings, "admin_emails", SUPER_ADMIN_EMAIL, raising=False)
    monkeypatch.setattr(admin_drive_auth.settings, "google_drive_client_id", "gdrive-cid.test", raising=False)
    monkeypatch.setattr(admin_drive_auth.settings, "google_drive_redirect_uri", "https://api.example.test/cb", raising=False)
    monkeypatch.setattr(admin_zoho_auth.settings, "zoho_client_id", "zoho-cid.test", raising=False)
    monkeypatch.setattr(admin_zoho_auth.settings, "zoho_redirect_uri", "https://api.example.test/zcb", raising=False)
    monkeypatch.setattr(zoho_email.settings, "frontend_url", "https://app.example.test", raising=False)
    pool = MagicMock()
    pool.acquire = MagicMock()
    app = FastAPI()
    for rtr in (google_drive.router, admin_drive_auth.router, zoho_email.router, admin_zoho_auth.router):
        app.include_router(rtr)
    app.dependency_overrides[get_database_pool] = lambda: pool
    return app, pool


def _set_user(app: FastAPI, user: dict[str, Any] | None) -> None:
    if user is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = lambda: user


def test_gdrive_user_oauth_flow_one_time_and_replay_rejected(
    app_and_pool: tuple[FastAPI, MagicMock], fake_redis: FakeAsyncRedis, mock_drive_service: MagicMock
) -> None:
    app, _ = app_and_pool
    _set_user(app, {"user_id": "user-syn-01", "email": STAFF_EMAIL, "role": "team"})
    client = TestClient(app)

    init_resp = client.get("/api/integrations/google-drive/auth/url")
    assert init_resp.status_code == 200
    state = init_resp.json()["state"]
    assert ":" not in state and fake_redis.last_ttl[f"oauth_state:google_drive:{state}"] == 600

    _set_user(app, None)
    cb1 = client.get(f"/api/integrations/google-drive/callback?code=code-1&state={state}", follow_redirects=False)
    assert cb1.status_code == 302 and "success=google_drive_connected" in cb1.headers["location"]
    mock_drive_service.exchange_code.assert_awaited_once_with("code-1", "user-syn-01")

    cb_replay = client.get(f"/api/integrations/google-drive/callback?code=code-1&state={state}", follow_redirects=False)
    assert cb_replay.status_code == 400 and mock_drive_service.exchange_code.await_count == 1


def test_gdrive_system_oauth_flow_one_time_and_forged_states_rejected(
    app_and_pool: tuple[FastAPI, MagicMock], fake_redis: FakeAsyncRedis, mock_drive_service: MagicMock
) -> None:
    app, _ = app_and_pool
    _set_user(app, {"user_id": "admin-syn-01", "email": SUPER_ADMIN_EMAIL, "role": "admin"})
    client = TestClient(app)

    state = client.get("/api/integrations/google-drive/system/authorize").json()["state"]
    _set_user(app, None)
    for bad in ("code=x&state=system", "code=x&state=SYSTEM:forged", "code=x&state=victim:forged", "code=x", f"state={state}"):
        resp = client.get(f"/api/integrations/google-drive/callback?{bad}", follow_redirects=False)
        assert resp.status_code in (400, 403)
    assert mock_drive_service.exchange_code.await_count == 0

    ok = client.get(f"/api/integrations/google-drive/callback?code=sys-c&state={state}", follow_redirects=False)
    assert ok.status_code == 302 and "success=system_drive_connected" in ok.headers["location"]
    mock_drive_service.exchange_code.assert_awaited_once_with("sys-c", "SYSTEM")
    assert client.get(f"/api/integrations/google-drive/callback?code=sys-c&state={state}", follow_redirects=False).status_code == 400


def test_gdrive_expired_state_rejected(
    app_and_pool: tuple[FastAPI, MagicMock], fake_redis: FakeAsyncRedis, mock_drive_service: MagicMock
) -> None:
    app, _ = app_and_pool
    _set_user(app, {"user_id": "user-syn-02", "email": STAFF_EMAIL, "role": "team"})
    client = TestClient(app)
    state = client.get("/api/integrations/google-drive/auth/url").json()["state"]
    fake_redis.expire_key(f"oauth_state:google_drive:{state}")
    _set_user(app, None)
    assert client.get(f"/api/integrations/google-drive/callback?code=c1&state={state}", follow_redirects=False).status_code == 400
    assert mock_drive_service.exchange_code.await_count == 0


def test_gdrive_system_and_raw_file_routes_block_anonymous_and_non_admin(
    app_and_pool: tuple[FastAPI, MagicMock], fake_redis: FakeAsyncRedis, mock_drive_service: MagicMock
) -> None:
    app, _ = app_and_pool
    client = TestClient(app)
    protected = [
        ("GET", "/api/integrations/google-drive/system/status"),
        ("GET", "/api/integrations/google-drive/system/authorize"),
        ("POST", "/api/integrations/google-drive/system/disconnect"),
        ("GET", "/api/integrations/google-drive/files"),
        ("GET", "/api/integrations/google-drive/files/file-syn-1"),
        ("GET", "/api/integrations/google-drive/search?q=passport"),
    ]
    _set_user(app, None)
    for method, path in protected:
        assert client.request(method, path).status_code == 401
    _set_user(app, {"user_id": "staff-1", "email": STAFF_EMAIL, "role": "team"})
    for method, path in protected:
        assert client.request(method, path).status_code == 403
    _set_user(app, {"user_id": "admin-1", "email": SUPER_ADMIN_EMAIL, "role": "admin"})
    assert client.get("/api/integrations/google-drive/system/status").status_code == 200
    assert client.get("/api/integrations/google-drive/files").status_code == 200


def test_zoho_user_oauth_one_time_and_forged_states_rejected(
    app_and_pool: tuple[FastAPI, MagicMock], fake_redis: FakeAsyncRedis, mock_zoho_oauth: MagicMock
) -> None:
    app, _ = app_and_pool
    _set_user(app, {"user_id": "zoho-user-syn-7", "email": STAFF_EMAIL, "role": "team"})
    client = TestClient(app)
    state = client.get("/api/integrations/zoho/auth/url").json()["state"]
    assert ":" not in state and fake_redis.last_ttl[f"oauth_state:zoho:{state}"] == 600

    _set_user(app, None)
    for bad in ("code=z1&state=victim:forged", "code=z1&state=system", "code=z1"):
        assert client.get(f"/api/integrations/zoho/callback?{bad}", follow_redirects=False).status_code in (400, 403)
    assert client.get(f"/api/integrations/google-drive/callback?code=z1&state={state}", follow_redirects=False).status_code == 400
    assert mock_zoho_oauth.exchange_code.await_count == 0

    ok = client.get(f"/api/integrations/zoho/callback?code=z1&state={state}", follow_redirects=False)
    assert ok.status_code in (302, 307) and "connected=true" in ok.headers["location"]
    mock_zoho_oauth.exchange_code.assert_awaited_once_with("z1", "zoho-user-syn-7")
    assert client.get(f"/api/integrations/zoho/callback?code=z1&state={state}", follow_redirects=False).status_code == 400


def test_admin_drive_and_zoho_urls_never_leak_admin_secret_and_enforce_one_time_state(
    app_and_pool: tuple[FastAPI, MagicMock],
    fake_redis: FakeAsyncRedis,
    mock_drive_service: MagicMock,
    mock_zoho_oauth: MagicMock,
) -> None:
    app, _ = app_and_pool
    client = TestClient(app)
    assert client.get("/admin/google-drive/auth-system").status_code == 401
    assert client.get("/admin/zoho/auth", headers={"X-Admin-Secret": "wrong"}).status_code == 401  # pragma: allowlist secret
    assert client.get("/admin/google-drive/auth-system", headers={"X-Admin-Secret": b"s\xe9cret"}).status_code == 401  # pragma: allowlist secret
    assert client.get("/admin/zoho/auth", headers={"X-Admin-Secret": b"s\xe9cret"}).status_code == 401  # pragma: allowlist secret

    drive_payload = client.get("/admin/google-drive/auth-system", headers={"X-Admin-Secret": SYNTHETIC_ADMIN_SECRET}).json()
    zoho_payload = client.get("/admin/zoho/auth", headers={"X-Admin-Secret": SYNTHETIC_ADMIN_SECRET}).json()
    for payload in (drive_payload, zoho_payload):
        assert SYNTHETIC_ADMIN_SECRET not in str(payload) and "secret=" not in str(payload)
    assert "state=system" not in drive_payload["auth_url"]
    assert admin_zoho_auth.ADMIN_ZOHO_USER_ID not in zoho_payload["auth_url"]

    assert client.get(f"/admin/google-drive/callback-system?code=c1&secret={SYNTHETIC_ADMIN_SECRET}").status_code == 400
    assert client.get(f"/admin/zoho/callback?code=c1&secret={SYNTHETIC_ADMIN_SECRET}").status_code == 400

    d_state, z_state = drive_payload["state"], zoho_payload["state"]
    assert client.get(f"/admin/google-drive/callback-system?code=dc&state={d_state}").status_code == 200
    assert client.get(f"/admin/google-drive/callback-system?code=dc&state={d_state}").status_code == 400
    assert client.get(f"/admin/zoho/callback?code=zc&state={z_state}").status_code == 200
    assert client.get(f"/admin/zoho/callback?code=zc&state={z_state}").status_code == 400


def test_user_oauth_state_cannot_be_replayed_against_admin_zoho_callback(
    app_and_pool: tuple[FastAPI, MagicMock], fake_redis: FakeAsyncRedis, mock_zoho_oauth: MagicMock
) -> None:
    app, _ = app_and_pool
    _set_user(app, {"user_id": "lowpriv-user", "email": STAFF_EMAIL, "role": "team"})
    client = TestClient(app)
    user_state = client.get("/api/integrations/zoho/auth/url").json()["state"]
    _set_user(app, None)
    assert client.get(f"/admin/zoho/callback?code=zc&state={user_state}").status_code == 403
    assert mock_zoho_oauth.exchange_code.await_count == 0


def test_shadow_zoho_callback_and_reflected_xss_debug_callback_removed(
    app_and_pool: tuple[FastAPI, MagicMock],
) -> None:
    app, _ = app_and_pool
    client = TestClient(app)
    admin_paths = {getattr(route, "path", None) for route in admin_zoho_auth.router.routes}
    assert "/api/integrations/zoho/callback" not in admin_paths
    assert "/admin/zoho/debug-callback" not in admin_paths
    xss_probe = client.get("/admin/zoho/debug-callback?code=<script>alert(1)</script>")
    assert xss_probe.status_code == 404 and "<script>" not in xss_probe.text


def test_production_fails_closed_when_redis_unavailable(
    app_and_pool: tuple[FastAPI, MagicMock], monkeypatch: pytest.MonkeyPatch, mock_drive_service: MagicMock
) -> None:
    app, _ = app_and_pool
    _set_user(app, {"user_id": "user-prod-1", "email": STAFF_EMAIL, "role": "team"})
    monkeypatch.setattr(google_drive.settings, "environment", "production", raising=False)
    client = TestClient(app)
    nonce = "A" * 43

    for redis_client in (None, FailingAsyncRedis()):
        mgr = MagicMock()
        mgr.get_async_client.return_value = redis_client
        monkeypatch.setattr(google_drive.RedisManager, "get_instance", classmethod(lambda cls, m=mgr: m))
        assert client.get("/api/integrations/google-drive/auth/url").status_code == 503
        assert client.get(f"/api/integrations/google-drive/callback?code=c&state={nonce}", follow_redirects=False).status_code == 403
