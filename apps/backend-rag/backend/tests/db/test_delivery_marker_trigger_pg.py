"""Migration 396: a delivery document whose file changes loses its delivery marker (the digest it names).

The documents PATCH handler needs Drive to run, so its dynamic `UPDATE documents SET <fields>` is run as SQL.
"""

import base64
import hashlib
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.services.crm import practice_delivery as service
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

ITK = "document_2"
MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations_v2"
    / "396_delivery_marker_follows_the_file.sql"
)


def sha(request):
    return hashlib.sha256(base64.b64decode(request.file_content)).hexdigest()


async def slot_view(conn):
    view = await service.state(conn, await service.practice(conn, 7, STAFF), STAFF)
    return next(s for s in view["slots"] if s["key"] == ITK)


async def uploaded_itk(pool):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    view = await service.upload(pool, 7, ITK, STAFF, final_file("itk-a"), BackgroundTasks())
    return view["slots"][1]["document_id"]


async def row(conn, doc):
    return await conn.fetchrow(
        "SELECT intake_idempotency_key AS key, content_hash, file_id FROM documents WHERE id=$1",
        doc,
    )


@pytest.mark.asyncio
async def test_patching_the_file_of_a_delivery_document_clears_its_digest_and_a_replay_is_not_success(
    pool,
):  # noqa: F811
    doc = await uploaded_itk(pool)
    async with pool.acquire() as conn:
        assert (await slot_view(conn))["content_sha256"] == sha(final_file("itk-a"))
        # The documents PATCH: the file_id of the same document now points at another file (B).
        await conn.execute(
            "UPDATE documents SET file_id=$2, updated_at=NOW() WHERE id=$1 AND client_id=31",
            doc,
            "drive-file-b",
        )
        changed = await row(conn, doc)
        assert changed["key"] is None and changed["content_hash"] is None
        assert (await slot_view(conn))[
            "content_sha256"
        ] is None  # the card can never confirm A here
    with pytest.raises(HTTPException) as replay:  # A is not "already there": no success is reported
        await service.upload(pool, 7, ITK, STAFF, final_file("itk-a"), BackgroundTasks())
    assert replay.value.status_code == 409
    async with pool.acquire() as conn:
        assert (await slot_view(conn))["content_sha256"] is None


@pytest.mark.asyncio
async def test_a_replacement_after_the_change_lands_with_its_own_digest(pool):  # noqa: F811
    doc = await uploaded_itk(pool)
    async with pool.acquire() as conn:
        await conn.execute("UPDATE documents SET file_id='drive-file-b' WHERE id=$1", doc)
    view = await service.upload(pool, 7, ITK, STAFF, final_file("itk-c"), BackgroundTasks())
    assert view["slots"][1]["content_sha256"] == sha(final_file("itk-c"))
    assert view["slots"][1]["document_id"] != doc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("assignment", "key_after", "hash_after"),
    [
        ("file_id='x'", None, None),
        ("file_url='https://example.test/x'", None, None),
        ("google_drive_file_url='https://example.test/x'", None, None),
        ("content_hash='recomputed'", None, "recomputed"),  # the writer's own hash is kept
        ("file_id='x', content_hash='recomputed'", None, "recomputed"),
        (
            "intake_idempotency_key='case-delivery:7:0:document_2:new', file_id='x'",
            "case-delivery:7:0:document_2:new",
            "keep",
        ),
        ("client_visible=false, updated_at=NOW()", "keep", "keep"),  # not an address: untouched
        ("file_id=file_id", "keep", "keep"),  # not a change
        (
            "file_id=NULL",
            "keep",
            "keep",
        ),  # a lost pointer is not a redirect (the repair path needs it)
        ("file_id=''", "keep", "keep"),
    ],
)
async def test_the_trigger_condition_part_by_part(pool, assignment, key_after, hash_after):  # noqa: F811
    doc = await uploaded_itk(pool)
    async with pool.acquire() as conn:
        before = await row(conn, doc)
        await conn.execute(f"UPDATE documents SET {assignment} WHERE id=$1", doc)
        after = await row(conn, doc)
    assert after["key"] == (before["key"] if key_after == "keep" else key_after)
    assert after["content_hash"] == (before["content_hash"] if hash_after == "keep" else hash_after)


@pytest.mark.asyncio
async def test_an_unmarked_document_is_untouched(pool):  # noqa: F811
    async with pool.acquire() as conn:
        doc = await conn.fetchval(
            """INSERT INTO documents(practice_id,client_id,file_id,file_name,intake_idempotency_key,content_hash)
               VALUES(7,31,'f','n.pdf','crm-upload:41','md5x') RETURNING id"""
        )
        plain = await conn.fetchval(
            "INSERT INTO documents(practice_id,client_id,file_id,content_hash) VALUES(7,31,'f2','md5y') RETURNING id"
        )
        await conn.execute("UPDATE documents SET file_id='moved' WHERE id IN ($1,$2)", doc, plain)
        assert tuple(await row(conn, doc)) == ("crm-upload:41", "md5x", "moved")
        assert tuple(await row(conn, plain)) == (None, "md5y", "moved")


@pytest.mark.asyncio
async def test_rollback_sql_runs_clean_and_removes_the_trigger(pool):  # noqa: F811
    doc = await uploaded_itk(pool)
    rollback = MIGRATION.read_text().split("-- === ROLLBACK ===")[1]
    async with pool.acquire() as conn:
        await conn.execute(rollback)
        await conn.execute(rollback)  # idempotent
        assert not await conn.fetchval(
            "SELECT 1 FROM pg_trigger WHERE tgname='documents_delivery_marker_follows_file'"
        )
        assert not await conn.fetchval(
            "SELECT 1 FROM pg_proc WHERE proname='documents_delivery_marker_follows_file'"
        )
        before = await row(conn, doc)
        await conn.execute("UPDATE documents SET file_id='moved' WHERE id=$1", doc)
        assert (await row(conn, doc))["key"] == before[
            "key"
        ]  # the marker stays: the trigger is gone
