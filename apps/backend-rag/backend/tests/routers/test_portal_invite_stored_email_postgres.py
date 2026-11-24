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
    resend_invitation,
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


# ---- round 2: one comparison form, binding at minting, recipient throttle ----

IDNA_STORED = "zz-client@xn--bcher-kva.de"


@pytest.mark.parametrize(
    "typed",
    [
        IDNA_STORED,
        "zz-client@b\u00fccher.de",
        "zz-client@bu\u0308cher.de",
        "ZZ-Client@XN--BCHER-KVA.DE",
    ],
)
async def test_an_idna_stored_address_is_accepted_however_the_request_spells_it(
    pool, typed
) -> None:
    client_id = await seed_client(pool, IDNA_STORED)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        response = await send(pool, client_id, typed)
    assert response["success"] is True
    assert await invitation_rows(pool, client_id) == [IDNA_STORED]
    assert sender.await_args.kwargs["to"] == IDNA_STORED


async def test_an_address_changed_between_the_check_and_the_mint_mints_nothing(pool) -> None:
    client_id = await seed_client(pool, OTHER)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO client_invitations "
            "(client_id, email, token, expires_at, created_by, created_at) "
            "VALUES ($1, $2, 'live', NOW() + INTERVAL '1 day', 'seed', NOW() - INTERVAL '2 hours')",
            client_id,
            OTHER,
        )

    async def stale_check(client_id, requested, conn):
        # The route read the record before a colleague changed it to OTHER.
        return STORED

    with (
        patch("backend.app.routers.portal_invite._stored_invite_address", new=stale_check),
        patch(
            "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
        ) as sender,
        pytest.raises(HTTPException) as exc,
    ):
        await send(pool, client_id, STORED)
    assert exc.value.status_code == 422
    assert "client record" in exc.value.detail
    sender.assert_not_awaited()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT email, expires_at > NOW() AS live FROM client_invitations WHERE client_id = $1",
            client_id,
        )
    assert [(r["email"], r["live"]) for r in rows] == [(OTHER, True)]


async def test_the_service_mints_only_for_the_address_on_record(pool) -> None:
    client_id = await seed_client(pool, STORED)
    with pytest.raises(ValueError, match="not the e-mail on the client record"):
        await InviteService(pool).create_invitation(
            client_id=client_id, email=OTHER, created_by="seed"
        )
    assert await invitation_rows(pool, client_id) == []


async def _seed_invitations(pool, client_id: int, ages: list[str]) -> None:
    async with pool.acquire() as conn:
        for age in ages:
            await conn.execute(
                "INSERT INTO client_invitations "
                "(client_id, email, token, expires_at, created_by, created_at) "
                "VALUES ($1, $2, md5(random()::text), NOW() + INTERVAL '1 day', 'seed', "
                "NOW() - $3::text::interval)",
                client_id,
                STORED,
                age,
            )


async def test_a_second_send_inside_a_minute_is_refused_and_mails_nothing(pool) -> None:
    client_id = await seed_client(pool, STORED)
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        await send(pool, client_id, STORED)
        with pytest.raises(HTTPException) as exc:
            await send(pool, client_id, STORED)
    assert exc.value.status_code == 429
    assert 0 < int(exc.value.headers["Retry-After"]) <= 60
    assert "1 minute" in exc.value.detail
    assert sender.await_count == 1
    assert await invitation_rows(pool, client_id) == [STORED]


async def test_the_eleventh_send_in_a_day_is_refused(pool) -> None:
    client_id = await seed_client(pool, STORED)
    await _seed_invitations(pool, client_id, [f"{h} hours" for h in range(2, 12)])
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        with pytest.raises(HTTPException) as exc:
            await send(pool, client_id, STORED)
    assert exc.value.status_code == 429
    # The oldest of the ten is 11 hours old: the day reopens in about 13 hours.
    assert 12 * 3600 < int(exc.value.headers["Retry-After"]) <= 13 * 3600
    sender.assert_not_awaited()
    assert len(await invitation_rows(pool, client_id)) == 10


async def test_sends_older_than_the_bounds_do_not_count(pool) -> None:
    client_id = await seed_client(pool, STORED)
    await _seed_invitations(pool, client_id, ["2 minutes"] + [f"{h} hours" for h in range(25, 40)])
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        response = await send(pool, client_id, STORED)
    assert response["success"] is True
    sender.assert_awaited_once()


async def test_a_resend_inside_a_minute_is_refused(pool) -> None:
    client_id = await seed_client(pool, STORED)
    await _seed_invitations(pool, client_id, ["10 seconds"])
    with patch(
        "backend.app.routers.portal_invite.send_portal_invite_email", new=AsyncMock()
    ) as sender:
        with pytest.raises(HTTPException) as exc:
            await resend_invitation(
                client_id,
                current_user=STAFF,
                invite_service=InviteService(pool),
                db_pool=pool,
            )
    assert exc.value.status_code == 429
    sender.assert_not_awaited()
    assert len(await invitation_rows(pool, client_id)) == 1
