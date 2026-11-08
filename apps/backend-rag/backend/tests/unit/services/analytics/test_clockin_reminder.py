"""Tests for clock-in reminder logic in AttendanceMonitor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_pool():
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)
    return pool, conn


@pytest.mark.asyncio
async def test_get_logged_in_not_clocked_returns_missing(mock_pool):
    """Should find members who logged in but didn't clock in."""
    from backend.services.analytics.attendance_monitor import AttendanceMonitor

    pool, conn = mock_pool
    monitor = AttendanceMonitor(pool)

    conn.fetch.return_value = [
        {"email": "ari.firda@balizero.com"},
        {"email": "dea@balizero.com"},
    ]

    result = await monitor.get_logged_in_not_clocked()

    assert len(result) == 2
    assert result[0]["email"] == "ari.firda@balizero.com"
    assert result[1]["email"] == "dea@balizero.com"
    conn.fetch.assert_called_once()
    # Verify the SQL checks both auth_audit_log and team_timesheet
    sql = conn.fetch.call_args[0][0]
    assert "auth_audit_log" in sql
    assert "team_timesheet" in sql


@pytest.mark.asyncio
async def test_get_logged_in_not_clocked_empty(mock_pool):
    """Should return empty when everyone clocked in."""
    from backend.services.analytics.attendance_monitor import AttendanceMonitor

    pool, conn = mock_pool
    monitor = AttendanceMonitor(pool)
    conn.fetch.return_value = []

    result = await monitor.get_logged_in_not_clocked()
    assert len(result) == 0


@pytest.mark.asyncio
async def test_retired_clockin_reminder_ignores_stale_telegram_configuration(mock_pool):
    """Retirement blocks transport construction, independently of attendance data."""
    from backend.services.analytics.attendance_monitor import AttendanceMonitor

    pool, conn = mock_pool
    monitor = AttendanceMonitor(pool)
    conn.fetch.return_value = [{"email": "member@example.test"}]

    with (
        patch.dict(
            "os.environ", {"TELEGRAM_BOT_TOKEN": "fake-token", "TELEGRAM_OWNER_CHAT_ID": "12345"}
        ),
        patch("backend.services.analytics.attendance_monitor.httpx.AsyncClient") as client,
    ):
        result = await monitor.send_clockin_reminder()

    assert result is None
    client.assert_not_called()
    client.return_value.post.assert_not_called()


@pytest.mark.asyncio
async def test_retired_clockin_reminder_remains_inert_with_no_missing_members(mock_pool):
    """The retired default remains inert for empty attendance data."""
    from backend.services.analytics.attendance_monitor import AttendanceMonitor

    pool, conn = mock_pool
    monitor = AttendanceMonitor(pool)
    conn.fetch.return_value = []

    with patch("backend.services.analytics.attendance_monitor.httpx.AsyncClient") as mock_cls:
        await monitor.send_clockin_reminder()
        mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_retired_clockin_reminder_remains_inert_without_token(mock_pool):
    """The retired default remains inert without a Telegram token."""
    from backend.services.analytics.attendance_monitor import AttendanceMonitor

    pool, conn = mock_pool
    monitor = AttendanceMonitor(pool)

    conn.fetch.return_value = [{"email": "ari.firda@balizero.com"}]

    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ""}, clear=False):
        with patch("backend.services.analytics.attendance_monitor.httpx.AsyncClient") as mock_cls:
            await monitor.send_clockin_reminder()
            mock_cls.assert_not_called()  # Should not attempt to send
