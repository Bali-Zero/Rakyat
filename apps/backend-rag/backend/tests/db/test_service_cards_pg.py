"""Service cards on a real Postgres (V1): the seeding module on the real tables, and the
396 migration executed (not parsed) against a scratch schema, compared with the reviewed list.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.services.crm.service_cards import seed_required_documents_from_card

BACKEND = Path(__file__).resolve().parents[2]
MIGRATION = BACKEND / "db" / "migrations_v2" / "396_service_cards.sql"
FIXTURE = BACKEND / "tests" / "unit" / "services" / "crm" / "fixtures" / "visa_cards_seed.json"
MARK = "-- === ROLLBACK ==="

# The legacy table is created by a Python-era migration that the SQL runner does not apply;
# create it as migration 054 does, inside the rolled-back transaction, when it is absent.
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

CODE_A, CODE_B = "zz_card_test_a", "zz_card_test_b"


async def _type(conn, code: str, active: bool = True) -> int:
    await conn.execute(PRD_DDL)
    type_id = await conn.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ($1, $1) RETURNING id", code
    )
    await conn.execute(
        "INSERT INTO service_cards (practice_type_id, block, active) VALUES ($1, 'visa', $2)",
        type_id,
        active,
    )
    return type_id


async def _doc(conn, type_id, doc_type, label, text, order, *, condition=None, active=True):
    await conn.execute(
        """INSERT INTO service_card_documents
           (practice_type_id, document_type, label, client_text, provider, condition_text,
            reuse, source, sort_order, active)
           VALUES ($1, $2, $3, $4, 'client', $5, 'fresh', 'house', $6, $7)""",
        type_id,
        doc_type,
        label,
        text,
        condition,
        order,
        active,
    )


async def _practice(conn, type_id) -> int:
    client_id = await conn.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz card client') RETURNING id"
    )
    return await conn.fetchval(
        "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id",
        client_id,
        type_id,
    )


async def _world(conn):
    a = await _type(conn, CODE_A)
    b = await _type(conn, CODE_B)
    await _doc(conn, a, "doc_one", "Label One", "Text for the client one", 1)
    await _doc(conn, a, "doc_when", "Label When", "Text when", 2, condition="only when X")
    await _doc(conn, a, "doc_off", "Label Off", "Text off", 3, active=False)
    await _doc(conn, a, "doc_two", "Label Two", "Text for the client two", 4)
    await _doc(conn, b, "doc_b", "Label B", "Text for the client B", 1)
    return a, b, await _practice(conn, a)


async def _rows(conn, practice_id):
    records = await conn.fetch(
        "SELECT * FROM practice_required_documents WHERE practice_id = $1 ORDER BY id",
        practice_id,
    )
    return [dict(r) for r in records]


def _arm(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "service_cards_seeding_codes", value)


@pytest.mark.asyncio
async def test_seeds_the_always_asked_documents_in_card_order(db_tx, monkeypatch) -> None:
    a, _b, practice = await _world(db_tx)
    _arm(monkeypatch, CODE_A)
    assert await seed_required_documents_from_card(db_tx, practice, a, "staff") == 2
    rows = await _rows(db_tx, practice)
    assert [r["document_type"] for r in rows] == ["doc_one", "doc_two"]
    assert [r["document_label"] for r in rows] == ["Label One", "Label Two"]
    assert [r["description"] for r in rows] == [
        "Text for the client one",
        "Text for the client two",
    ]
    assert all(r["is_required"] is True and r["status"] == "pending" for r in rows)
    assert all(r["team_member_notes"] is None and r["created_by"] == "staff" for r in rows)


@pytest.mark.asyncio
async def test_a_second_seeding_changes_nothing(db_tx, monkeypatch) -> None:
    a, _b, practice = await _world(db_tx)
    _arm(monkeypatch, CODE_A)
    await seed_required_documents_from_card(db_tx, practice, a, "staff")
    await db_tx.execute(
        """UPDATE practice_required_documents
           SET status = 'verified', document_label = 'KEEP', client_notes = 'x'
           WHERE practice_id = $1 AND document_type = 'doc_one'""",
        practice,
    )
    before = await _rows(db_tx, practice)
    await seed_required_documents_from_card(db_tx, practice, a, "other")
    assert await _rows(db_tx, practice) == before
    assert len(before) == 2 and before[0]["document_label"] == "KEEP"


@pytest.mark.asyncio
async def test_change_of_service_adds_the_new_documents_and_removes_none(
    db_tx, monkeypatch
) -> None:
    a, b, practice = await _world(db_tx)
    _arm(monkeypatch, f"{CODE_A},{CODE_B}")
    await seed_required_documents_from_card(db_tx, practice, a, "staff")
    await db_tx.execute(
        "UPDATE practice_required_documents SET status = 'verified' "
        "WHERE practice_id = $1 AND document_type = 'doc_one'",
        practice,
    )
    before = await _rows(db_tx, practice)
    assert await seed_required_documents_from_card(db_tx, practice, b, "staff") == 1
    after = await _rows(db_tx, practice)
    assert after[: len(before)] == before
    assert [r["document_type"] for r in after[len(before) :]] == ["doc_b"]


@pytest.mark.asyncio
async def test_inactive_card_under_star_seeds_nothing(db_tx, monkeypatch) -> None:
    a, _b, practice = await _world(db_tx)
    await db_tx.execute("UPDATE service_cards SET active = false WHERE practice_type_id = $1", a)
    _arm(monkeypatch, "*")
    assert await seed_required_documents_from_card(db_tx, practice, a, "staff") is None
    assert await _rows(db_tx, practice) == []


@pytest.mark.asyncio
async def test_executed_migration_equals_the_reviewed_list(db_tx) -> None:
    cards = json.loads(FIXTURE.read_text(encoding="utf-8"))["cards"]
    forward, rollback = MIGRATION.read_text(encoding="utf-8").split(MARK)
    await db_tx.execute("CREATE SCHEMA zz_cards_scratch")
    await db_tx.execute("SET LOCAL search_path TO zz_cards_scratch, public")
    await db_tx.execute(
        "CREATE TABLE practice_types (id serial PRIMARY KEY, code varchar UNIQUE, is_active boolean)"
    )
    await db_tx.executemany(
        "INSERT INTO zz_cards_scratch.practice_types (code, is_active) VALUES ($1, true)",
        [(c["code"],) for c in cards],
    )
    await db_tx.execute(forward)

    got_cards = {
        r["code"]: dict(r)
        for r in await db_tx.fetch(
            "SELECT pt.code, sc.active, sc.block, sc.visa_type_code, sc.stage "
            "FROM zz_cards_scratch.service_cards sc "
            "JOIN zz_cards_scratch.practice_types pt ON pt.id = sc.practice_type_id"
        )
    }
    assert len(got_cards) == 61
    assert sum(1 for r in got_cards.values() if r["active"]) == 17
    total = 0
    for card in cards:
        got = got_cards[card["code"]]
        assert (got["active"], got["block"], got["visa_type_code"], got["stage"]) == (
            card["active"],
            card["block"],
            card["visa_type_code"],
            card["stage"],
        ), card["code"]
        docs = await db_tx.fetch(
            "SELECT d.* FROM zz_cards_scratch.service_card_documents d "
            "JOIN zz_cards_scratch.practice_types pt ON pt.id = d.practice_type_id "
            "WHERE pt.code = $1 ORDER BY d.sort_order",
            card["code"],
        )
        assert len(docs) == len(card["documents"]), card["code"]
        for position, (row, want) in enumerate(zip(docs, card["documents"], strict=True), 1):
            total += 1
            for column in (
                "document_type",
                "label",
                "client_text",
                "provider",
                "condition_text",
                "reuse",
                "source",
            ):
                assert row[column] == want[column], (card["code"], position, column)
            assert row["sort_order"] == position
            assert row["active"] is True
    assert total == 220

    victim = next(c["code"] for c in cards if c["documents"])
    await db_tx.execute("DELETE FROM zz_cards_scratch.practice_types WHERE code = $1", victim)
    assert (
        await db_tx.fetchval(
            "SELECT count(*) FROM zz_cards_scratch.service_cards sc WHERE NOT EXISTS ("
            "SELECT 1 FROM zz_cards_scratch.practice_types pt WHERE pt.id = sc.practice_type_id)"
        )
        == 0
    )
    assert await db_tx.fetchval("SELECT count(*) FROM zz_cards_scratch.service_cards") == 60
    orphan_docs = await db_tx.fetchval(
        "SELECT count(*) FROM zz_cards_scratch.service_card_documents d WHERE NOT EXISTS ("
        "SELECT 1 FROM zz_cards_scratch.service_cards c WHERE c.practice_type_id = d.practice_type_id)"
    )
    assert orphan_docs == 0

    await db_tx.execute(rollback)
    assert await db_tx.fetchval("SELECT to_regclass('zz_cards_scratch.service_cards')") is None
    assert (
        await db_tx.fetchval("SELECT to_regclass('zz_cards_scratch.service_card_documents')")
        is None
    )
    assert await db_tx.fetchval("SELECT to_regclass('public.service_cards')") is not None
