"""People console read routes: scope, roster shape, the shared overview composition."""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.routers import hr_people
from backend.app.services.hr.hr_overview_compose import compose_member_overview
from backend.app.services.hr.hr_service import HRCardCode, HRCardError
from backend.tests.unit.app.routers.test_hr_my_overview import CARD_ROW, ROWS

# Synthetic data only.
ADMIN = {"user_id": "tm-admin", "email": "zero@balizero.com", "role": "member"}
DIREKTUR = {"user_id": "tm-dir", "email": "adit@balizero.com", "role": "member"}
SCOPED = {"user_id": "tm-scoped", "email": "ruslana@balizero.com", "role": "member"}
MEMBER = {"user_id": "tm-m", "email": "krisna@balizero.com", "role": "member"}
CLIENT = {"user_id": "tm-c", "email": "zero@balizero.com", "role": "client"}

ID_IN = "11111111-1111-4111-8111-111111111111"
ID_OUT = "22222222-2222-4222-8222-222222222222"
ID_NOREC = "33333333-3333-4333-8333-333333333333"

ROSTER = [
    {
        "team_member_id": ID_IN,
        "employee_id": 7,
        "has_record": True,
        "name": "Made Contoh",
        "role": "staff",
        "department": "setup",
        "unit": "ops",
        "hire_date": date(2025, 1, 15),
        "active": True,
        "flags": {
            "papers_missing": 3,
            "bonuses_pending": 2,
            "leave_pending": 0,
            "on_leave_today": True,
        },
    },
    {
        "team_member_id": ID_NOREC,
        "employee_id": None,
        "has_record": False,
        "name": "Wayan Tanpa",
        "role": "staff",
        "department": "tax",
        "unit": None,
        "hire_date": None,
        "active": True,
        "flags": {
            "papers_missing": None,
            "bonuses_pending": 0,
            "leave_pending": 0,
            "on_leave_today": False,
        },
    },
]
PULSE = {
    "active_people": 2,
    "people_without_record": 1,
    "bonuses_pending_count": 2,
    "bonuses_pending_idr": 10,
    "leave_pending_count": 0,
    "payroll": {"month": 11, "year": 2026, "status": None},
}
EMAILS = {ID_IN: "dea@balizero.com", ID_OUT: "krisna@balizero.com", ID_NOREC: "rio@balizero.com"}


def _people_service() -> MagicMock:
    s = MagicMock()
    s.roster = AsyncMock(return_value=ROSTER)
    s.pulse = AsyncMock(return_value=PULSE)
    s.member_email = AsyncMock(side_effect=lambda i: EMAILS.get(i))
    return s


def _hr_service(card: dict[str, Any] | None = CARD_ROW) -> AsyncMock:
    s = AsyncMock()
    s.get_card_for_member = AsyncMock(
        side_effect=lambda i: dict(card, team_member_id=i) if card else None
    )
    s.get_my_overview_rows = AsyncMock(return_value=ROWS)
    return s


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(user: dict[str, Any], people: Any = None, hr: Any = None) -> TestClient:
        app = FastAPI()
        app.include_router(hr_people.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        for target, name, val in (
            (hr_people, "_get_people_service", people or _people_service()),
            (hr_people, "_get_hr_service", hr or _hr_service()),
        ):
            p = patch.object(target, name, return_value=val)
            p.start()
            patchers.append(p)
        return TestClient(app)

    yield _make
    for p in patchers:
        p.stop()


def test_admin_roster_has_all_staff_including_people_without_a_record_and_the_pulse(make_client):
    people = _people_service()
    body = make_client(ADMIN, people).get("/api/hr/people").json()
    assert body["scope"] == "all" and body["can_edit"] is True
    assert body["viewer_team_member_id"] == "tm-admin"
    rows = {p["team_member_id"]: p for p in body["people"]}
    assert rows[ID_NOREC]["has_record"] is False and rows[ID_NOREC]["employee_id"] is None
    assert rows[ID_NOREC]["flags"]["papers_missing"] is None
    assert rows[ID_IN]["flags"] == ROSTER[0]["flags"]
    assert body["pulse"]["people_without_record"] == 1
    people.roster.assert_awaited_once_with(None)


def test_the_direktur_is_a_full_admin(make_client):
    body = make_client(DIREKTUR).get("/api/hr/people").json()
    assert body["scope"] == "all" and body["can_edit"] is True and body["pulse"] is not None


def test_scoped_viewer_gets_only_the_four_emails_no_pulse_and_no_edit(make_client):
    people = _people_service()
    body = make_client(SCOPED, people).get("/api/hr/people").json()
    assert body["scope"] == "scoped" and body["can_edit"] is False and body["pulse"] is None
    people.roster.assert_awaited_once_with(
        ["candra@balizero.com", "dea@balizero.com", "rina@balizero.com", "rio@balizero.com"]
    )
    people.pulse.assert_not_awaited()


@pytest.mark.parametrize("user", [MEMBER, CLIENT])
def test_member_and_client_are_refused_before_any_read(make_client, user):
    people = _people_service()
    c = make_client(user, people)
    assert c.get("/api/hr/people").status_code == 403
    assert c.get(f"/api/hr/people/{ID_IN}/overview").status_code == 403
    people.roster.assert_not_awaited()
    people.member_email.assert_not_awaited()


def test_a_roster_read_failure_is_a_fixed_500_without_driver_text(make_client):
    people = _people_service()
    people.roster = AsyncMock(side_effect=RuntimeError("ZQXLEAK value 42"))
    res = make_client(ADMIN, people).get("/api/hr/people")
    assert res.status_code == 500 and res.json() == {"detail": "People read failed"}


def test_admin_overview_is_the_shared_composition_for_that_member(make_client):
    hr = _hr_service()
    res = make_client(ADMIN, hr=hr).get(f"/api/hr/people/{ID_IN}/overview")
    assert res.status_code == 200
    body = res.json()
    assert body["viewer_team_member_id"] == "tm-admin"
    assert body["person"]["team_member_id"] == ID_IN
    hr.get_card_for_member.assert_awaited_once_with(ID_IN)
    hr.get_my_overview_rows.assert_awaited_once_with(7, ID_IN)


@pytest.mark.asyncio
async def test_person_overview_equals_what_the_member_route_composes():
    hr = _hr_service()
    expected = (await compose_member_overview(hr, ID_IN)).model_dump(mode="json")
    app = FastAPI()
    app.include_router(hr_people.router)
    app.include_router(hr_router.router)
    app.dependency_overrides[get_current_user] = lambda: dict(ADMIN)
    app.dependency_overrides[get_database_pool] = lambda: MagicMock()
    with (
        patch.object(hr_people, "_get_people_service", return_value=_people_service()),
        patch.object(hr_people, "_get_hr_service", return_value=hr),
        patch.object(hr_router, "_get_hr_service", return_value=hr),
    ):
        client = TestClient(app)
        person = client.get(f"/api/hr/people/{ID_IN}/overview").json()["person"]
        app.dependency_overrides[get_current_user] = lambda: {
            "user_id": ID_IN,
            "email": "dea@balizero.com",
            "role": "member",
        }
        mine = client.get("/api/hr/me/overview").json()
    assert person == expected == mine


def test_scoped_viewer_sees_a_scoped_member_and_is_refused_anyone_else(make_client):
    c = make_client(SCOPED)
    assert c.get(f"/api/hr/people/{ID_IN}/overview").status_code == 200
    out = c.get(f"/api/hr/people/{ID_OUT}/overview")
    assert out.status_code == 403


def test_scope_is_decided_by_the_stored_email_never_by_a_client_parameter(make_client):
    res = make_client(SCOPED).get(
        f"/api/hr/people/{ID_OUT}/overview?email=dea@balizero.com&scope=all"
    )
    assert res.status_code == 403


def test_a_bad_id_is_422_and_an_unknown_staff_id_is_404(make_client):
    c = make_client(ADMIN)
    assert c.get("/api/hr/people/not-a-uuid/overview").status_code == 422
    unknown = "44444444-4444-4444-8444-444444444444"
    assert c.get(f"/api/hr/people/{unknown}/overview").status_code == 404


def test_a_member_without_an_hr_row_is_404_with_the_fixed_message(make_client):
    res = make_client(ADMIN, hr=_hr_service(card=None)).get(f"/api/hr/people/{ID_NOREC}/overview")
    assert res.status_code == 404
    assert res.json() == {"detail": "No HR employee record for this person"}


def test_overview_failures_map_to_fixed_messages(make_client):
    hr = _hr_service()
    hr.get_card_for_member = AsyncMock(side_effect=HRCardError(HRCardCode.DB_ERROR))
    res = make_client(ADMIN, hr=hr).get(f"/api/hr/people/{ID_IN}/overview")
    assert res.status_code == 500 and res.json() == {"detail": "Card operation failed"}
    hr = _hr_service()
    hr.get_my_overview_rows = AsyncMock(side_effect=RuntimeError("ZQXLEAK secret"))
    res = make_client(ADMIN, hr=hr).get(f"/api/hr/people/{ID_IN}/overview")
    assert res.status_code == 500 and res.json() == {"detail": "Overview read failed"}
    assert "ZQXLEAK" not in res.text
