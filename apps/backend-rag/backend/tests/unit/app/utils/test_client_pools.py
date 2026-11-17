"""Shared client pools: a pool member holds the union of its peers' client ownership.

Pools come from ``CRM_CLIENT_POOLS_JSON`` (configuration, never code). Unset = today's
behaviour exactly. All emails are fictitious.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.deps import crm_access
from backend.app.routers import crm_practices
from backend.app.utils.client_pools import client_pool_peers
from backend.app.utils.crm_utils import verify_client_access

A = "a@example.test"
B = "b@example.test"
C = "c@example.test"
OUTSIDER = "outsider@example.test"
ADMIN = {"email": "director@example.test", "role": "admin"}


def _user(email: str) -> dict[str, str]:
    return {"email": email, "role": "agent"}


@pytest.fixture(autouse=True)
def pool_ab(monkeypatch):
    monkeypatch.setenv("CRM_CLIENT_POOLS_JSON", f'[["{A}", "{B.upper()}"]]')


def test_peers_of_a_member_are_the_whole_pool():
    assert client_pool_peers(A) == frozenset({A, B})
    assert client_pool_peers(f" {B.upper()} ") == frozenset({A, B})


def test_peers_of_a_non_member_are_only_itself():
    assert client_pool_peers(OUTSIDER) == frozenset({OUTSIDER})


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_peers_of_an_empty_email_match_no_owner(empty):
    assert client_pool_peers(empty) == frozenset({""})


def test_no_pools_configured_is_todays_behaviour(monkeypatch):
    monkeypatch.delenv("CRM_CLIENT_POOLS_JSON")
    assert client_pool_peers(A) == frozenset({A})


# --------------------------------------------------------------------------- #
# verify_client_access(write=True)                                             #
# --------------------------------------------------------------------------- #


def _conn(assigned_to: str | None, created_by: str | None) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"id": 1, "assigned_to": assigned_to, "created_by": created_by}
    )
    return conn


@pytest.mark.asyncio
async def test_pool_member_may_write_a_client_assigned_to_a_peer():
    allowed, owner = await verify_client_access(1, _user(A), _conn(B, None), write=True)
    assert allowed is True and owner == B


@pytest.mark.asyncio
async def test_pool_member_may_write_a_client_created_by_a_peer():
    allowed, _ = await verify_client_access(1, _user(B), _conn(OUTSIDER, A), write=True)
    assert allowed is True


@pytest.mark.asyncio
async def test_pool_member_is_denied_a_client_owned_by_a_non_member():
    with pytest.raises(HTTPException) as exc:
        await verify_client_access(1, _user(A), _conn(OUTSIDER, OUTSIDER), write=True)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_non_member_is_denied_a_pool_client():
    with pytest.raises(HTTPException) as exc:
        await verify_client_access(1, _user(OUTSIDER), _conn(A, B), write=True)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unassigned_client_stays_closed_to_pool_members():
    with pytest.raises(HTTPException) as exc:
        await verify_client_access(1, _user(A), _conn(None, None), write=True)
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# the two filter dependencies                                                  #
# --------------------------------------------------------------------------- #


def test_crm_filter_is_the_sorted_peer_list_for_a_member():
    assert crm_access.get_crm_user_filter(_user(B.upper())) == [A, B]


def test_crm_filter_is_own_email_for_a_non_member():
    assert crm_access.get_crm_user_filter(_user(OUTSIDER)) == [OUTSIDER]


def test_crm_filter_is_none_for_an_admin_even_inside_a_pool(monkeypatch):
    monkeypatch.setenv("CRM_CLIENT_POOLS_JSON", f'[["{ADMIN["email"]}", "{A}"]]')
    assert crm_access.get_crm_user_filter(ADMIN) is None


def test_practices_filter_is_the_sorted_peer_list_for_a_member():
    assert crm_access.get_practices_user_filter(_user(A)) == [A, B]


def test_practices_filter_is_own_email_for_a_non_member():
    assert crm_access.get_practices_user_filter(_user(OUTSIDER)) == [OUTSIDER]


def test_practices_filter_is_none_for_an_admin():
    assert crm_access.get_practices_user_filter(ADMIN) is None


# --------------------------------------------------------------------------- #
# router level: the clients list binds the peer LIST                           #
# --------------------------------------------------------------------------- #


def _list_call(user: dict[str, str]) -> tuple[str, tuple[Any, ...]]:
    from backend.app.routers import crm_clients
    from backend.core.cache import get_cache_service

    get_cache_service()._memory_cache.clear()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value=None)

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    app = FastAPI()
    app.include_router(crm_clients.router)
    app.dependency_overrides[get_database_pool] = lambda: _Pool()
    app.dependency_overrides[get_current_user] = lambda: user
    response = TestClient(app).get("/api/crm/clients")
    assert response.status_code == 200, response.text
    sql, *params = conn.fetch.await_args_list[0].args
    return sql, tuple(params)


def test_clients_list_binds_the_peer_list_for_a_member():
    sql, params = _list_call(_user(A))
    assert "c.assigned_to = ANY($1::text[])" in sql
    assert "c.tax_consultant = ANY($1::text[])" in sql
    assert params[0] == [A, B]


def test_clients_list_binds_only_own_email_for_a_non_member():
    _, params = _list_call(_user(OUTSIDER))
    assert params[0] == [OUTSIDER]


def test_clients_list_is_unfiltered_for_an_admin():
    sql, params = _list_call(ADMIN)
    assert "ANY($1::text[])" not in sql
    assert not any(isinstance(p, list) for p in params)


# --------------------------------------------------------------------------- #
# practices ownership                                                          #
# --------------------------------------------------------------------------- #


def test_practice_ownership_flags_true_for_a_peers_practice():
    _, created_by_match, assigned_to_match = crm_practices._practice_ownership_flags(
        _user(A), created_by=B, assigned_to=B
    )
    assert created_by_match and assigned_to_match


def test_practice_ownership_flags_false_for_a_non_members_practice():
    _, created_by_match, assigned_to_match = crm_practices._practice_ownership_flags(
        _user(A), created_by=OUTSIDER, assigned_to=OUTSIDER
    )
    assert not created_by_match and not assigned_to_match


def test_practice_ownership_flags_false_for_a_non_member_on_a_pool_practice():
    _, created_by_match, assigned_to_match = crm_practices._practice_ownership_flags(
        _user(OUTSIDER), created_by=A, assigned_to=B
    )
    assert not created_by_match and not assigned_to_match


# --------------------------------------------------------------------------- #
# can_write_client + the profile's viewer_can_write                            #
# --------------------------------------------------------------------------- #


def test_can_write_client_owner_peer_outsider_admin():
    from backend.app.utils.client_pools import can_write_client

    assert can_write_client(_user(A), A, None) is True
    assert can_write_client(_user(A), B, None) is True
    assert can_write_client(_user(A), OUTSIDER, B) is True
    assert can_write_client(_user(A), OUTSIDER, OUTSIDER) is False
    assert can_write_client(_user(OUTSIDER), A, B) is False
    assert can_write_client(_user(A), None, None) is False
    assert can_write_client(ADMIN, OUTSIDER, None) is True


async def _profile(user: dict[str, str], assigned_to: str | None, created_by: str | None):
    from backend.app.routers.crm_enhanced import get_client_profile

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": 1,
            "full_name": "Fixture Client",
            "assigned_to": assigned_to,
            "created_by": created_by,
        }
    )
    conn.fetch = AsyncMock(return_value=[])

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    with patch("backend.app.routers.crm_enhanced.verify_client_access", new=AsyncMock()):
        return await get_client_profile(client_id=1, pool=_Pool(), current_user=user)


@pytest.mark.asyncio
async def test_profile_viewer_can_write_true_for_owner_and_pool_peer_and_admin():
    assert (await _profile(_user(A), A, None))["viewer_can_write"] is True
    assert (await _profile(_user(A), B, None))["viewer_can_write"] is True
    assert (await _profile(ADMIN, OUTSIDER, None))["viewer_can_write"] is True


@pytest.mark.asyncio
async def test_profile_viewer_can_write_false_for_a_non_member_and_created_by_not_leaked():
    result = await _profile(_user(OUTSIDER), A, B)
    assert result["viewer_can_write"] is False
    assert "created_by" not in result["client"]
