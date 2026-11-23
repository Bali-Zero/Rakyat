"""Bounds for E33 advance event dates."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.e33_cases as e33_cases_module
from backend.app.dependencies import get_current_user, get_database_pool
from backend.services.crm.e33_case_repository import case_to_row
from backend.services.crm.e33_lifecycle import E33Case, E33Stage, GuaranteeBasis


@pytest.fixture
def admin_user() -> dict[str, str]:
    return {"id": "1", "email": "admin@balizero.com", "role": "admin"}


def _make_app(pool, user: dict[str, object]) -> FastAPI:
    application = FastAPI()
    application.include_router(e33_cases_module.router)
    application.dependency_overrides[get_current_user] = lambda: user
    application.dependency_overrides[get_database_pool] = lambda: pool
    return application


def _wire_case(conn, case: E33Case) -> None:
    conn.fetchrow = AsyncMock(
        side_effect=[
            case_to_row(case),
            {"full_name": "Synthetic Client", "assigned_to": "admin@balizero.com"},
        ]
    )
    conn.execute = AsyncMock(return_value="OK")


def _case(*, stage: E33Stage = E33Stage.VISA_ISSUED) -> E33Case:
    return E33Case(
        case_id="E33-2026-date01",
        client_id=1,
        basis=GuaranteeBasis.DEPOSIT,
        stage=stage,
    )


def _advance(client: TestClient, case: E33Case, occurred_on: date):
    return client.post(
        f"/api/e33/cases/{case.case_id}/advance",
        json={"to_stage": "entry", "occurred_on": occurred_on.isoformat()},
    )


@pytest.mark.integration
def test_advance_rejects_future_occurred_on(mock_db_pool, admin_user) -> None:
    pool, conn = mock_db_pool
    case = _case()
    _wire_case(conn, case)

    response = _advance(TestClient(_make_app(pool, admin_user)), case, date(2062, 1, 1))

    assert response.status_code == 422


@pytest.mark.integration
def test_advance_rejects_occurred_on_more_than_six_years_ago(mock_db_pool, admin_user) -> None:
    pool, conn = mock_db_pool
    case = _case()
    _wire_case(conn, case)
    too_old = date.today() - timedelta(days=7 * 366)

    response = _advance(TestClient(_make_app(pool, admin_user)), case, too_old)

    assert response.status_code == 422


@pytest.mark.integration
def test_advance_rejects_occurred_on_before_case_entry_date(mock_db_pool, admin_user) -> None:
    pool, conn = mock_db_pool
    case = _case(stage=E33Stage.ENTRY)
    case.entry_date = date.today()
    _wire_case(conn, case)
    client = TestClient(_make_app(pool, admin_user))

    response = client.post(
        f"/api/e33/cases/{case.case_id}/advance",
        json={
            "to_stage": "itas_active",
            "occurred_on": (case.entry_date - timedelta(days=1)).isoformat(),
        },
    )

    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.parametrize(
    "occurred_on",
    [date.today(), date.today() - timedelta(days=365)],
)
def test_advance_accepts_today_and_recent_past_dates(mock_db_pool, admin_user, occurred_on) -> None:
    pool, conn = mock_db_pool
    case = _case()
    _wire_case(conn, case)

    response = _advance(TestClient(_make_app(pool, admin_user)), case, occurred_on)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "case_id",
        "client_id",
        "client_name",
        "basis",
        "stage",
        "owner_email",
        "guarantee_proof_deadline",
        "stayguard_eligible",
        "dependent_code",
        "principal_case_id",
        "created_at",
        "entry_date",
        "itas_date",
        "dependents",
        "evidence",
        "stage_history",
        "allowed_next_stages",
        "guarantee",
        "guarantee_evidence_complete",
        "forecasts",
    }
    assert body["stage"] == "entry"
    assert body["entry_date"] == occurred_on.isoformat()


@pytest.mark.integration
def test_advance_accepts_today_in_bali_while_the_server_clock_is_still_on_yesterday(
    mock_db_pool, admin_user, monkeypatch
) -> None:
    server_today = date.today()
    late_utc = datetime.combine(server_today, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        hours=23
    )

    class _ServerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return late_utc.astimezone(tz) if tz else late_utc.replace(tzinfo=None)

    monkeypatch.setattr(e33_cases_module, "datetime", _ServerClock)
    bali_today = server_today + timedelta(days=1)
    pool, conn = mock_db_pool
    case = _case()
    _wire_case(conn, case)
    client = TestClient(_make_app(pool, admin_user))

    assert _advance(client, case, bali_today).status_code == 200
    _wire_case(conn, _case())
    assert _advance(client, case, bali_today + timedelta(days=1)).status_code == 422


@pytest.mark.integration
def test_a_same_stage_advance_is_still_refused_as_re_dating(mock_db_pool, admin_user) -> None:
    pool, conn = mock_db_pool
    case = _case(stage=E33Stage.ENTRY)
    case.entry_date = date.today()
    _wire_case(conn, case)

    response = _advance(
        TestClient(_make_app(pool, admin_user)), case, case.entry_date - timedelta(days=1)
    )

    assert response.status_code == 409


def _fix_today(monkeypatch, today: date) -> None:
    monkeypatch.setattr(e33_cases_module, "_latest_occurred_on", lambda: today)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("today", "oldest_accepted", "first_refused"),
    [
        (date(2026, 11, 23), date(2020, 11, 23), date(2020, 11, 22)),
        (date(2028, 2, 29), date(2022, 2, 28), date(2022, 2, 27)),
        (date(2030, 3, 1), date(2024, 3, 1), date(2024, 2, 29)),
    ],
)
def test_the_oldest_accepted_date_is_exactly_six_years_back(
    mock_db_pool, admin_user, monkeypatch, today, oldest_accepted, first_refused
) -> None:
    _fix_today(monkeypatch, today)
    pool, conn = mock_db_pool
    client = TestClient(_make_app(pool, admin_user))

    _wire_case(conn, _case())
    accepted = _advance(client, _case(), oldest_accepted)
    _wire_case(conn, _case())
    refused = _advance(client, _case(), first_refused)

    assert accepted.status_code == 200
    assert accepted.json()["entry_date"] == oldest_accepted.isoformat()
    assert refused.status_code == 422
    assert refused.json()["detail"] == "occurred_on cannot be more than six years ago"


@pytest.mark.integration
def test_an_event_on_the_entry_date_itself_is_accepted(mock_db_pool, admin_user) -> None:
    pool, conn = mock_db_pool
    case = _case(stage=E33Stage.ENTRY)
    case.entry_date = date.today() - timedelta(days=10)
    _wire_case(conn, case)

    response = TestClient(_make_app(pool, admin_user)).post(
        f"/api/e33/cases/{case.case_id}/advance",
        json={"to_stage": "itas_active", "occurred_on": case.entry_date.isoformat()},
    )

    assert response.status_code == 200
    assert response.json()["itas_date"] == case.entry_date.isoformat()


_FUTURE = date(2062, 1, 1)
_ANCIENT = date(1990, 1, 1)


@pytest.mark.integration
@pytest.mark.parametrize("occurred_on", [_FUTURE, _ANCIENT])
def test_a_same_stage_advance_with_an_impossible_date_is_still_a_conflict(
    mock_db_pool, admin_user, occurred_on
) -> None:
    pool, conn = mock_db_pool
    case = _case(stage=E33Stage.ENTRY)
    case.entry_date = date.today()
    _wire_case(conn, case)

    response = _advance(TestClient(_make_app(pool, admin_user)), case, occurred_on)

    assert response.status_code == 409
    assert "same-stage" in response.json()["detail"]


@pytest.mark.integration
@pytest.mark.parametrize("occurred_on", [_FUTURE, _ANCIENT])
def test_closing_the_guarantee_gate_without_evidence_is_still_a_conflict_whatever_the_date(
    mock_db_pool, admin_user, occurred_on
) -> None:
    pool, conn = mock_db_pool
    case = _case(stage=E33Stage.GUARANTEE_PROOF_DUE)
    case.entry_date = date.today() - timedelta(days=100)
    _wire_case(conn, case)

    response = TestClient(_make_app(pool, admin_user)).post(
        f"/api/e33/cases/{case.case_id}/advance",
        json={"to_stage": "annual_maintenance", "occurred_on": occurred_on.isoformat()},
    )

    assert response.status_code == 409
    assert "guarantee evidence" in response.json()["detail"]


@pytest.mark.integration
@pytest.mark.parametrize("occurred_on", [_FUTURE, _ANCIENT])
def test_a_transition_the_lifecycle_refuses_is_still_a_conflict_whatever_the_date(
    mock_db_pool, admin_user, occurred_on
) -> None:
    pool, conn = mock_db_pool
    case = _case(stage=E33Stage.VISA_ISSUED)
    _wire_case(conn, case)

    response = TestClient(_make_app(pool, admin_user)).post(
        f"/api/e33/cases/{case.case_id}/advance",
        json={"to_stage": "annual_maintenance", "occurred_on": occurred_on.isoformat()},
    )

    assert response.status_code == 409
    assert "allowed transitions" in response.json()["detail"]


@pytest.mark.integration
def test_a_missing_case_is_still_not_found_whatever_the_date(mock_db_pool, admin_user) -> None:
    pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="OK")

    response = TestClient(_make_app(pool, admin_user)).post(
        "/api/e33/cases/E33-2026-missing/advance",
        json={"to_stage": "entry", "occurred_on": _FUTURE.isoformat()},
    )

    assert response.status_code == 404
