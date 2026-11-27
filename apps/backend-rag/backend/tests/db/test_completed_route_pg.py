"""A practice Final delivery completed takes later documents on the completed route (step A).

Real PostgreSQL through the delivery suite's fixture; only the file sink is substituted. The practice is
completed here by writing what the first-proof completion writes (a completion row and the status), so
every case also holds for a practice a later release completes with documents still to come.
"""

import asyncio
import base64
import hashlib
import json

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers.crm_practice_delivery import DeliveryFile
from backend.services.crm import practice_delivery as service
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

ITK = "document_2"


def file_of(name, body=None, *, file_name=None):
    body = body if body is not None else f"%PDF-1.7\n{name}"
    return DeliveryFile(
        file_name=file_name or f"{name}.pdf",
        mime_type="application/pdf",
        file_content=base64.b64encode(body.encode()).decode(),
    )


def sha(request):
    return hashlib.sha256(base64.b64decode(request.file_content)).hexdigest()


async def complete_like_the_first_proof(db, pid=7):
    """The e-visa is in; the practice is completed with the ITK still to come."""
    await service.upload(
        db, pid, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    async with db.acquire() as conn:
        await conn.execute(
            """INSERT INTO practice_delivery_completions(practice_id,service_code,delivered_by)
               VALUES($1,'visa_c1_tourism','staff')""",
            pid,
        )
        await conn.execute(
            "UPDATE practices SET status='completed',completion_date=now() WHERE id=$1", pid
        )


def gate_after_stage(monkeypatch):
    """Let an upload stage its draft, then hold it until the test releases it."""
    real, reached, release = service.stage, asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        await real(*args, **kwargs)
        reached.set()
        await release.wait()

    monkeypatch.setattr(service, "stage", slow)
    return reached, release, lambda: monkeypatch.setattr(service, "stage", real)


async def slot_of(conn, key=ITK, pid=7):
    return await conn.fetchrow(
        """SELECT s.document_id,d.client_visible,d.file_name,d.intake_idempotency_key AS marker
           FROM practice_delivery_slots s LEFT JOIN documents d ON d.id=s.document_id
           WHERE s.practice_id=$1 AND s.slot_key=$2""",
        pid,
        key,
    )


async def unattached_drafts(conn):
    return await conn.fetchval(
        """SELECT count(*) FROM documents WHERE id NOT IN
           (SELECT document_id FROM practice_delivery_slots WHERE document_id IS NOT NULL)"""
    )


async def receipts(conn, pid=7):
    return await conn.fetch(
        "SELECT event_kind,earned_at FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
    )


@pytest.mark.asyncio
async def test_upload_overtaken_by_a_completion_attaches_on_the_completed_route(pool, monkeypatch):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    async with pool.acquire() as conn:
        before = await receipts(conn)
    reached, release, _ = gate_after_stage(monkeypatch)
    upload = asyncio.create_task(
        service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    )
    await reached.wait()  # the draft is staged and private; the transaction has not started
    async with pool.acquire() as conn:
        assert await unattached_drafts(conn) == 1
        await conn.execute(
            "INSERT INTO practice_delivery_completions(practice_id,service_code,delivered_by) VALUES(7,'visa_c1_tourism','s')"
        )
        await conn.execute(
            "UPDATE practices SET status='completed',completion_date=now() WHERE id=7"
        )
    release.set()
    result = await asyncio.wait_for(upload, 15)
    assert result["status"] == "completed" and result["slots"][1]["document_id"] is not None
    async with pool.acquire() as conn:
        slot = await slot_of(conn)
        assert slot["document_id"] == result["slots"][1]["document_id"] and slot["client_visible"]
        assert await unattached_drafts(conn) == 0
        assert await receipts(conn) == before  # a later document never earns
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM activity_log WHERE description=$1",
                f"Final document added after completion: {ITK}",
            )
            == 1
        )
        assert result["visa_followup"]["fulfilled_at"]  # the permit clock still runs


@pytest.mark.asyncio
async def test_overtaken_replacement_replaces_the_seen_document(pool, monkeypatch):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    first = await service.upload(pool, 7, ITK, STAFF, final_file("wrong-itk"), BackgroundTasks())
    seen = first["slots"][1]["document_id"]
    reached, release, _ = gate_after_stage(monkeypatch)
    upload = asyncio.create_task(
        service.upload(pool, 7, ITK, STAFF, final_file("right-itk"), BackgroundTasks())
    )
    await reached.wait()
    await service.complete(pool, 7, STAFF, BackgroundTasks())  # the practice is completed meanwhile
    release.set()
    result = await asyncio.wait_for(upload, 15)
    assert result["status"] == "completed" and result["slots"][1]["document_id"] != seen
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["file_name"] == "right-itk.pdf"
        assert (
            await conn.fetchval("SELECT client_visible FROM documents WHERE id=$1", seen) is False
        )


@pytest.mark.asyncio
async def test_replace_wrong_itk_retires_old_publishes_new(pool):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    await service.upload(pool, 7, ITK, STAFF, final_file("wrong-itk"), BackgroundTasks())
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        old = (await slot_of(conn))["document_id"]
    tasks = BackgroundTasks()
    result = await service.upload(pool, 7, ITK, STAFF, final_file("right-itk"), tasks)
    assert result["status"] == "completed" and not tasks.tasks  # no second completion message
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["file_name"] == "right-itk.pdf"
        assert await conn.fetchval("SELECT client_visible FROM documents WHERE id=$1", old) is False
        names = [d["file_name"] for d in (await service.portal_files(conn, 7, 31))["documents"]]
    assert names == ["electronic-visa.pdf", "right-itk.pdf"]


@pytest.mark.asyncio
async def test_stale_retry_after_a_replacement_is_409(pool, monkeypatch):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    await service.upload(pool, 7, ITK, STAFF, final_file("first-itk"), BackgroundTasks())
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    newer = await service.upload(pool, 7, ITK, STAFF, final_file("newer-itk"), BackgroundTasks())

    async def never(*args, **kwargs):
        raise AssertionError("a stale retry must be refused before anything is staged")

    monkeypatch.setattr(service, "stage", never)
    with pytest.raises(HTTPException) as stale:
        await service.upload(pool, 7, ITK, STAFF, final_file("first-itk"), BackgroundTasks())
    assert stale.value.status_code == 409 and "replaced by a newer" in stale.value.detail
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["document_id"] == newer["slots"][1]["document_id"]


@pytest.mark.asyncio
async def test_stale_retry_is_refused_on_an_open_practice_too(pool):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    await service.upload(pool, 7, ITK, STAFF, final_file("first-itk"), BackgroundTasks())
    await service.upload(pool, 7, ITK, STAFF, final_file("newer-itk"), BackgroundTasks())
    with pytest.raises(HTTPException, match="replaced by a newer"):
        await service.upload(pool, 7, ITK, STAFF, final_file("first-itk"), BackgroundTasks())


@pytest.mark.asyncio
async def test_replay_of_the_live_file_returns_the_view_and_writes_nothing(pool, monkeypatch):  # noqa: F811
    await complete_like_the_first_proof(pool)
    first = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    async with pool.acquire() as conn:
        audited = await conn.fetchval("SELECT count(*) FROM activity_log WHERE entity_id=7")

    async def never(*args, **kwargs):
        raise AssertionError("a replay must return the view before anything is staged")

    monkeypatch.setattr(service, "stage", never)
    again = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert again["slots"][1]["document_id"] == first["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM activity_log WHERE entity_id=7") == audited
        assert await unattached_drafts(conn) == 0


@pytest.mark.asyncio
async def test_reupload_of_an_archived_current_file_restores_without_a_second_receipt(pool):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        before = await receipts(conn)
        ledger = await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger")
        await conn.execute(
            "UPDATE documents SET is_archived=true WHERE id=$1",
            (await slot_of(conn))["document_id"],
        )
    restored = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert restored["slots"][1]["document_id"] is not None and restored["status"] == "completed"
    async with pool.acquire() as conn:
        assert await receipts(conn) == before
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == ledger


@pytest.mark.asyncio
async def test_same_name_different_bytes_b_wins_a_gets_409(pool, monkeypatch):  # noqa: F811
    await complete_like_the_first_proof(pool)
    a = file_of("a", "%PDF-1.7\nbytes of A", file_name="ITK.pdf")
    b = file_of("b", "%PDF-1.7\nbytes of B", file_name="ITK.pdf")
    reached, release, restore = gate_after_stage(monkeypatch)
    upload_a = asyncio.create_task(service.upload(pool, 7, ITK, STAFF, a, BackgroundTasks()))
    await reached.wait()
    restore()  # B is not gated
    await service.upload(pool, 7, ITK, STAFF, b, BackgroundTasks())
    release.set()
    with pytest.raises(HTTPException) as lost:
        await asyncio.wait_for(upload_a, 15)
    assert (
        lost.value.status_code == 409 and "Another operator changed this slot" in lost.value.detail
    )
    async with pool.acquire() as conn:
        assert await unattached_drafts(conn) == 1  # A's draft stays private until a retry adopts it
        a_draft = await conn.fetchval(
            "SELECT client_visible FROM documents WHERE intake_idempotency_key LIKE $1",
            f"%:{sha(a)}%",
        )
        assert a_draft is False


@pytest.mark.asyncio
async def test_empty_slot_inherits_case_company(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO companies VALUES(5),(6)")
        await conn.execute("INSERT INTO client_company_links VALUES(5,31),(6,31)")
    await service.upload(
        pool,
        7,
        "electronic_visa",
        STAFF,
        final_file("electronic-visa").model_copy(update={"company_id": 5}),
        BackgroundTasks(),
    )
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO practice_delivery_completions(practice_id,service_code,delivered_by) VALUES(7,'visa_c1_tourism','s')"
        )
        await conn.execute(
            "UPDATE practices SET status='completed',completion_date=now() WHERE id=7"
        )
    with pytest.raises(HTTPException, match="cannot change the permit's company"):
        await service.upload(
            pool,
            7,
            ITK,
            STAFF,
            final_file("itk").model_copy(update={"company_id": 6}),
            BackgroundTasks(),
        )
    result = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert result["slots"][1]["document_id"] is not None
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT company_id FROM practice_delivery_slots WHERE practice_id=7 AND slot_key=$1",
                ITK,
            )
            == 5
        )
        assert (await slot_of(conn))["marker"].startswith("case-delivery:7:5:document_2:")


@pytest.mark.asyncio
async def test_a_completed_practice_without_a_delivery_record_keeps_its_old_rules(pool):  # noqa: F811
    # Completed by a status edit before Final delivery (outside the November cohort): permits only.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE practices SET status='completed' WHERE id=7")
        await conn.execute(
            "UPDATE practice_status_log SET changed_at='2026-10-20 04:00+00' WHERE practice_id=7"
        )
    with pytest.raises(HTTPException, match="Only a missing final permit"):
        await service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
        )
    result = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert result["slots"][1]["document_id"] is not None
    async with pool.acquire() as conn:
        assert await receipts(conn) == []


# ── P7a: an exact replay vouches only for the file its acceptance recorded ─────────────────────────


async def acceptance(conn, key=ITK):
    return await conn.fetchrow(
        """SELECT changes FROM activity_log WHERE entity_id=7 AND changes ? 'file_id'
           AND changes->>'slot'=$1 ORDER BY performed_at DESC LIMIT 1""",
        key,
    )


@pytest.mark.asyncio
async def test_the_acceptance_row_records_slot_document_and_file_id(pool):  # noqa: F811
    await complete_like_the_first_proof(pool)
    view = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    doc = view["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        changes = json.loads((await acceptance(conn))["changes"])
        assert changes == {
            "slot": ITK,
            "document_id": doc,
            "file_id": await conn.fetchval("SELECT file_id FROM documents WHERE id=$1", doc),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [True, False])
async def test_a_moved_file_id_then_replay_lands_the_file_anew_and_retires_the_old_document(  # noqa: F811
    pool, completed
):
    if completed:
        await complete_like_the_first_proof(pool)
    else:
        await service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
        )
    first = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    old = first["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        receipts_before = await receipts(conn)
        # The documents PATCH: the same document now points at another file (B).
        await conn.execute(
            "UPDATE documents SET file_id='drive-file-b', updated_at=NOW() WHERE id=$1", old
        )
    again = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    new = again["slots"][1]["document_id"]
    assert new != old  # never reported as already there
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["document_id"] == new
        assert await conn.fetchval("SELECT client_visible FROM documents WHERE id=$1", old) is False
        assert (
            await conn.fetchval("SELECT client_visible FROM documents WHERE id=$1", new)
            is completed
        )
        if completed:
            ids = [d["id"] for d in (await service.portal_files(conn, 7, 31))["documents"]]
            assert ids[-1] == new and old not in ids
        assert await receipts(conn) == receipts_before  # a later document never earns twice
        changes = json.loads((await acceptance(conn))["changes"])
        assert changes["document_id"] == new
    # And now the new file is the recorded one: an exact replay is the view again.
    replay = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert replay["slots"][1]["document_id"] == new


@pytest.mark.asyncio
async def test_an_acceptance_row_without_a_file_id_never_vouches(pool):  # noqa: F811
    await complete_like_the_first_proof(pool)
    first = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    old = first["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        # What a row written before this release looks like: no changes at all.
        await conn.execute("UPDATE activity_log SET changes=NULL WHERE entity_id=7")
    again = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert again["slots"][1]["document_id"] != old
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT client_visible FROM documents WHERE id=$1", old) is False


@pytest.mark.asyncio
async def test_a_pointer_moved_between_snapshot_and_lock_is_refused_not_reported(pool, monkeypatch):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    first = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    reached, release, _ = gate_after_stage(monkeypatch)
    replay = asyncio.create_task(
        service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    )
    await reached.wait()  # the snapshot saw an unchanged file
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET file_id='drive-file-b' WHERE id=$1",
            first["slots"][1]["document_id"],
        )
    release.set()
    with pytest.raises(HTTPException, match="stored file changed"):
        await asyncio.wait_for(replay, 15)


@pytest.mark.asyncio
async def test_the_latest_acceptance_must_name_this_document_too(pool):  # noqa: F811
    await complete_like_the_first_proof(pool)
    first = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    old = first["slots"][1]["document_id"]
    async with pool.acquire() as conn:
        file_id = await conn.fetchval("SELECT file_id FROM documents WHERE id=$1", old)
        # A newer acceptance of the slot names another document (an older machine, a restore).
        await conn.execute(
            """INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,changes)
               VALUES('practice',7,'updated','seed','Final document uploaded: document_2',$1::text::jsonb)""",
            json.dumps({"slot": ITK, "document_id": old + 1000, "file_id": file_id}),
        )
    again = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert again["slots"][1]["document_id"] != old


@pytest.mark.asyncio
async def test_a_combined_permit_link_records_its_acceptance_too(pool):  # noqa: F811
    first = await service.upload(
        pool, 8, "document_1", STAFF, final_file("combined-permit"), BackgroundTasks()
    )
    doc = first["slots"][0]["document_id"]
    await service.reuse(pool, 8, "document_2", STAFF, doc)
    async with pool.acquire() as conn:
        changes = await conn.fetchval(
            """SELECT changes FROM activity_log WHERE entity_id=8
               AND description='Combined permit confirmed for slot: document_2'"""
        )
        assert json.loads(changes)["slot"] == "document_2"
        assert json.loads(changes)["document_id"] == doc
