"""Portal invitations are bound to the e-mail on the client record, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database with the minimal tables the invite
routes read and write, seeds invented clients (addresses under ``example.com``) and calls the
real route functions with the real ``InviteService``. Only the mail sender is mocked.
"""

from __future__ import annotations

import os
import re
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.routers.portal_invite import (
    SendInviteRequest,
    send_invitation,
)
from backend.services.portal import InviteService

STAFF = {"email": "zz-admin@example.com", "user_id": 1, "role": "admin"}
STORED = "zz-client@example.com"
OTHER = "zz-elsewhere@example.com"

SCHEMA_SQL = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT,
    assigned_to TEXT,
    created_by TEXT,
    deleted_at TIMESTAMPTZ
);
CREATE TABLE team_members (email TEXT, department TEXT, active BOOLEAN);
CREATE TABLE client_invitations (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL,
    email TEXT NOT NULL,
    token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


@pytest_asyncio.fixture
async def pool():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for the real PostgreSQL invite proof")
    admin = await asyncpg.connect(dsn)
    name = await admin.fetchval("SELECT current_database()")
    if not re.search(r"_test(?:_run_[0-9]+_[0-9]+)?(?:_gw[0-9]+)?$", name) and name != "test":
        await admin.close()
        pytest.fail("A test database or isolated test worker is required")
    schema = "portal_invite_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def seed_client(pool, email: str | None) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO clients (full_name, email) VALUES ('Zz Client', $1) RETURNING id", email
        )


async def invitation_rows(pool, client_id: int) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT email FROM client_invitations WHERE client_id = $1 ORDER BY id", client_id
        )
    return [r["email"] for r in rows]


async def send(pool, client_id: int, email: str):
    return await send_invitation(
        SendInviteRequest(client_id=client_id, email=email),
        current_user=STAFF,
        invite_service=InviteService(pool),
        db_pool=pool,
    )


async def test_invitation_to_an_address_not_on_the_record_is_refused(pool) -> None:
    client_id = await seed_client(pool, STORED)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        with pytest.raises(HTTPException) as exc:
            await send(pool, client_id, OTHER)
    assert exc.value.status_code == 422
    assert "client record" in exc.value.detail
    assert await invitation_rows(pool, client_id) == []
    sender.assert_not_awaited()


async def test_invitation_for_a_client_without_stored_email_is_refused(pool) -> None:
    client_id = await seed_client(pool, None)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        with pytest.raises(HTTPException) as exc:
            await send(pool, client_id, OTHER)
    assert exc.value.status_code == 422
    assert "no e-mail" in exc.value.detail
    assert await invitation_rows(pool, client_id) == []
    sender.assert_not_awaited()


@pytest.mark.parametrize("typed", [STORED, STORED.upper(), f"  {STORED.title()} "])
async def test_invitation_to_the_stored_address_is_accepted_in_any_case(pool, typed) -> None:
    client_id = await seed_client(pool, STORED)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        response = await send(pool, client_id, typed.strip())
    assert response["success"] is True
    assert response["email_sent"] is True
    assert await invitation_rows(pool, client_id) == [STORED]
    assert sender.await_args.kwargs["to"] == STORED
