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
from backend.app.utils import client_pools
from backend.app.utils.client_pools import client_pool_peers
from backend.app.utils.crm_utils import verify_client_access

A = "a@example.test"
B = "b@example.test"
C = "c@example.test"
OUTSIDER = "outsider@example.test"
ADMIN = {"email": "director@example.test", "role": "admin"}


def _user(email: str) -> dict[str, str]:
    return {"email": email, "role": "team"}


class _TeamMembers:
    """team_members double: ``fetch`` applies the same active/email filter as the SQL."""

    def __init__(self, rows: dict[str, tuple[str, bool]] | None = None, *, fail: bool = False):
        self.rows = rows if rows is not None else {A: ("team", True), B: ("team", True)}
        self.fail = fail
        self.calls = 0

    async def fetch(self, sql: str, entries: list[str]) -> list[dict[str, str]]:
        self.calls += 1
        if self.fail:
            raise OSError("db down")
        assert "active" in sql and "ANY($1::text[])" in sql
        return [
            {"email": e, "role": self.rows[e][0]}
            for e in entries
            if e in self.rows and self.rows[e][1]
        ]


def _run(coro) -> None:
    """Drive a coroutine that never truly suspends (the doubles above are immediate)."""
    try:
        coro.send(None)
    except StopIteration:
        return
    raise AssertionError("coroutine suspended")


def _validate(rows=None, *, fail=False) -> _TeamMembers:
    conn = _TeamMembers(rows, fail=fail)
    _run(client_pools.validate_client_pools(conn))
    return conn


@pytest.fixture(autouse=True)
def pool_ab(monkeypatch):
    monkeypatch.setenv("CRM_CLIENT_POOLS_JSON", f'[["{A}", "{B.upper()}"]]')
    monkeypatch.setattr(client_pools, "_VALIDATED", None)
    monkeypatch.setattr(client_pools, "_LAST_ATTEMPT", None)
    _validate()


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
    _validate()
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
    _validate({ADMIN["email"]: ("admin", True), A: ("team", True)})
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


@pytest.mark.asyncio
async def test_profile_viewer_can_create_company_follows_pool_membership():
    assert (await _profile({"email": A, "role": "Reception"}, A, None))[
        "viewer_can_create_company"
    ] is True
    assert (await _profile({"email": OUTSIDER, "role": "Reception"}, A, None))[
        "viewer_can_create_company"
    ] is False


# --------------------------------------------------------------------------- #
# machine principals never expand into a pool                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", ["internal", "user", "monitoring", "client", "partner", None])
def test_machine_or_foreign_role_in_a_pool_keeps_only_its_own_email(role):
    from backend.app.utils.client_pools import caller_pool_peers

    assert caller_pool_peers({"email": A, "role": role}) == frozenset({A})
    assert crm_access.get_crm_user_filter({"email": A, "role": role}) == [A]


def test_human_role_in_the_same_pool_expands():
    from backend.app.utils.client_pools import caller_pool_peers

    assert caller_pool_peers({"email": A, "role": "team"}) == frozenset({A, B})
    assert caller_pool_peers({"email": A, "role": "Tax Lead"}) == frozenset({A, B})


@pytest.mark.asyncio
async def test_machine_role_in_a_pool_is_denied_write_on_a_peers_client():
    with pytest.raises(HTTPException) as exc:
        await verify_client_access(1, {"email": A, "role": "internal"}, _conn(B, None), write=True)
    assert exc.value.status_code == 403
    allowed, _ = await verify_client_access(1, _user(A), _conn(B, None), write=True)
    assert allowed is True


# --------------------------------------------------------------------------- #
# compliance alert detail / outcome: peers allowed, non-members refused        #
# --------------------------------------------------------------------------- #


def _alert_pool(assigned_to: str | None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"alert_id": "al-1", "client_id": 7, "status": "open"})
    conn.fetchval = AsyncMock(side_effect=[assigned_to, None])
    conn.fetch = AsyncMock(return_value=[])

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    return _Pool()


@pytest.mark.asyncio
async def test_compliance_detail_allows_a_peer_and_refuses_a_non_member():
    from backend.app.routers.compliance_alerts import get_alert

    result = await get_alert("al-1", user=_user(A), pool=_alert_pool(B))
    assert result["alert"]["alert_id"] == "al-1"
    with pytest.raises(HTTPException) as exc:
        await get_alert("al-1", user=_user(OUTSIDER), pool=_alert_pool(B))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_compliance_outcome_allows_a_peer_and_refuses_a_non_member():
    from backend.app.routers.compliance_alerts import OutcomeBody, post_outcome

    body = OutcomeBody(outcome="acted")
    with pytest.raises(HTTPException) as peer:
        await post_outcome("al-1", body, user=_user(A), pool=_alert_pool(B))
    assert peer.value.status_code == 404  # past the RBAC fence: the lock read finds nothing
    with pytest.raises(HTTPException) as other:
        await post_outcome("al-1", body, user=_user(OUTSIDER), pool=_alert_pool(B))
    assert other.value.status_code == 403


# --------------------------------------------------------------------------- #
# WhatsApp copilot tools never expand a pool                                   #
# --------------------------------------------------------------------------- #


def _tool_pool():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    return _Pool(), conn


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["TeamMyClientsTool", "TeamMyPracticesTool"])
async def test_whatsapp_tools_bind_only_the_senders_own_email_even_inside_a_pool(
    tool_name, monkeypatch
):
    monkeypatch.setenv("WA_TEAM_CRM_TOOLS_ENABLED", "true")
    from backend.services.rag.agentic import team_crm_tools as tcm

    pool, conn = _tool_pool()
    await getattr(tcm, tool_name)(pool).execute(
        _caller_profile={"role": "team", "name": "Member A", "email": A.upper()}
    )
    bound = [p for p in conn.fetch.call_args.args[1:] if isinstance(p, list)]
    assert bound == [[A]]
    assert B not in str(conn.fetch.call_args.args)


# --------------------------------------------------------------------------- #
# direct-call identity: list_interactions(user_id=...) binds the resolved email #
# --------------------------------------------------------------------------- #


async def _direct_list(user_id: str | None, current_user: dict[str, str] | None):
    from backend.app.routers.crm_interactions import list_interactions

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    await list_interactions(
        client_id=None,
        practice_id=None,
        team_member=None,
        interaction_type=None,
        sentiment=None,
        limit=10,
        offset=0,
        current_user=current_user,
        db_pool=_Pool(),
        user_id=user_id,
        pool=None,
    )
    return conn.fetch.await_args_list[0].args[1]


@pytest.mark.asyncio
async def test_direct_call_by_user_id_binds_exactly_that_email_even_inside_a_pool():
    assert await _direct_list(A, None) == [A]


@pytest.mark.asyncio
async def test_interactions_list_through_the_router_still_expands_for_a_human_peer():
    assert await _direct_list(None, _user(A)) == [A, B]


# --------------------------------------------------------------------------- #
# pool entries are validated as ENTITIES (active human team_members rows)      #
# --------------------------------------------------------------------------- #


def _set_pools(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("CRM_CLIENT_POOLS_JSON", raw)
    monkeypatch.setattr(client_pools, "_VALIDATED", None)


def test_before_validation_there_are_no_pools(monkeypatch):
    monkeypatch.setattr(client_pools, "_VALIDATED", None)
    assert client_pool_peers(A) == frozenset({A})


def test_all_entries_active_and_human_activates_the_pools():
    assert client_pool_peers(A) == frozenset({A, B})


def test_an_unknown_email_in_a_pool_refuses_every_pool():
    _validate({A: ("team", True)})
    assert client_pool_peers(A) == frozenset({A})
    assert client_pool_peers(B) == frozenset({B})


def test_an_inactive_row_refuses_every_pool():
    _validate({A: ("team", True), B: ("team", False)})
    assert client_pool_peers(A) == frozenset({A})


@pytest.mark.parametrize("role", ["internal", "user", "monitoring", "client", "partner"])
def test_a_machine_or_foreign_role_row_refuses_every_pool(role):
    _validate({A: ("team", True), B: (role, True)})
    assert client_pool_peers(A) == frozenset({A})


def test_one_bad_entry_in_a_second_pool_refuses_the_valid_first_pool(monkeypatch):
    _set_pools(monkeypatch, f'[["{A}", "{B}"], ["{C}"]]')
    _validate({A: ("team", True), B: ("team", True)})
    assert client_pool_peers(A) == frozenset({A})


@pytest.mark.parametrize("spelling", ["x@.", ".@example.test", "not-an-email", "a..b@example.test"])
def test_the_gate_strings_fail_by_entity_not_by_spelling(monkeypatch, spelling):
    _set_pools(monkeypatch, f'[["{A}", "{B}"], ["{spelling}"]]')
    _validate()
    assert client_pool_peers(A) == frozenset({A})
    assert not hasattr(client_pools, "_plausible_email")


def test_a_db_error_during_validation_leaves_no_pools(monkeypatch):
    _set_pools(monkeypatch, f'[["{A}", "{B}"]]')
    _validate(fail=True)
    assert client_pools._VALIDATED is None
    assert client_pool_peers(A) == frozenset({A})


def test_validation_warning_names_the_variable_and_a_count_never_a_value(monkeypatch, caplog):
    _validate({A: ("team", True)})
    assert "CRM_CLIENT_POOLS_JSON" in caplog.text and "1 entries" in caplog.text
    assert B not in caplog.text and A not in caplog.text


def test_lazy_retry_runs_at_most_once_per_sixty_seconds(monkeypatch):
    _set_pools(monkeypatch, f'[["{A}", "{B}"]]')
    monkeypatch.setattr(client_pools, "_LAST_ATTEMPT", None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(client_pools.time, "monotonic", lambda: clock["t"])
    conn = _TeamMembers(fail=True)
    _run(client_pools.ensure_client_pools_validated(conn))
    _run(client_pools.ensure_client_pools_validated(conn))
    assert conn.calls == 1
    clock["t"] += 61
    ok = _TeamMembers()
    _run(client_pools.ensure_client_pools_validated(ok))
    assert ok.calls == 1 and client_pool_peers(A) == frozenset({A, B})
    _run(client_pools.ensure_client_pools_validated(ok))
    assert ok.calls == 1


@pytest.mark.asyncio
async def test_verify_client_access_triggers_the_lazy_validation(monkeypatch):
    _set_pools(monkeypatch, f'[["{A}", "{B}"]]')
    monkeypatch.setattr(client_pools, "_LAST_ATTEMPT", None)
    members = _TeamMembers()
    conn = _conn(B, None)
    conn.fetch = members.fetch
    allowed, _ = await verify_client_access(1, _user(A), conn, write=True)
    assert allowed is True and members.calls == 1
