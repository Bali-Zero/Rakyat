"""Guard: LKPM days late/remaining are WITA calendar days (DAI-141).

`get_deadlines` used to floor a UTC timestamp difference, so 15 Oct -> 10 Nov
(14:00 WITA) read 27 days late while the portal dashboard (#150) says 26. The
-30 day window is unchanged: a deadline exactly 30 calendar days late is still
listed, 31 is not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import backend.services.compliance.lkpm_service as lkpm_module

WITA = ZoneInfo("Asia/Makassar")


def _frozen(now_wita: datetime) -> type[datetime]:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return now_wita.astimezone(tz) if tz else now_wita.replace(tzinfo=None)

    return _Frozen


@pytest.fixture
def service():
    with (
        patch.object(lkpm_module, "LKPMValidator"),
        patch.object(lkpm_module, "LKPMDataCollector"),
    ):
        return lkpm_module.LKPMService(MagicMock())


def _q3(service, now_wita: datetime, monkeypatch):
    monkeypatch.setattr(lkpm_module, "datetime", _frozen(now_wita))
    return [d for d in service.get_deadlines(days_ahead=30) if d.quarter == "Q3"]


def test_overdue_days_are_wita_calendar_days(service, monkeypatch) -> None:
    # 10 Nov 2026 14:00 WITA = 06:00 UTC; Q3 due 15 Oct.
    (q3,) = _q3(service, datetime(2026, 11, 10, 14, 0, tzinfo=WITA), monkeypatch)

    assert q3.days_remaining == -26
    assert q3.is_overdue is True
    assert q3.deadline == datetime(2026, 10, 15, tzinfo=timezone.utc)


def test_wita_day_rolls_over_at_wita_midnight(service, monkeypatch) -> None:
    # 11 Nov 00:30 WITA is still 10 Nov in UTC; the count follows Bali's date.
    (q3,) = _q3(service, datetime(2026, 11, 11, 0, 30, tzinfo=WITA), monkeypatch)

    assert q3.days_remaining == -27


def test_minus_30_window_is_unchanged(service, monkeypatch) -> None:
    (q3,) = _q3(service, datetime(2026, 11, 14, 14, 0, tzinfo=WITA), monkeypatch)
    assert q3.days_remaining == -30

    assert _q3(service, datetime(2026, 11, 15, 14, 0, tzinfo=WITA), monkeypatch) == []
