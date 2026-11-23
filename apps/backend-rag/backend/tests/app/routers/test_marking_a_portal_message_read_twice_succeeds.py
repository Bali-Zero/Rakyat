"""Marking a portal message read twice succeeds, and keeps the first read time.

The client portal polls and can be open in two tabs, so the same team message is marked
read more than once: a second tab, a double fire when the thread opens, a retry after a
timeout whose first request had committed. The query matched only unread rows, so the
second call changed nothing, the service answered "no such message" and the route turned
that into a 404 for a message that exists and is read.

Real rows, inside a transaction that is rolled back.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg
import pytest
from fastapi import HTTPException

from backend.app.routers import portal as portal_router
from backend.services.portal.portal_service import PortalService

FIRST_READ = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)


class _OneConnection:
    """A pool that hands out the test's own connection, so nothing is committed."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    def acquire(self) -> _OneConnection:
        return self

    async def __aenter__(self) -> asyncpg.Connection:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _client(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "INSERT INTO clients (full_name, email, created_at, updated_at) "
        "VALUES ('Zz Portal Reader', $1, NOW(), NOW()) RETURNING id",
        f"zz.reader.{uuid.uuid4().hex}@example.test",
    )


async def _message(
    conn: asyncpg.Connection,
    client_id: int,
    *,
    direction: str = "team_to_client",
    read_at: datetime | None = None,
) -> int:
    return await conn.fetchval(
        "INSERT INTO portal_messages (client_id, direction, content, sent_by, read_at) "
        "VALUES ($1, $2, 'Synthetic message', 'synthetic-sender', $3) RETURNING id",
        client_id,
        direction,
        read_at,
    )


async def _read_at(conn: asyncpg.Connection, message_id: int) -> datetime | None:
    return await conn.fetchval("SELECT read_at FROM portal_messages WHERE id = $1", message_id)


async def _mark(conn: asyncpg.Connection, client_id: int, message_id: int) -> dict[str, Any]:
    return await PortalService(_OneConnection(conn)).mark_message_read(
        client_id, message_id, current_user={"client_id": client_id}
    )


@pytest.mark.asyncio
async def test_an_unread_team_message_is_marked_read(db_tx: asyncpg.Connection) -> None:
    client_id = await _client(db_tx)
    message_id = await _message(db_tx, client_id)

    assert await _mark(db_tx, client_id, message_id) == {"success": True}
    assert await _read_at(db_tx, message_id) is not None


@pytest.mark.asyncio
async def test_a_message_that_is_already_read_is_marked_again_and_keeps_its_first_read_time(
    db_tx: asyncpg.Connection,
) -> None:
    client_id = await _client(db_tx)
    message_id = await _message(db_tx, client_id, read_at=FIRST_READ)

    assert await _mark(db_tx, client_id, message_id) == {"success": True}
    assert await _mark(db_tx, client_id, message_id) == {"success": True}
    assert await _read_at(db_tx, message_id) == FIRST_READ


@pytest.mark.asyncio
async def test_the_route_answers_the_second_call_like_the_first(db_tx: asyncpg.Connection) -> None:
    client_id = await _client(db_tx)
    message_id = await _message(db_tx, client_id)
    service = PortalService(_OneConnection(db_tx))
    client = {"client_id": client_id}

    first = await portal_router.mark_message_read(message_id, client=client, portal_service=service)
    second = await portal_router.mark_message_read(
        message_id, client=client, portal_service=service
    )

    assert first == second == {"success": True, "message": "Message marked as read"}


@pytest.mark.asyncio
async def test_a_client_still_cannot_mark_its_own_outbound_message(
    db_tx: asyncpg.Connection,
) -> None:
    client_id = await _client(db_tx)
    message_id = await _message(db_tx, client_id, direction="client_to_team")

    assert await _mark(db_tx, client_id, message_id) == {"success": False}
    assert await _read_at(db_tx, message_id) is None


@pytest.mark.asyncio
async def test_another_client_s_message_is_still_not_found(db_tx: asyncpg.Connection) -> None:
    reader = await _client(db_tx)
    owner = await _client(db_tx)
    unread = await _message(db_tx, owner)
    already_read = await _message(db_tx, owner, read_at=FIRST_READ)

    assert await _mark(db_tx, reader, unread) == {"success": False}
    assert await _mark(db_tx, reader, already_read) == {"success": False}
    assert await _read_at(db_tx, unread) is None
    assert await _read_at(db_tx, already_read) == FIRST_READ


@pytest.mark.asyncio
async def test_a_message_that_does_not_exist_is_a_404_on_the_route(
    db_tx: asyncpg.Connection,
) -> None:
    client_id = await _client(db_tx)
    message_id = await _message(db_tx, client_id)
    await db_tx.execute("DELETE FROM portal_messages WHERE id = $1", message_id)
    service = PortalService(_OneConnection(db_tx))

    with pytest.raises(HTTPException) as refused:
        await portal_router.mark_message_read(
            message_id, client={"client_id": client_id}, portal_service=service
        )

    assert (refused.value.status_code, refused.value.detail) == (404, "Message not found")
