"""B2a: a November practice completed before Final delivery earns when its proof is uploaded.

Runs the real delivery service and migration 391 on an isolated PostgreSQL schema (the
fixture of the delivery suite). Only the file sink and the cache invalidation are substituted.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.services.hr.hr_service import business_today
from backend.db.migration_base import split_migration_sql
from backend.services.crm import practice_delivery as service
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

EMAIL = STAFF["email"]
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


async def upload(db, pid, key, name=None, **kwargs):
    return await service.upload(
        db, pid, key, STAFF, final_file(name or ('electronic-visa' if key == 'electronic_visa' else key)), kwargs.pop("tasks", BackgroundTasks()),
        repair=True, **kwargs,
    )  # fmt: skip


async def events(conn, pid):
    return await conn.fetch(
        "SELECT event_kind, amount_idr, forfeited FROM hr_delivery_bonus_events WHERE practice_id=$1",
        pid,
    )


async def accept_earlier(conn, pid, key, *, how="archive", doc_name="earlier"):
    """The state the old completed-permit restore left: an accepted document and its audit row."""
    doc = await conn.fetchval(
        """INSERT INTO documents(practice_id,client_id,file_id,file_name,uploaded_source,status,
               is_archived,client_visible,intake_idempotency_key,document_type,content_hash)
           VALUES($1,31,$2,$2,'team','active',$3,true,$2,'itk',$2) RETURNING id""",
        pid,
        f"{doc_name}-{pid}-{key}",
        how == "archive",
    )
    await conn.execute(
        """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by)
           SELECT $1::int,pt.code,$2::text,$3::int,'seed' FROM practices p JOIN practice_types pt
           ON pt.id=p.practice_type_id WHERE p.id=$1""",
        pid,
        key,
        doc,
    )
    await conn.execute(
        """INSERT INTO activity_log VALUES('practice',$1,'updated','seed',$2)""",
        pid,
        f"Missing final permit repaired: {key}",
    )
    if how == "delete":
        await conn.execute("DELETE FROM documents WHERE id=$1", doc)


async def view(conn, pid):
    if isinstance(conn, asyncpg.Pool):
        async with conn.acquire() as one:
            return await view(one, pid)
    return await service.state(conn, await service.practice(conn, pid, STAFF), STAFF)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completed", "cohort"),
    [
        (datetime(2026, 10, 31, 15, 59, tzinfo=UTC), False),  # 2026-10-31 23:59 in Bali
        (datetime(2026, 10, 31, 16, 0, tzinfo=UTC), True),  # 2026-11-01 00:00 in Bali
        (datetime(2026, 11, 30, 15, 59, tzinfo=UTC), True),  # 2026-11-30 23:59 in Bali
        (datetime(2026, 11, 30, 16, 0, tzinfo=UTC), False),  # 2026-12-01 00:00 in Bali
    ],
)
async def test_cohort_is_completion_in_november_in_bali(pool, completed, cohort):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 20, 4, completed=completed)
        await practice(conn, 23, 6, completed=completed)
        assert (await view(conn, 20))["legacy_proof"] is cohort
        # Outside the cohort a non-permit document is not accepted on a completed practice.
        assert (await view(conn, 23))["slots"][0]["can_repair"] is cohort
    # The permit restore of a completed practice is unchanged, but earns only inside the cohort.
    await upload(pool, 20, "document_1")
    async with pool.acquire() as conn:
        assert len(await events(conn, 20)) == int(cohort)


@pytest.mark.asyncio
async def test_a_delivered_practice_is_not_cohort_and_an_open_one_is_not_either(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 21, 4)
        await conn.execute("INSERT INTO practice_delivery_completions VALUES(21,'ext_b1_voa','x')")
        await practice(conn, 22, 4, status="on_process", completed=None)
        assert not (await view(conn, 21))["legacy_proof"]
        assert not (await view(conn, 22))["legacy_proof"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("type_id", "key", "kind", "amount", "total"),
    [
        (4, "document_1", "service_delivered", 15000, 850000),
        (5, "document_1", "service_delivered", 20000, 1700000),
        (6, "document_1", "service_delivered", 10000, None),
        (7, "document_1", "service_delivered", 100000, None),
        (3, "electronic_visa", "electronic_visa_issued", 20000, None),
    ],
)
async def test_the_first_proof_earns_in_the_same_write_and_changes_nothing_else(  # noqa: F811
    pool, type_id, key, kind, amount, total
):
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 30, type_id, total=total)
    tasks = BackgroundTasks()
    result = await upload(pool, 30, key, tasks=tasks)
    assert result["status"] == "completed" and not result["delivery_recorded"] and not tasks.tasks
    assert result["bonus_event"] == kind
    async with pool.acquire() as conn:
        assert [tuple(r.values()) for r in await events(conn, 30)] == [(kind, amount, False)]
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE practice_id=30") == 1
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_completions") == 0
        assert await conn.fetchval("SELECT count(*) FROM timeline_events") == 0
        assert (await conn.fetchval("SELECT status FROM practices WHERE id=30")) == "completed"
        assert await conn.fetchval(
            "SELECT recorded_at > now() - interval '1 minute' FROM hr_delivery_bonus_events"
        )
        assert len((await service.portal_files(conn, 30, 31))["documents"]) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM activity_log WHERE description=$1",
            f"Final proof added after completion: {key}",
        )


@pytest.mark.asyncio
async def test_a_paid_below_floor_extension_records_a_forfeited_receipt(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 31, 5, total=1600000)
    await upload(pool, 31, "document_1")
    async with pool.acquire() as conn:
        assert [tuple(r.values()) for r in await events(conn, 31)] == [
            ("service_delivered", None, True)
        ]
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0


@pytest.mark.asyncio
async def test_the_visa_proof_starts_the_follow_up_and_needs_its_issue_date(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 32, 3)
    with pytest.raises(HTTPException):
        await service.upload(
            pool, 32, "electronic_visa", STAFF,
            final_file("electronic-visa").model_copy(update={"issued_on": None}),
            BackgroundTasks(), repair=True,
        )  # fmt: skip
    result = await upload(pool, 32, "electronic_visa", "electronic-visa")
    assert result["visa_followup"]["started_on"] == business_today()
    async with pool.acquire() as conn:
        assert (
            await conn.fetchval("SELECT evidence_issued_on FROM hr_delivery_bonus_events")
            is not None
        )


@pytest.mark.asyncio
async def test_proof_after_a_non_proof_slot_still_earns_at_the_bonus_slot(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 33, 3)
    await upload(pool, 33, "document_2")
    async with pool.acquire() as conn:
        assert await events(conn, 33) == []
        assert (await view(conn, 33))["bonus_instruction"] == "upload"
    await upload(pool, 33, "electronic_visa", "electronic-visa")
    async with pool.acquire() as conn:
        assert [r["event_kind"] for r in await events(conn, 33)] == ["electronic_visa_issued"]


@pytest.mark.asyncio
async def test_waiver_then_another_slot_earns_but_another_slot_then_waiver_does_not(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 34, 3)
        await practice(conn, 35, 3)
    assert (await view(pool, 34))["bonus_instruction"] == "upload_or_waive"
    waived = await service.waive(pool, 34, "electronic_visa", STAFF, "Issued at the border")
    assert waived["status"] == "completed" and waived["bonus_event"] is None
    assert waived["bonus_instruction"] is None
    async with pool.acquire() as conn:
        assert await events(conn, 34) == []
    await upload(pool, 34, "document_2")
    async with pool.acquire() as conn:
        assert [r["event_kind"] for r in await events(conn, 34)] == ["service_delivered"]
    # The other order: a non-proof document first, the waiver afterwards, then a third slot.
    await upload(pool, 35, "document_2")
    await service.waive(pool, 35, "electronic_visa", STAFF, "Issued at the border")
    await upload(pool, 35, "document_3")
    async with pool.acquire() as conn:
        assert await events(conn, 35) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["archive", "delete"])
async def test_a_proof_accepted_earlier_is_never_earned_by_a_later_upload(pool, how):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 36, 3)
        await accept_earlier(conn, 36, "electronic_visa", how=how)
        shown = await view(conn, 36)
        assert shown["slots"][0]["document_id"] is None and shown["slots"][0]["can_repair"]
        assert shown["bonus_instruction"] is None
    await upload(pool, 36, "electronic_visa", "electronic-visa")
    async with pool.acquire() as conn:
        assert await events(conn, 36) == []
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0
    # The same for a service without a bonus slot: one accepted document is the proof.
    async with pool.acquire() as conn:
        await practice(conn, 37, 7)
        await accept_earlier(conn, 37, "document_1", how=how, doc_name="gone")
    await upload(pool, 37, "document_1")
    async with pool.acquire() as conn:
        assert await events(conn, 37) == []


@pytest.mark.asyncio
async def test_a_pre_existing_filled_slot_without_history_earns_nothing_for_a_new_upload(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 38, 3)
    await service.waive(pool, 38, "electronic_visa", STAFF, "Issued at the border")
    async with pool.acquire() as conn:
        doc = await conn.fetchval(
            """INSERT INTO documents(practice_id,client_id,file_id,file_name,uploaded_source,status,
                   is_archived,client_visible,intake_idempotency_key,document_type,content_hash)
               VALUES(38,31,'f','f.pdf','team','active',false,true,'f','itk','f') RETURNING id"""
        )
        await conn.execute(
            "INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by)"
            " VALUES(38,'visa_b1_voa','document_2',$1,'seed')",
            doc,
        )
    await upload(pool, 38, "document_3")
    async with pool.acquire() as conn:
        assert await events(conn, 38) == []


@pytest.mark.asyncio
async def test_a_receipt_or_a_ledger_row_suppresses_earning_but_not_filling(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 40, 3)
        await practice(conn, 41, 3)
        await practice(conn, 42, 4)
        await conn.execute(
            """INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr,awarded_at,status)
               VALUES(41,1,'visa_b1_voa',20000,now(),'pending'),(42,1,'ext_b1_voa',15000,now(),'pending')"""
        )
    await upload(pool, 40, "electronic_visa", "electronic-visa")
    await upload(pool, 40, "document_2")
    await upload(pool, 41, "document_2")
    await upload(pool, 41, "electronic_visa", "electronic-visa")
    shown = await upload(pool, 42, "document_1")
    assert shown["slots"][0]["document_id"] and shown["bonus_event"] is None
    async with pool.acquire() as conn:
        assert [r["event_kind"] for r in await events(conn, 40)] == ["electronic_visa_issued"]
        assert await events(conn, 41) == [] and await events(conn, 42) == []
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 3
        assert (await view(conn, 41))["bonus_instruction"] is None


@pytest.mark.asyncio
async def test_a_legacy_ledger_row_still_starts_the_permit_clock_at_acceptance(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 43, 3)
        await conn.execute(
            """INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr,awarded_at,status)
               VALUES(43,1,'visa_b1_voa',20000,now(),'pending')"""
        )
    result = await upload(pool, 43, "electronic_visa", "electronic-visa")
    assert result["bonus_event"] is None
    assert result["visa_followup"]["started_on"] == business_today()
    assert (result["visa_followup"]["due_on"] - result["visa_followup"]["started_on"]).days == 30


@pytest.mark.asyncio
async def test_an_existing_permit_clock_or_receipt_is_preserved(pool):  # noqa: F811
    old = business_today() - timedelta(days=10)
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 44, 3)
        await practice(conn, 45, 3)
        await conn.execute(
            """INSERT INTO hr_bonus_ledger(practice_id,employee_id,practice_type_code,amount_idr,awarded_at,status)
               VALUES(44,1,'visa_b1_voa',20000,now(),'pending')"""
        )
        await conn.execute(
            """INSERT INTO practice_visa_followups(practice_id,service_code,slot_key,started_on,due_on,allow_postpone)
               VALUES(44,'visa_b1_voa','document_2',$1::date,$1::date+30,true)""",
            old,
        )
        # A receipt already recorded (its slot document gone): the clock starts at the receipt.
        await conn.execute(
            """INSERT INTO hr_delivery_bonus_events(practice_id,employee_id,service_code,event_kind,
                   earned_at,awarded_at,recorded_at) VALUES(45,1,'visa_b1_voa','electronic_visa_issued',
                   now(),now(),now() - interval '4 days')"""
        )
    kept = await upload(pool, 44, "electronic_visa", "electronic-visa")
    assert kept["visa_followup"]["started_on"] == old
    receipt = await upload(pool, 45, "electronic_visa", "electronic-visa")
    assert receipt["visa_followup"]["started_on"] == business_today() - timedelta(days=4)
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events") == 1


@pytest.mark.asyncio
async def test_a_double_submit_creates_one_receipt(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 46, 3)
    results = await asyncio.gather(
        *[upload(pool, 46, "electronic_visa", "electronic-visa") for _ in range(2)],
        return_exceptions=True,
    )
    # A losing racer may be refused as a conflict; nothing else is acceptable.
    assert any(isinstance(r, dict) for r in results)
    assert all(
        isinstance(r, dict) or (isinstance(r, HTTPException) and r.status_code == 409)
        for r in results
    ), results
    async with pool.acquire() as conn:
        assert len(await events(conn, 46)) == 1
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM practice_delivery_slots WHERE practice_id=46")
            == 1
        )
        assert await conn.fetchval("SELECT count(*) FROM documents WHERE practice_id=46") == 1


@pytest.mark.asyncio
async def test_a_failure_before_commit_leaves_nothing(pool, monkeypatch):  # noqa: F811
    async def boom(*args, **kwargs):
        raise RuntimeError("injected")

    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 47, 3)
    monkeypatch.setattr(service, "record_visa_milestone", boom)
    with pytest.raises(RuntimeError, match="injected"):
        await upload(pool, 47, "electronic_visa", "electronic-visa")
    async with pool.acquire() as conn:
        assert await events(conn, 47) == []
        assert await conn.fetchval("SELECT count(*) FROM practice_delivery_slots") == 0
        assert await conn.fetchval("SELECT count(*) FROM hr_bonus_ledger") == 0
        assert await conn.fetchval("SELECT count(*) FROM activity_log") == 0


@pytest.mark.asyncio
async def test_a_filled_or_waived_slot_is_not_offered_and_not_replaceable(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 48, 3)
    await service.waive(pool, 48, "electronic_visa", STAFF, "Issued at the border")
    shown = await view(pool, 48)
    assert [s["can_repair"] for s in shown["slots"]] == [False, True, True]
    await upload(pool, 48, "document_2")
    for key, name in (("electronic_visa", "late"), ("document_2", "other-itk")):
        with pytest.raises(HTTPException):
            await upload(pool, 48, key, name)
    shown = await view(pool, 48)
    assert [s["can_repair"] for s in shown["slots"]] == [False, False, True]


@pytest.mark.asyncio
async def test_custom_titles_can_be_set_on_a_cohort_practice_and_its_first_document_earns(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 49, 8)
        await practice(conn, 50, 8, completed=datetime(2026, 10, 20, tzinfo=UTC))
    assert (await view(pool, 49))["needs_titles"]
    shown = await service.set_titles(pool, 49, STAFF, ["Deed", "Receipt"])
    assert not shown["needs_titles"] and shown["slots"][0]["can_repair"]
    with pytest.raises(HTTPException):
        await service.set_titles(pool, 50, STAFF, ["Deed"])
    result = await upload(pool, 49, "custom_2", "receipt")
    assert result["bonus_event"] == "service_delivered" and result["bonus_status"] == "no_bonus"


@pytest.mark.asyncio
async def test_a_combined_kitas_earns_at_the_imk_slot_whichever_comes_first(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 51, 2)
    await upload(pool, 51, "document_2")
    async with pool.acquire() as conn:
        assert await events(conn, 51) == []
    await upload(pool, 51, "document_1")
    async with pool.acquire() as conn:
        assert [r["event_kind"] for r in await events(conn, 51)] == ["kitas_imk_uploaded"]


@pytest.mark.asyncio
async def test_the_card_instruction_is_only_for_a_practice_that_can_still_earn(pool):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        for pid, type_id in ((52, 3), (53, 4)):
            await practice(conn, pid, type_id)
        shown = {pid: (await view(conn, pid))["bonus_instruction"] for pid in (52, 53)}
    # A service with no bonus slot has nothing to instruct: any first document earns.
    assert shown == {52: "upload_or_waive", 53: None}
    await upload(pool, 52, "electronic_visa", "electronic-visa")
    assert (await view(pool, 52))["bonus_instruction"] is None


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


@pytest.mark.asyncio
async def test_391_waits_for_a_concurrent_change_and_then_sees_it(pool):  # noqa: F811
    async with pool.acquire() as setup:
        await seed_f4(setup)
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = writer.transaction()
        await tx.start()
        await writer.execute("UPDATE documents SET is_archived=true WHERE id=21162")
        run = asyncio.create_task(migrator.execute(FORWARD))
        await asyncio.sleep(0.5)
        assert not run.done()  # blocked on the proof document, not validating stale facts
        await tx.commit()
        await asyncio.wait_for(run, 10)
        listed = {
            r["practice_id"]
            for r in await migrator.fetch("SELECT practice_id FROM hr_delivery_bonus_events")
        }
    assert listed == set(LISTED) - {1378}


@pytest.mark.asyncio
async def test_391_holds_its_facts_until_it_commits(pool):  # noqa: F811
    async with pool.acquire() as setup:
        await seed_f4(setup)
    async with pool.acquire() as writer, pool.acquire() as migrator:
        tx = migrator.transaction()
        await tx.start()
        await migrator.execute(FORWARD)
        change = asyncio.create_task(
            writer.execute("UPDATE documents SET is_archived=true WHERE id=21162")
        )
        await asyncio.sleep(0.5)
        assert not change.done()  # the change cannot slip in between validation and insert
        await tx.commit()
        await asyncio.wait_for(change, 10)
        assert await writer.fetchval("SELECT count(*) FROM hr_delivery_bonus_events") == len(LISTED)
