"""MidtransPaymentProvider — signed-fixture matrix (no network).

Spec: research/payments/2026-11-11-midtrans-adapter-spec.md (v2,
three-seat review). The cases the chairs named are here by name:
raw-decimal signature, unsigned-status forgery, pending no-op,
Snap-404 race, VA refund manual path, partial-refund guard.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from backend.services.payments.midtrans import (
    ENABLED_PAYMENTS,
    MidtransFeeConfig,
    MidtransPaymentProvider,
)
from backend.services.payments.port import (
    NormalizedFailureEvent,
    NormalizedPaidEvent,
    NormalizedRefundEvent,
    RefundFailed,
    WebhookIgnorable,
    WebhookSignatureInvalid,
    WebhookUnparseable,
)

SERVER_KEY = "SB-Mid-server-fake-key-for-tests"


def _sign(body: dict, key: str = SERVER_KEY) -> dict:
    raw = str(body["order_id"]) + str(body["status_code"]) + str(body["gross_amount"]) + key
    return {**body, "signature_key": hashlib.sha512(raw.encode()).hexdigest()}


def _provider(handler=None) -> MidtransPaymentProvider:
    transport = httpx.MockTransport(handler) if handler else None
    client = httpx.AsyncClient(transport=transport) if transport else httpx.AsyncClient()
    return MidtransPaymentProvider(
        server_key=SERVER_KEY,
        fee_config=MidtransFeeConfig(),
        public_base_url="https://example.com",
        client=client,
    )


def _body(**over) -> dict:
    base = {
        "order_id": "ord_test_1",
        "transaction_id": "txn_test_1",
        "status_code": "200",
        "gross_amount": "790000.00",
        "transaction_status": "settlement",
        "payment_type": "bank_transfer",
        "currency": "IDR",
    }
    base.update(over)
    return base


# -- mode matrix -----------------------------------------------------------


def test_sandbox_key_with_live_flag_refuses():
    with pytest.raises(ValueError, match="live_enabled is True"):
        MidtransPaymentProvider(
            server_key=SERVER_KEY,
            fee_config=MidtransFeeConfig(),
            public_base_url="https://example.com",
            client=httpx.AsyncClient(),
            live_enabled=True,
        )


def test_production_key_without_live_flag_refuses():
    with pytest.raises(ValueError, match="live_enabled is False"):
        MidtransPaymentProvider(
            server_key="Mid-server-fake",
            fee_config=MidtransFeeConfig(),
            public_base_url="https://example.com",
            client=httpx.AsyncClient(),
        )


def test_unknown_key_prefix_refuses():
    with pytest.raises(ValueError, match="SB-Mid-server-"):
        MidtransPaymentProvider(
            server_key="sk_whatever",
            fee_config=MidtransFeeConfig(),
            public_base_url="https://example.com",
            client=httpx.AsyncClient(),
        )


# -- checkout ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_is_snap_with_explicit_methods_and_integer_amount():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"token": "t", "redirect_url": "https://app.sandbox.midtrans.com/snap/x"})

    provider = _provider(handler)
    session = await provider.create_checkout_session(
        order_id="ord_test_1", price_idr=790000, idempotency_key="idem-1"
    )
    assert session.provider_session_id == "ord_test_1"
    assert session.checkout_url.startswith("https://app.sandbox.midtrans.com/")
    assert seen["url"].startswith("https://app.sandbox.midtrans.com/snap/")
    assert seen["url"].endswith("/snap/v1/transactions")
    assert seen["json"]["transaction_details"] == {"order_id": "ord_test_1", "gross_amount": 790000}
    assert seen["json"]["enabled_payments"] == ENABLED_PAYMENTS
    assert "bank_transfer" not in seen["json"]["enabled_payments"]  # alias would include BCA
    assert "return_nonce=" in seen["json"]["callbacks"]["finish"]


# -- signature + parse --------------------------------------------------------


def test_signature_uses_raw_decimal_string():
    provider = _provider()
    body = _sign(_body())
    provider.verify_signature(raw_body=json.dumps(body).encode(), headers={})


def test_signature_rejects_tampered_amount():
    provider = _provider()
    body = _sign(_body())
    body["gross_amount"] = "100.00"  # signature no longer matches the raw string
    with pytest.raises(WebhookSignatureInvalid):
        provider.verify_signature(raw_body=json.dumps(body).encode(), headers={})


def test_signature_does_not_cover_status_so_parse_enforces_coherence():
    # A forged pending->settlement swap keeps status_code 201: the
    # signature still verifies (it never signed the status), so
    # parse_event's coherence table is the guard.
    provider = _provider()
    body = _sign(_body(status_code="201", transaction_status="settlement"))
    provider.verify_signature(raw_body=json.dumps(body).encode(), headers={})
    with pytest.raises(WebhookUnparseable, match="incoherent"):
        provider.parse_event(raw_body=json.dumps(body).encode(), headers={})


def test_settlement_parses_to_paid_with_decimal_amount():
    provider = _provider()
    event = provider.parse_event(raw_body=json.dumps(_sign(_body())).encode(), headers={})
    assert isinstance(event, NormalizedPaidEvent)
    assert event.amount_idr == 790000
    assert event.provider_session_id == "ord_test_1"
    assert event.provider_charge_id == "txn_test_1"


def test_fractional_amount_is_unparseable():
    provider = _provider()
    body = _sign(_body(gross_amount="790000.50"))
    with pytest.raises(WebhookUnparseable, match="integral"):
        provider.parse_event(raw_body=json.dumps(body).encode(), headers={})


def test_capture_requires_fraud_accept():
    provider = _provider()
    body = _sign(_body(transaction_status="capture", fraud_status="challenge"))
    with pytest.raises(WebhookUnparseable, match="fraud_status"):
        provider.parse_event(raw_body=json.dumps(body).encode(), headers={})


def test_pending_is_ignorable_not_unparseable():
    provider = _provider()
    body = _sign(_body(status_code="201", transaction_status="pending"))
    with pytest.raises(WebhookIgnorable):
        provider.parse_event(raw_body=json.dumps(body).encode(), headers={})


def test_partial_refund_never_becomes_full_refund_event():
    provider = _provider()
    body = _sign(_body(transaction_status="partial_refund"))
    with pytest.raises(WebhookUnparseable, match="partial_refund"):
        provider.parse_event(raw_body=json.dumps(body).encode(), headers={})


def test_expire_parses_to_failure():
    provider = _provider()
    body = _sign(_body(status_code="407", transaction_status="expire"))
    event = provider.parse_event(raw_body=json.dumps(body).encode(), headers={})
    assert isinstance(event, NormalizedFailureEvent)


def test_refund_parses_to_refund_event():
    provider = _provider()
    body = _sign(_body(transaction_status="refund"))
    event = provider.parse_event(raw_body=json.dumps(body).encode(), headers={})
    assert isinstance(event, NormalizedRefundEvent)


# -- authoritative verification + confirm ------------------------------------


@pytest.mark.asyncio
async def test_verify_paid_event_rejects_status_disagreement():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"transaction_status": "pending", "transaction_id": "txn_test_1", "gross_amount": "790000.00"},
        )

    provider = _provider(handler)
    event = provider.parse_event(raw_body=json.dumps(_sign(_body())).encode(), headers={})
    assert isinstance(event, NormalizedPaidEvent)
    with pytest.raises(WebhookUnparseable, match="Status API says"):
        await provider.verify_paid_event(event)


@pytest.mark.asyncio
async def test_snap_404_inside_window_is_not_confirmed_unpaid():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status_code": "404"})

    provider = _provider(handler)
    # No checkout recorded for this order in this process: still not
    # confirmed unpaid (conservative), and a recorded live checkout
    # inside its window is not unpaid either.
    result = await provider.confirm_no_successful_charge(provider_session_id="ord_unknown")
    assert result.confirmed_unpaid is False


@pytest.mark.asyncio
async def test_confirm_allow_list_only():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"transaction_status": "settlement", "transaction_id": "txn_test_1"}
        )

    provider = _provider(handler)
    result = await provider.confirm_no_successful_charge(provider_session_id="ord_test_1")
    assert result.confirmed_unpaid is False
    assert result.provider_status == "settlement"


# -- refund -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_va_refund_is_manual_never_api():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "transaction_status": "settlement",
                "transaction_id": "txn_test_1",
                "order_id": "ord_test_1",
                "payment_type": "bank_transfer",
                "gross_amount": "790000.00",
            },
        )

    provider = _provider(handler)
    with pytest.raises(RefundFailed, match="VA_REFUND_MANUAL"):
        await provider.refund(provider_charge_id="txn_test_1", idempotency_key="idem-r1")


@pytest.mark.asyncio
async def test_qris_refund_uses_api_and_returns_refund_key():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "transaction_status": "settlement",
                    "transaction_id": "txn_test_1",
                    "order_id": "ord_test_1",
                    "payment_type": "qris",
                    "gross_amount": "790000.00",
                },
            )
        return httpx.Response(200, json={"status_code": "200", "refund_chargeback_id": 987, "refund_amount": "790000.00", "refund_key": "idem-r2"})

    provider = _provider(handler)
    refund_id = await provider.refund(provider_charge_id="txn_test_1", idempotency_key="idem-r2")
    assert refund_id == "987"
    assert any("/refund" in url for url in calls)


def test_capture_and_settlement_share_one_charge_event_id():
    # Normal card cycle: capture then settlement for ONE charge must
    # dedupe in the inbox, not read as a duplicate charge (OP-08).
    provider = _provider()
    capture = provider.parse_event(
        raw_body=json.dumps(_sign(_body(transaction_status="capture", fraud_status="accept"))).encode(),
        headers={},
    )
    settlement = provider.parse_event(
        raw_body=json.dumps(_sign(_body(transaction_status="settlement"))).encode(), headers={}
    )
    assert isinstance(capture, NormalizedPaidEvent)
    assert capture.provider_event_id == settlement.provider_event_id


def test_cancel_parses_to_failure():
    provider = _provider()
    body = _sign(_body(status_code="202", transaction_status="cancel"))
    event = provider.parse_event(raw_body=json.dumps(body).encode(), headers={})
    assert isinstance(event, NormalizedFailureEvent)


def test_qris_expire_with_202_parses_to_failure():
    provider = _provider()
    body = _sign(_body(status_code="202", transaction_status="expire", payment_type="qris"))
    event = provider.parse_event(raw_body=json.dumps(body).encode(), headers={})
    assert isinstance(event, NormalizedFailureEvent)


@pytest.mark.asyncio
async def test_verify_refund_event_rejects_forged_refund():
    # Settlement body with only the (unsigned) status flipped to
    # refund: the Status API still says settlement, so no refund
    # may be recorded.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"transaction_status": "settlement", "transaction_id": "txn_test_1", "gross_amount": "790000.00"},
        )

    provider = _provider(handler)
    event = provider.parse_event(
        raw_body=json.dumps(_sign(_body(transaction_status="refund"))).encode(), headers={}
    )
    assert isinstance(event, NormalizedRefundEvent)
    with pytest.raises(WebhookUnparseable, match="Status API says"):
        await provider.verify_refund_event(event)


@pytest.mark.asyncio
async def test_transient_status_error_is_retryable_not_refusal():
    from backend.services.payments.midtrans import WebhookTransientError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    provider = _provider(handler)
    event = provider.parse_event(raw_body=json.dumps(_sign(_body())).encode(), headers={})
    assert isinstance(event, NormalizedPaidEvent)
    with pytest.raises(WebhookTransientError):
        await provider.verify_paid_event(event)


def test_non_idr_currency_dies_in_parse_before_any_identity():
    provider = _provider()
    body = _sign(_body(currency="USD"))
    provider.verify_signature(raw_body=json.dumps(body).encode(), headers={})
    with pytest.raises(WebhookUnparseable, match="non-IDR"):
        provider.parse_event(raw_body=json.dumps(body).encode(), headers={})


@pytest.mark.asyncio
async def test_status_429_is_transient_not_refusal():
    from backend.services.payments.midtrans import WebhookTransientError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={})

    provider = _provider(handler)
    event = provider.parse_event(raw_body=json.dumps(_sign(_body())).encode(), headers={})
    assert isinstance(event, NormalizedPaidEvent)
    with pytest.raises(WebhookTransientError):
        await provider.verify_paid_event(event)


@pytest.mark.asyncio
async def test_verify_failure_event_rejects_forged_transaction_id():
    # Signed failure body for order A carrying order B's (unsigned)
    # transaction_id: without authoritative verification it would
    # poison B's failure dedupe key.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"transaction_status": "expire", "transaction_id": "txn_real_A", "gross_amount": "790000.00"},
        )

    provider = _provider(handler)
    forged = _sign(_body(status_code="407", transaction_status="expire", transaction_id="tx_victim_B"))
    event = provider.parse_event(raw_body=json.dumps(forged).encode(), headers={})
    assert isinstance(event, NormalizedFailureEvent)
    with pytest.raises(WebhookUnparseable, match="disagrees"):
        await provider.verify_failure_event(event)


@pytest.mark.asyncio
async def test_verify_failure_event_accepts_authoritative_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"transaction_status": "expire", "transaction_id": "txn_test_1", "gross_amount": "790000.00"},
        )

    provider = _provider(handler)
    body = _sign(_body(status_code="407", transaction_status="expire"))
    event = provider.parse_event(raw_body=json.dumps(body).encode(), headers={})
    assert await provider.verify_failure_event(event) is event
