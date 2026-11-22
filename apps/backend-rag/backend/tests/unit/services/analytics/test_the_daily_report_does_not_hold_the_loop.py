"""The daily check-in report does not hold the event loop while it talks to the mail server.

The report is sent from a coroutine on the API's own loop with the blocking ``smtplib`` and no
timeout. A server that accepts the connection and then says nothing held the loop, and with it
every request, for as long as it stayed silent. Here a real local socket plays that server.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.analytics import daily_checkin_notifier as notifier_module
from backend.services.analytics.daily_checkin_notifier import DailyCheckinNotifier


class _SilentServer:
    """Accepts one connection, never sends the greeting, and hangs up after ``hold`` seconds."""

    def __init__(self, hold: float) -> None:
        self._hold = hold
        self._released = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(10)
        try:
            connection, _ = self._listener.accept()
        except OSError:
            return
        self._released.wait(self._hold)
        connection.close()

    def close(self) -> None:
        self._released.set()
        self._thread.join(timeout=5)
        self._listener.close()


@pytest.fixture
def silent_server(request: pytest.FixtureRequest) -> Iterator[_SilentServer]:
    server = _SilentServer(hold=request.param)
    yield server
    server.close()


def _notifier_pointed_at(server: _SilentServer) -> tuple[DailyCheckinNotifier, MagicMock]:
    settings = MagicMock()
    settings.smtp_host = "127.0.0.1"
    settings.smtp_port = server.port
    settings.smtp_user = "zz-user"
    settings.smtp_password = "zz"  # pragma: allowlist secret
    settings.smtp_from = "zz@example.test"
    settings.smtp_use_tls = False
    notifier = DailyCheckinNotifier(MagicMock())
    notifier._get_checkin_data = AsyncMock(return_value=[])
    notifier._get_online_status = AsyncMock(return_value=[])
    return notifier, settings


async def _beats_while(work) -> tuple[int, float]:
    """How many times a 20 ms heartbeat on the same loop ran while ``work`` ran, and for how long."""
    beats = 0
    running = True

    async def heart() -> None:
        nonlocal beats
        while running:
            await asyncio.sleep(0.02)
            beats += 1

    task = asyncio.create_task(heart())
    await asyncio.sleep(0)  # let the heartbeat start before the work does
    started = time.monotonic()
    try:
        await work()
    finally:
        elapsed = time.monotonic() - started
        running = False
        await task
    return beats, elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize("silent_server", [1.0], indirect=True)
async def test_the_loop_keeps_running_while_the_mail_server_stays_silent(
    silent_server: _SilentServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    notifier, settings = _notifier_pointed_at(silent_server)

    with patch.object(notifier_module, "settings", settings):
        beats, elapsed = await _beats_while(notifier._send_daily_report)

    # The server hangs up after one second: a free loop beats about fifty times
    # in that second, a held loop does not beat at all.
    assert elapsed >= 0.8
    assert beats >= 15
    assert "Failed to send daily email" in caplog.text
    assert "Daily check-in email sent" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("silent_server", [6.0], indirect=True)
async def test_a_silent_mail_server_is_given_up_on_after_the_timeout(
    silent_server: _SilentServer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(notifier_module, "SMTP_TIMEOUT_SECONDS", 0.3, raising=False)
    notifier, settings = _notifier_pointed_at(silent_server)

    with patch.object(notifier_module, "settings", settings):
        _beats, elapsed = await _beats_while(notifier._send_daily_report)

    # The server would stay silent for six seconds; the report gives up after 0.3.
    assert elapsed < 3.0
    assert "Failed to send daily email" in caplog.text


@pytest.mark.asyncio
async def test_the_report_is_still_sent_with_the_same_conversation() -> None:
    """Connect with a timeout, TLS when configured, login, send: nothing else changed."""
    notifier = DailyCheckinNotifier(MagicMock())
    notifier._get_checkin_data = AsyncMock(return_value=[])
    notifier._get_online_status = AsyncMock(return_value=[])
    settings = MagicMock()
    settings.smtp_host = "mail.example.test"
    settings.smtp_port = 587
    settings.smtp_user = "zz-user"
    settings.smtp_password = "zz"  # pragma: allowlist secret
    settings.smtp_from = "zz@example.test"
    settings.smtp_use_tls = True

    with (
        patch.object(notifier_module, "settings", settings),
        patch.object(notifier_module.smtplib, "SMTP") as smtp,
    ):
        await notifier._send_daily_report()

    smtp.assert_called_once_with("mail.example.test", 587, timeout=30)
    server = smtp.return_value.__enter__.return_value
    assert [call[0] for call in server.method_calls] == ["starttls", "login", "send_message"]
    server.login.assert_called_once_with("zz-user", "zz")
