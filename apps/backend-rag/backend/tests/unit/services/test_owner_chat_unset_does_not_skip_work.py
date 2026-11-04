"""Token set + TELEGRAM_OWNER_CHAT_ID unset: report the real cause, skip only the send, keep the rest of the flow."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND_RAG = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _token_set_owner_unset(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)


def test_m13_checkpoint_still_promotes_and_returns_zero(monkeypatch, tmp_path, caplog):
    from backend.services.sota_loop import m13_checkpoint as mod

    root = tmp_path / "research" / "sota-social-2026-v1"
    root.mkdir(parents=True)
    (root / ".loop_start_date").write_text((date.today() - timedelta(days=30)).isoformat())
    monkeypatch.setenv("NUZANTARA_REPO_ROOT", str(tmp_path))
    with (
        patch.object(mod, "promote_research_output") as promote,
        patch("urllib.request.urlopen") as urlopen,
        caplog.at_level(logging.WARNING),
    ):
        assert mod.main() == 0
    promote.assert_called_once()
    urlopen.assert_not_called()
    assert "TELEGRAM_OWNER_CHAT_ID not set" in caplog.text


def test_m13_weekly_logs_the_missing_chat_id(caplog):
    from backend.services.sota_loop import m13_weekly as mod

    with patch("urllib.request.urlopen") as urlopen, caplog.at_level(logging.WARNING):
        mod._notify_telegram({}, [], False, "")
    urlopen.assert_not_called()
    assert "TELEGRAM_OWNER_CHAT_ID not set" in caplog.text


def _load(name: str):
    path = BACKEND_RAG / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_unset_probe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "name,func",
    [
        ("run_entity_linker_context_header", "_maybe_notify"),
        ("run_entity_linker_doc_propagation", "_maybe_notify"),
        ("run_entity_linker_full", "_maybe_notify"),
        ("generate_community_summaries", "_maybe_notify"),
        ("crm_automation_engine", "send_telegram"),
        ("crm_quality_bot", "send_telegram"),
    ],
)
def test_scripts_name_the_missing_chat_id_and_send_nothing(monkeypatch, caplog, name, func):
    monkeypatch.setenv("ENTITY_LINKER_DB_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:1/x")
    monkeypatch.setenv("QDRANT_API_KEY", "x")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:1")
    module = _load(name)
    monkeypatch.setattr(module, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(module, "TELEGRAM_CHAT_ID", "")
    with patch("httpx.AsyncClient") as client, caplog.at_level(logging.DEBUG):
        asyncio.run(getattr(module, func)("hello"))
    client.assert_not_called()
    assert "TELEGRAM_OWNER_CHAT_ID not set" in caplog.text
    assert "TELEGRAM_BOT_TOKEN not set" not in caplog.text


def test_email_audit_names_the_missing_chat_id(monkeypatch, caplog):
    from backend.services.notifications import email_audit

    monkeypatch.setattr(email_audit, "_OWNER_CHAT_ID", "")
    with patch("backend.services.notifications.email_audit.urllib.request.urlopen") as urlopen:
        with caplog.at_level(logging.WARNING):
            email_audit.notify_email_failure_critical(
                email_type="waiting_docs_client",
                to_email="client@example.test",
                subject="s",
                practice_id=1,
                error="e",
            )
    urlopen.assert_not_called()
    assert "TELEGRAM_OWNER_CHAT_ID not set" in caplog.text
    assert "TELEGRAM_BOT_TOKEN not set" not in caplog.text


def test_owner_chat_id_is_stripped(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", " 990000001 \n")
    from backend.core.env_lists import owner_telegram_chat_id

    assert owner_telegram_chat_id() == "990000001"
    import backend.services.review.review_handler as rh

    importlib = __import__("importlib")
    reloaded = importlib.reload(rh)
    assert reloaded.DEFAULT_OWNER_CHAT_ID == "990000001"
    monkeypatch.delenv("TELEGRAM_OWNER_CHAT_ID", raising=False)
    importlib.reload(rh)
