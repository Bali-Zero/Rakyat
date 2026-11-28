"""The reviewers' side of the document requests (V2, second cut) on a real Postgres.

One-connection cases run on the real tables (``practices``, ``practice_types``,
``service_cards``, ``service_card_documents``, ``practice_document_requests``) inside a
rolled-back transaction; ``practice_required_documents`` and ``practices.client_notification_sent``
are created when absent, as backend/tests/db/test_document_requests_pg.py does (the database the
hosted job builds has neither). The two-connection cases need committed data, because a second
connection cannot see an open transaction: they run in a throwaway schema with their own pool,
copied from the ``pool`` fixture of test_kita_document_delivery_pg.py. Invented names only.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.core.config import settings
from backend.services.crm.document_requests import (
    DocumentRequestRefused,
    create_document_request,
    decide_request,
    list_requests,
    reopen_request,
)
from backend.services.crm.service_cards import seed_required_documents_from_card

BACKEND = Path(__file__).resolve().parents[2]
MIGRATION_397 = BACKEND / "db" / "migrations_v2" / "397_practice_document_requests.sql"
MARK = "-- === ROLLBACK ==="
REVIEWER = "reviewer@example.test"

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


@pytest.fixture
def armed():
    """Arm the seeder for a code list, restoring the setting afterwards."""
    missing = object()
    previous = settings.__dict__.get("service_cards_seeding_codes", missing)

    def _set(value: str) -> None:
        settings.__dict__["service_cards_seeding_codes"] = value

    yield _set
    if previous is missing:
        settings.__dict__.pop("service_cards_seeding_codes", None)
    else:
        settings.__dict__["service_cards_seeding_codes"] = previous


class World:
    def __init__(self, conn: asyncpg.Connection, client_id: int, type_a: int, type_b: int) -> None:
        self.conn = conn
        self.client_id = client_id
        self.a = type_a
        self.b = type_b
        self.code_a = "zz_rev_a"

    async def practice(self, type_id: int | None, status: str | None = None) -> int:
        practice = await self.conn.fetchval(
            "INSERT INTO practices (client_id, practice_type_id) VALUES ($1, $2) RETURNING id",
            self.client_id,
            type_id,
        )
        if status is not None:
            await self.conn.execute(
                "UPDATE practices SET status = $2 WHERE id = $1", practice, status
            )
        return practice

    async def card(self, type_id: int, active: bool = True) -> None:
        await self.conn.execute(
            "INSERT INTO service_cards (practice_type_id, block, active) VALUES ($1, 'visa', $2)",
            type_id,
            active,
        )

    async def card_row(
        self, type_id: int, document_type: str, sort_order: int, **over: object
    ) -> int:
        values = {
            "source": "official",
            "source_request_id": None,
            "condition_text": None,
            "client_text": "Official text",
        }
        values.update(over)
        return await self.conn.fetchval(
            "INSERT INTO service_card_documents (practice_type_id, document_type, label, "
            "client_text, provider, condition_text, reuse, source, source_request_id, "
            "sort_order, active, created_by) VALUES ($1, $2, 'Zz Official', $3, 'client', $4, "
            "'fresh', $5, $6, $7, true, 'seed@example.test') RETURNING id",
            type_id,
            document_type,
            values["client_text"],
            values["condition_text"],
            values["source"],
            values["source_request_id"],
            sort_order,
        )

    async def ask(self, practice: int, document_type: str = "zz_document", **over: str):
        args = {
            "practice_id": practice,
            "requested_by": "asker@example.test",
            "document_label": "Zz Document",
            "client_text": "What the client reads",
            "reason": "Why this case needs it",
            "document_type": document_type,
        }
        args.update(over)
        return await create_document_request(self.conn, **args)

    async def decide(self, request_id: int, decision: str = "added_to_card", **over):
        args = {"decision": decision, "condition": None, "client_text": None}
        args.update(over)
        return await decide_request(self.conn, request_id, reviewer=REVIEWER, **args)

    async def request_row(self, request_id: int):
        return await self.conn.fetchrow(
            "SELECT * FROM practice_document_requests WHERE id = $1", request_id
        )

    async def card_docs(self, type_id: int):
        return await self.conn.fetch(
            "SELECT * FROM service_card_documents WHERE practice_type_id = $1 ORDER BY id",
            type_id,
        )

    async def snapshot(self) -> tuple[list[str], list[str]]:
        slots = await self.conn.fetch(
            "SELECT row_to_json(t)::text AS j FROM practice_required_documents t ORDER BY id"
        )
        practices = await self.conn.fetch(
            "SELECT row_to_json(t)::text AS j FROM practices t ORDER BY id"
        )
        return [r["j"] for r in slots], [r["j"] for r in practices]


@pytest_asyncio.fixture
async def world(db_tx: asyncpg.Connection) -> World:
    await db_tx.execute(PRD_DDL)
    await db_tx.execute(COLUMN_DDL)
    type_a = await db_tx.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('zz_rev_a', 'Zz Service A') RETURNING id"
    )
    type_b = await db_tx.fetchval(
        "INSERT INTO practice_types (code, name) VALUES ('zz_rev_b', 'Zz Service B') RETURNING id"
    )
    client_id = await db_tx.fetchval(
        "INSERT INTO clients (full_name) VALUES ('zz review client') RETURNING id"
    )
    return World(db_tx, client_id, type_a, type_b)


async def _list(world: World, state: str = "pending", limit: int = 100):
    return await list_requests(world.conn, state=state, limit=limit, offset=0)


def _mine(answer: dict, *request_ids: int) -> list[dict]:
    return [item for item in answer["items"] if item["id"] in request_ids]


# ---------------------------------------------------------------- the list


@pytest.mark.asyncio
async def test_the_pending_list_leaves_out_a_cancelled_practice_and_a_withdrawn_slot(
    world: World,
) -> None:
    await world.card(world.a)
    open_practice = await world.practice(world.a)
    cancelled_practice = await world.practice(world.a)
    withdrawn_practice = await world.practice(world.a)
    kept = await world.ask(open_practice, "zz_kept")
    cancelled = await world.ask(cancelled_practice, "zz_cancelled")
    withdrawn = await world.ask(withdrawn_practice, "zz_withdrawn")
    await world.conn.execute(
        "UPDATE practices SET status = 'Cancelled' WHERE id = $1", cancelled_practice
    )
    await world.conn.execute(
        "DELETE FROM practice_required_documents WHERE id = $1", withdrawn.slot["id"]
    )

    answer = await _list(world)
    ids = {item["id"] for item in answer["items"]}
    assert kept.request_id in ids
    assert cancelled.request_id not in ids and withdrawn.request_id not in ids
    assert answer["total"] == len(answer["items"])
    paged = await list_requests(world.conn, state="pending", limit=1, offset=0)
    assert len(paged["items"]) == 1 and paged["total"] == answer["total"]

    (item,) = _mine(answer, kept.request_id)
    assert item["service_name"] == "Zz Service A" and item["practice_id"] == open_practice
    assert item["card_state"] == "can_add" and item["decision"] == "pending"
    assert item["decided_by"] is None and item["seen_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["cancelled", "Canceled", " Cancelled ", "CANCELLED\n", "\tcanceled\r\n"]
)
async def test_a_cancelled_status_is_read_as_the_first_cut_reads_it(
    world: World, status: str
) -> None:
    """The first cut closes a practice on ``status.strip().lower()``; the pending list hides the
    same practices, whatever the case and the whitespace around the word."""
    await world.card(world.a)
    practice = await world.practice(world.a)
    asked = await world.ask(practice, "zz_padded")
    await world.conn.execute("UPDATE practices SET status = $2 WHERE id = $1", practice, status)

    answer = await _list(world)
    assert asked.request_id not in {item["id"] for item in answer["items"]}


@pytest.mark.asyncio
async def test_seen_count_counts_only_earlier_requests_inside_ninety_days(world: World) -> None:
    practices = [await world.practice(world.a) for _ in range(4)]
    other_type = await world.practice(world.b)

    async def seed(practice: int, type_id: int | None, days_ago: int, document: str = "zz_doc"):
        return await world.conn.fetchval(
            "INSERT INTO practice_document_requests (practice_id, practice_type_id, "
            "document_type, label, client_text, reason, requested_by, requested_at, decision, "
            "decided_by, decided_at) VALUES ($1, $2, $3, 'Zz', 'text', 'why', "
            "'asker@example.test', now() - make_interval(days => $4), 'case_only', "
            "'reviewer@example.test', now()) RETURNING id",
            practice,
            type_id,
            document,
            days_ago,
        )

    old = await seed(practices[0], world.a, 120)  # more than 90 days before the others
    first = await seed(practices[1], world.a, 20)
    second = await seed(practices[2], world.a, 10)
    third = await seed(practices[3], world.a, 1)
    elsewhere = await seed(other_type, world.b, 5)  # another service
    other_doc = await seed(practices[1], world.a, 2, "zz_other_doc")
    untyped = await seed(practices[0], None, 3)

    answer = await _list(world, "decided")
    seen = {item["id"]: item["seen_count"] for item in answer["items"]}
    assert seen[old] == 0
    assert (seen[first], seen[second], seen[third]) == (0, 1, 2)
    assert seen[elsewhere] == 0 and seen[other_doc] == 0 and seen[untyped] == 0
    # every decided_at is now() of one transaction: ties break on the id, newest first
    mine = [i["id"] for i in answer["items"] if i["id"] in set(seen)]
    assert mine == sorted(mine, reverse=True) and len(mine) == 7


@pytest.mark.asyncio
async def test_card_state_and_card_live_follow_the_card_of_the_service(world: World, armed) -> None:
    await world.card(world.a)
    await world.card_row(world.a, "zz_on_card", 1)
    practice_a = await world.practice(world.a)
    practice_b = await world.practice(world.b)
    practice_none = await world.practice(None)
    can_add = await world.ask(practice_a, "zz_new_doc")
    on_card = await world.ask(practice_a, "zz_on_card")
    no_card = await world.ask(practice_b, "zz_new_doc")
    no_type = await world.ask(practice_none, "zz_new_doc")

    armed("zz_rev_a")
    items = {
        i["id"]: i
        for i in (await _list(world))["items"]
        if i["id"]
        in {can_add.request_id, on_card.request_id, no_card.request_id, no_type.request_id}
    }
    assert items[can_add.request_id]["card_state"] == "can_add"
    assert items[can_add.request_id]["card_live"] is True
    assert items[on_card.request_id]["card_state"] == "on_card"
    assert items[no_card.request_id]["card_state"] == "no_card"
    assert items[no_card.request_id]["card_live"] is False
    assert items[no_type.request_id]["card_state"] == "no_card"
    assert items[no_type.request_id]["service_name"] is None

    armed("")
    again = _mine(await _list(world), can_add.request_id)
    assert again[0]["card_live"] is False
    await world.conn.execute(
        "UPDATE service_cards SET active = false WHERE practice_type_id = $1", world.a
    )
    armed("*")
    assert _mine(await _list(world), can_add.request_id)[0]["card_live"] is False


# ---------------------------------------------------------------- decide


@pytest.mark.asyncio
async def test_always_writes_the_row_and_the_real_seeder_asks_it_on_a_new_practice(
    world: World, armed
) -> None:
    armed("zz_rev_a")
    await world.card(world.a)
    asked_on = await world.practice(world.a)
    created = await world.ask(asked_on, "zz_always", document_label="Zz Always")

    answer = await world.decide(created.request_id, client_text="Edited client text")
    assert answer["decision"] == "added_to_card" and answer["decided_by"] == REVIEWER
    assert answer["decision_condition"] is None

    (row,) = await world.card_docs(world.a)
    assert (row["document_type"], row["label"]) == ("zz_always", "Zz Always")
    assert row["client_text"] == "Edited client text"
    assert (row["provider"], row["reuse"], row["source"]) == ("client", "fresh", "request")
    assert row["source_request_id"] == created.request_id and row["active"] is True
    assert row["condition_text"] is None and row["created_by"] == REVIEWER
    request = await world.request_row(created.request_id)
    assert request["decision"] == "added_to_card" and request["decided_by"] == REVIEWER
    assert request["decided_at"] is not None
    assert request["client_text"] == "What the client reads"  # the request itself is not edited

    new_practice = await world.practice(world.a)
    seeded = await seed_required_documents_from_card(
        world.conn, new_practice, world.a, "creator@example.test"
    )
    assert seeded == 1
    slots = await world.conn.fetch(
        "SELECT document_type, description FROM practice_required_documents WHERE practice_id = $1",
        new_practice,
    )
    assert [(s["document_type"], s["description"]) for s in slots] == [
        ("zz_always", "Edited client text")
    ]


@pytest.mark.asyncio
async def test_only_when_writes_the_condition_and_the_seeder_does_not_ask_it(
    world: World, armed
) -> None:
    armed("zz_rev_a")
    await world.card(world.a)
    created = await world.ask(await world.practice(world.a), "zz_when")

    answer = await world.decide(created.request_id, condition="when the sponsor is a company")
    assert answer["decision_condition"] == "when the sponsor is a company"
    (row,) = await world.card_docs(world.a)
    assert row["condition_text"] == "when the sponsor is a company"
    assert row["client_text"] == "What the client reads"  # no text in the body: the request's

    new_practice = await world.practice(world.a)
    assert (
        await seed_required_documents_from_card(world.conn, new_practice, world.a, "x@example.test")
        == 0
    )
    assert (
        await world.conn.fetchval(
            "SELECT count(*) FROM practice_required_documents WHERE practice_id = $1",
            new_practice,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_sort_order_is_the_highest_plus_one_and_one_on_an_empty_card(world: World) -> None:
    await world.card(world.a)
    await world.card(world.b)
    await world.card_row(world.a, "zz_first", 1)
    await world.card_row(world.a, "zz_fifth", 5)
    on_a = await world.ask(await world.practice(world.a), "zz_added_a")
    on_b = await world.ask(await world.practice(world.b), "zz_added_b")

    await world.decide(on_a.request_id)
    await world.decide(on_b.request_id)

    added_a = [r for r in await world.card_docs(world.a) if r["document_type"] == "zz_added_a"]
    added_b = await world.card_docs(world.b)
    assert added_a[0]["sort_order"] == 6
    assert [r["sort_order"] for r in added_b] == [1]
    touched = await world.conn.fetchval(
        "SELECT updated_by FROM service_cards WHERE practice_type_id = $1", world.a
    )
    assert touched == REVIEWER


@pytest.mark.asyncio
async def test_a_document_already_on_the_card_is_refused_and_the_request_stays_pending(
    world: World,
) -> None:
    await world.card(world.a)
    first = await world.ask(await world.practice(world.a), "zz_same")
    second = await world.ask(await world.practice(world.a), "zz_same")
    await world.decide(first.request_id)

    with pytest.raises(DocumentRequestRefused) as refused:
        await world.decide(second.request_id)
    assert refused.value.code == "already_on_card"
    assert (await world.request_row(second.request_id))["decision"] == "pending"
    assert len(await world.card_docs(world.a)) == 1
    with pytest.raises(DocumentRequestRefused) as again:
        await world.decide(first.request_id, "case_only")
    assert again.value.code == "already_decided"


@pytest.mark.asyncio
async def test_the_refusals_leave_nothing_behind(world: World) -> None:
    await world.card(world.a)
    no_type = await world.ask(await world.practice(None), "zz_no_type")
    no_card = await world.ask(await world.practice(world.b), "zz_no_card")
    for request_id in (no_type.request_id, no_card.request_id):
        with pytest.raises(DocumentRequestRefused) as refused:
            await world.decide(request_id)
        assert refused.value.code == "no_card"
        assert (await world.request_row(request_id))["decision"] == "pending"
    assert (
        await world.conn.fetchval(
            "SELECT count(*) FROM service_cards WHERE practice_type_id = $1", world.b
        )
        == 0
    )
    with pytest.raises(DocumentRequestRefused) as missing:
        await world.decide(987654321)
    assert missing.value.code == "request_not_found"
    with pytest.raises(DocumentRequestRefused) as not_decided:
        await reopen_request(world.conn, no_type.request_id, reviewer=REVIEWER)
    assert not_decided.value.code == "not_decided"

    only_case = await world.decide(no_type.request_id, "case_only")
    assert only_case["decision"] == "case_only"
    assert await world.card_docs(world.a) == []
    decided = _mine(await _list(world, "decided"), no_type.request_id)
    assert decided[0]["decision"] == "case_only" and decided[0]["decided_by"] == REVIEWER


# ---------------------------------------------------------------- reopen


@pytest.mark.asyncio
async def test_reopen_deletes_exactly_the_row_of_its_request(world: World) -> None:
    await world.card(world.a)
    official = await world.card_row(world.a, "zz_official", 1)
    mine = await world.ask(await world.practice(world.a), "zz_mine")
    other = await world.ask(await world.practice(world.a), "zz_other")
    await world.decide(mine.request_id, condition="when X")
    await world.decide(other.request_id)

    answer = await reopen_request(world.conn, mine.request_id, reviewer=REVIEWER)
    assert answer == {"id": mine.request_id, "decision": "pending", "card_document_removed": True}
    left = {r["document_type"]: r for r in await world.card_docs(world.a)}
    assert set(left) == {"zz_official", "zz_other"}
    assert left["zz_official"]["id"] == official
    assert left["zz_other"]["source_request_id"] == other.request_id
    request = await world.request_row(mine.request_id)
    assert request["decision"] == "pending" and request["decision_condition"] is None
    assert request["decided_by"] is None and request["decided_at"] is None
    assert (await world.request_row(other.request_id))["decision"] == "added_to_card"

    # a card row of the same document that another route made is not this request's to delete
    again = await world.decide(mine.request_id, "case_only")
    assert again["decision"] == "case_only"
    case_only = await reopen_request(world.conn, mine.request_id, reviewer=REVIEWER)
    assert case_only["card_document_removed"] is False
    assert len(await world.card_docs(world.a)) == 2


@pytest.mark.asyncio
async def test_decide_and_reopen_never_change_a_practice_or_a_slot(world: World, armed) -> None:
    armed("zz_rev_a")
    await world.card(world.a)
    first = await world.ask(await world.practice(world.a), "zz_one")
    second = await world.ask(await world.practice(world.a), "zz_two")
    before = await world.snapshot()

    await world.decide(first.request_id, client_text="Edited")
    await world.decide(second.request_id, "case_only")
    await reopen_request(world.conn, first.request_id, reviewer=REVIEWER)
    await reopen_request(world.conn, second.request_id, reviewer=REVIEWER)

    assert await world.snapshot() == before


# ---------------------------------------------------------------- two connections


SCHEMA_SQL = """
CREATE TABLE practices(id int primary key);
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
INSERT INTO practices VALUES (1);
INSERT INTO service_cards (practice_type_id, active) VALUES (10, true);
"""


@pytest_asyncio.fixture
async def pool():
    dsn = os.environ.get("TEST_DATABASE_URL", "")
    url = urlsplit(dsn)
    if not dsn or url.hostname not in {"localhost", "127.0.0.1", "::1"} or "test" not in url.path:
        pytest.skip("Requires a local test database, provided by the backend CI shard")
    schema = "docreq_review_" + uuid4().hex
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


async def _committed_request(db: asyncpg.Pool, document_type: str, decision: str = "pending"):
    async with db.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO practice_document_requests (practice_id, practice_type_id, "
            "document_type, label, client_text, reason, requested_by, decision) "
            "VALUES (1, 10, $1, 'Zz Label', 'text', 'why', 'asker@example.test', $2) RETURNING id",
            document_type,
            decision,
        )


async def _decide(db: asyncpg.Pool, request_id: int, decision: str = "added_to_card"):
    async with db.acquire() as conn:
        return await decide_request(
            conn, request_id, decision=decision, condition=None, client_text=None, reviewer=REVIEWER
        )


async def _reopen(db: asyncpg.Pool, request_id: int):
    async with db.acquire() as conn:
        return await reopen_request(conn, request_id, reviewer=REVIEWER)


@pytest.mark.asyncio
async def test_two_decisions_on_one_request_exactly_one_succeeds(pool) -> None:
    for attempt in range(5):
        request_id = await _committed_request(pool, f"zz_race_{attempt}")
        results = await asyncio.gather(
            _decide(pool, request_id, "added_to_card"),
            _decide(pool, request_id, "case_only"),
            return_exceptions=True,
        )
        won = [r for r in results if isinstance(r, dict)]
        lost = [r for r in results if isinstance(r, DocumentRequestRefused)]
        assert len(won) == 1 and len(lost) == 1, results
        assert lost[0].code == "already_decided"
        async with pool.acquire() as conn:
            decision = await conn.fetchval(
                "SELECT decision FROM practice_document_requests WHERE id = $1", request_id
            )
            rows = await conn.fetchval(
                "SELECT count(*) FROM service_card_documents WHERE source_request_id = $1",
                request_id,
            )
        assert decision == won[0]["decision"]
        assert rows == (1 if decision == "added_to_card" else 0)


@pytest.mark.asyncio
async def test_an_add_racing_the_reopen_of_the_request_that_put_the_document_on_the_card(
    pool,
) -> None:
    for attempt in range(8):
        document = f"zz_swap_{attempt}"
        first = await _committed_request(pool, document)
        second = await _committed_request(pool, document)
        await _decide(pool, first)  # the document is on the card because of `first`

        results = await asyncio.wait_for(
            asyncio.gather(_reopen(pool, first), _decide(pool, second), return_exceptions=True),
            timeout=20,
        )
        for result in results:
            assert isinstance(result, (dict, DocumentRequestRefused)), repr(result)

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT source_request_id FROM service_card_documents WHERE document_type = $1",
                document,
            )
            decisions = {
                r["id"]: r["decision"]
                for r in await conn.fetch(
                    "SELECT id, decision FROM practice_document_requests WHERE id = ANY($1)",
                    [first, second],
                )
            }
        assert len(rows) <= 1
        holders = {r["source_request_id"] for r in rows}
        for request_id, decision in decisions.items():
            assert (decision == "added_to_card") == (request_id in holders), (decisions, holders)
