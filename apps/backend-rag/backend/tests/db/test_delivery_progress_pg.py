"""Which final documents the client can see, and what each consumer tells them.

Runs on the real test PostgreSQL of test_kita_document_delivery_pg (TEST_DATABASE_URL only).
The listener case goes through the real trigger path: practices UPDATE -> notify_practice_change
-> events_outbox payload -> PracticeStatusListener._handle_notification, with only the client
fetch and the send stubbed.
"""

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from backend.app.routers.portal_matters import _shape_matter_detail
from backend.services.crm import delivery_progress as progress
from backend.services.crm import practice_delivery as service
from backend.services.crm.delivery_progress import documents_pending, wording_of
from backend.services.crm.practice_status_listener import PracticeStatusListener
from backend.tests.db.test_kita_document_delivery_pg import STAFF, final_file, pool  # noqa: F401

C1 = "visa_c1_tourism"
FOLDER = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"


async def add_doc(conn, practice=7, *, client=31, family=None, **flags):
    flags = {"visible": True, "trashed": False, "archived": False, "file_id": "f1", **flags}
    return await conn.fetchval(
        """INSERT INTO documents(practice_id,client_id,family_member_id,file_id,file_name,
               uploaded_source,status,client_visible,deleted_at,is_archived)
           VALUES($1,$2,$3,$4,'proof.pdf','team','active',$5,CASE WHEN $6 THEN now() END,$7)
           RETURNING id""",
        practice,
        client,
        family,
        flags["file_id"],
        flags["visible"],
        flags["trashed"],
        flags["archived"],
    )


async def fill(conn, key, doc_id, practice=7, code=C1):
    await conn.execute(
        """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,document_id,updated_by)
           VALUES($1,$2,$3,$4,'staff')""",
        practice,
        code,
        key,
        doc_id,
    )


async def waive(conn, key, practice=7, code=C1):
    await conn.execute(
        """INSERT INTO practice_delivery_slots(practice_id,service_code,slot_key,waiver_reason,updated_by)
           VALUES($1,$2,$3,'not in this order','staff')""",
        practice,
        code,
        key,
    )


async def facts(conn, practice=7):
    found = await documents_pending(conn, practice)
    return None if found is None else (found.pending, found.accessible_any, found.accessible_all)


async def add_practice(conn, pid, code, *, visible=True, type_id=None):
    type_id = type_id or 100 + pid
    await conn.execute(
        "INSERT INTO practice_types VALUES($1,$2,$2,'other') ON CONFLICT DO NOTHING", type_id, code
    )
    await conn.execute(
        "INSERT INTO practices VALUES($1,31,null,'consultant@example.test',$2,'on_process','paid',$3,null,now())",
        pid,
        visible,
        type_id,
    )


# ── the helper ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_slot_delivered_and_visible_is_full(pool):
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn))
        assert await facts(conn) == (False, True, True)
        assert wording_of(await documents_pending(conn, 7)) == "full"


@pytest.mark.asyncio
async def test_five_practice_cutover_shape_evisa_filled_itk_empty_is_partial(pool):
    # Production: a C1 visa holding its published e-visa and an empty ITK slot.
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn))
        assert await facts(conn) == (True, True, False)
        assert wording_of(await documents_pending(conn, 7)) == "partial"


@pytest.mark.asyncio
async def test_one_hidden_file_makes_it_partial(pool):
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn, visible=False))
        assert await facts(conn) == (False, True, False)
        assert wording_of(await documents_pending(conn, 7)) == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["visible", "trashed", "archived"])
async def test_nothing_visible_is_neutral(pool, hidden):
    flag = {"visible": False} if hidden == "visible" else {hidden: True}
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn, **flag))
        await fill(conn, "document_2", await add_doc(conn, **flag))
        assert (await facts(conn))[1] is False
        assert wording_of(await documents_pending(conn, 7)) == "neutral"


@pytest.mark.asyncio
async def test_archived_or_fileless_proof_still_counts_as_pending(pool):
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn, archived=True))
        assert (await facts(conn))[0] is True
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM practice_delivery_slots")
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn, file_id=""))
        # visible to the client but with no stored file: still to come, never "full"
        assert await facts(conn) == (True, True, True)
        assert wording_of(await documents_pending(conn, 7)) == "partial"


@pytest.mark.asyncio
async def test_a_waived_slot_is_not_pending_and_not_required_to_be_visible(pool):
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn))
        await waive(conn, "document_2")
        assert await facts(conn) == (False, True, True)
        assert wording_of(await documents_pending(conn, 7)) == "full"


@pytest.mark.asyncio
async def test_all_slots_waived_is_neutral(pool):
    async with pool.acquire() as conn:
        await waive(conn, "electronic_visa")
        await waive(conn, "document_2")
        assert await facts(conn) == (False, False, True)
        assert wording_of(await documents_pending(conn, 7)) == "neutral"


@pytest.mark.asyncio
async def test_custom_service_without_titles_is_neutral_and_pending(pool):
    async with pool.acquire() as conn:
        await add_practice(conn, 9, "other_custom")
        assert await facts(conn, 9) == (True, False, True)
        assert wording_of(await documents_pending(conn, 9)) == "neutral"


@pytest.mark.asyncio
async def test_custom_service_follows_its_titles(pool):
    async with pool.acquire() as conn:
        await add_practice(conn, 9, "other_custom")
        await conn.execute(
            "INSERT INTO practice_delivery_titles VALUES(9,'other_custom',ARRAY['One','Two'])"
        )
        await fill(conn, "custom_1", await add_doc(conn, 9), 9, "other_custom")
        assert await facts(conn, 9) == (True, True, False)
        await fill(conn, "custom_2", await add_doc(conn, 9), 9, "other_custom")
        assert await facts(conn, 9) == (False, True, True)


@pytest.mark.asyncio
async def test_service_without_a_delivery_policy_is_legacy(pool):
    async with pool.acquire() as conn:
        await add_practice(conn, 10, "zz_unknown_service")
        assert await documents_pending(conn, 10) is None
        assert await documents_pending(conn, 999) is None
        assert wording_of(None) == "legacy"


@pytest.mark.asyncio
async def test_a_hidden_practice_is_neutral_even_with_visible_files(pool):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE practices SET client_visible=false WHERE id=7")
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn))
        assert wording_of(await documents_pending(conn, 7)) == "neutral"


@pytest.mark.asyncio
async def test_another_family_members_file_is_pending_and_invisible(pool):
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO client_family_members VALUES(5,31)")
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn, family=5))
        assert await facts(conn) == (True, True, False)


@pytest.mark.asyncio
async def test_the_visibility_predicate_is_the_portals(pool):
    # Whatever portal_files lists is what the helper counts as accessible.
    async with pool.acquire() as conn:
        shown = await add_doc(conn)
        hidden = await add_doc(conn, visible=False)
        await fill(conn, "electronic_visa", shown)
        await fill(conn, "document_2", hidden)
        listed = {row["id"] for row in (await service.portal_files(conn, 7, 31))["documents"]}
        assert listed == {shown}
        assert (await facts(conn))[1] is True


# ── S1: the timeline row written by complete() ─────────────────────────────────


async def timeline(conn):
    return await conn.fetchval("SELECT description FROM timeline_events WHERE practice_id=7")


async def two_files(pool):
    """Both files saved, the practice open: what an older machine left (the first proof completes it
    now), so the completion and its timeline row are taken back."""
    for key, name in (("electronic_visa", "electronic-visa"), ("document_2", "itk")):
        await service.upload(pool, 7, key, STAFF, final_file(name), BackgroundTasks())
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM practice_delivery_completions WHERE practice_id=7")
        await conn.execute("DELETE FROM timeline_events WHERE practice_id=7")
        await conn.execute("UPDATE practices SET status='on_process' WHERE id=7")


@pytest.mark.asyncio
async def test_complete_writes_todays_text_when_everything_is_visible(pool):
    await two_files(pool)
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert await timeline(conn) == "Final documents delivered"


def publishing_only(monkeypatch, keys):
    """complete() on main publishes every slot, so a later document is modelled by publishing
    fewer: what the first-proof completion of the next step leaves behind."""
    real = service.publish
    monkeypatch.setattr(service, "publish", lambda conn, p, _all: real(conn, p, keys))


@pytest.mark.asyncio
async def test_complete_writes_the_partial_text_when_a_file_is_not_visible(pool, monkeypatch):
    await two_files(pool)
    async with pool.acquire() as conn:  # the later document is saved but not yet shown
        await conn.execute("UPDATE documents SET client_visible=false WHERE file_name LIKE 'itk%'")
    publishing_only(monkeypatch, ["electronic_visa"])
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert await timeline(conn) == (
            "First final documents delivered; the remaining documents will follow"
        )


@pytest.mark.asyncio
async def test_complete_writes_the_neutral_text_when_nothing_is_visible(pool, monkeypatch):
    await two_files(pool)
    async with pool.acquire() as conn:  # the e-visa was published at its own upload
        await conn.execute("UPDATE documents SET client_visible=false")
    publishing_only(monkeypatch, [])
    await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert await timeline(conn) == (
            "Case completed; your advisor will be in touch about the handover"
        )


@pytest.mark.asyncio
async def test_a_failing_wording_read_cannot_abort_the_completion(pool):
    await two_files(pool)
    # A SQL error inside the read: the savepoint keeps the completion transaction usable.
    with patch.object(progress, "_LIVE", "this_is_not_sql"):
        await service.complete(pool, 7, STAFF, BackgroundTasks())
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM practices WHERE id=7") == "completed"
        assert await timeline(conn) == (
            "Case completed; your advisor will be in touch about the handover"
        )


# ── S2: the listener, through the real practice_changed trigger ────────────────


async def install_trigger(conn):
    await conn.execute((FOLDER / "144_events_outbox.sql").read_text().split("-- === ROLLBACK")[0])
    body = re.search(
        r"CREATE OR REPLACE FUNCTION notify_practice_change\(\).*?\$\$ LANGUAGE plpgsql;",
        (FOLDER / "146_eventbus_triggers_use_outbox.sql").read_text(),
        re.S,
    ).group(0)
    await conn.execute(
        body
        + """ CREATE TRIGGER trg_practice_changed AFTER UPDATE ON practices
              FOR EACH ROW EXECUTE FUNCTION notify_practice_change();"""
    )


async def completion_mail(pool):
    """Complete practice 7 the way the sweep or Complete would, then run the real listener."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE practices SET status='completed' WHERE id=7")
        payload = await conn.fetchval(
            "SELECT payload::text FROM events_outbox WHERE channel='practice_changed' ORDER BY id DESC LIMIT 1"
        )
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
    await listener._handle_notification(payload)
    listener._send_logged.assert_awaited_once()
    return listener._send_logged.await_args.args


@pytest.mark.asyncio
async def test_listener_cutover_shape_sends_the_partial_mail(pool):
    async with pool.acquire() as conn:
        await install_trigger(conn)
        await fill(conn, "electronic_visa", await add_doc(conn))
    args = await completion_mail(pool)
    assert args[0] == "completion_client"
    assert args[4] == "[CLIENT] Your C1 Tourism Visa: first documents delivered"
    assert "fully complete" not in args[5] and "review" not in args[5]
    assert "The remaining documents will follow" in args[5]


@pytest.mark.asyncio
async def test_listener_sends_todays_mail_when_everything_is_visible(pool):
    async with pool.acquire() as conn:
        await install_trigger(conn)
        await fill(conn, "electronic_visa", await add_doc(conn))
        await fill(conn, "document_2", await add_doc(conn))
    args = await completion_mail(pool)
    assert args[4] == "[CLIENT] ✅ Completed: Your C1 Tourism Visa Documents Are Ready"
    assert "fully complete" in args[5]


@pytest.mark.asyncio
async def test_listener_sends_the_neutral_mail_when_nothing_is_visible(pool):
    async with pool.acquire() as conn:
        await install_trigger(conn)
        await fill(conn, "electronic_visa", await add_doc(conn, visible=False))
    args = await completion_mail(pool)
    assert args[4] == "[CLIENT] Completed: Your C1 Tourism Visa"
    assert "fully complete" not in args[5]


# ── S5: the portal matter detail ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matter_detail_text_follows_the_real_helper(pool):
    row = {
        "id": 7,
        "title": "C1",
        "category": "visa",
        "status": "completed",
        "missing_documents": None,
        "expiry_date": None,
        "updated_at": None,
    }
    async with pool.acquire() as conn:
        await fill(conn, "electronic_visa", await add_doc(conn))
        partial = _shape_matter_detail(row, wording_of(await documents_pending(conn, 7)))
        await fill(conn, "document_2", await add_doc(conn))
        full = _shape_matter_detail(row, wording_of(await documents_pending(conn, 7)))
    assert partial["description"].startswith("The first documents are delivered")
    assert full["description"] == "This matter is complete."
