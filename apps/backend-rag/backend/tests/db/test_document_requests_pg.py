"""Document requests (V2) on a real Postgres: the 397 migration executed (not parsed) in a scratch
schema, and the service on the real ``practices`` / ``practice_required_documents`` tables.

The database the hosted job builds has neither ``practice_required_documents`` nor
``practices.client_notification_sent`` (both come from Python-era migrations the SQL runner does
not apply): the fixture creates them when absent, inside the rolled-back transaction. Invented
names only.
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest

from backend.services.crm.document_requests import (
    DocumentRequestRefused,
    count_recent_requests,
    create_document_request,
)

BACKEND = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND / "db" / "migrations_v2" / "397_practice_document_requests.sql"
MARK = "-- === ROLLBACK ==="
SCRATCH = "zz_docreq_scratch"

# As migration 054 creates it; the column is added as that migration does.
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


async def _world(conn: asyncpg.Connection) -> dict[str, int]:
    """Legacy objects when absent, the migration's forward part in a scratch schema, two types and
    one practice. The scratch schema comes first on the search path, so the service reaches the
    new table there and every other table in public."""
    await conn.execute(PRD_DDL)
    await conn.execute(COLUMN_DDL)
    forward, _rollback = MIGRATION.read_text(encoding="utf-8").split(MARK)
    await conn.execute(f"CREATE SCHEMA {SCRATCH}")
    await conn.execute(f"SET LOCAL search_path TO {SCRATCH}, public")
    await conn.execute(forward)
    type_a = await conn.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('zz_req_a', 'Zz Service A') RETURNING id"
    )
    type_b = await conn.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('zz_req_b', 'Zz Service B') RETURNING id"
    )
    client_id = await conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz request client') RETURNING id"
    )
    practice = await conn.fetchval(
        "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id",
        client_id,
        type_a,
    )
    return {"a": type_a, "b": type_b, "practice": practice}


async def _ask(conn: asyncpg.Connection, practice: int, label: str = "Zz Document", **over):
    args = {
        "practice_id": practice,
        "requested_by": "asker@example.test",
        "document_label": label,
        "client_text": "What the client reads",
        "reason": "Why this case needs it",
        "document_type": "zz_document",
    }
    args.update(over)
    return await create_document_request(conn, **args)


async def _request_rows(conn, practice: int):
    return await conn.fetch(
        "SELECT * FROM practice_document_requests WHERE practice_id = $1 ORDER BY id", practice
    )


async def _slots(conn, practice: int):
    return await conn.fetch(
        "SELECT * FROM public.practice_required_documents WHERE practice_id = $1 ORDER BY id",
        practice,
    )


@pytest.mark.asyncio
async def test_forward_creates_the_table_with_its_checks_and_rollback_removes_it(db_tx) -> None:
    ids = await _world(db_tx)
    assert await db_tx.fetchval(f"SELECT to_regclass('{SCRATCH}.practice_document_requests')")
    indexes = {
        r["indexname"]
        for r in await db_tx.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 "
            "AND tablename = 'practice_document_requests'",
            SCRATCH,
        )
    }
    assert {
        "idx_practice_document_requests_practice",
        "idx_practice_document_requests_seen",
    } <= indexes

    async def insert(decision: str) -> None:
        async with db_tx.transaction():
            await db_tx.execute(
                "INSERT INTO practice_document_requests (practice_id, document_type, label, "
                "client_text, reason, requested_by, decision) "
                "VALUES ($1, 'zz_x', 'Zz', 'text', 'why', 'asker@example.test', $2)",
                ids["practice"],
                decision,
            )

    for decision in ("pending", "added_to_card", "case_only"):
        await insert(decision)
    with pytest.raises(asyncpg.CheckViolationError):
        await insert("approved")

    # No foreign key on the two plain integers: a deleted type or slot leaves the history.
    await db_tx.execute(
        "INSERT INTO practice_document_requests (practice_id, required_document_id, "
        "practice_type_id, document_type, label, client_text, reason, requested_by) "
        "VALUES ($1, 999999, 999999, 'zz_y', 'Zz', 'text', 'why', 'asker@example.test')",
        ids["practice"],
    )
    # ON DELETE CASCADE from the practice.
    await db_tx.execute(
        "DELETE FROM public.practice_required_documents WHERE practice_id = $1", ids["practice"]
    )
    await db_tx.execute("DELETE FROM public.practices WHERE id = $1", ids["practice"])
    assert await db_tx.fetchval("SELECT count(*) FROM practice_document_requests") == 0

    _forward, rollback = MIGRATION.read_text(encoding="utf-8").split(MARK)
    await db_tx.execute(rollback)
    assert (
        await db_tx.fetchval(f"SELECT to_regclass('{SCRATCH}.practice_document_requests')") is None
    )


@pytest.mark.asyncio
async def test_a_request_creates_slot_and_request_row_consistently(db_tx) -> None:
    ids = await _world(db_tx)
    await db_tx.execute(
        "UPDATE practices SET client_notification_sent = TRUE WHERE id = $1", ids["practice"]
    )

    created = await _ask(db_tx, ids["practice"])

    (slot,) = await _slots(db_tx, ids["practice"])
    (request,) = await _request_rows(db_tx, ids["practice"])
    assert slot["id"] == created.slot["id"] == request["required_document_id"]
    assert (slot["document_type"], slot["document_label"], slot["description"]) == (
        "zz_document",
        "Zz Document",
        "What the client reads",
    )
    assert slot["is_required"] is True and slot["status"] == "pending"
    assert slot["created_by"] == "asker@example.test"
    assert request["id"] == created.request_id
    assert request["practice_type_id"] == ids["a"]
    assert request["decision"] == "pending" and request["reviewers_notified_at"] is None
    assert request["reason"] == "Why this case needs it"
    assert request["requested_by"] == "asker@example.test"
    assert (request["document_type"], request["label"]) == ("zz_document", "Zz Document")
    assert (
        await db_tx.fetchval(
            "SELECT client_notification_sent FROM practices WHERE id = $1", ids["practice"]
        )
        is False
    )


@pytest.mark.asyncio
async def test_when_the_second_insert_fails_the_slot_is_rolled_back(db_tx) -> None:
    ids = await _world(db_tx)
    await db_tx.execute(
        "UPDATE practices SET client_notification_sent = TRUE WHERE id = $1", ids["practice"]
    )

    # reason is NOT NULL: the request-row insert fails after the slot was inserted.
    with pytest.raises(asyncpg.NotNullViolationError):
        await _ask(db_tx, ids["practice"], reason=None)

    assert await _slots(db_tx, ids["practice"]) == []
    assert await _request_rows(db_tx, ids["practice"]) == []
    assert (
        await db_tx.fetchval(
            "SELECT client_notification_sent FROM practices WHERE id = $1", ids["practice"]
        )
        is True
    )


@pytest.mark.asyncio
async def test_a_duplicate_or_a_closed_practice_writes_nothing(db_tx) -> None:
    ids = await _world(db_tx)
    await _ask(db_tx, ids["practice"])
    with pytest.raises(DocumentRequestRefused) as duplicate:
        await _ask(db_tx, ids["practice"], label="Same key, other words")
    assert duplicate.value.code == "already_on_practice"
    assert len(await _slots(db_tx, ids["practice"])) == 1
    assert len(await _request_rows(db_tx, ids["practice"])) == 1

    for status in ("completed", "cancelled"):
        await db_tx.execute(
            "UPDATE practices SET status = $2 WHERE id = $1", ids["practice"], status
        )
        with pytest.raises(DocumentRequestRefused) as closed:
            await _ask(db_tx, ids["practice"], document_type="zz_other")
        assert closed.value.code == "practice_closed"
    assert len(await _slots(db_tx, ids["practice"])) == 1
    assert len(await _request_rows(db_tx, ids["practice"])) == 1


@pytest.mark.asyncio
async def test_the_90_day_count_follows_the_definition(db_tx) -> None:
    ids = await _world(db_tx)

    async def seed(type_id, document_type, days_ago):
        return await db_tx.fetchval(
            "INSERT INTO practice_document_requests (practice_id, practice_type_id, document_type, "
            "label, client_text, reason, requested_by, requested_at) "
            "VALUES ($1, $2, $3, 'Zz', 'text', 'why', 'asker@example.test', "
            "now() - make_interval(days => $4)) RETURNING id",
            ids["practice"],
            type_id,
            document_type,
            days_ago,
        )

    await seed(ids["a"], "zz_document", 1)
    await seed(ids["a"], "zz_document", 89)
    await seed(ids["a"], "zz_document", 91)  # outside the window
    await seed(ids["a"], "zz_other_document", 1)  # other document
    await seed(ids["b"], "zz_document", 1)  # other type
    await seed(None, "zz_document", 1)  # no type

    created = await _ask(db_tx, ids["practice"])  # the row just written: not counted
    assert await count_recent_requests(db_tx, ids["a"], "zz_document", created.request_id) == 2
    assert await count_recent_requests(db_tx, ids["a"], "zz_document", 0) == 3
    assert await count_recent_requests(db_tx, ids["b"], "zz_document", created.request_id) == 1
    assert await count_recent_requests(db_tx, None, "zz_document", created.request_id) == 0
