"""PATCH /api/crm/notifications/{id}/read touches only the caller's bell alerts, on real PostgreSQL.

``notification_alerts.status = 'pending'`` is also the delivery queue of the notification
service, so an unfiltered mark-read silenced alerts that were never delivered. Throwaway
schema, invented clients only.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.routers.crm_notifications import BELL_ALERT_TYPES, mark_notification_read

_SCHEMA_SQL = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY,
    full_name TEXT,
    assigned_to TEXT,
    deleted_at TIMESTAMPTZ
);
CREATE TABLE notification_alerts (
    id BIGSERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    alert_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ
);
"""

OWNER = {"email": "owner@example.test", "role": "Junior Consultant"}
OTHER = {"email": "other@example.test", "role": "Junior Consultant"}
ADMIN = {"email": "admin@example.test", "role": "admin"}
BELL = BELL_ALERT_TYPES[0]
QUEUE_TYPE = "email_reminder_contoh"  # a delivery-queue row, not a bell type


@pytest_asyncio.fixture
async def db():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for the real PostgreSQL mark-read proof")
    schema = "notif_read_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = None
    try:
        pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        async with pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
        yield pool
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _alert(pool, alert_type: str, assigned_to: str = OWNER["email"]) -> int:
    async with pool.acquire() as conn:
        cid = await conn.fetchval(
            "INSERT INTO clients (full_name, assigned_to) VALUES ('Contoh Klien', $1) RETURNING id",
            assigned_to,
        )
        return await conn.fetchval(
            "INSERT INTO notification_alerts (client_id, alert_type, message)"
            " VALUES ($1, $2, 'Contoh') RETURNING id",
            cid,
            alert_type,
        )


async def _status(pool, alert_id: int) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT status FROM notification_alerts WHERE id = $1", alert_id)


async def _patch(pool, alert_id: int, user: dict):
    return await mark_notification_read(notification_id=alert_id, pool=pool, current_user=user)


@pytest.mark.asyncio
async def test_a_delivery_queue_row_is_not_markable_and_stays_pending(db):
    alert_id = await _alert(db, QUEUE_TYPE)

    with pytest.raises(HTTPException) as exc:
        await _patch(db, alert_id, OWNER)

    assert exc.value.status_code == 404
    assert await _status(db, alert_id) == "pending"


@pytest.mark.asyncio
async def test_another_members_bell_alert_stays_pending_for_a_non_admin(db):
    alert_id = await _alert(db, BELL, assigned_to=OWNER["email"])

    with pytest.raises(HTTPException) as exc:
        await _patch(db, alert_id, OTHER)

    assert exc.value.status_code == 404
    assert await _status(db, alert_id) == "pending"


@pytest.mark.asyncio
async def test_a_nonexistent_id_is_404(db):
    with pytest.raises(HTTPException) as exc:
        await _patch(db, 987654, OWNER)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_owner_marks_their_own_clients_bell_alert(db):
    alert_id = await _alert(db, BELL)

    result = await _patch(db, alert_id, OWNER)

    assert result == {"id": alert_id, "status": "sent"}
    assert await _status(db, alert_id) == "sent"


@pytest.mark.asyncio
async def test_an_admin_marks_any_clients_bell_alert(db):
    alert_id = await _alert(db, BELL, assigned_to=OWNER["email"])

    result = await _patch(db, alert_id, ADMIN)

    assert result == {"id": alert_id, "status": "sent"}
    assert await _status(db, alert_id) == "sent"


@pytest.mark.asyncio
async def test_a_second_patch_on_a_visible_alert_answers_already_read(db):
    alert_id = await _alert(db, BELL)
    await _patch(db, alert_id, OWNER)

    result = await _patch(db, alert_id, OWNER)

    assert result == {"id": alert_id, "status": "already_read"}
