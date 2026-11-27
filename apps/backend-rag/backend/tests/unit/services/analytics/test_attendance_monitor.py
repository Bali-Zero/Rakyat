"""
Supplementary unit tests for backend/services/analytics/attendance_monitor.py
Covers areas NOT in test_unit_attendance_monitor.py:
- _chunk_telegram_message
- _render_email_shell
- _get_member_by_email
- _has_approved_leave
- _post_email
- _send_gentle_reminder
- start_schedulers / stop_schedulers
- AttendanceMonitor init
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.services.analytics.attendance_monitor import (
    ADMIN_EMAIL,
    LATE_EXEMPT_EMAILS,
    AttendanceMonitor,
    _chunk_telegram_message,
    _render_email_shell,
)

BALI_TZ = ZoneInfo("Asia/Makassar")


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_pool() -> MagicMock:
    """Mock asyncpg pool."""
    pool = MagicMock()
    conn = AsyncMock()

    class _Ctx:
        async def __aenter__(self) -> AsyncMock:
            return conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    pool.acquire = MagicMock(return_value=_Ctx())
    pool._conn = conn
    return pool


@pytest.fixture
def monitor(mock_pool: MagicMock) -> AttendanceMonitor:
    """Fresh AttendanceMonitor."""
    return AttendanceMonitor(mock_pool)


# ═══════════════════════════════════════════════════════════════════════════════
# _chunk_telegram_message
# ═══════════════════════════════════════════════════════════════════════════════


class TestChunkTelegramMessage:
    def test_short_message_unchanged(self) -> None:
        msg = "Hello world"
        result = _chunk_telegram_message(msg)
        assert result == [msg]

    def test_single_element_when_under_limit(self) -> None:
        msg = "A" * 100
        result = _chunk_telegram_message(msg, max_len=200)
        assert len(result) == 1

    def test_splits_long_message(self) -> None:
        lines = [f"Line {i}: {'x' * 50}" for i in range(100)]
        msg = "\n".join(lines)
        result = _chunk_telegram_message(msg, max_len=500)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 500

    def test_chunks_have_part_headers(self) -> None:
        msg = "\n".join(["x" * 100] * 50)
        result = _chunk_telegram_message(msg, max_len=300)
        assert len(result) > 1
        assert result[0].startswith("(part 1/")
        assert result[-1].startswith(f"(part {len(result)}/")

    def test_hard_breaks_very_long_lines(self) -> None:
        msg = "A" * 10000  # Single very long line
        result = _chunk_telegram_message(msg, max_len=500)
        assert len(result) > 1
        # The "(part N/M)\n" header adds ~14 chars on top of the max_len body
        for chunk in result:
            assert len(chunk) <= 520  # body <= 500 + header overhead

    def test_empty_message(self) -> None:
        result = _chunk_telegram_message("")
        assert result == [""]

    def test_exact_limit(self) -> None:
        msg = "A" * 4000
        result = _chunk_telegram_message(msg, max_len=4000)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# _render_email_shell
# ═══════════════════════════════════════════════════════════════════════════════


class TestRenderEmailShell:
    def test_contains_header(self) -> None:
        html = _render_email_shell(
            header_title="Test Title",
            accent="#ff0000",
            body_html="<p>Hello</p>",
        )
        assert "Test Title" in html
        assert "#ff0000" in html
        assert "<p>Hello</p>" in html

    def test_contains_footer(self) -> None:
        html = _render_email_shell(
            header_title="Title",
            accent="#000",
            body_html="body",
        )
        assert "Bali Zero" in html
        assert "Zantara" in html

    def test_is_valid_html(self) -> None:
        html = _render_email_shell(
            header_title="Title",
            accent="#000",
            body_html="<p>Content</p>",
        )
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html


# ═══════════════════════════════════════════════════════════════════════════════
# AttendanceMonitor.__init__
# ═══════════════════════════════════════════════════════════════════════════════


class TestAttendanceMonitorInit:
    def test_init(self, monitor: AttendanceMonitor) -> None:
        assert monitor._running is False
        assert monitor._escalation_task is None
        assert monitor._digest_task is None


# ═══════════════════════════════════════════════════════════════════════════════
# _has_approved_leave
# ═══════════════════════════════════════════════════════════════════════════════


class TestHasApprovedLeave:
    @pytest.mark.asyncio
    async def test_has_leave(self, monitor: AttendanceMonitor, mock_pool: MagicMock) -> None:
        mock_pool._conn.fetchrow = AsyncMock(return_value={"1": 1})
        result = await monitor._has_approved_leave("test@balizero.com", date(2026, 4, 7))
        assert result is True

    @pytest.mark.asyncio
    async def test_no_leave(self, monitor: AttendanceMonitor, mock_pool: MagicMock) -> None:
        mock_pool._conn.fetchrow = AsyncMock(return_value=None)
        result = await monitor._has_approved_leave("test@balizero.com", date(2026, 4, 7))
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# _get_member_by_email
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetMemberByEmail:
    @pytest.mark.asyncio
    async def test_found(self, monitor: AttendanceMonitor, mock_pool: MagicMock) -> None:
        mock_pool._conn.fetchrow = AsyncMock(
            return_value={"email": "a@balizero.com", "full_name": "Alice"},
        )
        result = await monitor._get_member_by_email("a@balizero.com")
        assert result is not None
        assert result["full_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_not_found(self, monitor: AttendanceMonitor, mock_pool: MagicMock) -> None:
        mock_pool._conn.fetchrow = AsyncMock(return_value=None)
        result = await monitor._get_member_by_email("unknown@balizero.com")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# _post_email
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def api_recipient(monkeypatch: pytest.MonkeyPatch) -> str:
    from backend.app.setup.log_redaction import install_api_log_redaction

    # Exercise the API policy and restore the process-wide logging hook on teardown.
    monkeypatch.setattr(logging.Handler, "handle", logging.Handler.handle)
    install_api_log_redaction()
    return "recipient@example.invalid"


class TestPostEmail:
    @pytest.mark.asyncio
    async def test_post_email_success(self, monitor: AttendanceMonitor) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("backend.services.analytics.attendance_monitor.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            await monitor._post_email(
                to="test@example.com",
                subject="Test",
                html_body="<p>Hello</p>",
                cc="cc@example.com",
                log_tag="test_email",
            )
            mock_client.post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_email_http_error(
        self,
        monitor: AttendanceMonitor,
        caplog: pytest.LogCaptureFixture,
        api_recipient: str,
    ) -> None:
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("err", request=MagicMock(), response=mock_response),
        )

        with patch("backend.services.analytics.attendance_monitor.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            # Should not raise — the HTTP error must be swallowed and logged,
            # not propagated to the caller.
            with caplog.at_level(logging.ERROR):
                await monitor._post_email(to=api_recipient, subject="Test", html_body="<p>x</p>")

        assert mock_client.post.await_args.kwargs["json"]["to"] == api_recipient
        assert "HTTP 500" in caplog.text and "Internal Server Error" in caplog.text
        assert "[REDACTED]" in caplog.text and api_recipient not in caplog.text

    @pytest.mark.asyncio
    async def test_post_email_connection_error(
        self,
        monitor: AttendanceMonitor,
        caplog: pytest.LogCaptureFixture,
        api_recipient: str,
    ) -> None:
        with patch("backend.services.analytics.attendance_monitor.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=ConnectionError("Network down"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            # Should not raise — the connection error must be swallowed and
            # logged as a warning, not propagated to the caller.
            with caplog.at_level(logging.WARNING):
                await monitor._post_email(to=api_recipient, subject="Test", html_body="<p>x</p>")

        assert "Network down" in caplog.text
        assert mock_client.post.await_args.kwargs["json"]["to"] == api_recipient
        assert "[REDACTED]" in caplog.text and api_recipient not in caplog.text


# ═══════════════════════════════════════════════════════════════════════════════
# _send_gentle_reminder
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendGentleReminder:
    @pytest.mark.asyncio
    async def test_sends_email(self, monitor: AttendanceMonitor) -> None:
        monitor._post_email = AsyncMock()

        await monitor._send_gentle_reminder("alice@balizero.com", "Alice Test", "09:35")

        monitor._post_email.assert_awaited_once()
        call_kwargs = monitor._post_email.call_args.kwargs
        assert call_kwargs["to"] == "alice@balizero.com"
        assert "09:35" in call_kwargs["subject"]
        # No CC for gentle reminder
        assert call_kwargs.get("cc") is None


# ═══════════════════════════════════════════════════════════════════════════════
# start_schedulers / stop_schedulers
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchedulerLifecycle:
    @pytest.mark.asyncio
    async def test_start_schedulers(self, monitor: AttendanceMonitor) -> None:
        with (
            patch.object(monitor, "_escalation_loop", new_callable=AsyncMock),
            patch.object(monitor, "_daily_digest_loop", new_callable=AsyncMock),
        ):
            await monitor.start_schedulers()
            assert monitor._running is True
            assert monitor._escalation_task is not None
            assert monitor._digest_task is not None
            # Clean up
            await monitor.stop_schedulers()

    @pytest.mark.asyncio
    async def test_start_schedulers_idempotent(self, monitor: AttendanceMonitor) -> None:
        monitor._running = True
        # Should return early without creating tasks
        await monitor.start_schedulers()
        assert monitor._escalation_task is None

    @pytest.mark.asyncio
    async def test_stop_schedulers(self, monitor: AttendanceMonitor) -> None:
        with (
            patch.object(monitor, "_escalation_loop", new_callable=AsyncMock),
            patch.object(monitor, "_daily_digest_loop", new_callable=AsyncMock),
        ):
            await monitor.start_schedulers()
            await monitor.stop_schedulers()
            assert monitor._running is False
            assert monitor._escalation_task is None
            assert monitor._digest_task is None

    @pytest.mark.asyncio
    async def test_stop_schedulers_noop(self, monitor: AttendanceMonitor) -> None:
        # Should not raise when not started, and should still normalize
        # the running flag / task attributes even with nothing to cancel.
        monitor._running = True

        await monitor.stop_schedulers()

        assert monitor._running is False
        assert monitor._escalation_task is None
        assert monitor._digest_task is None


# ═══════════════════════════════════════════════════════════════════════════════
# Constants verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestConstants:
    def test_admin_email(self) -> None:
        assert ADMIN_EMAIL == "zero@balizero.com"

    def test_exempt_emails(self) -> None:
        assert "zero@balizero.com" in LATE_EXEMPT_EMAILS
        assert "ruslana@balizero.com" in LATE_EXEMPT_EMAILS
        assert "veronika@balizero.com" not in LATE_EXEMPT_EMAILS
        assert "tax@balizero.com" not in LATE_EXEMPT_EMAILS


class TestRemovedAbsenceSummary:
    def test_absence_summary_members_are_removed(self) -> None:
        for name in (
            "check_absent_members",
            "_send_absent_alert",
            "_get_active_members",
            "_get_last_clockin_date",
            "_count_working_days_since",
        ):
            assert not hasattr(AttendanceMonitor, name)
