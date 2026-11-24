"""What the staff pages say the `honoured` resolution does is what the code does.

`resolve_late_order` accepts `honoured` only for an order that recorded a
`payment.paid`; it then closes the case and enqueues the release against that
event, which an order paid through the webhook already has. An order with no
such event is refused. These pages are the instructions staff follow on a
money anomaly, so each one is read here as rendered.
"""

from __future__ import annotations

from typing import Any

from backend.services.garuda_orders.outbox_handlers import (
    OrderAnomalyFacts,
    StaffPageChargeWithoutWebhookHandler,
    StaffPageDuplicateChargeHandler,
    StaffPageLatePaidAfterRefundHandler,
)


def _page(handler: type, **over: Any) -> str:
    facts: dict[str, Any] = {
        "order_id": "ord_synthetic0001",
        "case_type": "synthetic_case",
        "price_idr": 790_000,
        "state": "awaiting_payment",
        "late_case_open": True,
        "late_case_charge_id": "ch_synthetic_1",
        "case_resolved_since_trigger": False,
        "late_charge_already_refunded": False,
        "detail": {
            "charge_id": "ch_synthetic_1",
            "provider_status": "settled",
            "second_charge_id": "ch_synthetic_2",
        },
    }
    facts.update(over)
    return handler(None, None)._compose(OrderAnomalyFacts(**facts))


def test_the_duplicate_charge_page_says_honoured_closes_and_refunds_nothing() -> None:
    text = _page(StaffPageDuplicateChargeHandler, state="paid")

    assert "PracticeNotMinted" not in text
    assert "`honoured` resolution closes the case and refunds nothing" in text
    assert "the duplicate money stays where it is" in text


def test_the_duplicate_charge_page_keeps_its_instruction() -> None:
    text = _page(StaffPageDuplicateChargeHandler, state="paid")

    assert "DO NOT close this case with resolveLateOrder" in text
    assert "Refund the duplicate through the provider" in text
    assert "leave the case OPEN and escalate" in text


def test_the_replayed_refunded_charge_page_says_honoured_is_refused() -> None:
    text = _page(
        StaffPageLatePaidAfterRefundHandler,
        state="refunded",
        late_charge_already_refunded=True,
    )

    assert "PracticeNotMinted" not in text
    assert "`honoured` closes the case" not in text
    assert "`honoured` is refused (409)" in text
    assert "never recorded a payment to honour" in text


def test_the_replayed_refunded_charge_page_keeps_its_instruction() -> None:
    text = _page(
        StaffPageLatePaidAfterRefundHandler,
        state="refunded",
        late_charge_already_refunded=True,
    )

    assert "DO NOT REFUND THIS CHARGE" in text
    assert "refund this same charge a SECOND time" in text
    assert "Leave the case OPEN and escalate" in text


def test_a_genuine_late_payment_after_refund_reads_as_before() -> None:
    text = _page(StaffPageLatePaidAfterRefundHandler, state="refunded")

    assert "refund this late charge too, then close via resolveLateOrder" in text
    assert "honoured" not in text


def test_the_charge_without_webhook_page_says_honouring_by_hand_is_refused() -> None:
    text = _page(StaffPageChargeWithoutWebhookHandler)

    assert "left paid with nothing running" not in text
    assert "it closes the case" not in text
    assert "Resolving it as honoured BY HAND is refused (409)" in text
    assert "the case stays open" in text


def test_the_charge_without_webhook_page_keeps_its_two_moves() -> None:
    text = _page(StaffPageChargeWithoutWebhookHandler)

    assert "replay the signed callback" in text
    assert "the only route that also starts the practice" in text
    assert "Use resolveLateOrder here only to refund." in text
    assert "REPLAY ONLY IF YOU HAVE NOT REFUNDED" in text
