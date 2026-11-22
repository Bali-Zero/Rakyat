"""A partner mail counts as sent only when the send endpoint says it was sent.

The internal send endpoint answers HTTP 200 with ``success: false`` when every provider failed.
The partner sender looked at the status code only: the outbox row was marked sent and the
partner (or the commission) was stamped as mailed, so nothing ever tried again.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import backend.services.crm.partners.emails as emails_mod
from backend.services.crm.partners.emails import enqueue_welcome, flush_outbox, send_welcome
from backend.services.notifications.email_audit import EmailNotDelivered

ALL_PROVIDERS_FAILED = {"success": False, "message": "All providers failed: brevo 401"}
UNCERTAIN = {"success": False, "message": "delivery uncertain", "delivery_uncertain": True}
SENT = {"success": True, "message": "Email sent"}

NOT_SENT = [
    pytest.param(ALL_PROVIDERS_FAILED, id="every provider failed"),
    pytest.param(UNCERTAIN, id="delivery uncertain"),
]


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch):
    """The send endpoint behind a real httpx client: set ``.status`` and ``.body``; ``.calls``."""
    state = SimpleNamespace(status=200, body=SENT, calls=[])

    def answer(request: httpx.Request) -> httpx.Response:
        state.calls.append(request)
        return httpx.Response(state.status, json=state.body)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(emails_mod, "httpx", SimpleNamespace(AsyncClient=client))
    monkeypatch.setenv("NOTIFICATIONS_ENDPOINT", "http://mail.test/send-email")
    monkeypatch.setenv("NOTIFICATIONS_X_API_KEY", "zz-test")
    return state


async def _post() -> None:
    await emails_mod._post_email(to="partner@example.test", cc=None, subject="s", body="b")


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NOT_SENT)
async def test_the_sender_raises_when_the_endpoint_says_not_sent(endpoint, body) -> None:
    endpoint.body = body

    with pytest.raises(EmailNotDelivered) as refused:
        await _post()

    assert refused.value.delivery_uncertain is (body is UNCERTAIN)
    assert len(endpoint.calls) == 1


@pytest.mark.asyncio
async def test_the_sender_returns_when_the_endpoint_says_sent(endpoint) -> None:
    await _post()

    assert len(endpoint.calls) == 1


@pytest.mark.asyncio
async def test_the_sender_still_raises_on_an_error_status(endpoint) -> None:
    endpoint.status, endpoint.body = 503, {"detail": "down"}

    with pytest.raises(httpx.HTTPStatusError):
        await _post()


async def _queued_welcome(db_conn, partner_factory) -> uuid.UUID:
    partner_id = uuid.UUID(int=(await partner_factory()).int)
    with patch.object(emails_mod, "_build_pricing_services", return_value=[]):
        await enqueue_welcome(db_conn, partner_id)
    return partner_id


async def _state(db_conn, partner_id: uuid.UUID) -> dict[str, Any]:
    row = await db_conn.fetchrow(
        "SELECT status, attempts, last_error FROM partner_email_outbox WHERE partner_id = $1",
        partner_id,
    )
    stamped = await db_conn.fetchval(
        "SELECT welcome_email_sent_at FROM partners WHERE id = $1", partner_id
    )
    return {**dict(row), "stamped": stamped is not None}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", NOT_SENT)
async def test_a_queued_mail_the_endpoint_did_not_send_stays_queued(
    db_conn, partner_factory, endpoint, body
) -> None:
    partner_id = await _queued_welcome(db_conn, partner_factory)
    endpoint.body = body

    assert await flush_outbox(db_conn) == {"sent": 0, "retried": 1, "dlq": 0}

    state = await _state(db_conn, partner_id)
    assert state["status"] == "pending"
    assert state["attempts"] == 1
    assert body["message"] in state["last_error"]
    assert state["stamped"] is False
    assert len(endpoint.calls) == 1


@pytest.mark.asyncio
async def test_a_queued_mail_the_endpoint_sent_is_marked_sent(
    db_conn, partner_factory, endpoint
) -> None:
    partner_id = await _queued_welcome(db_conn, partner_factory)

    assert await flush_outbox(db_conn) == {"sent": 1, "retried": 0, "dlq": 0}

    state = await _state(db_conn, partner_id)
    assert state["status"] == "sent"
    assert state["stamped"] is True


@pytest.mark.asyncio
async def test_a_direct_welcome_the_endpoint_did_not_send_does_not_stamp_the_partner(
    db_conn, partner_factory, endpoint
) -> None:
    partner_id = uuid.UUID(int=(await partner_factory()).int)
    endpoint.body = ALL_PROVIDERS_FAILED

    with (
        patch.object(emails_mod, "_build_pricing_services", return_value=[]),
        pytest.raises(EmailNotDelivered),
    ):
        await send_welcome(db_conn, partner_id)

    assert (
        await db_conn.fetchval(
            "SELECT welcome_email_sent_at FROM partners WHERE id = $1", partner_id
        )
        is None
    )
