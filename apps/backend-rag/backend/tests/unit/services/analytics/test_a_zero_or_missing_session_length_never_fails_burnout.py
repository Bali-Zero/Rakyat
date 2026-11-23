"""A zero or NULL session length never fails burnout detection for the whole team."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.analytics.burnout_detector import BurnoutDetectorService

_START = datetime(2026, 11, 2, 9, 0)


def _session(email: str, day: int, minutes: Any, conversations: Any = 10, dow: int = 2) -> dict:
    return {
        "user_name": email.split("@")[0],
        "user_email": email,
        "session_start": _START + timedelta(days=day),
        "duration_minutes": minutes,
        "conversations_count": conversations,
        "activities_count": 0,
        "day_of_week": dow,
    }


async def _detect(rows: list[dict]) -> list[dict]:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return await BurnoutDetectorService(db_pool=pool).detect_burnout_signals()


def _healthy(email: str) -> list[dict]:
    # Six sessions, the last ones long enough to raise one warning (Check 1),
    # so a healthy colleague is in the result when the batch survives.
    return [_session(email, d, m) for d, m in enumerate([300, 300, 300, 600, 600, 600])]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "minutes",
    [
        [480, 480, 480, 0, 0, 0],
        [0, 0, 0, 480, 480, 480],
        [480, None, 480, 480],
        [None, None, None, None, None, None],
        [0, 0, 0, 0, 0, 0],
    ],
    ids=["last-three-zero", "first-three-zero", "one-null", "all-null", "all-zero"],
)
async def test_one_member_with_empty_sessions_does_not_drop_the_team(minutes: list) -> None:
    rows = [_session("odd.member@example.invalid", d, m) for d, m in enumerate(minutes)]
    rows += _healthy("steady.member@example.invalid")

    result = await _detect(rows)

    assert "steady.member@example.invalid" in {r["email"] for r in result}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_null_conversation_count_counts_as_none() -> None:
    rows = [
        _session("odd.member@example.invalid", d, 480, conversations=c)
        for d, c in enumerate([10, None, 10, 10, None, 10])
    ]

    assert await _detect(rows) == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hours_rising_from_nothing_is_not_a_thirty_percent_rise() -> None:
    rows = [
        _session("odd.member@example.invalid", d, m)
        for d, m in enumerate([0, 0, 0, 0, 0, 480, 480])
    ]

    result = await _detect(rows)

    signals = [w for r in result for w in r["warning_signals"]]
    assert not any("increasing" in w for w in signals)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_real_rise_and_a_real_efficiency_drop_still_warn() -> None:
    # Five 5-hour sessions with 30 conversations, then five 10-hour ones with 5.
    rows = [
        _session(
            "busy.member@example.invalid",
            d,
            300 if d < 5 else 600,
            conversations=30 if d < 5 else 5,
        )
        for d in range(10)
    ]

    result = await _detect(rows)

    signals = result[0]["warning_signals"]
    assert any("increasing" in w for w in signals)
    assert any("efficiency" in w for w in signals)
