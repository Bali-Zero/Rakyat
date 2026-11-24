"""A first portal registration refuses an address another account holds (real PostgreSQL).

When no account row is linked to the invited client, registration inserts one. The branch
that updates an existing row checks whether another row holds the invited address; the
insert did not. ``team_members.email`` is unique by exact text while login lowers it: the
same text failed on the unique index as a server error on every retry, and an address that
differs only in letter case was stored as a second login for one mailbox. Invented rows only.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers import portal_invite
from backend.services.portal.invite_service import InviteService, _hash_token

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

_TABLES = """
CREATE TABLE clients (
    id integer PRIMARY KEY,
    full_name text,
    email text,
    deleted_at timestamptz
);
CREATE TABLE client_invitations (
    id serial PRIMARY KEY,
    client_id integer NOT NULL,
    email text,
    token text NOT NULL,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE team_members (
    id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name text NOT NULL,
    email text NOT NULL UNIQUE,
    full_name text,
    role text NOT NULL,
    pin_hash text,
    active boolean NOT NULL DEFAULT true,
    linked_client_id integer,
    portal_access boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE client_preferences (client_id integer PRIMARY KEY);
"""

CLIENT = 900001
INVITED = "person.one@example.test"
INVITE = "zz-invite-one"
CHOSEN_PIN = "1234"
REFUSAL = "Another account already uses this email address"


@pytest_asyncio.fixture()
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"portal_first_registration_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            db_pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
            )
            try:
                await db_pool.execute(_TABLES)
                await db_pool.execute(
                    "INSERT INTO clients (id, full_name, email) VALUES ($1, 'Person One', $2)",
                    CLIENT,
                    INVITED,
                )
                await db_pool.execute(
                    "INSERT INTO client_invitations (client_id, email, token, expires_at) "
                    "VALUES ($1, $2, $3, now() + interval '1 hour')",
                    CLIENT,
                    INVITED,
                    _hash_token(INVITE),
                )
                with patch(
                    "backend.services.common.cache._invalidate_cache",
                    new=AsyncMock(return_value=1),
                ):
                    yield db_pool
            finally:
                await db_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


async def _someone_else_holds(pool: asyncpg.Pool, address: str, role: str = "member") -> None:
    await pool.execute(
        "INSERT INTO team_members (name, email, full_name, role) VALUES ('Zz Other', $1, "
        "'Zz Other', $2)",
        address,
        role,
    )


async def _state(pool: asyncpg.Pool) -> dict[str, Any]:
    """What a registration writes: the accounts, the invitation, the preferences."""
    accounts = await pool.fetch(
        "SELECT email, role, linked_client_id, active, portal_access FROM team_members "
        "ORDER BY created_at, email"
    )
    return {
        "accounts": [tuple(row.values()) for row in accounts],
        "invitation_used": await pool.fetchval(
            "SELECT used_at IS NOT NULL FROM client_invitations WHERE client_id = $1", CLIENT
        ),
        "preferences": await pool.fetchval("SELECT count(*) FROM client_preferences"),
    }


async def _register(pool: asyncpg.Pool) -> dict[str, Any]:
    return await InviteService(pool).complete_registration(token=INVITE, pin=CHOSEN_PIN)


async def test_a_first_registration_creates_the_login_at_the_invited_address(
    pool: asyncpg.Pool,
) -> None:
    result = await _register(pool)

    assert (result["client_id"], result["email"]) == (900001, "person.one@example.test")
    assert await _state(pool) == {
        "accounts": [("person.one@example.test", "client", 900001, True, True)],
        "invitation_used": True,
        "preferences": 1,
    }


@pytest.mark.parametrize(
    "held",
    ["person.one@example.test", "Person.One@Example.Test"],
    ids=["the same text", "another letter case"],
)
async def test_an_address_another_account_holds_is_refused_and_nothing_is_written(
    pool: asyncpg.Pool, held: str
) -> None:
    await _someone_else_holds(pool, held)
    before = await _state(pool)

    with pytest.raises(ValueError, match=REFUSAL):
        await _register(pool)

    assert await _state(pool) == before
    assert before == {
        "accounts": [(held, "member", None, True, False)],
        "invitation_used": False,
        "preferences": 0,
    }


async def test_the_address_of_another_clients_account_is_refused_too(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "INSERT INTO team_members (name, email, full_name, role, linked_client_id, "
        "portal_access) VALUES ('Zz Relative', 'PERSON.ONE@example.test', 'Zz Relative', "
        "'client', 900002, true)"
    )

    with pytest.raises(ValueError, match=REFUSAL):
        await _register(pool)

    assert (await _state(pool))["accounts"] == [
        ("PERSON.ONE@example.test", "client", 900002, True, True)
    ]


async def test_the_route_answers_the_refusal_as_a_request_error(pool: asyncpg.Pool) -> None:
    await _someone_else_holds(pool, INVITED)

    with pytest.raises(HTTPException) as refused:
        await portal_invite.complete_registration(
            portal_invite.CompleteRegistrationRequest(token=INVITE, pin=CHOSEN_PIN),
            BackgroundTasks(),
            InviteService(pool),
        )

    assert (refused.value.status_code, refused.value.detail) == (400, REFUSAL)


async def test_the_same_invitation_registers_once_the_address_is_free(pool: asyncpg.Pool) -> None:
    await _someone_else_holds(pool, "Person.One@Example.Test")
    with pytest.raises(ValueError, match=REFUSAL):
        await _register(pool)

    await pool.execute(
        "UPDATE team_members SET email = 'zz.other@example.test' WHERE name = 'Zz Other'"
    )
    await _register(pool)

    assert (await _state(pool))["accounts"] == [
        ("zz.other@example.test", "member", None, True, False),
        ("person.one@example.test", "client", 900001, True, True),
    ]
