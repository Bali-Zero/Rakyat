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


# --- own contact numbers: the rule staff edits follow --------------------------------------------

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


async def _refusal(service, cid, fields):
    from backend.services.portal._mixins.billing import ProfileContactRefused

    with pytest.raises(ProfileContactRefused) as exc:
        await service.update_profile(cid, fields, current_user=_user(cid))
    return str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_a_number_owned_by_another_client_is_refused_and_nothing_is_saved(db, field):
    a, b = await _two_clients(db)
    service = PortalService(db)

    await _refusal(service, a, {field: "0800-0000-222", "address": "Never saved"})

    row = await _row(db, a)
    assert row["phone"] == _NUM_A and row["whatsapp"] == _NUM_A and row["address"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_an_empty_or_unusable_number_is_refused_the_same_way(db, field):
    a, b = await _two_clients(db)
    service = PortalService(db)

    taken = await _refusal(service, a, {field: "08000000222"})
    for unusable in ("", "   ", "12-3"):
        assert await _refusal(service, a, {field: unusable}) == taken

    row = await _row(db, a)
    assert row["phone"] == _NUM_A and row["whatsapp"] == _NUM_A


@pytest.mark.asyncio
async def test_the_refusal_names_nobody_and_does_not_say_which_case_it_was(db):
    a, b = await _two_clients(db)
    service = PortalService(db)

    message = await _refusal(service, a, {"phone": "08000000222"})

    assert "Zz" not in message and "Bravo" not in message and "222" not in message
    assert "owned" not in message.lower() and "empty" not in message.lower()


@pytest.mark.asyncio
async def test_the_clients_own_current_number_saved_again_is_accepted(db):
    a, b = await _two_clients(db)
    service = PortalService(db)

    result = await service.update_profile(
        a, {"phone": "08000000111", "whatsapp": _NUM_A, "address": "Kept"}, current_user=_user(a)
    )

    assert result["phone"] == "08000000111" and result["address"] == "Kept"


@pytest.mark.asyncio
async def test_a_free_number_and_an_address_edit_save_as_before(db):
    a, b = await _two_clients(db)
    service = PortalService(db)

    result = await service.update_profile(
        a, {"phone": _NUM_C, "address": "Street 9"}, current_user=_user(a)
    )

    assert result["phone"] == _NUM_C and result["address"] == "Street 9"
    assert (await _row(db, b))["phone"] == _NUM_B


@pytest.mark.asyncio
async def test_the_route_answers_a_refused_number_with_one_generic_409(db):
    from backend.app.routers.portal import UpdateProfileRequest, update_profile

    a, b = await _two_clients(db)
    service = PortalService(db)
    statuses = []
    for value in ("08000000222", ""):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await update_profile(
                UpdateProfileRequest(phone=value), client=_user(a), portal_service=service
            )
        statuses.append((exc.value.status_code, exc.value.detail))

    assert statuses[0] == statuses[1] and statuses[0][0] == 409
