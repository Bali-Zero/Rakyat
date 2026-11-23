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
-- as migration_071_notification_alerts.py:18-35 (created_at, sent_at, the generated created_date,
-- the daily unique index the alert writers conflict on)
CREATE TABLE notification_alerts (
    id BIGSERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    alert_type VARCHAR(50) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    message TEXT,
    email_subject VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    created_date DATE GENERATED ALWAYS AS ((created_at AT TIME ZONE 'UTC')::DATE) STORED
);
CREATE UNIQUE INDEX uq_notification_alert_daily
    ON notification_alerts (client_id, alert_type, created_date);
-- created_at, read_at and is_system_generated as the real portal_messages table has them
CREATE TABLE portal_messages (
    id SERIAL PRIMARY KEY,
    client_id INT,
    practice_id INT,
    subject TEXT,
    direction TEXT,
    content TEXT,
    sent_by TEXT NOT NULL,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_system_generated BOOLEAN NOT NULL DEFAULT FALSE
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
            " WHERE client_id = $1 AND subject = 'Contact change requested' ORDER BY id",
            cid,
        )
    return [(r["direction"], r["content"]) for r in rows]


async def _alerts(pool, cid):
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT message, status, created_at FROM notification_alerts"
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
    assert alerts[0]["message"] == (
        "Client requested a contact change: phone, WhatsApp; also updated: address"
    )
    assert alerts[0]["status"] == "pending"
    async with db.acquire() as conn:
        profile_rows = await conn.fetchval(
            "SELECT count(*) FROM portal_messages WHERE client_id = $1"
            " AND subject = 'Profile updated'",
            a,
        )
    assert profile_rows == 1


# --- round 3: a contact change request reaches staff as work, once, with the number as asked -----


def _long_form(digits: str, length: int) -> str:
    """A number whose digits are `digits`, padded with dashes to exactly `length` characters."""
    return "+" + "-" * (length - 1 - len(digits)) + digits


async def _alert_row(pool, cid):
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT id, status, message, created_at FROM notification_alerts"
            " WHERE client_id = $1 AND alert_type = 'portal_profile_update'",
            cid,
        )


async def _refused_status(service, cid, **body) -> int:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _route(service, cid, **body)
    return exc.value.status_code


async def _staff_reply(pool, cid, *, system: bool = False):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO portal_messages (client_id, subject, direction, content, sent_by,"
            " is_system_generated) VALUES ($1, 'Re', 'team_to_client', 'ok', 'staff@example.test', $2)",
            cid,
            system,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_order_unchanged_legacy_value_over_40_is_a_no_op_beside_an_address_change(db, field):
    legacy = "+62 800 0000 111" + " ext" * 8  # 48 characters, unchanged
    assert len(legacy) > 45
    cid = await _client(db, "Zz Alpha", **{field: legacy}, address="Old")
    service = PortalService(db)

    result = await service.update_profile(
        cid, {field: legacy, "address": "New street"}, current_user=_user(cid)
    )

    assert result["contact_change_requested"] == []
    assert (await _row(db, cid))["address"] == "New street"
    assert await _requests(db, cid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_order_41_characters_with_the_stored_digits_is_refused_nothing_written(db, field):
    cid = await _client(
        db, "Zz Alpha", phone=_NUM_A, phone_normalized="628000000111", whatsapp=_NUM_A
    )
    service = PortalService(db)
    value = _long_form("628000000111", 41)
    assert len(value) == 41

    assert await _refused_status(service, cid, **{field: value, "address": "Never"}) == 422

    assert (await _row(db, cid))["address"] is None
    assert await _requests(db, cid) == [] and await _alert_row(db, cid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_order_exactly_40_characters_is_a_request_carrying_all_40(db, field):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    value = _long_form("628000000333", 40)
    assert len(value) == 40

    result = await service.update_profile(cid, {field: f"  {value}  "}, current_user=_user(cid))

    assert result["contact_change_requested"] == [field]
    ((_, content),) = await _requests(db, cid)
    assert value in content


@pytest.mark.asyncio
async def test_r1_a_field_is_compared_with_its_own_stored_value_only(db):
    cid = await _client(
        db, "Zz Alpha", phone=_NUM_A, phone_normalized="628000000111", whatsapp=_NUM_B
    )
    service = PortalService(db)

    r1 = await service.update_profile(cid, {"whatsapp": _NUM_A}, current_user=_user(cid))
    assert r1["contact_change_requested"] == ["whatsapp"]
    r2 = await service.update_profile(cid, {"phone": _NUM_B}, current_user=_user(cid))
    assert r2["contact_change_requested"] == ["phone"]
    r3 = await service.update_profile(cid, {"phone": "08000000111"}, current_user=_user(cid))
    assert r3["contact_change_requested"] == []
    contents = [c for _, c in await _requests(db, cid)]
    assert contents == [
        "Requested contact change: WhatsApp → " + _NUM_A,
        "Requested contact change: phone → " + _NUM_B,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_r2_a_padded_number_is_stored_trimmed_in_full(db, field):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)

    await service.update_profile(
        cid, {field: " " * 20 + _NUM_C + " " * 20}, current_user=_user(cid)
    )

    ((_, content),) = await _requests(db, cid)
    assert content.endswith(_NUM_C) and len(_NUM_C) == 16


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_r2_a_41_character_number_after_trim_is_422_and_files_nothing(db, field):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    value = _long_form("628000000333", 41)

    assert await _refused_status(service, cid, **{field: f" {value} "}) == 422

    assert await _requests(db, cid) == [] and await _alert_row(db, cid) is None


@pytest.mark.asyncio
async def test_r3_a_request_is_pending_reply_work_until_staff_answer(db):
    from backend.services.portal.portal_reply_service import pending_reply_query

    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    sql, args = pending_reply_query(None)

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    async with db.acquire() as conn:
        assert [r["client_id"] for r in await conn.fetch(sql, *args)] == [cid]
        sent_by = await conn.fetchval(
            "SELECT sent_by FROM portal_messages WHERE subject = 'Contact change requested'"
        )
        sysflag = await conn.fetchval(
            "SELECT is_system_generated FROM portal_messages"
            " WHERE subject = 'Contact change requested'"
        )
    assert sent_by == "zz.alpha@example.test" and sysflag is False
    alert = await _alert_row(db, cid)
    assert alert["status"] == "pending"
    assert alert["message"] == "Client requested a contact change: phone"
    assert not any(ch.isdigit() for ch in alert["message"])
    await _staff_reply(db, cid)
    async with db.acquire() as conn:
        assert await conn.fetch(sql, *args) == []


@pytest.mark.asyncio
async def test_r3_a_client_without_email_is_attributed_to_the_client_id(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, email=None)
    service = PortalService(db)

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT sent_by FROM portal_messages WHERE subject = 'Contact change requested'"
            )
            == f"client-{cid}"
        )


@pytest.mark.asyncio
async def test_r3_a_request_rearms_the_alert_an_address_change_wrote_read(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"address": "Street 1"}, current_user=_user(cid))
    before = await _alert_row(db, cid)
    assert before["status"] == "sent"

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    after = await _alert_row(db, cid)
    assert after["id"] == before["id"]
    assert after["status"] == "pending"
    assert after["message"] == "Client requested a contact change: phone"
    assert after["created_at"] > before["created_at"]


@pytest.mark.asyncio
async def test_r3_a_rearmed_alert_is_first_of_the_bell_and_an_address_save_leaves_it(db):
    from backend.app.routers.crm_notifications import get_crm_notifications

    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"address": "Street 1"}, current_user=_user(cid))
    others = [await _client(db, f"Zz Other {i}") for i in range(21)]
    async with db.acquire() as conn:
        for o in others:
            await conn.execute(
                "INSERT INTO notification_alerts (client_id, alert_type, status, message)"
                " VALUES ($1, 'portal_document_upload', 'pending', 'doc')",
                o,
            )
    admin = {"email": "admin@example.invalid", "role": "admin"}

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    bell = await get_crm_notifications(unread_only=False, limit=20, pool=db, current_user=admin)
    assert bell[0]["client_id"] == cid and bell[0]["status"] == "pending"

    armed = await _alert_row(db, cid)
    await service.update_profile(cid, {"address": "Street 2"}, current_user=_user(cid))
    again = await _alert_row(db, cid)
    assert (again["created_at"], again["status"], again["message"]) == (
        armed["created_at"],
        armed["status"],
        armed["message"],
    )


@pytest.mark.asyncio
async def test_r3_a_combined_save_names_the_address_in_the_alert(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)

    await service.update_profile(
        cid, {"phone": _NUM_C, "address": "Street 9"}, current_user=_user(cid)
    )

    assert (await _alert_row(db, cid))["message"] == (
        "Client requested a contact change: phone; also updated: address"
    )


@pytest.mark.asyncio
async def test_r4_the_same_request_twice_is_one_row_and_a_different_one_is_a_second(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)

    first = await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    second = await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    assert first["contact_change_requested"] == second["contact_change_requested"] == ["phone"]
    assert len(await _requests(db, cid)) == 1

    await service.update_profile(cid, {"phone": "+62 800 0000 444"}, current_user=_user(cid))
    assert len(await _requests(db, cid)) == 2


@pytest.mark.asyncio
async def test_r4_after_a_staff_reply_the_same_request_is_new_work(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    await _staff_reply(db, cid)

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    assert len(await _requests(db, cid)) == 2


@pytest.mark.asyncio
async def test_r4_a_system_message_or_a_read_receipt_is_not_a_staff_reply(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    await _staff_reply(db, cid, system=True)
    async with db.acquire() as conn:
        await conn.execute("UPDATE portal_messages SET read_at = NOW()")

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    assert len(await _requests(db, cid)) == 1


@pytest.mark.asyncio
async def test_r4_a_suppressed_repeat_leaves_an_alert_staff_already_read(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    async with db.acquire() as conn:
        # what crm_notifications mark-read does
        await conn.execute("UPDATE notification_alerts SET status = 'sent', sent_at = NOW()")
    read_alert = await _alert_row(db, cid)

    result = await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    assert result["contact_change_requested"] == ["phone"]
    assert len(await _requests(db, cid)) == 1
    alert = await _alert_row(db, cid)
    assert alert["status"] == "sent" and alert["created_at"] == read_alert["created_at"]


@pytest.mark.asyncio
async def test_r4_a_suppressed_repeat_with_an_address_saves_it_without_a_rearm(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
    async with db.acquire() as conn:
        await conn.execute("UPDATE notification_alerts SET status = 'sent', sent_at = NOW()")
    before = await _alert_row(db, cid)

    await service.update_profile(
        cid, {"phone": _NUM_C, "address": "Street 3"}, current_user=_user(cid)
    )

    assert (await _row(db, cid))["address"] == "Street 3"
    assert len(await _requests(db, cid)) == 1
    after = await _alert_row(db, cid)
    assert after["status"] == "sent" and after["created_at"] == before["created_at"]
    assert after["message"] == before["message"]  # DO NOTHING on the daily conflict


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "  "])
async def test_r5_an_address_empty_after_trim_is_stored_as_null(db, blank):
    cid = await _client(db, "Zz Alpha", address="Old street")
    service = PortalService(db)

    await service.update_profile(cid, {"address": blank}, current_user=_user(cid))

    assert (await _row(db, cid))["address"] is None


@pytest.mark.asyncio
async def test_r7_two_overlapping_identical_saves_file_one_request_and_one_pending_alert(db):
    import asyncio

    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    holder = await db.acquire()
    tx = holder.transaction()
    await tx.start()
    try:
        await holder.fetchval("SELECT id FROM clients WHERE id = $1 FOR UPDATE", cid)
        saves = [
            asyncio.create_task(
                service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0.5)
        assert not any(t.done() for t in saves)  # both wait for the client row
    finally:
        await tx.commit()
        await db.release(holder)
    await asyncio.gather(*saves)

    assert len(await _requests(db, cid)) == 1
    alert = await _alert_row(db, cid)
    assert alert["status"] == "pending"


@pytest.mark.asyncio
async def test_r7_a_failing_request_alert_fails_the_save_and_leaves_no_message(db):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)

    class _AlertFails(_LoggedConn):
        async def execute(self, query, *args, **kwargs):
            if "INSERT INTO notification_alerts" in str(query):
                raise RuntimeError("alert insert failed")
            return await self._conn.execute(query, *args, **kwargs)

    class _Pool(_LoggedPool):
        def acquire(self):
            outer = self

            class _Ctx:
                async def __aenter__(self_inner):
                    self_inner._cm = outer._pool.acquire()
                    return _AlertFails(await self_inner._cm.__aenter__(), outer.log)

                async def __aexit__(self_inner, *exc):
                    return await self_inner._cm.__aexit__(*exc)

            return _Ctx()

    with pytest.raises(RuntimeError):
        await PortalService(_Pool(db)).update_profile(
            cid, {"phone": _NUM_C}, current_user=_user(cid)
        )

    assert await _requests(db, cid) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["phone", "whatsapp"])
async def test_r8_a_lone_surrogate_is_422_and_nothing_is_written(db, field):
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)

    assert (
        await _refused_status(service, cid, **{field: "+62 800 \ud800 0000", "address": "X"}) == 422
    )

    assert (await _row(db, cid))["address"] is None
    assert await _requests(db, cid) == [] and await _alert_row(db, cid) is None


# --- v4/v5/v6: lock level, alert time and order, on two pooled connections -----------------------


class _Gate:
    """Pauses one pooled connection around chosen statements; every wait is bounded."""

    def __init__(self, *, pause_before=None, pause_after=None):
        import asyncio

        self.pause_before = pause_before
        self.pause_after = pause_after
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()

    async def around(self, conn, name, query, args, kwargs):
        text = str(query)
        if self.pause_before and self.pause_before in text:
            self.reached.set()
            await self.release.wait()
        result = await getattr(conn, name)(query, *args, **kwargs)
        if self.pause_before and self.pause_before in text:
            self.returned.set()
        if self.pause_after and self.pause_after in text:
            self.reached.set()
            await self.release.wait()
        return result


class _GatedConn:
    def __init__(self, conn, gate):
        self._conn = conn
        self._gate = gate

    def __getattr__(self, name):
        attr = getattr(self._conn, name)
        if name in {"execute", "fetch", "fetchrow", "fetchval"}:

            async def _call(query, *args, **kwargs):
                return await self._gate.around(self._conn, name, query, args, kwargs)

            return _call
        return attr


class _GatedPool:
    def __init__(self, pool, gate):
        self._pool = pool
        self._gate = gate

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                self_inner._cm = outer._pool.acquire()
                return _GatedConn(await self_inner._cm.__aenter__(), outer._gate)

            async def __aexit__(self_inner, *exc):
                return await self_inner._cm.__aexit__(*exc)

        return _Ctx()


_LOCK_STATEMENT = "SELECT phone, phone_normalized"


async def _wait(event, seconds=5.0):
    import asyncio

    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        return False
    return True


async def _settle(tasks, *gates):
    """Release every pause, then await the tasks (bounded) and cancel what is left."""
    import asyncio

    for gate in gates:
        gate.release.set()
    pending = [t for t in tasks if t is not None]
    if pending:
        _, still = await asyncio.wait(pending, timeout=10)
        for task in still:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_r7_an_address_saves_alert_insert_never_waits_on_a_contact_saves_lock(db):
    import asyncio

    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    assert await _alert_row(db, cid) is None
    gate_a = _Gate(pause_before="INSERT INTO notification_alerts")
    gate_b = _Gate(pause_after=_LOCK_STATEMENT)
    task_a = task_b = None
    try:
        task_a = asyncio.create_task(
            PortalService(_GatedPool(db, gate_a)).update_profile(
                cid, {"address": "Street A"}, current_user=_user(cid)
            )
        )
        assert await _wait(gate_a.reached), "A never reached its alert insert"
        task_b = asyncio.create_task(
            PortalService(_GatedPool(db, gate_b)).update_profile(
                cid, {"phone": _NUM_C, "address": "Street B"}, current_user=_user(cid)
            )
        )
        assert await _wait(gate_b.reached), "B never returned from its lock statement"

        gate_a.release.set()
        completed = await _wait(gate_a.returned, 5.0)
        assert completed, "A's alert insert waited on B's client lock"
        first = await _alert_row(db, cid)
        assert first is not None and first["status"] == "sent"

        gate_b.release.set()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), 15)
    finally:
        await _settle([task_a, task_b], gate_a, gate_b)

    assert (await _row(db, cid))["address"] == "Street B"
    assert len(await _requests(db, cid)) == 1
    alert = await _alert_row(db, cid)
    assert alert["status"] == "pending"
    assert alert["message"] == ("Client requested a contact change: phone; also updated: address")
    assert alert["created_at"] > first["created_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rearm", [True, False], ids=["rearm", "plain_insert"])
async def test_r3_order_the_request_alert_is_stamped_when_its_statement_runs(db, rearm):
    import asyncio

    from backend.app.routers.crm_notifications import get_crm_notifications

    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    others = [await _client(db, f"Zz Other {i}") for i in range(21)]
    if rearm:
        await PortalService(db).update_profile(
            cid, {"address": "Street 1"}, current_user=_user(cid)
        )
        assert (await _alert_row(db, cid))["status"] == "sent"
    else:
        assert await _alert_row(db, cid) is None
    gate = _Gate(pause_after=_LOCK_STATEMENT)
    task = None
    try:
        task = asyncio.create_task(
            PortalService(_GatedPool(db, gate)).update_profile(
                cid, {"phone": _NUM_C}, current_user=_user(cid)
            )
        )
        assert await _wait(gate.reached), "B never returned from its lock statement"
        async with db.acquire() as conn:
            for o in others:
                await conn.execute(
                    "INSERT INTO notification_alerts (client_id, alert_type, status, message)"
                    " VALUES ($1, 'portal_document_upload', 'pending', 'doc')",
                    o,
                )
            lower = await conn.fetchval("SELECT clock_timestamp()")
        gate.release.set()
        await asyncio.wait_for(task, 15)
    finally:
        await _settle([task], gate)

    row = await _alert_row(db, cid)
    assert row["created_at"] >= lower
    assert row["status"] == "pending"
    assert row["message"] == "Client requested a contact change: phone"
    admin = {"email": "admin@example.invalid", "role": "admin"}
    bell = await get_crm_notifications(unread_only=False, limit=20, pool=db, current_user=admin)
    assert bell[0]["client_id"] == cid


@pytest.mark.asyncio
async def test_r3_time_a_rearm_never_moves_a_row_backwards(db):
    from datetime import timedelta

    async with db.acquire() as conn:
        seconds = await conn.fetchval(
            "SELECT EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'UTC')::time)::int"
        )
    if seconds >= 86398:
        pytest.skip("the run starts in the last two seconds of a UTC day")
    cid = await _client(db, "Zz Alpha", phone=_NUM_A, whatsapp=_NUM_A)
    service = PortalService(db)
    await service.update_profile(cid, {"address": "Street 1"}, current_user=_user(cid))
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE notification_alerts SET created_at ="
            " (date_trunc('day', NOW() AT TIME ZONE 'UTC') + interval '23:59:59') AT TIME ZONE 'UTC'"
            " WHERE client_id = $1",
            cid,
        )
    before = await _alert_row(db, cid)
    assert before["status"] == "sent"

    await service.update_profile(cid, {"phone": _NUM_C}, current_user=_user(cid))

    after = await _alert_row(db, cid)
    assert after["status"] == "pending"
    assert after["message"] == "Client requested a contact change: phone"
    assert after["created_at"] == before["created_at"] + timedelta(microseconds=1)
