"""The document reader finds a portal caller's client by its login, never by its address.

The guard's real SQL runs against synthetic TEMP tables on a local test database (the same
shapes as test_documents_proxy_ownership_sql.py). Invented rows only.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers import documents_proxy as mod

LOGIN_A = "tm-a-0001"
LOGIN_B = "tm-b-0002"
CLIENT_A = 1
CLIENT_B = 2
OLD = "person.old@example.test"
NEW = "person.new@example.test"
FILE_A = "fileA0001A"
FILE_B = "fileB0002B"
INVOICE_A = "invA0003A"
SESSION_A = {"user_id": LOGIN_A, "email": OLD, "role": "client"}


@pytest_asyncio.fixture
async def conn():
    target = urlsplit(os.environ.get("TEST_DATABASE_URL", ""))
    if target.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in target.path:
        pytest.skip("requires a local test database")
    db = await asyncpg.connect(os.environ["TEST_DATABASE_URL"], timeout=3)
    try:
        await db.execute("SET search_path TO pg_temp")
        await db.execute(
            f"""
            CREATE TEMP TABLE clients (id int, status text, deleted_at timestamptz);
            CREATE TEMP TABLE documents (client_id int, file_id text, google_drive_file_url text,
                file_url text, client_visible boolean DEFAULT true, deleted_at timestamptz,
                is_archived boolean DEFAULT false, status text);
            CREATE TEMP TABLE invoices (client_id int, drive_file_id text);
            CREATE TEMP TABLE team_members (id text, email text, role text, active boolean,
                portal_access boolean, linked_client_id int);
            INSERT INTO clients VALUES ({CLIENT_A}, 'active', NULL), ({CLIENT_B}, 'active', NULL);
            INSERT INTO documents(client_id, file_id) VALUES ({CLIENT_A}, '{FILE_A}'),
                ({CLIENT_B}, '{FILE_B}');
            INSERT INTO invoices VALUES ({CLIENT_A}, '{INVOICE_A}');
            INSERT INTO team_members VALUES ('{LOGIN_A}', '{OLD}', 'client', true, true, {CLIENT_A});
            """
        )
        yield db
    finally:
        await db.close()


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class NeverAsked:
    def acquire(self):
        raise AssertionError("the database was asked")


async def _refused(conn, file_id, user) -> None:
    with pytest.raises(mod.HTTPException) as exc:
        await mod._assert_portal_caller_owns_file(file_id, user, Pool(conn))
    assert exc.value.status_code == 403
    assert exc.value.detail == "File is not in a known client folder"


async def _allowed(conn, file_id, user) -> None:
    assert await mod._assert_portal_caller_owns_file(file_id, user, Pool(conn)) is None


async def _address_moves_and_is_reused(conn) -> None:
    """A redeems an invitation at a new address; B is then provisioned at the old one."""
    await conn.execute("UPDATE team_members SET email = $1 WHERE id = $2", NEW, LOGIN_A)
    await conn.execute(
        "INSERT INTO team_members VALUES ($1, $2, 'client', true, true, $3)",
        LOGIN_B,
        OLD,
        CLIENT_B,
    )


@pytest.mark.asyncio
async def test_a_stale_session_of_an_archived_client_opens_no_file(conn):
    await _address_moves_and_is_reused(conn)
    await conn.execute("UPDATE clients SET deleted_at = now() WHERE id = $1", CLIENT_A)
    await _refused(conn, FILE_B, SESSION_A)
    await _refused(conn, FILE_A, SESSION_A)


@pytest.mark.asyncio
async def test_a_login_whose_address_moved_still_reads_only_its_own_client(conn):
    await _address_moves_and_is_reused(conn)
    await _refused(conn, FILE_B, SESSION_A)
    await _allowed(conn, FILE_A, SESSION_A)
    await _allowed(conn, INVOICE_A, SESSION_A)


@pytest.mark.asyncio
async def test_a_session_that_carries_only_an_address_is_refused(conn):
    await _address_moves_and_is_reused(conn)
    await _refused(conn, FILE_B, {"user_id": OLD, "email": OLD, "role": "client"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        f"UPDATE team_members SET role = 'team' WHERE id = '{LOGIN_A}'",
        f"UPDATE team_members SET active = false WHERE id = '{LOGIN_A}'",
        f"UPDATE team_members SET portal_access = false WHERE id = '{LOGIN_A}'",
        f"DELETE FROM team_members WHERE id = '{LOGIN_A}'",
        f"UPDATE team_members SET linked_client_id = NULL WHERE id = '{LOGIN_A}'",
        f"UPDATE clients SET deleted_at = now() WHERE id = {CLIENT_A}",
    ],
    ids=["role", "inactive", "portal-off", "no-login", "unlinked", "client-deleted"],
)
async def test_an_ineligible_login_is_refused_even_with_a_matching_claim(conn, change):
    await _allowed(conn, FILE_A, SESSION_A)
    await conn.execute(change)
    await _refused(conn, FILE_A, SESSION_A)
    await _refused(conn, FILE_A, {**SESSION_A, "client_id": CLIENT_A})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    ["absent", None, CLIENT_A, str(CLIENT_A)],
    ids=["absent", "none", "int", "digits"],
)
async def test_a_claim_that_matches_the_login_narrows_nothing(conn, claim):
    user = dict(SESSION_A) if claim == "absent" else {**SESSION_A, "client_id": claim}
    await _allowed(conn, FILE_A, user)


@pytest.mark.asyncio
async def test_a_claim_for_another_client_is_refused(conn):
    await _address_moves_and_is_reused(conn)
    user = {**SESSION_A, "client_id": CLIENT_B}
    await _refused(conn, FILE_B, user)
    await _refused(conn, FILE_A, user)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim",
    [True, float(CLIENT_A), "0", f"0{CLIENT_A}", "x", "-1", 2147483648, [CLIENT_A]],
    ids=["bool", "float", "zero", "leading-zero", "text", "negative", "too-large", "list"],
)
async def test_a_malformed_claim_is_refused(conn, claim):
    await _refused(conn, FILE_A, {**SESSION_A, "client_id": claim})


@pytest.mark.asyncio
async def test_an_empty_login_id_is_refused_before_the_database_is_asked():
    user = {"user_id": "", "email": OLD, "role": "client"}
    with pytest.raises(mod.HTTPException) as exc:
        await mod._assert_portal_caller_owns_file(FILE_B, user, NeverAsked())
    assert exc.value.status_code == 403
