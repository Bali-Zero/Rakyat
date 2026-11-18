"""`resumeOrderCheckout` HTTP shell — auth, error mapping, privacy headers.

No Postgres: the repository is a fake. The money-path behaviour (no write,
no second session, ownership) is proven against a real database in
`tests/services/garuda_orders/test_repository_integration.py`.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.routers import garuda_orders_router
from backend.services.garuda_orders.errors import (
    CheckoutNotResumable,
    InvalidStateTransition,
    OrderNotFound,
    PaymentProviderUnavailable,
)

_ACTOR = "result-resume-test-0000000"
_URL = "https://checkout.xendit.co/web/inv-fake-0002"
_PATH = "/api/visa/voa/orders/ord_fake/checkout-session"


@pytest.fixture(autouse=True)
def _garuda_public_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")


async def _verifier(cookie: str) -> str | None:
    return _ACTOR if cookie else None


class _Repository:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    async def resume_checkout(self, **kwargs) -> str:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return str(self.outcome)


def _app(repository: _Repository, *, verifier=_verifier) -> FastAPI:
    application = FastAPI()
    application.include_router(garuda_orders_router.router)
    application.state.garuda_order_repository = repository
    application.state.garuda_magic_session_verifier = verifier
    return application


async def _post(application: FastAPI, *, cookie: str | None = "session-value"):
    cookies = {"garuda_session": cookie} if cookie else {}
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(_PATH)


def _assert_privacy_headers(headers) -> None:
    assert headers.get("cache-control") == "no-store, private"
    assert headers.get("referrer-policy") == "no-referrer"
    assert headers.get("x-robots-tag") == "noindex, nofollow, noarchive"


@pytest.mark.asyncio
async def test_open_session_returns_only_the_url_scoped_to_the_session_actor() -> None:
    repository = _Repository(_URL)
    response = await _post(_app(repository))
    assert response.status_code == 200
    assert response.json() == {"checkout_url": _URL}
    _assert_privacy_headers(response.headers)
    assert repository.calls == [{"order_id": "ord_fake", "result_id": _ACTOR}]


@pytest.mark.asyncio
async def test_no_session_cookie_is_401_and_never_reaches_the_repository() -> None:
    repository = _Repository(_URL)
    response = await _post(_app(repository), cookie=None)
    assert response.status_code == 401
    assert response.json()["code"] == "SESSION_REQUIRED"
    assert repository.calls == []


@pytest.mark.asyncio
async def test_unverifiable_session_is_401() -> None:
    async def _rejects(cookie: str) -> str | None:
        return None

    repository = _Repository(_URL)
    response = await _post(_app(repository, verifier=_rejects))
    assert response.status_code == 401
    assert repository.calls == []


@pytest.mark.parametrize(
    ("error", "status_code", "code", "retryable"),
    [
        (OrderNotFound("ord_fake"), 404, "ORDER_NOT_FOUND", False),
        (InvalidStateTransition("ord_fake"), 409, "INVALID_STATE_TRANSITION", False),
        (CheckoutNotResumable("ord_fake"), 409, "CHECKOUT_NOT_RESUMABLE", False),
        (PaymentProviderUnavailable("ord_fake"), 503, "PAYMENT_PROVIDER_UNAVAILABLE", True),
    ],
)
@pytest.mark.asyncio
async def test_refusals_use_the_flat_contract_envelope(error, status_code, code, retryable) -> None:
    response = await _post(_app(_Repository(error)))
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == code
    assert body["retryable"] is retryable
    assert set(body) == {"code", "retryable", "message_key"}
    assert "checkout_url" not in response.text
    _assert_privacy_headers(response.headers)
