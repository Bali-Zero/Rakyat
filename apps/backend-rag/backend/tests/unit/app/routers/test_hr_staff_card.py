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
from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService

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
    service.patch_card = AsyncMock(side_effect=_patch_card)
    return service


async def _patch_card(employee_id, fields, profile_patch, view):
    return view(FAKE_ROW)


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
    assert service.patch_card.await_args.args[:3] == (
        7,
        {"ptkp_status": "K/1"},
        {"unit": "fake unit", "docs": {"npwp": "f-2"}},
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
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    result = await HRService(pool).merge_card("tm-fake-7", {"npwp": "N-1"}, {"unit": "u"})
    assert result == 7
    sql = conn.fetchrow.await_args.args[0]
    params = conn.fetchrow.await_args.args[1:]
    assert "npwp = COALESCE($3, npwp)" in sql
    assert "is_active = COALESCE($10, is_active)" in sql
    assert "jsonb_typeof(profile) = 'object'" in sql and "|| $11::jsonb" in sql
    assert "base_salary_idr" not in sql and "notes" not in sql
    assert params[0] == "tm-fake-7" and params[2] == "N-1"
    assert params[1] is None and params[3] is None  # omitted columns are NULL -> kept by COALESCE
    assert params[10] == {"unit": "u"}  # a dict: the codec encodes it, never a pre-dumped string
    assert conn.fetchrow.await_count == 1  # row existed: no INSERT


@pytest.mark.asyncio
async def test_merge_card_inserts_for_a_member_without_a_row():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": 9, "profile_type": "object"}])
    result = await HRService(pool).merge_card("tm-fake-9", {"hire_date": date(2025, 2, 1)}, {})
    assert result == 9
    insert_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "NOT EXISTS" in insert_sql and "SELECT tm.id, $2, 0" in insert_sql


@pytest.mark.asyncio
async def test_merge_card_without_row_and_without_hire_date_is_refused():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(HRCardError) as exc:
        await HRService(pool).merge_card("tm-fake-9", {}, {})
    assert exc.value.code is HRCardCode.HIRE_DATE_REQUIRED


@pytest.mark.asyncio
async def test_card_queries_use_an_explicit_column_list():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value=FAKE_ROW)
    card = await HRService(pool).get_card_by_email("fake.member@balizero.com")
    sql = conn.fetchrow.await_args.args[0]
    assert "e.*" not in sql
    assert "LOWER(tm.email) = LOWER($1)" in sql
    assert card is not None and card["profile"]["legal_name"] == "Fake Legal Name"
    conn.fetchrow = AsyncMock(return_value=None)
    assert await HRService(pool).get_card_by_email("nobody@balizero.com") is None


# ─── the pre-existing employee read: HR notes are admin-only ───


def _employee_service() -> AsyncMock:
    service = AsyncMock()
    service.get_employee_by_email = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    service.get_employee = AsyncMock(
        return_value={"id": 7, "full_name": "Fake Member", "notes": "fake hr note"}
    )
    return service


def test_member_self_read_of_the_employee_row_has_no_notes(make_client):
    res = make_client(MEMBER, _employee_service()).get("/api/hr/employees/7")
    assert res.status_code == 200
    assert "notes" not in res.json()
    assert res.json()["full_name"] == "Fake Member"


def test_admin_read_of_the_employee_row_still_has_notes(make_client):
    res = make_client(ADMIN, _employee_service()).get("/api/hr/employees/7")
    assert res.status_code == 200
    assert res.json()["notes"] == "fake hr note"


# ─── strict input, no echo, no driver message ───


def test_lone_surrogate_is_422_and_the_value_never_comes_back_or_gets_logged(make_client, caplog):
    service = _service()
    with caplog.at_level("DEBUG"):
        res = make_client(ADMIN, service).patch(
            "/api/hr/employees/7/card",
            content='{"bank_name": "ZQXLEAK\\ud800", "profile": {"legal_name": "ZQXLEAK\\ud800"}}',
            headers={"content-type": "application/json"},
        )
    assert res.status_code == 422
    body = res.text
    assert "input" not in res.json()["detail"][0]
    assert "ZQXLEAK" not in body
    assert all("ZQXLEAK" not in r.getMessage() for r in caplog.records)
    service.patch_card.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"npwp": "12ab"},
        {"bank_account": "1234;DROP"},
        {"profile": {"nik": "x" * 40}},
        {"bank_name": "line\nbreak"},
        {"bank_account_holder": "nul\x00byte"},
        {"profile": {"address": "a" * 501}},
        {"profile": {"docs": ["x"]}},
        {"profile": {"docs": "x"}},
        {"profile": {"docs": {"bad slot!": "f"}}},
        {"profile": {"contract": ["x"]}},
        {"profile": {"contract": {"number": "ok", "extra": "no"}}},
    ],
)
def test_malformed_card_input_is_422_without_echo(make_client, payload):
    res = make_client(ADMIN, _service()).patch("/api/hr/employees/7/card", json=payload)
    assert res.status_code == 422
    assert all(set(e) <= {"type", "loc", "msg"} for e in res.json()["detail"])


def test_a_database_error_becomes_a_fixed_500_without_driver_text(make_client):
    service = _service()
    service.patch_card = AsyncMock(side_effect=HRCardError(HRCardCode.DB_ERROR))
    res = make_client(ADMIN, service).patch("/api/hr/employees/7/card", json={"bank_name": "ok"})
    assert res.status_code == 500
    assert res.json()["detail"] == "Card operation failed"


@pytest.mark.asyncio
async def test_merge_card_replaces_a_driver_error_by_a_code_without_its_message():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(side_effect=ValueError("driver quotes SECRET-VALUE here"))
    with pytest.raises(HRCardError) as exc:
        await HRService(pool).merge_card("tm-fake-7", {"bank_name": "x"}, {})
    assert exc.value.code is HRCardCode.DB_ERROR
    assert "SECRET" not in str(exc.value) and exc.value.__suppress_context__


@pytest.mark.asyncio
async def test_merge_card_accepts_an_iso_hire_date_string_as_a_date():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    await HRService(pool).merge_card("tm-fake-7", {"hire_date": "2025-03-01"}, {})
    assert conn.fetchrow.await_args.args[2] == date(2025, 3, 1)


def test_the_card_response_cannot_fail_on_odd_stored_json(make_client):
    odd = {**FAKE_ROW, "profile": {"docs": ["x"], "contract": "c", "birth_date": 5, "nik": 7}}
    res = make_client(ADMIN, _service(card=odd)).get("/api/hr/employees/7/card")
    assert res.status_code == 200
    body = res.json()
    assert body["docs"] == {} and body["contract"] is None and body["nik"] is None


# ─── S1a: a 422 carries no caller-controlled text (defects i, ii) ───

SENTINEL = "ZQXSENTINEL"


def test_422_loc_hides_an_extra_key_name(make_client):
    res = make_client(ADMIN, _service()).patch(
        "/api/hr/employees/7/card", json={f"{SENTINEL}_extra": "v", "profile": {SENTINEL: 1}}
    )
    assert res.status_code == 422
    assert SENTINEL not in res.text
    locs = [e["loc"] for e in res.json()["detail"]]
    assert ["body", "<key>"] in locs and ["body", "profile", "<key>"] in locs
    assert all(set(e) == {"type", "loc", "msg"} for e in res.json()["detail"])


def test_422_loc_hides_a_docs_slot_name_with_an_invalid_value(make_client):
    res = make_client(ADMIN, _service()).patch(
        "/api/hr/employees/7/card", json={"profile": {"docs": {f"{SENTINEL}-slot": 12345}}}
    )
    assert res.status_code == 422
    assert SENTINEL not in res.text and "12345" not in res.text
    assert all(seg != f"{SENTINEL}-slot" for e in res.json()["detail"] for seg in e["loc"])


def test_422_loc_keeps_declared_field_names(make_client):
    res = make_client(ADMIN, _service()).patch(
        "/api/hr/employees/7/card", json={"profile": {"contract": {"number": 5}}}
    )
    assert res.json()["detail"][0]["loc"] == ["body", "profile", "contract", "number"]


def test_post_employees_422_does_not_echo_personal_values(make_client):
    service = _service()
    service.upsert_employee = AsyncMock()
    res = make_client(ADMIN, service).post(
        "/api/hr/employees",
        json={
            "team_member_id": "tm-1",
            "hire_date": f"{SENTINEL}-date",
            "base_salary_idr": -5,
            "bank_account": {"x": f"{SENTINEL}-acct"},
            "npwp": ["a"],
            SENTINEL: "extra",
        },
    )
    assert res.status_code == 422
    assert SENTINEL not in res.text and "-5" not in res.text
    assert all(set(e) == {"type", "loc", "msg"} for e in res.json()["detail"])
    service.upsert_employee.assert_not_awaited()


def test_post_employees_db_error_is_a_fixed_500(make_client):
    service = _service()
    service.upsert_employee = AsyncMock(side_effect=HRCardError(HRCardCode.DB_ERROR))
    res = make_client(ADMIN, service).post(
        "/api/hr/employees",
        json={"team_member_id": "t", "hire_date": "2025-01-01", "base_salary_idr": 1},
    )
    assert res.status_code == 500 and res.json()["detail"] == "Card operation failed"


# ─── S2: reads never validate (defects iv, v) ───


def test_get_card_with_a_newline_in_the_stored_contract_number_is_200(make_client, caplog):
    odd = {**FAKE_ROW, "profile": {"contract": {"number": f"{SENTINEL}\nline2", "file_id": "f\t1"}}}
    with caplog.at_level("DEBUG"):
        member = make_client(MEMBER, _service(card=odd)).get("/api/hr/me/card")
        admin = make_client(ADMIN, _service(card=odd)).get("/api/hr/employees/7/card")
    assert member.status_code == 200 and admin.status_code == 200
    assert member.json()["contract"]["number"] == f"{SENTINEL}\nline2"
    assert all(SENTINEL not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("stored", ["x", 7, [1], None, True])
def test_get_card_with_a_non_object_profile_is_200(make_client, stored):
    res = make_client(MEMBER, _service(card={**FAKE_ROW, "profile": stored})).get("/api/hr/me/card")
    assert res.status_code == 200
    body = res.json()
    assert body["docs"] == {} and body["contract"] is None and body["legal_name"] is None
    assert body["full_name"] == "Fake Member"


def test_get_card_with_a_json_string_profile_is_200_without_decode_errors(make_client):
    res = make_client(MEMBER, _service(card={**FAKE_ROW, "profile": '"x"'})).get("/api/hr/me/card")
    assert res.status_code == 200 and res.json()["nik"] is None


def test_get_card_with_odd_stored_strings_shows_them_as_stored(make_client):
    odd = {
        **FAKE_ROW,
        "profile": {"legal_name": "a​b\x07c", "address": "l1\nl2", "birth_date": "nope"},
    }
    body = make_client(MEMBER, _service(card=odd)).get("/api/hr/me/card").json()
    assert body["legal_name"] == "a​b\x07c" and body["address"] == "l1\nl2"
    assert body["birth_date"] is None


# ─── S7: real data is accepted (defect vii) ───


def test_multiline_address_is_accepted_and_other_controls_are_not(make_client):
    client = make_client(ADMIN, _service())
    ok = client.patch(
        "/api/hr/employees/7/card",
        json={"profile": {"address": "Jl. Raya Anyar No. 2/4,\nBanjar Dinas, Bali\r\nID"}},
    )
    assert ok.status_code == 200
    bad = client.patch("/api/hr/employees/7/card", json={"profile": {"address": "a\tb"}})
    assert bad.status_code == 422
    bad = client.patch("/api/hr/employees/7/card", json={"profile": {"legal_name": "a\nb"}})
    assert bad.status_code == 422


def test_multiline_address_reaches_the_service_normalised(make_client):
    service = _service()
    make_client(ADMIN, service).patch(
        "/api/hr/employees/7/card", json={"profile": {"address": "L1,\r\nL2/3"}}
    )
    assert service.patch_card.await_args.args[2] == {"address": "L1,\nL2/3"}


def test_real_data_shapes_are_accepted(make_client):
    service = _service()
    body = {
        "npwp": "01.234.567.8-901.000",
        "bank_name": "Bank Central Asia Tbk",
        "bank_account": "123-456 7890",
        "bank_account_holder": "I Gusti Ngurah O'Brien-Smith Jr.",
        "bpjs_kesehatan_number": "0001234567890",
        "profile": {"nik": "5171 0000 0000 0001", "legal_name": "Ni Luh D'Arc-Putri, S.H."},
    }
    with patch.object(hr_router, "invalidate_cache", new=AsyncMock()):
        assert (
            make_client(ADMIN, service).patch("/api/hr/employees/7/card", json=body).status_code
            == 200
        )
        for npwp in ("0123456789012345", "012345678901234", "01.234.567.8-901.000"):
            res = make_client(ADMIN, service).patch("/api/hr/employees/7/card", json={"npwp": npwp})
            assert res.status_code == 200


# ─── S1c: codes map to fixed statuses ───


@pytest.mark.parametrize(
    "code,status",
    [
        (HRCardCode.HIRE_DATE_REQUIRED, 422),
        (HRCardCode.PROFILE_NOT_OBJECT, 422),
        (HRCardCode.NOT_APPLIED, 409),
        (HRCardCode.DB_ERROR, 500),
    ],
)
def test_every_card_code_maps_to_a_fixed_status(make_client, code, status):
    service = _service()
    service.patch_card = AsyncMock(side_effect=HRCardError(code))
    res = make_client(ADMIN, service).patch("/api/hr/employees/7/card", json={"is_active": True})
    assert res.status_code == status
    assert code.value not in res.json()["detail"] or code is HRCardCode.HIRE_DATE_REQUIRED


def test_get_routes_map_a_db_failure_to_a_fixed_500(make_client):
    service = _service()
    service.get_card_by_email = AsyncMock(side_effect=HRCardError(HRCardCode.DB_ERROR))
    res = make_client(MEMBER, service).get("/api/hr/me/card")
    assert res.status_code == 500 and res.json()["detail"] == "Card operation failed"
