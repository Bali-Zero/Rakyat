"""Guard: the portal profile API masks passport number + date of birth (DAI-140).

GET/PATCH /api/portal/profile return masked identity fields by default. The
full values come only from POST /api/portal/profile/reveal, for the client's
own session (a superuser ``?as_client=`` impersonation is refused), and every
reveal is audit-logged with field names only — never the values.

Synthetic identity only — no real client data.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.portal as portal_module
from backend.app.dependencies import get_database_pool

PASSPORT = "X0000123"
DOB = date(1990, 2, 14)

_ROW = {
    "id": 7,
    "full_name": "Test Client",
    "email": "client@example.test",
    "phone": None,
    "whatsapp": None,
    "nationality": "Testland",
    "passport_number": PASSPORT,
    "passport_expiry": date(2031, 1, 1),
    "date_of_birth": DOB,
    "gender": "female",
    "address": "Test Street 1",
    "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
    "assigned_to": None,
    "assigned_to_name": None,
    "assigned_to_avatar": None,
}


def _session(**extra: object) -> dict[str, object]:
    return {
        "client_id": 7,
        "user_id": "client-user-7",
        "email": "client@example.test",
        "name": "Test Client",
        "impersonating": False,
        **extra,
    }


def _app(mock_db_pool, session: dict[str, object], portal_service=None) -> TestClient:
    pool = mock_db_pool
    application = FastAPI()
    application.include_router(portal_module.router)
    application.dependency_overrides[portal_module.get_current_client] = lambda: session
    application.dependency_overrides[get_database_pool] = lambda: pool
    if portal_service is not None:
        application.dependency_overrides[portal_module.get_portal_service] = lambda: portal_service
    return TestClient(application, raise_server_exceptions=False)


def test_get_profile_masks_passport_and_date_of_birth(mock_db_pool, mock_db_conn) -> None:
    conn = mock_db_conn
    conn.fetchrow = AsyncMock(return_value=_ROW)

    response = _app(mock_db_pool, _session()).get("/api/portal/profile")

    assert response.status_code == 200
    body = response.text
    assert PASSPORT not in body
    assert DOB.isoformat() not in body
    data = response.json()["data"]
    assert data["passport_number"] == "•" * 5 + "123"
    assert data["date_of_birth"] == "1990"
    assert data["identity_masked"] is True
    assert isinstance(data["birthday_today"], bool)
    # Non-identity fields are untouched.
    assert data["passport_expiry"] == "2031-01-01"
    assert data["full_name"] == "Test Client"


def test_patch_profile_response_is_masked_too(mock_db_pool) -> None:
    service = MagicMock()
    service.update_profile = AsyncMock(
        return_value={
            "id": 7,
            "full_name": "Test Client",
            "passport_number": PASSPORT,
            "date_of_birth": DOB.isoformat(),
        }
    )

    response = _app(mock_db_pool, _session(), service).patch(
        "/api/portal/profile", json={"address": "Test Street 2"}
    )

    assert response.status_code == 200
    assert PASSPORT not in response.text
    assert DOB.isoformat() not in response.text
    assert response.json()["data"]["identity_masked"] is True


def test_reveal_returns_full_values_for_own_session_and_audits_without_values(
    mock_db_pool, mock_db_conn, monkeypatch
) -> None:
    conn = mock_db_conn
    conn.fetchrow = AsyncMock(return_value={"passport_number": PASSPORT, "date_of_birth": DOB})
    audit = AsyncMock()
    monkeypatch.setattr(
        "backend.services.security.audit_service.SecurityAuditService.log_event", audit
    )

    response = _app(mock_db_pool, _session()).post("/api/portal/profile/reveal")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {
        "passport_number": PASSPORT,
        "date_of_birth": DOB.isoformat(),
    }
    # Scoped to the session's own client row.
    assert conn.fetchrow.await_args.args[1] == 7
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["action"] == "portal_identity_reveal"
    assert kwargs["resource_id"] == "7"
    logged = json.dumps(kwargs, default=str)
    assert PASSPORT not in logged
    assert DOB.isoformat() not in logged


@pytest.mark.parametrize(
    "session",
    [_session(impersonating=True), _session(client_id=None)],
    ids=["impersonation", "no-client"],
)
def test_reveal_fails_closed_for_another_session(mock_db_pool, mock_db_conn, session) -> None:
    conn = mock_db_conn
    conn.fetchrow = AsyncMock(return_value={"passport_number": PASSPORT, "date_of_birth": DOB})

    response = _app(mock_db_pool, session).post("/api/portal/profile/reveal")

    assert response.status_code == 403
    assert PASSPORT not in response.text
    conn.fetchrow.assert_not_awaited()


def test_reveal_404_when_client_row_missing(mock_db_pool, mock_db_conn) -> None:
    conn = mock_db_conn
    conn.fetchrow = AsyncMock(return_value=None)

    response = _app(mock_db_pool, _session()).post("/api/portal/profile/reveal")

    assert response.status_code == 404


def test_reveal_is_refused_when_the_audit_row_cannot_be_written(mock_db_pool, mock_db_conn) -> None:
    conn = mock_db_conn
    conn.fetchrow = AsyncMock(return_value={"passport_number": PASSPORT, "date_of_birth": DOB})
    conn.execute = AsyncMock(side_effect=RuntimeError("relation security_audit_log is unavailable"))

    response = _app(mock_db_pool, _session()).post("/api/portal/profile/reveal")

    assert response.status_code == 500
    assert PASSPORT not in response.text
    assert DOB.isoformat() not in response.text
    assert "security_audit_log" not in response.text


def test_reveal_answer_is_unchanged_when_the_audit_row_is_written(
    mock_db_pool, mock_db_conn
) -> None:
    conn = mock_db_conn
    conn.fetchrow = AsyncMock(return_value={"passport_number": PASSPORT, "date_of_birth": DOB})
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    response = _app(mock_db_pool, _session()).post("/api/portal/profile/reveal")

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {"passport_number": PASSPORT, "date_of_birth": DOB.isoformat()},
    }
    conn.execute.assert_awaited_once()
    assert "security_audit_log" in conn.execute.await_args.args[0]
