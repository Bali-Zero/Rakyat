"""Who may edit / create a company: managing staff (and pool peers) edit, pool members create.

Synthetic emails only (@example.test).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.modules.crm.company_router import router
from backend.app.utils import client_pools
from backend.app.utils.client_pools import caller_in_client_pool, can_edit_company
from backend.app.utils.crm_utils import can_create_company

A = "a@example.test"
B = "b@example.test"
OTHER = "other@example.test"
ADMIN = {"email": "director@example.test", "role": "admin"}


def _user(email: str = A, role: str = "Tax Consultant") -> dict[str, str]:
    return {"email": email, "role": role}


def _owner(assigned=None, created=None, tax=None) -> dict[str, str | None]:
    return {"assigned_to": assigned, "created_by": created, "tax_consultant": tax}


@pytest.fixture
def pool_ab(monkeypatch):
    monkeypatch.setattr(client_pools, "_VALIDATED", (frozenset({A, B}),))


@pytest.fixture(autouse=True)
def no_pools_by_default(monkeypatch):
    monkeypatch.setattr(client_pools, "_VALIDATED", ())
    monkeypatch.setattr(client_pools, "_LAST_ATTEMPT", None)


def test_admin_edits_anything():
    assert can_edit_company(ADMIN, None, []) is True


@pytest.mark.parametrize(
    "owners",
    [
        [_owner(assigned=A)],
        [_owner(tax=A)],
        [_owner(created=A)],
        [_owner(assigned=OTHER), _owner(tax=A.upper())],
    ],
)
def test_staff_managing_a_linked_client_edits(owners):
    assert can_edit_company(_user(), OTHER, owners) is True


def test_company_creator_edits_without_links():
    assert can_edit_company(_user(), f" {A.upper()} ", []) is True


def test_pool_peer_owning_a_linked_client_edits_only_when_validated(pool_ab, monkeypatch):
    assert can_edit_company(_user(A), OTHER, [_owner(assigned=B)]) is True
    monkeypatch.setattr(client_pools, "_VALIDATED", None)
    assert can_edit_company(_user(A), OTHER, [_owner(assigned=B)]) is False


def test_unrelated_staff_and_orphan_company_are_refused():
    assert can_edit_company(_user(), OTHER, [_owner(assigned=B, created=OTHER)]) is False
    assert can_edit_company(_user(), OTHER, []) is False


def test_machine_role_never_edits_even_when_it_is_the_owner(pool_ab):
    assert can_edit_company(_user(A, "internal"), A, [_owner(assigned=A)]) is False


def test_blank_owners_are_ignored():
    assert can_edit_company(_user(""), "", [_owner(assigned=None, created="  ", tax="")]) is False


def test_caller_in_client_pool_and_can_create_company(pool_ab, monkeypatch):
    assert caller_in_client_pool(_user(A, "Reception")) is True
    assert can_create_company(_user(A, "Reception")) is True
    assert can_create_company(_user(A, "Tax Consultant")) is True
    assert caller_in_client_pool(_user(OTHER, "Reception")) is False
    assert can_create_company(_user(OTHER, "Reception")) is False
    assert caller_in_client_pool(_user(A, "internal")) is False
    assert can_create_company(_user(A, "internal")) is False
    monkeypatch.setattr(client_pools, "_VALIDATED", ())
    assert can_create_company(_user(A, "Reception")) is False


def _client(user: dict, *, company, owners, update_row=None):
    pool = MagicMock()
    conn = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)

    async def fetchrow(sql, *args):
        if sql.startswith("SELECT created_by"):
            return company
        return update_row

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(return_value=owners)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_database_pool] = lambda: pool
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), conn


def _updates(conn) -> list:
    return [c for c in conn.fetchrow.await_args_list if c.args[0].startswith("UPDATE")]


def _patch(tc):
    return tc.patch("/api/crm/companies/1", json={"company_name": "Renamed PT"})


def test_router_managing_staff_runs_the_update():
    tc, conn = _client(
        _user(),
        company={"created_by": OTHER},
        owners=[_owner(assigned=A)],
        update_row={"id": 1, "company_name": "Renamed PT"},
    )
    with tc:
        resp = _patch(tc)
    assert resp.status_code == 200, resp.text
    assert len(_updates(conn)) == 1


def test_router_unrelated_staff_gets_403_and_no_update():
    tc, conn = _client(_user(), company={"created_by": OTHER}, owners=[_owner(assigned=B)])
    with tc:
        resp = _patch(tc)
    assert resp.status_code == 403
    assert _updates(conn) == []


def test_router_missing_company_is_404_for_staff():
    tc, conn = _client(_user(), company=None, owners=[])
    with tc:
        resp = _patch(tc)
    assert resp.status_code == 404
    assert _updates(conn) == []


def test_router_admin_skips_the_ownership_lookup():
    tc, conn = _client(
        ADMIN, company=None, owners=[], update_row={"id": 1, "company_name": "Renamed PT"}
    )
    with tc:
        resp = _patch(tc)
    assert resp.status_code == 200
    conn.fetch.assert_not_awaited()
    assert len(_updates(conn)) == 1


def test_router_delete_stays_admin_only():
    tc, conn = _client(_user(), company={"created_by": A}, owners=[_owner(assigned=A)])
    with tc:
        resp = tc.delete("/api/crm/companies/1")
    assert resp.status_code == 403
    conn.execute.assert_not_awaited()


def test_admin_identity_unchanged_from_base():
    # Mirrors `_require_crm_admin` on base: any identity is_crm_admin accepts (the admin API
    # key principal included) could PATCH before this rule and still can.
    tc, conn = _client(
        {"role": "admin", "email": "admin@internal"},
        company=None,
        owners=[],
        update_row={"id": 1, "company_name": "Renamed PT"},
    )
    with tc:
        resp = _patch(tc)
    assert resp.status_code == 200
    assert len(_updates(conn)) == 1


def test_router_unrelated_staff_with_empty_payload_gets_403_not_400():
    tc, conn = _client(_user(), company={"created_by": OTHER}, owners=[_owner(assigned=B)])
    with tc:
        resp = tc.patch("/api/crm/companies/1", json={})
    assert resp.status_code == 403
    assert _updates(conn) == []


def test_router_allowed_staff_with_empty_payload_gets_400():
    tc, conn = _client(_user(), company={"created_by": OTHER}, owners=[_owner(assigned=A)])
    with tc:
        resp = tc.patch("/api/crm/companies/1", json={})
    assert resp.status_code == 400
    assert _updates(conn) == []


def test_router_owners_query_excludes_deleted_clients():
    tc, conn = _client(_user(), company={"created_by": OTHER}, owners=[_owner(assigned=A)])
    with tc:
        _patch(tc)
    sqls = [c.args[0] for c in conn.fetch.await_args_list]
    assert len(sqls) == 1
    assert "c.deleted_at IS NULL" in sqls[0]
