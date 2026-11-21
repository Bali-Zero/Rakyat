"""WhatsApp webhook ack-first contract tests.

Verifies that the WhatsApp webhook router persists the inbound payload
to ``inbound_webhooks`` and returns 200 OK before any business processing
runs (P0-6 from zero-crash audit 2026-04-29).

This is a contract test: it asserts the router calls
``inbound_webhook_repo.persist`` and returns 200 OK in the same async
event tick — i.e. without `await`-ing on heavy services like
``orchestrator.process_query``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.services.integrations.wa_outbox_worker import META_INBOX_PHONE_NUMBER_ID


@pytest.fixture
def mock_db_pool():
    """Build an asyncpg.Pool mock with acquire() context manager."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value={"id": 100})

    transaction_ctx = MagicMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction_ctx)

    pool = MagicMock()
    pool._conn = conn

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acquire_ctx)
    return pool


@pytest.fixture
def app(mock_db_pool):
    from backend.app.dependencies import get_database
    from backend.app.routers import whatsapp_chat

    application = FastAPI()
    application.include_router(whatsapp_chat.router)
    application.dependency_overrides[get_database] = lambda: mock_db_pool
    application.state.db_pool = mock_db_pool
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _whatsapp_payload(message_id: str = "wamid.ABC123") -> dict:
    """Standard Meta WhatsApp message payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "entry_1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"profile": {"name": "Test User"}}],
                            "messages": [
                                {
                                    "from": "6284000220000",
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "type": "text",
                                    "text": {"body": "Hello"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


@pytest.mark.integration
def test_acks_in_under_200ms(client: TestClient, mock_db_pool):
    """Webhook returns 200 OK in <200ms — measuring the ack-critical path only.

    The background row close is mocked: Starlette's ``Response.__call__``
    awaits ``send()`` (which flushes bytes to the real socket under uvicorn)
    BEFORE running ``BackgroundTasks``, so production callers (Meta) receive
    the 200 ACK before background work starts — but FastAPI's ``TestClient``
    runs the whole ASGI call, background task included, before returning
    control to ``client.post()``. Unmocked, the timing would include work that
    happens after the ACK in production.

    Do NOT widen the 200ms to silence a red. A red here means either the mock
    was dropped or the ack path genuinely gained synchronous work.
    """
    payload = _whatsapp_payload()

    with (
        # Patch the signature verification to no-op (no app secret set in test)
        patch(
            "backend.app.routers.whatsapp_chat._verify_whatsapp_signature",
            return_value=True,
        ),
        patch(
            "backend.app.routers.whatsapp_chat._close_retired_legacy_rows",
            new_callable=AsyncMock,
        ),
    ):
        start = time.monotonic()
        resp = client.post("/webhook/whatsapp", json=payload)
        elapsed_ms = (time.monotonic() - start) * 1000

    assert resp.status_code == 200
    assert elapsed_ms < 200, f"ack took {elapsed_ms:.0f}ms — possible sync work"


@pytest.mark.integration
def test_persists_payload_to_inbound_webhooks(client: TestClient, mock_db_pool):
    """Router writes payload to inbound_webhooks via persist helper."""
    payload = _whatsapp_payload(message_id="wamid.TEST_PERSIST")

    with (
        patch(
            "backend.app.routers.whatsapp_chat._verify_whatsapp_signature",
            return_value=True,
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.persist",
            new_callable=AsyncMock,
        ) as mock_persist,
    ):
        mock_persist.return_value = (1, True)  # (id, inserted)
        resp = client.post("/webhook/whatsapp", json=payload)

    assert resp.status_code == 200
    mock_persist.assert_awaited()
    # Inspect call: channel="whatsapp", dedup_key derives from message_id
    call_kwargs = mock_persist.call_args.kwargs
    assert call_kwargs.get("channel") == "whatsapp"
    assert "wamid.TEST_PERSIST" in call_kwargs.get("dedup_key", "")
    assert call_kwargs.get("recovery_after_seconds") == 30


def _meta_inbox_payload(message_id: str) -> dict:
    payload = _whatsapp_payload(message_id=message_id)
    payload["entry"][0]["changes"][0]["value"]["metadata"] = {
        "display_phone_number": "15556151111",
        "phone_number_id": META_INBOX_PHONE_NUMBER_ID,
    }
    return payload


@pytest.mark.integration
def test_row_for_a_number_with_no_handler_is_closed_with_a_reason(client: TestClient, mock_db_pool):
    """The retired legacy path used to be the only thing that marked non-meta
    rows processed. Without it they would stay pending and the WebhookProcessor
    would retry them into a route that raises. The webhook now closes them,
    with the retired-path reason, right after the ack."""
    from backend.app.routers import whatsapp_chat

    with (
        patch(
            "backend.app.routers.whatsapp_chat._verify_whatsapp_signature",
            return_value=True,
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.persist",
            new_callable=AsyncMock,
            return_value=(1, True),
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.mark_processed",
            new_callable=AsyncMock,
        ) as mock_mark,
    ):
        resp = client.post("/webhook/whatsapp", json=_whatsapp_payload("wamid.NO_HANDLER"))

    assert resp.status_code == 200
    mock_mark.assert_awaited_once_with(
        mock_db_pool,
        channel="whatsapp",
        dedup_key="wamid.NO_HANDLER",
        error_message=whatsapp_chat.LEGACY_PATH_RETIRED_REASON,
    )


@pytest.mark.integration
def test_duplicate_delivery_is_closed_idempotently(client: TestClient, mock_db_pool):
    """A Meta retry of an already persisted message re-runs the same close;
    mark_processed only touches rows still pending, so this is a no-op."""
    with (
        patch(
            "backend.app.routers.whatsapp_chat._verify_whatsapp_signature",
            return_value=True,
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.persist",
            new_callable=AsyncMock,
            return_value=(None, False),
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.mark_processed",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_mark,
    ):
        resp = client.post("/webhook/whatsapp", json=_whatsapp_payload("wamid.TEST_DUPLICATE"))

    assert resp.status_code == 200
    mock_mark.assert_awaited_once()


@pytest.mark.integration
def test_meta_inbox_row_is_not_closed_by_the_retired_path_guard(client: TestClient, mock_db_pool):
    """Meta-inbox rows keep their own lifecycle: the guard must not touch them."""
    with (
        patch(
            "backend.app.routers.whatsapp_chat._verify_whatsapp_signature",
            return_value=True,
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.persist",
            new_callable=AsyncMock,
            return_value=(1, True),
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.mark_processed",
            new_callable=AsyncMock,
        ) as mock_mark,
        patch(
            "backend.app.routers.whatsapp_chat.process_meta_inbox_payload", new=AsyncMock()
        ) as meta,
        patch("backend.app.routers.whatsapp_chat._ingest_meta_inbox_media", new=AsyncMock()),
    ):
        resp = client.post("/webhook/whatsapp", json=_meta_inbox_payload("wamid.META"))

    assert resp.status_code == 200
    meta.assert_awaited_once()
    mock_mark.assert_not_awaited()


@pytest.mark.integration
def test_invalid_signature_returns_401_without_persist(client: TestClient, mock_db_pool):
    """Signature verification runs BEFORE persist — bad sig = no DB write."""
    with (
        patch(
            "backend.app.routers.whatsapp_chat._verify_whatsapp_signature",
            return_value=False,
        ),
        patch(
            "backend.services.channels.inbound_webhook_repo.persist",
            new_callable=AsyncMock,
        ) as mock_persist,
    ):
        resp = client.post("/webhook/whatsapp", json=_whatsapp_payload())

    assert resp.status_code == 401
    mock_persist.assert_not_awaited()
