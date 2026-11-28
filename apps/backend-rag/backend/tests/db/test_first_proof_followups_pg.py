"""Step B follow-ups: cache refresh on every completion, commit ordering, lock-before-read, P8.

Real PostgreSQL through the delivery suite's fixture (TEST_DATABASE_URL only).
"""

import asyncio
import contextlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.services.crm import delivery_proof_sweep as sweep
from backend.services.crm import practice_delivery as service
from backend.tests.db.test_completed_route_pg import gate_after_stage, unattached_drafts
from backend.tests.db.test_first_proof_instant_pg import ONSHORE, PMA, accept, setup
from backend.tests.db.test_first_proof_proofs_pg import old_visa_proof, written
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

NOV_2 = datetime(2026, 11, 2, 4, tzinfo=UTC)
ITK = "document_2"


@pytest.fixture(autouse=True)
def fresh_cursor(monkeypatch):
    monkeypatch.setattr(sweep, "_cursor", 0)
    monkeypatch.setattr(service, "queue_completed", lambda *a, **k: None)


# ── F-a: every first-proof completion refreshes the delivery counts ─────────────


@pytest.mark.asyncio
async def test_a_waiver_refreshes_the_counts_after_it_commits(pool, monkeypatch):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
    seen: list = []

    async def committed_state(*args, **kwargs):
        async with pool.acquire() as other:  # a second connection sees only committed rows
            seen.append(
                (
                    await other.fetchval("SELECT status FROM practices WHERE id=$1", ONSHORE),
                    await other.fetchval(
                        "SELECT waiver_reason FROM practice_delivery_slots"
                        " WHERE practice_id=$1 AND slot_key='electronic_visa'",
                        ONSHORE,
                    ),
                )
            )

    refreshed = AsyncMock(side_effect=committed_state)
    monkeypatch.setattr(service, "invalidate_delivery_cache", refreshed)
    await service.upload(
        pool, ONSHORE, "document_2", STAFF, final_file("e-itas"), BackgroundTasks()
    )
    refreshed.reset_mock()
    seen.clear()
    done = await service.waive(
        pool, ONSHORE, "electronic_visa", STAFF, "Not on this route", BackgroundTasks()
    )
    assert done["status"] == "completed"  # the waiver completed the practice
    refreshed.assert_awaited_once()
    assert refreshed.await_args.args[0]["id"] == ONSHORE
    # The refresh ran after the commit: the other connection already saw the completion and the waiver.
    assert seen == [("completed", "Not on this route")]


@pytest.mark.asyncio
async def test_an_exact_replay_that_completes_refreshes_the_counts_once_it_has_committed(  # noqa: F811
    pool,
    monkeypatch,
):
    visa = final_file("electronic-visa")
    await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
    async with pool.acquire() as conn:  # what an older machine left: the proof saved, still open
        await conn.execute("DELETE FROM practice_delivery_completions WHERE practice_id=7")
        await conn.execute("UPDATE practices SET status='on_process' WHERE id=7")
    refreshed = AsyncMock()
    monkeypatch.setattr(service, "invalidate_delivery_cache", refreshed)
    tasks = BackgroundTasks()
    again = await service.upload(pool, 7, "electronic_visa", STAFF, visa, tasks)
    assert again["status"] == "completed"
    refreshed.assert_not_awaited()  # not inside the transaction: it has not committed yet
    for task in tasks.tasks:
        await task()
    refreshed.assert_awaited_once()
    assert refreshed.await_args.args[0]["id"] == 7


@pytest.mark.asyncio
async def test_a_replay_that_changes_nothing_refreshes_nothing(pool, monkeypatch):  # noqa: F811
    visa = final_file("electronic-visa")
    await service.upload(pool, 7, "electronic_visa", STAFF, visa, BackgroundTasks())
    refreshed = AsyncMock()
    monkeypatch.setattr(service, "invalidate_delivery_cache", refreshed)
    tasks = BackgroundTasks()
    with contextlib.suppress(HTTPException):  # a completed retry may answer 409, never refresh
        await service.upload(pool, 7, "electronic_visa", STAFF, visa, tasks)
    for task in tasks.tasks:
        await task()
    refreshed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replay_into_an_open_practice_without_proof_queues_no_refresh(pool, monkeypatch):  # noqa: F811
    itk = final_file("itk")
    await service.upload(pool, 7, ITK, STAFF, itk, BackgroundTasks())  # saved, but no proof yet
    refreshed = AsyncMock()
    monkeypatch.setattr(service, "invalidate_delivery_cache", refreshed)
    tasks = BackgroundTasks()
    again = await service.upload(
        pool, 7, ITK, STAFF, itk, tasks
    )  # the exact file, an open practice
    assert again["status"] == "on_process"  # finish_on_proof found no proof and returned None
    assert tasks.tasks == []  # so no refresh was queued
    refreshed.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_sweep_refreshes_once_per_page_that_completed_something(pool, monkeypatch):  # noqa: F811
    async with pool.acquire() as conn:
        await old_visa_proof(conn, 7)
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_2)
    patterns = AsyncMock()
    monkeypatch.setattr(service, "invalidate_cache", patterns)
    ticked = await sweep.sweep_open_proofs(pool)
    assert ticked["completed"] == [7, PMA]
    seen = [c.args[0] for c in patterns.await_args_list]
    assert seen.count("zantara:crm_practices_stats:*") == 1  # the shared lists: once for the page
    assert "zantara:crm_practice:7:*" in seen and f"zantara:crm_practice:{PMA}:*" in seen
    assert seen.count("zantara:portal_documents:31:*") == 1  # one client, one refresh
    patterns.reset_mock()
    assert (await sweep.sweep_open_proofs(pool))["completed"] == []
    patterns.assert_not_awaited()  # a page that completed nothing refreshes nothing


@pytest.mark.asyncio
async def test_the_sweep_refreshes_only_after_its_page_has_committed(pool, monkeypatch):  # noqa: F811
    async with pool.acquire() as conn:
        await old_visa_proof(conn, 7)
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_2)
    seen: list = []

    async def committed_state(practices):
        async with pool.acquire() as other:  # a second connection sees only committed rows
            for p in practices:
                seen.append(
                    await other.fetchval("SELECT status FROM practices WHERE id=$1", p["id"])
                )

    monkeypatch.setattr(
        service, "invalidate_delivery_caches", AsyncMock(side_effect=committed_state)
    )
    assert (await sweep.sweep_open_proofs(pool))["completed"] == [7, PMA]
    assert seen == ["completed", "completed"]


@pytest.mark.asyncio
async def test_a_failing_sweep_refresh_neither_stops_the_notices_nor_undoes_the_completion(  # noqa: F811
    pool,
    monkeypatch,
):
    async with pool.acquire() as conn:
        await old_visa_proof(conn, 7)
    sent: list = []
    monkeypatch.setattr(
        service,
        "queue_completed",
        lambda pool, p, actor, tasks: tasks.add_task(sent.append, p["id"]),
    )

    async def broken(practices):
        raise RuntimeError("cache down")

    monkeypatch.setattr(service, "invalidate_delivery_caches", broken)
    ticked = await sweep.sweep_open_proofs(pool)  # the tick still returns
    assert ticked["completed"] == [7]
    assert sent == [7]  # the notices ran after the failed refresh
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM practices WHERE id=7") == "completed"


@pytest.mark.asyncio
async def test_invalidate_delivery_cache_clears_exactly_the_seven_patterns(monkeypatch):
    cleared = AsyncMock()
    monkeypatch.setattr(service, "invalidate_cache", cleared)
    await service.invalidate_delivery_cache({"id": 5, "client_id": 9})
    assert sorted(c.args[0] for c in cleared.await_args_list) == sorted(
        [
            "zantara:crm_practice:5:*",
            "zantara:crm_practices:*",
            "zantara:crm_practices_stats:*",
            "zantara:crm_clients_stats:*",
            "zantara:crm_practices_revenue_growth:*",
            "zantara:hr_bonuses:*",
            "zantara:portal_documents:9:*",
        ]
    )


# ── F-b (i): the notices run strictly after the page commits ────────────────────


@pytest.mark.asyncio
async def test_sweep_notices_run_after_the_page_commits(pool, monkeypatch):  # noqa: F811
    async with pool.acquire() as conn:
        await old_visa_proof(conn, 7)
    seen: list = []

    async def check():
        async with pool.acquire() as other:  # a different connection sees only committed rows
            seen.append(await other.fetchval("SELECT status FROM practices WHERE id=7"))

    monkeypatch.setattr(
        service, "queue_completed", lambda pool, p, actor, tasks: tasks.add_task(check)
    )
    assert (await sweep.sweep_open_proofs(pool))["completed"] == [7]
    assert seen == ["completed"]


# ── F-b (ii): the proof is read under the slot-document locks ───────────────────


@pytest.mark.asyncio
async def test_a_concurrent_archive_waits_for_the_first_proof_that_read_the_documents(  # noqa: F811
    pool,
    monkeypatch,
):
    """finish_on_proof locks the slot documents BEFORE it reads them: an archive arriving after the
    read cannot commit between the read and the completion write."""
    await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    async with pool.acquire() as conn:
        doc = await conn.fetchval(
            "SELECT document_id FROM practice_delivery_slots WHERE practice_id=7 AND slot_key=$1",
            ITK,
        )
    read_done, release = asyncio.Event(), asyncio.Event()
    real = service.finish

    async def paused(*args, **kwargs):
        read_done.set()  # the proof has been read; the completion is not yet written
        await asyncio.wait_for(release.wait(), 20)
        return await real(*args, **kwargs)

    monkeypatch.setattr(service, "finish", paused)
    upload = asyncio.create_task(
        service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
        )
    )
    await asyncio.wait_for(read_done.wait(), 20)

    async def archive():
        async with pool.acquire() as conn:
            await conn.execute("UPDATE documents SET is_archived=true WHERE id=$1", doc)

    archived = asyncio.create_task(archive())
    await asyncio.sleep(0.6)
    assert not archived.done()  # it waits for the transaction that read the documents
    release.set()
    done = await asyncio.wait_for(upload, 20)
    await asyncio.wait_for(archived, 20)
    assert done["status"] == "completed"
    async with pool.acquire() as conn:
        assert (await written(conn, 7))["done"] == 1


# ── F-c: a PATCH landing during the upload aborts it (P8) ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("while_waiting", [False, True])
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("client_id=32", "Practice changed during upload"),  # the subject was PATCHed
        ("assigned_to='other@example.test'", "Practice not found"),  # reassigned away from staff
    ],
)
async def test_patch_landing_during_the_upload_aborts_the_upload(  # noqa: F811
    pool,
    monkeypatch,
    while_waiting,
    change,
    message,
):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO team_members VALUES('staff-9','other@example.test','consultant',true)"
        )
    reached, release, restore = gate_after_stage(monkeypatch)
    upload = asyncio.create_task(
        service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
        )
    )
    await reached.wait()  # the draft is staged and private; the transaction has not started
    restore()
    async with pool.acquire() as conn:
        assert await unattached_drafts(conn) == 1
        if while_waiting:  # the PATCH holds the row when the upload reaches its lock
            patch = conn.transaction()
            await patch.start()
            await conn.execute(f"UPDATE practices SET {change} WHERE id=7")
            release.set()
            await asyncio.sleep(0.6)
            assert not upload.done()
            await patch.commit()
        else:
            await conn.execute(f"UPDATE practices SET {change} WHERE id=7")
            release.set()
    with pytest.raises(HTTPException, match=message) as refused:
        await asyncio.wait_for(upload, 20)
    assert refused.value.status_code in (404, 409)
    async with pool.acquire() as conn:
        row = await written(conn, 7)
        assert (row["status"], row["done"], row["receipts"], row["ledger"]) == (
            "on_process",
            0,
            0,
            0,
        )
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_slots") == 0
        assert await unattached_drafts(conn) == 1  # the draft stays private and recoverable
