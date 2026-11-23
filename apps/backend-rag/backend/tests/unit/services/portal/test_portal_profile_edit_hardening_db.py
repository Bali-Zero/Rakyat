"""Portal self-service profile and settings saves, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database holding only the columns the statements under
test read or write, seeds invented clients (fake numbers only) and drops the schema in ``finally``.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.portal.portal_service import PortalService

_SCHEMA_SQL = """
CREATE TABLE clients (
    id SERIAL PRIMARY KEY,
    full_name TEXT,
    email TEXT,
    phone TEXT,
    phone_normalized TEXT,
    whatsapp TEXT,
    nationality TEXT,
    passport_number TEXT,
    passport_expiry DATE,
    date_of_birth DATE,
    gender TEXT,
    address TEXT,
    assigned_to TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE TABLE team_members (
    email TEXT PRIMARY KEY,
    full_name TEXT,
    avatar TEXT,
    role TEXT,
    active BOOLEAN DEFAULT TRUE
);
CREATE TABLE notification_alerts (
    id SERIAL PRIMARY KEY,
    client_id INT,
    alert_type TEXT,
    status TEXT,
    message TEXT,
    email_subject TEXT,
    created_date DATE DEFAULT CURRENT_DATE,
    UNIQUE (client_id, alert_type, created_date)
);
CREATE TABLE portal_messages (
    id SERIAL PRIMARY KEY,
    client_id INT,
    practice_id INT,
    subject TEXT,
    direction TEXT,
    content TEXT,
    sent_by TEXT,
    is_system_generated BOOLEAN
);
CREATE TABLE client_preferences (
    client_id INT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT 'en',
    timezone TEXT NOT NULL DEFAULT 'Asia/Jakarta'
);
"""


@pytest_asyncio.fixture
async def db():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for the real PostgreSQL profile proof")
    schema = "portal_profile_" + uuid4().hex
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


async def _client(pool, name: str, **cols) -> int:
    cols = {"full_name": name, "email": f"{name.lower().replace(' ', '.')}@example.test", **cols}
    names = ", ".join(cols)
    marks = ", ".join(f"${i}" for i in range(1, len(cols) + 1))
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"INSERT INTO clients ({names}) VALUES ({marks}) RETURNING id", *cols.values()
        )


def _user(client_id: int) -> dict:
    return {"client_id": client_id, "email": "client@example.test"}


@pytest.mark.asyncio
async def test_profile_patch_with_language_saves_the_other_fields(db):
    """A PATCH carrying `language` next to other fields used to roll everything back with an
    undefined-column error; the other fields must be saved and the call must succeed."""
    cid = await _client(db, "Zz Alpha", phone="+62 800 0000 111", address="Old street")
    service = PortalService(db)

    result = await service.update_profile(
        cid, {"language": "id", "address": "New street 1"}, current_user=_user(cid)
    )

    assert result["address"] == "New street 1"
    async with db.acquire() as conn:
        assert (
            await conn.fetchval("SELECT address FROM clients WHERE id = $1", cid) == "New street 1"
        )


@pytest.mark.asyncio
async def test_profile_patch_language_alone_is_a_no_op_success(db):
    cid = await _client(db, "Zz Alpha", address="Old street")
    service = PortalService(db)

    result = await service.update_profile(cid, {"language": "id"}, current_user=_user(cid))

    assert result["address"] == "Old street"


# --- contact numbers: a client files a change request, never writes the column -----------------

_NUM_A = "+62 800 0000 111"
_NUM_B = "+62 800 0000 222"
_NUM_C = "+62 800 0000 333"


async def _two_clients(pool):
    a = await _client(
        pool, "Zz Alpha", phone=_NUM_A, phone_normalized="628000000111", whatsapp=_NUM_A
    )
    b = await _client(
        pool, "Zz Bravo", phone=_NUM_B, phone_normalized="628000000222", whatsapp=_NUM_B
    )
    return a, b


async def _row(pool, cid):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT phone, whatsapp, address FROM clients WHERE id = $1", cid
        )


async def _requests(pool, cid):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT direction, content FROM portal_messages"
            " WHERE client_id = $1 AND content LIKE 'Requested contact change:%' ORDER BY id",
            cid,
        )
    return [(r["direction"], r["content"]) for r in rows]


async def _alerts(pool, cid):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT message FROM notification_alerts"
            " WHERE client_id = $1 AND alert_type = 'portal_profile_update'",
            cid,
        )


class _LoggedConn:
    """Wraps a pooled connection and records every statement text it is given."""

    def __init__(self, conn, log):
        self._conn = conn
        self._log = log

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if name in {"execute", "fetch", "fetchrow", "fetchval"}:

            async def _call(query, *args, **kwargs):
                self._log.append(str(query))
                return await attr(query, *args, **kwargs)

            return _call
        return attr


class _LoggedPool:
    def __init__(self, pool):
        self._pool = pool
        self.log: list[str] = []

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                self_inner._cm = outer._pool.acquire()
                conn = await self_inner._cm.__aenter__()
                return _LoggedConn(conn, outer.log)

            async def __aexit__(self_inner, *exc):
                return await self_inner._cm.__aexit__(*exc)

        return _Ctx()


async def _route(service, cid, **body):
    from backend.app.routers.portal import UpdateProfileRequest, update_profile

    return await update_profile(
        UpdateProfileRequest(**body), client=_user(cid), portal_service=service
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_an_owned_number_and_a_free_number_get_identical_answers(db, field):
    """No ownership-dependent result: another client's number and a free one produce the same
    response body and the same read-back (old number kept), each filing ONE request row."""
    a, b = await _two_clients(db)
    pool = _LoggedPool(db)
    service = PortalService(pool)

    owned = await _route(service, a, **{field: "08000000222"})
    after_owned = await _row(db, a)
    free = await _route(service, a, **{field: "+62 800 0000 444"})
    after_free = await _row(db, a)

    assert owned == free
    assert owned["contact_change_requested"] == [field]
    assert owned["data"]["phone"] == _NUM_A and owned["data"]["whatsapp"] == _NUM_A
    assert dict(after_owned) == dict(after_free)
    assert after_free["phone"] == _NUM_A and after_free["whatsapp"] == _NUM_A
    assert (await _row(db, b))["phone"] == _NUM_B
    requests = await _requests(db, a)
    assert [d for d, _ in requests] == ["client_to_team", "client_to_team"]
    assert "Zz" not in " ".join(c for _, c in requests)
    assert await _requests(db, b) == []


@pytest.mark.asyncio
async def test_the_ownership_query_is_never_executed_on_the_client_path(db):
    from backend.db.repositories.client_repository import DUP_OWNER_SQL

    a, _b = await _two_clients(db)
    pool = _LoggedPool(db)
    service = PortalService(pool)

    await service.update_profile(
        a, {"phone": "08000000222", "whatsapp": _NUM_C, "address": "X"}, current_user=_user(a)
    )

    assert pool.log and DUP_OWNER_SQL not in pool.log


@pytest.mark.asyncio
async def test_an_address_edit_saves_when_the_stored_contacts_are_unusable(db):
    cid = await _client(db, "Zz Alpha", phone="+62999", whatsapp="+62999", address="Old")
    service = PortalService(db)

    result = await _route(service, cid, phone="+62999", whatsapp="+62999", address="New street 1")

    assert result["contact_change_requested"] == []
    row = await _row(db, cid)
    assert row["address"] == "New street 1" and row["phone"] == "+62999"
    assert await _requests(db, cid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_an_empty_or_unusable_new_number_is_refused_and_nothing_is_saved(db, field):
    from backend.services.portal._mixins.billing import ProfileContactRefused

    a, _b = await _two_clients(db)
    service = PortalService(db)

    for unusable in ("", "   ", "12-3"):
        with pytest.raises(ProfileContactRefused):
            await service.update_profile(
                a, {field: unusable, "address": "Never saved"}, current_user=_user(a)
            )

    row = await _row(db, a)
    assert row["phone"] == _NUM_A and row["whatsapp"] == _NUM_A and row["address"] is None
    assert await _requests(db, a) == [] and await _alerts(db, a) == []


@pytest.mark.asyncio
async def test_the_same_core_as_the_current_number_files_no_request(db):
    a, _b = await _two_clients(db)
    service = PortalService(db)

    result = await service.update_profile(
        a, {"phone": "08000000111", "whatsapp": "+62-800-0000-111"}, current_user=_user(a)
    )

    assert result["contact_change_requested"] == []
    assert await _requests(db, a) == [] and await _alerts(db, a) == []
    assert (await _row(db, a))["phone"] == _NUM_A


@pytest.mark.asyncio
async def test_other_fields_are_saved_with_the_request_and_the_alert_is_written(db):
    a, _b = await _two_clients(db)
    service = PortalService(db)

    result = await service.update_profile(
        a,
        {"phone": _NUM_C, "whatsapp": "+62 800 0000 444", "address": "Street 9"},
        current_user=_user(a),
    )

    assert result["contact_change_requested"] == ["phone", "whatsapp"]
    row = await _row(db, a)
    assert row["address"] == "Street 9" and row["phone"] == _NUM_A and row["whatsapp"] == _NUM_A
    requests = await _requests(db, a)
    assert len(requests) == 1
    assert requests[0][1] == (
        "Requested contact change: phone \u2192 +62 800 0000 333; WhatsApp \u2192 +62 800 0000 444"
    )
    alerts = await _alerts(db, a)
    assert len(alerts) == 1
    assert "address" in alerts[0]["message"] and "phone change requested" in alerts[0]["message"]
    async with db.acquire() as conn:
        profile_rows = await conn.fetchval(
            "SELECT count(*) FROM portal_messages WHERE client_id = $1"
            " AND subject = 'Profile updated'",
            a,
        )
    assert profile_rows == 1
