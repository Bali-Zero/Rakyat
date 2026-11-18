"""`resumeOrderCheckout` through the REAL application (gate REWORK on ce96206881).

The bare-router tests next to this file proved the handler; they could not see
that `HybridAuthMiddleware` answered `401 Bearer` before the handler ran,
because the path had no `public_endpoints.py` entry, nor that the shared rate
limiter answered a 429 without the contract's envelope. These tests send the
customer's only credential, the `garuda_session` magic cookie, to the object
`uvicorn backend.app.main_api:app` serves, with every middleware in place and
only the downstream verifier and repository faked (no lifespan, no database).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import backend.middleware.rate_limiter as rate_limiter_module
from backend.app import main_api as _main_api_module
from backend.app.auth.public_endpoints import find_entry
from backend.services.garuda_orders.errors import OrderNotFound

_APP = _main_api_module.app
_PATH = "/api/visa/voa/orders/ord_mounted_0001/checkout-session"
_URL = "https://checkout.xendit.co/web/inv-mounted-fake-0001"
_SESSIONS = {"cookie-owner-0001": "result-owner-0001", "cookie-other-0001": "result-other-0001"}
_PRIVACY = {
    "cache-control": "no-store, private",
    "referrer-policy": "no-referrer",
    "x-robots-tag": "noindex, nofollow, noarchive",
}


class _OwnedOrderRepository:
    """One order, owned by `result-owner-0001`; anyone else sees it absent."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def resume_checkout(self, *, order_id: str, result_id: str) -> str:
        self.calls.append({"order_id": order_id, "result_id": result_id})
        if order_id == "ord_mounted_0001" and result_id == "result-owner-0001":
            return _URL
        raise OrderNotFound(order_id)


async def _verifier(cookie: str) -> str | None:
    return _SESSIONS.get(cookie)


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> _OwnedOrderRepository:
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")
    repo = _OwnedOrderRepository()
    monkeypatch.setattr(_APP.state, "garuda_order_repository", repo, raising=False)
    monkeypatch.setattr(_APP.state, "garuda_magic_session_verifier", _verifier, raising=False)
    return repo


async def _post(cookie: str | None):
    cookies = {"garuda_session": cookie} if cookie else {}
    async with AsyncClient(
        transport=ASGITransport(app=_APP), base_url="http://test", cookies=cookies
    ) as client:
        return await client.post(_PATH)


@pytest.mark.asyncio
async def test_owner_magic_session_reaches_the_handler_and_gets_the_url(repository):
    response = await _post("cookie-owner-0001")
    assert response.status_code == 200, (response.status_code, response.headers)
    assert response.json() == {"checkout_url": _URL}
    assert response.headers.get("X-Auth-Type") == "public"
    assert response.headers.get("WWW-Authenticate") is None
    for name, value in _PRIVACY.items():
        assert response.headers.get(name) == value
    assert repository.calls == [{"order_id": "ord_mounted_0001", "result_id": "result-owner-0001"}]


@pytest.mark.asyncio
async def test_another_session_gets_the_non_enumerating_404(repository):
    response = await _post("cookie-other-0001")
    assert response.status_code == 404
    assert response.json()["code"] == "ORDER_NOT_FOUND"
    assert "checkout_url" not in response.text
    assert repository.calls == [{"order_id": "ord_mounted_0001", "result_id": "result-other-0001"}]


@pytest.mark.parametrize("cookie", [None, "cookie-unknown-0001"])
@pytest.mark.asyncio
async def test_absent_or_invalid_session_is_the_contract_401_not_team_auth(repository, cookie):
    response = await _post(cookie)
    assert response.status_code == 401
    assert response.json()["code"] == "SESSION_REQUIRED"
    assert response.headers.get("WWW-Authenticate") is None  # handler's 401, not Bearer
    assert repository.calls == []


def test_registry_entry_is_exactly_this_template_and_not_a_root_prefix():
    entry = find_entry(_PATH)
    assert entry is not None
    assert entry.prefix == "/api/visa/voa/orders/{order_id}/checkout-session"
    assert entry.match == "template"
    assert find_entry("/api/visa/voa/staff/orders/ord_mounted_0001/late-resolution") is None
    assert find_entry("/api/visa/voa/orders/ord_mounted_0001/checkout-session/extra") is None


@pytest.mark.asyncio
async def test_rate_limited_refusal_carries_the_contract_envelope_and_privacy_headers(
    repository, monkeypatch
):
    def _refuse(key, limit, window):
        return False, {"limit": limit, "remaining": 0, "reset": 1_900_000_000}

    monkeypatch.setattr(rate_limiter_module.rate_limiter, "is_allowed", _refuse)
    response = await _post("cookie-owner-0001")
    assert response.status_code == 429
    assert response.json() == {
        "code": "RATE_LIMITED",
        "retryable": True,
        "message_key": "garuda_voa.error.rate_limited",
    }
    for name, value in _PRIVACY.items():
        assert response.headers.get(name) == value
    assert response.headers.get("Retry-After")
    assert repository.calls == []


@pytest.mark.asyncio
async def test_rate_limited_refusal_outside_garuda_keeps_the_generic_body(monkeypatch):
    def _refuse(key, limit, window):
        return False, {"limit": limit, "remaining": 0, "reset": 1_900_000_000}

    monkeypatch.setattr(rate_limiter_module.rate_limiter, "is_allowed", _refuse)
    async with AsyncClient(transport=ASGITransport(app=_APP), base_url="http://test") as client:
        response = await client.get("/api/visa/voa-not-garuda/ping")
    if response.status_code == 429:  # limiter ran before any other refusal
        assert "code" not in response.json()
        assert response.json().get("error") == "Rate limit exceeded"
        assert response.headers.get("X-Robots-Tag") is None
