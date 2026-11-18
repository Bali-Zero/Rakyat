"""Tracker "Pay now" — `resume_checkout_session` re-reads the bound invoice.

Open only for `PENDING` with an expiry more than 60 s away; every other
status (paid, settled, expired, unknown) is closed and carries no URL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend.services.payments.xendit import XenditFeeConfig, XenditPaymentProvider

_URL = "https://checkout-staging.xendit.co/web/inv-fake-0001"


def _provider(handler) -> XenditPaymentProvider:
    return XenditPaymentProvider(
        secret_key="xnd_development_fake_key_for_tests",
        callback_verification_token="fake-token",
        public_base_url="https://example.com",
        fee_config=XenditFeeConfig(percentage_bps=350, fixed_idr=6000),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")


def _invoice(status: str, *, expires_in: timedelta = timedelta(minutes=30)) -> dict:
    return {"id": "inv-1", "status": status, "invoice_url": _URL, "expiry_date": _iso(expires_in)}


@pytest.mark.asyncio
async def test_pending_invoice_is_open_and_returns_its_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_invoice("PENDING"))

    status = await _provider(handler).resume_checkout_session(provider_session_id="inv/1")
    assert status.is_open is True
    assert status.checkout_url == _URL
    assert len(seen) == 1
    assert seen[0].method == "GET"
    # The session id is a path segment, never path syntax.
    assert seen[0].url.raw_path == b"/v2/invoices/inv%2F1"


@pytest.mark.parametrize("status_value", ["PAID", "SETTLED", "EXPIRED", "FAILED", "SOMETHING_NEW"])
@pytest.mark.asyncio
async def test_every_status_but_pending_is_closed_without_a_url(status_value: str) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=_invoice(status_value)))
    status = await provider.resume_checkout_session(provider_session_id="inv-1")
    assert status.is_open is False
    assert status.checkout_url is None
    assert status.provider_status == status_value


@pytest.mark.asyncio
async def test_pending_invoice_about_to_expire_is_closed() -> None:
    body = _invoice("PENDING", expires_in=timedelta(seconds=20))
    status = await _provider(
        lambda request: httpx.Response(200, json=body)
    ).resume_checkout_session(provider_session_id="inv-1")
    assert status.is_open is False
    assert status.checkout_url is None


@pytest.mark.asyncio
async def test_pending_invoice_without_a_readable_expiry_is_closed() -> None:
    body = {"id": "inv-1", "status": "PENDING", "invoice_url": _URL, "expiry_date": "soon"}
    status = await _provider(
        lambda request: httpx.Response(200, json=body)
    ).resume_checkout_session(provider_session_id="inv-1")
    assert status.is_open is False


@pytest.mark.asyncio
async def test_provider_http_error_propagates() -> None:
    provider = _provider(lambda request: httpx.Response(500, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        await provider.resume_checkout_session(provider_session_id="inv-1")


def test_checkout_hosts_are_the_xendit_checkout_hosts_only() -> None:
    provider = _provider(lambda request: httpx.Response(200, json={}))
    assert provider.checkout_hosts == frozenset(
        {"checkout.xendit.co", "checkout-staging.xendit.co"}
    )
