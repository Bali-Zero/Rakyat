"""Unit tests for extended wa_human_handoff paths (no pool, Connection, direct thread, new reasons)."""

from __future__ import annotations

import pytest


class TestNotifyHumanHandoffExtendedPaths:
    @pytest.mark.asyncio
    async def test_pool_none_and_thread_id_none_still_sends_redacted_email(
        self, monkeypatch
    ) -> None:
        """Direct router escalations (pool=None, thread_id=None) redact phone & send email."""
        from backend.services.integrations import human_escalation_notifier, wa_human_handoff

        monkeypatch.setattr(human_escalation_notifier, "_recent_escalations", {})
        monkeypatch.setattr(wa_human_handoff, "_recent_failed_attempts", {})

        calls: list[dict] = []

        async def _fake_send(**kwargs):
            calls.append(kwargs)
            return True

        monkeypatch.setattr(wa_human_handoff, "send_internal_email", _fake_send)

        ok = await wa_human_handoff.notify_human_handoff(
            None,
            thread_id=None,
            counterpart_phone="001001",
            language="?",
            reason="ai_escalation",
        )

        assert ok is True
        assert len(calls) == 1
        assert calls[0]["to"] == wa_human_handoff._FALLBACK_RECIPIENT
        assert "001001" not in calls[0]["subject"]
        assert "001001" not in calls[0]["body"]
        assert "Bahasa klien: en" in calls[0]["body"]
        assert "Eskalasi AI" in calls[0]["subject"]

    @pytest.mark.asyncio
    async def test_direct_connection_no_acquire_and_terminal_apology_reason(
        self, monkeypatch
    ) -> None:
        """Passing a bare asyncpg Connection works and normalizes terminal_apology:*."""
        from backend.services.integrations import human_escalation_notifier, wa_human_handoff

        monkeypatch.setattr(human_escalation_notifier, "_recent_escalations", {})
        monkeypatch.setattr(wa_human_handoff, "_recent_failed_attempts", {})

        class _BareConn:
            def __init__(self) -> None:
                self.executed: list[tuple[str, tuple]] = []

            async def fetchrow(self, sql: str, *args: object) -> dict:
                return {"id": 91, "assigned_to": "synthetic-assignee"}

            async def execute(self, sql: str, *args: object) -> None:
                self.executed.append((sql, args))

        calls: list[dict] = []

        async def _fake_send(**kwargs):
            calls.append(kwargs)
            return True

        monkeypatch.setattr(wa_human_handoff, "send_internal_email", _fake_send)

        conn = _BareConn()
        ok = await wa_human_handoff.notify_human_handoff(
            conn,
            thread_id=505,
            counterpart_phone="001002",
            language="id",
            reason="terminal_apology:max_attempts",
        )

        assert ok is True
        assert len(conn.executed) == 1
        assert calls[0]["to"] == "synthetic-assignee"
        assert calls[0]["pool"] is None
        assert "Outbox terminal apology" in calls[0]["subject"]
