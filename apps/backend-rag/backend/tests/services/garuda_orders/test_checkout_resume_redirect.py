"""Unit regression tests for resume checkout redirect behavior in GarudaOrderRepository.

Verifies without requiring a migrated Postgres instance that resuming an
already-`awaiting_payment` (or `paid`) order with a fresh Idempotency-Key
returns the real order state and ``checkout_url=None`` instead of fabricating
an unnavigable ``pending-resume:<provider_session_id>`` URL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from backend.services.garuda_flow.intake import CaseType
from backend.services.garuda_orders.idempotency import (
    canonical_payload_sha256,
    scoped_key_sha256,
)
from backend.services.garuda_orders.models import Applicant
from backend.services.garuda_orders.ports import ReviewedCheckSnapshot
from backend.services.garuda_orders.repository import GarudaOrderRepository
from backend.services.payments.port import ChargeConfirmation, CheckoutSession


class _FakeLookup:
    async def get_reviewed_check(self, result_id: str) -> ReviewedCheckSnapshot | None:
        return ReviewedCheckSnapshot(
            result_id=result_id,
            case_type=CaseType.ISSUANCE,
            review_confirmed=True,
        )


class _FakeProvider:
    def __init__(self) -> None:
        self.create_calls: list[str] = []

    async def create_checkout_session(
        self,
        *,
        order_id: str,
        price_idr: int,
        idempotency_key: str,
    ) -> CheckoutSession:
        self.create_calls.append(order_id)
        return CheckoutSession(
            provider_session_id=f"sess-opaque-{len(self.create_calls)}",
            checkout_url=f"https://sandbox.provider.invalid/checkout/{order_id}",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    def verify_signature(self, *, raw_body: bytes, headers: dict[str, str]) -> None:
        return None

    def parse_event(self, *, raw_body: bytes, headers: dict[str, str]) -> Any:
        raise NotImplementedError

    async def confirm_no_successful_charge(self, *, provider_session_id: str) -> ChargeConfirmation:
        return ChargeConfirmation(confirmed_unpaid=True)


@dataclass
class _InMemoryState:
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency_rows: dict[bytes, dict[str, Any]] = field(default_factory=dict)


class _FakeConnection:
    def __init__(self, state: _InMemoryState) -> None:
        self._state = state

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "active_garuda_order_policy_available" in query:
            return True
        raise AssertionError(f"Unexpected fetchval SQL: {query}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "INSERT INTO garuda_order_idempotency" in query:
            key_sha256, payload_sha256 = args
            if key_sha256 in self._state.idempotency_rows:
                return None
            self._state.idempotency_rows[key_sha256] = {
                "key_sha256": key_sha256,
                "canonical_payload_sha256": payload_sha256,
                "order_id": None,
                "response_status": None,
                "response_body": None,
                "completed_at": None,
            }
            return {"key_sha256": key_sha256}

        if "FROM garuda_order_idempotency" in query:
            (key_sha256,) = args
            return self._state.idempotency_rows.get(key_sha256)

        if "SELECT order_id FROM garuda_orders" in query:
            (result_id_ref,) = args
            for order in self._state.orders.values():
                if order["result_id_ref"] == result_id_ref and order["state"] in (
                    "created",
                    "awaiting_payment",
                    "paid",
                ):
                    return {"order_id": order["order_id"]}
            return None

        if "SELECT state, price_idr FROM garuda_orders" in query:
            (order_id,) = args
            order = self._state.orders.get(order_id)
            if order is None:
                return None
            return {"state": order["state"], "price_idr": order["price_idr"]}

        if "UPDATE garuda_orders" in query and "SET state = 'awaiting_payment'" in query:
            order_id, provider_session_id, checkout_expires_at, provider = args
            order = self._state.orders.get(order_id)
            if order is None or order["state"] != "created":
                return None
            order["state"] = "awaiting_payment"
            order["provider_session_id"] = provider_session_id
            order["checkout_expires_at"] = checkout_expires_at
            order["provider"] = provider
            return {"order_id": order_id}

        raise AssertionError(f"Unexpected fetchrow SQL: {query}")

    async def execute(self, query: str, *args: Any) -> str:
        if "UPDATE garuda_order_idempotency SET order_id = $2" in query:
            key_sha256, order_id = args
            row = self._state.idempotency_rows[key_sha256]
            if row["order_id"] is None:
                row["order_id"] = order_id
            return "UPDATE 1"

        if "INSERT INTO garuda_orders" in query:
            (
                order_id,
                result_id_ref,
                case_type,
                _full_name,
                _contact,
                _tel,
                _ref,
                price_idr,
                price_catalogue_key,
            ) = args
            self._state.orders[order_id] = {
                "order_id": order_id,
                "result_id_ref": result_id_ref,
                "case_type": case_type,
                "price_idr": price_idr,
                "price_catalogue_key": price_catalogue_key,
                "state": "created",
                "provider": None,
                "provider_session_id": None,
                "checkout_expires_at": None,
            }
            return "INSERT 0 1"

        if "INSERT INTO garuda_order_journal" in query:
            return "INSERT 0 1"

        if "INSERT INTO garuda_order_outbox" in query:
            return "INSERT 0 1"

        if "UPDATE garuda_order_idempotency" in query and "completed_at" in query:
            key_sha256, response_status, response_body = args
            row = self._state.idempotency_rows[key_sha256]
            row["response_status"] = response_status
            row["response_body"] = response_body
            row["completed_at"] = datetime.now(UTC)
            return "UPDATE 1"

        raise AssertionError(f"Unexpected execute SQL: {query}")


class _FakePool:
    def __init__(self, state: _InMemoryState) -> None:
        self._state = state

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConnection(self._state)


def _synthetic_applicant() -> Applicant:
    return Applicant(
        full_name="applicant-name",
        email="applicant-contact",
        phone="applicant-tel",
        passport_number="applicant-ref",
    )


@pytest.mark.asyncio
async def test_fresh_key_resume_never_returns_pending_resume_scheme() -> None:
    state = _InMemoryState()
    provider = _FakeProvider()
    repo = GarudaOrderRepository(
        _FakePool(state),  # type: ignore[arg-type]
        eligibility_lookup=_FakeLookup(),
        provider=provider,
        environment="TEST",
    )

    result_id = "res-check-alpha"
    payload = {"result_id": result_id, "review_confirmed": True}
    payload_sha = canonical_payload_sha256(payload)
    key_alpha = scoped_key_sha256(actor=result_id, operation="create_order", raw_key="idem-alpha")
    key_beta = scoped_key_sha256(actor=result_id, operation="create_order", raw_key="idem-beta")
    key_gamma = scoped_key_sha256(actor=result_id, operation="create_order", raw_key="idem-gamma")

    # 1. Initial creation: calls provider once and returns HTTPS checkout URL.
    body_initial, replayed_initial = await repo.create_order_and_checkout(
        result_id=result_id,
        applicant=_synthetic_applicant(),
        review_confirmed=True,
        idempotency_key_sha256=key_alpha,
        canonical_payload_sha256=payload_sha,
    )
    assert replayed_initial is False
    assert body_initial["order_state"] == "awaiting_payment"
    assert str(body_initial["checkout_url"]).startswith("https://")
    assert len(provider.create_calls) == 1
    order_id = body_initial["order_id"]

    # 2. Same Idempotency-Key replay: returns cached HTTPS URL without extra provider call.
    body_replay, replayed_same = await repo.create_order_and_checkout(
        result_id=result_id,
        applicant=_synthetic_applicant(),
        review_confirmed=True,
        idempotency_key_sha256=key_alpha,
        canonical_payload_sha256=payload_sha,
    )
    assert replayed_same is True
    assert body_replay == body_initial
    assert len(provider.create_calls) == 1

    # 3. Fresh Idempotency-Key while order is awaiting_payment: must NOT fabricate pending-resume:.
    body_resume, replayed_fresh = await repo.create_order_and_checkout(
        result_id=result_id,
        applicant=_synthetic_applicant(),
        review_confirmed=True,
        idempotency_key_sha256=key_beta,
        canonical_payload_sha256=payload_sha,
    )
    assert replayed_fresh is False
    assert body_resume["order_id"] == order_id
    assert body_resume["order_state"] == "awaiting_payment"
    assert body_resume["checkout_url"] is None
    assert "pending-resume:" not in repr(body_resume)
    assert len(provider.create_calls) == 1
    assert state.idempotency_rows[key_beta]["order_id"] == order_id
    assert state.idempotency_rows[key_beta]["response_body"] == body_resume

    # 4. Fresh Idempotency-Key after webhook moved the order to `paid`.
    state.orders[order_id]["state"] = "paid"
    body_paid, replayed_paid = await repo.create_order_and_checkout(
        result_id=result_id,
        applicant=_synthetic_applicant(),
        review_confirmed=True,
        idempotency_key_sha256=key_gamma,
        canonical_payload_sha256=payload_sha,
    )
    assert replayed_paid is False
    assert body_paid["order_id"] == order_id
    assert body_paid["order_state"] == "paid"
    assert body_paid["checkout_url"] is None
    assert "pending-resume:" not in repr(body_paid)
    assert len(provider.create_calls) == 1
