"""GET /api/hr/me/card: one parameterless read of the caller's own card (STAFF_CARD.md)."""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.services.hr.hr_service import HRCardCode, HRCardError

# The session dict is the team_members row get_current_user validated: `id` + `email`.
ADMIN = {"id": "tm-admin", "email": "adit@balizero.com", "role": "member"}
MEMBER = {"id": "tm-fake-7", "email": "fake.member@balizero.com", "role": "member"}
CLIENT = {"id": "tm-client", "email": "fake.client@example.com", "role": "client"}

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
    "profile": {
        "legal_name": "Fake Legal Name",
        "nik": "0000000000000042",
        "employer_entity": "PT Fake Entity",
        "docs": {"ktp": "file-1", "kk": "file-2"},
        "doc_proof": {
            "ktp": {"id": "file-1", "by": "x@balizero.com", "at": "2026-11-12T00:00:00+00:00"}
        },
        "address": "1 Fake Street",
    },
    "full_name": "Fake Member",
    "role": "staff",
    "department": "ops",
}


@pytest.fixture
def it_admin_in_allowlist(monkeypatch):
    """A synthetic IT account that IS a global admin: it must still not write HR data."""
    from backend.app.core.config import settings

    monkeypatch.setattr(
        settings, "admin_emails", "zero@balizero.com,asya@balizero.com,it.dev@balizero.com"
    )
    assert "it.dev@balizero.com" in settings.admin_emails_set


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(user: dict[str, Any], service: Any) -> TestClient:
        app = FastAPI()
        app.include_router(hr_router.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        patcher = patch.object(hr_router, "_get_hr_service", return_value=service)
        patcher.start()
        patchers.append(patcher)
        return TestClient(app)

    yield _make
    for p in patchers:
        p.stop()


def _service(card: dict[str, Any] | None = FAKE_ROW) -> AsyncMock:
    service = AsyncMock()
    service.get_card_for_member = AsyncMock(return_value=card)
    return service


def test_member_gets_own_card_in_full_without_notes(make_client):
    res = make_client(MEMBER, _service()).get("/api/hr/me/card")
    assert res.status_code == 200
    body = res.json()
    assert body["employee_id"] == 7 and body["full_name"] == "Fake Member"
    assert body["npwp"] == "00.000.000.0-000.000" and body["base_salary_idr"] == 1
    assert body["nik"] == "0000000000000042"
    # R4: a verified slot shows its id, an unverified one is only "present", the rest is absent
    assert body["documents"]["ktp"] == {"present": True, "file_id": "file-1"}
    assert body["documents"]["kk"] == {"present": True, "file_id": None}
    assert body["documents"]["npwp"] == {"present": False, "file_id": None}
    assert set(body["documents"]) == {"ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract"}
    assert (
        "docs" not in body and "doc_proof" not in body and "file_id" not in (body["contract"] or {})
    )
    assert "notes" not in body and "team_member_id" not in body


def test_the_card_is_keyed_by_the_session_member_id_never_by_email(make_client):
    service = _service()
    make_client(MEMBER, service).get("/api/hr/me/card")
    service.get_card_for_member.assert_awaited_once_with("tm-fake-7")
    assert len(service.mock_calls) == 1  # nothing else on the service was touched


def test_a_client_session_is_refused_before_any_read(make_client):
    service = _service()
    res = make_client(CLIENT, service).get("/api/hr/me/card")
    assert res.status_code == 403
    service.get_card_for_member.assert_not_awaited()


def test_member_without_hr_row_gets_404(make_client):
    assert make_client(MEMBER, _service(card=None)).get("/api/hr/me/card").status_code == 404


def test_a_database_failure_is_a_fixed_500_without_driver_text(make_client):
    service = _service()
    service.get_card_for_member = AsyncMock(side_effect=HRCardError(HRCardCode.DB_ERROR))
    res = make_client(MEMBER, service).get("/api/hr/me/card")
    assert res.status_code == 500 and res.json() == {"detail": "Card operation failed"}


@pytest.mark.parametrize("stored", ["x", 7, [1], None, True])
def test_a_non_object_stored_profile_still_reads(make_client, stored):
    res = make_client(MEMBER, _service(card={**FAKE_ROW, "profile": stored})).get("/api/hr/me/card")
    body = res.json()
    assert (
        res.status_code == 200
        and all(not d["present"] for d in body["documents"].values())
        and body["contract"] is None
    )
    assert body["legal_name"] is None and body["full_name"] == "Fake Member"


def test_odd_stored_strings_and_values_read_as_stored(make_client):
    odd = {
        **FAKE_ROW,
        "profile": {
            "legal_name": "a​b\x07c",
            "address": "l1\nl2",
            "birth_date": "nope",
            "contract": {"number": "N\nX", "file_id": 3},
            "docs": ["x"],
            "nik": 7,
        },
    }
    body = make_client(MEMBER, _service(card=odd)).get("/api/hr/me/card").json()
    assert body["legal_name"] == "a​b\x07c" and body["address"] == "l1\nl2"
    assert (
        body["birth_date"] is None
        and body["nik"] is None
        and not body["documents"]["ktp"]["present"]
    )
    assert body["contract"] == {"number": "N\nX"}


def test_the_surface_added_is_one_get_without_path_or_query_parameters():
    routes = [r for r in hr_router.router.routes if "card" in getattr(r, "path", "")]
    assert [(r.path, sorted(r.methods)) for r in routes] == [("/api/hr/me/card", ["GET"])]
    route = routes[0]
    assert not route.dependant.path_params and not route.dependant.query_params
    assert not route.dependant.body_params
    assert not [r for r in hr_router.router.routes if "PATCH" in getattr(r, "methods", ())]


# ─── the legacy employee routes: HR notes admin-only, profile never served ───


def _employee_service() -> AsyncMock:
    service = AsyncMock()
    service.get_employee_by_email = AsyncMock(return_value={"id": 7})
    service.get_employee = AsyncMock(
        return_value={"id": 7, "full_name": "Fake Member", "notes": "fake hr note"}
    )
    return service


def test_member_self_read_of_the_employee_row_has_no_notes(make_client):
    res = make_client(MEMBER, _employee_service()).get("/api/hr/employees/7")
    assert res.status_code == 200 and "notes" not in res.json()
    assert res.json()["full_name"] == "Fake Member"


def test_admin_read_of_the_employee_row_still_has_notes(make_client):
    res = make_client(ADMIN, _employee_service()).get("/api/hr/employees/7")
    assert res.status_code == 200 and res.json()["notes"] == "fake hr note"


# ─── the legacy write door obeys the card-write rule (C11: two doors, one rule) ───

_EMPLOYEE = {"team_member_id": "tm-fake-7", "hire_date": "2025-01-15", "base_salary_idr": 1}


@pytest.mark.parametrize(
    "email,status",
    [
        ("zero@balizero.com", 200),
        ("asya@balizero.com", 200),
        ("adit@balizero.com", 403),  # HR extra admin: reads and bonuses, no card writes
        ("ruslana@balizero.com", 403),
        ("fake.member@balizero.com", 403),
        ("it.dev@balizero.com", 403),  # global admin, but not an HR writer (2026-11-13)
    ],
)
def test_post_employees_is_for_the_hr_writers_only(
    make_client, it_admin_in_allowlist, email, status
):
    service = AsyncMock()
    service.upsert_employee = AsyncMock(return_value={"id": 7})
    client = make_client({"id": "tm-x", "email": email, "role": "member"}, service)
    with patch.object(hr_router, "invalidate_cache", AsyncMock()):
        res = client.post("/api/hr/employees", json=_EMPLOYEE)
    assert res.status_code == status
    assert service.upsert_employee.await_count == (1 if status == 200 else 0)
