import base64
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from backend.app.routers.crm_practice_delivery import DeliveryFile
from backend.services.crm import practice_delivery as service

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
    await service.upload(
        pool, 7, "electronic_visa", STAFF, request(date(2026, 1, 15)), BackgroundTasks()
    )
    writes["record_delivery_bonus"].assert_not_awaited()


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
        writes["record_delivery_bonus"].assert_awaited_once_with(
            conn, 7, "visa_c1_tourism", STAFF["email"], event
        )
    else:
        writes["record_delivery_bonus"].assert_not_awaited()
    cmds = [call.args[0] for call in conn.execute.await_args_list]
    assert "INSERT INTO practice_delivery_completions" in cmds[0] and "UPDATE practices" in cmds[1]


@pytest.mark.asyncio
async def test_completed_retry_returns_receipt_and_preserves_history(db, writes):
    pool, conn = db
    conn.fetchrow.return_value = {**PRACTICE, "status": "completed"}
    conn.fetchval.return_value = 1
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
        service.upload_marker(7, None, encoded, "file.pdf", mime)


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
@pytest.mark.parametrize("issued_on", [None, date.today() + timedelta(days=3)])
async def test_first_issuance_requires_official_bali_issue_date_before_storage(
    db, writes, issued_on
):
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
