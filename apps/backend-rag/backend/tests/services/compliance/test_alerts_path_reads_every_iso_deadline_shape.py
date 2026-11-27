from __future__ import annotations

import logging
import warnings
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.services.compliance import AlertSeverity, ComplianceType
from backend.services.compliance.severity_calculator import (
    SeverityCalculatorService,
    parse_deadline_utc,
)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from backend.services.misc.proactive_compliance_monitor import ProactiveComplianceMonitor


def _future_datetime(days: int) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(days=days)


@pytest.mark.parametrize(
    "deadline",
    [
        lambda: _future_datetime(5).date().isoformat(),
        lambda: _future_datetime(5).replace(tzinfo=None).isoformat(),
        lambda: _future_datetime(5).isoformat().replace("+00:00", "Z"),
    ],
)
def test_severity_calculator_reads_every_iso_deadline_shape(deadline) -> None:
    calculator = SeverityCalculatorService()

    severity, days_until = calculator.calculate_severity(deadline())

    assert severity == AlertSeverity.URGENT
    assert 0 <= days_until <= 7
    assert 0 <= calculator.get_days_until_deadline(deadline()) <= 7


def test_monitor_returns_alert_for_date_only_deadline() -> None:
    monitor = ProactiveComplianceMonitor()
    monitor.add_compliance_item(
        client_id="client-900001",
        compliance_type=ComplianceType.DOCUMENT_EXPIRY,
        title="Synthetic document renewal",
        deadline=_future_datetime(5).date().isoformat(),
    )

    assert len(monitor.check_compliance_items()) == 1


def test_monitor_skips_invalid_deadline_and_checks_remaining_item(caplog) -> None:
    monitor = ProactiveComplianceMonitor()
    invalid_item = monitor.add_compliance_item(
        client_id="client-900002",
        compliance_type=ComplianceType.DOCUMENT_EXPIRY,
        title="Synthetic invalid deadline",
        deadline="not-a-deadline",
    )
    monitor.add_compliance_item(
        client_id="client-900003",
        compliance_type=ComplianceType.DOCUMENT_EXPIRY,
        title="Synthetic valid deadline",
        deadline=_future_datetime(5).date().isoformat(),
    )

    assert len(monitor.check_compliance_items()) == 1
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned == [f"Skipping compliance item with invalid deadline: {invalid_item.item_id}"]


def test_explicit_offset_and_overdue_deadlines_keep_existing_classification() -> None:
    calculator = SeverityCalculatorService()

    severity, _ = calculator.calculate_severity(_future_datetime(3).isoformat())
    overdue_severity, _ = calculator.calculate_severity(_future_datetime(-5).isoformat())

    assert severity == AlertSeverity.URGENT
    assert overdue_severity == AlertSeverity.CRITICAL


def _router_shaped_deadline(shape: str) -> datetime:
    # The track endpoint stores datetime.fromisoformat(request.deadline).
    day = (_future_datetime(5)).date().isoformat()
    suffix = "" if shape == "naive" else "+08:00"
    return datetime.fromisoformat(f"{day}T00:00:00{suffix}")


@pytest.mark.parametrize("deadline_shape", ["naive", "aware"])
def test_monitor_returns_alert_for_datetime_deadline_as_router_stores_it(
    deadline_shape,
) -> None:
    monitor = ProactiveComplianceMonitor()
    deadline = _router_shaped_deadline(deadline_shape)
    monitor.add_compliance_item(
        client_id="client-900004",
        compliance_type=ComplianceType.DOCUMENT_EXPIRY,
        title="Synthetic datetime deadline",
        deadline=deadline,
    )

    assert len(monitor.check_compliance_items()) == 1


@pytest.mark.parametrize("deadline_shape", ["naive", "aware"])
def test_tracker_lists_datetime_deadline_as_router_stores_it(deadline_shape) -> None:
    from backend.services.compliance.compliance_tracker import ComplianceTrackerService

    tracker = ComplianceTrackerService()
    deadline = _router_shaped_deadline(deadline_shape)
    tracker.add_compliance_item(
        client_id="client-900005",
        compliance_type=ComplianceType.DOCUMENT_EXPIRY,
        title="Synthetic datetime deadline",
        deadline=deadline,
    )

    assert len(tracker.get_upcoming_deadlines(days_ahead=30)) == 1


def _parsed(value):
    try:
        return parse_deadline_utc(value)
    except ValueError:
        return None


def test_parse_deadline_utc_reads_datetime_and_date_instances() -> None:
    naive = datetime(2026, 12, 20, 0, 0, 0)
    aware = datetime.fromisoformat("2026-12-20T08:00:00+08:00")
    expected = datetime(2026, 12, 20, tzinfo=timezone.utc)

    assert _parsed(naive) == expected
    assert _parsed(aware) == expected
    assert _parsed(date(2026, 12, 20)) == expected
    assert _parsed(naive).utcoffset() == timedelta(0)
    assert _parsed(aware).utcoffset() == timedelta(0)


def test_parse_deadline_utc_rejects_other_types() -> None:
    with pytest.raises(ValueError):
        parse_deadline_utc(20261220)  # type: ignore[arg-type]
