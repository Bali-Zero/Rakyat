"""The member's own My HR overview shows a payslip only once its payroll period is approved or paid.

A period in `draft` or `calculated` can still be recalculated by the accountant, so its payslip is a
provisional figure. The member's own view (`self_view=True`, `/me/overview`) leaves it out; an HR
viewer of the same person (`self_view=False`) keeps every payslip.
"""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.services.hr.hr_overview_compose import compose_member_overview

# Synthetic data only.
MEMBER = {"user_id": "tm-fake-7", "email": "fake.member@balizero.com", "role": "member"}

CARD_ROW: dict[str, Any] = {
    "id": 7,
    "team_member_id": "tm-fake-7",
    "hire_date": date(2025, 1, 15),
    "is_active": True,
    "ptkp_status": "TK/0",
    "base_salary_idr": 1,
    "on_payroll": True,
    "profile": {"legal_name": "Made Contoh"},
    "full_name": "Made Contoh",
    "role": "staff",
    "department": "ops",
}


def _slip(slip_id: int, month: int, status: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": slip_id,
        "payroll_year": 2026,
        "payroll_month": month,
        "net_salary_idr": 1,
        "base_salary_idr": 1,
        "bonus_total_idr": 0,
        "allowance_total_idr": 0,
        "deduction_total_idr": 0,
        "thr_idr": 0,
    }
    if status is not None:  # a row without the key is the "status unknown" case
        row["period_status"] = status
    return row


# newest first, as get_payslips returns them
SLIP_DRAFT = _slip(14, 11, "draft")
SLIP_CALCULATED = _slip(13, 10, "calculated")
SLIP_APPROVED = _slip(12, 9, "approved")
SLIP_PAID = _slip(11, 8, "paid")


def _rows(payslips: list[dict[str, Any]]) -> dict[str, Any]:
    return {"payslips": payslips, "bonuses": [], "balances": [], "requests": [], "attendance": []}


def _service(payslips: list[dict[str, Any]], card: dict[str, Any] = CARD_ROW) -> AsyncMock:
    service = AsyncMock()
    service.get_card_for_member = AsyncMock(return_value=card)
    service.get_my_overview_rows = AsyncMock(return_value=_rows(payslips))
    return service


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(service: Any) -> TestClient:
        app = FastAPI()
        app.include_router(hr_router.router)
        app.dependency_overrides[get_current_user] = lambda: MEMBER
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        patcher = patch.object(hr_router, "_get_hr_service", return_value=service)
        patcher.start()
        patchers.append(patcher)
        return TestClient(app)

    yield _make
    for p in patchers:
        p.stop()


def _ids(overview: Any) -> list[int]:
    return [p["id"] if isinstance(p, dict) else p.id for p in overview.payslips]


@pytest.mark.asyncio
async def test_the_members_own_view_leaves_out_payslips_of_periods_not_yet_approved() -> None:
    service = _service([SLIP_DRAFT, SLIP_CALCULATED, SLIP_APPROVED, SLIP_PAID])
    own = await compose_member_overview(service, "tm-fake-7", self_view=True)
    assert own is not None
    assert _ids(own) == [12, 11]  # approved and paid only, order kept


@pytest.mark.asyncio
async def test_an_hr_view_of_the_same_person_keeps_every_payslip() -> None:
    service = _service([SLIP_DRAFT, SLIP_CALCULATED, SLIP_APPROVED, SLIP_PAID])
    seen_by_hr = await compose_member_overview(service, "tm-fake-7")
    assert seen_by_hr is not None
    assert _ids(seen_by_hr) == [14, 13, 12, 11]


@pytest.mark.asyncio
async def test_a_payslip_whose_period_status_is_unknown_is_not_shown_to_the_member() -> None:
    service = _service([_slip(15, 12, None), SLIP_PAID])
    own = await compose_member_overview(service, "tm-fake-7", self_view=True)
    assert own is not None
    assert _ids(own) == [11]


@pytest.mark.asyncio
async def test_a_member_whose_only_payslips_are_provisional_gets_an_empty_list_not_an_error() -> (
    None
):
    service = _service([SLIP_DRAFT, SLIP_CALCULATED])
    own = await compose_member_overview(service, "tm-fake-7", self_view=True)
    assert own is not None
    assert own.payslips == []
    assert own.card.base_salary_idr == 1  # the rest of the overview is unchanged


@pytest.mark.asyncio
async def test_payslips_that_are_all_approved_or_paid_pass_through_unchanged() -> None:
    service = _service([SLIP_APPROVED, SLIP_PAID])
    own = await compose_member_overview(service, "tm-fake-7", self_view=True)
    assert own is not None
    assert _ids(own) == [12, 11]


def test_me_overview_route_does_not_show_the_provisional_payslip_as_take_home(make_client) -> None:
    service = _service([SLIP_CALCULATED, SLIP_APPROVED])
    res = make_client(service).get("/api/hr/me/overview")
    assert res.status_code == 200
    body = res.json()
    assert [p["id"] for p in body["payslips"]] == [12]
    assert "period_status" not in res.text
