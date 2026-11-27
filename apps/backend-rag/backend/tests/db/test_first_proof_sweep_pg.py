"""The sweep completes what an older machine accepted without completing (P10).

Real PostgreSQL through the delivery suite's fixture; practices are seeded the way the older code
left them: the proof saved and its history row written, the practice still On Process.
"""

import asyncio
from datetime import UTC, date, datetime

import pytest
from fastapi import BackgroundTasks

from backend.services.crm import delivery_proof_sweep as sweep
from backend.services.crm import practice_delivery as service
from backend.tests.db.test_first_proof_instant_pg import ONSHORE, PMA, accept, setup, waiver
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

NOV_30 = datetime(2026, 11, 30, 4, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_cursor(monkeypatch):
    monkeypatch.setattr(sweep, "_cursor", 0)
    sent = []
    monkeypatch.setattr(
        service, "queue_completed", lambda pool, p, actor, tasks: sent.append((p["id"], actor))
    )
    return sent


async def old_proof(conn, pid=7, at=NOV_30):
    """The e-visa saved and its receipt written at upload, as the older code did; the practice open."""
    await accept(conn, pid, "electronic_visa", at)
    await service.record_delivery_bonus(
        conn,
        pid,
        "visa_c1_tourism",
        STAFF["email"],
        "electronic_visa_issued",
        issued_on=date(2026, 11, 28),
        earned_at=at,
    )


async def counts(conn, pid):
    return (
        await conn.fetchval("SELECT status FROM practices WHERE id=$1", pid),
        await conn.fetchval(
            "SELECT count(*) FROM practice_delivery_completions WHERE practice_id=$1", pid
        ),
        await conn.fetchval(
            "SELECT count(*) FROM practice_status_log WHERE practice_id=$1 AND new_status='completed'",
            pid,
        ),
        await conn.fetchval(
            "SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
        ),
    )


@pytest.mark.asyncio
async def test_sweep_completes_an_old_code_proof_with_no_further_request(pool, fresh_cursor):  # noqa: F811
    async with pool.acquire() as conn:
        await old_proof(conn)
    ticked = await sweep.sweep_open_proofs(pool)
    assert ticked["completed"] == [7] and ticked["busy"] == []
    async with pool.acquire() as conn:
        assert await counts(conn, 7) == ("completed", 1, 1, 1)
        row = await conn.fetchrow(
            "SELECT earned_at,event_kind FROM hr_delivery_bonus_events WHERE practice_id=7"
        )
        assert row["earned_at"] == NOV_30 and row["event_kind"] == "electronic_visa_issued"
        assert (
            await conn.fetchval(
                "SELECT performed_by FROM activity_log WHERE description LIKE 'Final documents delivered%'"
            )
            == "system:first-proof-sweep"
        )
        text = await conn.fetchval("SELECT description FROM timeline_events WHERE practice_id=7")
        assert "remaining documents will follow" in text
    assert fresh_cursor == [(7, "system:first-proof-sweep")]
    again = await sweep.sweep_open_proofs(pool)
    assert again["completed"] == [] and fresh_cursor == [(7, "system:first-proof-sweep")]


@pytest.mark.asyncio
async def test_sweep_reaches_a_proof_behind_250_non_qualifying_candidates(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO practices SELECT g,31,null,$1,true,'on_process','paid',1,null,now()
               FROM generate_series(100,349) g""",
            STAFF["email"],
        )
        for pid in range(100, 350):  # an ITK without the e-visa: a document, no proof
            await accept(conn, pid, "document_2", NOV_30, history=False)
        await conn.execute("UPDATE practices SET id=id+1000 WHERE id=7")  # the proof sorts last
        await old_proof(conn, 1007)
    first = await sweep.sweep_open_proofs(pool)
    assert first["completed"] == [1007] and first["examined"] == 251
    async with pool.acquire() as conn:
        await conn.execute("UPDATE practices SET status='on_process' WHERE id=1007")
        await conn.execute("DELETE FROM practice_delivery_completions WHERE practice_id=1007")
    sweep._cursor = 0
    pages = []
    for _ in range(3):
        pages.append(await sweep.sweep_open_proofs(pool, pages=1))
        if pages[-1]["completed"]:
            break
    assert [p["completed"] for p in pages][-1] == [1007] and len(pages) <= 2


@pytest.mark.asyncio
async def test_sweep_ignores_waivers_only_and_archived_references(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
        await waiver(conn, ONSHORE, "electronic_visa", NOV_30)  # waiver, no document: no proof
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_30, archived=True)  # an archived reference
    ticked = await sweep.sweep_open_proofs(pool)
    assert ticked["completed"] == []


@pytest.mark.asyncio
async def test_two_concurrent_sweeps_complete_once(pool, fresh_cursor):  # noqa: F811
    async with pool.acquire() as conn:
        await old_proof(conn)
    await asyncio.gather(sweep.sweep_open_proofs(pool), sweep.sweep_open_proofs(pool))
    async with pool.acquire() as conn:
        assert await counts(conn, 7) == ("completed", 1, 1, 1)
    assert fresh_cursor == [(7, "system:first-proof-sweep")]


@pytest.mark.asyncio
async def test_sweep_retries_a_locked_practice_next_cycle(pool, fresh_cursor):  # noqa: F811
    async with pool.acquire() as conn:
        await old_proof(conn)
    async with pool.acquire() as holder, holder.transaction():
        await holder.fetchval("SELECT id FROM practices WHERE id=7 FOR UPDATE")
        ticked = await asyncio.wait_for(sweep.sweep_open_proofs(pool), 20)
    assert ticked["busy"] == [7] and ticked["completed"] == []
    async with pool.acquire() as conn:
        assert (await counts(conn, 7))[0] == "on_process"
    sweep._cursor = 0
    assert (await sweep.sweep_open_proofs(pool))["completed"] == [7]


async def reopen(pool, pid):
    """What an older machine left: the proof saved, the practice still open."""
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM practice_delivery_completions WHERE practice_id=$1", pid)
        await conn.execute("UPDATE practices SET status='on_process' WHERE id=$1", pid)


@pytest.mark.asyncio
async def test_exact_retry_of_an_old_code_proof_completes_it_before_returning(pool, fresh_cursor):  # noqa: F811
    visa = final_file("electronic-visa")
    await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
    await reopen(pool, 7)
    again = await service.upload(
        pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks()
    )  # the same bytes
    assert again["status"] == "completed" and len(fresh_cursor) == 2  # announced again
    async with pool.acquire() as conn:
        assert (await counts(conn, 7))[1] == 1


@pytest.mark.asyncio
async def test_reuse_on_an_open_practice_that_holds_a_proof_completes_it(pool, fresh_cursor):  # noqa: F811
    first = await service.upload(
        pool, 8, "document_1", STAFF, final_file("combined-permit"), BackgroundTasks()
    )
    await reopen(pool, 8)
    shown = await service.reuse(
        pool, 8, "document_2", STAFF, first["slots"][0]["document_id"], BackgroundTasks()
    )
    assert shown["status"] == "completed" and len(fresh_cursor) == 2


@pytest.mark.asyncio
async def test_a_busy_practice_does_not_stop_the_next_candidate_of_the_page(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO practices VALUES(9,31,null,$1,true,'on_process','paid',1,null,now())",
            STAFF["email"],
        )
        await old_proof(conn, 7)
        await old_proof(conn, 9)
    async with pool.acquire() as holder, holder.transaction():
        await holder.fetchval("SELECT id FROM practices WHERE id=7 FOR UPDATE")
        ticked = await asyncio.wait_for(sweep.sweep_open_proofs(pool), 20)
    assert ticked["busy"] == [7] and ticked["completed"] == [9]


@pytest.mark.asyncio
async def test_a_practice_completed_while_the_sweep_waited_is_not_completed_again(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await old_proof(conn)
    async with pool.acquire() as holder:
        txn = holder.transaction()
        await txn.start()
        await holder.fetchval("SELECT id FROM practices WHERE id=7 FOR UPDATE")
        task = asyncio.create_task(sweep.sweep_open_proofs(pool))
        pid = await holder.fetchval("SELECT pg_backend_pid()")
        for _ in range(100):
            if await pool.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))", pid
            ):
                break
            await asyncio.sleep(0.05)
        await holder.execute("UPDATE practices SET status='completed' WHERE id=7")
        await txn.commit()
    ticked = await asyncio.wait_for(task, 20)
    assert ticked["completed"] == [] and ticked["busy"] == [7]
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM activity_log WHERE description LIKE 'Final documents delivered%'"
            )
            == 0
        )


@pytest.mark.asyncio
async def test_a_sweep_holding_the_advisory_lock_makes_the_other_tick_end(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await old_proof(conn)
    async with pool.acquire() as other, other.transaction():
        await other.fetchval("SELECT pg_advisory_xact_lock($1)", sweep._LOCK_KEY)
        ticked = await sweep.sweep_open_proofs(pool)
        assert ticked["examined"] == 0 and ticked["completed"] == []
    assert (await sweep.sweep_open_proofs(pool))["completed"] == [7]
