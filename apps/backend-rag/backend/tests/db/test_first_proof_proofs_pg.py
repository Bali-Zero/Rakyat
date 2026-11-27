"""Step B proofs: the real listener, rollback under a fault, forced commit orders, document-lock order.

Real PostgreSQL through the delivery suite's fixture. The listener cases go through the real
practice_changed trigger and PracticeStatusListener (only the client fetch and the send are stubbed).
"""

import asyncio
import json
import time
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from backend.app.modules.crm import tax_drive_documents as tax
from backend.services.crm import delivery_proof_sweep as sweep
from backend.services.crm import practice_delivery as service
from backend.services.crm.practice_status_listener import PracticeStatusListener
from backend.tests.db.test_delivery_progress_pg import install_trigger
from backend.tests.db.test_first_proof_instant_pg import PMA, accept, setup
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

NOV_2 = datetime(2026, 11, 2, 4, tzinfo=UTC)
ITK = "document_2"


@pytest.fixture(autouse=True)
def fresh_cursor(monkeypatch):
    monkeypatch.setattr(sweep, "_cursor", 0)
    sent = []
    monkeypatch.setattr(
        service,
        "queue_completed",  # a notice is "sent" only when its task runs, after the commit
        lambda pool, p, actor, tasks: tasks.add_task(sent.append, (p["id"], actor)),
    )
    return sent


# ── (a) the sweep through the real trigger and listener ────────────────────────


async def old_visa_proof(conn, pid=7):
    await accept(conn, pid, "electronic_visa", NOV_2)
    await service.record_delivery_bonus(
        conn,
        pid,
        "visa_c1_tourism",
        STAFF["email"],
        "electronic_visa_issued",
        issued_on=date(2026, 11, 1),
        earned_at=NOV_2,
    )


async def outbox(pool):  # noqa: F811
    async with pool.acquire() as conn:
        return [
            r["payload"]
            for r in await conn.fetch(
                "SELECT payload::text FROM events_outbox WHERE channel='practice_changed' ORDER BY id"
            )
        ]


async def listener_sends(pool, payloads):  # noqa: F811
    with patch("backend.services.crm.practice_status_listener.ProcessAutomationService") as pas:
        svc = pas.return_value
        svc._fetch_practice_data = AsyncMock(
            return_value={
                "client_id": 31,
                "practice_type_name": "C1 Tourism Visa",
                "assigned_to": None,
            }
        )
        svc._fetch_client_data = AsyncMock(
            return_value={"full_name": "CLIENT", "email": "client@example.test"}
        )
        listener = PracticeStatusListener("postgres://unused", pool)
    listener._send_logged = AsyncMock()
    listener._recorded_without_notifications = AsyncMock(return_value=False)
    for payload in payloads:
        await listener._handle_notification(payload)
    return [c.args for c in listener._send_logged.await_args_list]


@pytest.mark.asyncio
async def test_sweep_of_a_visa_with_the_itk_missing_sends_exactly_one_partial_mail(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await install_trigger(conn)
        await old_visa_proof(conn)
    assert (await sweep.sweep_open_proofs(pool))["completed"] == [7]
    payloads = await outbox(pool)
    assert len(payloads) == 1
    sends = await listener_sends(pool, payloads)
    assert len(sends) == 1
    assert sends[0][0] == "completion_client"
    assert sends[0][4] == "[CLIENT] Your C1 Tourism Visa: first documents delivered"
    assert "The remaining documents will follow" in sends[0][5]
    assert "fully complete" not in sends[0][5]


@pytest.mark.asyncio
async def test_sweep_of_an_already_completed_practice_sends_nothing(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await install_trigger(conn)
        await accept(conn, 7, "electronic_visa", NOV_2)
        await accept(conn, 7, ITK, NOV_2)
    await service.complete(pool, 7, STAFF, BackgroundTasks())  # step A's Complete: one change
    for _ in range(2):
        assert (await sweep.sweep_open_proofs(pool))["completed"] == []
    payloads = await outbox(pool)
    assert len(payloads) == 1  # the sweep wrote no second status change
    sends = await listener_sends(pool, payloads)
    assert [s[4] for s in sends] == [
        "[CLIENT] ✅ Completed: Your C1 Tourism Visa Documents Are Ready"
    ]
    # A same-status change (completed to completed) is never a milestone for the listener.
    again = json.loads(payloads[0])
    again["old_status"] = "completed"
    assert await listener_sends(pool, [json.dumps(again)]) == []


# ── (b) fault injection right after the status UPDATE ──────────────────────────


def fail_after_the_status_update(monkeypatch, only=None):
    real = service.audit

    async def audit(conn, practice_id, actor, description):
        if description.startswith("Final documents delivered") and only in (None, practice_id):
            raise RuntimeError("injected after the status UPDATE")
        await real(conn, practice_id, actor, description)

    monkeypatch.setattr(service, "audit", audit)


async def written(conn, pid):
    return await conn.fetchrow(
        """SELECT (SELECT status FROM practices WHERE id=$1) AS status,
                  (SELECT count(*) FROM practice_delivery_completions WHERE practice_id=$1) AS done,
                  (SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=$1) AS receipts,
                  (SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1) AS ledger,
                  (SELECT count(*) FROM activity_log WHERE entity_id=$1
                     AND description LIKE 'Final document%') AS audit,
                  (SELECT count(*) FROM timeline_events WHERE practice_id=$1) AS timeline,
                  (SELECT count(*) FROM practice_status_log WHERE practice_id=$1) AS status_log""",
        pid,
    )


@pytest.mark.asyncio
async def test_a_fault_after_the_status_update_rolls_the_whole_upload_back(pool, monkeypatch):  # noqa: F811
    fail_after_the_status_update(monkeypatch)
    tasks = BackgroundTasks()
    with pytest.raises(RuntimeError, match="injected"):
        await service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), tasks
        )
    assert tasks.tasks == []
    async with pool.acquire() as conn:
        row = await written(conn, 7)
        assert dict(row) == {
            "status": "on_process",
            "done": 0,
            "receipts": 0,
            "ledger": 0,
            "audit": 0,
            "timeline": 0,
            "status_log": 0,
        }
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_slots") == 0


@pytest.mark.asyncio
async def test_a_fault_after_the_status_update_rolls_the_whole_sweep_page_back(  # noqa: F811
    pool,
    monkeypatch,
    fresh_cursor,
):
    async with pool.acquire() as conn:
        await old_visa_proof(conn, 7)  # an earlier candidate of the same page completes fine
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_2)  # a service_delivered proof: a receipt follows
        before = await written(conn, PMA)
        before_first = await written(conn, 7)
    fail_after_the_status_update(monkeypatch, only=PMA)
    with pytest.raises(RuntimeError, match="injected"):
        await sweep.sweep_open_proofs(pool)
    assert fresh_cursor == []  # no notice was queued
    async with pool.acquire() as conn:
        assert dict(await written(conn, PMA)) == dict(before)
        assert dict(await written(conn, 7)) == dict(before_first)  # the page is atomic
        assert before["status"] == "on_process" and before["receipts"] == 0


@pytest.mark.asyncio
async def test_a_reopened_practice_that_kept_its_completion_row_finishes_again(pool, fresh_cursor):  # noqa: F811
    await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    async with pool.acquire() as conn:  # an undo left the row and moved the status back
        await conn.execute("UPDATE practices SET status='on_process' WHERE id=7")
    ticked = await sweep.sweep_open_proofs(pool)
    assert ticked["completed"] == [7] and ticked["busy"] == []  # ON CONFLICT: no unique violation
    async with pool.acquire() as conn:
        assert (await written(conn, 7))["done"] == 1


# ── (c) two slots uploaded concurrently, both commit orders forced ─────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["electronic_visa", ITK])
async def test_two_slots_commit_in_a_forced_order_one_completion_both_attached(  # noqa: F811
    pool,
    monkeypatch,
    first,
):
    real = service.stage
    staged = {"electronic_visa": asyncio.Event(), ITK: asyncio.Event()}
    go = {"electronic_visa": asyncio.Event(), ITK: asyncio.Event()}

    async def barrier(*args, **kwargs):
        key = args[2]["key"]
        await real(*args, **kwargs)
        staged[key].set()
        await asyncio.wait_for(go[key].wait(), 20)

    monkeypatch.setattr(service, "stage", barrier)
    tasks = {
        "electronic_visa": asyncio.create_task(
            service.upload(
                pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
            )
        ),
        ITK: asyncio.create_task(
            service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
        ),
    }
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in staged.values())), 20)
    second = ITK if first == "electronic_visa" else "electronic_visa"
    go[first].set()
    await asyncio.wait_for(tasks[first], 20)  # the first commit lands before the second starts
    go[second].set()
    await asyncio.wait_for(tasks[second], 20)
    async with pool.acquire() as conn:
        filled = await conn.fetch(
            "SELECT slot_key FROM practice_delivery_slots WHERE practice_id=7 AND document_id IS NOT NULL"
        )
        assert {r["slot_key"] for r in filled} == {"electronic_visa", ITK}
        row = await written(conn, 7)
        assert (row["status"], row["done"], row["receipts"], row["ledger"], row["status_log"]) == (
            "completed",
            1,
            1,
            1,
            1,
        )
        assert row["timeline"] == 1


# ── lock order against the generic document writers ────────────────────────────


async def seed_itk_then_pause_the_first_proof(pool, monkeypatch):  # noqa: F811
    """The ITK is saved; the e-visa upload is paused inside finish(), holding every lock it takes."""
    await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    async with pool.acquire() as conn:
        doc = await conn.fetchrow(
            """SELECT d.id,d.file_id FROM documents d JOIN practice_delivery_slots s
               ON s.document_id=d.id WHERE s.practice_id=7 AND s.slot_key=$1""",
            ITK,
        )
    inside, release = asyncio.Event(), asyncio.Event()
    real = service.completion_wording

    async def paused(conn, practice_id):
        inside.set()
        await asyncio.wait_for(release.wait(), 20)
        return await real(conn, practice_id)

    monkeypatch.setattr(service, "completion_wording", paused)
    upload = asyncio.create_task(
        service.upload(
            pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
        )
    )
    await asyncio.wait_for(inside.wait(), 20)
    return doc, upload, release


async def writer(pool, kind, doc):  # noqa: F811
    if kind == "patch":  # crm_enhanced_documents: lock the row, then update it, one transaction
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchrow(
                "SELECT client_visible FROM documents WHERE id=$1 FOR UPDATE", doc["id"]
            )
            await conn.execute(
                "UPDATE documents SET file_name='patched.pdf' WHERE id=$1", doc["id"]
            )
    elif (
        kind == "archive"
    ):  # crm_enhanced_documents archive: one statement, fires the follow-up trigger
        async with pool.acquire() as conn:
            await conn.execute("UPDATE documents SET is_archived=true WHERE id=$1", doc["id"])
    elif kind == "tax_rename":  # the Drive writer's DB sync
        await tax._sync_renamed_registrations(pool, 31, [], doc["file_id"], "renamed.pdf")
    else:
        await tax._sync_trashed_registrations(pool, 31, [], doc["file_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["patch", "archive", "tax_rename", "tax_trash"])
async def test_a_document_writer_arriving_while_the_first_proof_commits_waits_then_goes(  # noqa: F811
    pool,
    monkeypatch,
    kind,
):
    doc, upload, release = await seed_itk_then_pause_the_first_proof(pool, monkeypatch)
    started = time.monotonic()
    other = asyncio.create_task(writer(pool, kind, doc))
    await asyncio.sleep(0.6)
    assert not other.done()  # it waits for the practice's transaction on the slot document
    release.set()
    result = await asyncio.wait_for(upload, 20)
    await asyncio.wait_for(other, 20)  # no deadlock
    assert result["status"] == "completed" and time.monotonic() - started < 10
    async with pool.acquire() as conn:
        assert (await written(conn, 7))["done"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["patch", "archive"])
async def test_the_first_proof_arriving_while_a_document_writer_holds_the_slot_document(  # noqa: F811
    pool,
    kind,
):
    await service.upload(pool, 7, ITK, STAFF, final_file("itk"), BackgroundTasks())
    async with pool.acquire() as conn:
        doc = await conn.fetchrow(
            """SELECT d.id,d.file_id FROM documents d JOIN practice_delivery_slots s
               ON s.document_id=d.id WHERE s.practice_id=7 AND s.slot_key=$1""",
            ITK,
        )
    async with pool.acquire() as holder:
        txn = holder.transaction()
        await txn.start()
        await holder.fetchrow("SELECT 1 FROM documents WHERE id=$1 FOR UPDATE", doc["id"])
        if kind == "archive":
            await holder.execute("UPDATE documents SET is_archived=true WHERE id=$1", doc["id"])
        else:
            await holder.execute(
                "UPDATE documents SET file_name='patched.pdf' WHERE id=$1", doc["id"]
            )
        started = time.monotonic()
        upload = asyncio.create_task(
            service.upload(
                pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
            )
        )
        await asyncio.sleep(0.6)
        assert not upload.done()  # the ordered slot-document lock waits for the writer
        await txn.commit()
    result = await asyncio.wait_for(upload, 20)  # no deadlock, no wait beyond the writer's commit
    assert result["status"] == "completed" and time.monotonic() - started < 10
