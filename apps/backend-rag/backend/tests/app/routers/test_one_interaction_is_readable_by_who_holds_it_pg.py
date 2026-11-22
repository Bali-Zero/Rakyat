"""One interaction is readable by who holds it.

The route judges a caller who is not an admin by the owner of the interaction's client and by
its author. The owner lives on ``clients``; this runs the route's own query on a real table,
where a column the query does not select is a KeyError and not a faked value. Invented rows.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

import backend.app.utils.client_pools as client_pools
from backend.app.routers.crm_interactions import get_interaction

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_OWNER = {"email": "owner.one@example.test", "role": "consultant"}
_AUTHOR = {"email": "author.two@example.test", "role": "consultant"}
_OUTSIDER = {"email": "outsider.three@example.test", "role": "consultant"}
_ADMIN = {"email": "admin.four@example.test", "role": "admin"}


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = pytest.mark.skipif(
    not _is_safe_test_database(_DB_URL), reason="needs a local test database"
)

# The columns the real tables have, as far as this route reads them.
_TABLES = """
CREATE TABLE clients (
    id integer PRIMARY KEY, full_name text, email text, assigned_to text
);
CREATE TABLE interactions (
    id integer PRIMARY KEY, client_id integer, practice_id integer, conversation_id integer,
    interaction_type text, channel text, subject text, summary text, full_content text,
    sentiment text, team_member text, direction text, interaction_date timestamptz,
    created_at timestamptz DEFAULT now()
);
INSERT INTO clients VALUES (900001, 'Zz Client One', 'client.one@example.test',
                            'Owner.One@Example.Test');
INSERT INTO interactions (id, client_id, interaction_type, summary, team_member)
VALUES (1, 900001, 'note', 'synthetic', 'Author.Two@Example.Test');
INSERT INTO interactions (id, client_id, interaction_type, summary, team_member)
VALUES (2, NULL, 'note', 'no client', NULL);
"""


@pytest_asyncio.fixture
async def pool(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[asyncpg.Pool, None]:
    monkeypatch.setattr(client_pools, "_VALIDATED", (), raising=False)
    admin = await asyncpg.connect(_DB_URL)
    schema = "one_interaction_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(_TABLES)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _status(pool: asyncpg.Pool, user: dict[str, str], interaction_id: int = 1) -> Any:
    """The record, or the status of the refusal."""
    try:
        return await get_interaction(interaction_id=interaction_id, current_user=user, db_pool=pool)
    except HTTPException as refused:
        return refused.status_code


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [_OWNER, _AUTHOR], ids=["the owner of the client", "the author"])
async def test_who_holds_the_interaction_reads_it(pool: asyncpg.Pool, user: dict[str, str]) -> None:
    answer = await _status(pool, user)

    assert isinstance(answer, dict), f"answered {answer} instead of the record"
    assert answer["id"] == 1 and answer["client_name"] == "Zz Client One"


@pytest.mark.asyncio
async def test_a_colleague_who_holds_neither_is_refused(pool: asyncpg.Pool) -> None:
    assert await _status(pool, _OUTSIDER) == 403


@pytest.mark.asyncio
async def test_an_interaction_with_no_client_and_no_author_is_refused_to_a_non_admin(
    pool: asyncpg.Pool,
) -> None:
    assert await _status(pool, _OWNER, interaction_id=2) == 403


@pytest.mark.asyncio
async def test_an_admin_reads_any_interaction(pool: asyncpg.Pool) -> None:
    first, second = await _status(pool, _ADMIN), await _status(pool, _ADMIN, interaction_id=2)

    assert isinstance(first, dict) and isinstance(second, dict)
    assert first["summary"] == "synthetic" and second["summary"] == "no client"


@pytest.mark.asyncio
async def test_an_unknown_interaction_is_not_found(pool: asyncpg.Pool) -> None:
    assert await _status(pool, _OWNER, interaction_id=999) == 404
