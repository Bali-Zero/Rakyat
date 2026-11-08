"""Retirement must win over stale credentials and cached HTTP clients."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI

from backend.core.telegram_retirement import telegram_is_retired


def test_no_environment_opt_in(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "stale-test-token")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TG_OWNER_TELEGRAM", "1")
    assert telegram_is_retired() is True


@pytest.mark.asyncio
async def test_cached_bot_client_is_not_used():
    from backend.services.integrations.telegram_bot_service import TelegramBotService

    svc = TelegramBotService()
    svc._token = "stale-test-token"
    svc._client = AsyncMock()
    svc._client.is_closed = False
    with pytest.raises(RuntimeError, match="retired"):
        await svc.send_message(chat_id="test-chat", text="synthetic")
    svc._client.post.assert_not_awaited()
    svc._client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_and_wa_transports_do_not_send():
    from backend.services.review.telegram_adapter import TelegramReviewAdapter
    from backend.services.wa_copilot.telegram_notifier import send_telegram_message

    client = AsyncMock()
    adapter = TelegramReviewAdapter(bot_token="stale-test-token", http_client=client)
    result = await adapter.send_message(chat_id="test-chat", text="synthetic")
    assert result.ok is False and "retired" in result.error
    ok, error = await send_telegram_message(client, "stale-test-token", "test-chat", "synthetic")
    assert ok is False and "retired" in error
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_retains_web_without_creating_telegram(monkeypatch):
    from backend.app.setup import service_initializer as init

    app = FastAPI()
    app.state.orchestrator = MagicMock()
    router = MagicMock()
    with (
        patch.object(init.settings, "telegram_bot_token", "stale-test-token"),
        patch("backend.channels.router.ChannelRouter", return_value=router),
        patch("backend.channels.telegram.adapter.TelegramChannelAdapter") as telegram,
        patch("backend.channels.web.adapter.WebChannelAdapter") as web,
        patch("backend.conversation.engine.ConversationEngine"),
    ):
        monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("INSTAGRAM_ACCESS_TOKEN", raising=False)
        await init.initialize_channel_router(app, MagicMock(), None)
    telegram.assert_not_called()
    router.register_adapter.assert_any_call("web", web.return_value)


@pytest.mark.asyncio
async def test_sla_preserves_expiry_without_alert_adapter():
    from backend.services.review.sla_worker import SLAWorker
    from backend.services.war_room.models import DraftStatus

    now = datetime.now(timezone.utc)
    pending, expired = uuid4(), uuid4()
    repo = AsyncMock()
    repo.fetch_safe.return_value = [
        {
            "id": pending,
            "topic": "synthetic",
            "created_at": now - timedelta(hours=15),
            "status": "pending_review",
        },
        {
            "id": expired,
            "topic": "synthetic",
            "created_at": now - timedelta(hours=49),
            "status": "pending_review",
        },
    ]
    result = await SLAWorker(repo=repo).sweep_once(now=now)
    assert result.expired_count == 1
    assert result.soft_alerts_sent == result.repeat_alerts_sent == 0
    assert result.errors == []
    assert repo.update_status.await_args.args[0:2] == (expired, DraftStatus.REJECTED)


@pytest.mark.asyncio
async def test_claim_excludes_six_retired_types_before_increment():
    from backend.services.garuda_orders.outbox_consumer import _claim_one

    conn = AsyncMock()
    conn.fetchrow.return_value = None
    await _claim_one(conn, max_attempts=5, exclude_ids=[])
    sql, maximum, ids, retired = conn.fetchrow.await_args.args
    assert "AND NOT (c.job_type = ANY($3::text[]))" in sql
    assert maximum == 5 and ids == []
    assert set(retired) == {
        "staff_page_duplicate_charge",
        "staff_page_late_paid_after_refund",
        "staff_page_late_paid_after_terminal",
        "staff_page_charge_without_webhook",
        "staff_page_payment_failure",
        "staff_page_refund_out_of_order",
    }


@pytest.mark.asyncio
async def test_sla_cli_needs_no_telegram_credential_or_owner_chat(monkeypatch, capsys):
    from backend.services.review import sla_worker_cli as cli

    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic-test-target")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    pool = AsyncMock()
    repo = AsyncMock()
    repo.fetch_safe.return_value = []
    with (
        patch.object(cli.asyncpg, "create_pool", new=AsyncMock(return_value=pool)),
        patch.object(cli, "WarRoomRepository", return_value=repo),
    ):
        assert await cli.run() == 0
    pool.close.assert_awaited_once()
    assert '"expired_count": 0' in capsys.readouterr().out
