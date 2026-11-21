"""Leave request: the days sent by the client cannot exceed the calendar days of the period."""

from datetime import date

import pytest
from pydantic import ValidationError

from backend.app.routers.hr import LeaveRequestCreate


def _payload(start: date, end: date, days: int) -> dict:
    return {"leave_type_id": 1, "start_date": start, "end_date": end, "total_days": days}


def test_days_larger_than_calendar_span_are_rejected() -> None:
    with pytest.raises(ValidationError, match="total_days"):
        LeaveRequestCreate(**_payload(date(2027, 3, 1), date(2027, 3, 3), 4))


def test_days_far_larger_than_span_are_rejected() -> None:
    with pytest.raises(ValidationError, match="total_days"):
        LeaveRequestCreate(**_payload(date(2027, 3, 1), date(2027, 3, 1), 30))


def test_days_equal_to_calendar_span_are_accepted() -> None:
    data = LeaveRequestCreate(**_payload(date(2027, 3, 1), date(2027, 3, 3), 3))
    assert data.total_days == 3


def test_working_days_below_calendar_span_are_accepted() -> None:
    data = LeaveRequestCreate(**_payload(date(2027, 3, 1), date(2027, 3, 31), 22))
    assert data.total_days == 22


def test_single_day_request_is_accepted() -> None:
    data = LeaveRequestCreate(**_payload(date(2027, 3, 1), date(2027, 3, 1), 1))
    assert data.total_days == 1
