"""LKPM deadlines include the prior year's Q4 through its January due date."""

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


def _deadlines(service, now_wita: datetime, monkeypatch, days_ahead: int = 30):
    monkeypatch.setattr(lkpm_module, "datetime", _frozen(now_wita))
    return service.get_deadlines(days_ahead=days_ahead)


def test_q4_of_previous_year_is_upcoming_on_january_5(service, monkeypatch) -> None:
    deadlines = _deadlines(service, datetime(2027, 1, 5, tzinfo=WITA), monkeypatch)

    q4 = [deadline for deadline in deadlines if deadline.quarter == "Q4"]
    assert len(q4) == 1
    q4 = q4[0]
    assert q4.year == 2026
    assert q4.deadline == datetime(2027, 1, 15, tzinfo=timezone.utc)
    assert q4.days_remaining == 10
    assert q4.is_overdue is False


def test_q4_of_previous_year_is_overdue_on_january_25(service, monkeypatch) -> None:
    deadlines = _deadlines(service, datetime(2027, 1, 25, tzinfo=WITA), monkeypatch)

    q4 = [deadline for deadline in deadlines if deadline.quarter == "Q4"]
    assert len(q4) == 1
    q4 = q4[0]
    assert q4.year == 2026
    assert q4.deadline == datetime(2027, 1, 15, tzinfo=timezone.utc)
    assert q4.days_remaining == -10
    assert q4.is_overdue is True


def test_deadlines_keep_current_q4_and_q1_behavior_without_duplicates(service, monkeypatch) -> None:
    december = _deadlines(service, datetime(2026, 12, 20, tzinfo=WITA), monkeypatch)
    q4 = [deadline for deadline in december if deadline.quarter == "Q4"]
    assert len(q4) == 1
    assert q4[0].year == 2026
    assert q4[0].deadline == datetime(2027, 1, 15, tzinfo=timezone.utc)
    assert q4[0].days_remaining == 26

    april = _deadlines(service, datetime(2026, 4, 10, tzinfo=WITA), monkeypatch)
    (q1,) = [deadline for deadline in april if deadline.quarter == "Q1"]
    assert q1.year == 2026
    assert q1.deadline == datetime(2026, 4, 15, tzinfo=timezone.utc)
    assert q1.days_remaining == 5

    wide_window = _deadlines(
        service,
        datetime(2026, 12, 20, tzinfo=WITA),
        monkeypatch,
        days_ahead=400,
    )
    assert len({(deadline.quarter, deadline.year) for deadline in wide_window}) == len(
        wide_window,
    )
