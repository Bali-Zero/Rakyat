"""group_staff (HR card, no app) is refused by the PIN doors and invite redemption, active or not."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from backend.app.utils.service_accounts import is_group_staff



class _Conn:
    """Recording connection; `row` is what fetchrow returns after the first call."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.sql: list[str] = []

    async def fetchrow(self, sql, *a):
        self.sql.append(sql)
        return self.rows.pop(0) if self.rows else None

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        return []

    async def fetchval(self, sql, *a):
        self.sql.append(sql)
        return None

    async def close(self):
        return None

    async def execute(self, sql, *a):
        self.sql.append(sql)

    def transaction(self):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=None)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm


def _pool(conn):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=cm)
    return pool


def test_is_group_staff_is_normalised() -> None:
    assert is_group_staff("group_staff") and is_group_staff(" Group_Staff ")
    assert not is_group_staff("client") and not is_group_staff(None)
    assert not is_group_staff("Tax Care")


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.asyncio
async def test_invite_redemption_refuses_a_group_staff_row(active: bool) -> None:
    from backend.services.portal import invite_service
    from backend.services.portal.invite_service import (
        PLACEHOLDER_PIN_HASH,
        InviteService,
    )

    invitation = {
        "id": 1,
        "client_id": 5,
        "email": "s@example.test",
        "expires_at": datetime(2999, 1, 1, tzinfo=timezone.utc),
        "used_at": None,
        "client_name": "C",
        "client_email": "s@example.test",
    }
    # The one row the registration reads (invite_service._REGISTRATION_STATE_SQL):
    # a group-staff login linked to the client, never registered (placeholder
    # PIN), and no other login holding the address. Every other clause of
    # _refusal_of would let this row register; only the group-staff one refuses.
    state = {
        "linked_id": "tm-1",
        "linked_active": active,
        "linked_pin_hash": PLACEHOLDER_PIN_HASH,
        "linked_role": "group_staff",
        "holder_id": None,
    }
    conn = _Conn([invitation, state])
    with pytest.raises(ValueError, match="cannot be registered"):
        await InviteService(_pool(conn)).complete_registration("tok", "1234")
    # Which statement served the state row; a change of the row's shape still
    # surfaces first as a KeyError in _registration_state.
    assert conn.sql[1] == invite_service._REGISTRATION_STATE_SQL
    assert not any("UPDATE team_members" in s for s in conn.sql)
    assert not any("INSERT INTO team_members" in s for s in conn.sql)


@pytest.mark.asyncio
async def test_pin_login_service_refuses_group_staff_before_checking_the_pin() -> None:
    from backend.app.modules.identity.service import IdentityService

    row = {"id": "tm-1", "role": "group_staff", "pin_hash": "x", "locked_until": None}
    conn = _Conn([row])
    svc = IdentityService()
    svc.verify_password = MagicMock(return_value=True)
    with patch.object(svc, "get_db_connection", AsyncMock(return_value=conn)):
        assert await svc.authenticate_user("s@example.test", "1234") is None
    svc.verify_password.assert_not_called()


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.asyncio
async def test_login_route_throttles_group_staff_like_an_inactive_account(active: bool) -> None:
    from backend.app.routers import auth
    from backend.app.routers.auth import LoginRequest, login

    row = {
        "id": "tm-1", "email": "s@example.test", "name": "S", "password_hash": "x",
        "role": "group_staff", "active": active, "linked_client_id": None,
    }
    audit = MagicMock(pool=object(), log_auth_event=AsyncMock())
    req = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers.get.return_value = "ua"
    verify = MagicMock(return_value=True)
    detector = MagicMock()
    detector.is_blocked = AsyncMock(return_value=False)
    detector.record_failure = AsyncMock()
    with (
        patch("backend.services.monitoring.audit_service.get_audit_service", return_value=audit),
        patch("backend.services.security.brute_force.BruteForceDetector", return_value=detector),
        patch("backend.core.redis_manager.RedisManager"),
        patch.object(auth, "verify_password", verify),
    ):
        with pytest.raises(HTTPException) as exc:
            await login(
                LoginRequest(email="s@example.com", credentials="1234"),
                req, MagicMock(), _pool(_Conn([row])),
            )
    assert exc.value.status_code == 401 and exc.value.detail == "Account inactive"
    detector.record_failure.assert_awaited_once()
    audit.log_auth_event.assert_awaited_once()
    verify.assert_not_called()
