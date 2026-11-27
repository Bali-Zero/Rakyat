"""The first proof completes the practice and earns (step B).

Real PostgreSQL through the delivery suite's fixture; only the file sink is substituted.
"""

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.services.crm import practice_delivery as service
from backend.tests.db.test_first_proof_instant_pg import ONSHORE, setup
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

ITK = "document_2"


async def receipts(conn, pid):
    return await conn.fetch(
        "SELECT event_kind,earned_at FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
    )


async def completions(conn, pid):
    return await conn.fetchval(
        "SELECT count(*) FROM practice_delivery_completions WHERE practice_id=$1", pid
    )


async def status_rows(conn, pid):
    return await conn.fetchval(
        "SELECT count(*) FROM practice_status_log WHERE practice_id=$1 AND new_status='completed'",
        pid,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("visa_first", [True, False])
async def test_two_slots_concurrently_one_completion_both_attached(pool, visa_first):  # noqa: F811
    visa = service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    itk = service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    results = await asyncio.gather(*((visa, itk) if visa_first else (itk, visa)))
    assert all(r["status"] in {"on_process", "completed"} for r in results)
    async with pool.acquire() as conn:
        slots = await conn.fetch(
            "SELECT slot_key,document_id FROM practice_delivery_slots WHERE practice_id=7"
        )
        assert {s["slot_key"] for s in slots if s["document_id"]} == {"electronic_visa", ITK}
        assert await conn.fetchval("SELECT status FROM practices WHERE id=7") == "completed"
        assert await completions(conn, 7) == 1
        assert [r["event_kind"] for r in await receipts(conn, 7)] == ["electronic_visa_issued"]
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        assert await status_rows(conn, 7) == 1
        assert {r["file_name"] for r in (await service.portal_files(conn, 7, 31))["documents"]} == {
            "electronic-visa.pdf",
            "itk.pdf",
        }


@pytest.mark.asyncio
async def test_the_first_proof_completes_notifies_once_and_a_later_proof_does_not_earn(pool):  # noqa: F811
    tasks = BackgroundTasks()
    result = await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), tasks
    )
    assert result["status"] == "completed" and len(tasks.tasks) == 2  # portal message + process
    async with pool.acquire() as conn:
        assert await completions(conn, 7) == 1 and await status_rows(conn, 7) == 1
        text = await conn.fetchval("SELECT description FROM timeline_events WHERE practice_id=7")
        assert "remaining documents will follow" in text  # the ITK is still to come
        before = await receipts(conn, 7)
    later = BackgroundTasks()
    await service.upload(pool, 7, ITK, STAFF, final_file("itk"), later)
    assert not later.tasks  # attaching to a completed practice does not announce a completion
    async with pool.acquire() as conn:
        assert await receipts(conn, 7) == before and await status_rows(conn, 7) == 1


@pytest.mark.asyncio
async def test_waiving_the_onshore_evisa_completes_with_the_earlier_document(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
    opened = await service.upload(
        pool, ONSHORE, "document_2", STAFF, final_file("e-itas"), BackgroundTasks()
    )
    assert opened["status"] == "on_process"  # a non-proof document leaves it open
    tasks = BackgroundTasks()
    waived = await service.waive(
        pool, ONSHORE, "electronic_visa", STAFF, "Not on this route", tasks
    )
    assert waived["status"] == "completed" and len(tasks.tasks) == 2
    async with pool.acquire() as conn:
        assert [r["event_kind"] for r in await receipts(conn, ONSHORE)] == ["service_delivered"]
        assert await completions(conn, ONSHORE) == 1


@pytest.mark.asyncio
async def test_reuse_of_the_combined_permit_completes_and_a_manpower_waiver_follows(pool):  # noqa: F811
    first = await service.upload(
        pool, 8, "document_1", STAFF, final_file("combined-permit"), BackgroundTasks()
    )
    assert first["status"] == "completed"
    doc = first["slots"][0]["document_id"]
    await service.reuse(pool, 8, "document_2", STAFF, doc, BackgroundTasks())
    waived = await service.waive(
        pool, 8, "document_3", STAFF, "No separate manpower filing", BackgroundTasks()
    )
    assert waived["slots"][2]["waiver_reason"] and waived["status"] == "completed"
    async with pool.acquire() as conn:
        assert len(await receipts(conn, 8)) == 1 and await completions(conn, 8) == 1


@pytest.mark.asyncio
async def test_a_waiver_on_a_completed_practice_is_refused_when_the_slot_is_not_conditional(pool):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    with pytest.raises(HTTPException) as refused:
        await service.waive(pool, 7, ITK, STAFF, "Pending arrival", BackgroundTasks())
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_a_custom_practice_completed_by_its_first_document_takes_the_other_titles(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO practice_types VALUES(30,'other_custom','Other','other')")
        await conn.execute(
            "INSERT INTO practices VALUES(30,31,null,$1,true,'on_process','paid',30,null,now())",
            STAFF["email"],
        )
    await service.set_titles(pool, 30, STAFF, ["Deed", "Receipt"])
    first = await service.upload(pool, 30, "custom_1", STAFF, final_file("deed"), BackgroundTasks())
    assert first["status"] == "completed"
    second = await service.upload(
        pool, 30, "custom_2", STAFF, final_file("receipt"), BackgroundTasks()
    )
    assert [s["document_id"] is not None for s in second["slots"]] == [True, True]
    async with pool.acquire() as conn:
        assert len(await receipts(conn, 30)) == 1 and await completions(conn, 30) == 1


@pytest.mark.asyncio
async def test_complete_needs_a_proof_and_then_not_every_slot(pool):  # noqa: F811
    with pytest.raises(HTTPException, match="Upload every required"):
        await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:  # a proof an older machine saved without completing
        await conn.execute(
            """INSERT INTO documents(id,practice_id,client_id,file_id,file_name,document_type,client_visible)
               VALUES(900,7,31,'f','v.pdf','electronic_visa',false)"""
        )
        await conn.execute(
            """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by)
               VALUES(7,'visa_c1_tourism','electronic_visa',900,'seed')"""
        )
    done = await service.complete(pool, 7, STAFF, BackgroundTasks())
    assert done["status"] == "completed"
