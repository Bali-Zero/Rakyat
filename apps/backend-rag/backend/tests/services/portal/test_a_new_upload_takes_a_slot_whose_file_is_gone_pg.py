"""A new upload takes a Required Documents slot whose file is gone.

A slot filled by a document stayed 'uploaded' after that document was deleted, and the filler
only took a 'pending' slot: the corrected document a client uploaded next linked to nothing.
The two writers of ``required_slots`` run here against real PostgreSQL tables in a private
schema, with the foreign key the slot table has in production. The last tests run an upload
against a restore, a hard delete and another upload on separate connections, and wait for the
database to report the lock each order must take. Invented rows only.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.portal.required_slots import (
    fill_matching_pending_slot,
    mark_required_slot_uploaded,
)

_DB_URL = os.environ.get("TEST_DATABASE_URL")
PRACTICE = 700001
OTHER_PRACTICE = 700002


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

_TABLES = """
CREATE TABLE documents (
    id serial PRIMARY KEY,
    practice_id integer,
    document_type text,
    file_name text,
    status text DEFAULT 'received',
    client_visible boolean DEFAULT true,
    is_archived boolean DEFAULT false,
    deleted_at timestamptz,
    deleted_by text
);
CREATE TABLE practice_required_documents (
    id serial PRIMARY KEY,
    practice_id integer NOT NULL,
    document_type varchar(50) NOT NULL,
    document_label varchar(100) NOT NULL,
    uploaded_by_client boolean DEFAULT false,
    uploaded_file_id integer REFERENCES documents(id) ON DELETE SET NULL,
    uploaded_at timestamp,
    client_notes text,
    status varchar(20) DEFAULT 'pending',
    updated_at timestamp DEFAULT now(),
    UNIQUE (practice_id, document_type),
    CONSTRAINT valid_status CHECK (status IN ('pending', 'uploaded', 'verified', 'rejected'))
);
"""

# The ways the file of a slot goes: the client removes it, the team deletes it, the row is
# deleted outright and the foreign key leaves NULL behind.
GONE = {
    "the client removed it": "UPDATE documents SET deleted_at = now(), deleted_by = 'x' WHERE id = $1",
    "the team deleted it": "UPDATE documents SET status = 'deleted' WHERE id = $1",
    "its row no longer exists": "DELETE FROM documents WHERE id = $1",
}
# A file that is out of sight is not a file that is gone.
STILL_THERE = {
    "archived by the team": "UPDATE documents SET is_archived = true WHERE id = $1",
    "hidden from the client": "UPDATE documents SET client_visible = false WHERE id = $1",
    "untouched": "SELECT $1::int",
}


RESTORE = "UPDATE documents SET deleted_at = NULL, deleted_by = NULL WHERE id = $1"
Connect = Callable[[], Awaitable[asyncpg.Connection]]


@pytest_asyncio.fixture
async def connect() -> AsyncGenerator[Connect, None]:
    """Open as many connections as a test asks for, all on one private schema."""
    schema = f"required_slots_pg_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    opened: list[asyncpg.Connection] = []

    async def open_one() -> asyncpg.Connection:
        connection = await asyncpg.connect(_DB_URL, server_settings={"search_path": schema})
        opened.append(connection)
        return connection

    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await (await open_one()).execute(_TABLES)
            yield open_one
        finally:
            for connection in opened:
                connection.terminate()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin.close()


@pytest_asyncio.fixture
async def conn(connect: Connect) -> asyncpg.Connection:
    return await connect()


async def _waits_for_a_lock(monitor: asyncpg.Connection, waiting: asyncpg.Connection) -> None:
    """Return once the database reports that connection blocked on a lock."""
    for _ in range(200):
        if await monitor.fetchval(
            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = $1",
            waiting.get_server_pid(),
        ):
            return
        await asyncio.sleep(0.025)
    raise AssertionError("the statement never waited for a lock")


async def _document(connection: asyncpg.Connection, name: str) -> int:
    return await connection.fetchval(
        "INSERT INTO documents (practice_id, document_type, file_name) "
        "VALUES ($1, 'passport', $2) RETURNING id",
        PRACTICE,
        name,
    )


async def _slot(
    connection: asyncpg.Connection,
    *,
    status: str,
    file_id: int | None,
    document_type: str = "passport",
    practice_id: int = PRACTICE,
) -> int:
    return await connection.fetchval(
        "INSERT INTO practice_required_documents "
        "(practice_id, document_type, document_label, status, uploaded_file_id, "
        " uploaded_by_client, client_notes) "
        "VALUES ($1, $2, 'zz label', $3, $4, $5, 'zz note') RETURNING id",
        practice_id,
        document_type,
        status,
        file_id,
        file_id is not None,
    )


async def _row(connection: asyncpg.Connection, slot_id: int) -> dict[str, Any]:
    row = await connection.fetchrow(
        "SELECT status, uploaded_file_id, uploaded_by_client, uploaded_at, client_notes "
        "FROM practice_required_documents WHERE id = $1",
        slot_id,
    )
    return dict(row)


async def _fill(connection: asyncpg.Connection, document_id: int, **over: Any) -> int | None:
    args: dict[str, Any] = {"practice_id": PRACTICE, "document_type": "passport"}
    return await fill_matching_pending_slot(connection, document_id=document_id, **{**args, **over})


# ── the defect ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("way", list(GONE))
async def test_a_corrected_document_takes_the_slot_its_deleted_predecessor_held(
    conn: asyncpg.Connection, way: str
) -> None:
    first = await _document(conn, "zz-first.pdf")
    slot = await _slot(conn, status="uploaded", file_id=first)
    await conn.execute(GONE[way], first)
    corrected = await _document(conn, "zz-corrected.pdf")

    assert await _fill(conn, corrected) == slot

    row = await _row(conn, slot)
    assert row["status"] == "uploaded"
    assert row["uploaded_file_id"] == corrected
    assert row["uploaded_by_client"] is True
    assert row["uploaded_at"] is not None
    assert row["client_notes"] == "zz note"


async def test_a_slot_marked_uploaded_with_no_file_at_all_takes_the_upload(
    conn: asyncpg.Connection,
) -> None:
    slot = await _slot(conn, status="uploaded", file_id=None)
    document = await _document(conn, "zz-first.pdf")

    assert await _fill(conn, document) == slot
    assert (await _row(conn, slot))["uploaded_file_id"] == document


# ── what stays as it was ──────────────────────────────────────────────────────


@pytest.mark.parametrize("way", list(STILL_THERE))
async def test_a_slot_whose_file_is_still_there_is_not_taken(
    conn: asyncpg.Connection, way: str
) -> None:
    first = await _document(conn, "zz-first.pdf")
    slot = await _slot(conn, status="uploaded", file_id=first)
    await conn.execute(STILL_THERE[way], first)
    before = await _row(conn, slot)
    second = await _document(conn, "zz-second.pdf")

    assert await _fill(conn, second) is None
    assert await _row(conn, slot) == before


@pytest.mark.parametrize("judged", ["verified", "rejected"])
@pytest.mark.parametrize("way", list(GONE))
async def test_a_slot_the_team_has_judged_stays_as_the_team_left_it(
    conn: asyncpg.Connection, judged: str, way: str
) -> None:
    first = await _document(conn, "zz-first.pdf")
    slot = await _slot(conn, status=judged, file_id=first)
    await conn.execute(GONE[way], first)
    before = await _row(conn, slot)
    second = await _document(conn, "zz-second.pdf")

    assert await _fill(conn, second) is None
    assert await _row(conn, slot) == before


async def test_a_pending_slot_is_filled_as_before(conn: asyncpg.Connection) -> None:
    slot = await _slot(conn, status="pending", file_id=None)
    document = await _document(conn, "zz-first.pdf")

    assert await _fill(conn, document) == slot

    row = await _row(conn, slot)
    assert (row["status"], row["uploaded_file_id"]) == ("uploaded", document)


@pytest.mark.parametrize(
    "over",
    [
        {"document_type": "other"},
        {"document_type": ""},
        {"document_type": None},
        {"document_type": "kitas"},
        {"practice_id": OTHER_PRACTICE},
    ],
    ids=["untyped", "blank type", "no type", "another type", "another practice"],
)
async def test_an_upload_that_names_no_slot_of_the_practice_takes_none(
    conn: asyncpg.Connection, over: dict[str, Any]
) -> None:
    first = await _document(conn, "zz-first.pdf")
    slot = await _slot(conn, status="uploaded", file_id=first)
    await conn.execute(GONE["the client removed it"], first)
    before = await _row(conn, slot)
    second = await _document(conn, "zz-second.pdf")

    assert await _fill(conn, second, **over) is None
    assert await _row(conn, slot) == before


async def test_two_slots_of_one_type_are_both_left_alone(conn: asyncpg.Connection) -> None:
    first = await _document(conn, "zz-first.pdf")
    one = await _slot(conn, status="uploaded", file_id=first, document_type="passport")
    two = await _slot(conn, status="pending", file_id=None, document_type="Passport")
    await conn.execute(GONE["the client removed it"], first)
    second = await _document(conn, "zz-second.pdf")

    assert await _fill(conn, second) is None
    assert (await _row(conn, one))["uploaded_file_id"] == first
    assert (await _row(conn, two))["status"] == "pending"


async def test_a_slot_picked_by_hand_is_written_whatever_its_state(
    conn: asyncpg.Connection,
) -> None:
    first = await _document(conn, "zz-first.pdf")
    slot = await _slot(conn, status="verified", file_id=first)
    second = await _document(conn, "zz-second.pdf")

    assert await mark_required_slot_uploaded(conn, slot_id=slot, document_id=second, notes="zz new")

    row = await _row(conn, slot)
    assert (row["status"], row["uploaded_file_id"], row["client_notes"]) == (
        "uploaded",
        second,
        "zz new",
    )


# ── an upload against a restore, a hard delete, another upload ───────────────


async def _deleted_file_and_its_slot(setup: asyncpg.Connection) -> tuple[int, int, int]:
    first = await _document(setup, "zz-first.pdf")
    slot = await _slot(setup, status="uploaded", file_id=first)
    await setup.execute(GONE["the client removed it"], first)
    corrected = await _document(setup, "zz-corrected.pdf")
    return first, slot, corrected


async def test_a_restore_cannot_commit_between_the_verdict_and_the_write(
    connect: Connect,
) -> None:
    setup, holder, uploader, restorer, monitor = [await connect() for _ in range(5)]
    first, slot, corrected = await _deleted_file_and_its_slot(setup)
    held = holder.transaction()
    await held.start()
    await holder.execute("SELECT 1 FROM practice_required_documents WHERE id = $1 FOR UPDATE", slot)

    # The upload has judged the file gone and now waits for the slot another session holds.
    upload = asyncio.create_task(_fill(uploader, corrected))
    await _waits_for_a_lock(monitor, uploader)
    # A restore of that file must wait for the upload: it cannot slip in and be overwritten.
    restore = asyncio.create_task(restorer.execute(RESTORE, first))
    await _waits_for_a_lock(monitor, restorer)
    await held.commit()

    assert await asyncio.wait_for(upload, 10) == slot
    await asyncio.wait_for(restore, 10)
    assert (await _row(setup, slot))["uploaded_file_id"] == corrected
    assert await setup.fetchval("SELECT deleted_at FROM documents WHERE id = $1", first) is None


@pytest.mark.parametrize("outcome", ["committed", "rolled back"])
async def test_an_upload_that_arrives_during_a_restore_waits_for_its_outcome(
    connect: Connect, outcome: str
) -> None:
    setup, uploader, restorer, monitor = [await connect() for _ in range(4)]
    first, slot, corrected = await _deleted_file_and_its_slot(setup)
    restoring = restorer.transaction()
    await restoring.start()
    await restorer.execute(RESTORE, first)

    upload = asyncio.create_task(_fill(uploader, corrected))
    await _waits_for_a_lock(monitor, uploader)
    if outcome == "committed":
        await restoring.commit()
    else:
        await restoring.rollback()

    taken = await asyncio.wait_for(upload, 10)
    if outcome == "committed":
        # The file is back: its slot is not open.
        assert taken is None
        assert (await _row(setup, slot))["uploaded_file_id"] == first
    else:
        assert taken == slot
        assert (await _row(setup, slot))["uploaded_file_id"] == corrected


async def test_an_upload_that_arrives_during_a_hard_delete_takes_the_slot_after_it(
    connect: Connect,
) -> None:
    setup, uploader, deleter, monitor = [await connect() for _ in range(4)]
    first = await _document(setup, "zz-first.pdf")
    slot = await _slot(setup, status="uploaded", file_id=first)
    corrected = await _document(setup, "zz-corrected.pdf")
    deleting = deleter.transaction()
    await deleting.start()
    await deleter.execute("DELETE FROM documents WHERE id = $1", first)

    upload = asyncio.create_task(_fill(uploader, corrected))
    await _waits_for_a_lock(monitor, uploader)
    await deleting.commit()

    assert await asyncio.wait_for(upload, 10) == slot
    assert (await _row(setup, slot))["uploaded_file_id"] == corrected


@pytest.mark.parametrize("state", ["pending", "left by a deleted file"])
async def test_two_uploads_for_one_slot_fill_it_once(connect: Connect, state: str) -> None:
    setup, one, two, monitor = [await connect() for _ in range(4)]
    if state == "pending":
        slot = await _slot(setup, status="pending", file_id=None)
    else:
        _first, slot, _corrected = await _deleted_file_and_its_slot(setup)
    document_one = await _document(setup, "zz-one.pdf")
    document_two = await _document(setup, "zz-two.pdf")
    first_upload = one.transaction()
    await first_upload.start()
    assert await _fill(one, document_one) == slot

    second = asyncio.create_task(_fill(two, document_two))
    await _waits_for_a_lock(monitor, two)
    await first_upload.commit()

    assert await asyncio.wait_for(second, 10) is None
    assert (await _row(setup, slot))["uploaded_file_id"] == document_one
