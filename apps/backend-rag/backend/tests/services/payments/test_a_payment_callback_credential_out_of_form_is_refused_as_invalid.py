"""A payment callback whose credential or body is out of form is refused as invalid.

The webhook is public: anyone can post to it, and everything up to the signature check
runs on what an unauthenticated caller sent. The contract's answer to a callback that
does not prove itself is 401 WEBHOOK_SIGNATURE_INVALID. Three kinds of input got a 500
instead, because the check itself raised something else:

- a token or signature with a non-ASCII character (``hmac.compare_digest`` on two strings
  raises ``TypeError``; a header byte above 0x7F arrives as such a character);
- a body that is JSON but not an object, or holds a lone surrogate (Midtrans);
- a deeply nested body (``RecursionError`` from the parser, in the Midtrans check and in the
  router's routing step, which runs before any check when two providers are mounted);
- an order id that the database cannot take as text, a lone surrogate or a NUL: the routing
  step asks the orders table about it before any check.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import garuda_orders_router
from backend.services.garuda_orders.repository import GarudaOrderRepository
from backend.services.payments.midtrans import MidtransFeeConfig, MidtransPaymentProvider
from backend.services.payments.port import WebhookSignatureInvalid
from backend.services.payments.xendit import XenditFeeConfig, XenditPaymentProvider

TOKEN = "test-webhook-callback-token"
SERVER_KEY = "SB-Mid-server-fake-key-for-tests"
XENDIT_SANDBOX_KEY = "xnd_development_fake_key_for_tests"
URL = "/api/visa/voa/webhooks/payment"
DEEPLY_NESTED = b"[" * 100_000


def _xendit() -> XenditPaymentProvider:
    return XenditPaymentProvider(
        secret_key=XENDIT_SANDBOX_KEY,
        callback_verification_token=TOKEN,
        public_base_url="https://example.com",
        fee_config=XenditFeeConfig(percentage_bps=350, fixed_idr=6000),
        client=httpx.AsyncClient(),
    )


def _midtrans() -> MidtransPaymentProvider:
    return MidtransPaymentProvider(
        server_key=SERVER_KEY,
        fee_config=MidtransFeeConfig(),
        public_base_url="https://example.com",
        client=httpx.AsyncClient(),
    )


def _notification(**changed: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "order_id": "ord_test_1",
        "transaction_id": "txn_test_1",
        "status_code": "200",
        "gross_amount": "790000.00",
        "transaction_status": "settlement",
        "payment_type": "bank_transfer",
        "currency": "IDR",
    }
    return {**body, **changed}


def _digest(body: dict[str, Any]) -> str:
    """The provider's published rule, written out here and not taken from the adapter."""
    raw = str(body["order_id"]) + str(body["status_code"]) + str(body["gross_amount"]) + SERVER_KEY
    return hashlib.sha512(raw.encode()).hexdigest()


def _signed(**changed: Any) -> bytes:
    body = _notification(**changed)
    return json.dumps({**body, "signature_key": _digest(body)}).encode()


def _with_signature(signature: Any, **changed: Any) -> bytes:
    return json.dumps(_notification(signature_key=signature, **changed)).encode()


# ---------------------------------------------------------------- the Xendit token

TOKENS_THAT_ARE_NOT_ASCII = {
    "a latin-1 character": "café",
    "the right token and one more character above 0x7F": TOKEN + "ÿ",
    "letters of another script": "ключ",
    "a lone surrogate": "\ud800",
    "a long run of a non-ASCII character": "é" * 100_000,
}

WRONG_ASCII_TOKENS = {
    "another word": "wrong",
    "the right token and a newline": TOKEN + "\n",
    "a space before the right token": " " + TOKEN,
    "the right token in upper case": TOKEN.upper(),
    "one character short": TOKEN[:-1],
    "one character long": TOKEN + "x",
    "a NUL": "\x00",
}


@pytest.mark.parametrize(
    "received", TOKENS_THAT_ARE_NOT_ASCII.values(), ids=TOKENS_THAT_ARE_NOT_ASCII.keys()
)
def test_a_token_that_is_not_ascii_is_refused_like_any_wrong_token(received: str) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        _xendit().verify_signature(raw_body=b"{}", headers={"x-callback-token": received})


@pytest.mark.parametrize("received", WRONG_ASCII_TOKENS.values(), ids=WRONG_ASCII_TOKENS.keys())
def test_a_wrong_ascii_token_is_refused_as_before(received: str) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        _xendit().verify_signature(raw_body=b"{}", headers={"x-callback-token": received})


@pytest.mark.parametrize("header", ["x-callback-token", "X-Callback-Token", "X-CALLBACK-TOKEN"])
def test_the_right_token_is_accepted_as_before(header: str) -> None:
    assert _xendit().verify_signature(raw_body=b"{}", headers={header: TOKEN}) is None


# ---------------------------------------------------------------- the Midtrans signature

BODIES_THAT_ARE_NOT_AN_OBJECT = {
    "a list": b"[1, 2]",
    "an empty list": b"[]",
    "a string": b'"text"',
    "a number": b"7",
    "null": b"null",
    "true": b"true",
    "a deeply nested list": DEEPLY_NESTED,
}

BODIES_WITH_A_FIELD_OUT_OF_FORM = {
    "a signature with a non-ASCII character": _with_signature("café"),
    "a signature that is a lone surrogate": _with_signature("\ud800"),
    "an order id that is a lone surrogate": _with_signature("abc", order_id="\ud800"),
    "a status code that is a lone surrogate": _with_signature("abc", status_code="\udfff"),
    "an amount that is a lone surrogate": _with_signature("abc", gross_amount="\ud800"),
}

BODIES_REFUSED_ALREADY = {
    "not JSON": b"\xff\xfe not json",
    "a wrong ASCII signature": _with_signature("abc"),
    "a signature that is a number": _with_signature(7),
    "a signature that is null": _with_signature(None),
    "a signature that is a list": _with_signature(["a"]),
    "an empty signature": _with_signature(""),
    "no signature at all": json.dumps(_notification()).encode(),
    "an empty object": b"{}",
    "a right signature for another amount": json.dumps(
        {**_notification(gross_amount="1.00"), "signature_key": _digest(_notification())}
    ).encode(),
}


@pytest.mark.parametrize(
    "raw_body", BODIES_THAT_ARE_NOT_AN_OBJECT.values(), ids=BODIES_THAT_ARE_NOT_AN_OBJECT.keys()
)
def test_a_body_that_is_not_a_json_object_is_refused(raw_body: bytes) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        _midtrans().verify_signature(raw_body=raw_body, headers={})


@pytest.mark.parametrize(
    "raw_body",
    BODIES_WITH_A_FIELD_OUT_OF_FORM.values(),
    ids=BODIES_WITH_A_FIELD_OUT_OF_FORM.keys(),
)
def test_a_body_with_a_field_that_cannot_be_compared_is_refused(raw_body: bytes) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        _midtrans().verify_signature(raw_body=raw_body, headers={})


@pytest.mark.parametrize(
    "raw_body", BODIES_REFUSED_ALREADY.values(), ids=BODIES_REFUSED_ALREADY.keys()
)
def test_a_body_that_does_not_prove_itself_is_refused_as_before(raw_body: bytes) -> None:
    with pytest.raises(WebhookSignatureInvalid):
        _midtrans().verify_signature(raw_body=raw_body, headers={})


@pytest.mark.parametrize(
    "changed",
    [{}, {"order_id": "ordre-été"}, {"gross_amount": "1.00"}, {"status_code": "201"}],
    ids=["the usual notification", "a non-ASCII order id", "another amount", "another status"],
)
def test_a_rightly_signed_notification_is_accepted_as_before(changed: dict[str, Any]) -> None:
    assert _midtrans().verify_signature(raw_body=_signed(**changed), headers={}) is None


@pytest.mark.parametrize(
    "changed",
    [{}, {"order_id": "ordre-été"}, {"order_id": "訂單-1"}],
    ids=["ascii", "latin", "cjk"],
)
def test_the_expected_signature_of_a_well_formed_body_has_not_moved(
    changed: dict[str, Any],
) -> None:
    body = _notification(**changed)

    assert _midtrans()._expected_signature(body) == _digest(body)


# ---------------------------------------------------------------- through the route


class _TextAsTheDatabaseTakesIt:
    """Stands in for the driver and the server on the one question the route asks before the
    signature check. Text goes out as strict UTF-8, which refuses a lone surrogate (the
    driver), and holds no NUL (the server). Both were measured against the real pair; the
    database-backed test file beside the orders tests repeats them there."""

    def __init__(self, provider_of: dict[str, str]) -> None:
        self.provider_of = provider_of
        self.asked: list[str] = []

    def acquire(self) -> _TextAsTheDatabaseTakesIt:
        return self

    async def __aenter__(self) -> _TextAsTheDatabaseTakesIt:
        return self

    async def __aexit__(self, *raised: object) -> None:
        return None

    async def fetchval(self, query: str, order_id: str) -> str | None:
        order_id.encode("utf-8")
        if "\x00" in order_id:
            raise ValueError('invalid byte sequence for encoding "UTF8": 0x00')
        self.asked.append(order_id)
        return self.provider_of.get(order_id)


def _repository(database: _TextAsTheDatabaseTakesIt, selected: Any) -> GarudaOrderRepository:
    """The REAL repository: a stand-in that answers any string would hide what the real
    one does with the text it is handed."""
    return GarudaOrderRepository(
        database,  # type: ignore[arg-type]
        eligibility_lookup=None,  # type: ignore[arg-type]
        provider=selected,
        environment="test",
    )


def _api(default: Any, *, two_providers: bool) -> TestClient:
    app = FastAPI()
    app.include_router(garuda_orders_router.router)
    app.state.garuda_order_repository = _repository(_TextAsTheDatabaseTakesIt({}), default)
    app.state.garuda_payment_provider = default
    if two_providers:
        app.state.garuda_payment_providers = {"xendit": _xendit(), "midtrans": _midtrans()}
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _the_funnel_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "true")


def _refused(answer: Any) -> bool:
    assert answer.status_code == 401
    return answer.json()["code"] == "WEBHOOK_SIGNATURE_INVALID"


@pytest.mark.parametrize("two_providers", [False, True], ids=["one provider", "two providers"])
def test_a_token_header_with_a_byte_above_0x7f_is_answered_401(two_providers: bool) -> None:
    api = _api(_xendit(), two_providers=two_providers)

    answer = api.post(URL, content=b"{}", headers={"x-callback-token": b"caf\xe9"})

    assert _refused(answer)


def test_a_deeply_nested_body_is_answered_401_when_two_providers_are_mounted() -> None:
    """The routing step parses the body before any check; it must not be able to raise."""
    api = _api(_xendit(), two_providers=True)

    answer = api.post(URL, content=DEEPLY_NESTED, headers={"x-callback-token": "wrong"})

    assert _refused(answer)


@pytest.mark.parametrize(
    "raw_body",
    [*BODIES_THAT_ARE_NOT_AN_OBJECT.values(), *BODIES_WITH_A_FIELD_OUT_OF_FORM.values()],
    ids=[*BODIES_THAT_ARE_NOT_AN_OBJECT.keys(), *BODIES_WITH_A_FIELD_OUT_OF_FORM.keys()],
)
@pytest.mark.parametrize("two_providers", [False, True], ids=["one provider", "two providers"])
def test_a_midtrans_body_out_of_form_is_answered_401(raw_body: bytes, two_providers: bool) -> None:
    api = _api(_midtrans(), two_providers=two_providers)

    assert _refused(api.post(URL, content=raw_body))


@pytest.mark.parametrize("two_providers", [False, True], ids=["one provider", "two providers"])
def test_a_wrong_ascii_token_is_answered_401_as_before(two_providers: bool) -> None:
    api = _api(_xendit(), two_providers=two_providers)

    assert _refused(api.post(URL, content=b"{}", headers={"x-callback-token": "wrong"}))
    assert _refused(api.post(URL, content=b"[1, 2]", headers={"x-callback-token": "wrong"}))
    assert _refused(api.post(URL, content=b"{}"))


# ---------------------------------------------------------------- the id read before the check

ORDER = "ord_routing_form_0000000001"

# Sixteen characters in the form of an order id; the cases below are built around it.
STEM = "ord_" + "a" * 12

IDS_THE_DATABASE_CANNOT_TAKE = {
    "a lone surrogate": "\ud800",
    "a surrogate inside an id": STEM + "\udfff" + STEM,
    "a NUL": "\x00",
    "a NUL inside an id": STEM + "\x00" + STEM,
}
IDS_THAT_CANNOT_BE_OURS = {
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
    "an id as issued": ORDER,
}


def _api_with_orders(
    selected: str, provider_of: dict[str, str] | None = None
) -> tuple[TestClient, _TextAsTheDatabaseTakesIt]:
    """The real router and the REAL repository, on a database that takes text as the real one
    does. Both providers are mounted, as production mounts them."""
    database = _TextAsTheDatabaseTakesIt(provider_of or {})
    providers = {"xendit": _xendit(), "midtrans": _midtrans()}
    app = FastAPI()
    app.include_router(garuda_orders_router.router)
    app.state.garuda_order_repository = _repository(database, providers[selected])
    app.state.garuda_payment_provider = providers[selected]
    app.state.garuda_payment_providers = providers
    return TestClient(app, raise_server_exceptions=False), database


def _unproven(key: str, unverified_id: str) -> bytes:
    return json.dumps({key: unverified_id, "signature_key": "abc"}).encode()


@pytest.mark.parametrize("selected", ["xendit", "midtrans"])
@pytest.mark.parametrize("key", ["order_id", "external_id"])
@pytest.mark.parametrize(
    "unverified_id",
    IDS_THE_DATABASE_CANNOT_TAKE.values(),
    ids=IDS_THE_DATABASE_CANNOT_TAKE.keys(),
)
def test_an_order_id_the_database_cannot_take_is_answered_401(
    selected: str, key: str, unverified_id: str
) -> None:
    api, database = _api_with_orders(selected)

    answer = api.post(
        URL, content=_unproven(key, unverified_id), headers={"x-callback-token": "wrong"}
    )

    assert _refused(answer)
    assert database.asked == []


@pytest.mark.parametrize("key", ["order_id", "external_id"])
@pytest.mark.parametrize(
    "unverified_id", IDS_THAT_CANNOT_BE_OURS.values(), ids=IDS_THAT_CANNOT_BE_OURS.keys()
)
def test_an_id_that_cannot_be_ours_is_answered_401_as_before(key: str, unverified_id: str) -> None:
    """These the database could always answer: no such order. The answer does not move."""
    api, _ = _api_with_orders("xendit")

    answer = api.post(
        URL, content=_unproven(key, unverified_id), headers={"x-callback-token": "wrong"}
    )

    assert _refused(answer)


@pytest.mark.parametrize("key", ["order_id", "external_id"])
@pytest.mark.parametrize(
    "unverified_id", IDS_THAT_CANNOT_BE_OURS.values(), ids=IDS_THAT_CANNOT_BE_OURS.keys()
)
def test_an_id_that_cannot_be_ours_is_not_asked_of_the_orders_table(
    key: str, unverified_id: str
) -> None:
    api, database = _api_with_orders("xendit")

    api.post(URL, content=_unproven(key, unverified_id), headers={"x-callback-token": "wrong"})

    assert database.asked == []


@pytest.mark.parametrize("key", ["order_id", "external_id"])
@pytest.mark.parametrize("unverified_id", IDS_IN_FORM.values(), ids=IDS_IN_FORM.keys())
def test_an_id_in_form_is_still_asked_of_the_orders_table(key: str, unverified_id: str) -> None:
    api, database = _api_with_orders("xendit")

    answer = api.post(
        URL, content=_unproven(key, unverified_id), headers={"x-callback-token": "wrong"}
    )

    assert _refused(answer)
    assert database.asked == [unverified_id]


def test_a_midtrans_order_is_still_verified_by_midtrans_when_xendit_is_selected() -> None:
    """Per-order dispatch is untouched: Xendit alone would refuse this (it carries no token)."""
    api, database = _api_with_orders("xendit", {ORDER: "midtrans"})
    pending = _signed(order_id=ORDER, status_code="201", transaction_status="pending")

    answer = api.post(URL, content=pending)

    assert answer.status_code == 204
    assert database.asked == [ORDER]


def test_a_xendit_order_is_still_verified_by_xendit_when_midtrans_is_selected() -> None:
    """Midtrans alone would refuse this (no signature). Xendit accepts the token and then
    finds no invoice id in the body: 422, which only a verified callback can get."""
    api, database = _api_with_orders("midtrans", {ORDER: "xendit"})

    answer = api.post(
        URL,
        content=json.dumps({"external_id": ORDER}).encode(),
        headers={"x-callback-token": TOKEN},
    )

    assert answer.status_code == 422
    assert database.asked == [ORDER]


def test_an_order_the_table_does_not_know_stays_with_the_selected_provider() -> None:
    api, database = _api_with_orders("xendit")
    pending = _signed(order_id=ORDER, status_code="201", transaction_status="pending")

    assert _refused(api.post(URL, content=pending))
    assert database.asked == [ORDER]


@pytest.mark.parametrize(
    "unverified_id",
    IDS_THE_DATABASE_CANNOT_TAKE.values(),
    ids=IDS_THE_DATABASE_CANNOT_TAKE.keys(),
)
def test_a_rightly_signed_callback_is_not_refused_for_an_id_field_out_of_form(
    unverified_id: str,
) -> None:
    """The id is not a credential. With the right token the callback is verified as before
    and goes on to be parsed; here the body has no invoice id, so the answer is 422."""
    api, database = _api_with_orders("xendit")

    answer = api.post(
        URL,
        content=json.dumps({"external_id": unverified_id}).encode(),
        headers={"x-callback-token": TOKEN},
    )

    assert answer.status_code == 422
    assert database.asked == []


ANSWERS_THAT_COME_FIRST = {
    "the funnel is off": ("flag", 404, "GARUDA_PUBLIC_DISABLED"),
    "no repository is wired": ("repository", 503, "SERVICE_UNAVAILABLE"),
    "no provider is wired": ("provider", 503, "SERVICE_UNAVAILABLE"),
}


@pytest.mark.parametrize(
    ("missing", "status", "code"),
    ANSWERS_THAT_COME_FIRST.values(),
    ids=ANSWERS_THAT_COME_FIRST.keys(),
)
@pytest.mark.parametrize(
    "unverified_id",
    [*IDS_THE_DATABASE_CANNOT_TAKE.values(), "abc", ORDER],
    ids=[*IDS_THE_DATABASE_CANNOT_TAKE.keys(), "three letters", "an id as issued"],
)
def test_every_answer_that_comes_before_the_lookup_keeps_its_place(
    monkeypatch: pytest.MonkeyPatch, missing: str, status: int, code: str, unverified_id: str
) -> None:
    api, database = _api_with_orders("xendit")
    if missing == "flag":
        monkeypatch.setenv("GARUDA_PUBLIC_ENABLED", "false")
    elif missing == "repository":
        api.app.state.garuda_order_repository = None
    else:
        api.app.state.garuda_payment_provider = None

    answer = api.post(
        URL, content=_unproven("order_id", unverified_id), headers={"x-callback-token": "wrong"}
    )

    assert answer.status_code == status
    assert answer.json()["code"] == code
    assert database.asked == []
