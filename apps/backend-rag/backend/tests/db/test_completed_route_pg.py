"""A practice Final delivery completed takes later documents on the completed route (step A).

Real PostgreSQL through the delivery suite's fixture; only the file sink is substituted. The practice is
completed here by writing what the first-proof completion writes (a completion row and the status), so
every case also holds for a practice a later release completes with documents still to come.
"""

import asyncio
import base64
import hashlib

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
    """The e-visa is the first proof: it completes the practice with the ITK still to come."""
    view = await service.upload(
        db, pid, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    assert view["status"] == "completed"


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
    reached, release, restore = gate_after_stage(monkeypatch)
    upload = asyncio.create_task(
        service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    )
    await reached.wait()  # the draft is staged and private; the transaction has not started
    restore()
    async with pool.acquire() as conn:
        assert await unattached_drafts(conn) == 1
    await complete_like_the_first_proof(pool)  # the e-visa, a first proof, completes the practice
    async with pool.acquire() as conn:
        before = await receipts(conn)
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


@pytest.mark.asyncio
async def test_a_delivered_practice_takes_an_upload_into_an_empty_slot_and_never_earns(pool):  # noqa: F811
    await complete_like_the_first_proof(pool)
    async with pool.acquire() as conn:
        before = await receipts(conn)
    view = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    assert view["status"] == "completed" and view["slots"][1]["document_id"] is not None
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["client_visible"] is True  # published at once
        assert await receipts(conn) == before


@pytest.mark.asyncio
async def test_a_filled_slot_of_a_completed_practice_is_409_before_anything_is_staged(pool):  # noqa: F811
    await complete_like_the_first_proof(pool)
    first = await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    live = first["slots"][1]["document_id"]
    with pytest.raises(HTTPException) as refused:
        await service.upload(pool, 7, ITK, STAFF, final_file("another-itk"), BackgroundTasks())
    assert refused.value.status_code == 409
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["document_id"] == live  # the slot is untouched
        assert (
            await unattached_drafts(conn) == 0
        )  # refused at the snapshot, as on main: nothing staged
        ids = [d["id"] for d in (await service.portal_files(conn, 7, 31))["documents"]]
    assert live in ids and len(ids) == 2  # the client sees the e-visa and the live ITK only


@pytest.mark.asyncio
async def test_a_slot_filled_and_completed_under_an_upload_leaves_its_draft_private(
    pool, monkeypatch
):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    reached, release, restore = gate_after_stage(monkeypatch)
    late = asyncio.create_task(
        service.upload(pool, 7, ITK, STAFF, final_file("late-itk"), BackgroundTasks())
    )
    await reached.wait()  # the draft is staged; the transaction has not started
    restore()
    winner = await service.upload(pool, 7, ITK, STAFF, final_file("winner-itk"), BackgroundTasks())
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    release.set()
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(late, 15)
    assert refused.value.status_code == 409
    async with pool.acquire() as conn:
        assert (await slot_of(conn))["document_id"] == winner["slots"][1]["document_id"]
        # The staged draft is private and recoverable (a retry of the same bytes adopts it), as on main.
        assert await unattached_drafts(conn) == 1
        assert await conn.fetchval(
            "SELECT client_visible IS NOT TRUE FROM documents WHERE file_name='late-itk.pdf'"
        )


@pytest.mark.asyncio
async def test_retry_of_a_first_proof_after_a_lost_response_keeps_one_receipt_and_no_lost_file(
    pool,
):  # noqa: F811
    visa = final_file("electronic-visa")
    first = await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
    doc = first["slots"][0]["document_id"]
    try:  # the retry may answer today's 409; both answers are allowed
        retry = await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
        assert retry["slots"][0]["document_id"] == doc
    except HTTPException as refused:
        assert refused.status_code == 409
    async with pool.acquire() as conn:
        assert len(await receipts(conn)) == 1  # one receipt
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=7") == 1
        ids = [d["id"] for d in (await service.portal_files(conn, 7, 31))["documents"]]
        assert ids == [doc]  # no visible duplicate for the client
        assert (await slot_of(conn, "electronic_visa"))["document_id"] == doc  # no lost file


@pytest.mark.asyncio
async def test_a_company_upload_overtaken_by_the_first_proof_keeps_its_company(pool, monkeypatch):  # noqa: F811
    # Carry 2: no other slot names a company when the completion lands under an upload sent with one.
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO companies VALUES(5)")
        await conn.execute("INSERT INTO client_company_links VALUES(5,31)")
    reached, release, restore = gate_after_stage(monkeypatch)
    late = asyncio.create_task(
        service.upload(
            pool,
            7,
            ITK,
            STAFF,
            final_file("itk").model_copy(update={"company_id": 5}),
            BackgroundTasks(),
        )
    )
    await reached.wait()
    restore()
    await complete_like_the_first_proof(pool)  # the e-visa (no company) completes the practice
    release.set()
    result = await asyncio.wait_for(late, 15)  # not "Permit destination changed"
    assert result["slots"][1]["document_id"] is not None
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT company_id FROM practice_delivery_slots WHERE practice_id=7 AND slot_key=$1",
                ITK,
            )
            == 5
        )
        assert (await slot_of(conn))["client_visible"] is True
        assert await unattached_drafts(conn) == 0


@pytest.mark.asyncio
async def test_a_company_upload_overtaken_for_an_unlinked_company_is_still_refused(
    pool, monkeypatch
):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO companies VALUES(5)")  # not linked to the client
    reached, release, restore = gate_after_stage(monkeypatch)
    late = asyncio.create_task(
        service.upload(
            pool,
            7,
            ITK,
            STAFF,
            final_file("itk").model_copy(update={"company_id": 5}),
            BackgroundTasks(),
        )
    )
    await reached.wait()
    restore()
    await complete_like_the_first_proof(pool)
    release.set()
    with pytest.raises(HTTPException, match="not linked"):
        await asyncio.wait_for(late, 15)


@pytest.mark.asyncio
async def test_a_delivered_practice_takes_an_empty_non_permit_slot(pool):  # noqa: F811
    # Carry 3: a PT PMA's Akta is not a permit; once the NIB (the first proof) completes it, the
    # Akta still attaches into its empty slot, published at once and without a second receipt.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO practice_types VALUES(20,'company_pt_pma','New Company (PT PMA)','company')"
        )
        await conn.execute(
            "INSERT INTO practices VALUES(20,31,null,$1,true,'on_process','paid',20,null,now())",
            STAFF["email"],
        )
    first = await service.upload(
        pool, 20, "document_3", STAFF, final_file("nib"), BackgroundTasks()
    )
    assert first["status"] == "completed"
    async with pool.acquire() as conn:
        before = await receipts(conn, 20)
    view = await service.upload(
        pool, 20, "document_1", STAFF, final_file("akta"), BackgroundTasks()
    )
    assert view["status"] == "completed"
    async with pool.acquire() as conn:
        assert (await slot_of(conn, "document_1", 20))["client_visible"] is True
        assert await receipts(conn, 20) == before
    assert len(before) == 1


@pytest.mark.asyncio
async def test_first_proof_retry_after_a_lost_response_on_the_completed_practice(pool):  # noqa: F811
    # Carry 1: the retry now meets a completed practice. A 409 is allowed; nothing else changes.
    visa = final_file("electronic-visa")
    first = await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
    assert first["status"] == "completed"
    doc = first["slots"][0]["document_id"]
    try:
        retry = await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
        assert retry["slots"][0]["document_id"] == doc
    except HTTPException as refused:
        assert refused.status_code == 409
    async with pool.acquire() as conn:
        assert len(await receipts(conn)) == 1
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=7") == 1
        assert (await slot_of(conn, "electronic_visa"))["document_id"] == doc
        ids = [d["id"] for d in (await service.portal_files(conn, 7, 31))["documents"]]
        assert ids == [doc]
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_completions") == 1
