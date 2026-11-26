"""The order id of a payment callback is looked up only in the form the orders table accepts.

The webhook reads an order id from the body BEFORE the signature is verified and asks the
orders table which provider that order belongs to. Two kinds of string made the question
itself fail, so a caller who proved nothing got a 500 where the contract says 401: a lone
surrogate, which the driver cannot encode, and a NUL, which the server refuses as text.

The router now asks only about a string in the form of an order id. That moves no answer
if, and only if, the table cannot hold an id outside that form. This file measures both
against the real driver and the real server: the two failures, and the table's own rule.
It writes nothing: the inserts it tries run in a transaction that is rolled back.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

asyncpg = pytest.importorskip("asyncpg")

from backend.app.routers import garuda_orders_router  # noqa: E402
from backend.services.garuda_orders.repository import GarudaOrderRepository  # noqa: E402
from backend.services.payments.xendit import XenditFeeConfig, XenditPaymentProvider  # noqa: E402
from backend.tests.fixtures.prod_shaped_pool import create_prod_shaped_pool  # noqa: E402

_DSN = (
    os.environ.get("GARUDA_L3_TEST_DSN")
    or os.environ.get("INTAKE_TEST_DSN")
    or "postgresql://localhost:5432/nuzantara_test"
)
TOKEN = "test-webhook-callback-token"
XENDIT_SANDBOX_KEY = "xnd_development_fake_key_for_tests"
URL = "/api/visa/voa/webhooks/payment"

# Sixteen characters in the form of an order id; the cases below are built around it.
STEM = "ord_" + "a" * 12

IDS_THE_DATABASE_CANNOT_TAKE = {
    "a lone surrogate": "\ud800",
    "a surrogate inside an id": STEM + "\udfff" + STEM,
    "a NUL": "\x00",
    "a NUL inside an id": STEM + "\x00" + STEM,
}
IDS_OUT_OF_FORM = {
    "empty": "",
    "three letters": "abc",
    "15 characters": "a" * 15,
    "129 characters": "a" * 129,
    "a dot inside": STEM + "." + STEM,
    "a space inside": STEM + " " + STEM,
    "a newline at the end": STEM + "\n",
    "a letter that is not ASCII": STEM + "\xe9",
    "an emoji": STEM + "\U0001f600",
}
IDS_IN_FORM = {
    "16 characters": STEM,
    "128 characters": "aZ0_-9bY" * 16,
    "an id as issued": "ord_routing_form_0000000001",
}


@pytest.fixture(autouse=True)
def _the_funnel_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")


@pytest.fixture
async def pool():
    try:
        made = await create_prod_shaped_pool(_DSN, min_size=1, max_size=2)
    except (OSError, asyncpg.PostgresError) as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI has no reachable Postgres at {_DSN!r}: {exc}")
        pytest.skip(f"no local Postgres reachable at {_DSN}: {exc}")
        raise AssertionError("unreachable: pytest.skip always raises") from exc
    yield made
    await made.close()


@pytest.fixture
def api(pool) -> AsyncClient:
    """The real router, the real repository, the real driver and server. The provider mapping
    is mounted, as in production: it is what makes the route ask before the check."""
    provider = XenditPaymentProvider(
        secret_key=XENDIT_SANDBOX_KEY,
        callback_verification_token=TOKEN,
        public_base_url="https://example.com",
        fee_config=XenditFeeConfig(percentage_bps=350, fixed_idr=6000),
        client=httpx.AsyncClient(),
    )
    app = FastAPI()
    app.include_router(garuda_orders_router.router)
    app.state.garuda_order_repository = GarudaOrderRepository(
        pool,
        eligibility_lookup=None,  # type: ignore[arg-type]
        provider=provider,
        environment="test",
    )
    app.state.garuda_payment_provider = provider
    app.state.garuda_payment_providers = {"xendit": provider}
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    )


@pytest.mark.parametrize("key", ["order_id", "external_id"])
@pytest.mark.parametrize(
    "unverified_id",
    IDS_THE_DATABASE_CANNOT_TAKE.values(),
    ids=IDS_THE_DATABASE_CANNOT_TAKE.keys(),
)
async def test_an_order_id_the_real_database_cannot_take_is_answered_401(
    api: AsyncClient, key: str, unverified_id: str
) -> None:
    body = json.dumps({key: unverified_id, "signature_key": "abc"}).encode()

    answer = await api.post(URL, content=body, headers={"x-callback-token": "wrong"})

    assert answer.status_code == 401
    assert answer.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"


@pytest.mark.parametrize(
    "unverified_id",
    [*IDS_OUT_OF_FORM.values(), *IDS_IN_FORM.values()],
    ids=[*IDS_OUT_OF_FORM.keys(), *IDS_IN_FORM.keys()],
)
async def test_an_id_the_real_database_can_answer_is_answered_401_as_before(
    api: AsyncClient, unverified_id: str
) -> None:
    body = json.dumps({"order_id": unverified_id, "signature_key": "abc"}).encode()

    answer = await api.post(URL, content=body, headers={"x-callback-token": "wrong"})

    assert answer.status_code == 401
    assert answer.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"


async def _try_to_insert(conn, order_id: str, number: int) -> None:
    await conn.execute(
        """
        INSERT INTO garuda_orders (order_id, result_id_ref, case_type, applicant_full_name,
            applicant_email, applicant_phone, applicant_passport_number, price_idr,
            price_catalogue_key, state, provider_session_id, checkout_expires_at)
        VALUES ($1, $2, 'issuance', 'Form Test', 'form@example.com', '+10000010', 'P0000001',
                790000, 'B1 Visa on Arrival (VOA)', 'awaiting_payment', $3, $4)
        """,
        order_id,
        f"result-form-of-an-id-{number:04d}",
        f"sess-form-of-an-id-{number:04d}",
        datetime.now(UTC) + timedelta(hours=1),
    )


async def test_the_orders_table_refuses_every_id_the_router_does_not_look_up(pool) -> None:
    """Not asking about an id out of form is the same as asking, because the table's own
    CHECK keeps such an id out. If that CHECK is ever loosened, this test names the router's
    rule that has to follow it."""
    async with pool.acquire() as conn:
        outer = conn.transaction()
        await outer.start()
        try:
            for number, (name, order_id) in enumerate(IDS_OUT_OF_FORM.items()):
                assert garuda_orders_router._ORDER_ID_FORM.fullmatch(order_id) is None, name
                with pytest.raises(asyncpg.CheckViolationError) as refused:
                    async with conn.transaction():
                        await _try_to_insert(conn, order_id, number)
                assert refused.value.constraint_name == "garuda_orders_order_id_check", name
            for number, (name, order_id) in enumerate(IDS_IN_FORM.items(), start=100):
                assert garuda_orders_router._ORDER_ID_FORM.fullmatch(order_id), name
                async with conn.transaction():
                    await _try_to_insert(conn, order_id, number)
            kept = await conn.fetchval(
                "SELECT count(*) FROM garuda_orders WHERE order_id = ANY($1::text[])",
                list(IDS_IN_FORM.values()),
            )
            assert kept == len(IDS_IN_FORM)
        finally:
            await outer.rollback()
