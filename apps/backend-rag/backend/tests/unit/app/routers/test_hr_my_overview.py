"""GET /api/hr/me/overview: the member's own HR picture, keyed by the token's sub only."""

from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.services.hr.hr_service import HRCardCode, HRCardError

# Synthetic data only.
MEMBER = {"user_id": "tm-fake-7", "email": "fake.member@balizero.com", "role": "member"}
CLIENT = {"user_id": "tm-client", "email": "fake.client@example.com", "role": "client"}

CARD_ROW: dict[str, Any] = {
    "id": 7,
    "team_member_id": "tm-fake-7",
    "hire_date": date(2025, 1, 15),
    "is_active": True,
    "ptkp_status": "TK/0",
    "base_salary_idr": 1,
    "profile": {"legal_name": "Made Contoh"},
    "full_name": "Made Contoh",
    "role": "staff",
    "department": "ops",
}
PAYSLIP = {
    "id": 31,
    "payroll_year": 2026,
    "payroll_month": 10,
    "net_salary_idr": 1,
    "base_salary_idr": 1,
    "bonus_total_idr": 0,
    "allowance_total_idr": 0,
    "deduction_total_idr": 0,
    "thr_idr": 0,
    "notes": "HR note that must not leave",
    "employee_email": "fake.member@balizero.com",
    "period_status": "approved",
}
ROWS: dict[str, Any] = {
    "payslips": [PAYSLIP],
    "bonuses": [
        {
            "id": 1,
            "practice_type_code": "VISA",
            "awarded_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
            "status": "paid",
            "amount_idr": 5,
        },
        {
            "id": 2,
            "practice_type_code": "VISA",
            "awarded_at": datetime(2026, 3, 2, tzinfo=timezone.utc),
            "status": "rejected",
            "amount_idr": 9,
        },
    ],
    "balances": [
        {
            "id": 3,
            "leave_type_id": 1,
            "code": "annual",
            "leave_type_name": "Annual leave",
            "is_paid": True,
            "balance_year": 2026,
            "allocated_days": 12,
            "carried_over": 0,
            "used_days": 2,
            "pending_days": 0,
            "notes": "HR note that must not leave",
        }
    ],
    "requests": [
        {
            "id": 4,
            "leave_type_name": "Annual leave",
            "start_date": date(2026, 12, 1),
            "end_date": date(2026, 12, 2),
            "total_days": 2,
            "status": "pending",
            "requested_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
            "reason": "private reason that must not leave",
            "employee_email": "fake.member@balizero.com",
        }
    ],
    "attendance": [
        {
            "month": "2026-11",
            "days_present": 3,
            "hours": 24.5,
            "avg_clock_in": "09:10",
            "late_incidents": 1,
        }
    ],
}


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


def _service(
    card: dict[str, Any] | None = CARD_ROW, rows: dict[str, Any] | None = None
) -> AsyncMock:
    service = AsyncMock()
    service.get_card_for_member = AsyncMock(return_value=card)
    service.get_my_overview_rows = AsyncMock(return_value=rows if rows is not None else ROWS)
    return service


def test_member_gets_own_overview_with_owner_equal_to_the_token_sub(make_client):
    res = make_client(MEMBER, _service()).get("/api/hr/me/overview")
    assert res.status_code == 200
    body = res.json()
    assert body["team_member_id"] == "tm-fake-7"
    assert body["card"]["employee_id"] == 7 and body["card"]["legal_name"] == "Made Contoh"
    assert body["payslips"][0]["id"] == 31 and body["payslips"][0]["net_salary_idr"] == 1
    assert body["leave"]["balances"][0]["code"] == "annual"
    assert body["leave"]["requests"][0]["status"] == "pending"
    assert body["attendance"][0] == ROWS["attendance"][0]


def test_only_declared_fields_leave_no_notes_reasons_or_emails(make_client):
    text = make_client(MEMBER, _service()).get("/api/hr/me/overview").text
    for leak in ("HR note that must not leave", "private reason", "fake.member@balizero.com"):
        assert leak not in text
    assert "period_status" not in text


def test_the_member_is_the_sub_and_a_query_parameter_naming_someone_else_is_ignored(make_client):
    service = _service()
    res = make_client(MEMBER, service).get(
        "/api/hr/me/overview?employee_id=99&team_member_id=tm-other&email=x@example.com&user_id=tm-other"
    )
    assert res.status_code == 200
    service.get_card_for_member.assert_awaited_once_with("tm-fake-7")
    service.get_my_overview_rows.assert_awaited_once_with(7, "tm-fake-7")
    assert res.json()["team_member_id"] == "tm-fake-7"


def test_a_client_session_is_refused_before_any_read(make_client):
    service = _service()
    res = make_client(CLIENT, service).get("/api/hr/me/overview")
    assert res.status_code == 403
    service.get_card_for_member.assert_not_awaited()
    service.get_my_overview_rows.assert_not_awaited()


def test_member_without_an_hr_row_gets_404_and_nothing_else_is_read(make_client):
    service = _service(card=None)
    assert make_client(MEMBER, service).get("/api/hr/me/overview").status_code == 404
    service.get_my_overview_rows.assert_not_awaited()


def test_a_card_read_failure_is_a_fixed_500_without_driver_text(make_client):
    service = _service()
    service.get_card_for_member = AsyncMock(side_effect=HRCardError(HRCardCode.DB_ERROR))
    res = make_client(MEMBER, service).get("/api/hr/me/overview")
    assert res.status_code == 500 and res.json() == {"detail": "Card operation failed"}


def test_empty_states_are_empty_lists_and_zero_totals(make_client):
    rows = {"payslips": [], "bonuses": [], "balances": [], "requests": [], "attendance": []}
    body = make_client(MEMBER, _service(rows=rows)).get("/api/hr/me/overview").json()
    assert body["payslips"] == [] and body["bonuses"] == [] and body["attendance"] == []
    assert body["leave"] == {"balances": [], "requests": []}
    assert body["bonus_totals"] == {"month_total_idr": 0, "month_count": 0, "year_total_idr": 0}


def test_a_database_error_in_the_overview_read_is_a_fixed_500_without_driver_text(make_client):
    service = _service()
    service.get_my_overview_rows = AsyncMock(side_effect=RuntimeError("ZQXLEAK secret value 42"))
    res = make_client(MEMBER, service).get("/api/hr/me/overview")
    assert res.status_code == 500
    assert res.json() == {"detail": "Overview read failed"}
    assert "ZQXLEAK" not in res.text
