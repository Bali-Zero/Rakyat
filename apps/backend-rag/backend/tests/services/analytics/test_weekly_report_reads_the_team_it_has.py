"""The weekly report reads the team it has and says sent only when sent (real PostgreSQL).

`team_members` carries `active` (migration 137), never `is_active`; the activity query binds a
timedelta, never a string; the mail goes through the sanctioned send-email door and the scheduler marks a day as
sent only when it was delivered, retrying every 10 minutes inside a two-hour window.
Each test gets a throwaway schema cloned from the migrated test database. Invented rows only.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from backend.services.analytics import weekly_email_reporter as wer
from backend.services.analytics.weekly_email_reporter import WeeklyEmailReporter

_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://test:test@localhost:5433/test")


@pytest_asyncio.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    try:
        admin = await asyncpg.connect(_DSN)
    except Exception:
        pytest.skip("isolated test Postgres is not reachable")
    schema = f"weekly_t_{uuid.uuid4().hex[:12]}"
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(
            f"""
            SET search_path = {schema};
            CREATE TABLE team_members (LIKE public.team_members INCLUDING ALL);
            ALTER TABLE team_members ALTER COLUMN id SET DEFAULT gen_random_uuid()::text;
            -- migration_040 (a Python migration, absent from the migrated kit), same shape
            CREATE TABLE email_activity_log (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) NOT NULL REFERENCES team_members(id) ON DELETE CASCADE,
                user_email TEXT NOT NULL,
                operation VARCHAR(50) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE TABLE zoho_email_cache (user_id VARCHAR(255), is_read BOOLEAN);
            """
        )
        pool = await asyncpg.create_pool(
            _DSN, min_size=1, max_size=3, server_settings={"search_path": schema}
        )
        try:
            yield pool
        finally:
            await pool.close()
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _member(pool: asyncpg.Pool, email: str, name: str, active: bool) -> None:
    await pool.execute(
        "INSERT INTO team_members (email, full_name, department, active, pin_hash)"
        " VALUES ($1, $2, 'Contoh', $3, 'x')",
        email,
        name,
        active,
    )


async def test_the_report_team_query_returns_active_members_only(pg_pool):
    await _member(pg_pool, "aktif@example.test", "Contoh Aktif", True)
    await _member(pg_pool, "pergi@example.test", "Contoh Pergi", False)

    team = await WeeklyEmailReporter(pg_pool)._get_team_members()

    assert [m["email"] for m in team] == ["aktif@example.test"]


async def test_the_activity_query_counts_only_the_day_window(pg_pool):
    await _member(pg_pool, "aktif@example.test", "Contoh Aktif", True)
    uid = await pg_pool.fetchval("SELECT id FROM team_members")
    now = datetime.now(timezone.utc)
    for age_days in (1, 2, 30):
        await pg_pool.execute(
            "INSERT INTO email_activity_log (user_id, user_email, operation, created_at)"
            " VALUES ($1, 'aktif@example.test', 'sent', $2)",
            uid,
            now - timedelta(days=age_days),
        )

    out = await WeeklyEmailReporter(pg_pool)._get_email_activity(uid, days=7)

    assert out["sent"] == 2


def _sunday(hour: int, minute: int) -> datetime:
    """The coming Sunday (today if it is one) at hour:minute, in the reporter's own zone."""
    now = datetime.now(wer.BALI_TZ)
    day = now + timedelta(days=(wer.SUNDAY - now.weekday()) % 7)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _drive(monkeypatch, reporter, start: datetime, ticks: int, results: list[bool]):
    """Run the scheduler through `ticks` one-minute ticks from `start`; returns attempt times."""
    clock = {"now": start}
    attempts: list[datetime] = []

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    left = {"n": ticks}

    async def _sleep(_s):
        clock["now"] += timedelta(seconds=60)
        left["n"] -= 1
        if left["n"] <= 0:
            reporter.running = False

    queue = list(results)

    async def _send():
        attempts.append(clock["now"])
        return queue.pop(0) if queue else True

    monkeypatch.setattr(wer, "datetime", _Clock)
    monkeypatch.setattr(wer.asyncio, "sleep", _sleep)
    monkeypatch.setattr(reporter, "_send_weekly_report", _send)
    reporter.running = True
    await reporter._scheduler_loop()
    return attempts


async def test_the_sender_uses_the_sanctioned_door_and_no_smtp(monkeypatch):
    reporter = WeeklyEmailReporter(object())
    reporter._get_team_members = AsyncMock(return_value=[])
    reporter._get_weekly_summary = AsyncMock(return_value={})
    reporter._build_html_email = lambda *_a, **_k: "<p>Contoh</p>"
    door = AsyncMock(return_value=True)
    monkeypatch.setattr(wer, "send_internal_email", door)

    assert await reporter._send_weekly_report() is True

    door.assert_awaited_once()
    kwargs = door.await_args.kwargs
    assert kwargs["to"] == wer.ADMIN_EMAIL
    assert kwargs["subject"].startswith("[REPORT]") and "Weekly Email Report" in kwargs["subject"]
    assert kwargs["body"] == "<p>Contoh</p>"
    assert "pool" not in kwargs and "email_type" not in kwargs
    assert not hasattr(wer, "aiosmtplib")


async def test_a_door_that_did_not_deliver_makes_the_send_false(monkeypatch):
    reporter = WeeklyEmailReporter(object())
    reporter._get_team_members = AsyncMock(return_value=[])
    reporter._get_weekly_summary = AsyncMock(return_value={})
    reporter._build_html_email = lambda *_a, **_k: "<p>Contoh</p>"
    monkeypatch.setattr(wer, "send_internal_email", AsyncMock(return_value=False))

    assert await reporter._send_weekly_report() is False


async def test_a_failed_send_does_not_mark_the_day_sent_and_logs_no_sent_line(monkeypatch, caplog):
    reporter = WeeklyEmailReporter(object())
    caplog.set_level(logging.INFO, logger=wer.logger.name)

    attempts = await _drive(monkeypatch, reporter, _sunday(16, 0), 5, [False, False])

    assert len(attempts) == 1  # the retry is not due inside 10 minutes
    assert not [r for r in caplog.records if "report sent" in r.getMessage()]
    assert [r for r in caplog.records if "was not sent" in r.getMessage()]


async def test_a_failed_attempt_is_retried_ten_minutes_later_and_then_marked_sent(monkeypatch):
    reporter = WeeklyEmailReporter(object())
    start = _sunday(16, 0)

    attempts = await _drive(monkeypatch, reporter, start, 40, [False, True])

    assert attempts == [start, start + timedelta(minutes=10)]


async def test_a_success_at_the_check_time_sends_exactly_once_across_the_window(monkeypatch):
    reporter = WeeklyEmailReporter(object())

    attempts = await _drive(monkeypatch, reporter, _sunday(16, 0), 120, [True])

    assert len(attempts) == 1


async def test_the_window_closes_at_the_end_and_a_late_start_does_not_send(monkeypatch):
    late = await _drive(monkeypatch, WeeklyEmailReporter(object()), _sunday(18, 0), 5, [True])
    assert late == []


async def test_a_failing_report_gives_up_when_the_window_closes(monkeypatch):
    attempts = await _drive(
        monkeypatch, WeeklyEmailReporter(object()), _sunday(16, 0), 180, [False] * 50
    )

    assert attempts and all(a.hour < wer.CHECK_TIME_HOUR + 2 for a in attempts)
    assert len(attempts) == 12  # 16:00, 16:10 ... 17:50


async def test_no_attempt_on_a_saturday(monkeypatch):
    saturday = _sunday(16, 0) - timedelta(days=1)

    attempts = await _drive(monkeypatch, WeeklyEmailReporter(object()), saturday, 10, [True])

    assert attempts == []


async def test_the_logs_carry_the_exception_class_never_its_text(monkeypatch, caplog):
    reporter = WeeklyEmailReporter(object())
    reporter._get_team_members = AsyncMock(return_value=[])
    reporter._get_weekly_summary = AsyncMock(return_value={})
    reporter._build_html_email = lambda *_a, **_k: "<p>Contoh</p>"
    monkeypatch.setattr(
        wer,
        "send_internal_email",
        AsyncMock(side_effect=RuntimeError("contoh@example.test refused")),
    )
    caplog.set_level(logging.DEBUG, logger=wer.logger.name)

    assert await reporter._send_weekly_report() is False

    assert "contoh@example.test" not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_the_loop_error_log_carries_the_exception_class_only(monkeypatch, caplog):
    reporter = WeeklyEmailReporter(object())
    caplog.set_level(logging.DEBUG, logger=wer.logger.name)
    boom = AsyncMock(side_effect=RuntimeError("contoh@example.test refused"))
    start = _sunday(16, 0)
    clock = {"now": start}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    async def _sleep(_s):
        reporter.running = False

    monkeypatch.setattr(wer, "datetime", _Clock)
    monkeypatch.setattr(wer.asyncio, "sleep", _sleep)
    monkeypatch.setattr(reporter, "_send_weekly_report", boom)
    reporter.running = True
    await reporter._scheduler_loop()

    assert "contoh@example.test" not in caplog.text
    assert "RuntimeError" in caplog.text
