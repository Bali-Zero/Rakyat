import base64
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers.crm_practice_delivery import DeliveryFile
from backend.services.crm import practice_delivery as service
from backend.services.crm.delivery_progress import DeliveryProgress

STAFF = {"email": "consultant@example.test", "role": "consultant"}
PRACTICE = {
    "id": 7,
    "code": "visa_c1_tourism",
    "name": "C1",
    "category": "single_entry_visa",
    "client_id": 31,
    "family_member_id": None,
    "client_visible": True,
    "assigned_to": STAFF["email"],
    "payment_status": "paid",
    "status": "on_process",
}


@pytest.fixture
def db():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=dict(PRACTICE))
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    return pool, conn


def issued_policy(code):
    return {
        "slots": [
            {"key": "electronic_visa", "label": "Electronic Visa", "condition": None},
            {"key": "itk", "label": "ITK", "condition": None},
        ],
        "bonus_slot": "electronic_visa",
        "bonus_trigger": "electronic_visa_issued",
        "followup_slot": "itk",
        "can_postpone": True,
    }


@pytest.fixture
def writes(monkeypatch):
    monkeypatch.setattr(service, "delivery_policy", issued_policy)
    mocks = {}
    for name in (
        "stage",
        "audit",
        "invalidate_delivery_cache",
        "publish",
        "get_delivery_bonus",
        "record_delivery_bonus",
        "get_followup",
        "record_visa_milestone",
        "legacy_cohort",  # these cases are about the ordinary paths; the cohort has its own pg suite
    ):
        mocks[name] = AsyncMock(return_value=None)
        monkeypatch.setattr(service, name, mocks[name])
    return mocks


def request(issued_on=None):
    return DeliveryFile(
        file_name="issued.pdf",
        issued_on=issued_on,
        mime_type="application/pdf",
        file_content=base64.b64encode(b"%PDF-1.7\nissued").decode(),
    )


@pytest.mark.parametrize(
    "status,payment",
    [("sending_invoice", "unpaid"), ("on_process", "partial"), ("on_process", "unpaid")],
)
@pytest.mark.asyncio
async def test_upload_cannot_start_or_pay_a_practice(db, writes, status, payment):
    pool, conn = db
    conn.fetchrow.return_value = {**PRACTICE, "status": status, "payment_status": payment}
    with pytest.raises(HTTPException) as error:
        await service.upload(
            pool, 7, "electronic_visa", STAFF, request(date(2026, 1, 15)), BackgroundTasks()
        )
    assert error.value.status_code == 409
    writes["stage"].assert_not_awaited()


@pytest.mark.parametrize(
    "user",
    [
        {"role": "client", "email": STAFF["email"]},
        {"role": "accounting", "email": STAFF["email"]},
        {"role": "tax consultant", "email": "another@example.test"},
    ],
)
@pytest.mark.asyncio
async def test_only_authorised_delivery_operators(db, user):
    with pytest.raises(HTTPException):
        await service.practice(db[1], 7, user)


@pytest.mark.asyncio
async def test_issuance_earns_bonus_while_post_entry_task_remains_open(db, writes, monkeypatch):
    pool, conn = db
    monkeypatch.setattr(
        service,
        "staged_file",
        AsyncMock(return_value={"document_id": 9, "company_document_id": None}),
    )
    result = await service.upload(
        pool, 7, "electronic_visa", STAFF, request(date(2026, 1, 15)), BackgroundTasks()
    )
    writes["record_delivery_bonus"].assert_awaited_once_with(
        conn,
        7,
        "visa_c1_tourism",
        STAFF["email"],
        "electronic_visa_issued",
        issued_on=date(2026, 1, 15),
    )
    writes["publish"].assert_awaited_once_with(conn, PRACTICE, ["electronic_visa"])
    assert result["status"] == "on_process"
    writes["record_visa_milestone"].assert_awaited_once_with(conn, PRACTICE, "electronic_visa")
    assert result["slots"][1]["document_id"] is None
    assert not any("UPDATE practices" in call.args[0] for call in conn.execute.await_args_list)


@pytest.mark.asyncio
async def test_same_issuance_file_retry_does_not_requalify_bonus(db, writes, monkeypatch):
    pool, conn = db
    conn.fetch.return_value = [
        {
            "slot_key": "electronic_visa",
            "waiver_reason": None,
            "document_id": 9,
            "file_name": "issued.pdf",
        }
    ]
    monkeypatch.setattr(
        service,
        "staged_file",
        AsyncMock(return_value={"document_id": 9, "company_document_id": None}),
    )
    monkeypatch.setattr(service, "file_unchanged", AsyncMock(return_value=True))
    await service.upload(
        pool, 7, "electronic_visa", STAFF, request(date(2026, 1, 15)), BackgroundTasks()
    )
    writes["record_delivery_bonus"].assert_not_awaited()
    writes[
        "stage"
    ].assert_awaited_once()  # the lock phase returns the view; nothing else is written


@pytest.mark.asyncio
async def test_invalid_or_concurrent_destination_is_rechecked_after_storage(db, writes):
    db[1].fetchrow.side_effect = [PRACTICE, {**PRACTICE, "family_member_id": 55}]
    with pytest.raises(HTTPException) as error:
        await service.upload(db[0], 7, "itk", STAFF, request(), BackgroundTasks())
    assert error.value.status_code == 409
    writes["publish"].assert_not_awaited()
    writes["record_delivery_bonus"].assert_not_awaited()


@pytest.mark.asyncio
async def test_visa_waits_for_itk_and_cannot_waive_it(db, writes):
    pool, conn = db
    conn.fetch.return_value = [
        {
            "slot_key": "electronic_visa",
            "document_id": 9,
            "file_name": "issued.pdf",
            "waiver_reason": None,
        }
    ]
    with pytest.raises(HTTPException) as incomplete:
        await service.complete(pool, 7, STAFF, BackgroundTasks())
    assert incomplete.value.status_code == 409
    with pytest.raises(HTTPException) as required:
        await service.waive(pool, 7, "itk", STAFF, "Pending arrival")
    assert required.value.status_code == 409
    writes["record_delivery_bonus"].assert_not_awaited()


@pytest.mark.parametrize(
    "bonus_slot,waived,event",
    [
        ("electronic_visa", False, None),
        (None, False, "service_delivered"),
        ("electronic_visa", True, "service_delivered"),
    ],
)
@pytest.mark.asyncio
async def test_final_delivery_uses_correct_bonus_event(
    db, writes, monkeypatch, bonus_slot, waived, event
):
    pool, conn = db
    policy = issued_policy("")
    policy["bonus_slot"] = bonus_slot
    if waived:
        policy["slots"][0]["condition"] = "Only if issued on the booked route"
    monkeypatch.setattr(service, "delivery_policy", lambda _code: policy)
    instant = datetime(2026, 11, 30, 3, tzinfo=UTC)
    monkeypatch.setattr(
        service, "first_proof_instant", AsyncMock(return_value=(instant, "earlier@example.test"))
    )
    conn.fetch.return_value = [
        {
            "slot_key": "electronic_visa",
            "document_id": None if waived else 9,
            "file_name": "issued.pdf",
            "waiver_reason": "Domestic conversion" if waived else None,
        },
        {"slot_key": "itk", "document_id": 10, "file_name": "permit.pdf", "waiver_reason": None},
    ]
    result = await service.complete(pool, 7, STAFF, BackgroundTasks())
    assert result["status"] == "completed"
    if event:
        # Earned when the proof first existed, for whoever held the practice then.
        writes["record_delivery_bonus"].assert_awaited_once_with(
            conn, 7, "visa_c1_tourism", "earlier@example.test", event, earned_at=instant
        )
    else:
        writes["record_delivery_bonus"].assert_not_awaited()
    cmds = [call.args[0] for call in conn.execute.await_args_list]
    assert "INSERT INTO practice_delivery_completions" in cmds[0] and "UPDATE practices" in cmds[1]


@pytest.mark.asyncio
async def test_completed_retry_returns_receipt_and_preserves_history(db, writes):
    pool, conn = db
    conn.fetchrow.return_value = {**PRACTICE, "status": "completed"}
    conn.fetchval.return_value = PRACTICE["code"]
    tasks = BackgroundTasks()
    assert (await service.complete(pool, 7, STAFF, tasks))["status"] == "completed"
    assert not tasks.tasks
    writes["record_delivery_bonus"].assert_not_awaited()


@pytest.mark.asyncio
async def test_portal_list_requires_recipient_and_published_live_document(db):
    conn = db[1]
    with pytest.raises(HTTPException) as error:
        await service.portal_files(conn, 7, 789)
    assert error.value.status_code == 404
    conn.fetchval.return_value = True
    conn.fetch.return_value = [{"id": 12, "file_name": "issued.pdf"}]
    assert await service.portal_files(conn, 7, 31) == {
        "documents": [{"id": 12, "file_name": "issued.pdf"}]
    }
    sql, *params = conn.fetch.await_args.args
    assert params == [7, 31]
    assert (
        "d.client_visible = true" in sql
        and "d.deleted_at IS NULL" in sql
        and "d.family_member_id IS NOT DISTINCT FROM p.family_member_id" in sql
    )


@pytest.mark.parametrize(
    "encoded,mime", [("!", "application/pdf"), ("", "application/pdf"), ("eQ==", "text/html")]
)
def test_reject_invalid_final_file(encoded, mime):
    with pytest.raises(HTTPException):
        service.upload_digest(encoded, "file.pdf", mime)


@pytest.mark.asyncio
async def test_combined_permit_can_only_reuse_its_same_practice_partner(db, monkeypatch):
    pool, conn = db
    monkeypatch.setattr(service, "get_delivery_bonus", AsyncMock(return_value=None))
    p = {**PRACTICE, "code": "kitas_working_extend"}
    conn.fetchrow.return_value = p
    conn.fetch.return_value = [
        {
            "slot_key": "document_1",
            "waiver_reason": None,
            "document_id": 9,
            "file_name": "combined.pdf",
        }
    ]
    view = await service.state(conn, p)
    first, second, manpower = view["slots"]
    assert second["reusable_document_id"] == first["document_id"] == 9
    assert manpower["reusable_document_id"] is None
    with pytest.raises(HTTPException):
        await service.reuse(pool, 7, second["key"], STAFF, 97)
    with pytest.raises(HTTPException):
        await service.reuse(pool, 7, manpower["key"], STAFF, 9)
    await service.reuse(pool, 7, second["key"], STAFF, 9)
    assert any(
        "INSERT INTO practice_delivery_slots" in call.args[0]
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_custom_needs_named_outputs_and_cannot_close_empty(db, monkeypatch):
    pool, conn = db
    monkeypatch.setattr(service, "get_delivery_bonus", AsyncMock(return_value=None))
    conn.fetchrow.return_value = {**PRACTICE, "code": "other_custom"}
    p = await service.practice(conn, 7, STAFF)
    assert (await service.state(conn, p))["needs_titles"]
    with pytest.raises(HTTPException):
        await service.complete(pool, 7, STAFF, BackgroundTasks())
    conn.fetchval.return_value = ["Final advice", "Filing receipt"]
    slots = (await service.state(conn, p))["slots"]
    assert [(s["key"], s["label"]) for s in slots] == [
        ("custom_1", "Final advice"),
        ("custom_2", "Filing receipt"),
    ]


@pytest.mark.parametrize(
    "label,kind",
    [
        ("ITK — Visit Stay Permit · after confirmed entry", "itk"),
        ("Renewed e-ITAS — Limited Stay Permit", "itas"),
        ("Renewed IMK — Re-entry Permit", "imk"),
        ("e-ITAP — Permanent Stay Permit", "itap"),
    ],
)
def test_immigration_outputs_retain_semantic_document_type(label, kind):
    assert service.document_type("document_1", label) == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("days_ahead", [None, 3])
async def test_first_issuance_requires_official_bali_issue_date_before_storage(
    db, writes, days_ahead
):
    issued_on = None if days_ahead is None else date.today() + timedelta(days=days_ahead)
    with pytest.raises(HTTPException) as invalid:
        await service.upload(
            db[0], 7, "electronic_visa", STAFF, request(issued_on), BackgroundTasks()
        )
    assert invalid.value.status_code == 422
    writes["stage"].assert_not_awaited()


def test_replacement_keeps_first_issuance_date():
    actual = date(2026, 1, 15)
    assert (
        service.issue_date(
            {"bonus_slot": "electronic_visa"}, {"key": "electronic_visa", "issued_on": actual}, None
        )
        == actual
    )
    assert (
        service.issue_date(
            {"bonus_slot": "electronic_visa"},
            {"key": "electronic_visa", "issued_on": actual},
            date(2026, 1, 29),
        )
        == actual
    )


@pytest.mark.asyncio
async def test_retired_stage_recovers_under_a_fresh_key(db):
    _, conn = db
    conn.fetchval.return_value = 45
    assert (
        await service.recoverable_marker(conn, PRACTICE, "case-delivery:original")
        == "case-delivery:original:45"
    )
    sql, *args = conn.fetchval.await_args.args
    assert "deleted_at IS NULL" in sql and "is_archived" in sql
    assert args == [PRACTICE["client_id"], "case-delivery:original", None]


@pytest.mark.asyncio
async def test_ordinary_staff_output_adoption_requires_same_visible_subject_and_document_type(db):
    _, conn = db
    await service.existing_output(
        conn, PRACTICE, {"key": "electronic_visa", "label": "Electronic Visa"}, request()
    )
    sql, *args = conn.fetchrow.await_args.args
    assert (
        "d.practice_id = p.id" in sql
        and "d.family_member_id IS NOT DISTINCT FROM p.family_member_id" in sql
    )
    assert (
        "d.uploaded_source='team'" in sql
        and "d.client_visible = true" in sql
        and "d.content_hash=$3" in sql
    )
    assert args[-1] == "evisa"


@pytest.mark.parametrize(
    "label,kind",
    [
        ("Akta Pendirian", "akta_pendirian"),
        ("SK Pengesahan AHU", "sk_decree"),
        ("NIB", "nib"),
        ("Company NPWP", "npwp"),
    ],
)
def test_company_outputs_populate_the_existing_company_vault_cards(label, kind):
    assert service.document_type("document_2", label) == kind


@pytest.mark.asyncio
async def test_same_service_reopened_practice_keeps_original_delivery_receipt(db, writes):
    pool, conn = db
    answers = iter([PRACTICE["code"], None, True])

    async def lookup(sql, *args):
        # The delivery view's bonus rate is not part of this completion sequence.
        return None if "hr_bonus_rates" in sql else next(answers)

    conn.fetchval.side_effect = lookup
    conn.fetch.return_value = [
        {"slot_key": key, "document_id": i, "file_name": "result.pdf", "waiver_reason": None}
        for i, key in enumerate(("electronic_visa", "itk"), 1)
    ]
    result = await service.complete(pool, 7, STAFF, BackgroundTasks())
    assert result["status"] == "completed"
    assert not any(
        "INSERT INTO practice_delivery_completions" in c.args[0]
        for c in conn.execute.await_args_list
    )
    writes["record_delivery_bonus"].assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_conflict", [True, False])
async def test_other_slot_content_conflict_cannot_publish_or_qualify(
    db, writes, monkeypatch, first_conflict
):
    pool, conn = db
    checks = []

    async def lookup(sql, *args):
        if "practice_delivery_fingerprints" in sql:
            checks.append(args)
            return first_conflict or len(checks) == 2
        return None

    conn.fetchval.side_effect = lookup
    monkeypatch.setattr(
        service,
        "staged_file",
        AsyncMock(return_value={"document_id": 9, "company_document_id": None}),
    )
    with pytest.raises(HTTPException) as refused:
        await service.upload(
            pool, 7, "electronic_visa", STAFF, request(date(2026, 1, 15)), BackgroundTasks()
        )
    assert refused.value.status_code == 409
    assert len(checks) == (1 if first_conflict else 2)
    assert checks[0][0] == 7 and checks[0][2:] == ("visa_c1_tourism", "electronic_visa")
    writes["record_delivery_bonus"].assert_not_awaited()
    writes["publish"].assert_not_awaited()
    if first_conflict:
        writes["stage"].assert_not_awaited()
    else:
        assert "electronic_visa:" in writes["stage"].await_args.args[-2]


@pytest.mark.asyncio
async def test_staged_document_lookup_matches_kind_and_practice_subject(db):
    await service.staged_file(
        db[1], PRACTICE, None, "case-delivery:7:0:document_2:test", "itk", lock=True
    )
    sql, *args = db[1].fetchrow.await_args.args
    assert "d.document_type=$4" in sql and "d.practice_id = p.id" in sql
    assert (
        "d.client_id = p.client_id" in sql
        and "d.family_member_id IS NOT DISTINCT FROM p.family_member_id" in sql
    )
    assert args[-1] == "itk"


@pytest.mark.asyncio
async def test_renewed_imk_rewards_upload_before_completion_without_second_followup(
    db, writes, monkeypatch
):
    from backend.services.crm.delivery_catalog import delivery_policy

    pool, conn = db
    practice = {**PRACTICE, "code": "kitas_working_extend"}
    conn.fetchrow.return_value = practice
    monkeypatch.setattr(service, "delivery_policy", delivery_policy)
    monkeypatch.setattr(
        service,
        "staged_file",
        AsyncMock(return_value={"document_id": 9, "company_document_id": None}),
    )
    result = await service.upload(
        pool, practice["id"], "document_1", STAFF, request(), BackgroundTasks()
    )
    writes["record_delivery_bonus"].assert_awaited_once_with(
        conn,
        practice["id"],
        practice["code"],
        STAFF["email"],
        "kitas_imk_uploaded",
        issued_on=None,
    )
    writes["record_visa_milestone"].assert_not_awaited()
    assert result["status"] == "on_process"
    assert result["visa_followup"] is None


@pytest.mark.asyncio
async def test_explicit_combined_permit_can_satisfy_renewal_bonus_slot(db, writes, monkeypatch):
    from backend.services.crm.delivery_catalog import delivery_policy

    pool, conn = db
    p = {**PRACTICE, "code": "kitas_investor_2yr_extend"}
    conn.fetchrow.return_value = p
    conn.fetch.return_value = [
        {
            "slot_key": "document_2",
            "document_id": 42,
            "file_name": "combined.pdf",
            "waiver_reason": None,
            "issued_on": None,
        }
    ]
    monkeypatch.setattr(service, "delivery_policy", delivery_policy)
    await service.reuse(pool, p["id"], "document_1", STAFF, 42)
    writes["record_delivery_bonus"].assert_awaited_once_with(
        conn,
        p["id"],
        p["code"],
        STAFF["email"],
        "kitas_imk_uploaded",
        issued_on=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,completion,document,waiver,expected",
    [
        ("completed", True, None, None, True),
        ("completed", True, 12, None, False),
        # Completed before Final delivery existed: no completion record.
        ("completed", False, None, None, True),
        ("completed", True, None, "Out of scope", False),
        ("on_process", False, None, None, False),
    ],
)
async def test_completed_slot_repair_projection_requires_missing_permit_on_completed_practice(
    db, writes, status, completion, document, waiver, expected
):
    # A delivered practice takes every empty, unwaived slot; any other completed one only a permit.
    conn = db[1]
    conn.fetchval.return_value = completion
    conn.fetch.return_value = [
        {"slot_key": "itk", "document_id": document, "file_name": None, "waiver_reason": waiver}
    ]
    writes["get_delivery_bonus"].return_value = {
        "event_kind": "electronic_visa_issued",
        "status": "ineligible",
    }
    view = await service.state(conn, {**PRACTICE, "status": status})
    assert view["delivery_recorded"] is (status == "completed" and completion)
    assert view["slots"][1]["can_repair"] is expected
    assert view["slots"][0]["can_repair"] is bool(status == "completed" and completion)
    assert view["bonus_status"] == "ineligible" and "amount_idr" not in view


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [True, False])
async def test_repair_publishes_only_missing_permit_and_never_triggers_issuance_or_completion(
    db, writes, monkeypatch, recorded
):
    pool, conn = db
    conn.fetchrow.return_value = {**PRACTICE, "status": "completed"}

    async def value(sql, *args):
        return True if recorded and "practice_delivery_completions" in sql else None

    conn.fetchval.side_effect = value
    monkeypatch.setattr(service, "previous_slot", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "staged_file",
        AsyncMock(return_value={"document_id": 28, "company_document_id": None}),
    )
    tasks = BackgroundTasks()
    result = await service.upload(pool, 7, "itk", STAFF, request(), tasks, repair=True)
    assert result["status"] == "completed" and not tasks.tasks
    writes["publish"].assert_awaited_once()
    assert writes["publish"].await_args.args[-1] == ["itk"]
    writes["record_delivery_bonus"].assert_not_awaited()
    assert all(
        "UPDATE practices SET" not in c.args[0]
        and "INSERT INTO timeline_events" not in c.args[0]
        and "practice_delivery_completions" not in c.args[0]
        for c in conn.execute.await_args_list
    )
    assert ("added after completion" if recorded else "repaired") in writes[
        "audit"
    ].await_args.args[-1]


@pytest.mark.asyncio
async def test_replaced_company_identity_comes_from_old_slot_and_respects_other_valid_refs(db):
    conn = db[1]
    await service.retire_replaced_file(
        conn, PRACTICE, {"document_id": 19, "company_document_id": 93, "company_id": 81}
    )
    sql, *params = conn.execute.await_args.args
    assert params == [7, 19, 93, 81]
    assert "cd.id=$3 AND cd.company_id=$4" in sql and "cd.google_drive_file_id=d.file_id" in sql
    assert "s.company_document_id=cd.id AND s.company_id=cd.company_id" in sql


# ── completion timeline text and the wording helper ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "wording"),
    [
        (DeliveryProgress(True, True, False), "partial"),
        (DeliveryProgress(False, True, False), "partial"),
        (DeliveryProgress(False, False, False), "neutral"),
        (DeliveryProgress(False, True, True), "full"),
        (None, "legacy"),
    ],
)
async def test_completion_wording_follows_the_helper(db, monkeypatch, answer, wording):
    monkeypatch.setattr(service, "documents_pending", AsyncMock(return_value=answer))
    assert await service.completion_wording(db[1], 7) == wording


@pytest.mark.asyncio
async def test_completion_wording_failure_is_neutral_never_full(db, monkeypatch):
    monkeypatch.setattr(service, "documents_pending", AsyncMock(side_effect=RuntimeError("x")))
    assert await service.completion_wording(db[1], 7) == "neutral"


# ── content identity, the earning instant and the visa clock of a delivered practice ──────────────


@pytest.mark.parametrize(
    ("key", "event", "clock"),
    [
        ("electronic_visa", None, False),  # no issuance receipt: nothing to anchor, never a 409
        ("electronic_visa", "electronic_visa_issued", True),
        ("itk", None, True),  # the permit slot only refreshes the follow-up
        ("itk", "service_delivered", True),
    ],
)
@pytest.mark.asyncio
async def test_no_visa_clock_for_evisa_without_receipt(db, writes, key, event, clock):
    view = {"bonus_trigger": "electronic_visa_issued", "bonus_event": event}
    await service.delivered_visa_clock(db[1], PRACTICE, view, key)
    assert writes["record_visa_milestone"].await_count == int(clock)


@pytest.mark.asyncio
async def test_a_delivered_service_without_the_visa_trigger_never_starts_a_clock(db, writes):
    view = {"bonus_trigger": "service_delivered", "bonus_event": None}
    await service.delivered_visa_clock(db[1], PRACTICE, view, "electronic_visa")
    writes["record_visa_milestone"].assert_not_awaited()


T1, T2, T3 = (datetime(2026, 11, d, 4, tzinfo=UTC) for d in (20, 25, 29))
NOW_ISH = datetime.now(UTC)


def accepted(at, key):
    return {"performed_at": at, "accepted": key, "waived": None}


def waived(at, key):
    return {"performed_at": at, "accepted": None, "waived": key}


def moved(at, to):
    return {"performed_at": at, "assignee": to}


async def instant(
    db, history, moves, *, bonus_slot="electronic_visa", assignee="current@example.test"
):
    conn = db[1]
    conn.fetch.side_effect = [history, moves]
    policy = {**issued_policy(""), "bonus_slot": bonus_slot}
    return await service.first_proof_instant(conn, {**PRACTICE, "assigned_to": assignee}, policy)


@pytest.mark.parametrize(
    ("history", "bonus_slot", "expected"),
    [
        # The bonus slot is the proof: an earlier other document is not.
        ([accepted(T1, "itk"), accepted(T2, "electronic_visa")], "electronic_visa", T2),
        # A waiver of the bonus slot makes an earlier upload the proof, at the waiver.
        ([accepted(T1, "itk"), waived(T2, "electronic_visa")], "electronic_visa", T2),
        # A waiver alone proves nothing.
        ([waived(T1, "electronic_visa")], "electronic_visa", None),
        # An upload accepted after the waiver is the proof, and a later acceptance of the waived key un-waives it.
        ([waived(T1, "electronic_visa"), accepted(T2, "itk")], "electronic_visa", T2),
        ([waived(T1, "electronic_visa"), accepted(T2, "electronic_visa")], "electronic_visa", T2),
        # No bonus slot: any acceptance, whatever its key.
        ([accepted(T2, "document_4"), accepted(T3, "document_1")], None, T2),
        # Rows of the same instant are one event: the acceptance clears the waiver of its key.
        ([waived(T1, "electronic_visa"), accepted(T1, "electronic_visa")], "electronic_visa", T1),
        ([accepted(T1, "itk"), waived(T1, "electronic_visa")], "electronic_visa", T1),
        # No rows at all: now.
        ([], "electronic_visa", None),
    ],
)
@pytest.mark.asyncio
async def test_first_proof_instant_folds_acceptances_and_waivers(db, history, bonus_slot, expected):
    when, _ = await instant(db, history, [], bonus_slot=bonus_slot)
    if expected is None:
        assert (datetime.now(UTC) - when).total_seconds() < 60
    else:
        assert when == expected


@pytest.mark.parametrize(
    ("moves", "who"),
    [
        ([], "current@example.test"),  # never reassigned: the current assignee held it
        ([moved(T1, "early@example.test")], "current@example.test"),  # nothing after T
        ([moved(T1, "early@example.test"), moved(T3, "late@example.test")], "early@example.test"),
        ([moved(T2, "at@example.test"), moved(T3, "late@example.test")], "at@example.test"),
        ([moved(T3, "late@example.test")], None),  # the value before the change is not recorded
        ([moved(T1, "early@example.test"), moved(T3, None)], "early@example.test"),
        ([moved(T1, None), moved(T3, "late@example.test")], None),  # an empty value
    ],
)
@pytest.mark.asyncio
async def test_first_proof_instant_names_the_assignee_at_that_instant(db, moves, who):
    # The proof is at T2.
    _, assignee = await instant(db, [accepted(T2, "electronic_visa")], moves)
    assert assignee == who
