import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest

from backend.services.compliance.compliance_tracker import ComplianceTrackerService


def _deadline(days: float) -> datetime:
    return datetime.now(tz=UTC) + timedelta(days=days)


def test_get_upcoming_deadlines_reads_date_naive_and_z_deadlines() -> None:
    service = ComplianceTrackerService()
    date_only = service.add_compliance_item(
        "client-900001", "date", "Date only", _deadline(3).date().isoformat()
    )
    naive = service.add_compliance_item(
        "client-900001", "naive", "Naive", _deadline(2).replace(tzinfo=None).isoformat()
    )
    z_suffix = service.add_compliance_item(
        "client-900001", "z", "Z suffix", _deadline(1).isoformat().replace("+00:00", "Z")
    )

    upcoming = service.get_upcoming_deadlines(client_id="client-900001", days_ahead=10)

    assert {item.item_id for item in upcoming} == {
        date_only.item_id,
        naive.item_id,
        z_suffix.item_id,
    }


def test_get_upcoming_deadlines_skips_unparseable_deadline(caplog) -> None:
    service = ComplianceTrackerService()
    invalid = service.add_compliance_item("client-900001", "invalid", "Invalid", "not-a-deadline")
    good = service.add_compliance_item("client-900001", "good", "Good", _deadline(2).isoformat())

    with caplog.at_level(logging.WARNING):
        upcoming = service.get_upcoming_deadlines(client_id="client-900001", days_ahead=10)

    assert upcoming == [good]
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == [f"Skipping compliance item with invalid deadline: {invalid.item_id}"]


@pytest.mark.parametrize("deadline", ["0001-01-01T00:00+01:00", "9999-12-31T23:59-01:00"])
def test_get_upcoming_deadlines_skips_utc_overflow_deadline(caplog, deadline: str) -> None:
    service = ComplianceTrackerService()
    invalid = service.add_compliance_item("client-900001", "invalid", "Invalid", deadline)
    good = service.add_compliance_item("client-900001", "good", "Good", _deadline(2).isoformat())

    with caplog.at_level(logging.WARNING):
        upcoming = service.get_upcoming_deadlines(client_id="client-900001", days_ahead=10)

    assert upcoming == [good]
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == [f"Skipping compliance item with invalid deadline: {invalid.item_id}"]


def test_get_upcoming_deadlines_keeps_offset_window_filter_and_instant_order() -> None:
    service = ComplianceTrackerService()
    offset = service.add_compliance_item(
        "client-900001",
        "offset",
        "Offset",
        _deadline(1).astimezone(timezone(timedelta(hours=14))).isoformat(),
    )
    naive = service.add_compliance_item(
        "client-900001", "naive", "Naive", _deadline(1.5).replace(tzinfo=None).isoformat()
    )
    z_suffix = service.add_compliance_item(
        "client-900001", "z", "Z suffix", _deadline(3).isoformat().replace("+00:00", "Z")
    )
    service.add_compliance_item("client-900001", "beyond", "Beyond", _deadline(20).isoformat())
    service.add_compliance_item("client-900002", "other", "Other client", _deadline(0).isoformat())

    upcoming = service.get_upcoming_deadlines(client_id="client-900001", days_ahead=10)

    assert upcoming == [offset, naive, z_suffix]
