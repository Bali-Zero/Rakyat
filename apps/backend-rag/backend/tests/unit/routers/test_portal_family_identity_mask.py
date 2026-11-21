"""Guard: GET /api/portal/family sends family identity fields masked (DAI-149).

Same masks as the profile endpoint (#273): passport keeps its last 3
characters, date of birth keeps only its year. There is no family reveal
endpoint, so the full values must never leave this route. Synthetic data only.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_database_pool
from backend.app.routers.portal import get_current_client
from backend.app.routers.portal_family import router

ADULT_PASSPORT = "X1234567"
MINOR_PASSPORT = "X7654321"
ADULT_DOB = date(1990, 5, 10)
MINOR_DOB = date(date.today().year - 8, 3, 15)


def _row(**overrides):
    row = {
        "id": 1,
        "full_name": "Ada Example",
        "relationship": "spouse",
        "date_of_birth": None,
        "nationality": "IT",
        "passport_number": None,
        "passport_expiry": None,
        "current_visa_type": None,
        "visa_expiry": None,
        "email": None,
        "phone": None,
    }
    row.update(overrides)
    return row


def _get_family(rows):
    conn = AsyncMock()
    conn.fetch.return_value = rows

    class _Ctx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Ctx()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_client] = lambda: {"client_id": 42}
    app.dependency_overrides[get_database_pool] = lambda: _Pool()
    r = TestClient(app).get("/api/portal/family")
    assert r.status_code == 200
    return r


def test_family_response_never_contains_full_passport_or_birth_date() -> None:
    r = _get_family(
        [
            _row(id=1, date_of_birth=ADULT_DOB, passport_number=ADULT_PASSPORT),
            _row(
                id=2,
                full_name="Ben Example",
                relationship="child",
                date_of_birth=MINOR_DOB,
                passport_number=MINOR_PASSPORT,
            ),
        ]
    )
    raw = r.text
    for secret in (ADULT_PASSPORT, MINOR_PASSPORT, ADULT_DOB.isoformat(), MINOR_DOB.isoformat()):
        assert secret not in raw

    body = r.json()
    adult, minor = body["adults"][0], body["minors"][0]
    assert adult["passport_number"] == "•" * 5 + "567"
    assert minor["passport_number"] == "•" * 5 + "321"
    assert adult["date_of_birth"] == "1990"
    assert minor["date_of_birth"] == str(MINOR_DOB.year)
    assert adult["identity_masked"] is True


def test_adult_minor_split_still_uses_the_full_birth_date() -> None:
    # Masking happens after the split: a child must not become an adult
    # because only the year is sent.
    body = _get_family([_row(id=2, relationship="child", date_of_birth=MINOR_DOB)]).json()
    assert body["adults"] == []
    assert body["minors"][0]["is_adult"] is False


def test_missing_identity_values_stay_null() -> None:
    body = _get_family([_row()]).json()
    member = body["adults"][0]
    assert member["passport_number"] is None
    assert member["date_of_birth"] is None
