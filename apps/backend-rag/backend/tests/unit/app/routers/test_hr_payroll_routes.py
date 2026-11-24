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
