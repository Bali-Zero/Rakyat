"""The practice row says where its request stands; its client text can be corrected (V2, third cut)
on a real Postgres.

One-connection cases run on the real tables inside a rolled-back transaction, through the real
service functions; ``practice_required_documents`` and ``practices.client_notification_sent`` are
created when absent, as backend/tests/db/test_document_request_review_pg.py does (the database the
hosted job builds has neither). Inside one rolled-back transaction ``NOW()`` is fixed, so these
cases do not assert that ``updated_at`` moves. The two-connection case needs committed data and
runs in a throwaway schema (its own pool and schema, the shape of the review file's) built WITHOUT
the production ``BEFORE UPDATE`` trigger: the statement itself must move ``updated_at``. Invented
names only.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.crm.document_requests import (
    DocumentRequestRefused,
    correct_client_text,
    create_document_request,
    decide_request,
    list_practice_requests,
    list_requests,
    reopen_request,
)

BACKEND = Path(__file__).resolve().parents[2]
MIGRATION_397 = BACKEND / "db" / "migrations_v2" / "397_practice_document_requests.sql"
MARK = "-- === ROLLBACK ==="
REVIEWER = "reviewer@example.test"
NEW_TEXT = "Zz the corrected text the client reads"

# As migration 054 creates it (without its trigger).
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


class World:
    def __init__(self, conn: asyncpg.Connection, client_id: int, type_id: int) -> None:
        self.conn = conn
        self.client_id = client_id
        self.type_id = type_id

    async def practice(self, status: str | None = None) -> int:
        practice = await self.conn.fetchval(
            "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id",
            self.client_id,
            self.type_id,
        )
        if status is not None:
            await self.conn.execute(
                "UPDATE practices SET status = $2 WHERE id = $1", practice, status
            )
        return practice

    async def card(self) -> None:
        await self.conn.execute(
            "INSERT INTO service_cards (practice_type_id, block, active) VALUES ($1, 'visa', true)",
            self.type_id,
        )

    async def ask(self, practice: int, document_type: str = "zz_document"):
        return await create_document_request(
            self.conn,
            practice_id=practice,
            requested_by="asker@example.test",
            document_label="Zz Document",
            client_text="What the client reads",
            reason="Why this case needs it",
            document_type=document_type,
        )

    async def correct(self, practice: int, request_id: int, text: str = NEW_TEXT):
        return await correct_client_text(
            self.conn, practice_id=practice, request_id=request_id, client_text=text
        )

    async def rows(self, sql: str, *args) -> list[dict]:
        found = await self.conn.fetch(f"SELECT row_to_json(t)::text AS j FROM ({sql}) t", *args)
        return [json.loads(r["j"]) for r in found]

    async def request(self, request_id: int) -> dict:
        (row,) = await self.rows(
            "SELECT * FROM practice_document_requests WHERE id = $1", request_id
        )
        return row

    async def slots(self, practice: int) -> list[dict]:
        return await self.rows(
            "SELECT * FROM practice_required_documents WHERE practice_id = $1 ORDER BY id",
            practice,
        )

    async def snapshot(self, practice: int, request_id: int) -> dict:
        return {
            "request": await self.request(request_id),
            "slots": await self.slots(practice),
            "practice": await self.rows("SELECT * FROM practices WHERE id = $1", practice),
            "card_documents": await self.rows("SELECT * FROM service_card_documents ORDER BY id"),
            "cards": await self.rows("SELECT * FROM service_cards ORDER BY practice_type_id"),
        }


@pytest_asyncio.fixture
async def world(db_tx: asyncpg.Connection) -> World:
    await db_tx.execute(PRD_DDL)
    await db_tx.execute(COLUMN_DDL)
    type_id = await db_tx.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('zz_slot_a', 'Zz Service') RETURNING id"
    )
    client_id = await db_tx.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz slot state client') RETURNING id"
    )
    return World(db_tx, client_id, type_id)


def _without(row: dict, *keys: str) -> dict:
    return {k: v for k, v in row.items() if k not in keys}


# ------------------------------------------------------------------------ the read


@pytest.mark.asyncio
async def test_after_the_ask_the_read_shows_one_pending_item_with_the_slot_id(
    world: World,
) -> None:
    practice = await world.practice()
    asked = await world.ask(practice)
    items = await list_practice_requests(world.conn, practice)
    assert len(items) == 1
    (item,) = items
    assert set(item) == {"id", "required_document_id", "state", "requested_by", "requested_at"}
    assert item["id"] == asked.request_id and item["required_document_id"] == asked.slot["id"]
    assert item["state"] == "pending" and item["requested_by"] == "asker@example.test"
    assert await list_practice_requests(world.conn, await world.practice()) == []


@pytest.mark.asyncio
async def test_the_state_follows_each_decision_and_the_reopen(world: World) -> None:
    await world.card()
    practice = await world.practice()
    asked = await world.ask(practice)

    async def state() -> str:
        (item,) = await list_practice_requests(world.conn, practice)
        return item["state"]

    await decide_request(
        world.conn, asked.request_id, decision="case_only", condition=None,
        client_text=None, reviewer=REVIEWER,
    )  # fmt: skip
    assert await state() == "case_only"
    await reopen_request(world.conn, asked.request_id, reviewer=REVIEWER)
    assert await state() == "pending"
    await decide_request(
        world.conn, asked.request_id, decision="added_to_card", condition=None,
        client_text=None, reviewer=REVIEWER,
    )  # fmt: skip
    assert await state() == "added_to_card"


@pytest.mark.asyncio
async def test_the_read_is_newest_first_and_keeps_a_request_whose_slot_was_deleted(
    world: World,
) -> None:
    practice = await world.practice()
    first = await world.ask(practice, "zz_first")
    second = await world.ask(practice, "zz_second")
    await world.conn.execute(
        "DELETE FROM practice_required_documents WHERE id = $1", first.slot["id"]
    )
    items = await list_practice_requests(world.conn, practice)
    assert [i["id"] for i in items] == [second.request_id, first.request_id]
    assert items[1]["state"] == "pending"


# -------------------------------------------------------------------- the correction


@pytest.mark.asyncio
@pytest.mark.parametrize("decided", ["pending", "case_only", "added_to_card"])
async def test_a_correction_changes_the_description_and_the_client_text_and_nothing_else(
    world: World, decided: str
) -> None:
    await world.card()
    practice = await world.practice()
    other = await world.practice()
    asked = await world.ask(practice)
    await world.ask(practice, "zz_sibling")
    await world.ask(other, "zz_elsewhere")
    if decided != "pending":
        await decide_request(
            world.conn, asked.request_id, decision=decided, condition="Zz condition",
            client_text=None, reviewer=REVIEWER,
        )  # fmt: skip
    before = await world.snapshot(practice, asked.request_id)
    before_other = await world.slots(other)

    document = await world.correct(practice, asked.request_id, NEW_TEXT)

    after = await world.snapshot(practice, asked.request_id)
    assert document["description"] == NEW_TEXT and document["id"] == asked.slot["id"]
    assert document["practice_id"] == practice
    assert set(document) == {
        "id", "practice_id", "document_type", "document_label", "description", "is_required",
        "uploaded_by_client", "uploaded_file_id", "uploaded_at", "client_notes",
        "team_member_notes", "status", "created_at", "updated_at",
    }  # fmt: skip
    # the slot: only description and updated_at may differ
    changed = [
        (b, a)
        for b, a in zip(before["slots"], after["slots"], strict=True)
        if b["id"] == asked.slot["id"]
    ]
    ((slot_before, slot_after),) = changed
    assert _without(slot_before, "description", "updated_at") == _without(
        slot_after, "description", "updated_at"
    )
    assert slot_after["description"] == NEW_TEXT
    other_slots = [
        (b, a)
        for b, a in zip(before["slots"], after["slots"], strict=True)
        if b["id"] != asked.slot["id"]
    ]
    assert all(b == a for b, a in other_slots)
    # the request: only client_text differs
    assert after["request"]["client_text"] == NEW_TEXT
    assert _without(before["request"], "client_text") == _without(after["request"], "client_text")
    # nothing else of the world moved
    assert after["practice"] == before["practice"]
    assert after["card_documents"] == before["card_documents"]
    assert after["cards"] == before["cards"]
    assert await world.slots(other) == before_other


@pytest.mark.asyncio
async def test_after_a_correction_the_reviewers_list_returns_the_corrected_text(
    world: World,
) -> None:
    practice = await world.practice()
    asked = await world.ask(practice)
    await world.correct(practice, asked.request_id)
    answer = await list_requests(world.conn, state="pending", limit=100, offset=0)
    (item,) = [i for i in answer["items"] if i["id"] == asked.request_id]
    assert item["client_text"] == NEW_TEXT


@pytest.mark.asyncio
async def test_a_deleted_slot_is_slot_withdrawn_and_the_request_is_unchanged(
    world: World,
) -> None:
    practice = await world.practice()
    asked = await world.ask(practice)
    await world.conn.execute(
        "DELETE FROM practice_required_documents WHERE id = $1", asked.slot["id"]
    )
    before = await world.snapshot(practice, asked.request_id)
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(practice, asked.request_id)
    assert refused.value.code == "slot_withdrawn"
    assert await world.snapshot(practice, asked.request_id) == before


@pytest.mark.asyncio
async def test_a_request_with_no_slot_id_is_slot_withdrawn_and_nothing_changes(
    world: World,
) -> None:
    """`required_document_id` is nullable and has no foreign key: a row without it is refused
    and neither the request nor any slot of the practice is written."""
    practice = await world.practice()
    asked = await world.ask(practice)
    await world.conn.execute(
        "UPDATE practice_document_requests SET required_document_id = NULL WHERE id = $1",
        asked.request_id,
    )
    before = await world.snapshot(practice, asked.request_id)
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(practice, asked.request_id)
    assert refused.value.code == "slot_withdrawn"
    assert await world.snapshot(practice, asked.request_id) == before


@pytest.mark.asyncio
async def test_an_uploaded_slot_is_slot_answered_and_a_rejected_one_is_corrected(
    world: World,
) -> None:
    practice = await world.practice()
    asked = await world.ask(practice)
    slot_id = asked.slot["id"]
    await world.conn.execute(
        "UPDATE practice_required_documents SET status = 'uploaded' WHERE id = $1", slot_id
    )
    before = await world.snapshot(practice, asked.request_id)
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(practice, asked.request_id)
    assert refused.value.code == "slot_answered"
    assert await world.snapshot(practice, asked.request_id) == before

    await world.conn.execute(
        "UPDATE practice_required_documents SET status = 'verified' WHERE id = $1", slot_id
    )
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(practice, asked.request_id)
    assert refused.value.code == "slot_answered"

    await world.conn.execute(
        "UPDATE practice_required_documents SET status = 'rejected' WHERE id = $1", slot_id
    )
    document = await world.correct(practice, asked.request_id)
    assert document["status"] == "rejected" and document["description"] == NEW_TEXT
    assert (await world.request(asked.request_id))["client_text"] == NEW_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [" Cancelled ", "completed", "CANCELED"])
async def test_a_closed_practice_is_practice_closed_and_nothing_changes(
    world: World, status: str
) -> None:
    practice = await world.practice()
    asked = await world.ask(practice)
    await world.conn.execute("UPDATE practices SET status = $2 WHERE id = $1", practice, status)
    before = await world.snapshot(practice, asked.request_id)
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(practice, asked.request_id)
    assert refused.value.code == "practice_closed"
    assert await world.snapshot(practice, asked.request_id) == before


@pytest.mark.asyncio
async def test_the_request_of_another_practice_is_request_not_found(world: World) -> None:
    practice = await world.practice()
    other = await world.practice()
    asked = await world.ask(practice)
    before = await world.snapshot(practice, asked.request_id)
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(other, asked.request_id)
    assert refused.value.code == "request_not_found"
    with pytest.raises(DocumentRequestRefused) as missing:
        await world.correct(practice, asked.request_id + 100000)
    assert missing.value.code == "request_not_found"
    assert await world.snapshot(practice, asked.request_id) == before


@pytest.mark.asyncio
async def test_a_practice_that_does_not_exist_is_practice_not_found(world: World) -> None:
    with pytest.raises(DocumentRequestRefused) as refused:
        await world.correct(2_000_000_000, 1)
    assert refused.value.code == "practice_not_found"


# ---------------------------------------------------------------- two connections

# Throwaway schema, built WITHOUT the production BEFORE UPDATE trigger of the slot table.
SCHEMA_SQL = """
CREATE TABLE practices(
    id int primary key,
    status text,
    practice_type_id int,
    client_notification_sent boolean DEFAULT false,
    updated_at timestamptz DEFAULT now()
);
CREATE TABLE service_cards(practice_type_id int PRIMARY KEY, active boolean NOT NULL DEFAULT false,
    updated_by text, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE service_card_documents(
    id bigserial PRIMARY KEY,
    practice_type_id integer NOT NULL REFERENCES service_cards(practice_type_id) ON DELETE CASCADE,
    document_type varchar(50) NOT NULL,
    label varchar(100) NOT NULL,
    client_text text NOT NULL,
    provider text NOT NULL CHECK (provider IN ('client', 'us', 'sponsor')),
    condition_text text,
    reuse text NOT NULL CHECK (reuse IN ('client_file', 'fresh')),
    source text NOT NULL CHECK (source IN ('official', 'house', 'request')),
    source_request_id bigint,
    sort_order integer NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (practice_type_id, document_type)
);
CREATE TABLE practice_required_documents (
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
    UNIQUE (practice_id, document_type)
);
INSERT INTO practices (id, status, practice_type_id) VALUES (1, 'inquiry', 10);
INSERT INTO service_cards (practice_type_id, active) VALUES (10, true);
"""


@pytest_asyncio.fixture
async def pool():
    dsn = os.environ.get("TEST_DATABASE_URL", "")
    url = urlsplit(dsn)
    if not dsn or url.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in url.path:
        pytest.skip("Requires a local test database, provided by the backend CI shard")
    schema = "docreq_slot_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f"CREATE SCHEMA {schema}")
    db = None
    try:
        db = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        async with db.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
            forward = MIGRATION_397.read_text(encoding="utf-8").split(MARK)[0]
            await conn.execute(forward)
        yield db
    finally:
        if db is not None:
            await db.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def _committed_ask(db: asyncpg.Pool, document_type: str):
    async with db.acquire() as conn:
        return await create_document_request(
            conn,
            practice_id=1,
            requested_by="asker@example.test",
            document_label="Zz Label",
            client_text="What the client reads",
            reason="Why this case needs it",
            document_type=document_type,
        )


async def _correct(db: asyncpg.Pool, request_id: int, text: str):
    async with db.acquire() as conn:
        return await correct_client_text(
            conn, practice_id=1, request_id=request_id, client_text=text
        )


async def _decide(db: asyncpg.Pool, request_id: int, decision: str):
    async with db.acquire() as conn:
        return await decide_request(
            conn, request_id, decision=decision, condition=None, client_text=None, reviewer=REVIEWER
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["case_only", "added_to_card"])
async def test_a_correction_racing_a_decision_both_end_cleanly(pool, decision: str) -> None:
    for attempt in range(5):
        asked = await _committed_ask(pool, f"zz_race_{decision}_{attempt}")
        async with pool.acquire() as conn:
            slot_before = await conn.fetchval(
                "SELECT updated_at FROM practice_required_documents WHERE id = $1",
                asked.slot["id"],
            )
        await asyncio.sleep(0.01)
        text = f"{NEW_TEXT} {attempt}"
        results = await asyncio.wait_for(
            asyncio.gather(
                _correct(pool, asked.request_id, text),
                _decide(pool, asked.request_id, decision),
                return_exceptions=True,
            ),
            timeout=20,
        )
        for result in results:
            assert isinstance(result, dict), repr(result)
        async with pool.acquire() as conn:
            request = await conn.fetchrow(
                "SELECT client_text, decision FROM practice_document_requests WHERE id = $1",
                asked.request_id,
            )
            slot = await conn.fetchrow(
                "SELECT description, updated_at FROM practice_required_documents WHERE id = $1",
                asked.slot["id"],
            )
        assert request["client_text"] == text
        assert request["decision"] == decision
        assert slot["description"] == text
        assert slot["updated_at"] > slot_before
