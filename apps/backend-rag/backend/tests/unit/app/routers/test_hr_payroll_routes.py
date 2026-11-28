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


# ─── bonus runs (PR3a): approve and mark paid ────────────────────────────────────────────────────

HR_ADMIN_ONLY = {"user_id": "tm-fake-9", "email": "adit@balizero.com", "role": "member"}
RUN_BODY = {"approved_total_idr": 300000, "pending_count": 2}
RUN_POSTS = (
    ("/api/hr/bonus-runs/2026-11/approve", RUN_BODY),
    ("/api/hr/bonus-runs/2026-11/mark-paid", None),
)


@pytest.mark.parametrize(("url", "body"), RUN_POSTS)
def test_an_hr_admin_without_payroll_write_gets_403_on_both_run_posts(make_client, url, body):
    svc = AsyncMock()
    res = make_client(HR_ADMIN_ONLY, svc).post(url, json=body)
    assert res.status_code == 403
    assert not svc.method_calls


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "2026-1", "nov-2026", "2026-11-01"])
@pytest.mark.parametrize("path", ["approve", "mark-paid"])
def test_a_bad_bonus_month_is_a_422(make_client, month, path):
    svc = AsyncMock()
    res = make_client(ADMIN, svc).post(f"/api/hr/bonus-runs/{month}/{path}", json=RUN_BODY)
    assert res.status_code == 422
    assert not svc.method_calls


def test_approving_a_run_passes_the_screen_figures_and_the_first_of_the_month(make_client):
    svc = AsyncMock()
    svc.approve_bonus_run = AsyncMock(
        return_value={"run": {"status": "approved"}, "reserved_count": 3}
    )
    res = make_client(ADMIN, svc).post("/api/hr/bonus-runs/2026-11/approve", json=RUN_BODY)
    assert res.status_code == 200
    assert res.json() == {"success": True, "run": {"status": "approved"}, "reserved_count": 3}
    from datetime import date

    svc.approve_bonus_run.assert_awaited_once_with(
        date(2026, 11, 1), "tm-admin", expected_approved_idr=300000, expected_pending=2
    )


@pytest.mark.parametrize("code", ["not_due", "out_of_order", "closed", "changed", "not_approved"])
@pytest.mark.parametrize(("url", "body"), RUN_POSTS)
def test_each_refusal_code_is_a_409_carrying_its_code(make_client, code, url, body):
    from backend.app.services.hr.hr_service import BonusRunRefusedError

    err = BonusRunRefusedError(code, "fake refusal")
    svc = AsyncMock()
    svc.approve_bonus_run = AsyncMock(side_effect=err)
    svc.mark_bonus_run_paid = AsyncMock(side_effect=err)
    res = make_client(ADMIN, svc).post(url, json=body)
    assert res.status_code == 409
    assert res.json() == {"detail": "fake refusal", "code": f"bonus_run_{code}"}


def test_paying_an_unknown_run_is_a_404_and_paying_a_known_one_a_200(make_client):
    svc = AsyncMock()
    svc.mark_bonus_run_paid = AsyncMock(side_effect=LookupError("No bonus run for 2026-11"))
    client = make_client(ADMIN, svc)
    assert client.post("/api/hr/bonus-runs/2026-11/mark-paid").status_code == 404
    svc.mark_bonus_run_paid = AsyncMock(return_value={"run": {"status": "paid"}, "paid_count": 1})
    res = client.post("/api/hr/bonus-runs/2026-11/mark-paid")
    assert (res.status_code, res.json()["paid_count"]) == (200, 1)
    hr_router.invalidate_cache.assert_any_await("zantara:hr_bonuses:*")
    hr_router.invalidate_cache.assert_any_await("zantara:hr_payroll:*")
