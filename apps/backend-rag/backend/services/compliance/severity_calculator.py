"""
Severity Calculator Service
Responsibility: Calculate alert severity
"""

import logging
from datetime import date, datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


def parse_deadline_utc(deadline: str | datetime | date) -> datetime:
    """Read a deadline as an aware UTC datetime.

    Accepts an ISO string, a datetime (the compliance track endpoint stores one;
    naive means UTC) or a date. A date-only deadline is midnight UTC (08:00 Bali),
    so a severity bucket can be one day off for up to 8 hours near a boundary.
    """
    if isinstance(deadline, datetime):
        parsed = deadline
    elif isinstance(deadline, date):
        parsed = datetime(deadline.year, deadline.month, deadline.day)
    elif isinstance(deadline, str):
        if deadline.endswith("Z"):
            deadline = f"{deadline[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(deadline)
        except (TypeError, ValueError) as error:
            raise ValueError("Deadline must be an ISO date string") from error
    else:
        raise ValueError("Deadline must be an ISO date string")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AlertSeverity(str, Enum):
    """Alert severity levels"""

    INFO = "info"  # >60 days
    WARNING = "warning"  # 30-60 days
    URGENT = "urgent"  # 7-30 days
    CRITICAL = "critical"  # <7 days or overdue


class SeverityCalculatorService:
    """
    Service for calculating alert severity.

    Responsibility: Calculate alert severity based on days until deadline.
    """

    # Alert thresholds (days before deadline)
    ALERT_THRESHOLDS = {
        AlertSeverity.INFO: 60,
        AlertSeverity.WARNING: 30,
        AlertSeverity.URGENT: 7,
        AlertSeverity.CRITICAL: 0,  # Overdue
    }

    def calculate_severity(self, deadline: str) -> tuple[AlertSeverity, int]:
        """
        Calculate alert severity based on days until deadline.

        Args:
            deadline: Deadline date (ISO)

        Returns:
            Tuple of (severity, days_until_deadline)
        """
        deadline_date = parse_deadline_utc(deadline)
        now = datetime.now(tz=timezone.utc)
        days_until = (deadline_date - now).days

        if days_until < 0:
            return AlertSeverity.CRITICAL, days_until
        if days_until <= self.ALERT_THRESHOLDS[AlertSeverity.URGENT]:
            return AlertSeverity.URGENT, days_until
        if days_until <= self.ALERT_THRESHOLDS[AlertSeverity.WARNING]:
            return AlertSeverity.WARNING, days_until
        return AlertSeverity.INFO, days_until

    def get_days_until_deadline(self, deadline: str) -> int:
        """
        Get days until deadline.

        Args:
            deadline: Deadline date (ISO)

        Returns:
            Days until deadline (negative if overdue)
        """
        deadline_date = parse_deadline_utc(deadline)
        now = datetime.now(tz=timezone.utc)
        return (deadline_date - now).days
