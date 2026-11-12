"""Manual bonus, reject, delete and practice-type endpoints (synthetic data)."""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.services.hr.hr_service import BonusPeriodLockedError, PayrollBonusesChangedError

ADMIN = {"user_id": "tm-admin", "email": "Asya@balizero.com", "role": "member"}
MEMBER = {"user_id": "tm-fake-7", "email": "fake.member@balizero.com", "role": "member"}
GOOD = {"employee_id": 7, "amount_idr": 150000, "reason": "Weekend filing"}


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
        inv = patch.object(hr_router, "invalidate_cache", new=AsyncMock())
        inv.start()
        patchers.append(inv)
        return TestClient(app)

    yield _make
    for p in reversed(patchers):  # LIFO: patches of one attribute must unwind in order
        p.stop()


def test_create_manual_bonus_passes_caller_email_and_defaults_the_date(make_client):
    svc = AsyncMock()
    svc.create_manual_bonus = AsyncMock(return_value={"id": 5, "source": "manual"})
    res = make_client(ADMIN, svc).post("/api/hr/bonuses", json=GOOD)
    assert res.status_code == 200 and res.json()["bonus"]["id"] == 5
    kw = svc.create_manual_bonus.await_args.kwargs
    assert kw["created_by"] == "asya@balizero.com"
    assert isinstance(kw["awarded_on"], date)
    assert kw["practice_type_code"] is None


def test_create_manual_bonus_is_admin_only(make_client):
    svc = AsyncMock()
    assert make_client(MEMBER, svc).post("/api/hr/bonuses", json=GOOD).status_code == 403
    svc.create_manual_bonus.assert_not_awaited()


@pytest.mark.parametrize(
    "patch_body",
    [
        {"amount_idr": 0},
        {"amount_idr": -5},
        {"amount_idr": 100_000_001},
        {"reason": "ab"},
        {"reason": "   x  "},
        {"reason": "x" * 301},
        {"awarded_on": "not-a-date"},
    ],
)
def test_create_manual_bonus_validation(make_client, patch_body):
    svc = AsyncMock()
    res = make_client(ADMIN, svc).post("/api/hr/bonuses", json={**GOOD, **patch_body})
    assert res.status_code == 422
    svc.create_manual_bonus.assert_not_awaited()


def test_create_manual_bonus_boundaries_are_accepted(make_client):
    svc = AsyncMock()
    svc.create_manual_bonus = AsyncMock(return_value={"id": 1})
    body = {**GOOD, "amount_idr": 100_000_000, "reason": "abc"}
    assert make_client(ADMIN, svc).post("/api/hr/bonuses", json=body).status_code == 200


@pytest.mark.parametrize(
    "result, code",
    [
        (None, 404),
        (BonusPeriodLockedError("locked"), 409),
        (ValueError("Unknown or inactive service"), 400),
    ],
)
def test_create_manual_bonus_error_mapping(make_client, result, code):
    svc = AsyncMock()
    if isinstance(result, Exception):
        svc.create_manual_bonus = AsyncMock(side_effect=result)
    else:
        svc.create_manual_bonus = AsyncMock(return_value=result)
    assert make_client(ADMIN, svc).post("/api/hr/bonuses", json=GOOD).status_code == code


def test_reject_maps_200_404_409(make_client):
    svc = AsyncMock()
    c = make_client(ADMIN, svc)
    svc.reject_bonus = AsyncMock(return_value={"id": 3, "status": "rejected"})
    assert c.post("/api/hr/bonuses/3/reject", json={"note": "dup"}).status_code == 200
    assert svc.reject_bonus.await_args.args == (3, "tm-admin", "dup")
    assert c.post("/api/hr/bonuses/3/reject").status_code == 200  # body optional
    svc.reject_bonus = AsyncMock(side_effect=LookupError("not found"))
    assert c.post("/api/hr/bonuses/999/reject").status_code == 404
    svc.reject_bonus = AsyncMock(side_effect=ValueError("not pending"))
    assert c.post("/api/hr/bonuses/3/reject").status_code == 409


@pytest.mark.parametrize("outcome, code", [("deleted", 200), ("missing", 404), ("conflict", 409)])
def test_delete_maps_200_404_409(make_client, outcome, code):
    svc = AsyncMock()
    svc.delete_manual_bonus = AsyncMock(return_value=outcome)
    assert make_client(ADMIN, svc).delete("/api/hr/bonuses/3").status_code == code


def test_reject_and_delete_are_admin_only(make_client):
    svc = AsyncMock()
    c = make_client(MEMBER, svc)
    assert c.post("/api/hr/bonuses/3/reject").status_code == 403
    assert c.delete("/api/hr/bonuses/3").status_code == 403
    svc.delete_manual_bonus.assert_not_awaited()


def test_practice_types_admin_only_and_served(make_client):
    svc = AsyncMock()
    svc.list_bonus_practice_types = AsyncMock(
        return_value=[{"code": "tax_x", "name": "X", "category": "tax", "group": "tax"}]
    )
    assert make_client(MEMBER, svc).get("/api/hr/bonus-practice-types").status_code == 403
    res = make_client(ADMIN, svc).get("/api/hr/bonus-practice-types")
    assert res.status_code == 200 and res.json()["practice_types"][0]["group"] == "tax"


def test_member_bonus_list_hides_created_by_admin_keeps_it(make_client):
    row = {"id": 1, "source": "manual", "reason": "r", "created_by": "asya@balizero.com"}
    svc = AsyncMock()
    svc.list_bonuses = AsyncMock(return_value=[row])
    svc.get_employee_by_email = AsyncMock(return_value={"id": 7})
    member = make_client(MEMBER, svc).get("/api/hr/bonuses").json()
    assert "created_by" not in member["bonuses"][0] and member["bonuses"][0]["reason"] == "r"
    assert svc.list_bonuses.await_args.args[0] == 7  # forced to the member's own rows
    admin = make_client(ADMIN, svc).get("/api/hr/bonuses").json()
    assert admin["bonuses"][0]["created_by"] == "asya@balizero.com"


def test_approve_bonus_maps_200_409_400(make_client):
    svc = AsyncMock()
    c = make_client(ADMIN, svc)
    svc.approve_bonus = AsyncMock(return_value={"id": 3, "status": "approved"})
    assert c.post("/api/hr/bonuses/3/approve").status_code == 200
    svc.approve_bonus = AsyncMock(
        side_effect=BonusPeriodLockedError("That month's payroll is already approved")
    )
    res = c.post("/api/hr/bonuses/3/approve")
    assert res.status_code == 409 and "payroll is already approved" in res.json()["detail"]
    svc.approve_bonus = AsyncMock(side_effect=ValueError("not pending"))
    assert c.post("/api/hr/bonuses/3/approve").status_code == 400


def test_approve_payroll_maps_200_409_400(make_client):
    svc = AsyncMock()
    c = make_client(ADMIN, svc)
    svc.approve_payroll = AsyncMock(return_value={"id": 1, "status": "approved"})
    assert c.post("/api/hr/payroll/1/approve").status_code == 200
    svc.approve_payroll = AsyncMock(
        side_effect=PayrollBonusesChangedError(
            "Bonuses changed since this payroll was calculated; recalculate before approving"
        )
    )
    res = c.post("/api/hr/payroll/1/approve")
    assert res.status_code == 409 and "recalculate" in res.json()["detail"]
    svc.approve_payroll = AsyncMock(side_effect=ValueError("not calculated"))
    assert c.post("/api/hr/payroll/1/approve").status_code == 400
