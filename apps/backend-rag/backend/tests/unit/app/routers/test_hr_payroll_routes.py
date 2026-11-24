"""Payroll calculate and payslip read routes (synthetic data only)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router

ADMIN = {"user_id": "tm-admin", "email": "Asya@balizero.com", "role": "member"}
MEMBER = {"user_id": "tm-fake-7", "email": "fake.member@example.test", "role": "member"}
CALC = {"month": 11, "year": 2026}


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(user: dict[str, Any], service: Any) -> TestClient:
        app = FastAPI()
        app.include_router(hr_router.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        for target, kw in (
            ("_get_hr_service", {"return_value": service}),
            ("invalidate_cache", {"new": AsyncMock()}),
        ):
            p = patch.object(hr_router, target, **kw)
            p.start()
            patchers.append(p)
        return TestClient(app)

    yield _make
    for p in reversed(patchers):
        p.stop()


# ─── calculate on a closed period ────────────────────────────────────────


@pytest.mark.parametrize(
    "trigger_text",
    [
        "Payroll period 4 is paid and immutable",
        "Approved period 4 only allows transition to paid",
    ],
)
def test_calculating_a_closed_period_is_a_fixed_409(make_client, trigger_text):
    svc = AsyncMock()
    svc.calculate_payroll = AsyncMock(side_effect=asyncpg.exceptions.RaiseError(trigger_text))
    res = make_client(ADMIN, svc).post("/api/hr/payroll/calculate", json=CALC)
    assert res.status_code == 409
    assert res.json() == {"detail": "This payroll period is closed"}
    assert trigger_text not in res.text


def test_calculating_an_open_period_answers_as_before(make_client):
    svc = AsyncMock()
    svc.calculate_payroll = AsyncMock(return_value={"period": {"id": 4}})
    res = make_client(ADMIN, svc).post("/api/hr/payroll/calculate", json=CALC)
    assert res.status_code == 200
    assert res.json() == {"success": True, "period": {"id": 4}}


def test_an_unrelated_service_error_is_not_turned_into_a_409(make_client):
    svc = AsyncMock()
    svc.calculate_payroll = AsyncMock(
        side_effect=asyncpg.exceptions.RaiseError("Practice 9 must be completed")
    )
    with pytest.raises(asyncpg.exceptions.RaiseError):
        make_client(ADMIN, svc).post("/api/hr/payroll/calculate", json=CALC)


# ─── payslips of a period that is not yet approved ───────────────────────


def _slip(slip_id: int, status: str, employee_id: int = 7) -> dict[str, Any]:
    return {"id": slip_id, "employee_id": employee_id, "period_status": status}


def _payslip_service(slips: list[dict[str, Any]]) -> AsyncMock:
    svc = AsyncMock()
    svc.get_employee_by_email = AsyncMock(return_value={"id": 7})
    svc.is_on_payroll = AsyncMock(return_value=True)
    svc.get_payslips = AsyncMock(return_value=slips)
    by_id = {s["id"]: s for s in slips}
    svc.get_payslip_detail = AsyncMock(side_effect=lambda i: by_id.get(i))
    return svc


FOUR = [_slip(1, "draft"), _slip(2, "calculated"), _slip(3, "approved"), _slip(4, "paid")]


def test_a_member_list_leaves_out_payslips_of_unapproved_periods(make_client):
    body = make_client(MEMBER, _payslip_service(FOUR)).get("/api/hr/payslips").json()
    assert [s["id"] for s in body["payslips"]] == [3, 4]
    assert body["count"] == 2


def test_an_hr_admin_list_has_all_four(make_client):
    body = make_client(ADMIN, _payslip_service(FOUR)).get("/api/hr/payslips").json()
    assert [s["id"] for s in body["payslips"]] == [1, 2, 3, 4]


@pytest.mark.parametrize("slip_id", [1, 2])
def test_a_member_reading_an_unapproved_payslip_gets_the_not_found_of_a_missing_one(
    make_client, slip_id
):
    client = make_client(MEMBER, _payslip_service(FOUR))
    missing = client.get("/api/hr/payslips/999")
    res = client.get(f"/api/hr/payslips/{slip_id}")
    assert res.status_code == 404
    assert (res.status_code, res.json()) == (missing.status_code, missing.json())


def test_a_member_cannot_tell_a_colleagues_unapproved_payslip_from_a_missing_one(make_client):
    svc = _payslip_service([_slip(5, "draft", employee_id=8)])
    client = make_client(MEMBER, svc)
    assert client.get("/api/hr/payslips/5").json() == client.get("/api/hr/payslips/999").json()


@pytest.mark.parametrize("slip_id", [3, 4])
def test_a_member_still_reads_payslips_of_approved_and_paid_periods(make_client, slip_id):
    res = make_client(MEMBER, _payslip_service(FOUR)).get(f"/api/hr/payslips/{slip_id}")
    assert res.status_code == 200 and res.json()["id"] == slip_id


@pytest.mark.parametrize("slip_id", [1, 2, 3, 4])
def test_an_hr_admin_reads_all_four_payslips(make_client, slip_id):
    res = make_client(ADMIN, _payslip_service(FOUR)).get(f"/api/hr/payslips/{slip_id}")
    assert res.status_code == 200


def test_a_member_still_gets_403_for_a_colleagues_approved_payslip(make_client):
    svc = _payslip_service([_slip(6, "approved", employee_id=8)])
    assert make_client(MEMBER, svc).get("/api/hr/payslips/6").status_code == 403
