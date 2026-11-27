"""B2a: a November practice completed before Final delivery earns when its proof is uploaded.

Runs the real delivery service on an isolated PostgreSQL schema (the
fixture of the delivery suite). Only the file sink and the cache invalidation are substituted.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.services.hr.hr_service import business_today
from backend.services.crm import practice_delivery as service
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

EMAIL = STAFF["email"]
NOVEMBER = datetime(2026, 11, 20, 4, tzinfo=UTC)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("visa_b1_voa", "upload_or_waive"),
        ("visa_c1_tourism", "upload"),
        ("kitas_working_extend", "upload"),
    ],
)
async def test_the_waiver_is_promised_only_for_a_conditional_bonus_slot(pool, code, expected):  # noqa: F811
    async with pool.acquire() as conn:
        await types(conn)
        type_id = await conn.fetchval("SELECT id FROM practice_types WHERE code=$1", code)
        await practice(conn, 60, type_id)
        shown = await view(conn, 60)
        bonus = next(s for s in shown["slots"] if s["key"] == shown["bonus_slot"])
    assert shown["bonus_instruction"] == expected
    assert (expected == "upload_or_waive") is bool(bonus["condition"])
    if expected == "upload":
        with pytest.raises(HTTPException):
            await service.waive(pool, 60, bonus["key"], STAFF, "Not in this order")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "then"),
    [
        (("document_1", "upload"), ("document_2", "upload")),
        (("document_2", "upload"), ("document_1", "upload")),
        (("document_1", "upload"), ("document_2", "reuse")),
        (("document_2", "upload"), ("document_1", "reuse")),
    ],
)
async def test_a_combined_permit_earns_exactly_once_whichever_slot_comes_first(  # noqa: F811
    pool, first, then
):
    async with pool.acquire() as conn:
        await types(conn)
        await practice(conn, 61, 2)
    shown = None
    for key, how in (first, then):
        if how == "upload":
            shown = await upload(pool, 61, key, f"permit-{key}")
        else:
            donor = next(s for s in shown["slots"] if s["document_id"])["document_id"]
            shown = await service.reuse(pool, 61, key, STAFF, donor)
    async with pool.acquire() as conn:
        assert [r["event_kind"] for r in await events(conn, 61)] == ["kitas_imk_uploaded"]
        assert await conn.fetchval("SELECT count(*) FROM hr_delivery_bonus_events") == 1
    if then[1] == "reuse":
        assert shown["slots"][0]["document_id"] == shown["slots"][1]["document_id"]
