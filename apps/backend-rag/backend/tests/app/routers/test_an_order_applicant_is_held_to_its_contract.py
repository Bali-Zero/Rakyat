"""createOrderFromCheck holds its applicant to the frozen contract.

`products/garuda-voa/contracts/openapi.yaml#/components/schemas/Applicant`:
four required string keys and no other, each within its length bounds, and
`email` in `format: email`. A request outside it is answered 422
INVALID_REQUEST before the repository is reached, so no idempotency key is
reserved and nothing is stored on a payable order. No database: the
repository and the session verifier are fakes.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import garuda_orders_router
from backend.services.garuda_orders.models import Applicant

RESULT_ID = "r" * 22
VALID = {
    "full_name": "Test Traveller",
    "email": "customer@example.com",
    "phone": "+62 812 0000 0000",
    "passport_number": "P0000002",
}
REFUSED = {
    "code": "INVALID_REQUEST",
    "retryable": False,
    "message_key": "garuda_voa.error.invalid_request",
}
LONG_EMAIL = "a" * 64 + "@" + ".".join(["b" * 63] * 3) + "." + "c" * 63
assert len(LONG_EMAIL) == 320


class _Repository:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_order_and_checkout(self, **kwargs: object) -> tuple[dict, bool]:
        self.calls.append(kwargs)
        return {"order_id": "ord_test", "order_state": "awaiting_payment"}, False


@pytest.fixture
def repository() -> _Repository:
    return _Repository()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, repository: _Repository) -> TestClient:
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")

    async def verifier(cookie: str) -> str:
        return RESULT_ID

    app = FastAPI()
    app.include_router(garuda_orders_router.router)
    app.state.garuda_order_repository = repository
    app.state.garuda_magic_session_verifier = verifier
    client = TestClient(app)
    client.cookies.set("garuda_session", "session-cookie")
    return client


def _create(client: TestClient, applicant: object) -> object:
    return client.post(
        "/api/visa/voa/orders",
        json={"result_id": RESULT_ID, "applicant": applicant, "review_confirmed": True},
        headers={"Idempotency-Key": "k" * 32},
    )


def _with(**changes: object) -> dict:
    return {**VALID, **changes}


@pytest.mark.parametrize(
    "applicant",
    [
        _with(full_name=1),
        _with(full_name=None),
        _with(full_name=True),
        _with(full_name=["Test", "Traveller"]),
        _with(email=7),
        _with(phone=None),
        _with(phone=628120000),
        _with(passport_number=123456),
        _with(passport_number={"number": "P0000002"}),
    ],
)
def test_a_value_that_is_not_a_string_is_refused_before_the_repository(
    client: TestClient, repository: _Repository, applicant: dict
) -> None:
    response = _create(client, applicant)

    assert response.status_code == 422
    assert response.json() == REFUSED
    assert response.headers["Cache-Control"] == "no-store, private"
    assert repository.calls == []


@pytest.mark.parametrize(
    "applicant",
    [
        _with(full_name=""),
        _with(passport_number=""),
        _with(phone="12"),
        _with(phone=""),
        _with(full_name="n" * 201),
        _with(phone="1" * 33),
        _with(passport_number="P" * 65),
        _with(email="a" + LONG_EMAIL),
    ],
)
def test_a_value_outside_its_length_bounds_is_refused(
    client: TestClient, repository: _Repository, applicant: dict
) -> None:
    response = _create(client, applicant)

    assert response.status_code == 422
    assert response.json() == REFUSED
    assert repository.calls == []


@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "customer@",
        "@example.com",
        "customer @example.com",
        "customer@exa mple.com",
        "customer@example..com",
        "customer@-example.com",
        "customer@example-.com",
        "customer@" + "b" * 64 + ".com",
        "customer@example.com\n",
        "customer@@example.com",
        "customer@example.com,other@example.com",
    ],
)
def test_an_email_outside_the_email_format_is_refused(
    client: TestClient, repository: _Repository, email: str
) -> None:
    response = _create(client, _with(email=email))

    assert response.status_code == 422
    assert response.json() == REFUSED
    assert repository.calls == []


@pytest.mark.parametrize(
    "applicant",
    [
        {**VALID, "nationality": "IT"},
        {key: value for key, value in VALID.items() if key != "phone"},
        {},
        "Test Traveller",
        ["Test Traveller"],
        None,
    ],
)
def test_an_applicant_without_exactly_the_four_keys_is_refused(
    client: TestClient, repository: _Repository, applicant: object
) -> None:
    response = _create(client, applicant)

    assert response.status_code == 422
    assert response.json() == REFUSED
    assert repository.calls == []


@pytest.mark.parametrize(
    "applicant",
    [
        VALID,
        _with(full_name="N", passport_number="P", phone="123"),
        _with(full_name="n" * 200, phone="1" * 32, passport_number="P" * 64, email=LONG_EMAIL),
        _with(full_name="Ñandú Łukasz 王" + "é" * 186),
        _with(email="tracker-e2e@example.invalid"),
        _with(email="customer@localhost"),
        _with(email="first.last+tag@sub.example.co.id"),
        _with(email="o'neil_x-y@example.com"),
    ],
)
def test_an_applicant_within_the_contract_reaches_the_repository_unchanged(
    client: TestClient, repository: _Repository, applicant: dict
) -> None:
    response = _create(client, applicant)

    assert response.status_code == 201
    assert [call["applicant"] for call in repository.calls] == [Applicant(**applicant)]
