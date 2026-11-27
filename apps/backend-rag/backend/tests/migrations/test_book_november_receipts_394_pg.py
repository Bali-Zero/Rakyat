"""Migration 394, effect 2: the November receipts the paid salary left unbooked are booked as pending.

Runs the real migration (forward, guards, locks, rollback) on an isolated PostgreSQL schema built by the
delivery suite's fixture (migrations 383-388 and 393). No runtime delivery code except the concurrent
resolve in P7. Effect 1 (rows 645-649) is tested in test_book_november_paid_bonuses_394_pg.py.
"""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr.delivery_bonus import resolve_delivery_bonus
from backend.db import migration_base
from backend.db.migration_base import split_migration_sql
from backend.tests.db.test_kita_document_delivery_pg import pool  # noqa: F401

PATH = Path(__file__).resolve().parents[2] / "db/migrations_v2/394_book_november_bonus_receipts.sql"
FORWARD, ROLLBACK = split_migration_sql(PATH.read_text())
MARKER = "Booked by migration 394 into the November 2026 bonus month"
NOV = datetime(2026, 11, 20, 4, tzinfo=UTC)


async def receipt(
    conn,
    pid,
    *,
    kind="service_delivered",
    pstatus="completed",
    amount=15000,
    awarded=NOV,
    employee=1,
    forfeited=False,
    ineligible=False,
    booked_by=None,
):
    """One practice plus its receipt. booked_by: an employee id whose ledger row is written first."""
    await conn.execute(
        """INSERT INTO practices(id,client_id,assigned_to,client_visible,status,payment_status,
               practice_type_id,updated_at) VALUES($1,31,'consultant@example.test',true,$2,'paid',1,now())""",
        pid,
        pstatus,
    )
    ledger = None
    if booked_by is not None:
        ledger = await conn.fetchval(
            """INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,bonus_rate_id,
                   amount_idr,awarded_at,status) VALUES($1,$2,'visa_c1_tourism',1,$3,$4,'pending') RETURNING id""",
            pid,
            booked_by,
            amount,
            awarded,
        )
    receipt_ledger = ledger if booked_by == employee and employee is not None else None
    if forfeited or ineligible:
        amount = None
    await conn.execute(
        """INSERT INTO hr_delivery_bonus_events(practice_id,employee_id,original_team_member_id,
               payroll_ineligible,service_code,event_kind,earned_at,awarded_at,bonus_rate_id,amount_idr,
               ledger_id,forfeited)
           VALUES($1,$2,NULL,$3,'visa_c1_tourism',$4,$5,$5,1,$6,$7,$8)""",
        pid,
        None if ineligible else employee,
        ineligible,
        kind,
        awarded,
        amount,
        receipt_ledger,
        forfeited,
    )


async def seed_classes(conn):
    await conn.execute("INSERT INTO hr_employees VALUES(2,'staff-1',true,'{}'::jsonb)")
    await receipt(conn, 101)  # (a) priced service on a completed practice
    await receipt(conn, 102, kind="electronic_visa_issued", pstatus="on_process")  # (b) e-visa
    await receipt(conn, 103, forfeited=True)  # (c)
    await receipt(conn, 104, amount=0)  # (d)
    await receipt(conn, 105, amount=None)  # (e)
    await receipt(conn, 106, employee=None)  # (f)
    await receipt(conn, 107, ineligible=True)  # (g)
    await receipt(conn, 108, booked_by=1)  # (h) already booked
    await receipt(conn, 109, awarded=datetime(2026, 10, 20, 4, tzinfo=UTC))  # (i) October
    await receipt(conn, 110, awarded=datetime(2026, 12, 20, 4, tzinfo=UTC))  # (j) December


async def booked(conn):
    return {
        r["practice_id"]: r
        for r in await conn.fetch("SELECT * FROM hr_bonus_ledger WHERE notes = $1", MARKER)
    }


@pytest.mark.asyncio
async def test_p1_books_each_bookable_class_and_leaves_the_rest(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await seed_classes(conn)
        before = await conn.fetch("SELECT * FROM hr_delivery_bonus_events ORDER BY practice_id")
        await conn.execute(FORWARD)
        rows = await booked(conn)
        assert set(rows) == {101, 102}
        for pid, row in rows.items():
            event = await conn.fetchrow(
                "SELECT * FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
            )
            assert event["ledger_id"] == row["id"]
            assert row["status"] == "pending" and row["payroll_period_id"] is None
            assert (row["employee_id"], row["practice_type_code"], row["bonus_rate_id"]) == (
                event["employee_id"],
                event["service_code"],
                event["bonus_rate_id"],
            )
            assert row["amount_idr"] == event["amount_idr"] == 15000
            assert row["awarded_at"] == event["awarded_at"] == NOV
        after = await conn.fetch("SELECT * FROM hr_delivery_bonus_events ORDER BY practice_id")
        untouched = [(b, a) for b, a in zip(before, after, strict=True) if b["practice_id"] > 102]
        assert untouched and all(dict(b) == dict(a) for b, a in untouched)
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 3
        )  # 101, 102, and (h) 108
        assert (
            await conn.fetchval("SELECT notes FROM hr_bonus_ledger WHERE practice_id=108") is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("awarded", "expected"),
    [
        ("2026-10-31T15:59:59+00:00", False),
        ("2026-10-31T16:00:00+00:00", True),
        ("2026-11-30T15:59:59+00:00", True),
        ("2026-11-30T16:00:00+00:00", False),
    ],
)
async def test_p2_the_month_is_the_bali_calendar_month(pool, awarded, expected):  # noqa: F811
    async with pool.acquire() as conn:
        await receipt(conn, 120, awarded=datetime.fromisoformat(awarded))
        await conn.execute(FORWARD)
        assert (120 in await booked(conn)) is expected


@pytest.mark.asyncio
async def test_p3_a_changed_practice_is_skipped_while_the_others_commit(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO hr_employees VALUES(2,'staff-1',true,'{}'::jsonb)")
        await receipt(conn, 130, pstatus="on_process")  # reopened after the receipt
        await receipt(conn, 131, booked_by=2)  # a ledger row exists under another employee
        await receipt(conn, 132)
        await conn.execute(FORWARD)
        assert set(await booked(conn)) == {132}
        assert (
            await conn.fetchval(
                "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=130"
            )
            is None
        )
        assert (
            await conn.fetchval(
                "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=131"
            )
            is None
        )


@pytest.mark.asyncio
async def test_p4_a_second_run_books_nothing_and_skips_what_runtime_booked(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await receipt(conn, 140)
        await receipt(conn, 141)
        await receipt(conn, 142)
        # 142 is booked first through the runtime resolve path: no marker.
        async with conn.transaction():
            await resolve_delivery_bonus(conn, 142, 15000, "hr-reviewer")
        await conn.execute(FORWARD)
        first = await conn.fetch("SELECT id, practice_id FROM hr_bonus_ledger ORDER BY id")
        assert set(await booked(conn)) == {140, 141}
        await conn.execute(FORWARD)
        assert await conn.fetch("SELECT id, practice_id FROM hr_bonus_ledger ORDER BY id") == first
        assert (
            await conn.fetchval("SELECT notes FROM hr_bonus_ledger WHERE practice_id=142") is None
        )


@pytest.mark.asyncio
async def test_p5_nothing_is_booked_into_a_closed_bonus_month(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await receipt(conn, 150)
        await conn.execute(
            "INSERT INTO hr_bonus_runs(bonus_month, status) VALUES ('2026-11-01','approved')"
        )
        await conn.execute(FORWARD)
        assert await booked(conn) == {}
        assert (
            await conn.fetchval(
                "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=150"
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["receipt", "practice", "month"])
async def test_p6_it_never_waits_and_books_after_release(pool, held):  # noqa: F811
    async with pool.acquire() as setup:
        await receipt(setup, 160)
    async with pool.acquire() as holder, pool.acquire() as migrator:
        tx = holder.transaction()
        await tx.start()
        if held == "receipt":
            await holder.execute(
                "SELECT 1 FROM hr_delivery_bonus_events WHERE practice_id=160 FOR UPDATE"
            )
        elif held == "practice":
            await holder.execute(
                "SELECT 1 FROM practices WHERE id=160 FOR UPDATE"
            )  # confirm_payment shape
        else:
            await holder.execute("SELECT pg_advisory_xact_lock(hashtext('hr_bonus_month:2026-11'))")
        started = time.monotonic()
        with pytest.raises(asyncpg.LockNotAvailableError) as refused:
            await asyncio.wait_for(migrator.execute(FORWARD), 5)
        assert not isinstance(refused.value, asyncpg.DeadlockDetectedError)
        assert time.monotonic() - started < 1
        assert await booked(migrator) == {}
        await tx.commit()
        await migrator.execute(FORWARD)
        assert set(await booked(migrator)) == {160}


@pytest.mark.asyncio
async def test_p6_through_the_real_runner_retries_until_the_lock_is_released(pool, monkeypatch):  # noqa: F811
    monkeypatch.setattr(migration_base, "LOCK_RETRY_BACKOFF_BASE_SECONDS", 0.05)
    async with pool.acquire() as setup:
        await receipt(setup, 161)
    attempts = []
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("SELECT 1 FROM practices WHERE id=161 FOR UPDATE")

        async def release():
            await asyncio.sleep(1)
            await tx.commit()

        async def attempt():
            attempts.append(1)
            await migrator.execute(FORWARD)
            return True

        freed = asyncio.create_task(release())
        assert await migration_base.run_with_lock_retry(migrator, "394", attempt) is True
        await freed
        assert len(attempts) >= 2
        assert set(await booked(migrator)) == {161}


@pytest.mark.asyncio
async def test_p7_it_holds_its_facts_until_commit(pool):  # noqa: F811
    async with pool.acquire() as setup:
        await receipt(setup, 170)
        await receipt(setup, 171)
    async with pool.acquire() as mig, pool.acquire() as resolver, pool.acquire() as editor:
        tx = mig.transaction()
        await tx.start()
        await mig.execute(FORWARD)

        async def resolve():
            async with resolver.transaction():
                return await resolve_delivery_bonus(resolver, 170, 15000, "hr-reviewer")

        late_resolve = asyncio.create_task(resolve())
        late_edit = asyncio.create_task(
            editor.execute("UPDATE practices SET status='on_process' WHERE id=171")
        )
        await asyncio.sleep(0.5)
        assert not late_resolve.done() and not late_edit.done()  # both wait for the commit
        await tx.commit()
        result = await asyncio.wait_for(late_resolve, 5)
        await asyncio.wait_for(late_edit, 5)
        assert result["status"] == "pending"
        assert set(await booked(mig)) == {170, 171}
        assert await mig.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=170") == 1


async def ledger_state(conn):
    return [dict(r) for r in await conn.fetch("SELECT * FROM hr_bonus_ledger ORDER BY id")]


@pytest.mark.asyncio
async def test_p8_rollback_removes_only_the_marked_rows_and_forward_books_again(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await receipt(conn, 180)
        await receipt(conn, 181)
        await receipt(conn, 182, booked_by=1)  # booked by the runtime: no marker
        runtime_before = dict(
            await conn.fetchrow("SELECT * FROM hr_bonus_ledger WHERE practice_id=182")
        )
        await conn.execute(FORWARD)
        assert set(await booked(conn)) == {180, 181}
        await conn.execute(ROLLBACK)
        assert await booked(conn) == {}
        assert (
            dict(await conn.fetchrow("SELECT * FROM hr_bonus_ledger WHERE practice_id=182"))
            == runtime_before
        )
        for pid in (180, 181):
            assert (
                await conn.fetchval(
                    "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=$1", pid
                )
                is None
            )
        assert (
            await conn.fetchval(
                "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=182"
            )
            is not None
        )
        await conn.execute(FORWARD)
        assert set(await booked(conn)) == {180, 181}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "moved_on",
    [
        "UPDATE hr_bonus_ledger SET status='approved' WHERE practice_id=190",
        "UPDATE hr_bonus_ledger SET status='rejected' WHERE practice_id=190",  # no note of its own
        "UPDATE hr_bonus_ledger SET payroll_period_id=5 WHERE practice_id=190",
    ],
)
async def test_p8_rollback_refuses_a_marked_row_that_moved_on(pool, moved_on):  # noqa: F811
    async with pool.acquire() as conn:
        await receipt(conn, 190)
        await receipt(conn, 191)
        await conn.execute(FORWARD)
        await conn.execute(moved_on)
        snapshot = await ledger_state(conn)
        with pytest.raises(asyncpg.RaiseError, match="Preserve November bonuses"):
            await conn.execute(ROLLBACK)
        assert await ledger_state(conn) == snapshot
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_delivery_bonus_events WHERE ledger_id IS NOT NULL"
            )
            == 2
        )


@pytest.mark.asyncio
async def test_p8_a_row_rejected_with_its_own_note_is_no_longer_recognised(pool):  # noqa: F811
    # reject_bonus overwrites notes: the marker is gone, so the rollback leaves the row and its link alone.
    async with pool.acquire() as conn:
        await receipt(conn, 195)
        await receipt(conn, 196)
        await conn.execute(FORWARD)
        await conn.execute(
            "UPDATE hr_bonus_ledger SET status='rejected', notes='HR: not due' WHERE practice_id=195"
        )
        rejected = dict(await conn.fetchrow("SELECT * FROM hr_bonus_ledger WHERE practice_id=195"))
        await conn.execute(ROLLBACK)
        assert (
            dict(await conn.fetchrow("SELECT * FROM hr_bonus_ledger WHERE practice_id=195"))
            == rejected
        )
        assert (
            await conn.fetchval(
                "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=195"
            )
            == rejected["id"]
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=196") == 0
        )
        assert (
            await conn.fetchval(
                "SELECT ledger_id FROM hr_delivery_bonus_events WHERE practice_id=196"
            )
            is None
        )
        # No money moves: the rejected row is not approved, reserved or paid.
        assert (rejected["status"], rejected["payroll_period_id"]) == ("rejected", None)


@pytest.mark.asyncio
async def test_p9_it_runs_clean_on_a_schema_with_no_bonus_tables(pool):  # noqa: F811
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("CREATE SCHEMA empty_394; SET LOCAL search_path TO empty_394")
        await conn.execute(FORWARD)
        await conn.execute(ROLLBACK)
        await tx.rollback()


@pytest.mark.asyncio
async def test_p9_the_ci_bootstrap_shape_with_a_ledger_but_no_receipts_books_nothing(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(FORWARD)
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0


def test_p10_the_file_carries_no_address():
    assert "@" not in PATH.read_text()
