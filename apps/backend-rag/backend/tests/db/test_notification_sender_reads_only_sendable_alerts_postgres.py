"""Real-Postgres tests: the e-mail alert machinery touches only sendable alerts.

``notification_alerts`` also holds CRM bell rows (``wa_human_handoff``,
``portal_*``) where ``pending`` means UNREAD and ``email_body`` is NULL. The
sender's reader, the duplicate-superseder and the admin pause must leave them
alone: a bell row used to raise a ValidationError in ``get_pending_alerts``
(skipping every client's e-mails) and was suppressed by the other two.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import asyncpg
import pytest
import pytest_asyncio

from backend.app.modules.notifications import admin_router
from backend.app.modules.notifications.models import AlertType
from backend.app.modules.notifications.service import NotificationService
from backend.migrations.migration_071_notification_alerts import UPGRADE_SQL

pytestmark = pytest.mark.integration


class _Pool:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest_asyncio.fixture
async def conn(db_tx: asyncpg.Connection) -> asyncpg.Connection:
    await db_tx.execute(UPGRADE_SQL)
    return db_tx


@pytest.fixture
def service(conn: asyncpg.Connection) -> NotificationService:
    return NotificationService(db_pool=_Pool(conn), email_provider=Mock())


async def _client(conn: asyncpg.Connection, tag: str) -> int:
    row = await conn.fetchrow(
        "INSERT INTO clients (full_name, email) VALUES ($1, $2) RETURNING id",
        f"Test Client {tag}",
        f"sendable-{tag}@example.com",
    )
    return row["id"]


async def _alert(
    conn: asyncpg.Connection,
    client_id: int,
    alert_type: str,
    *,
    days_ago: int = 0,
    status: str = "pending",
    body: str | None = "<p>b</p>",
) -> int:
    # One row per (client, type, UTC day): duplicates use different days.
    created = datetime.now(timezone.utc) - timedelta(days=days_ago, minutes=1)
    row = await conn.fetchrow(
        """
        INSERT INTO notification_alerts
            (client_id, alert_type, status, message, email_subject, email_body, created_at)
        VALUES ($1, $2, $3, 'm', 's', $4, $5)
        RETURNING id
        """,
        client_id,
        alert_type,
        status,
        body,
        created,
    )
    return row["id"]


async def _status(conn: asyncpg.Connection, alert_id: int) -> str:
    return await conn.fetchval("SELECT status FROM notification_alerts WHERE id = $1", alert_id)


def test_sendable_alert_types_are_exactly_the_alert_type_values() -> None:
    from backend.app.modules.notifications.models import SENDABLE_ALERT_TYPES

    assert set(SENDABLE_ALERT_TYPES) == {t.value for t in AlertType}
    assert len(SENDABLE_ALERT_TYPES) == len(AlertType)
    assert not {"wa_human_handoff", "portal_profile_update", "portal_document_upload"} & set(
        SENDABLE_ALERT_TYPES
    )


@pytest.mark.asyncio
async def test_get_pending_alerts_returns_only_the_sendable_row(
    service: NotificationService, conn: asyncpg.Connection
) -> None:
    cid = await _client(conn, "read")
    await _alert(conn, cid, "wa_human_handoff", body=None)
    await _alert(conn, cid, "portal_profile_update", body=None)
    visa = await _alert(conn, cid, "visa_warning")

    alerts = await service.get_pending_alerts()

    assert [a.id for a in alerts] == [visa]
    assert alerts[0].alert_type == AlertType.VISA_WARNING


@pytest.mark.asyncio
async def test_supersede_leaves_bell_rows_pending_but_still_dedups_sendable(
    service: NotificationService, conn: asyncpg.Connection
) -> None:
    cid = await _client(conn, "sup")
    bell_old = await _alert(conn, cid, "wa_human_handoff", days_ago=1, body=None)
    bell_new = await _alert(conn, cid, "wa_human_handoff", days_ago=0, body=None)
    visa_old = await _alert(conn, cid, "visa_warning", days_ago=1)
    visa_new = await _alert(conn, cid, "visa_warning", days_ago=0)

    count = await service.supersede_duplicate_pending_alerts()

    assert count == 1
    assert await _status(conn, bell_old) == "pending"
    assert await _status(conn, bell_new) == "pending"
    assert await _status(conn, visa_old) == "suppressed"
    assert await _status(conn, visa_new) == "pending"


@pytest.mark.asyncio
async def test_admin_pause_suppresses_sendable_but_not_bell_rows(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await _client(conn, "pause")
    visa = await _alert(conn, cid, "visa_warning")
    bell = await _alert(conn, cid, "wa_human_handoff", body=None)
    admin = {"email": "admin@example.com", "role": "admin"}
    monkeypatch.setattr(admin_router, "is_crm_admin", lambda _u: True)

    await admin_router.pause_client_notifications(
        client_id=cid, hours=24, current_user=admin, pool=_Pool(conn)
    )

    assert await _status(conn, visa) == "suppressed"
    assert await _status(conn, bell) == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("by_ids", [True, False], ids=["alert_ids", "last_7_days"])
async def test_retry_failed_hands_only_sendable_alerts_to_the_sender(
    conn: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch, by_ids: bool
) -> None:
    cid = await _client(conn, "retry-ids" if by_ids else "retry-all")
    bell = await _alert(conn, cid, "wa_human_handoff", status="failed", body=None)
    visa = await _alert(conn, cid, "visa_warning", status="failed")
    handed: list[int] = []

    async def fake_batch(self, alerts, get_client_email_func):
        handed.extend(a.id for a in alerts)
        return []

    monkeypatch.setattr(NotificationService, "process_alerts_batch", fake_batch)
    monkeypatch.setattr(admin_router, "is_crm_admin", lambda _u: True)

    response = await admin_router.retry_failed_alerts(
        request=admin_router.RetryRequest(alert_ids=[bell, visa] if by_ids else None),
        current_user={"email": "admin@example.com", "role": "admin"},
        pool=_Pool(conn),
    )

    assert response.success is True
    assert handed == [visa]
    assert response.retried == 1
    assert await _status(conn, bell) == "failed"
