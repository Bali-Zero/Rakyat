"""Whole production perimeter includes standalone scripts, not just backend/."""

from __future__ import annotations

import ast
import logging
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def stale_environment(monkeypatch):
    for key, value in {
        "TELEGRAM_BOT_TOKEN": "synthetic-stale-token",
        "TELEGRAM_OWNER_CHAT_ID": "synthetic-owner",
        "TELEGRAM_ZERO_CHAT_ID": "synthetic-owner",
        "TG_OWNER_TELEGRAM": "1",
        "TELEGRAM_ENABLED": "true",
        "DATABASE_URL": "postgresql://fixture@127.0.0.1:9/fixture",
        "ENTITY_LINKER_DB_URL": "postgresql://fixture@127.0.0.1:9/fixture",
        "QDRANT_URL": "http://127.0.0.1:9",
        "QDRANT_API_KEY": "synthetic-key",  # pragma: allowlist secret - synthetic import-only fixture
    }.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def load_script(name, monkeypatch):
    """Load actual script functions without opening its real import-time log."""
    with monkeypatch.context() as limited:
        limited.setattr(logging, "FileHandler", lambda *args, **kwargs: logging.NullHandler())
        limited.setattr(Path, "mkdir", lambda *args, **kwargs: None)
        import dotenv

        limited.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
        return runpy.run_path(str(ROOT / "scripts" / name))


@pytest.mark.parametrize(
    ("script", "function", "expected"),
    [
        ("crm_automation_engine.py", "send_telegram", False),
        ("crm_quality_bot.py", "send_telegram", False),
        ("generate_community_summaries.py", "_maybe_notify", None),
        ("run_entity_linker_full.py", "_maybe_notify", None),
        ("run_entity_linker_doc_propagation.py", "_maybe_notify", None),
        ("run_entity_linker_context_header.py", "_maybe_notify", None),
    ],
)
@pytest.mark.asyncio
async def test_standalone_async_senders_never_construct_http_client(
    stale_environment, script, function, expected
):
    module = load_script(script, stale_environment)
    client = Mock(side_effect=AssertionError("Telegram client must not be constructed"))
    stale_environment.setattr(httpx, "AsyncClient", client)
    assert await module[function]("synthetic") is expected
    client.assert_not_called()


def test_legal_radar_send_is_inert(stale_environment):
    module = load_script("legal_radar.py", stale_environment)
    network = Mock(side_effect=AssertionError("Telegram transport must not execute"))
    stale_environment.setattr("urllib.request.urlopen", network)
    assert module["send_telegram_message"]("synthetic", "synthetic", "synthetic") is None
    network.assert_not_called()


def test_reviewer_send_poll_and_caller_terminate_without_waiting(stale_environment):
    reviewer = load_script("telegram_reviewer.py", stale_environment)
    generator = load_script("verified_generator.py", stale_environment)
    network = Mock(side_effect=AssertionError("Telegram transport must not execute"))
    wait = Mock(side_effect=AssertionError("Retired review must never poll or wait"))
    child = Mock(side_effect=AssertionError("Retired reviewer subprocess must not launch"))
    stale_environment.setattr("urllib.request.urlopen", network)
    stale_environment.setattr("time.sleep", wait)
    stale_environment.setattr(subprocess, "run", child)
    assert reviewer["send_telegram_message"]("synthetic", "synthetic", "synthetic") == {
        "ok": False,
        "retired": True,
    }
    assert reviewer["get_updates"]("synthetic") == []
    assert reviewer["poll_for_decision"]("synthetic", "synthetic", "synthetic") == "retired"
    assert generator["run_telegram_review"]("unused", "unused", "synthetic") == "retired"
    network.assert_not_called()
    wait.assert_not_called()
    child.assert_not_called()


@pytest.mark.parametrize(
    "command",
    [["/bin/bash", "setup_telegram_bot.sh"], [sys.executable, "scripts/telegram_reviewer.py"]],
)
def test_retired_bootstrap_and_reviewer_cli_refuse_without_prompt_or_credential_output(
    stale_environment, command
):
    result = subprocess.run(
        command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0
    assert "retired" in (result.stdout + result.stderr).lower()
    assert "synthetic-stale-token" not in result.stdout + result.stderr


@pytest.mark.asyncio
async def test_omnichannel_assignment_keeps_db_action_and_cache_without_telegram(stale_environment):
    from backend.app.routers import omnichannel

    db = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=AssertionError("Telegram preview read must be skipped"))
    )
    manager = SimpleNamespace(assign_thread=AsyncMock())
    invalidate = AsyncMock()
    stale_environment.setattr(omnichannel, "can_view_all_clients", lambda user: True)
    stale_environment.setattr(omnichannel, "_get_db", lambda request: db)
    stale_environment.setattr(omnichannel, "_get_thread_manager", lambda pool: manager)
    stale_environment.setattr(omnichannel, "invalidate_cache", invalidate)
    bot = Mock(side_effect=AssertionError("Telegram service must not be constructed"))
    stale_environment.setattr(
        "backend.services.integrations.telegram_bot_service.TelegramBotService", bot
    )
    thread = UUID("00000000-0000-0000-0000-000000000001")
    result = await omnichannel.assign_thread(
        SimpleNamespace(), thread, "fixture", {"role": "admin"}
    )
    assert result["status"] == "assigned"
    manager.assign_thread.assert_awaited_once_with(thread, "fixture")
    invalidate.assert_awaited_once()
    bot.assert_not_called()
    db.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_credit_sentinel_retains_whatsapp_notifier_without_telegram(stale_environment):
    from backend.services.hardening import llm_credit_sentinel_cli as cli

    assert cli._build_notifiers(None) == {}
    svc = SimpleNamespace(
        send_message=AsyncMock(return_value={"messages": [{"id": "synthetic"}]}), close=AsyncMock()
    )
    stale_environment.setattr(
        "backend.services.integrations.whatsapp_service.WhatsAppService", lambda: svc
    )
    notifiers = cli._build_notifiers("synthetic-destination")
    assert set(notifiers) == {"whatsapp:surya"}
    assert await notifiers["whatsapp:surya"]("synthetic") is True
    svc.send_message.assert_awaited_once()
    svc.close.assert_awaited_once()


def test_all_live_api_literal_modules_import_the_shared_policy():
    """Regression for the missed scripts/ half of the Rakyat production tree."""
    transports = []
    for path in ROOT.rglob("*.py"):
        if any(part in {"tests", ".venv", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        direct = any(
            isinstance(node, ast.Constant)
            and id(node) not in docstrings
            and isinstance(node.value, str)
            and "api.telegram.org" in node.value
            for node in ast.walk(tree)
        )
        if direct:
            transports.append(path)
            assert "telegram_is_retired" in text, str(path.relative_to(ROOT))
    assert len(transports) >= 20  # proves the full-tree census did not silently shrink to backend/
    assert any(path.parent == ROOT / "scripts" for path in transports)


def test_legal_radar_core_scan_runs_without_telegram_credentials(stale_environment, capsys):
    module = load_script("legal_radar.py", stale_environment)
    main = module["main"]
    research = Mock(return_value="synthetic research result")
    send = Mock(side_effect=AssertionError("Retired transport must not be called"))
    stale_environment.setitem(main.__globals__, "dispatch_gemini_search", research)
    stale_environment.setitem(main.__globals__, "send_telegram_message", send)
    stale_environment.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    stale_environment.delenv("TELEGRAM_ZERO_CHAT_ID", raising=False)
    assert main() is None
    assert research.call_count == len(module["DOMAINS"])
    send.assert_not_called()
    assert "sent to Telegram" not in capsys.readouterr().out
