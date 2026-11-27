"""Step B money interleavings and idempotence: the sweep and the first-proof writers against HR.

Real PostgreSQL through the delivery suite's fixture (production foreign key on the ledger).
"""

import asyncio
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.services.hr import delivery_bonus as bonus
from backend.app.services.hr.hr_service import _lock_bonus_month, business_today
from backend.services.crm import delivery_proof_sweep as sweep
from backend.services.crm import practice_delivery as service
from backend.services.crm import visa_followup
from backend.tests.db.test_first_proof_instant_pg import (
    ONSHORE,
    OTHER,
    PMA,
    accept,
    assigned,
    receipt,
    setup,
    waiver,
)
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

NOV_2 = datetime(2026, 11, 2, 4, tzinfo=UTC)
NOV_1 = datetime(2026, 11, 1, 4, tzinfo=UTC)
NOV_20 = datetime(2026, 11, 20, 4, tzinfo=UTC)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(sweep, "_cursor", 0)
    monkeypatch.setattr(service, "queue_completed", lambda *a, **k: None)


async def totals(conn, pid):
    return (
        await conn.fetchval(
            "SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
        ),
        await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", pid),
    )


async def wait_for_lock_wait(pool, holder):  # noqa: F811
    async with pool.acquire() as probe:
        for _ in range(150):
            if await probe.fetchval(
                "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",
                holder.get("pid", 0),
            ):
                return
            await asyncio.sleep(0.1)
    pytest.fail("the HR connection never waited on a lock")


async def onshore_with_unbooked_receipt(conn):
    """An open practice an older machine left: e-visa receipt unbooked (no rate), e-visa waived."""
    await setup(conn, ONSHORE, ONSHORE)
    await conn.execute(
        "DELETE FROM hr_bonus_rates WHERE practice_type_code='kitas_investor_2yr_onshore'"
    )
    async with conn.transaction():
        await conn.fetchrow("SELECT id FROM practices WHERE id=$1 FOR UPDATE", ONSHORE)
        await bonus.record_delivery_bonus(
            conn,
            ONSHORE,
            "kitas_investor_2yr_onshore",
            STAFF["email"],
            "electronic_visa_issued",
            issued_on=date(2026, 11, 20),
        )
    await accept(conn, ONSHORE, "document_2", NOV_2)
    await accept(conn, ONSHORE, "document_3", NOV_20)
    await waiver(conn, ONSHORE, "electronic_visa", NOV_20)
    assert (await receipt(conn, ONSHORE))["ledger_id"] is None


@pytest.mark.asyncio
async def test_sweep_reads_the_existing_receipt_while_hr_resolves_it_blocked_on_the_practice(
    pool,  # noqa: F811
    monkeypatch,
):
    async with pool.acquire() as conn:
        await onshore_with_unbooked_receipt(conn)
    entered, hr_waiting = asyncio.Event(), asyncio.Event()
    real = service.first_proof_instant

    async def held(conn, p, policy=None):
        entered.set()  # the sweep holds the practice row lock here
        await asyncio.wait_for(hr_waiting.wait(), 15)
        return await real(conn, p, policy)

    monkeypatch.setattr(service, "first_proof_instant", held)
    swept = asyncio.create_task(sweep.sweep_open_proofs(pool))
    await asyncio.wait_for(entered.wait(), 15)
    hr_pid: dict = {}

    async def hr():
        async with pool.acquire() as conn, conn.transaction():
            hr_pid["pid"] = await conn.fetchval("SELECT pg_backend_pid()")
            return await bonus.resolve_delivery_bonus(conn, ONSHORE, 300000, "hr@example.test")

    hr_task = asyncio.create_task(hr())
    await wait_for_lock_wait(pool, hr_pid)  # HR waits on the practice key-share of its ledger row
    hr_waiting.set()
    started = time.monotonic()
    ticked = await asyncio.wait_for(swept, 15)  # no deadlock, no wait on HR
    assert time.monotonic() - started < 5 and ticked["completed"] == [ONSHORE]
    resolved = await asyncio.wait_for(hr_task, 15)
    assert resolved["status"] == "pending"
    async with pool.acquire() as conn:
        assert await totals(conn, ONSHORE) == (1, 1)
        assert (await receipt(conn, ONSHORE))["event_kind"] == "electronic_visa_issued"


@pytest.mark.asyncio
async def test_sweep_never_waits_on_an_hr_lock_of_the_receipt_row(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await onshore_with_unbooked_receipt(conn)
    hr = await pool.acquire()
    txn = hr.transaction()
    await txn.start()
    try:
        await hr.fetchrow(
            "SELECT * FROM hr_delivery_bonus_events WHERE practice_id=$1 FOR UPDATE", ONSHORE
        )
        started = time.monotonic()
        ticked = await asyncio.wait_for(sweep.sweep_open_proofs(pool), 15)
        assert time.monotonic() - started < 4  # read-before-insert: no INSERT, so no wait on HR
        assert ticked["completed"] == [ONSHORE] and ticked["busy"] == []
        resolved = await bonus.resolve_delivery_bonus(hr, ONSHORE, 300000, "hr@example.test")
        assert resolved["status"] == "pending"
    finally:
        await txn.commit()
        await pool.release(hr)
    async with pool.acquire() as conn:
        assert await totals(conn, ONSHORE) == (1, 1)


async def pma_open_with_proof(conn, at=NOV_2):
    await setup(conn, PMA, PMA)
    await accept(conn, PMA, "document_1", at)


@pytest.mark.asyncio
async def test_new_receipt_from_the_sweep_waits_for_hr_holding_the_month_then_books_once(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await pma_open_with_proof(conn)
    hr = await pool.acquire()
    txn = hr.transaction()
    await txn.start()
    await _lock_bonus_month(hr, 11, 2026, exclusive=True)  # HR is closing or checking November
    swept = asyncio.create_task(sweep.sweep_open_proofs(pool))
    await asyncio.sleep(0.8)
    assert not swept.done()  # the ledger write waits for the month lock, inside lock_timeout
    await txn.commit()
    await pool.release(hr)
    ticked = await asyncio.wait_for(swept, 15)
    assert ticked["completed"] == [PMA] and ticked["busy"] == []
    async with pool.acquire() as conn:
        assert await totals(conn, PMA) == (1, 1)
        row = await receipt(conn, PMA)
        assert row["earned_at"] == NOV_2 and row["event_kind"] == "service_delivered"


@pytest.mark.asyncio
async def test_hr_closing_the_month_waits_for_the_sweep_page_and_finds_one_receipt(  # noqa: F811
    pool,  # noqa: F811
    monkeypatch,
):
    async with pool.acquire() as conn:
        await pma_open_with_proof(conn)
    after, release = asyncio.Event(), asyncio.Event()
    real = sweep._complete_one

    async def held(conn, practice_id):
        done = await real(conn, practice_id)
        after.set()  # receipt and ledger written, the page not yet committed
        await asyncio.wait_for(release.wait(), 15)
        return done

    monkeypatch.setattr(sweep, "_complete_one", held)
    swept = asyncio.create_task(sweep.sweep_open_proofs(pool))
    await asyncio.wait_for(after.wait(), 15)

    async def close_month():
        async with pool.acquire() as conn, conn.transaction():
            await _lock_bonus_month(conn, 11, 2026, exclusive=True)
            return await conn.fetchval(
                "SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", PMA
            )

    closer = asyncio.create_task(close_month())
    await asyncio.sleep(0.5)
    assert not closer.done()  # it waits for the sweep's shared hold, no deadlock
    release.set()
    await asyncio.wait_for(swept, 15)
    assert await asyncio.wait_for(closer, 15) == 1  # and then sees the one ledger row
    async with pool.acquire() as conn:
        assert await totals(conn, PMA) == (1, 1)


@pytest.mark.asyncio
async def test_akta_archived_then_nib_after_deploy_earns_at_the_akta(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await assigned(conn, PMA, OTHER, NOV_1)
        await accept(conn, PMA, "document_1", NOV_2, archived=True)  # the Akta, archived since
        await assigned(conn, PMA, STAFF["email"], NOV_20)
    opened = await service.upload(
        pool, PMA, "document_3", STAFF, final_file("nib"), BackgroundTasks()
    )
    assert opened["status"] == "completed"
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM hr_delivery_bonus_events WHERE practice_id=$1", PMA)
        assert len(rows) == 1
        row = await receipt(conn, PMA)
        assert row["event_kind"] == "service_delivered" and row["earned_at"] == NOV_2
        assert row["employee_id"] == 2  # the assignee at the Akta, not the one at the NIB


async def watch_status_notifies(pool):  # noqa: F811
    """Stands in for migration 075's trigger: one NOTIFY per status change, on a private channel."""
    async with pool.acquire() as conn:
        await conn.execute(
            """CREATE FUNCTION test_notify_status() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN PERFORM pg_notify('stepb_status_probe', NEW.id::text); RETURN NEW; END $$;
               CREATE TRIGGER test_notify_status AFTER UPDATE OF status ON practices
               FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
               EXECUTE FUNCTION test_notify_status()"""
        )
    listener = await pool.acquire()
    got: list = []
    await listener.add_listener("stepb_status_probe", lambda *args: got.append(args[-1]))
    return listener, got


@pytest.mark.asyncio
async def test_sweep_after_a_step_a_complete_changes_nothing(pool, monkeypatch):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await assigned(conn, PMA, OTHER, NOV_1)
        await accept(conn, PMA, "document_1", NOV_2)
        for key in ("document_2", "document_3", "document_4"):
            await accept(conn, PMA, key, NOV_20)
        await assigned(conn, PMA, STAFF["email"], NOV_20)
    # The sweep picks the practice as a candidate; step A's Complete lands before it takes the lock.
    after_check, go = asyncio.Event(), asyncio.Event()
    real = sweep._has_proof

    async def checked(conn, practice_id):
        found = await real(conn, practice_id)
        after_check.set()
        await asyncio.wait_for(go.wait(), 15)
        return found

    monkeypatch.setattr(sweep, "_has_proof", checked)
    swept = asyncio.create_task(sweep.sweep_open_proofs(pool))
    await asyncio.wait_for(after_check.wait(), 15)
    await service.complete(pool, PMA, STAFF, BackgroundTasks())  # step A's Complete
    async with pool.acquire() as conn:
        before = (await receipt(conn, PMA), await totals(conn, PMA))
        marks = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM practice_status_log WHERE practice_id=$1) AS log,
                      (SELECT count(*) FROM practice_delivery_completions WHERE practice_id=$1) AS done,
                      (SELECT count(*) FROM activity_log WHERE entity_id=$1
                        AND description LIKE 'Final documents delivered%') AS audit,
                      (SELECT count(*) FROM timeline_events WHERE practice_id=$1) AS timeline""",
            PMA,
        )
    go.set()
    ticked = await asyncio.wait_for(swept, 15)
    assert ticked["completed"] == []  # refused under the lock: no longer open
    async with pool.acquire() as conn:
        after = (await receipt(conn, PMA), await totals(conn, PMA))
        again = await conn.fetchrow(
            """SELECT (SELECT count(*) FROM practice_status_log WHERE practice_id=$1) AS log,
                      (SELECT count(*) FROM practice_delivery_completions WHERE practice_id=$1) AS done,
                      (SELECT count(*) FROM activity_log WHERE entity_id=$1
                        AND description LIKE 'Final documents delivered%') AS audit,
                      (SELECT count(*) FROM timeline_events WHERE practice_id=$1) AS timeline""",
            PMA,
        )
    assert after == before and before[0]["earned_at"] == NOV_2 and before[0]["employee_id"] == 2
    assert dict(again) == dict(marks) and again["done"] == 1 and again["log"] == 1


@pytest.mark.asyncio
async def test_sweep_sends_no_notify_for_a_practice_step_a_already_completed(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_2)
        await setup(conn, 22, PMA)
        await accept(conn, 22, "document_1", NOV_2)
    listener, got = await watch_status_notifies(pool)
    try:
        await service.complete(pool, PMA, STAFF, BackgroundTasks())  # step A's Complete
        await asyncio.sleep(0.3)
        assert got == [str(PMA)]
        ticked = await sweep.sweep_open_proofs(pool)
        await asyncio.sleep(0.3)
        assert ticked["completed"] == [22]  # the control: a swept practice does notify
        assert got == [str(PMA), "22"]  # and the completed one stays silent
        await sweep.sweep_open_proofs(pool)
        await asyncio.sleep(0.3)
        assert got == [str(PMA), "22"]
    finally:
        await pool.release(listener)


async def seed_followup_started(conn, days_ago):
    today = business_today()
    await conn.execute(
        "UPDATE practice_visa_followups SET started_on=$1::date, due_on=$1::date+30 WHERE practice_id=7",
        today - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_completed_visa_keeps_postpone_escalate_confirm(pool, monkeypatch):  # noqa: F811
    # Main (27e4c6115c) gates none of the three on the practice status: each takes the practice
    # row lock and works on the follow-up record. A first proof now completes the practice, so
    # these must keep working on a completed visa.
    first = await service.upload(
        pool, 7, "electronic_visa", STAFF, final_file("electronic-visa"), BackgroundTasks()
    )
    assert first["status"] == "completed" and first["visa_followup"] is not None
    async with pool.acquire() as conn:
        await seed_followup_started(conn, 35)
    postponed = await visa_followup.postpone(pool, 7, STAFF, "Awaiting the client's arrival")
    assert postponed["postponed_at"] is not None and postponed["due_on"] == postponed[
        "started_on"
    ] + timedelta(days=60)
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE practice_visa_followups SET started_on=$1::date, due_on=$1::date+60,
               postponed_at=($1::date+40)::timestamptz WHERE practice_id=7""",
            business_today() - timedelta(days=65),
        )
    monkeypatch.setattr(visa_followup, "send_internal_email", _uncertain_send)
    with pytest.raises(HTTPException) as uncertain:
        await visa_followup.escalate(pool, 7, STAFF, "The permit is still missing")
    assert uncertain.value.status_code == 502  # accepted-or-not unknown, the record stays claimable
    monkeypatch.setattr(visa_followup, "OWNER_EMAILS", {STAFF["email"]})

    async def owner(user):
        return None

    monkeypatch.setattr(visa_followup, "require_owner", owner)
    confirmed = await visa_followup.confirm_receipt(pool, 7, STAFF, "message-ref-1")
    assert confirmed["email_state"] == "sent" and confirmed["email_receipt_confirmed_at"]
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM practices WHERE id=7") == "completed"


async def _uncertain_send(**kwargs):
    return False


@pytest.mark.asyncio
async def test_a_service_delivered_completion_never_starts_the_evisa_clock(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
    await service.upload(
        pool, ONSHORE, "document_2", STAFF, final_file("e-itas"), BackgroundTasks()
    )
    done = await service.waive(
        pool, ONSHORE, "electronic_visa", STAFF, "Not on this route", BackgroundTasks()
    )
    assert done["status"] == "completed"
    async with pool.acquire() as conn:
        assert [
            r["event_kind"]
            for r in await conn.fetch(
                "SELECT event_kind FROM hr_delivery_bonus_events WHERE practice_id=$1", ONSHORE
            )
        ] == ["service_delivered"]
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM practice_visa_followups WHERE practice_id=$1", ONSHORE
            )
            == 0
        )
