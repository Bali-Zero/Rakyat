"""A first `service_delivered` receipt is earned when the proof FIRST existed, for whoever held the practice then.

History is read from the durable activity rows (acceptances of any slot, archived since or not, and
waivers). Complete is the path under test; it runs on today's code with only the earning instant changed.
Real PostgreSQL through the delivery suite's fixture.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi import BackgroundTasks

from backend.app.services.hr import delivery_bonus as bonus
from backend.services.crm import practice_delivery as service
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

OTHER = "former-assignee@example.test"
PMA, ONSHORE = (
    20,
    21,
)  # company_pt_pma (service_delivered), kitas_investor_2yr_onshore (e-visa waivable)
NOV_30 = datetime(2026, 11, 30, 4, tzinfo=UTC)  # 12:00 in Bali
DEC_1 = datetime(2026, 12, 1, 4, tzinfo=UTC)
NOV_29 = datetime(2026, 11, 29, 4, tzinfo=UTC)


async def setup(conn, pid, type_id, *, assignee=STAFF["email"]):
    await conn.execute(
        f"""INSERT INTO practice_types VALUES(20,'company_pt_pma','New Company (PT PMA)','company'),
              (21,'kitas_investor_2yr_onshore','Investor KITAS onshore','kitas') ON CONFLICT DO NOTHING;
           INSERT INTO team_members VALUES('staff-2','{OTHER}','consultant',true) ON CONFLICT DO NOTHING;
           INSERT INTO hr_employees VALUES(2,'staff-2',true,'{{}}'::jsonb) ON CONFLICT DO NOTHING;
           INSERT INTO hr_bonus_rates(practice_type_code,is_active,effective_from,amount_idr)
              VALUES('company_pt_pma',true,'2026-11-01',200000),
                    ('kitas_investor_2yr_onshore',true,'2026-11-01',300000) ON CONFLICT DO NOTHING"""
    )
    await conn.execute(
        """INSERT INTO practices VALUES($1,31,null,$2,true,'on_process','paid',$3,null,now())""",
        pid,
        assignee,
        type_id,
    )


async def accept(conn, pid, key, at, *, archived=False, live_name=None, history=True):
    """A slot document and its accepted-upload history row; `live_name` None leaves the slot pointing at it."""
    name = live_name or f"{key}-{at:%m%d}"
    digest = hashlib.sha256(name.encode()).hexdigest()
    marker = f"case-delivery:{pid}:0:{key}:{digest}"
    doc = await conn.fetchval(
        """INSERT INTO documents(practice_id,client_id,file_id,file_name,uploaded_source,status,is_archived,
               client_visible,intake_idempotency_key,document_type,content_hash)
           VALUES($1,31,$2,$3,'team','active',$4,true,$2,$5,$2) RETURNING id""",
        pid,
        marker,
        f"{name}.pdf",
        archived,
        key,
    )
    await conn.execute(
        """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by)
           SELECT $1::int,pt.code,$2::text,$3::int,'seed' FROM practices p JOIN practice_types pt
           ON pt.id=p.practice_type_id WHERE p.id=$1
           ON CONFLICT(practice_id,slot_key) DO UPDATE SET document_id=excluded.document_id""",
        pid,
        key,
        doc,
    )
    if history:
        await conn.execute(
            "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)"
            " VALUES('practice',$1,'updated','seed',$2,$3)",
            pid,
            f"Final document uploaded: {key}",
            at,
        )


async def waiver(conn, pid, key, at):
    await conn.execute(
        """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,waiver_reason,updated_by)
           SELECT $1::int,pt.code,$2::text,'Not on this route','seed' FROM practices p JOIN practice_types pt
           ON pt.id=p.practice_type_id WHERE p.id=$1""",
        pid,
        key,
    )
    await conn.execute(
        "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)"
        " VALUES('practice',$1,'updated','seed',$2,$3)",
        pid,
        f"Delivery slot {key} inapplicable: Not on this route",
        at,
    )


async def assigned(conn, pid, to, at, *, changes=True):
    await conn.execute(
        "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at,changes)"
        " VALUES('practice',$1,'updated','staff',$2,$3,$4::text::jsonb)",
        pid,
        "Updated: assigned_to",
        at,
        json.dumps({"assigned_to": to}) if changes else None,
    )


async def receipt(conn, pid):
    return await conn.fetchrow(
        """SELECT e.event_kind,e.earned_at,e.awarded_at,e.employee_id,e.recorded_at,e.ledger_id
           FROM hr_delivery_bonus_events e WHERE e.practice_id=$1""",
        pid,
    )


async def complete_pma(conn, pid=PMA):
    for key, at in (("document_2", DEC_1), ("document_3", DEC_1), ("document_4", DEC_1)):
        await accept(conn, pid, key, at)


@pytest.mark.asyncio
async def test_complete_in_december_earns_at_the_november_first_proof_for_the_assignee_then(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA, assignee=STAFF["email"])
        await assigned(conn, PMA, OTHER, datetime(2026, 11, 20, 4, tzinfo=UTC))
        await accept(conn, PMA, "document_1", NOV_30)
        await complete_pma(conn)
        await assigned(conn, PMA, STAFF["email"], DEC_1)
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM hr_delivery_bonus_events WHERE practice_id=$1", PMA)
        assert len(rows) == 1
        row = await receipt(conn, PMA)
        assert row["event_kind"] == "service_delivered" and row["earned_at"] == NOV_30
        assert row["awarded_at"].astimezone(bonus.WITA).month == 11 and row["employee_id"] == 2
        assert (
            datetime.now(UTC) - row["recorded_at"]
        ).total_seconds() < 120  # recorded_at stays now
        assert row["ledger_id"] is not None
        ledger = await conn.fetchrow(
            "SELECT employee_id,awarded_at FROM hr_bonus_ledger WHERE practice_id=$1", PMA
        )
        assert (
            ledger["employee_id"] == 2 and ledger["awarded_at"].astimezone(bonus.WITA).month == 11
        )


@pytest.mark.asyncio
async def test_upload_then_waiver_in_november_then_complete_in_december_earns_at_the_waiver(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
        await accept(conn, ONSHORE, "document_2", NOV_29)  # e-ITAS
        await waiver(conn, ONSHORE, "electronic_visa", NOV_30)
        await accept(conn, ONSHORE, "document_3", DEC_1)  # IMK
    await service.complete(pool, ONSHORE, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        row = await receipt(conn, ONSHORE)
    assert row["event_kind"] == "service_delivered" and row["earned_at"] == NOV_30
    assert row["employee_id"] == 1


@pytest.mark.asyncio
async def test_first_document_archived_second_live_earns_at_the_first_acceptance(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        # The Akta was accepted in November and archived since; its slot holds a December upload.
        await accept(conn, PMA, "document_1", NOV_30, archived=True, live_name="akta-old")
        await accept(conn, PMA, "document_1", DEC_1, history=False, live_name="akta-new")
        await accept(conn, PMA, "document_2", DEC_1)
        await accept(conn, PMA, "document_3", DEC_1)  # the NIB, live, accepted in December
        await accept(conn, PMA, "document_4", DEC_1)
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert (await receipt(conn, PMA))["earned_at"] == NOV_30


@pytest.mark.asyncio
async def test_assignment_rows_only_after_t_leave_the_receipt_to_hr(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_30)
        await complete_pma(conn)
        # The creation row records no assignee: all that is known is a change AFTER the proof.
        await assigned(conn, PMA, STAFF["email"], DEC_1)
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        row = await receipt(conn, PMA)
        status = (await bonus.get_delivery_bonus(conn, PMA))["status"]
    assert row["earned_at"] == NOV_30 and row["employee_id"] is None
    assert status == "awaiting_employee" and row["ledger_id"] is None


@pytest.mark.asyncio
async def test_a_proof_before_november_earns_nothing(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await accept(
            conn, PMA, "document_1", datetime(2026, 10, 31, 15, tzinfo=UTC)
        )  # 22:00 in Bali
        await complete_pma(conn)
    result = await service.complete(pool, PMA, STAFF, BackgroundTasks())
    assert result["status"] == "completed"
    async with pool.acquire() as conn:
        assert await receipt(conn, PMA) is None


@pytest.mark.asyncio
async def test_no_history_earns_now_for_the_current_assignee(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        for key in ("document_1", "document_2", "document_3", "document_4"):
            await accept(conn, PMA, key, DEC_1, history=False)
    before = datetime.now(UTC)
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        row = await receipt(conn, PMA)
    assert row["earned_at"] >= before and row["employee_id"] == 1


@pytest.mark.asyncio
async def test_backdated_receipt_into_a_closed_november_is_kept_without_a_ledger_row(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_30)
        await complete_pma(conn)
        await conn.execute(
            "INSERT INTO hr_bonus_runs(bonus_month,status) VALUES('2026-11-01','approved')"
        )
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        row = await receipt(conn, PMA)
        assert row["earned_at"] == NOV_30 and row["employee_id"] == 1 and row["ledger_id"] is None
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", PMA)
            == 0
        )
        # HR carries it: the receipt stays visible with its month closed.
        assert (await bonus.get_delivery_bonus(conn, PMA))["status"] == "payroll_locked"


@pytest.mark.asyncio
async def test_existing_receipt_is_read_not_reinserted_while_hr_resolves_it(pool, monkeypatch):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
        # An unbooked e-visa receipt (no rate yet), the e-visa archived and waived, the e-ITAS and IMK live.
        await conn.execute(
            "DELETE FROM hr_bonus_rates WHERE practice_type_code='kitas_investor_2yr_onshore'"
        )
        async with conn.transaction():
            await conn.fetchrow("SELECT id FROM practices WHERE id=$1 FOR UPDATE", ONSHORE)
            await bonus.record_delivery_bonus(
                conn, ONSHORE, "kitas_investor_2yr_onshore", STAFF["email"], "electronic_visa_issued",
                issued_on=datetime(2026, 11, 20).date(),
            )  # fmt: skip
        await accept(conn, ONSHORE, "document_2", NOV_29)
        await accept(conn, ONSHORE, "document_3", NOV_30)
        await waiver(conn, ONSHORE, "electronic_visa", NOV_30)
        assert (await receipt(conn, ONSHORE))["ledger_id"] is None
    entered, hr_blocked = asyncio.Event(), asyncio.Event()
    real = service.first_proof_instant

    async def held(conn, p, policy=None):
        entered.set()  # W holds the practice row lock here
        await asyncio.wait_for(hr_blocked.wait(), 15)
        return await real(conn, p, policy)

    monkeypatch.setattr(service, "first_proof_instant", held)
    hr_pid: dict = {}
    if True:
        writer = asyncio.create_task(service.complete(pool, ONSHORE, STAFF, BackgroundTasks()))
        await asyncio.wait_for(entered.wait(), 15)

        async def hr():
            async with pool.acquire() as conn, conn.transaction():
                hr_pid["pid"] = await conn.fetchval("SELECT pg_backend_pid()")
                return await bonus.resolve_delivery_bonus(conn, ONSHORE, 300000, "hr@example.test")

        hr_task = asyncio.create_task(hr())
        async with pool.acquire() as probe:
            for _ in range(
                150
            ):  # H is blocked on the practice key-share lock once it waits on a lock
                if await probe.fetchval(
                    "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=$1",
                    hr_pid.get("pid", 0),
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("HR never reached its ledger insert")
        hr_blocked.set()
        completed = await asyncio.wait_for(writer, 15)  # no deadlock, no wait on HR
        resolved = await asyncio.wait_for(hr_task, 15)
    assert completed["status"] == "completed" and resolved["status"] == "pending"
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_delivery_bonus_events WHERE practice_id=$1", ONSHORE
            )
            == 1
        )
        row = await receipt(conn, ONSHORE)
        assert row["event_kind"] == "electronic_visa_issued" and row["ledger_id"] is not None
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=$1", ONSHORE
            )
            == 1
        )


@pytest.mark.asyncio
async def test_an_undated_acceptance_row_is_ignored(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_30)
        await complete_pma(conn)
        await conn.execute(
            "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)"
            " VALUES('practice',$1,'updated','seed','Final document uploaded: document_2',NULL)",
            PMA,
        )
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert (await receipt(conn, PMA))["earned_at"] == NOV_30


@pytest.mark.asyncio
async def test_only_undated_acceptances_earn_now(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        for key in ("document_1", "document_2", "document_3", "document_4"):
            await accept(conn, PMA, key, DEC_1, history=False)
        await conn.execute(
            "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at)"
            " VALUES('practice',$1,'updated','seed','Final document uploaded: document_1',NULL)",
            PMA,
        )
    before = datetime.now(UTC)
    await service.complete(pool, PMA, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert (await receipt(conn, PMA))["earned_at"] >= before


@pytest.mark.asyncio
async def test_an_undated_assignment_row_leaves_the_receipt_to_hr(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await accept(conn, PMA, "document_1", NOV_30)
        await complete_pma(conn)
        await assigned(conn, PMA, OTHER, NOV_29)
        await conn.execute(
            "INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,performed_at,changes)"
            " VALUES('practice',$1,'updated','staff','Updated: assigned_to',NULL,$2::text::jsonb)",
            PMA,
            json.dumps({"assigned_to": STAFF["email"]}),
        )
    result = await service.complete(pool, PMA, STAFF, BackgroundTasks())  # no TypeError
    assert result["status"] == "completed"
    async with pool.acquire() as conn:
        row = await receipt(conn, PMA)
    assert row["earned_at"] == NOV_30 and row["employee_id"] is None


# ── the history order is the order the practice lock was won, not the order transactions began ──

NOV_20 = datetime(2026, 11, 20, 4, tzinfo=UTC)
ADMIN = {"email": "admin@example.test", "role": "admin"}


async def unrestricted(conn, user):
    return None  # an admin sees every practice, whoever holds it now


async def blocked_on_a_lock(pool, *, other_than):  # noqa: F811
    async with pool.acquire() as probe:
        for _ in range(150):
            if await probe.fetchval(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND pid<>$1",
                other_than,
            ):
                return
            await asyncio.sleep(0.1)
    pytest.fail("nobody ever waited on the practice lock")


async def patch_assignee(conn, pid, to):
    """What the PATCH route does, in its order: lock the row, UPDATE, then write its stamped audit row."""
    await conn.fetchrow("SELECT id FROM practices WHERE id=$1 FOR UPDATE", pid)
    await conn.execute("UPDATE practices SET assigned_to=$2 WHERE id=$1", pid, to)
    await conn.execute(
        """INSERT INTO activity_log(entity_type,entity_id,action,performed_by,description,changes,performed_at)
           VALUES('practice',$1,'updated','staff','Updated: assigned_to',$2::text::jsonb,clock_timestamp())""",
        pid,
        json.dumps({"assigned_to": to}),
    )


@pytest.mark.asyncio
async def test_a_waiver_that_waited_for_the_lock_is_dated_after_the_reassignment_that_won_it(
    pool,
    monkeypatch,  # noqa: F811
):
    monkeypatch.setattr(service, "process_assignees", unrestricted)
    async with pool.acquire() as conn:
        await setup(conn, ONSHORE, ONSHORE)
        await assigned(conn, ONSHORE, STAFF["email"], NOV_20)
        await accept(conn, ONSHORE, "document_2", NOV_20)
        await accept(conn, ONSHORE, "document_3", NOV_20)
    async with pool.acquire() as holder:
        tx = holder.transaction()
        await tx.start()
        await holder.fetchrow("SELECT id FROM practices WHERE id=$1 FOR UPDATE", ONSHORE)
        pid = await holder.fetchval("SELECT pg_backend_pid()")
        waiver_task = asyncio.create_task(
            service.waive(pool, ONSHORE, "electronic_visa", ADMIN, "Not on this route")
        )
        await blocked_on_a_lock(pool, other_than=pid)  # the waiver's transaction began, and waits
        await asyncio.sleep(0.3)
        await patch_assignee(holder, ONSHORE, OTHER)
        await tx.commit()
    await asyncio.wait_for(waiver_task, 15)
    await service.complete(pool, ONSHORE, ADMIN, BackgroundTasks())
    async with pool.acquire() as conn:
        assert (await receipt(conn, ONSHORE))["employee_id"] == 2  # the NEW assignee held it at T


@pytest.mark.asyncio
async def test_a_reassignment_that_waited_behind_an_upload_is_dated_after_it(pool, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "process_assignees", unrestricted)
    async with pool.acquire() as conn:
        await setup(conn, PMA, PMA)
        await assigned(conn, PMA, STAFF["email"], NOV_20)
        for key in ("document_2", "document_3", "document_4"):
            await accept(conn, PMA, key, NOV_20, history=False)
    audited, release = asyncio.Event(), asyncio.Event()
    real = service.audit

    async def held(conn, practice_id, actor, description, **kwargs):
        if description.startswith("Final document uploaded"):
            audited.set()  # the lock is held; the acceptance row is not written yet
            await asyncio.wait_for(release.wait(), 15)
        await real(conn, practice_id, actor, description, **kwargs)

    monkeypatch.setattr(service, "audit", held)
    upload = asyncio.create_task(
        service.upload(pool, PMA, "document_1", STAFF, final_file("akta"), BackgroundTasks())
    )
    await asyncio.wait_for(audited.wait(), 15)

    async def reassign():
        async with pool.acquire() as conn, conn.transaction():
            reassign.pid = await conn.fetchval("SELECT pg_backend_pid()")
            await patch_assignee(conn, PMA, OTHER)

    reassigning = asyncio.create_task(reassign())
    await blocked_on_a_lock(pool, other_than=0)
    await asyncio.sleep(0.3)
    release.set()
    await asyncio.wait_for(upload, 15)
    await asyncio.wait_for(reassigning, 15)
    monkeypatch.setattr(service, "audit", real)
    await service.complete(pool, PMA, ADMIN, BackgroundTasks())
    async with pool.acquire() as conn:
        assert (await receipt(conn, PMA))["employee_id"] == 1  # the OLD assignee held it at T


def test_the_practice_patch_stamps_its_activity_row_with_the_clock_not_the_transaction_start():
    import inspect

    from backend.app.routers import crm_practices

    source = inspect.getsource(crm_practices)
    assert "VALUES ($1, $2, $3, $4, $5, $6::text::jsonb, clock_timestamp())" in source
