"""Regression tests for LKPM reminders sent only to active assignees."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.app.routers.lkpm as lkpm_router
import backend.services.compliance.lkpm_deadline_notifier as notifier_module
from backend.services.compliance.lkpm_deadline_notifier import LKPMDeadlineNotifier


def _row(
    report_id: int,
    assignee: str | None,
    company_name: str,
) -> dict[str, object]:
    return {
        "id": report_id,
        "client_id": report_id,
        "quarter": "Q1",
        "year": 2026,
        "status": "draft",
        "oss_submitted": False,
        "lkpm_assigned_to": assignee,
        "company_name": company_name,
        "oss_username": f"portal_user_{report_id}",
    }


def _make_pool(rows: list[dict[str, object]]) -> AsyncMock:
    pool = AsyncMock()
    connection = AsyncMock()

    async def fetchval(sql: str, *args: object) -> str | None:
        if notifier_module.KILLSWITCH_KEY in sql:
            return "true"
        return None

    async def fetch(sql: str, *args: object) -> list[dict[str, object]]:
        return rows

    connection.fetchval = fetchval
    connection.fetch = fetch
    acquired = AsyncMock()
    acquired.__aenter__ = AsyncMock(return_value=connection)
    acquired.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquired)
    return pool


@pytest.fixture()
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return datetime(2026, 4, 7, 9, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(notifier_module, "datetime", FrozenDatetime)


async def _no_telegram(*args: object, **kwargs: object) -> None:
    return None


async def test_inactive_assignee_joins_unassigned_alert(
    frozen_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    inactive_email = "former.consultant@example.test"
    monkeypatch.setattr(
        lkpm_router,
        "LKPM_ASSIGNEES",
        {"person.one@example.test", "person.two@example.test"},
    )
    notifier = LKPMDeadlineNotifier(_make_pool([_row(900001, inactive_email, "PT Contoh Satu")]))
    reminders: list[tuple[str, list[dict[str, object]]]] = []
    unassigned_alerts: list[list[dict[str, object]]] = []

    async def send_reminder(assignee: str, rows: list[dict[str, object]]) -> None:
        reminders.append((assignee, rows))

    async def send_unassigned(rows: list[dict[str, object]]) -> None:
        unassigned_alerts.append(rows)

    monkeypatch.setattr(notifier, "_send_assignee_reminder", send_reminder)
    monkeypatch.setattr(notifier, "_send_unassigned_alert", send_unassigned)
    monkeypatch.setattr(notifier, "_send_telegram_alert", _no_telegram)

    await notifier.check_and_notify()

    assert reminders == []
    assert [row["id"] for row in unassigned_alerts[0]] == [900001]


async def test_active_assignee_is_matched_case_insensitively_and_trimmed(
    frozen_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        lkpm_router,
        "LKPM_ASSIGNEES",
        {"person.one@example.test", "person.two@example.test"},
    )
    assignee = " Person.One@Example.Test "
    notifier = LKPMDeadlineNotifier(_make_pool([_row(900002, assignee, "PT Contoh Dua")]))
    reminders: list[tuple[str, list[dict[str, object]]]] = []

    async def send_reminder(email: str, rows: list[dict[str, object]]) -> None:
        reminders.append((email, rows))

    monkeypatch.setattr(notifier, "_send_assignee_reminder", send_reminder)
    monkeypatch.setattr(notifier, "_send_unassigned_alert", AsyncMock())
    monkeypatch.setattr(notifier, "_send_telegram_alert", _no_telegram)

    await notifier.check_and_notify()

    assert [email for email, _ in reminders] == ["person.one@example.test"]


async def test_active_assignee_and_empty_assignee_keep_current_delivery(
    frozen_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_email = "person.two@example.test"
    monkeypatch.setattr(
        lkpm_router,
        "LKPM_ASSIGNEES",
        {"person.one@example.test", active_email},
    )
    notifier = LKPMDeadlineNotifier(
        _make_pool(
            [
                _row(900003, active_email, "PT Contoh Tiga"),
                _row(900004, None, "PT Contoh Empat"),
            ]
        )
    )
    reminders: list[tuple[str, list[dict[str, object]]]] = []
    unassigned_alerts: list[list[dict[str, object]]] = []

    async def send_reminder(email: str, rows: list[dict[str, object]]) -> None:
        reminders.append((email, rows))

    async def send_unassigned(rows: list[dict[str, object]]) -> None:
        unassigned_alerts.append(rows)

    monkeypatch.setattr(notifier, "_send_assignee_reminder", send_reminder)
    monkeypatch.setattr(notifier, "_send_unassigned_alert", send_unassigned)
    monkeypatch.setattr(notifier, "_send_telegram_alert", _no_telegram)

    await notifier.check_and_notify()

    assert [email for email, _ in reminders] == [active_email]
    assert [row["id"] for row in unassigned_alerts[0]] == [900004]
