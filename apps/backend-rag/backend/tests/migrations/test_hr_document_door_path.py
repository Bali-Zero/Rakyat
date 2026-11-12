"""The HR document door (STAFF_CARD.md C13) DB step against a real Postgres: the card write and the
audit row share one transaction, and a failure leaves neither. Synthetic data only."""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg
import pytest

from backend.app.services.hr.hr_documents import (
    HRDocCode,
    HRDocError,
    HRDocumentService,
    value_sha,
)
from backend.tests.migrations.test_hr_card_write_path import pool  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

A_ID, A_MAIL = "tm-a", "fake.a@balizero.com"
_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS security_audit_log (
    id BIGSERIAL PRIMARY KEY, user_id VARCHAR(255), user_email VARCHAR(255),
    action VARCHAR(100) NOT NULL, resource_type VARCHAR(100), resource_id VARCHAR(255),
    ip_address INET, user_agent TEXT, details JSONB, success BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())
"""


async def _prep(pool: asyncpg.Pool) -> HRDocumentService:  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(_AUDIT_DDL)
    return HRDocumentService(pool, drive=None)


async def _step(svc: HRDocumentService, slot: str, new_id: str | None, who=(A_ID, A_MAIL)):
    return await svc._db_step(
        actor_id="tm-owner", member_id=who[0], email=who[1], slot=slot, new_id=new_id
    )


async def _profile(pool: asyncpg.Pool, member: str = A_ID) -> Any:  # noqa: F811
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT profile::text FROM hr_employees WHERE team_member_id = $1", member
        )
    return json.loads(raw)


async def _audit(pool: asyncpg.Pool) -> list[asyncpg.Record]:  # noqa: F811
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM security_audit_log ORDER BY id")


@pytest.mark.asyncio
async def test_upload_writes_card_and_audit_together(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    assert await _step(svc, "ktp", "f1") is None
    assert await _profile(pool) == {"docs": {"ktp": "f1"}}
    rows = await _audit(pool)
    assert len(rows) == 1 and rows[0]["action"] == "hr_document_upload"
    assert rows[0]["resource_id"] == A_ID and rows[0]["user_id"] == "tm-owner"
    assert rows[0]["details"] == {"slot": "ktp", "new_id": "f1", "previous_sha": None}


@pytest.mark.asyncio
async def test_replace_returns_previous_and_keeps_the_other_slots(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _step(svc, "ktp", "f1")
    await _step(svc, "kk", "f2")
    assert await _step(svc, "ktp", "f3") == "f1"
    assert (await _profile(pool))["docs"] == {"ktp": "f3", "kk": "f2"}
    assert (await _audit(pool))[-1]["details"]["previous_sha"] == value_sha("f1")


@pytest.mark.asyncio
async def test_contract_slot_uses_contract_file_id_and_keeps_the_number(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"contract": {"number": "C-1"}, "unit": "Tax"},
        )
    await _step(svc, "contract", "c1")
    assert (await _profile(pool))["contract"] == {"number": "C-1", "file_id": "c1"}
    assert await _step(svc, "contract", None) == "c1"
    assert await _profile(pool) == {"contract": {"number": "C-1"}, "unit": "Tax"}


@pytest.mark.asyncio
async def test_remove_clears_the_slot_and_audits(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    await _step(svc, "npwp", "f1")
    await _step(svc, "kk", "f2")
    assert await _step(svc, "npwp", None) == "f1"
    assert (await _profile(pool))["docs"] == {"kk": "f2"}
    last = (await _audit(pool))[-1]
    assert last["action"] == "hr_document_remove"
    assert last["details"] == {"slot": "npwp", "new_id": None, "previous_sha": value_sha("f1")}


@pytest.mark.asyncio
async def test_removing_an_empty_slot_changes_and_audits_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    assert await _step(svc, "kk", None) is None
    assert await _profile(pool) == {} and await _audit(pool) == []


@pytest.mark.asyncio
async def test_failed_audit_rolls_the_card_back(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    async with pool.acquire() as conn:  # make the audit insert fail
        await conn.execute("ALTER TABLE security_audit_log ADD CONSTRAINT nope CHECK (false)")
    with pytest.raises(HRDocError) as exc:
        await _step(svc, "ktp", "f1")
    assert exc.value.code is HRDocCode.DB_ERROR
    assert await _profile(pool) == {}  # neither the id...
    assert await _audit(pool) == []  # ...nor the row


@pytest.mark.asyncio
async def test_no_hr_row_and_invisible_person_write_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    with pytest.raises(HRDocError) as exc:  # tm-b is staff but has no hr_employees row
        await _step(svc, "ktp", "f1", who=("tm-b", "fake.b@balizero.com"))
    assert exc.value.code is HRDocCode.HR_RECORD_REQUIRED
    with pytest.raises(HRDocError) as exc2:  # tm-c is a client
        await _step(svc, "ktp", "f1", who=("tm-c", "fake.c@balizero.com"))
    assert exc2.value.code is HRDocCode.NOT_FOUND
    with pytest.raises(HRDocError) as exc3:  # stale email
        await _step(svc, "ktp", "f1", who=(A_ID, "other@balizero.com"))
    assert exc3.value.code is HRDocCode.NOT_FOUND
    assert await _audit(pool) == []


NIK = "3171234567890001"


@pytest.mark.asyncio
async def test_free_text_in_a_slot_never_reaches_the_audit_or_the_logs(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    svc = await _prep(pool)
    async with pool.acquire() as conn:  # C10 accepts free text as a "file id"
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"docs": {"ktp": NIK, "kk": "KK Made Contoh.pdf"}},
        )
    with caplog.at_level(logging.DEBUG):
        assert await _step(svc, "ktp", "newid1234567") == NIK  # replace
        assert await _step(svc, "kk", None) == "KK Made Contoh.pdf"  # remove
    rows = await _audit(pool)
    blob = json.dumps([dict(r["details"]) for r in rows])
    assert NIK not in blob and "Made Contoh" not in blob and NIK not in caplog.text
    assert rows[0]["details"]["previous_sha"] == value_sha(NIK)
    assert rows[1]["details"]["previous_sha"] == value_sha("KK Made Contoh.pdf")
    assert rows[0]["details"]["new_id"] == "newid1234567"  # the new id is in clear


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
                IF NEW.details->>'new_id' = 'boomid123456' THEN RAISE EXCEPTION 'no'; END IF;
                RETURN NULL;
            END $$ LANGUAGE plpgsql;
            CREATE CONSTRAINT TRIGGER t_boom AFTER INSERT ON security_audit_log
                DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION boom_at_commit();
            """
        )
    state: dict[str, Any] = {}
    with pytest.raises(HRDocError) as exc:
        await svc._db_step(
            actor_id="tm-owner",
            member_id=A_ID,
            email=A_MAIL,
            slot="ktp",
            new_id="boomid123456",
            state=state,
        )
    assert exc.value.code is HRDocCode.COMMIT_UNKNOWN and state["previous"] is None
    assert await _profile(pool) == {}  # it really rolled back
    assert await svc._slot_holds(A_ID, "ktp", "boomid123456") is False
    await _step(svc, "ktp", "goodid123456")
    assert await svc._slot_holds(A_ID, "ktp", "goodid123456") is True
    assert await svc._slot_holds(A_ID, "ktp", "boomid123456") is False


@pytest.mark.asyncio
async def test_a_failure_before_commit_is_a_plain_db_error(pool: asyncpg.Pool) -> None:  # noqa: F811
    svc = await _prep(pool)
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE security_audit_log ADD CONSTRAINT nope2 CHECK (false)")
    with pytest.raises(HRDocError) as exc:
        await _step(svc, "ktp", "f1")
    assert exc.value.code is HRDocCode.DB_ERROR


@pytest.mark.asyncio
async def test_reconcile_waits_for_an_in_flight_commit_then_reads_its_outcome(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    import asyncio

    svc = await _prep(pool)
    holder = await pool.acquire()
    try:
        tx = holder.transaction()
        await tx.start()
        # the original DB step, still before its COMMIT: it holds the member lock and the row
        await holder.execute("SELECT pg_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", A_ID)
        await holder.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = $2",
            {"docs": {"ktp": "landed123456"}},
            A_ID,
        )
        task = asyncio.create_task(svc._slot_holds(A_ID, "ktp", "landed123456"))
        await asyncio.sleep(0.4)
        assert not task.done()  # the reconcile read is waiting on the same locks
        await tx.commit()
        assert await asyncio.wait_for(task, 5) is True
    finally:
        await pool.release(holder)


@pytest.mark.asyncio
async def test_a_unit_change_between_the_folder_step_and_the_commit_ends_in_the_new_unit(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    from backend.tests.unit.app.services.hr.test_hr_documents import ROOT, FakeDrive

    svc = await _prep(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"unit": "Tax"},
        )
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
        actor_id="tm-owner",
        member_id=A_ID,
        email=A_MAIL,
        slot="ktp",
        data=b"%PDF-1.7",
        mime="application/pdf",
        ext="pdf",
    )
    assert out["file_id"] in drive.files
    person = next(f for f in drive.folders.values() if f["appProperties"])
    unit_folder = drive.folders[person["parents"][0]]
    assert unit_folder["name"] == "MANAGEMENT" and unit_folder["parents"] == [ROOT]
    assert drive.files[out["file_id"]]["parent"] == person["id"]  # the file moved with its folder
    assert len(drive.moves) == 1


# ── the real googleapiclient: a stored value is never in a request or a log (C13, "opaque") ──


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


def _wire(http: Any) -> str:
    return json.dumps([(r[0], r[1], str(r[2])) for r in http.request_sequence])


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["replace", "remove"])
@pytest.mark.parametrize("hit", [False, True])
async def test_the_real_sdk_never_sees_the_stored_value(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture, op: str, hit: bool
) -> None:
    svc = await _prep(pool)
    stored = NIK if not hit else "RealDriveFile12345"
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"docs": {"ktp": stored}},
        )
    folder = {"files": [{"id": "pf1", "name": "x", "parents": ["u1"]}]}
    door_files = {
        "files": [{"id": "RealDriveFile12345", "mimeType": "application/pdf", "parents": ["pf1"]}]
    }
    seq: list[tuple[int, Any]] = []
    if op == "remove":
        seq.append((200, folder))  # _our_folder
    seq.append((200, door_files))  # the door's own files
    if hit:
        seq.append((200, {"id": "RealDriveFile12345", "trashed": True}))  # the trash
    drive, http = _real_drive(seq)
    svc.drive = drive
    with caplog.at_level(logging.DEBUG):
        if op == "remove":
            out = await svc.remove(actor_id="tm-owner", member_id=A_ID, email=A_MAIL, slot="ktp")
        else:
            prev = await _step(svc, "ktp", "newfile1234567")
            assert prev == stored
            out = {"previous": await svc._trash_if_ours(A_ID, "ktp", prev, "newfile1234567", "pf1")}
    assert out["previous"] == ("trashed" if hit else "kept")
    wire = _wire(http)
    assert NIK not in wire and NIK not in caplog.text
    patched = [r for r in http.request_sequence if r[1] == "PATCH"]
    assert bool(patched) is hit  # only a match, by id equality, ever reaches files.update
    blob = json.dumps([dict(r["details"]) for r in await _audit(pool)])
    assert NIK not in blob


@pytest.mark.asyncio
async def test_a_card_patch_waits_for_the_doors_folder_move_and_the_next_move_matches(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    import asyncio

    from backend.app.services.hr.hr_service import HRService
    from backend.tests.unit.app.services.hr.test_hr_documents import FakeDrive

    svc = await _prep(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'", {"unit": "Tax"}
        )
    drive = FakeDrive()
    svc.drive = drive
    real_upload, real_move = drive.upload_file_to_folder, drive.move_file
    patch_task: list[asyncio.Task[Any]] = []
    waited: list[bool] = []

    async def upload_then_patch_one(*a: Any, **kw: Any) -> Any:
        res = await real_upload(*a, **kw)
        async with pool.acquire() as conn:  # first PATCH: lands before the door's re-check
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
        actor_id="tm-owner",
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
