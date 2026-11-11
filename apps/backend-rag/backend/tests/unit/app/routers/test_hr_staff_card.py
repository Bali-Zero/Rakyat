"""The staff HR card: owner-readable (masked), admin-editable (merge, never clobber)."""

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.services.hr.hr_service import HRService

ADMIN = {"email": "adit@balizero.com", "role": "member"}
MEMBER = {"email": "fake.member@balizero.com", "role": "member"}

# Synthetic data only.
FAKE_ROW: dict[str, Any] = {
    "id": 7,
    "team_member_id": "tm-fake-7",
    "hire_date": date(2025, 1, 15),
    "is_active": True,
    "ptkp_status": "TK/0",
    "npwp": "00.000.000.0-000.000",
    "bpjs_kesehatan_number": "0000000001234",
    "bpjs_ketenagakerjaan_number": None,
    "bank_name": "Fake Bank",
    "bank_account": "9999900001",
    "bank_account_holder": "FAKE MEMBER",
    "base_salary_idr": 1,
    "notes": "fake hr note",
    "profile": {
        "legal_name": "Fake Legal Name",
        "nik": "0000000000000042",
        "employer_entity": "PT Fake Entity",
        "docs": {"ktp": "file-1"},
        "address": "1 Fake Street",
    },
    "full_name": "Fake Member",
    "role": "staff",
    "department": "ops",
}


def _client(user: dict[str, Any], service: Any) -> TestClient:
    app = FastAPI()
    app.include_router(hr_router.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: MagicMock()
    patcher = patch.object(hr_router, "_get_hr_service", return_value=service)
    patcher.start()
    client = TestClient(app)
    client._patcher = patcher  # type: ignore[attr-defined]
    return client


@pytest.fixture
def make_client():
    clients: list[TestClient] = []

    def _make(user: dict[str, Any], service: Any) -> TestClient:
        c = _client(user, service)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c._patcher.stop()  # type: ignore[attr-defined]


def _service(card: dict[str, Any] | None = FAKE_ROW) -> AsyncMock:
    service = AsyncMock()
    service.get_card_by_email = AsyncMock(return_value=card)
    service.get_card_by_id = AsyncMock(return_value=card)
    service.merge_card = AsyncMock(return_value=7)
    return service


def test_member_gets_own_card_in_full_without_notes(make_client):
    service = _service()
    res = make_client(MEMBER, service).get("/api/hr/me/card")
    assert res.status_code == 200
    body = res.json()
    assert body["employee_id"] == 7
    assert body["nik"] == "0000000000000042"
    assert body["npwp"] == "00.000.000.0-000.000"
    assert body["bank_account"] == "9999900001"
    assert body["address"] == "1 Fake Street"
    assert body["base_salary_idr"] == 1
    assert "notes" not in body
    assert "fake hr note" not in json.dumps(body)
    service.get_card_by_email.assert_awaited_once_with(MEMBER["email"])


def test_member_without_hr_row_gets_404(make_client):
    res = make_client(MEMBER, _service(card=None)).get("/api/hr/me/card")
    assert res.status_code == 404


def test_non_admin_is_refused_on_the_admin_routes(make_client):
    client = make_client(MEMBER, _service())
    assert client.get("/api/hr/employees/7/card").status_code == 403
    assert client.patch("/api/hr/employees/7/card", json={"is_active": False}).status_code == 403


def test_admin_card_adds_the_hr_notes(make_client):
    res = make_client(ADMIN, _service()).get("/api/hr/employees/7/card")
    assert res.status_code == 200
    body = res.json()
    assert body["nik"] == "0000000000000042"
    assert body["notes"] == "fake hr note"


def test_patch_passes_only_given_fields_and_profile_keys(make_client):
    service = _service()
    with patch.object(hr_router, "invalidate_cache", new=AsyncMock()) as inval:
        res = make_client(ADMIN, service).patch(
            "/api/hr/employees/7/card",
            json={"ptkp_status": "K/1", "profile": {"unit": "fake unit", "docs": {"npwp": "f-2"}}},
        )
    assert res.status_code == 200
    service.merge_card.assert_awaited_once_with(
        "tm-fake-7", {"ptkp_status": "K/1"}, {"unit": "fake unit", "docs": {"npwp": "f-2"}}
    )
    inval.assert_awaited_once_with("zantara:hr_employees:*")


def test_unknown_profile_key_is_422(make_client):
    res = make_client(ADMIN, _service()).patch(
        "/api/hr/employees/7/card", json={"profile": {"salary": 1}}
    )
    assert res.status_code == 422


def test_invalid_ptkp_is_422(make_client):
    res = make_client(ADMIN, _service()).patch(
        "/api/hr/employees/7/card", json={"ptkp_status": "ZZ/9"}
    )
    assert res.status_code == 422


# ─── service: the SQL and params of the non-clobbering write ───


def _pool() -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


@pytest.mark.asyncio
async def test_merge_card_coalesces_columns_and_merges_profile():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 7})
    result = await HRService(pool).merge_card("tm-fake-7", {"npwp": "N-1"}, {"unit": "u"})
    assert result == 7
    sql = conn.fetchrow.await_args.args[0]
    params = conn.fetchrow.await_args.args[1:]
    assert "npwp = COALESCE($3, npwp)" in sql
    assert "is_active = COALESCE($10, is_active)" in sql
    assert "profile = profile || $11::jsonb" in sql
    assert "base_salary_idr" not in sql and "notes" not in sql
    assert params[0] == "tm-fake-7" and params[2] == "N-1"
    assert params[1] is None and params[3] is None  # omitted columns are NULL -> kept by COALESCE
    assert json.loads(params[10]) == {"unit": "u"}
    assert conn.fetchrow.await_count == 1  # row existed: no INSERT


@pytest.mark.asyncio
async def test_merge_card_inserts_for_a_member_without_a_row():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": 9}])
    result = await HRService(pool).merge_card("tm-fake-9", {"hire_date": date(2025, 2, 1)}, {})
    assert result == 9
    insert_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "NOT EXISTS" in insert_sql and "SELECT tm.id, $2, 0" in insert_sql


@pytest.mark.asyncio
async def test_merge_card_without_row_and_without_hire_date_is_refused():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(ValueError):
        await HRService(pool).merge_card("tm-fake-9", {}, {})


@pytest.mark.asyncio
async def test_card_queries_use_an_explicit_column_list():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={**FAKE_ROW, "profile": json.dumps(FAKE_ROW["profile"])})
    card = await HRService(pool).get_card_by_email("fake.member@balizero.com")
    sql = conn.fetchrow.await_args.args[0]
    assert "e.*" not in sql
    assert card is not None and card["profile"]["legal_name"] == "Fake Legal Name"
    conn.fetchrow = AsyncMock(return_value=None)
    assert await HRService(pool).get_card_by_email("nobody@balizero.com") is None
