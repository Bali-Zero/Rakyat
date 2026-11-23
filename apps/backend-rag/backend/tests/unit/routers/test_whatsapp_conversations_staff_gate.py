"""
Staff-only gate on the WhatsApp conversation read routes.

GET /api/whatsapp/conversations, GET /api/whatsapp/messages/{phone} and
POST /api/whatsapp/send expose customer conversations or message customers, so they require a person on the team
(`require_team_member`), not merely an authenticated caller. The identities
below are shaped the way `HybridAuthMiddleware` and `APIKeyAuth` really build
them (placed on `request.state.user`, then reduced by `get_current_user`).

Guilt: a monitoring probe, a role-less token, a user-level API key, the
internal service key, a client and a partner get 403 and the store is never
touched. Innocence: a team member still gets the same answers as before.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.app.dependencies import get_optional_database_pool
from backend.app.routers import whatsapp_conversations
from backend.app.routers.whatsapp_conversations import _outbound_rate, router
from backend.app.services import api_key_auth

PHONE = "6280000000001"
ROUTES = ["/api/whatsapp/conversations", f"/api/whatsapp/messages/{PHONE}"]


def _jwt_identity(role: str) -> dict:
    """Shape of `HybridAuthMiddleware.authenticate_jwt` for a valid token."""
    return {
        "id": "subject-0001",
        "email": "synthetic@example.test",
        "role": role,
        "auth_method": "jwt_stateless",
        "name": "synthetic",
        "status": "active",
    }


def _user_api_key_identity(monkeypatch: pytest.MonkeyPatch) -> dict:
    """The real `APIKeyAuth` context for a key with no declared role (role `user`)."""
    monkeypatch.setattr(api_key_auth.settings, "api_keys", "synthetic-key-0000000001")
    monkeypatch.setattr(api_key_auth.settings, "api_key_roles", "", raising=False)
    context = api_key_auth.APIKeyAuth().validate_api_key("synthetic-key-0000000001")
    assert context is not None and context["role"] == "user"
    return context


def _internal_key_identity() -> dict:
    """Shape of the X-Internal-Key pseudo-user built by `authenticate_request`."""
    return {
        "role": "internal",
        "email": "wa-mirror-internal@example.test",
        "auth_method": "internal_key",
        "user_id": "wa-mirror-internal",
    }


def _identity(kind: str, monkeypatch: pytest.MonkeyPatch) -> dict:
    if kind == "monitoring":
        return _jwt_identity("monitoring")
    if kind == "roleless":
        return _jwt_identity("")
    if kind == "user_api_key":
        return _user_api_key_identity(monkeypatch)
    if kind == "internal_key":
        return _internal_key_identity()
    if kind == "client":
        return _jwt_identity("client")
    if kind == "partner":
        return _jwt_identity("partner")
    return _jwt_identity(kind)


def _client(identity: dict, db_pool: MagicMock) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _place_identity(request: Request, call_next):
        request.state.user = identity
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_optional_database_pool] = lambda: db_pool
    return TestClient(app)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "kind",
    ["monitoring", "roleless", "user_api_key", "internal_key", "client", "partner"],
)
def test_non_team_identity_is_refused_and_store_untouched(
    route, kind, monkeypatch, mock_db_pool, mock_db_conn
):
    client = _client(_identity(kind, monkeypatch), mock_db_pool)

    response = client.get(route)

    assert response.status_code == 403
    mock_db_pool.acquire.assert_not_called()
    mock_db_conn.fetch.assert_not_awaited()
    mock_db_conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("role", ["Consultant", "member", "Tax Lead", "admin"])
def test_team_member_still_reads_conversations(role, monkeypatch, mock_db_pool, mock_db_conn):
    mock_db_conn.fetch.return_value = [
        {
            "id": 7,
            "session_id": f"wa_session_{PHONE}",
            "user_id": f"whatsapp_{PHONE}",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"sender_name": "Synthetic Client"},
            "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        }
    ]
    client = _client(_identity(role, monkeypatch), mock_db_pool)

    response = client.get("/api/whatsapp/conversations")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["phone"] == PHONE
    assert body[0]["last_message"] == "hello"
    assert body[0]["client_name"] == "Synthetic Client"


@pytest.mark.parametrize("role", ["Consultant", "member", "Tax Lead", "admin"])
def test_team_member_still_reads_messages(role, monkeypatch, mock_db_pool, mock_db_conn):
    mock_db_conn.fetchrow.return_value = {
        "id": 7,
        "messages": [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}],
        "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    }
    client = _client(_identity(role, monkeypatch), mock_db_pool)

    response = client.get(f"/api/whatsapp/messages/{PHONE}")

    assert response.status_code == 200
    body = response.json()
    assert [m["message_text"] for m in body] == ["hello", "hi"]
    assert [m["direction"] for m in body] == ["inbound", "outbound"]


@pytest.fixture
def _clean_rate_state():
    _outbound_rate.clear()
    yield
    _outbound_rate.clear()


@pytest.mark.parametrize(
    "kind",
    ["monitoring", "roleless", "user_api_key", "internal_key", "client", "partner"],
)
def test_send_refuses_non_team_identity_and_never_sends(
    kind, monkeypatch, mock_db_pool, mock_db_conn, _clean_rate_state
):
    # The recipient is known: only the caller is wrong.
    mock_db_conn.fetchval = AsyncMock(return_value=True)
    client = _client(_identity(kind, monkeypatch), mock_db_pool)

    with patch.object(
        whatsapp_conversations.whatsapp_service, "send_message", new=AsyncMock()
    ) as mock_send:
        response = client.post(
            "/api/whatsapp/send", json={"phone": "+6280000000001", "message": "hello"}
        )

    assert response.status_code == 403
    mock_send.assert_not_awaited()
    mock_db_pool.acquire.assert_not_called()
    assert not any(_outbound_rate.values())


@pytest.mark.parametrize("role", ["Consultant", "member", "Tax Lead", "admin"])
def test_team_member_still_sends(role, monkeypatch, mock_db_pool, mock_db_conn, _clean_rate_state):
    mock_db_conn.fetchval = AsyncMock(return_value=True)
    client = _client(_identity(role, monkeypatch), mock_db_pool)

    with patch.object(
        whatsapp_conversations.whatsapp_service,
        "send_message",
        new=AsyncMock(return_value={"messages": [{"id": "wamid.synthetic"}]}),
    ) as mock_send:
        response = client.post(
            "/api/whatsapp/send", json={"phone": "+6280000000001", "message": "hello"}
        )

    assert response.status_code == 200
    mock_send.assert_awaited_once_with(phone="6280000000001", text="hello")
