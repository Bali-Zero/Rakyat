"""An eligibility check that cannot be computed or stored is refused, not crashed on.

The public create types its three dates as bare dates and ``travellers`` with a minimum
only, as the contract does. The engine adds the length of a stay to the arrival date and
subtracts the filing window from the expiry before any other check, so a date at the very
edge of the calendar raised ``OverflowError``; a traveller count above the INT column failed
at the INSERT. Both escaped as a 500.

Nothing else changes: every request the route answers today gets the same answer, however
far its dates are. That is why the refusal is tied to what the engine cannot compute and to
the column's width, and not to a window of plausible years. And each refusal is made where
the 500 used to arise, so every answer that came before it still comes before it: the funnel
switched off, a missing key, a missing acknowledgement, and for the traveller count also the
store's own conflict and missing-policy answers.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from backend.app.routers import garuda_voa_public as router_mod
from backend.services.garuda_flow import check_store
from backend.services.garuda_flow.check_store import PostgresCheckStore
from backend.services.garuda_flow.civil_clock import garuda_today
from backend.services.garuda_flow.constants import EXTENSION_WINDOW_OPENS_DAYS
from backend.services.garuda_flow.public_api import EligibilityCheckOutcome, StoredCheck
from backend.services.visa_check.catalogue import VISA_META, VisaType
from backend.tests.fixtures.prod_shaped_pool import create_prod_shaped_pool

TODAY = garuda_today()
URL = "/api/visa/voa/eligibility-checks"
KEY = "test-key-0123456789abcdef"
INVALID = {
    "code": "INVALID_REQUEST",
    "retryable": False,
    "message_key": "garuda_voa.error.invalid_request",
}
STAY_DAYS = VISA_META[VisaType.B1].duration_days
WINDOW_DAYS = EXTENSION_WINDOW_OPENS_DAYS
INT_COLUMN_MAX = 2**31 - 1
_DSN = (
    os.environ.get("GARUDA_L3_TEST_DSN")
    or os.environ.get("INTAKE_TEST_DSN")
    or "postgresql://localhost:5432/nuzantara_test"
)


def _issuance(**changed: Any) -> dict[str, Any]:
    arrival = TODAY + timedelta(days=7)
    body: dict[str, Any] = {
        "case_type": "issuance",
        "nationality": "USA",
        "entry_date": arrival.isoformat(),
        "passport_expiry_date": (arrival + timedelta(days=200)).isoformat(),
        "purpose": "tourism",
        "travellers": 1,
        "self_pay": True,
        "extension_already_used": False,
        "retention_notice_acknowledged": True,
    }
    return {**body, **changed}


def _extension(**changed: Any) -> dict[str, Any]:
    body = _issuance(
        case_type="extension",
        entry_date=(TODAY - timedelta(days=10)).isoformat(),
        voa_expiry_date=(TODAY + timedelta(days=STAY_DAYS - 10)).isoformat(),
    )
    return {**body, **changed}


class _Store:
    """Keeps what it is asked to store; the engine and the route are the real ones."""

    def __init__(self) -> None:
        self.stored: list[dict[str, object]] = []

    async def create(
        self,
        *,
        idempotency_key: str,
        canonical_request: dict[str, object],
        outcome: EligibilityCheckOutcome,
    ) -> StoredCheck:
        self.stored.append(canonical_request)
        return StoredCheck(
            result_id="r" * 22,
            outcome=outcome,
            idempotency_replayed=False,
            session_secret="s" * 43,
        )


@pytest.fixture
def store() -> _Store:
    return _Store()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, store: _Store) -> TestClient:
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[router_mod.get_garuda_check_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def _post(api: TestClient, body: dict[str, Any]) -> Any:
    return api.post(URL, json=body, headers={"Idempotency-Key": KEY})


# ---------------------------------------------------------------- dates the engine cannot use


@pytest.mark.parametrize("days_before_the_last_day", [0, 1, STAY_DAYS - 1])
def test_an_arrival_whose_stay_would_end_after_the_last_day_of_the_calendar_is_refused(
    api: TestClient, store: _Store, days_before_the_last_day: int
) -> None:
    arrival = date.max - timedelta(days=days_before_the_last_day)

    answer = _post(api, _issuance(entry_date=arrival.isoformat()))

    assert answer.status_code == 422
    assert answer.json() == INVALID
    assert store.stored == []


@pytest.mark.parametrize("days_after_the_first_day", [0, 1, WINDOW_DAYS - 1])
def test_an_expiry_whose_filing_window_would_open_before_the_first_day_is_refused(
    api: TestClient, store: _Store, days_after_the_first_day: int
) -> None:
    expiry = date.min + timedelta(days=days_after_the_first_day)

    answer = _post(api, _extension(voa_expiry_date=expiry.isoformat()))

    assert answer.status_code == 422
    assert answer.json() == INVALID
    assert store.stored == []


@pytest.mark.parametrize("passport_expiry", [date.max, date.min])
def test_an_uncomputable_arrival_is_refused_whatever_the_passport_says(
    api: TestClient, store: _Store, passport_expiry: date
) -> None:
    body = _issuance(
        entry_date=date.max.isoformat(), passport_expiry_date=passport_expiry.isoformat()
    )

    answer = _post(api, body)

    assert answer.status_code == 422
    assert answer.json() == INVALID
    assert store.stored == []


def test_no_date_near_either_end_of_the_calendar_makes_the_route_fail(api: TestClient) -> None:
    """Every field, both case types, the first and the last 45 days of the calendar."""
    edges = [date.min + timedelta(days=n) for n in range(46)]
    edges += [date.max - timedelta(days=n) for n in range(46)]
    asked = 0
    neither_a_verdict_nor_a_refusal: list[tuple[str, str, str, int]] = []
    for make, fields in (
        (_issuance, ("entry_date", "passport_expiry_date")),
        (_extension, ("entry_date", "passport_expiry_date", "voa_expiry_date")),
    ):
        for field in fields:
            for day in edges:
                status = _post(api, make(**{field: day.isoformat()})).status_code
                asked += 1
                if status not in (201, 422):
                    neither_a_verdict_nor_a_refusal.append(
                        (make.__name__, field, day.isoformat(), status)
                    )

    assert asked == 5 * 92
    assert neither_a_verdict_nor_a_refusal == []


# ---------------------------------------------------------------- dates that answer as before


def test_the_first_arrival_the_engine_can_compute_is_still_a_verdict(
    api: TestClient, store: _Store
) -> None:
    arrival = date.max - timedelta(days=STAY_DAYS)

    answer = _post(api, _issuance(entry_date=arrival.isoformat()))

    assert answer.status_code == 201
    assert answer.json()["verdict"] == "DECLINE"
    assert "ARRIVAL_TOO_FAR" in answer.json()["reason_codes"]
    assert [request["entry_date"] for request in store.stored] == [arrival.isoformat()]


def test_the_first_expiry_the_engine_can_compute_is_still_a_verdict(
    api: TestClient, store: _Store
) -> None:
    expiry = date.min + timedelta(days=WINDOW_DAYS)

    answer = _post(api, _extension(voa_expiry_date=expiry.isoformat()))

    assert answer.status_code == 201
    assert answer.json()["verdict"] == "DECLINE"
    assert [request["voa_expiry_date"] for request in store.stored] == [expiry.isoformat()]


def test_a_passport_that_expires_on_the_last_day_of_the_calendar_is_still_accepted(
    api: TestClient, store: _Store
) -> None:
    """The reason the refusal is not a window of years: this request has an answer today."""
    answer = _post(api, _issuance(passport_expiry_date=date.max.isoformat()))

    assert answer.status_code == 201
    assert answer.json()["verdict"] == "ACCEPT"
    assert len(store.stored) == 1


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (_issuance(passport_expiry_date=date.min.isoformat()), "PASSPORT_VALIDITY"),
        (_issuance(entry_date=date.min.isoformat()), "ARRIVAL_DATE_UNCONFIRMED"),
        (_extension(entry_date=date.min.isoformat()), "EXTENSION_EXCEEDS_MAX_STAY"),
    ],
    ids=["passport on the first day", "arrival on the first day", "extension arrival long ago"],
)
def test_a_far_date_the_engine_can_compute_is_still_declined_with_its_reason(
    api: TestClient, store: _Store, body: dict[str, Any], reason: str
) -> None:
    answer = _post(api, body)

    assert answer.status_code == 201
    assert answer.json()["verdict"] == "DECLINE"
    assert reason in answer.json()["reason_codes"]
    assert len(store.stored) == 1


# ---------------------------------------------------------------- travellers


def test_the_bound_on_travellers_is_the_width_of_an_int_column() -> None:
    assert getattr(check_store, "_TRAVELLERS_COLUMN_MAX", None) == INT_COLUMN_MAX


@pytest.mark.parametrize("travellers", [2, 1000, INT_COLUMN_MAX])
def test_a_group_the_column_can_hold_is_still_declined_as_a_group(
    api: TestClient, store: _Store, travellers: int
) -> None:
    answer = _post(api, _issuance(travellers=travellers))

    assert answer.status_code == 201
    assert answer.json()["verdict"] == "DECLINE"
    assert "GROUP_CASE" in answer.json()["reason_codes"]
    assert [request["travellers"] for request in store.stored] == [travellers]


# ---------------------------------------------------------------- the answers that come first

CANNOT_BE_SERVED = {
    "too many travellers": _issuance(travellers=2**31),
    "an uncomputable arrival": _issuance(entry_date=date.max.isoformat()),
    "an uncomputable expiry": _extension(voa_expiry_date=date.min.isoformat()),
}


@pytest.mark.parametrize("body", CANNOT_BE_SERVED.values(), ids=CANNOT_BE_SERVED.keys())
def test_with_the_funnel_switched_off_the_answer_is_still_that_it_is_off(
    monkeypatch: pytest.MonkeyPatch, api: TestClient, store: _Store, body: dict[str, Any]
) -> None:
    monkeypatch.delenv("GARUDA_PUBLIC_ENABLED")

    answer = _post(api, body)

    assert (answer.status_code, answer.json()["code"]) == (404, "GARUDA_PUBLIC_DISABLED")
    assert store.stored == []


@pytest.mark.parametrize("body", CANNOT_BE_SERVED.values(), ids=CANNOT_BE_SERVED.keys())
@pytest.mark.parametrize("headers", [{}, {"Idempotency-Key": "x"}], ids=["no key", "a bad key"])
def test_without_a_usable_key_the_answer_is_still_that_a_key_is_required(
    api: TestClient, store: _Store, body: dict[str, Any], headers: dict[str, str]
) -> None:
    answer = api.post(URL, json=body, headers=headers)

    assert (answer.status_code, answer.json()["code"]) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    assert store.stored == []


@pytest.mark.parametrize("body", CANNOT_BE_SERVED.values(), ids=CANNOT_BE_SERVED.keys())
def test_without_the_acknowledgement_the_answer_is_still_that_it_is_required(
    api: TestClient, store: _Store, body: dict[str, Any]
) -> None:
    answer = _post(api, {**body, "retention_notice_acknowledged": False})

    assert answer.status_code == 422
    assert answer.json()["code"] == "NOTICE_ACKNOWLEDGEMENT_REQUIRED"
    assert store.stored == []


# ---------------------------------------------------------------- the real store


async def _close_open_policies(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        UPDATE public.visa_decision_retention_policies
           SET effective_period = tstzrange(lower(effective_period), clock_timestamp(), '[)')
         WHERE environment = 'TEST' AND policy_scope = 'GARUDA_CHECK'
           AND upper(effective_period) IS NULL
        """
    )


@pytest.fixture
async def real_store(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The route over ``PostgresCheckStore``, with one open TEST policy for the checks."""
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")
    try:
        pool = await create_prod_shaped_pool(_DSN, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI has no reachable Postgres at the test DSN: {exc}")
        pytest.skip(f"no local Postgres reachable at the test DSN: {exc}")
        raise
    async with pool.acquire() as conn:
        await _close_open_policies(conn)
        await conn.execute(
            """
            INSERT INTO public.visa_decision_retention_policies (
                environment, policy_scope, policy_version, retention_interval,
                idempotency_retention_interval, legal_hold_review_interval,
                retention_anchor, effective_period, approved_by, approval_reference
            ) VALUES (
                'TEST', 'GARUDA_CHECK', $1, INTERVAL '90 days',
                INTERVAL '1 hour', INTERVAL '30 days',
                'CREATED_AT', tstzrange(clock_timestamp(), NULL, '[)'),
                'zero-test-approver', 'ZERO-TRAVELLERS-WIDTH-TEST-APPROVAL'
            )
            """,
            f"travellers-width-test-{uuid.uuid4().hex[:16]}",
        )
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[router_mod.get_garuda_check_store] = lambda: PostgresCheckStore(
        pool, environment="TEST"
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="https://testserver") as client:
            yield client, pool
    finally:
        async with pool.acquire() as conn:
            await _close_open_policies(conn)
        await pool.close()


def _fresh_key() -> str:
    return f"travellers-width-{uuid.uuid4().hex}"


async def _results(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT count(*) FROM garuda_voa_check_results")


@pytest.mark.asyncio
async def test_the_real_store_keeps_the_largest_group_the_route_admits(real_store: Any) -> None:
    client, pool = real_store

    answer = await client.post(
        URL, json=_issuance(travellers=INT_COLUMN_MAX), headers={"Idempotency-Key": _fresh_key()}
    )

    assert answer.status_code == 201
    result_id = answer.headers["Location"].rsplit("/", 1)[-1]
    async with pool.acquire() as conn:
        kept = await conn.fetchval(
            "SELECT travellers FROM garuda_voa_check_results WHERE result_id = $1", result_id
        )
    assert kept == INT_COLUMN_MAX


@pytest.mark.asyncio
@pytest.mark.parametrize("travellers", [2**31, 3_000_000_000, 2**63, 10**30])
async def test_more_travellers_than_the_column_holds_is_refused_and_nothing_is_kept(
    real_store: Any, travellers: int
) -> None:
    client, pool = real_store
    before = await _results(pool)
    key = _fresh_key()

    refused = await client.post(
        URL, json=_issuance(travellers=travellers), headers={"Idempotency-Key": key}
    )

    assert refused.status_code == 422
    assert refused.json() == INVALID
    assert await _results(pool) == before
    # The key is not kept either: the same key serves a request that can be stored, as new.
    served = await client.post(URL, json=_issuance(), headers={"Idempotency-Key": key})
    assert (served.status_code, served.headers["Idempotency-Replayed"]) == (201, "false")


@pytest.mark.asyncio
async def test_under_a_key_bound_to_another_request_the_answer_is_still_a_conflict(
    real_store: Any,
) -> None:
    client, _pool = real_store
    key = _fresh_key()
    first = await client.post(URL, json=_issuance(), headers={"Idempotency-Key": key})
    assert first.status_code == 201

    answer = await client.post(
        URL, json=_issuance(travellers=2**31), headers={"Idempotency-Key": key}
    )

    assert (answer.status_code, answer.json()["code"]) == (409, "IDEMPOTENCY_CONFLICT")


@pytest.mark.asyncio
async def test_with_no_policy_in_force_the_answer_is_still_that_storing_is_unavailable(
    real_store: Any,
) -> None:
    client, pool = real_store
    async with pool.acquire() as conn:
        await _close_open_policies(conn)

    answer = await client.post(
        URL, json=_issuance(travellers=2**31), headers={"Idempotency-Key": _fresh_key()}
    )

    assert answer.status_code == 503
    assert answer.json()["code"] == "PERSISTENCE_POLICY_UNAVAILABLE"
