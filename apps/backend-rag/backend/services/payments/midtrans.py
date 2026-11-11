"""Midtrans adapter — GARUDA VOA second payment provider (2026-11-11).

Added under Zero's 2026-11-11 re-ruling, which supersedes owner
decision 1's tier scope: Virtual Account and QRIS (previously tier
(c), out of scope) plus cards (D1: VA+QRIS+cards from day one — the
VOA buyer is largely a foreigner paying before arrival) are served
through Midtrans Snap Redirect at the lowest automated fee
(deep research 2026-11-11: VA Rp 4.000 + 11% VAT per successful
transaction; QRIS 0.7% BI MDR; cards 2.9% + Rp 2.000 — all absorbed
into the one all-inclusive `price_idr`, never on the wire, SM-G04).

Design notes the three-seat spec review (Fable/Astra/Carbon, all
REVISE on v1) forced, each verified against Midtrans' official docs:

* Snap Redirect ONLY. Core API VA returns no hosted page, so
  `CheckoutSession.checkout_url` would have no honest value.
* The notification signature SHA512(order_id+status_code+
  gross_amount+server_key) does NOT sign `transaction_status`,
  `fraud_status` or `transaction_id`. A Paid-class body is therefore
  never acted on alone: `verify_paid_event` re-reads the
  authoritative Status API before the repository commits OP-02.
* `pending` is a verified no-op (`WebhookIgnorable` → route 200):
  Midtrans retries any non-2xx, and every VA/QRIS checkout emits a
  pending notification first — 422 there is a retry storm by design.
* Snap creates no transaction until the buyer picks a method, so
  the Status API 404s while the page is still payable: 404 is
  confirmed-unpaid ONLY after the checkout expiry recorded here.
* API refunds do NOT support bank transfer/VA (official Refund
  doc: credit_card, e-wallets, QRIS, kredivo, akulaku only). VA
  refunds run the staff manual-disbursement path: `refund` raises
  `RefundFailed` naming it, and no resolution is ever recorded
  for a refund that did not happen (port contract).
* Identities: provider_session_id = order_id (the Status API key),
  provider_charge_id = transaction_id.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from backend.services.payments.port import (
    ChargeConfirmation,
    CheckoutSession,
    NormalizedFailureEvent,
    NormalizedPaidEvent,
    NormalizedRefundEvent,
    RefundFailed,
    WebhookIgnorable,
    WebhookSignatureInvalid,
    WebhookUnparseable,
)
from backend.services.payments.terminal_taxonomy import (
    FailureOutcome,
    classify,
)

logger = logging.getLogger(__name__)


class WebhookTransientError(Exception):
    """The authoritative check could not run (5xx/transport).

    NOT a refusal: the route answers 503 so the provider retries,
    and nothing is persisted — persisting the Paid identity here
    would swallow the retry as a duplicate.
    """

_SANDBOX_API = "https://api.sandbox.midtrans.com"
_LIVE_API = "https://api.midtrans.com"
# Snap lives on the APP host, Status/Refund on the API host — one
# base for both was gate finding #1 (every checkout would 404).
_SANDBOX_SNAP = "https://app.sandbox.midtrans.com"
_LIVE_SNAP = "https://app.midtrans.com"
_CHECKOUT_TTL_MINUTES = 60

#: Snap methods for v1 (D1). Explicit, never the `bank_transfer`
#: alias — it would include BCA VA, whose Midtrans activation is a
#: separate collaborative process.
ENABLED_PAYMENTS = [
    "bri_va",
    "bni_va",
    "echannel",
    "permata_va",
    "cimb_va",
    "bsi_va",
    "other_qris",
    "credit_card",
]

#: The only transaction_status values that positively prove no
#: charge was accepted (port contract: allow-list, never negation).
_STATUSES_WITH_NO_ACCEPTED_CHARGE = frozenset({"expire", "cancel", "deny", "failure"})

#: status_code <-> transaction_status coherence (Midtrans pairs).
_STATUS_CODE_PAIRS = {
    "200": {"settlement", "capture", "refund", "partial_refund", "chargeback", "partial_chargeback"},
    "201": {"pending"},
    "202": {"deny", "cancel", "failure", "expire"},
    "407": {"expire"},
}

#: payment_type values whose refunds the API does not support.
_VA_PAYMENT_TYPES = frozenset({"bank_transfer", "echannel"})


@dataclass(frozen=True, slots=True)
class MidtransFeeConfig:
    """Operator configuration, never read by mapping code (SM-G04).

    Exists so a repricing (cf. Xendit's Oct-2026 platform fee) is an
    env change, not a code change. Figures are ex-VAT; VAT 11%
    applies on top at settlement.
    """

    va_fixed_idr: int = 4000
    qris_bps: int = 70
    card_bps: int = 290
    card_fixed_idr: int = 2000


class MidtransPaymentProvider:
    """`PaymentProvider` against Midtrans Snap (sandbox by default;

    live only when a `Mid-server-` key AND `live_enabled` agree —
    the same two-switch discipline as the Xendit adapter).
    """

    name = "midtrans"

    def __init__(
        self,
        *,
        server_key: str,
        fee_config: MidtransFeeConfig,
        public_base_url: str,
        client: httpx.AsyncClient,
        live_enabled: bool = False,
    ) -> None:
        if server_key.startswith("Mid-server-"):
            if not live_enabled:
                raise ValueError(
                    "MidtransPaymentProvider was given a production (Mid-server-) "
                    "server key but live_enabled is False: set GARUDA_PAYMENTS_LIVE=true "
                    "to arm live mode, or use a sandbox (SB-Mid-server-) key"
                )
            self._mode = "live"
            self._base_url = _LIVE_API
            self._snap_base_url = _LIVE_SNAP
        elif server_key.startswith("SB-Mid-server-"):
            if live_enabled:
                raise ValueError(
                    "MidtransPaymentProvider was given a sandbox (SB-Mid-server-) "
                    "server key but live_enabled is True (GARUDA_PAYMENTS_LIVE=true): "
                    "a live-mode deploy requires a production server key"
                )
            self._mode = "sandbox"
            self._base_url = _SANDBOX_API
            self._snap_base_url = _SANDBOX_SNAP
        else:
            raise ValueError(
                "MidtransPaymentProvider requires a sandbox (SB-Mid-server-) or "
                "production (Mid-server-) server key"
            )
        self._server_key = server_key
        self._fee_config = fee_config  # operator visibility only
        self._public_base_url = public_base_url.rstrip("/")
        self._client = client

    @property
    def mode(self) -> str:
        """`"live"` or `"sandbox"` — set once in `__init__`, never mutated."""
        return self._mode

    async def create_checkout_session(
        self,
        *,
        order_id: str,
        price_idr: int,
        idempotency_key: str,
    ) -> CheckoutSession:
        if price_idr <= 0:
            raise ValueError("price_idr must be a positive all-inclusive integer")
        expires_at = datetime.now(UTC) + timedelta(minutes=_CHECKOUT_TTL_MINUTES)
        return_nonce = secrets.token_urlsafe(32)
        finish_url = (
            f"{self._public_base_url}/visa/voa/orders/{quote(order_id, safe='')}"
            f"/return?return_nonce={quote(return_nonce, safe='')}"
        )
        response = await self._client.post(
            f"{self._snap_base_url}/snap/v1/transactions",
            auth=(self._server_key, ""),
            headers={"X-Idempotency-Key": idempotency_key},
            json={
                "transaction_details": {"order_id": order_id, "gross_amount": price_idr},
                "enabled_payments": ENABLED_PAYMENTS,
                "callbacks": {"finish": finish_url},
                "expiry": {
                    # start_time pins Midtrans' expiry clock to OUR
                    # creation instant; without it the countdown can
                    # start when the buyer picks a method, and the
                    # two clocks diverge (gate finding).
                    "start_time": (expires_at - timedelta(minutes=_CHECKOUT_TTL_MINUTES))
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S %z"),
                    "unit": "minutes",
                    "duration": _CHECKOUT_TTL_MINUTES,
                },
            },
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        redirect_url = body.get("redirect_url")
        if not isinstance(redirect_url, str) or not redirect_url:
            raise WebhookUnparseable("Midtrans Snap response missing redirect_url")
        return CheckoutSession(
            provider_session_id=order_id,
            checkout_url=redirect_url,
            expires_at=expires_at,
        )

    def _expected_signature(self, body: dict[str, Any]) -> str:
        # RAW strings, exactly as received — gross_amount arrives as
        # "790000.00" and re-serializing a number would break the digest.
        raw = (
            str(body.get("order_id", ""))
            + str(body.get("status_code", ""))
            + str(body.get("gross_amount", ""))
            + self._server_key
        )
        return hashlib.sha512(raw.encode()).hexdigest()

    def verify_signature(self, *, raw_body: bytes, headers: dict[str, str]) -> None:
        try:
            body: dict[str, Any] = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookSignatureInvalid("notification body is not valid JSON") from exc
        received = body.get("signature_key")
        if not isinstance(received, str) or not received:
            raise WebhookSignatureInvalid("notification missing signature_key")
        if not hmac.compare_digest(received, self._expected_signature(body)):
            raise WebhookSignatureInvalid("notification signature mismatch")

    @staticmethod
    def _amount_idr(body: dict[str, Any]) -> int:
        raw = body.get("gross_amount")
        try:
            amount = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise WebhookUnparseable(f"gross_amount is not a number: {raw!r}") from exc
        if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
            raise WebhookUnparseable(f"gross_amount is not a positive integral IDR value: {raw!r}")
        return int(amount)

    def parse_event(
        self, *, raw_body: bytes, headers: dict[str, str]
    ) -> NormalizedPaidEvent | NormalizedFailureEvent | NormalizedRefundEvent:
        try:
            body: dict[str, Any] = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookUnparseable("notification body is not valid JSON") from exc

        order_id = body.get("order_id")
        transaction_id = body.get("transaction_id")
        status_code = str(body.get("status_code", ""))
        tx_status = body.get("transaction_status")
        if not isinstance(order_id, str) or not order_id:
            raise WebhookUnparseable("notification missing order_id")
        if not isinstance(transaction_id, str) or not transaction_id:
            raise WebhookUnparseable("notification missing transaction_id")
        if tx_status not in _STATUS_CODE_PAIRS.get(status_code, set()):
            raise WebhookUnparseable(
                f"status_code {status_code!r} incoherent with transaction_status {tx_status!r}"
            )
        event_id = f"{transaction_id}:{tx_status}:{status_code}"

        if tx_status == "pending":
            raise WebhookIgnorable("checkout created, no money moved yet")
        if tx_status in ("settlement", "capture"):
            if tx_status == "capture" and body.get("fraud_status") != "accept":
                raise WebhookUnparseable(
                    f"capture without fraud_status=accept: {body.get('fraud_status')!r}"
                )
            return NormalizedPaidEvent(
                # capture and settlement are two hops of ONE charge:
                # the event identity is the charge, so the second hop
                # dedupes in the inbox instead of reading as OP-08
                # duplicate-charge (normal card status cycle).
                provider_event_id=f"{transaction_id}:paid",
                provider_charge_id=transaction_id,
                provider_session_id=order_id,
                amount_idr=self._amount_idr(body),
                currency=str(body.get("currency", "IDR")),
            )
        if tx_status in ("deny", "cancel", "expire", "failure"):
            outcome = {
                "deny": FailureOutcome.DECLINED_BY_ISSUER,
                "cancel": FailureOutcome.EXPIRED,
                "expire": FailureOutcome.EXPIRED,
                "failure": FailureOutcome.PROVIDER_UNAVAILABLE,
            }[tx_status]
            return NormalizedFailureEvent(
                provider_event_id=event_id,
                provider_session_id=order_id,
                failure=classify(outcome),
            )
        if tx_status in ("refund", "chargeback"):
            self._amount_idr(body)  # validate shape even though the port carries no amount
            return NormalizedRefundEvent(
                provider_event_id=event_id,
                provider_refund_id=transaction_id,
                provider_charge_id=transaction_id,
                provider_session_id=order_id,
            )
        # partial_refund / partial_chargeback and anything unlisted:
        # never a full-refund event (the consumer would mark the whole
        # order refunded). Loud, human, on the record.
        raise WebhookUnparseable(f"unhandled Midtrans transaction_status: {tx_status!r}")

    async def _fetch_status(self, order_id: str) -> tuple[int, dict[str, Any]]:
        try:
            response = await self._client.get(
                f"{self._base_url}/v2/{quote(order_id, safe='')}/status",
                auth=(self._server_key, ""),
            )
        except httpx.HTTPError as exc:
            raise WebhookTransientError(f"Status API unreachable: {exc}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    async def verify_refund_event(
        self, event: NormalizedRefundEvent
    ) -> NormalizedRefundEvent:
        """A refund body is no more self-proving than a Paid body:
        `transaction_status` is unsigned, so a settlement body with
        the status flipped to `refund` would otherwise record a
        refund that never happened. The Status API must agree."""
        http_status, body = await self._fetch_status(event.provider_session_id)
        if http_status >= 500:
            raise WebhookTransientError(f"Status API HTTP {http_status}")
        tx_status = body.get("transaction_status")
        if http_status != 200 or tx_status not in (
            "refund",
            "chargeback",
            "partial_refund",
            "partial_chargeback",
        ):
            raise WebhookUnparseable(
                f"webhook claimed refund but Status API says {tx_status!r} "
                f"(HTTP {http_status}) for order {event.provider_session_id}"
            )
        if str(body.get("transaction_id", "")) != event.provider_charge_id:
            raise WebhookUnparseable("Status API transaction_id disagrees with refund body")
        if tx_status.startswith("partial"):
            raise WebhookUnparseable("authoritative status is a PARTIAL refund — staff case")
        return event

    async def verify_paid_event(self, event: NormalizedPaidEvent) -> NormalizedPaidEvent:
        """Re-read the authoritative state before OP-02 acts on a Paid body.

        The notification signature does not cover `transaction_status`
        or `transaction_id`, so a body alone — even a correctly signed
        one — is not proof of settlement. Disagreement is
        `WebhookUnparseable`: loud, quarantined, never a practice.
        """
        http_status, body = await self._fetch_status(event.provider_session_id)
        if http_status >= 500:
            # Transient: the provider must RETRY, so the route
            # answers 503 and nothing is written to the inbox — a
            # quarantined Paid identity would swallow the retry as
            # a duplicate (gate finding).
            raise WebhookTransientError(
                f"authoritative status check HTTP {http_status} "
                f"for order {event.provider_session_id}"
            )
        if http_status != 200:
            raise WebhookUnparseable(
                f"authoritative status check for order {event.provider_session_id} "
                f"answered HTTP {http_status}"
            )
        tx_status = body.get("transaction_status")
        if tx_status not in ("settlement", "capture"):
            raise WebhookUnparseable(
                f"webhook claimed Paid but Status API says {tx_status!r} "
                f"for order {event.provider_session_id}"
            )
        if tx_status == "capture" and body.get("fraud_status") != "accept":
            raise WebhookUnparseable("Status API capture without fraud_status=accept")
        if str(body.get("transaction_id", "")) != event.provider_charge_id:
            raise WebhookUnparseable("Status API transaction_id disagrees with webhook body")
        if self._amount_idr(body) != event.amount_idr:
            raise WebhookUnparseable("Status API amount disagrees with webhook body")
        return event

    async def confirm_no_successful_charge(self, *, provider_session_id: str) -> ChargeConfirmation:
        http_status, body = await self._fetch_status(provider_session_id)
        tx_status = body.get("transaction_status")
        status = str(tx_status) if tx_status is not None else None
        charge_id = body.get("transaction_id")
        if http_status == 404:
            # Snap 404 = the buyer has not chosen a method yet; the
            # page may still be payable. NEVER confirmed-unpaid from
            # here: an in-process expiry record would flip the answer
            # across a restart (gate finding). OP-04 then opens a
            # late case instead of expiring — the safe direction.
            return ChargeConfirmation(
                confirmed_unpaid=False,
                provider_charge_id=None,
                provider_status="not_found",
            )
        return ChargeConfirmation(
            confirmed_unpaid=status in _STATUSES_WITH_NO_ACCEPTED_CHARGE,
            provider_charge_id=str(charge_id) if charge_id else None,
            provider_status=status,
        )

    async def refund(self, *, provider_charge_id: str, idempotency_key: str) -> str:
        if not provider_charge_id:
            raise RefundFailed("refund requested with no charge id")
        # The Refund API addresses a transaction by order OR transaction
        # id; learn the method + amount from the authoritative status
        # first, because VA has no API refund at all.
        http_status, body = await self._fetch_status_by_charge(provider_charge_id)
        if http_status != 200:
            raise RefundFailed(
                f"cannot refund charge {provider_charge_id}: status check HTTP {http_status}"
            )
        payment_type = str(body.get("payment_type", ""))
        if payment_type in _VA_PAYMENT_TYPES:
            raise RefundFailed(
                "VA_REFUND_MANUAL: Midtrans Refund API does not support bank "
                "transfer/VA — refund by manual bank disbursement under the "
                "staff honour-or-refund path; this case stays open until a "
                "human closes it"
            )
        order_id = str(body.get("order_id", ""))
        amount = self._amount_idr(body)
        try:
            response = await self._client.post(
                f"{self._base_url}/v2/{quote(order_id, safe='')}/refund",
                auth=(self._server_key, ""),
                json={"refund_key": idempotency_key, "amount": amount, "reason": "requested_by_merchant"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RefundFailed(f"Midtrans refund failed for charge {provider_charge_id}") from exc
        refund_body: dict[str, Any] = response.json()
        # Application-level proof, not just HTTP 2xx. The documented
        # success shape carries refund_chargeback_id / refund_amount /
        # refund_key — an earlier draft demanded a `refunds` list the
        # API never sends and would have refused real refunds while
        # the money moved (gate finding).
        refund_id = refund_body.get("refund_chargeback_id") or refund_body.get("refund_key")
        if refund_body.get("status_code") not in ("200", 200) or not refund_id:
            raise RefundFailed("Midtrans refund response carries no refund record")
        return str(refund_id)

    async def _fetch_status_by_charge(self, charge_id: str) -> tuple[int, dict[str, Any]]:
        # The Status endpoint accepts a transaction_id in place of
        # order_id; that is the lookup the refund path needs.
        return await self._fetch_status(charge_id)


__all__ = [
    "ENABLED_PAYMENTS",
    "MidtransFeeConfig",
    "MidtransPaymentProvider",
    "WebhookTransientError",
]
