"""Tests for portal notification prefs endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_database_pool
from backend.app.routers.portal import get_current_client
from backend.app.routers.portal_notification_prefs import router


def _build_app(mock_conn: AsyncMock, client: dict | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)

    class _PoolCtx:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _PoolCtx()

    app.dependency_overrides[get_current_client] = lambda: (
        client
        if client is not None
        else {
            "client_id": 42,
            "user_id": "550e8400-e29b-41d4-a716-446655440000",
        }
    )
    app.dependency_overrides[get_database_pool] = lambda: _Pool()
    return TestClient(app)


def test_get_prefs_returns_defaults_when_row_missing() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = None
    tc = _build_app(mock_conn)
    r = tc.get("/api/portal/notifications/prefs")
    assert r.status_code == 200
    assert r.json() == {
        "email_enabled": True,
        "wa_enabled": False,
        "wa_phone": None,
    }


def test_get_prefs_rejects_missing_user_identity() -> None:
    mock_conn = AsyncMock()
    tc = _build_app(mock_conn, client={"client_id": 42})

    r = tc.get("/api/portal/notifications/prefs")

    assert r.status_code == 400
    assert r.json() == {"detail": "missing user_id"}
    mock_conn.fetchrow.assert_not_awaited()


def test_get_prefs_returns_stored_row() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "email_enabled": False,
        "wa_enabled": True,
        "wa_phone": "628400022000",
    }
    tc = _build_app(mock_conn)
    r = tc.get("/api/portal/notifications/prefs")
    assert r.status_code == 200
    body = r.json()
    assert body["wa_enabled"] is True
    assert body["wa_phone"] == "628400022000"


def test_get_prefs_503_is_client_safe_when_database_read_fails() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = Exception(
        "relation 'notification_prefs' does not exist at internal-host"
    )
    tc = _build_app(mock_conn)

    r = tc.get("/api/portal/notifications/prefs")

    assert r.status_code == 503
    assert r.json() == {"detail": "Notification preferences are temporarily unavailable"}
    assert "notification_prefs" not in r.text
    assert "internal-host" not in r.text


def test_put_prefs_rejects_wa_enabled_without_phone() -> None:
    mock_conn = AsyncMock()
    tc = _build_app(mock_conn)
    r = tc.put(
        "/api/portal/notifications/prefs",
        json={"email_enabled": True, "wa_enabled": True, "wa_phone": None},
    )
    assert r.status_code == 422


def test_put_prefs_rejects_invalid_e164() -> None:
    mock_conn = AsyncMock()
    tc = _build_app(mock_conn)
    r = tc.put(
        "/api/portal/notifications/prefs",
        json={
            "email_enabled": True,
            "wa_enabled": True,
            "wa_phone": "+628400022000",  # leading '+' not allowed
        },
    )
    assert r.status_code == 422


def test_put_prefs_upserts_successfully() -> None:
    mock_conn = AsyncMock()
    mock_conn.execute.return_value = "INSERT 0 1"
    tc = _build_app(mock_conn)
    r = tc.put(
        "/api/portal/notifications/prefs",
        json={
            "email_enabled": True,
            "wa_enabled": True,
            "wa_phone": "628400022000",
        },
    )
    assert r.status_code == 200
    assert r.json()["wa_enabled"] is True
    mock_conn.execute.assert_awaited_once()


def test_put_prefs_503_is_client_safe_when_database_write_fails() -> None:
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = Exception("relation 'notification_prefs' does not exist")
    tc = _build_app(mock_conn)
    r = tc.put(
        "/api/portal/notifications/prefs",
        json={"email_enabled": True, "wa_enabled": False, "wa_phone": None},
    )
    assert r.status_code == 503
    assert r.json() == {"detail": "Notification preferences are temporarily unavailable"}
    assert "notification_prefs" not in r.text
    assert "migration" not in r.text


_VIEWER_ID = "00000000-0000-4000-8000-0000000000aa"
_TARGET_ACCOUNT_ID = "00000000-0000-4000-8000-0000000000bb"


def _viewing_client() -> dict:
    return {"client_id": 42, "user_id": _VIEWER_ID, "impersonating": True}


def test_get_prefs_while_viewing_reads_the_target_clients_own_row() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = _TARGET_ACCOUNT_ID
    mock_conn.fetchrow.return_value = {
        "email_enabled": False,
        "wa_enabled": True,
        "wa_phone": "628400022000",
    }
    tc = _build_app(mock_conn, client=_viewing_client())

    r = tc.get("/api/portal/notifications/prefs?as_client=42")

    assert r.status_code == 200
    assert r.json()["wa_phone"] == "628400022000"
    prefs_args = mock_conn.fetchrow.await_args.args
    assert _TARGET_ACCOUNT_ID in prefs_args
    assert _VIEWER_ID not in prefs_args


def test_get_prefs_while_viewing_target_without_account_returns_defaults() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = None
    tc = _build_app(mock_conn, client=_viewing_client())

    r = tc.get("/api/portal/notifications/prefs?as_client=42")

    assert r.status_code == 200
    assert r.json() == {"email_enabled": True, "wa_enabled": False, "wa_phone": None}
    for call in mock_conn.fetchrow.await_args_list:
        assert _VIEWER_ID not in call.args


def test_put_prefs_refused_while_viewing_as_client() -> None:
    mock_conn = AsyncMock()
    tc = _build_app(mock_conn, client=_viewing_client())

    r = tc.put(
        "/api/portal/notifications/prefs?as_client=42",
        json={"email_enabled": False, "wa_enabled": False, "wa_phone": None},
    )

    assert r.status_code == 403
    assert "read-only" in r.json()["detail"]
    mock_conn.execute.assert_not_awaited()


def test_own_session_still_saves_prefs_under_its_own_id() -> None:
    mock_conn = AsyncMock()
    own = {"client_id": 42, "user_id": _TARGET_ACCOUNT_ID, "impersonating": False}
    tc = _build_app(mock_conn, client=own)

    r = tc.put(
        "/api/portal/notifications/prefs",
        json={"email_enabled": False, "wa_enabled": True, "wa_phone": "628400022000"},
    )

    assert r.status_code == 200
    assert _TARGET_ACCOUNT_ID in mock_conn.execute.await_args.args
    mock_conn.fetchval.assert_not_awaited()


def test_get_prefs_while_viewing_excludes_viewer_and_disabled_accounts() -> None:
    mock_conn = AsyncMock()
    mock_conn.fetchval.return_value = None
    tc = _build_app(mock_conn, client=_viewing_client())

    tc.get("/api/portal/notifications/prefs?as_client=42")

    sql, *args = mock_conn.fetchval.await_args.args
    assert "portal_access = true" in sql
    assert "active = true" in sql
    assert "role = 'client'" in sql
    assert "id::text <> $2" in sql
    assert "ORDER BY created_at, id" in sql
    assert args == [42, _VIEWER_ID]
