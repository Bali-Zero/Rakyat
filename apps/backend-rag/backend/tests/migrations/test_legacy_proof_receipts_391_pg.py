"""Migration 391: the nine November proofs restored before cohort earning existed.

Runs the real migration (forward, guards, locks, rollback) on an isolated PostgreSQL schema.
Self-contained: only the delivery suite's schema fixture and plain SQL, no runtime delivery code.
"""

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from backend.db import migration_base
from backend.db.migration_base import split_migration_sql
from backend.tests.db.test_kita_document_delivery_pg import pool  # noqa: F401

EMAIL = "consultant@example.test"
NOVEMBER = datetime(2026, 11, 20, 4, tzinfo=UTC)
FORWARD, ROLLBACK = split_migration_sql(
    (
        Path(__file__).resolve().parents[2] / "db/migrations_v2/391_legacy_proof_receipts.sql"
    ).read_text()
)
MEMBER = "d76f5df0-d194-415c-86b3-9c3d38ad3cf1"
LISTED = (1378, 1379, 1380, 1407, 1408, 1409, 1410, 1411, 1414)
SLOT_AT = {
    1378: "2026-11-27 06:05:30.006241+00",
    1379: "2026-11-27 06:06:04.028734+00",
    1380: "2026-11-27 06:07:02.244355+00",
    1407: "2026-11-27 06:28:28.149941+00",
    1408: "2026-11-27 06:29:24.569338+00",
    1409: "2026-11-27 06:31:02.949951+00",
    1410: "2026-11-27 06:31:48.181301+00",
    1411: "2026-11-27 06:32:26.014731+00",
    1414: "2026-11-27 06:52:12.022591+00",
}


async def types(conn):
    # Production's activity_log carries the time of every row; the delivery suite's stub does not.
    await conn.execute(
        "ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS performed_at timestamptz DEFAULT now()"
    )
    await conn.execute("""
        INSERT INTO practice_types VALUES (3,'visa_b1_voa','B1 VOA','single_entry_visa'),
            (4,'ext_b1_voa','Extension B1','visa_extension'),
            (5,'ext_c1_tourism','C1 extension','visa_extension'),
            (6,'other_mutation_passport','Mutation','other'),
            (7,'visa_bridging','Bridging','single_entry_visa'),
            (8,'other_custom','Custom','other');
        DELETE FROM hr_bonus_rates WHERE practice_type_code IN ('ext_b1_voa','ext_c1_tourism','visa_b1_voa',
            'other_mutation_passport','visa_bridging','other_custom');
        INSERT INTO hr_bonus_rates(id,practice_type_code,is_active,effective_from,amount_idr,forfeit_below_idr)
            VALUES (68,'ext_b1_voa',true,'2026-11-01',15000,850000),
                   (70,'ext_c1_tourism',true,'2026-11-01',20000,1700000),
                   (71,'visa_b1_voa',true,'2026-11-01',20000,NULL),
                   (72,'other_mutation_passport',true,'2026-11-01',10000,NULL),
                   (73,'visa_bridging',true,'2026-11-01',100000,NULL),
                   (74,'other_custom',true,'2026-11-01',0,NULL);
    """)


async def practice(conn, pid, type_id, *, completed=NOVEMBER, status="completed", total=None):
    await conn.execute(
        """INSERT INTO practices(id,client_id,assigned_to,client_visible,status,payment_status,
               practice_type_id,completion_date,updated_at) VALUES($1,31,$2,true,$3,'paid',$4,$5,now())""",
        pid,
        EMAIL,
        status,
        type_id,
        completed,
    )
    if total is not None:
        await conn.execute("INSERT INTO invoices(practice_id,amount_idr) VALUES($1,$2)", pid, total)


# ---- Migration 391 -------------------------------------------------------------------------


async def seed_f4(conn, *, only=LISTED):
    await types(conn)
    await conn.execute(
        "INSERT INTO team_members VALUES($1,'f4-assignee@example.test','consultant',true)", MEMBER
    )
    await conn.execute("INSERT INTO hr_employees VALUES(6,$1,true,'{}'::jsonb)", MEMBER)
    for pid in only:
        type_id = 5 if pid == 1414 else 4
        code = "ext_c1_tourism" if pid == 1414 else "ext_b1_voa"
        await conn.execute(
            """INSERT INTO practices(id,client_id,assigned_to,client_visible,status,payment_status,
                   practice_type_id,completion_date,updated_at)
               VALUES($1,31,'f4-assignee@example.test',true,'completed','paid',$2,$3,now())""",
            pid,
            type_id,
            NOVEMBER,
        )
        doc = {1378: 21162, 1379: 21163, 1380: 21164, 1407: 21165, 1408: 21166, 1409: 21167,
               1410: 21168, 1411: 21169, 1414: 21170}[pid]  # fmt: skip
        await conn.execute(
            """INSERT INTO documents(id,practice_id,client_id,file_id,file_name,uploaded_source,
                   status,is_archived,client_visible,intake_idempotency_key,document_type,content_hash)
               VALUES($1,$2,31,'f','f.pdf','team','active',false,true,$3,'itk',$3)""",
            doc,
            pid,
            f"k{pid}",
        )
        await conn.execute(
            """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by,updated_at)
               VALUES($1,$2,'document_1',$3,'seed',$4)""",
            pid,
            code,
            doc,
            datetime.fromisoformat(SLOT_AT[pid]),
        )
        await conn.execute(
            "INSERT INTO invoices(practice_id,amount_idr) VALUES($1,$2)",
            pid,
            1600000 if pid == 1414 else 850000,
        )
        # The accepting write's own audit row carries the slot's transaction time.
        await conn.execute(
            """INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)
               VALUES('practice',$1,'updated','seed','Missing final permit repaired: document_1',$2)""",
            pid,
            datetime.fromisoformat(SLOT_AT[pid]),
        )


@pytest.mark.asyncio
async def test_391_records_the_measured_receipts_and_nothing_else(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await seed_f4(conn)
        await conn.execute(FORWARD)
        rows = await conn.fetch("SELECT * FROM hr_delivery_bonus_events ORDER BY practice_id")
        assert [r["practice_id"] for r in rows] == list(LISTED)
        for r in rows:
            assert r["event_kind"] == "service_delivered" and r["ledger_id"] is None
            assert r["employee_id"] == 6 and r["original_team_member_id"] == MEMBER
            assert r["earned_at"] == datetime.fromisoformat(SLOT_AT[r["practice_id"]])
            assert r["awarded_at"] == r["earned_at"]
            assert r["recorded_at"] == datetime(2026, 11, 27, 11, tzinfo=UTC)
        assert {
            r["practice_id"]: (r["bonus_rate_id"], r["amount_idr"], r["forfeited"]) for r in rows
        } == {
            **dict.fromkeys(LISTED[:-1], (68, 15000, False)),
            1414: (70, None, True),
        }
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0
        with pytest.raises(asyncpg.RaiseError, match="marker"):
            await conn.execute(FORWARD)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spoil",
    [
        "UPDATE practices SET status='on_process' WHERE id=1378",
        "INSERT INTO hr_delivery_bonus_events(practice_id,service_code,event_kind,earned_at,awarded_at)"
        " VALUES(1378,'ext_b1_voa','service_delivered',now(),now())",
        "INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr,awarded_at,status)"
        " VALUES(1378,1,'ext_b1_voa',1,now(),'pending')",
        "INSERT INTO practice_delivery_completions VALUES(1378,'ext_b1_voa','x')",
        "UPDATE practice_delivery_slots SET updated_at=updated_at+interval '1 second' WHERE practice_id=1378",
        "UPDATE documents SET is_archived=true WHERE id=21162",
        "UPDATE documents SET file_id=NULL WHERE id=21162",
        "UPDATE invoices SET amount_idr=amount_idr+1 WHERE practice_id=1378",
        "UPDATE team_members SET email='someone-else@example.test'",
        "UPDATE hr_bonus_rates SET amount_idr=16000 WHERE id=68",
        "UPDATE practices SET completion_date='2026-10-31 15:59+00' WHERE id=1378",
        "UPDATE practices SET completion_date='2026-11-30 16:00+00' WHERE id=1378",
        "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)"
        " VALUES('practice',1378,'updated','x','Updated: assigned_to','2026-11-27 07:00+00')",
        "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)"
        " VALUES('practice',1378,'updated','x','Final document uploaded: document_1','2026-11-27 06:05:30.006242+00')",
    ],
)
async def test_391_skips_a_row_whose_measured_fact_no_longer_holds(pool, spoil):  # noqa: F811
    async with pool.acquire() as conn:
        await seed_f4(conn)
        await conn.execute(spoil)
        notices = []
        conn.add_log_listener(lambda c, m: notices.append(m.message))
        await conn.execute(FORWARD)
        listed = {
            r["practice_id"]
            for r in await conn.fetch(
                "SELECT practice_id FROM hr_delivery_bonus_events WHERE recorded_at=$1",
                datetime(2026, 11, 27, 11, tzinfo=UTC),
            )
        }
    if "team_members" in spoil:
        assert listed == set()
    elif "hr_bonus_rates" in spoil:
        assert listed == {1414}  # only the ext_b1_voa rate changed
    else:
        assert listed == set(LISTED) - {1378}
        assert any("B2a skip 1378" in n for n in notices)


@pytest.mark.asyncio
async def test_391_rollback_removes_only_its_marked_receipts_and_refuses_booked_ones(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await seed_f4(conn)
        await conn.execute(FORWARD)
        # A receipt of another origin on a listed practice's neighbour is untouched.
        await practice(conn, 9000, 4, total=850000)
        await conn.execute(
            """INSERT INTO hr_delivery_bonus_events(practice_id,service_code,event_kind,earned_at,awarded_at)
               VALUES(9000,'ext_b1_voa','service_delivered',now(),now())"""
        )
        ledger = await conn.fetchval(
            """INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr,awarded_at,status)
               VALUES(1378,6,'ext_b1_voa',15000,now(),'pending') RETURNING id"""
        )
        await conn.execute(
            "UPDATE hr_delivery_bonus_events SET ledger_id=$1 WHERE practice_id=1378", ledger
        )
        with pytest.raises(asyncpg.RaiseError, match="booked"):
            async with conn.transaction():
                await conn.execute(ROLLBACK)
        assert await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events") == 10
        await conn.execute(
            "UPDATE hr_delivery_bonus_events SET ledger_id=NULL WHERE practice_id=1378"
        )
        await conn.execute("DELETE FROM hr_bonus_ledger")
        # The same practice with a different recorded_at is not ours.
        await conn.execute(
            "UPDATE hr_delivery_bonus_events SET recorded_at=recorded_at+interval '1 second' WHERE practice_id=1379"
        )
        await conn.execute(ROLLBACK)
        left = {
            r["practice_id"]
            for r in await conn.fetch("SELECT practice_id FROM hr_delivery_bonus_events")
        }
    assert left == {1379, 9000}


def test_the_migration_carries_no_staff_or_client_identity():
    text = (
        Path(__file__).resolve().parents[2] / "db/migrations_v2/391_legacy_proof_receipts.sql"
    ).read_text()
    assert "@" not in text


ARCHIVE = "UPDATE documents SET is_archived=true WHERE id=21162"
# A permanent delete: the FK cascade removes the slot too. The migration must lock in the
# upload path's order (practice, document, slots) or the two would deadlock.
DELETE = "DELETE FROM documents WHERE id=21162"
INVOICE = "UPDATE invoices SET amount_idr=amount_idr+1 WHERE practice_id=1378"
RATE = "UPDATE hr_bonus_rates SET amount_idr=16000 WHERE id=68"
OUTCOMES = {
    ARCHIVE: set(LISTED) - {1378},
    DELETE: set(LISTED) - {1378},
    INVOICE: set(LISTED) - {1378},
    RATE: {1414},  # the eight ext_b1_voa rows read rate 68, 1414 reads rate 70
}


async def written(conn):
    return {
        r["practice_id"]
        for r in await conn.fetch("SELECT practice_id FROM hr_delivery_bonus_events")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [ARCHIVE, DELETE, INVOICE, RATE])
async def test_391_never_waits_for_a_held_lock_and_applies_once_it_is_free(pool, change):  # noqa: F811
    async with pool.acquire() as setup:
        await seed_f4(setup)
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = writer.transaction()
        await tx.start()
        await writer.execute(change)
        started = time.monotonic()
        with pytest.raises(asyncpg.LockNotAvailableError):  # 55P03, the one the runner retries
            await asyncio.wait_for(migrator.execute(FORWARD), 5)  # a waiting migration fails here
        assert time.monotonic() - started < 1
        assert await written(migrator) == set()
        await tx.commit()
        await migrator.execute(FORWARD)  # the retry now reads the committed change
        assert await written(migrator) == OUTCOMES[change]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [ARCHIVE, DELETE, INVOICE, RATE])
async def test_391_holds_its_facts_until_it_commits(pool, change):  # noqa: F811
    async with pool.acquire() as setup:
        await seed_f4(setup)
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = migrator.transaction()
        await tx.start()
        await migrator.execute(FORWARD)
        late = asyncio.create_task(writer.execute(change))
        await asyncio.sleep(0.5)
        assert not late.done()  # the change cannot slip in between validation and insert
        await tx.commit()
        await asyncio.wait_for(late, 10)  # no deadlock error, the change then succeeds
        # Every receipt was written on the facts as measured. After a delete the receipt of
        # that practice outlives its document, which is fine: the receipt is history.
        assert await written(writer) == set(LISTED)


@pytest.mark.asyncio
@pytest.mark.parametrize("bump", [True, False])
async def test_391_cannot_deadlock_with_the_payment_confirmation_order(pool, bump):  # noqa: F811
    # confirm_payment locks the practice and then updates its invoice; the migration holding
    # the invoice table first and then waiting for the practice would be a deadlock cycle.
    async with pool.acquire() as setup:
        await seed_f4(setup)
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("SELECT 1 FROM practices WHERE id=1378 FOR UPDATE")
        started = time.monotonic()
        with pytest.raises(asyncpg.LockNotAvailableError) as refused:
            await asyncio.wait_for(migrator.execute(FORWARD), 5)
        assert not isinstance(refused.value, asyncpg.DeadlockDetectedError)
        assert time.monotonic() - started < 0.5  # at once, far below deadlock_timeout
        if bump:
            await asyncio.wait_for(
                writer.execute(
                    "UPDATE invoices SET amount_idr=amount_idr+1 WHERE practice_id=1378"
                ),
                5,
            )
        await tx.commit()
        await migrator.execute(FORWARD)
        assert await written(migrator) == (set(LISTED) - {1378} if bump else set(LISTED))


@pytest.mark.asyncio
async def test_391_through_the_real_runner_retries_until_the_lock_is_released(pool, monkeypatch):  # noqa: F811
    monkeypatch.setattr(migration_base, "LOCK_RETRY_BACKOFF_BASE_SECONDS", 0.05)
    async with pool.acquire() as setup:
        await seed_f4(setup)
    attempts = []
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("SELECT 1 FROM practices WHERE id=1411 FOR UPDATE")

        async def release():
            await asyncio.sleep(1)
            await tx.commit()

        async def attempt():
            attempts.append(1)
            await migrator.execute(FORWARD)
            return True

        freed = asyncio.create_task(release())
        assert await migration_base.run_with_lock_retry(migrator, "391", attempt) is True
        await freed
        assert len(attempts) >= 2
        assert await written(migrator) == set(LISTED)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rate_change",
    [
        "UPDATE hr_bonus_rates SET effective_from='2026-12-01' WHERE id=68",  # not yet in force
        "UPDATE hr_bonus_rates SET effective_to='2026-11-26' WHERE id=68",  # expired before the 27th
    ],
)
async def test_391_requires_the_rate_to_be_in_force_on_the_earning_day(pool, rate_change):  # noqa: F811
    async with pool.acquire() as conn:
        await seed_f4(conn)
        await conn.execute(rate_change)
        await conn.execute(FORWARD)
        # The eight ext_b1_voa rows read rate 68; 1414 reads rate 70, still in force.
        assert await written(conn) == {1414}


@pytest.mark.asyncio
async def test_391_rollback_never_waits_for_a_held_receipt(pool):  # noqa: F811
    async with pool.acquire() as setup:
        await seed_f4(setup)
        await setup.execute(FORWARD)
    async with pool.acquire() as holder, pool.acquire() as rolling:
        tx = holder.transaction()
        await tx.start()
        await holder.execute(
            "SELECT 1 FROM hr_delivery_bonus_events WHERE practice_id=1378 FOR UPDATE"
        )
        started = time.monotonic()
        with pytest.raises(asyncpg.LockNotAvailableError):
            await asyncio.wait_for(rolling.execute(ROLLBACK), 5)  # waiting would fail here
        assert time.monotonic() - started < 1
        await tx.commit()
        await rolling.execute(ROLLBACK)
        assert await written(rolling) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["invoices", "activity_log"])
async def test_391_records_nothing_where_billing_or_activity_history_does_not_exist(pool, missing):  # noqa: F811
    # The schema CI bootstraps from scratch has neither table; every measured fact is unprovable.
    async with pool.acquire() as conn:
        await seed_f4(conn)
        tx = conn.transaction()
        await tx.start()
        await conn.execute(f"DROP TABLE {missing} CASCADE")
        await conn.execute(FORWARD)
        assert await written(conn) == set()
        await tx.rollback()
