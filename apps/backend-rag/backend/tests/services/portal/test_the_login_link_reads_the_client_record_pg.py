"""The e-mail login link reads the client record, as the PIN login does (real PostgreSQL).

Archiving a client writes ``clients.deleted_at`` and leaves the portal account row as it was.
The PIN login joins the client record; the link's two steps read the account row alone. Here
the service runs against real tables. Invented rows only.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.portal.magic_link_service import MagicLinkService

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
    deleted_at timestamptz
);
CREATE TABLE team_members (
    id serial PRIMARY KEY,
    email text NOT NULL UNIQUE,
    full_name text,
    name text,
    role text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    portal_access boolean NOT NULL DEFAULT true,
    linked_client_id integer
);
CREATE TABLE magic_link_tokens (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email varchar(255) NOT NULL,
    token_hash char(64) NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    created_ip varchar(64),
    created_at timestamptz NOT NULL DEFAULT now()
);
"""

ONE, TWO = 900001, 900002
ONE_MAIL, TWO_MAIL = "person.one@example.test", "person.two@example.test"
NO_LINK = {"token": None, "is_client": False}


@pytest_asyncio.fixture()
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"portal_login_link_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            db_pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
            )
            try:
                await db_pool.execute(_TABLES)
                for client, mail, name in ((ONE, ONE_MAIL, "Person One"), (TWO, TWO_MAIL, "Person Two")):  # fmt: skip
                    await db_pool.execute(
                        "INSERT INTO clients (id, full_name) VALUES ($1, $2)", client, name
                    )
                    await db_pool.execute(
                        "INSERT INTO team_members (email, full_name, role, linked_client_id) "
                        "VALUES ($1, $2, 'client', $3)",
                        mail,
                        name,
                        client,
                    )
                yield db_pool
            finally:
                await db_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


async def _archive(pool: asyncpg.Pool, client: int) -> None:
    await pool.execute("UPDATE clients SET deleted_at = now() WHERE id = $1", client)


async def _links(pool: asyncpg.Pool) -> list[str]:
    rows = await pool.fetch("SELECT email FROM magic_link_tokens ORDER BY id")
    return [row["email"] for row in rows]


async def test_a_client_whose_record_is_live_gets_a_link_that_opens_a_session(
    pool: asyncpg.Pool,
) -> None:
    service = MagicLinkService(pool)
    account = await pool.fetchval("SELECT id FROM team_members WHERE email = $1", ONE_MAIL)

    asked = await service.request_magic_link("Person.One@Example.Test")

    assert asked["is_client"] is True
    assert await _links(pool) == [ONE_MAIL]
    assert await service.verify_magic_link(asked["token"]) == {
        "id": str(account),
        "email": ONE_MAIL,
        "name": "Person One",
        "role": "client",
        "client_id": 900001,
        "portal_access": True,
    }


async def test_an_archived_client_is_answered_as_an_unknown_address_and_gets_no_link(
    pool: asyncpg.Pool,
) -> None:
    await _archive(pool, ONE)
    service = MagicLinkService(pool)

    archived = await service.request_magic_link(ONE_MAIL)
    unknown = await service.request_magic_link("nobody@example.test")
    other = await service.request_magic_link(TWO_MAIL)

    assert archived == {"email": ONE_MAIL, **NO_LINK}
    assert unknown == {"email": "nobody@example.test", **NO_LINK}
    assert other["is_client"] is True  # another client's archive changes nothing for this one
    assert await _links(pool) == [TWO_MAIL]


async def test_a_link_asked_for_before_the_client_was_archived_opens_no_session(
    pool: asyncpg.Pool,
) -> None:
    service = MagicLinkService(pool)
    asked = await service.request_magic_link(ONE_MAIL)
    await _archive(pool, ONE)

    assert await service.verify_magic_link(asked["token"]) is None
    assert await service.verify_magic_link(asked["token"]) is None


async def test_a_restored_client_gets_a_link_again(pool: asyncpg.Pool) -> None:
    await _archive(pool, ONE)
    service = MagicLinkService(pool)
    assert (await service.request_magic_link(ONE_MAIL))["token"] is None

    await pool.execute("UPDATE clients SET deleted_at = NULL WHERE id = $1", ONE)
    asked = await service.request_magic_link(ONE_MAIL)

    assert await service.verify_magic_link(asked["token"]) is not None
    assert await _links(pool) == [ONE_MAIL]


async def test_an_account_linked_to_no_client_record_gets_no_link(pool: asyncpg.Pool) -> None:
    """The portal itself refuses such an account on every page."""
    await pool.execute(
        "INSERT INTO team_members (email, full_name, role, linked_client_id) "
        "VALUES ('person.three@example.test', 'Person Three', 'client', 900003)"
    )

    asked = await MagicLinkService(pool).request_magic_link("person.three@example.test")

    assert asked == {"email": "person.three@example.test", **NO_LINK}
    assert await _links(pool) == []
