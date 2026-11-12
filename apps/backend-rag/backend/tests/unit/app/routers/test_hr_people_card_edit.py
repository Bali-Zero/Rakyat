"""PATCH /api/hr/people/{id}/card (STAFF_CARD.md C11): authz, validation, no value echoed."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr_people
from backend.app.services.hr.hr_service import HRCardCode, HRCardError
from backend.tests.unit.app.routers.test_hr_my_overview import CARD_ROW, ROWS

# Synthetic data only.
OWNER = {"user_id": "tm-owner", "email": "zero@balizero.com", "role": "member"}
ASYA = {"user_id": "tm-asya", "email": "asya@balizero.com", "role": "member"}
ADIT = {"user_id": "tm-adit", "email": "adit@balizero.com", "role": "member"}
SCOPED = {"user_id": "tm-scoped", "email": "ruslana@balizero.com", "role": "member"}
MEMBER = {"user_id": "tm-m", "email": "krisna@balizero.com", "role": "member"}
CLIENT = {"user_id": "tm-c", "email": "zero@balizero.com", "role": "client"}

ID_IN = "11111111-1111-4111-8111-111111111111"
ID_NONE = "22222222-2222-4222-8222-222222222222"
URL = f"/api/hr/people/{ID_IN}/card"
NIK = "3171234567890001"  # synthetic 16 digits
BAD_NIK = "9999-not-a-nik-9999"


def _hr_service(row_id: int | None = 7, error: HRCardCode | None = None) -> AsyncMock:
    s = AsyncMock()
    if error:
        s.edit_card = AsyncMock(side_effect=HRCardError(error))
    else:
        s.edit_card = AsyncMock(return_value=row_id)
    s.get_card_for_member = AsyncMock(side_effect=lambda i: dict(CARD_ROW, team_member_id=i))
    s.get_my_overview_rows = AsyncMock(return_value=ROWS)
    return s


def _people() -> MagicMock:
    s = MagicMock()
    s.member_email = AsyncMock(side_effect=lambda i: "dea@balizero.com" if i == ID_IN else None)
    return s


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(user: dict[str, Any], hr: Any = None) -> tuple[TestClient, Any]:
        hr = hr or _hr_service()
        app = FastAPI()
        app.include_router(hr_people.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        for name, val in (("_get_people_service", _people()), ("_get_hr_service", hr)):
            p = patch.object(hr_people, name, return_value=val)
            p.start()
            patchers.append(p)
        return TestClient(app), hr

    yield _make
    for p in patchers:
        p.stop()


@pytest.mark.parametrize("user", [OWNER, ASYA])
def test_the_global_admins_may_write(make_client, user):
    client, hr = make_client(user)
    res = client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}})
    assert res.status_code == 200
    assert res.json()["viewer_team_member_id"] == user["user_id"]
    assert res.json()["person"]["card"]["employee_id"] == CARD_ROW["id"]
    hr.edit_card.assert_awaited_once_with(
        ID_IN, "dea@balizero.com", {"bank_name": "Bank Contoh"}, {}, []
    )


@pytest.mark.parametrize("user", [ADIT, SCOPED, MEMBER, CLIENT])
def test_everyone_else_is_refused_before_any_work(make_client, user):
    client, hr = make_client(user)
    res = client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}})
    assert res.status_code == 403
    hr.edit_card.assert_not_awaited()


def test_a_person_who_is_not_visible_is_404(make_client):
    client, hr = make_client(OWNER)
    res = client.patch(
        f"/api/hr/people/{ID_NONE}/card", json={"card": {"bank_name": "Bank Contoh"}}
    )
    assert res.status_code == 404
    hr.edit_card.assert_not_awaited()


def test_a_path_that_is_not_a_uuid_is_422(make_client):
    client, _ = make_client(OWNER)
    assert (
        client.patch("/api/hr/people/not-a-uuid/card", json={"clear": ["unit"]}).status_code == 422
    )


def test_set_clear_and_nested_keys_reach_the_service_split(make_client):
    client, hr = make_client(OWNER)
    body = {
        "card": {
            "npwp": "12.345.678.9-012.345",
            "profile": {"nik": NIK, "contract": {"number": "C-1"}, "birth_date": "1990-02-03"},
        },
        "clear": ["address", "end_date", "bank_account"],
    }
    assert client.patch(URL, json=body).status_code == 200
    args = hr.edit_card.await_args.args
    assert args[2] == {"npwp": "12.345.678.9-012.345"}
    assert args[3] == {"nik": NIK, "birth_date": "1990-02-03", "contract": {"number": "C-1"}}
    assert args[4] == ["address", "bank_account", "end_date"]


def test_clearing_contract_number_is_expressible(make_client):
    client, hr = make_client(OWNER)
    assert client.patch(URL, json={"clear": ["contract_number"]}).status_code == 200
    assert hr.edit_card.await_args.args[2:] == ({}, {}, ["contract_number"])


@pytest.mark.parametrize(
    "body,field",
    [
        ({"card": {"unit": "ops"}, "clear": ["unit"]}, ""),  # set and cleared
        ({"card": {"profile": {"unit": "ops"}}, "clear": ["unit"]}, ""),
        ({"card": {"profile": {"nik": BAD_NIK}}}, "nik"),
        ({"card": {"npwp": BAD_NIK}}, "npwp"),
        ({"card": {"ptkp_status": "XX/9"}}, "ptkp_status"),
        ({"card": {"hire_date": "not-a-date"}}, "hire_date"),
        ({"card": {"mystery": "x"}}, ""),  # unknown key
        ({"card": {"profile": {"mystery": "x"}}}, ""),
        ({"mystery": 1}, ""),
        ({"clear": ["hire_date"]}, "clear"),  # not clearable
        ({"clear": ["is_active"]}, "clear"),
        ({"clear": ["ptkp_status"]}, "clear"),
        ({"clear": ["docs"]}, "clear"),
        ({"card": {"unit": None}}, ""),  # a null sets nothing
        ({"card": {"profile": {"docs": {"ktp": "f1"}}}}, ""),  # documents are the upload PR's
        ({"card": {"profile": {"contract": {"file_id": "f1"}}}}, ""),
        ({"card": {}}, ""),  # nothing to change
        ({"clear": ["unit", "unit"]}, ""),
    ],
)
def test_invalid_bodies_are_422_and_nothing_is_written(make_client, body, field):
    client, hr = make_client(OWNER)
    res = client.patch(URL, json=body)
    assert res.status_code == 422
    errors = res.json()["detail"]
    assert errors and all(set(e) == {"field", "code", "message"} for e in errors)
    if field:
        assert field in {e["field"] for e in errors}
    hr.edit_card.assert_not_awaited()


def test_a_422_never_echoes_a_submitted_value_or_key(make_client):
    client, _ = make_client(OWNER)
    body = {
        "card": {"profile": {"nik": BAD_NIK, "invented_key_xyz": "secret-value-1"}},
        "clear": ["unit"],
    }
    res = client.patch(URL, json=body)
    assert res.status_code == 422
    text = res.text
    for leaked in (BAD_NIK, "invented_key_xyz", "secret-value-1"):
        assert leaked not in text
    res = client.patch(
        URL,
        content=b'{"card": {"nik": "' + BAD_NIK.encode(),
        headers={"content-type": "application/json"},
    )
    assert res.status_code == 422 and BAD_NIK not in res.text


def test_a_record_that_needs_a_hire_date_is_422_hire_date_required(make_client):
    client, _ = make_client(OWNER, _hr_service(error=HRCardCode.HIRE_DATE_REQUIRED))
    res = client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}})
    assert res.status_code == 422
    assert res.json()["detail"][0]["code"] == "hire_date_required"
    assert res.json()["detail"][0]["field"] == "hire_date"


def test_create_with_a_hire_date_is_sent_through(make_client):
    client, hr = make_client(OWNER)
    assert client.patch(URL, json={"card": {"hire_date": "2025-03-01"}}).status_code == 200
    assert str(hr.edit_card.await_args.args[2]["hire_date"]) == "2025-03-01"


@pytest.mark.parametrize(
    "code,status",
    [
        (HRCardCode.LOCK_TIMEOUT, 409),
        (HRCardCode.DB_ERROR, 500),
        (HRCardCode.PROFILE_NOT_OBJECT, 500),
    ],
)
def test_service_failures_are_fixed_texts(make_client, code, status):
    client, _ = make_client(OWNER, _hr_service(error=code))
    res = client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}})
    assert res.status_code == status
    assert code.value not in res.text or status == 422


def test_a_member_that_changed_under_the_write_is_404(make_client):
    client, _ = make_client(OWNER, _hr_service(row_id=None))
    assert client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}}).status_code == 404


def test_the_log_names_actor_target_and_keys_but_never_a_value(make_client, caplog):
    client, _ = make_client(OWNER)
    with caplog.at_level(logging.DEBUG):
        res = client.patch(
            URL,
            json={
                "card": {"profile": {"nik": NIK}, "bank_name": "Bank Rahasia"},
                "clear": ["address"],
            },
        )
    assert res.status_code == 200
    text = caplog.text
    assert "tm-owner" in text and ID_IN in text and "nik" in text and "address" in text
    assert NIK not in text and "Bank Rahasia" not in text
