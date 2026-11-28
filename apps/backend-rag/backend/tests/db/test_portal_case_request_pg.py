"""The portal route that lists a client's required documents names who asked for a slot
(V2 fourth cut), on a real Postgres.

Every case runs the route's real handler, which sends the module-level statement
``REQUIRED_DOCUMENTS_SQL``, on the real tables inside a rolled-back transaction (a thin pool
hands the one connection to the handler). ``practice_required_documents`` and
``practices.client_notification_sent`` are created when absent, as the earlier cuts' Postgres
tests do (the database the hosted job builds has neither). ``create_document_request`` stamps
every slot with the transaction's ``NOW()``, so each slot's ``created_at`` is set explicitly and
distinct afterwards, the asked slots OLDER than the seeded ones, all ``is_required = true``: the
leading term of the ORDER BY, and not base's two terms, is what the order cases prove. Invented
names and ``example.test`` addresses only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers.portal import REQUIRED_DOCUMENTS_SQL, get_required_documents
from backend.services.crm.document_requests import create_document_request

PRD_DDL = """
CREATE TABLE IF NOT EXISTS practice_required_documents (
    id SERIAL PRIMARY KEY,
    practice_id INTEGER NOT NULL REFERENCES practices(id) ON DELETE CASCADE,
    document_type VARCHAR(50) NOT NULL,
    document_label VARCHAR(100) NOT NULL,
    description TEXT,
    is_required BOOLEAN DEFAULT TRUE,
    uploaded_by_client BOOLEAN DEFAULT FALSE,
    uploaded_file_id INTEGER,
    uploaded_at TIMESTAMP,
    client_notes TEXT,
    team_member_notes TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    created_by VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (practice_id, document_type),
    CONSTRAINT valid_status CHECK (status IN ('pending', 'uploaded', 'verified', 'rejected'))
)
"""
COLUMN_DDL = (
    "ALTER TABLE practices ADD COLUMN IF NOT EXISTS client_notification_sent boolean DEFAULT false"
)

# Base's statement, kept here as the oracle for a client with no request row.
BASE_SQL = """
    SELECT prd.id
    FROM practice_required_documents prd
    JOIN practices p ON prd.practice_id = p.id
    LEFT JOIN practice_types pt ON p.practice_type_id = pt.id
    WHERE p.client_id = $1 AND p.status != 'cancelled'
    ORDER BY prd.is_required DESC, prd.created_at DESC
"""

T0 = datetime(2026, 6, 1, 8, 0, 0)  # naive: practice_required_documents.created_at is timestamp
ASKED_AT = datetime(2026, 11, 28, 12, 0, 0, tzinfo=timezone.utc)
ASKER = "zz.asker@example.test"


class _Acquire:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def __aenter__(self) -> asyncpg.Connection:
        return self.conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _Pool:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


class World:
    def __init__(self, conn: asyncpg.Connection, client_id: int, type_id: int) -> None:
        self.conn = conn
        self.client_id = client_id
        self.type_id = type_id

    async def practice(self, status: str | None = None, client_id: int | None = None) -> int:
        practice = await self.conn.fetchval(
            "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id",
            client_id or self.client_id,
            self.type_id,
        )
        if status is not None:
            await self.conn.execute(
                "UPDATE practices SET status = $2 WHERE id = $1", practice, status
            )
        return practice

    async def member(
        self,
        email: str,
        name: str | None,
        role: str = "member",
        full_name: str | None = "Zz Full Name Family",
    ) -> str:
        member_id = str(uuid4())
        await self.conn.execute(
            "INSERT INTO team_members (id, full_name, email, pin_hash, role, name) "
            "VALUES ($1, $2, $3, 'x', $4, $5)",
            member_id,
            full_name,
            email,
            role,
            name,
        )
        return member_id

    async def seed(self, practice: int, count: int = 3, offset: int = 0) -> list[int]:
        """Seeded slots, required, distinct created_at, all NEWER than any asked slot."""
        ids = []
        for i in range(count):
            slot = await self.conn.fetchval(
                "INSERT INTO practice_required_documents "
                "(practice_id, document_type, document_label, description, is_required, created_at) "
                "VALUES ($1, $2, $3, 'seeded', true, $4) RETURNING id",
                practice,
                f"zz_seed_{i}",
                f"Zz Seed {i}",
                T0 + timedelta(days=100 + offset + i),
            )
            ids.append(slot)
        return ids

    async def ask(
        self,
        practice: int,
        document_type: str = "zz_asked",
        age: int = 0,
        status: str = "pending",
        requested_by: str = ASKER,
    ) -> tuple[int, int]:
        """Ask through the real service, then set the slot's created_at (older than the seeded
        ones; a larger ``age`` is older) and status. Returns (slot id, request id)."""
        created = await create_document_request(
            self.conn,
            practice_id=practice,
            requested_by=requested_by,
            document_label="Zz Asked",
            client_text="What the client reads",
            reason="Why this case needs it",
            document_type=document_type,
        )
        slot_id = created.slot["id"]
        await self.conn.execute(
            "UPDATE practice_required_documents SET created_at = $2, status = $3 WHERE id = $1",
            slot_id,
            T0 - timedelta(days=age),
            status,
        )
        await self.conn.execute(
            "UPDATE practice_document_requests SET requested_at = $2 WHERE id = $1",
            created.request_id,
            ASKED_AT,
        )
        return slot_id, created.request_id

    async def answer(self, client_id: int | None = None) -> list[dict]:
        out = await get_required_documents(
            client={"client_id": client_id or self.client_id}, db_pool=_Pool(self.conn)
        )
        assert out["success"] is True
        assert "@" not in json.dumps(out)
        return out["data"]


@pytest_asyncio.fixture
async def world(db_tx: asyncpg.Connection) -> World:
    await db_tx.execute(PRD_DDL)
    await db_tx.execute(COLUMN_DDL)
    type_id = await db_tx.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('zz_case_req', 'Zz Service') RETURNING id"
    )
    client_id = await db_tx.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz case request client') RETURNING id"
    )
    return World(db_tx, client_id, type_id)


def _ids(items: list[dict]) -> list[int]:
    return [i["id"] for i in items]


@pytest.mark.asyncio
async def test_the_asked_slot_is_first_with_the_member_name_and_the_request_time(
    world: World,
) -> None:
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    seeded = await world.seed(practice)
    slot, _ = await world.ask(practice)
    items = await world.answer()
    assert _ids(items)[0] == slot
    assert items[0]["case_request"] == {"added_by": "Zz Maya", "added_at": ASKED_AT.isoformat()}
    assert set(_ids(items[1:])) == set(seeded)
    assert all("case_request" not in i for i in items[1:])
    assert len(items) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploaded", "verified"])
async def test_an_uploaded_or_verified_asked_slot_is_placed_by_base_terms_key_kept(
    world: World, status: str
) -> None:
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    seeded = await world.seed(practice)
    slot, _ = await world.ask(practice, status=status)
    items = await world.answer()
    assert _ids(items)[-1] == slot
    assert set(_ids(items[:-1])) == set(seeded)
    assert items[-1]["case_request"]["added_by"] == "Zz Maya"


@pytest.mark.asyncio
async def test_a_rejected_asked_slot_is_first_again(world: World) -> None:
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    await world.seed(practice)
    slot, _ = await world.ask(practice, status="rejected")
    items = await world.answer()
    assert _ids(items)[0] == slot
    assert items[0]["case_request"]["added_by"] == "Zz Maya"


@pytest.mark.asyncio
async def test_two_asked_slots_are_both_first_the_later_created_at_first(world: World) -> None:
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    seeded = await world.seed(practice)
    older, _ = await world.ask(practice, "zz_asked_older", age=20)
    newer, _ = await world.ask(practice, "zz_asked_newer", age=10)
    items = await world.answer()
    assert _ids(items)[:2] == [newer, older]
    assert set(_ids(items[2:])) == set(seeded)


@pytest.mark.asyncio
async def test_a_second_request_row_on_a_slot_does_not_double_it(world: World) -> None:
    await world.member(ASKER, "Zz Maya")
    await world.member("zz.second@example.test", "Zz Tala")
    practice = await world.practice()
    await world.seed(practice)
    slot, first_request = await world.ask(practice)
    later = ASKED_AT + timedelta(days=2)
    await world.conn.execute(
        "INSERT INTO practice_document_requests "
        "(practice_id, required_document_id, document_type, label, client_text, reason, "
        " requested_by, requested_at) "
        "VALUES ($1, $2, 'zz_asked', 'Zz Asked', 'again', 'again', $3, $4)",
        practice,
        slot,
        "zz.second@example.test",
        later,
    )
    items = await world.answer()
    assert _ids(items).count(slot) == 1 and len(items) == 4
    asked = next(i for i in items if i["id"] == slot)
    assert asked["case_request"] == {"added_by": "Zz Tala", "added_at": later.isoformat()}


@pytest.mark.asyncio
async def test_a_requester_with_no_member_row_gives_a_null_name_and_keeps_the_time(
    world: World,
) -> None:
    practice = await world.practice()
    slot, _ = await world.ask(practice, requested_by="zz.nobody@example.test")
    (item,) = await world.answer()
    assert item["id"] == slot
    assert item["case_request"] == {"added_by": None, "added_at": ASKED_AT.isoformat()}


@pytest.mark.asyncio
async def test_a_requester_whose_only_member_row_is_a_client_gives_null(world: World) -> None:
    await world.member(ASKER, "Zz Client Person", role="client")
    practice = await world.practice()
    await world.ask(practice)
    (item,) = await world.answer()
    assert item["case_request"]["added_by"] is None


@pytest.mark.asyncio
async def test_the_member_address_in_another_letter_case_is_matched(world: World) -> None:
    await world.member("ZZ.Asker@Example.Test", "Zz Maya")
    practice = await world.practice()
    await world.ask(practice)
    (item,) = await world.answer()
    assert item["case_request"]["added_by"] == "Zz Maya"


@pytest.mark.asyncio
async def test_two_member_rows_with_one_address_in_two_cases_give_one_item_lowest_id_name(
    world: World,
) -> None:
    low = "00000000-0000-0000-0000-000000000001"
    high = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    for member_id, email, name in (
        (high, "ZZ.ASKER@EXAMPLE.TEST", "Zz High"),
        (low, ASKER, "Zz Low"),
    ):
        await world.conn.execute(
            "INSERT INTO team_members (id, full_name, email, pin_hash, role, name) "
            "VALUES ($1, 'Zz Full', $2, 'x', 'member', $3)",
            member_id,
            email,
            name,
        )
    practice = await world.practice()
    slot, _ = await world.ask(practice)
    items = await world.answer()
    assert _ids(items) == [slot]
    assert items[0]["case_request"]["added_by"] == "Zz Low"


@pytest.mark.asyncio
async def test_a_cancelled_practice_gives_none_of_its_slots(world: World) -> None:
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    await world.seed(practice)
    await world.ask(practice)
    await world.conn.execute("UPDATE practices SET status = 'cancelled' WHERE id = $1", practice)
    assert await world.answer() == []


@pytest.mark.asyncio
async def test_another_clients_asked_slot_does_not_reach_this_client(world: World) -> None:
    await world.member(ASKER, "Zz Maya")
    other = await world.conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz other case client') RETURNING id"
    )
    other_practice = await world.practice(client_id=other)
    other_slot, _ = await world.ask(other_practice)
    mine = await world.practice()
    my_seeded = await world.seed(mine, 1)
    assert _ids(await world.answer()) == my_seeded
    assert _ids(await world.answer(other)) == [other_slot]


@pytest.mark.asyncio
async def test_a_request_row_of_another_practice_does_not_label_this_slot(world: World) -> None:
    """``required_document_id`` carries no foreign key, so the join matches the practice too: a
    request row of another practice that holds this slot's id names nobody on it."""
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    seeded = await world.seed(practice)
    other_client = await world.conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz stray request client') RETURNING id"
    )
    other_practice = await world.practice(client_id=other_client)
    await world.conn.execute(
        "INSERT INTO practice_document_requests "
        "(practice_id, required_document_id, document_type, label, client_text, reason, "
        " requested_by, requested_at) "
        "VALUES ($1, $2, 'zz_stray', 'Zz Stray', 'stray', 'stray', $3, $4)",
        other_practice,
        seeded[0],
        ASKER,
        ASKED_AT,
    )
    items = await world.answer()
    assert sorted(_ids(items)) == sorted(seeded)
    assert all("case_request" not in item for item in items)
    assert _ids(items) == [r["id"] for r in await world.conn.fetch(BASE_SQL, world.client_id)]


@pytest.mark.asyncio
async def test_for_a_client_with_no_request_rows_the_order_is_the_one_of_base(
    world: World,
) -> None:
    first = await world.practice()
    second = await world.practice()
    await world.seed(first, 3)
    await world.seed(second, 2, offset=10)
    # a not-required slot, to exercise base's first term
    await world.conn.execute(
        "INSERT INTO practice_required_documents "
        "(practice_id, document_type, document_label, is_required, created_at) "
        "VALUES ($1, 'zz_optional', 'Zz Optional', false, $2)",
        first,
        T0 + timedelta(days=500),
    )
    base = [r["id"] for r in await world.conn.fetch(BASE_SQL, world.client_id)]
    items = await world.answer()
    assert _ids(items) == base
    assert all("case_request" not in i for i in items)


@pytest.mark.asyncio
async def test_the_statement_result_never_carries_requested_by_email_or_full_name(
    world: World,
) -> None:
    await world.member(ASKER, "Zz Maya")
    practice = await world.practice()
    await world.ask(practice)
    rows = await world.conn.fetch(REQUIRED_DOCUMENTS_SQL, world.client_id)
    assert rows
    keys = set(rows[0].keys())
    assert keys.isdisjoint({"requested_by", "email", "full_name", "reason", "client_text"})
    assert {"case_requested_at", "case_added_by_name"} <= keys
    for item in await world.answer():
        assert set(item).isdisjoint({"requested_by", "email", "full_name"})
        assert "Zz Full Name Family" not in json.dumps(item)
