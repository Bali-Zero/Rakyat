"""The HR document door (STAFF_CARD.md C13) against a real Postgres. Synthetic data only.

Provenance lives in the card (`profile.doc_proof.<slot>`), written in the same UPDATE as the slot.
The test schema deliberately has NO `security_audit_log`: production does not have one either, so
every door transaction here proves it touches only `hr_employees`."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import asyncpg
import pytest

from backend.app.services.hr import hr_documents as hd
from backend.app.services.hr.hr_documents import HRDocCode, HRDocError, HRDocumentService
from backend.tests.migrations.test_hr_card_write_path import pool  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

A_ID, A_MAIL = "tm-a", "fake.a@balizero.com"
ACTOR = "zero@balizero.com"
NIK = "3171234567890001"  # synthetic 16 digits
SIX = {"ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract"}


async def _prep(pool: asyncpg.Pool) -> HRDocumentService:  # noqa: F811
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('security_audit_log')") is None
    return HRDocumentService(pool, drive=None)


async def _step(svc: HRDocumentService, slot: str, new_id: str | None, who=(A_ID, A_MAIL)):
    return await svc._db_step(
        actor_email=ACTOR, member_id=who[0], email=who[1], slot=slot, new_id=new_id
    )


async def _set(pool: asyncpg.Pool, profile: Any, member: str = A_ID) -> None:  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = $2", profile, member
        )


async def _profile(pool: asyncpg.Pool, member: str = A_ID) -> Any:  # noqa: F811
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT profile::text FROM hr_employees WHERE team_member_id = $1", member
        )
    return json.loads(raw)


def _proof(profile: dict[str, Any], slot: str) -> dict[str, Any]:
    return profile["doc_proof"][slot]


# ── acceptance 4: no audit table, the door works ──


@pytest.mark.asyncio
async def test_the_door_works_on_a_database_without_the_audit_table(pool: asyncpg.Pool) -> None:  # noqa: F811
    import inspect

    svc = await _prep(pool)  # asserts the table is absent
    assert "security_audit_log" not in inspect.getsource(hd)
    verified, docs = await _step(svc, "ktp", "f1")
    assert verified is None and docs["ktp"] == {"present": True, "file_id": "f1"}
    await _step(svc, "ktp", None)
    assert (await _profile(pool))["doc_proof"]["ktp"]["id"] is None


# ── the write: slot and proof in one UPDATE, per slot ──


@pytest.mark.asyncio
async def test_upload_writes_slot_and_proof_together(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _step(svc, "ktp", "f1")
    p = await _profile(pool)
    assert p["docs"] == {"ktp": "f1"}
    proof = _proof(p, "ktp")
    assert proof["id"] == "f1" and proof["by"] == ACTOR
    assert proof["at"].startswith("20") and "T" in proof["at"]  # a UTC ISO timestamp


@pytest.mark.asyncio
async def test_a_write_keeps_the_other_proofs_and_every_other_profile_key(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    svc = await _prep(pool)
    await _set(
        pool, {"unit": "Tax", "on_payroll": False, "nik": NIK, "contract": {"number": "C-1"}}
    )
    await _step(svc, "kk", "f2")
    await _step(svc, "ktp", "f1")
    p = await _profile(pool)
    assert p["docs"] == {"kk": "f2", "ktp": "f1"}
    assert set(p["doc_proof"]) == {"kk", "ktp"} and _proof(p, "kk")["id"] == "f2"
    assert p["unit"] == "Tax" and p["on_payroll"] is False and p["nik"] == NIK
    assert p["contract"] == {"number": "C-1"}


@pytest.mark.asyncio
async def test_replace_returns_the_previous_value_only_when_it_was_verified(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    svc = await _prep(pool)
    await _step(svc, "ktp", "f1")
    verified, docs = await _step(svc, "ktp", "f3")
    assert verified == "f1" and docs["ktp"]["file_id"] == "f3"
    assert _proof(await _profile(pool), "ktp")["id"] == "f3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [
        {"docs": {"ktp": NIK}},  # legacy: a value with no proof at all
        {"docs": {"ktp": NIK}, "doc_proof": {"ktp": {"id": "other", "by": "x", "at": "y"}}},
        {"docs": {"ktp": NIK}, "doc_proof": {"ktp": {"id": None, "by": "x", "at": "y"}}},
        {"docs": {"ktp": NIK}, "doc_proof": {"ktp": "f1"}},  # proof not an object
        {"docs": {"ktp": NIK}, "doc_proof": ["ktp"]},
        {"docs": {"ktp": 12345}, "doc_proof": {"ktp": {"id": 12345, "by": "x", "at": "y"}}},
        {"docs": {"ktp": "  "}, "doc_proof": {"ktp": {"id": "  ", "by": "x", "at": "y"}}},
        {"docs": ["ktp"]},
        {"docs": "x"},
    ],
)
async def test_an_unverified_previous_value_is_never_returned(  # noqa: F811
    pool: asyncpg.Pool, profile: dict[str, Any]
) -> None:
    svc = await _prep(pool)
    await _set(pool, profile)
    verified, _docs = await _step(svc, "ktp", "newid1234567")
    assert verified is None
    assert (await _profile(pool))["docs"]["ktp"] == "newid1234567"


# ── contract slot and the legacy duplicate (N4) ──


@pytest.mark.asyncio
async def test_a_contract_write_and_remove_delete_the_legacy_docs_contract_duplicate(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    svc = await _prep(pool)
    await _set(
        pool, {"contract": {"number": "C-1", "file_id": NIK}, "docs": {"contract": NIK, "kk": "k1"}}
    )
    verified, docs = await _step(svc, "contract", "c1")
    assert verified is None  # legacy value, no proof
    p = await _profile(pool)
    assert p["contract"] == {"number": "C-1", "file_id": "c1"}
    assert p["docs"] == {"kk": "k1"}  # the duplicate is gone, the other slot stays
    assert docs["contract"] == {"present": True, "file_id": "c1"}
    verified, docs = await _step(svc, "contract", None)
    assert verified == "c1" and docs["contract"] == {"present": False, "file_id": None}
    p = await _profile(pool)
    assert p["contract"] == {"number": "C-1"} and p["docs"] == {"kk": "k1"}
    assert _proof(p, "contract")["id"] is None


@pytest.mark.asyncio
async def test_removing_a_legacy_contract_clears_both_places_without_a_verified_previous(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    svc = await _prep(pool)
    await _set(pool, {"contract": {"file_id": NIK}, "docs": {"contract": NIK}})
    verified, docs = await _step(svc, "contract", None)
    assert verified is None and docs["contract"] == {"present": False, "file_id": None}
    p = await _profile(pool)
    assert p["contract"] == {} and p["docs"] == {}


# ── remove ──


@pytest.mark.asyncio
async def test_remove_clears_the_slot_sets_a_null_proof_and_keeps_the_rest(
    pool: asyncpg.Pool,
) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _step(svc, "npwp", "f1")
    await _step(svc, "kk", "f2")
    verified, docs = await _step(svc, "npwp", None)
    assert verified == "f1" and docs["npwp"]["present"] is False and docs["kk"]["present"] is True
    p = await _profile(pool)
    assert p["docs"] == {"kk": "f2"}
    assert _proof(p, "npwp")["id"] is None and _proof(p, "npwp")["by"] == ACTOR
    assert _proof(p, "kk")["id"] == "f2"


@pytest.mark.asyncio
async def test_removing_a_legacy_value_clears_it_and_returns_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _set(pool, {"docs": {"ktp": NIK}})
    verified, docs = await _step(svc, "ktp", None)
    assert verified is None and docs["ktp"]["present"] is False
    assert (await _profile(pool))["docs"] == {}


@pytest.mark.asyncio
async def test_removing_an_empty_slot_is_harmless(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    verified, docs = await _step(svc, "kk", None)
    assert verified is None and docs["kk"] == {"present": False, "file_id": None}


# ── refusals and rollbacks ──


@pytest.mark.asyncio
async def test_no_hr_row_invisible_person_and_stale_email_write_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    for who, code in (
        (("tm-b", "fake.b@balizero.com"), HRDocCode.HR_RECORD_REQUIRED),  # staff, no HR row
        (("tm-c", "fake.c@balizero.com"), HRDocCode.NOT_FOUND),  # a client
        ((A_ID, "other@balizero.com"), HRDocCode.NOT_FOUND),  # stale email
    ):
        with pytest.raises(HRDocError) as exc:
            await _step(svc, "ktp", "f1", who=who)
        assert exc.value.code is code
    assert await _profile(pool) == {}


@pytest.mark.asyncio
async def test_a_non_object_profile_is_refused_and_left_alone(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = '[1]'::jsonb WHERE team_member_id = 'tm-a'"
        )
    with pytest.raises(HRDocError) as exc:
        await _step(svc, "ktp", "f1")
    assert exc.value.code is HRDocCode.DB_ERROR
    assert await _profile(pool) == [1]


@pytest.mark.asyncio
async def test_an_error_at_commit_is_commit_unknown_and_reconcile_tells_the_truth(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    svc = await _prep(pool)
    async with pool.acquire() as conn:  # a deferred check that only fires at COMMIT
        await conn.execute(
            """
            CREATE FUNCTION boom_at_commit() RETURNS trigger AS $$
            BEGIN
                IF NEW.profile->'docs'->>'ktp' = 'boomid123456' THEN RAISE EXCEPTION 'no'; END IF;
                RETURN NULL;
            END $$ LANGUAGE plpgsql;
            CREATE CONSTRAINT TRIGGER t_boom AFTER UPDATE ON hr_employees
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION boom_at_commit();
            """
        )
    state: dict[str, Any] = {}
    with pytest.raises(HRDocError) as exc:
        await svc._db_step(
            actor_email=ACTOR,
            member_id=A_ID,
            email=A_MAIL,
            slot="ktp",
            new_id="boomid123456",
            state=state,
        )
    assert exc.value.code is HRDocCode.COMMIT_UNKNOWN and state["verified"] is None
    assert await _profile(pool) == {}  # it really rolled back: neither slot nor proof
    assert await svc._slot_holds(A_ID, "ktp", "boomid123456") is False
    await _step(svc, "ktp", "goodid123456")
    assert await svc._slot_holds(A_ID, "ktp", "goodid123456") is True
    # a value without its proof does not count as landed
    await _set(pool, {"docs": {"ktp": "unproven12345"}})
    assert await svc._slot_holds(A_ID, "ktp", "unproven12345") is False


@pytest.mark.asyncio
async def test_reconcile_waits_for_an_in_flight_commit_then_reads_its_outcome(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    svc = await _prep(pool)
    holder = await pool.acquire()
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.execute("SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", A_ID)
        await holder.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = $2",
            {
                "docs": {"ktp": "landed123456"},
                "doc_proof": {"ktp": {"id": "landed123456", "by": "x", "at": "y"}},
            },
            A_ID,
        )
        task = asyncio.create_task(svc._slot_holds(A_ID, "ktp", "landed123456"))
        await asyncio.sleep(0.4)
        assert not task.done()  # the reconcile read is waiting on the same locks
        await tx.commit()
        assert await asyncio.wait_for(task, 5) is True
    finally:
        await pool.release(holder)


# ── unit changes, with a real interleaving ──


@pytest.mark.asyncio
async def test_a_unit_change_between_the_folder_step_and_the_commit_ends_in_the_new_unit(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    from backend.tests.unit.app.services.hr.test_hr_documents import ROOT, FakeDrive

    svc = await _prep(pool)
    await _set(pool, {"unit": "Tax"})
    drive = FakeDrive()
    svc.drive = drive
    real_upload = drive.upload_file_to_folder

    async def upload_then_unit_changes(*a: Any, **kw: Any) -> Any:
        res = await real_upload(*a, **kw)
        async with pool.acquire() as conn:  # the C11 PATCH lands after ensure_folder
            await conn.execute(
                "UPDATE hr_employees SET profile = profile || $1 WHERE team_member_id = 'tm-a'",
                {"unit": "Management"},
            )
        return res

    drive.upload_file_to_folder = upload_then_unit_changes  # type: ignore[method-assign]
    out = await svc.upload(
        actor_email=ACTOR,
        member_id=A_ID,
        email=A_MAIL,
        slot="ktp",
        data=b"%PDF-1.7",
        mime="application/pdf",
        ext="pdf",
    )
    assert out["documents"]["ktp"]["present"] is True and set(out) == {"slot", "documents"}
    person = next(f for f in drive.folders.values() if f["appProperties"])
    unit_folder = drive.folders[person["parents"][0]]
    assert unit_folder["name"] == "MANAGEMENT" and unit_folder["parents"] == [ROOT]
    assert len(drive.moves) == 1


@pytest.mark.asyncio
async def test_a_card_patch_waits_for_the_doors_folder_move_and_the_next_move_matches(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    from backend.app.services.hr.hr_service import HRService
    from backend.tests.unit.app.services.hr.test_hr_documents import FakeDrive

    svc = await _prep(pool)
    await _set(pool, {"unit": "Tax"})
    drive = FakeDrive()
    svc.drive = drive
    real_upload, real_move = drive.upload_file_to_folder, drive.move_file
    patch_task: list[asyncio.Task[Any]] = []
    waited: list[bool] = []

    async def upload_then_patch_one(*a: Any, **kw: Any) -> Any:
        res = await real_upload(*a, **kw)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hr_employees SET profile = profile || $1 WHERE team_member_id = 'tm-a'",
                {"unit": "Management"},
            )
        return res

    async def move_while_a_second_patch_tries(*a: Any, **kw: Any) -> Any:
        patch_task.append(
            asyncio.create_task(
                HRService(pool).edit_card(A_ID, A_MAIL, {}, {"unit": "Marketing"}, [])
            )
        )
        await asyncio.sleep(0.4)
        waited.append(not patch_task[0].done())  # the door holds the member lock during the move
        return await real_move(*a, **kw)

    drive.upload_file_to_folder = upload_then_patch_one  # type: ignore[method-assign]
    drive.move_file = move_while_a_second_patch_tries  # type: ignore[method-assign]
    await svc.upload(
        actor_email=ACTOR,
        member_id=A_ID,
        email=A_MAIL,
        slot="ktp",
        data=b"%PDF-1.7",
        mime="application/pdf",
        ext="pdf",
    )
    assert waited == [True]
    assert await asyncio.wait_for(patch_task[0], 5) is not None
    assert (await _profile(pool))["unit"] == "Marketing"
    person = next(f for f in drive.folders.values() if f["appProperties"])
    assert drive.folders[person["parents"][0]]["name"] == "MANAGEMENT"  # what the door last saw
    drive.move_file = real_move  # type: ignore[method-assign]
    await svc.ensure_folder(A_ID)  # the next door call follows the final unit
    assert drive.folders[person["parents"][0]]["name"] == "MARKETING"


# ── the real googleapiclient: a stored value is never in a request or a log (R3) ──


def _real_drive(responses: list[tuple[int, Any]]) -> tuple[Any, Any]:
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpMockSequence

    from backend.services.integrations.service_account_drive_service import (
        ServiceAccountDriveService,
    )

    http = HttpMockSequence([({"status": str(st)}, json.dumps(body)) for st, body in responses])
    drive = ServiceAccountDriveService.__new__(ServiceAccountDriveService)
    drive.service = build("drive", "v3", http=http, static_discovery=True)
    drive.root_folder_id = "root"
    return drive, http


PF = {"files": [{"id": "pf1", "name": "x", "parents": ["u1"]}]}
LISTING = {
    "files": [{"id": "RealDriveFile12345", "mimeType": "application/pdf", "parents": ["pf1"]}]
}


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["replace", "remove"])
async def test_an_unverified_value_makes_no_drive_request_and_leaves_no_trace(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture, op: str
) -> None:
    svc = await _prep(pool)
    await _set(pool, {"docs": {"ktp": NIK}})  # legacy, no proof
    drive, http = _real_drive([])  # any Drive request would raise: the mock has no responses
    svc.drive = drive
    with caplog.at_level(logging.DEBUG):
        if op == "remove":
            out = await svc.remove(actor_email=ACTOR, member_id=A_ID, email=A_MAIL, slot="ktp")
        else:
            verified, docs = await _step(svc, "ktp", "newfile1234567")
            await svc._trash_if_ours(A_ID, "ktp", verified, "newfile1234567", "pf1")
            out = {"documents": docs}
    assert http.request_sequence == []  # nothing was sent to Drive, not even a query
    assert NIK not in caplog.text and NIK not in json.dumps(out)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["replace", "remove"])
async def test_a_verified_previous_is_trashed_by_its_listing_id_only(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture, op: str
) -> None:
    svc = await _prep(pool)
    await _step(svc, "ktp", "RealDriveFile12345")  # verified: proof id == value
    seq: list[tuple[int, Any]] = []
    if op == "remove":
        seq.append((200, PF))  # _our_folder
    seq += [(200, LISTING), (200, {"id": "RealDriveFile12345", "trashed": True})]
    drive, http = _real_drive(seq)
    svc.drive = drive
    with caplog.at_level(logging.DEBUG):
        if op == "remove":
            await svc.remove(actor_email=ACTOR, member_id=A_ID, email=A_MAIL, slot="ktp")
        else:
            verified, _ = await _step(svc, "ktp", "newfile1234567")
            await svc._trash_if_ours(A_ID, "ktp", verified, "newfile1234567", "pf1")
    patched = [r for r in http.request_sequence if r[1] == "PATCH"]
    assert len(patched) == 1 and "RealDriveFile12345" in patched[0][0]


# ── acceptance 1: the C10 import refuses the door-owned keys, whole, and writes nothing ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [{"docs": {"ktp": NIK}}, {"doc_proof": {"ktp": {"id": NIK}}}, {"contract": {"file_id": NIK}}],
)
async def test_the_import_refuses_door_keys_and_writes_nothing(  # noqa: F811
    pool: asyncpg.Pool, profile: dict[str, Any]
) -> None:
    from backend.scripts import _import_staff_cards_worker as imp

    await _set(pool, {"unit": "Tax"})
    document = {
        "cards": [
            {"email": A_MAIL, "fields": {"bank_name": "Changed"}, "profile": profile},
            {"email": "fake.b@balizero.com", "fields": {"hire_date": "2025-02-01"}},
        ]
    }
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, document, apply=True)
    assert ok is False and counts["rejected:doc_keys_door_only"] == 1
    assert counts["committed:no"] == 1  # a batch commits only when every card is fine
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT bank_name FROM hr_employees WHERE id = 1") is None
        assert (
            await conn.fetchval("SELECT count(*) FROM hr_employees WHERE team_member_id = 'tm-b'")
            == 0
        )
    assert await _profile(pool) == {"unit": "Tax"}


@pytest.mark.asyncio
async def test_the_import_still_writes_contract_number_and_other_keys(pool: asyncpg.Pool) -> None:  # noqa: F811
    from backend.scripts import _import_staff_cards_worker as imp

    document = {
        "cards": [{"email": A_MAIL, "profile": {"contract": {"number": "C-9"}, "unit": "Tax"}}]
    }
    async with pool.acquire() as conn:
        counts, ok = await imp.run(conn, document, apply=True)
    assert ok is True and counts["applied"] == 1
    assert await _profile(pool) == {"contract": {"number": "C-9"}, "unit": "Tax"}


# ── acceptance 2 on stored data: a NIK-like slot value never reaches a card response ──


@pytest.mark.asyncio
async def test_a_stored_nik_like_slot_value_never_reaches_the_card_view(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    from backend.app.services.hr.hr_card_models import member_card
    from backend.app.services.hr.hr_service import HRService

    await _set(
        pool, {"docs": {"ktp": NIK, "mystery": NIK}, "contract": {"file_id": NIK, "number": "C"}}
    )
    with caplog.at_level(logging.DEBUG):
        card = await HRService(pool).get_card_for_member(A_ID)
        view = member_card(card).model_dump(mode="json")
    assert NIK not in json.dumps(view) and NIK not in caplog.text
    assert set(view["documents"]) == SIX
    assert view["documents"]["ktp"] == {"present": True, "file_id": None}
