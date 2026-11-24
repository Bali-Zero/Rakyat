from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.compliance import AlertSeverity, ComplianceType
from backend.services.compliance.severity_calculator import SeverityCalculatorService

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
    assert caplog.messages == [
        f"Skipping compliance item with invalid deadline: {invalid_item.item_id}"
    ]


def test_explicit_offset_and_overdue_deadlines_keep_existing_classification() -> None:
    calculator = SeverityCalculatorService()

    severity, _ = calculator.calculate_severity(_future_datetime(3).isoformat())
    overdue_severity, _ = calculator.calculate_severity(_future_datetime(-5).isoformat())

    assert severity == AlertSeverity.URGENT
    assert overdue_severity == AlertSeverity.CRITICAL
