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
async def test_claim_excludes_six_retired_types_before_increment(monkeypatch):
    from backend.services.garuda_orders import outbox_consumer

    assert outbox_consumer.RETIRED_TELEGRAM_JOB_TYPES == ()

    conn = AsyncMock()
    conn.fetchrow.return_value = None
    await outbox_consumer._claim_one(conn, max_attempts=5, exclude_ids=[])
    sql, maximum, ids, retired = conn.fetchrow.await_args.args
    assert "AND NOT (c.job_type = ANY($3::text[]))" in sql
    assert maximum == 5 and ids == [] and retired == []

    monkeypatch.setattr(
        outbox_consumer,
        "RETIRED_TELEGRAM_JOB_TYPES",
        ("synthetic_retired_tg_a", "synthetic_retired_tg_b"),
    )
    conn.fetchrow.reset_mock()
    await outbox_consumer._claim_one(conn, max_attempts=5, exclude_ids=[])
    sql, maximum, ids, retired = conn.fetchrow.await_args.args
    assert "AND NOT (c.job_type = ANY($3::text[]))" in sql
    assert maximum == 5 and ids == []
    assert set(retired) == {"synthetic_retired_tg_a", "synthetic_retired_tg_b"}


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


@pytest.mark.asyncio
async def test_hardening_clis_run_without_telegram_owner_chat_id(monkeypatch, capsys):
    from backend.services.hardening import missed_runs_cli, quota_cli, token_watchdog_cli

    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic-test-target")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)

    assert await token_watchdog_cli.run() == 0
    assert '"warnings_sent": 0' in capsys.readouterr().out

    pool = AsyncMock()
    missed_result = MagicMock(
        ran_at=datetime.now(timezone.utc),
        pending_count=0,
        notified_count=0,
        message_sent=False,
        error=None,
    )
    with (
        patch.object(missed_runs_cli.asyncpg, "create_pool", new=AsyncMock(return_value=pool)),
        patch.object(missed_runs_cli, "WarRoomRepository", return_value=AsyncMock()),
        patch.object(
            missed_runs_cli,
            "MissedRunsAlerter",
            return_value=MagicMock(sweep_once=AsyncMock(return_value=missed_result)),
        ),
    ):
        assert await missed_runs_cli.run() == 0
    assert '"message_sent": false' in capsys.readouterr().out

    quota_result = MagicMock(
        ran_at=datetime.now(timezone.utc),
        reports=[],
        alerts_sent=0,
        errors=[],
    )
    with (
        patch.object(quota_cli.asyncpg, "create_pool", new=AsyncMock(return_value=pool)),
        patch.object(quota_cli, "WarRoomRepository", return_value=AsyncMock()),
        patch.object(
            quota_cli,
            "QuotaMonitor",
            return_value=MagicMock(sweep_once=AsyncMock(return_value=quota_result)),
        ),
    ):
        assert await quota_cli.run() == 0
    assert '"alerts_sent": 0' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cognitive_deliver_clis_skip_when_telegram_retired(monkeypatch, capsys):
    from backend.services.cognitive import oracle_cli, strategos_cli

    monkeypatch.setattr(oracle_cli, "DEFAULT_OWNER", "stale-owner-chat")
    monkeypatch.setattr(strategos_cli, "DEFAULT_OWNER", "stale-owner-chat")
    pool = AsyncMock()

    with patch.object(oracle_cli, "TelegramReviewAdapter") as oracle_adapter:
        assert await oracle_cli._deliver(pool) == 0
        oracle_adapter.assert_not_called()
    assert '"Telegram is retired"' in capsys.readouterr().out

    with patch.object(strategos_cli, "TelegramReviewAdapter") as strategos_adapter:
        assert await strategos_cli._deliver(pool) == 0
        strategos_adapter.assert_not_called()
    assert '"Telegram is retired"' in capsys.readouterr().out
