"""Retirement blocks direct transports even with stale credentials, not core work."""

from __future__ import annotations

import importlib
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_transport_with_stale_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-stale-not-a-provider-token")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "synthetic-admin")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TG_OWNER_TELEGRAM", "1")

    def forbidden(*args, **kwargs):
        raise AssertionError("retired transport attempted network/client construction")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)


@pytest.mark.parametrize(
    ("module", "function", "kwargs", "expected"),
    [
        ("backend.scripts.cost_advisor_cli", "send_telegram", {"text": "synthetic"}, None),
        (
            "backend.services.canva_renderer_v2._telegram",
            "send_telegram",
            {"text": "synthetic"},
            None,
        ),
        (
            "backend.services.crm.drive_poll_service",
            "_send_telegram_alert",
            {"message": "synthetic"},
            None,
        ),
        (
            "backend.services.sota_loop.m13_weekly",
            "_notify_telegram",
            {"deltas": {}, "breaches": [], "retrained": False, "retrain_result": "synthetic"},
            None,
        ),
        (
            "backend.services.notifications.email_audit",
            "notify_email_failure_critical",
            {
                "email_type": "synthetic",
                "to_email": "fixture@example.invalid",
                "subject": None,
                "practice_id": None,
                "error": "synthetic",
            },
            None,
        ),
        (
            "backend.services.notifications.email_health_monitor",
            "_post_telegram",
            {"text": "synthetic"},
            None,
        ),
        (
            "backend.services.federation_alerts.webhook",
            "_answer_callback",
            {"bot_token": "synthetic", "callback_id": "fixture", "text": "synthetic"},
            None,
        ),
        (
            "backend.services.federation_alerts.notifier",
            "send_proposal_to_telegram",
            {
                "bot_token": "synthetic",
                "chat_id": "fixture",
                "proposal": SimpleNamespace(proposal_id="fixture"),
                "approval_token": "synthetic",
            },
            None,
        ),
        (
            "backend.services.federation_alerts.notifier",
            "edit_message_after_decision",
            {
                "bot_token": "synthetic",
                "chat_id": "fixture",
                "message_id": 1,
                "decision": "approved",
            },
            False,
        ),
    ],
)
def test_sync_transports_do_not_construct_clients(module, function, kwargs, expected):
    loaded = importlib.import_module(module)
    assert loaded.telegram_is_retired() is True
    assert getattr(loaded, function)(**kwargs) is expected


@pytest.mark.asyncio
async def test_cashout_transport_is_inert():
    from backend.services.hr.owner_cashout.telegram_alert import send_alert

    assert await send_alert("synthetic") is None


@pytest.mark.asyncio
async def test_federation_callback_cannot_mutate_approval_state():
    from backend.services.federation_alerts.webhook import handle_fad_callback

    pool = Mock()
    result = await handle_fad_callback(
        {"data": "fad:approve:fixture:synthetic"}, pool=pool, bot_token="synthetic"
    )
    assert result is False
    assert pool.mock_calls == []


@pytest.mark.asyncio
async def test_attendance_reminder_digest_and_send_are_inert_without_db_work():
    from backend.services.analytics.attendance_monitor import AttendanceMonitor

    monitor = AttendanceMonitor.__new__(AttendanceMonitor)
    monitor.pool = Mock()
    assert await monitor.send_clockin_reminder() is None
    assert await monitor._send_telegram("synthetic") is None
    assert await monitor.send_daily_telegram_digest() is False
    assert monitor.pool.mock_calls == []


@pytest.mark.asyncio
async def test_dlq_telegram_alert_does_not_construct_client():
    from backend.channels.optimizations import DeliveryManager

    manager = DeliveryManager.__new__(DeliveryManager)
    manager._get_alert_client = AsyncMock(side_effect=AssertionError("must not build client"))
    assert await manager._alert_exhausted({}, "synthetic") is None
    manager._get_alert_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitoring_preserves_local_slack_discord_and_false_telegram_status(monkeypatch):
    from backend.services.monitoring.alert_service import AlertLevel, AlertService

    svc = AlertService()
    assert svc.enable_telegram is False
    svc.enable_telegram = True  # simulate an already-constructed stale service
    svc.enable_slack = svc.enable_discord = True
    log = Mock()
    slack, discord = AsyncMock(), AsyncMock()
    monkeypatch.setattr(svc, "_log_alert", log)
    monkeypatch.setattr(svc, "_send_slack_alert", slack)
    monkeypatch.setattr(svc, "_send_discord_alert", discord)
    result = await svc.send_alert("synthetic", "synthetic", AlertLevel.WARNING)
    assert result == {"telegram": False, "slack": True, "discord": True, "logging": True}
    log.assert_called_once()
    slack.assert_awaited_once()
    discord.assert_awaited_once()
    await svc._send_telegram_alert("synthetic", "synthetic", AlertLevel.WARNING)


def test_checkpoint_still_writes_and_promotes_internal_report(tmp_path, monkeypatch):
    from backend.services.sota_loop import m13_checkpoint

    target = tmp_path / "research/sota-social-2026-v1"
    target.mkdir(parents=True)
    (target / ".loop_start_date").write_text((date.today() - timedelta(days=30)).isoformat())
    monkeypatch.setenv("NUZANTARA_REPO_ROOT", str(tmp_path))
    promote = Mock()
    monkeypatch.setattr(m13_checkpoint, "promote_research_output", promote)
    assert m13_checkpoint.main() == 0
    report = target / "checkpoint_day_30.md"
    assert report.is_file()
    assert "internal checkpoint report" in report.read_text()
    promote.assert_called_once()


@pytest.mark.asyncio
async def test_hourly_digest_keeps_local_visibility_without_transport(monkeypatch, caplog):
    import logging

    from backend.services.monitoring.alert_service import AlertService

    svc = AlertService()
    svc.enable_telegram = True
    svc._latency_buffer = [
        {"path": "/synthetic", "method": "GET", "duration_ms": 400, "ts": "fixture"}
    ]
    client = Mock(side_effect=AssertionError("retired digest must not construct a client"))
    monkeypatch.setattr(svc, "_get_client", client)
    with caplog.at_level(logging.INFO):
        await svc.send_hourly_digest()
    client.assert_not_called()
    assert "Telegram retired; local latency log covers 1 events across 1 paths" in caplog.text
    assert svc._latency_buffer == []
