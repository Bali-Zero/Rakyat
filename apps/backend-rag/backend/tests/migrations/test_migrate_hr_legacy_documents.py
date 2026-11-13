"""R5 (SPEC v2.1, STAFF_CARD.md C13): the legacy-paper migration against a real Postgres and a fake
Drive. Synthetic data only; the schema has no `security_audit_log`, as production."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from asyncpg.transaction import Transaction

from backend.app.services.hr import hr_documents as hd
from backend.app.services.hr.hr_card_models import project_documents
from backend.scripts import migrate_hr_legacy_documents as job
from backend.tests.migrations.test_hr_card_write_path import pool  # noqa: F401  (fixture)
from backend.tests.unit.app.services.hr.test_hr_documents import FakeDrive

pytestmark = pytest.mark.integration

NIK = "3171234567890001"  # synthetic 16 digits
NIK2 = "3171234567890002" * 2  # 30 digits: an admitted length, no letter
L1, L2, L3 = (f"1{c}AbCdEfGhIjKlMnOpQrStUvWxYz01234" for c in "XYZ")  # 33 id-shaped chars


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        self.resp = type("R", (), {"status": status})()


class Drive(FakeDrive):
    """The door's fake plus the sources, `files.copy` and every call recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[Any, ...]] = []
        self.copies: list[str] = []
        self.on_copy: Any = None
        self.on_trash: Any = None
        self.copy_result: Any = None  # when set, what `copy_file` returns instead of the real id
        self.fail_trash = False
        self.truncated = False  # the real client's page cap reached with a page still pending
        self.truncated_folders = False  # the same, for the person-folder lookup

    def add_source(self, fid: str, mime: str = "application/pdf") -> None:
        self.files[fid] = {
            "name": "legacy scan",
            "parent": "legacy-folder",
            "mime": mime,
            "appProperties": {},
            "modifiedTime": "2026-11-11T00:00:00Z",
        }

    async def get_file_metadata(self, file_id: str) -> dict[str, Any]:
        self.calls.append(("get", file_id))
        if file_id not in self.files:
            raise _HttpError(404)
        f = self.files[file_id]
        return {
            "id": file_id,
            "name": f["name"],
            "mimeType": f["mime"],
            "parents": [f["parent"]],
            "trashed": file_id in self.trashed,
            "modifiedTime": f.get("modifiedTime"),
        }

    async def copy_file(self, file_id, parent_id, name, app_properties):
        self.calls.append(("copy", file_id, parent_id))
        cid = self._id("cp")
        self.files[cid] = {
            "name": name,
            "parent": parent_id,
            "mime": self.files[file_id]["mime"],
            "appProperties": dict(app_properties),
        }
        self.copies.append(cid)
        if self.on_copy is not None:
            await self.on_copy(cid)
        if self.copy_result is not None:
            return self.copy_result
        return {"id": cid, "mimeType": self.files[cid]["mime"], "parents": [parent_id]}

    async def find_folders_by_app_property(self, key, value, strict=False):
        if strict and self.truncated_folders:
            raise RuntimeError("lookup truncated")
        return await super().find_folders_by_app_property(key, value)

    async def list_files_by_app_properties(self, props, parent_id, mimes, strict=False):
        self.calls.append(("list", tuple(sorted(props.items())), parent_id))
        if strict and self.truncated:
            raise RuntimeError("listing truncated")
        return [
            {"id": fid, "mimeType": f["mime"], "parents": [f["parent"]]}
            for fid, f in self.files.items()
            if f["parent"] == parent_id
            and fid not in self.trashed
            and (mimes is None or f["mime"] in mimes)
            and all(f["appProperties"].get(k) == v for k, v in props.items())
        ]

    async def trash_file(self, file_id):
        self.calls.append(("trash", file_id))
        if self.on_trash is not None:
            await self.on_trash(file_id)
        if self.fail_trash:
            raise RuntimeError("trash refused")
        await super().trash_file(file_id)

    def seed_person(self, member: str = "tm-a", unit: str = "TAX") -> str:
        """The unit folder and person folder `ensure_folder(member)` finds."""
        self.folders["u-seed"] = {
            "id": "u-seed",
            "name": unit,
            "parents": [hd.settings.hr_documents_drive_root_id],
            "appProperties": {},
        }
        self.folders["pf-seed"] = {
            "id": "pf-seed",
            "name": "Fake A",
            "parents": ["u-seed"],
            "appProperties": {hd.MEMBER_PROP: member},
        }
        return "pf-seed"

    async def move_file(self, file_id, from_parent_id, to_parent_id):
        self.calls.append(("move", file_id))
        return await super().move_file(file_id, from_parent_id, to_parent_id)


async def _set(pool: asyncpg.Pool, member: str, profile: Any) -> int:  # noqa: F811
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = $2 RETURNING id",
            profile,
            member,
        )


async def _profile(pool: asyncpg.Pool, member: str) -> Any:  # noqa: F811
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT profile::text FROM hr_employees WHERE team_member_id = $1", member
        )
    return json.loads(raw)


def _manifest(*entries: tuple[int, str, str]) -> dict[tuple[int, str], str]:
    return {(i, slot): job.sha256_hex(v) for i, slot, v in entries}


def _untouched(drive: Drive, *sources: str) -> None:
    for src in sources:
        assert src not in drive.trashed
        assert ("move", src) not in drive.calls
        assert drive.files[src]["parent"] == "legacy-folder"
        assert drive.files[src]["appProperties"] == {}


@pytest.mark.asyncio
async def test_a_nik_like_value_outside_the_manifest_never_reaches_drive(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    drive = Drive()
    drive.add_source(L1)
    drive.add_source(L2)
    emp = await _set(
        pool,
        "tm-a",
        {"unit": "Tax", "docs": {"ktp": NIK, "kk": NIK2, "npwp": L1, "bpjs_kes": L2}},
    )
    # kk is IN the manifest by hash, but digits only: the second fence keeps it from Drive too;
    # bpjs_kes is id-shaped but the manifest holds the hash of ANOTHER value: changed
    manifest = _manifest((emp, "kk", NIK2), (emp, "npwp", L1), (emp, "bpjs_kes", L3))
    with caplog.at_level(logging.DEBUG):
        counts = await job.run(pool, drive, manifest, apply=True)
    assert counts["unlisted"] == 1 and counts["fence_failed"] == 1  # ktp, kk
    assert counts["done"] == 1 and counts["changed"] == 1 and job.per_entry(counts) == 3
    assert NIK not in repr(drive.calls) and NIK2 not in repr(drive.calls)
    assert L2 not in repr(drive.calls)
    assert NIK not in caplog.text and NIK2 not in caplog.text and L1 not in caplog.text
    p = await _profile(pool, "tm-a")
    assert p["docs"]["ktp"] == NIK and p["docs"]["kk"] == NIK2  # left in place for a human
    assert project_documents(p)["npwp"]["file_id"] == drive.copies[0]
    _untouched(drive, L1)


@pytest.mark.asyncio
async def test_a_door_write_between_copy_and_cas_is_a_conflict(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    door_id = "door0upload0id0000000000000000001"

    async def door_upload_lands(copy_id: str) -> None:
        async with pool.acquire() as other:
            async with other.transaction():  # the member lock is held by the job for the slot
                assert not await other.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", "tm-a"
                )
            # the door's own write of an upload, forced past that lock (the only way one can land)
            await other.fetchrow(
                hd._WRITE_SQL[("ktp", False)], "tm-a", "ktp", door_id, "zero@balizero.com", "now"
            )

    async def lock_still_held(copy_id: str) -> None:
        async with pool.acquire() as other, other.transaction():
            assert not await other.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('hr_docs_folder:'||$1))", "tm-a"
            )

    drive.on_copy = door_upload_lands
    drive.on_trash = lock_still_held
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["conflict"] == 1 and counts["done"] == 0 and counts["copy_left"] == 0
    assert drive.trashed == drive.copies  # the copy, by its own Drive id; never the source
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == door_id
    assert counts["source_baselined"] == counts["audit_unchanged"] == 1
    _untouched(drive, L1)


@pytest.mark.asyncio
async def test_after_an_uncertain_commit_a_rerun_reuses_the_copy(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L2)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"npwp": L2}})
    manifest = _manifest((emp, "npwp", L2))
    real_commit = Transaction.commit

    async def lost_at_commit(self: Any) -> None:
        raise ConnectionResetError  # the connection drops: the server rolls back

    monkeypatch.setattr(Transaction, "commit", lost_at_commit)
    first = await job.run(pool, drive, manifest, apply=True)
    monkeypatch.setattr(Transaction, "commit", real_commit)
    assert first["uncertain"] == 1 and len(drive.copies) == 1 and not drive.trashed
    assert (await _profile(pool, "tm-a"))["docs"]["npwp"] == L2  # still the legacy value

    second = await job.run(pool, drive, manifest, apply=True)
    assert second["reused"] == 1 and len(drive.copies) == 1  # found through nz_hr_src
    p = await _profile(pool, "tm-a")
    assert p["docs"]["npwp"] == drive.copies[0] == p["doc_proof"]["npwp"]["id"]
    assert p["doc_proof"]["npwp"]["by"] == job.ACTOR
    _untouched(drive, L2)


@pytest.mark.asyncio
async def test_the_contract_slot_drops_docs_contract(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L3)
    profile = {
        "unit": "Tax",
        "contract": {"number": "C-1", "file_id": L3},
        "docs": {"contract": L3},
    }
    emp = await _set(pool, "tm-a", profile)
    counts = await job.run(pool, drive, _manifest((emp, "contract", L3)), apply=True)
    assert counts["done"] == 1
    p = await _profile(pool, "tm-a")
    copy_id = drive.copies[0]
    assert "contract" not in p["docs"]
    assert p["contract"] == {"number": "C-1", "file_id": copy_id}
    assert p["doc_proof"]["contract"]["id"] == copy_id
    copy = drive.files[copy_id]
    assert copy["name"].startswith("Kontrak - Fake A - ") and copy["name"].endswith(".pdf")
    assert copy["appProperties"] == {
        hd.MEMBER_PROP: "tm-a",
        hd.SLOT_PROP: "contract",
        job.SRC_PROP: job.sha256_hex(L3)[:16],
    }
    _untouched(drive, L3)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_a_second_run_changes_nothing(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    for src in (L1, L2, L3):
        drive.add_source(src)
    a = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})
    t = await _set(pool, "tm-t1", {"unit": "Ops", "docs": {"bpjs_tk": L3}})
    manifest = _manifest((a, "ktp", L1), (a, "kk", L2), (t, "bpjs_tk", L3))
    before = (await _profile(pool, "tm-a"), await _profile(pool, "tm-t1"))

    dry = await job.run(pool, drive, manifest, apply=False)
    assert dry["eligible"] == 3 and dry["source:pdf"] == 3
    assert job.LIVE.acceptance5 == "nothing_migrated"
    assert {c[0] for c in drive.calls} == {"get"} and not drive.folders
    assert (await _profile(pool, "tm-a"), await _profile(pool, "tm-t1")) == before

    first = await job.run(pool, drive, manifest, apply=True)
    assert first["done"] == 3 and first["source_baselined"] == first["audit_unchanged"] == 3
    assert job.LIVE.acceptance5 == "observed" and first["copy_held"] == 3
    after = (await _profile(pool, "tm-a"), await _profile(pool, "tm-t1"))
    second = await job.run(pool, drive, manifest, apply=True)
    assert second["changed"] == 3 and second["done"] == second["reused"] == 0
    assert len(drive.copies) == 3 and not drive.trashed
    assert (await _profile(pool, "tm-a"), await _profile(pool, "tm-t1")) == after
    _untouched(drive, L1, L2, L3)


@pytest.mark.asyncio
async def test_an_unreadable_source_is_left_in_place(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()  # L1 is not in this Drive: zero@ cannot read it
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["drive_error:404"] == 1 and not drive.copies
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1


def test_a_bound_sentry_client_stops_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    monkeypatch.setattr(
        sentry_sdk, "get_client", lambda: type("C", (), {"is_active": lambda s: True})()
    )
    with pytest.raises(job.Fatal) as err:
        job.assert_no_sentry()
    assert err.value.code == "sentry_bound"


def test_the_job_refuses_to_run_outside_isolated_mode() -> None:
    script = Path(job.__file__).resolve()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=60)
    assert done.returncode == 2 and done.stdout == b""
    assert done.stderr == b"migrate_hr_legacy_documents: run with python -I\n"


@pytest.mark.asyncio
async def test_real_client_copies_with_the_door_shape_and_only_reads_the_source(  # noqa: F811
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    from urllib.parse import unquote_plus

    from backend.tests.migrations.test_hr_document_door_path import _real_drive

    source = {"id": L1, "mimeType": "image/jpeg", "parents": ["legacy"], "modifiedTime": "t"}
    unit = {"files": [{"id": "u1", "name": "TAX", "mimeType": hd.FOLDER_MIME, "parents": ["r"]}]}
    person = {"files": [{"id": "pf1", "name": "x", "parents": ["u1"]}]}
    copied = {"id": "CopyReturnedByDrive1234", "mimeType": "image/jpeg", "parents": ["pf1"]}
    drive, http = _real_drive(
        [
            (200, source),  # the baseline, before any mutation (R5.4)
            (200, person),  # member_folder: the person folders of the member
            (200, unit),  # ensure_folder: the unit folder
            (200, person),  # ensure_folder: the person folder
            (200, {"files": []}),  # the reuse lookup
            (200, source),
            (200, copied),
            (200, source),  # the audit
            (200, person),  # the copy census: the person folder
            (200, {"files": [copied]}),  # the copy census: the entry's tagged files
        ]
    )
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    with caplog.at_level(logging.DEBUG):
        counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["done"] == 1 and counts["source_baselined"] == counts["audit_unchanged"] == 1
    sent = [(method, unquote_plus(uri), body) for uri, method, body, _ in http.request_sequence]
    assert [m for m, _, _ in sent] == ["GET"] * 6 + ["POST"] + ["GET"] * 3
    assert counts["copy_held"] == 1 and counts["copy_residual"] == 0
    on_source = [(m, u) for m, u, _ in sent if f"/files/{L1}" in u]
    # baseline, read, copy, audit: a source is only ever read, and copied
    assert [m for m, _ in on_source] == ["GET", "GET", "POST", "GET"]
    assert f"/files/{L1}/copy?" in on_source[2][1] and "supportsAllDrives=true" in on_source[2][1]
    assert "mimeType != 'application/vnd.google-apps.folder'" in sent[4][1]  # the reuse lookup
    assert f"{job.SRC_PROP}' and value='{job.sha256_hex(L1)[:16]}'" in sent[4][1]
    body = json.loads(sent[6][2])
    assert body["parents"] == ["pf1"] and body["name"].startswith("KTP - Fake A - ")
    assert body["name"].endswith(".jpg")
    assert body["appProperties"] == {
        hd.MEMBER_PROP: "tm-a",
        hd.SLOT_PROP: "ktp",
        job.SRC_PROP: job.sha256_hex(L1)[:16],
    }
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == copied["id"]
    assert L1 not in caplog.text and copied["id"] not in caplog.text


async def _door_write_after_the_copy(pool: asyncpg.Pool, copy_id: str) -> None:  # noqa: F811
    async with pool.acquire() as other:
        await other.fetchrow(
            hd._WRITE_SQL[("ktp", False)], "tm-a", "ktp", "door0upload0id0000000000000000001",
            "zero@balizero.com", "now",
        )  # fmt: skip


async def _seed_conflict(pool: asyncpg.Pool, drive: Drive) -> int:  # noqa: F811
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})

    async def door_lands(copy_id: str) -> None:
        await _door_write_after_the_copy(pool, copy_id)

    drive.on_copy = door_lands
    return emp


@pytest.mark.asyncio
async def test_a_failed_trash_on_a_conflict_is_counted_as_copy_left(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    emp = await _seed_conflict(pool, drive)
    drive.fail_trash = True
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["conflict"] == 1 and counts["copy_left"] == 1 and counts["done"] == 0
    assert not drive.trashed and len(drive.copies) == 1
    _untouched(drive, L1)


@pytest.mark.asyncio
async def test_a_db_error_at_the_cas_step_trashes_the_copy_and_leaves_the_slot(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    bad = "SELECT $1::text, $2::text, $3::text, $4::text, $5::text, $6::text, 1/0"
    monkeypatch.setitem(job.CAS_SQL, "ktp", bad)
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["db_error"] == 1 and counts["copy_left"] == 0 and counts["done"] == 0
    assert len(drive.copies) == 1 and drive.trashed == drive.copies
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1
    _untouched(drive, L1)


@pytest.mark.asyncio
async def test_a_commit_that_landed_but_whose_answer_was_lost_is_done(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    real_commit = Transaction.commit
    calls: list[int] = []

    async def commit_then_lose(self: Any) -> None:
        await real_commit(self)
        if not calls:
            calls.append(1)
            raise ConnectionResetError

    monkeypatch.setattr(Transaction, "commit", commit_then_lose)
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["done"] == 1 and counts["uncertain"] == 0
    p = await _profile(pool, "tm-a")
    assert project_documents(p)["ktp"]["file_id"] == drive.copies[0]
    _untouched(drive, L1)


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [True, False])
async def test_the_slot_is_re_read_before_any_drive_call(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, apply: bool
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    real_reread = job._eligible_under_lock
    landed: list[bool] = []

    async def door_writes_before_the_first_locked_re_read(conn: Any, item: Any) -> Any:
        if not landed:  # after classify, before the first locked re-read (baseline or slot)
            landed.append(True)
            await _door_write_after_the_copy(pool, "")
        return await real_reread(conn, item)

    monkeypatch.setattr(job, "_eligible_under_lock", door_writes_before_the_first_locked_re_read)
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=apply)
    assert counts["changed"] == 1 and counts["done"] == counts["eligible"] == 0
    assert ("get", L1) not in drive.calls
    assert not [c for c in drive.calls if c[0] == "get"]  # no Drive read of any file
    assert not drive.copies


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_the_source_is_never_reused_or_trashed(  # noqa: F811
    pool: asyncpg.Pool, conflict: bool
) -> None:
    drive = Drive()
    drive.add_source(L1)
    person = drive.seed_person()
    drive.files[L1]["parent"] = person
    drive.files[L1]["appProperties"] = {
        hd.MEMBER_PROP: "tm-a",
        hd.SLOT_PROP: "ktp",
        job.SRC_PROP: hashlib.sha256(L1.encode()).hexdigest()[:16],
    }
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})

    async def door_lands(copy_id: str) -> None:
        await _door_write_after_the_copy(pool, copy_id)

    if conflict:
        drive.on_copy = door_lands
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert len(drive.copies) == 1 and L1 not in drive.trashed and ("trash", L1) not in drive.calls
    if conflict:
        assert counts["conflict"] == 1 and drive.trashed == drive.copies
    else:
        assert counts["done"] == 1 and counts["reused"] == 0 and not drive.trashed
        slot = project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"]
        assert slot == drive.copies[0] != L1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"id": None}, {"id": ""}])
async def test_a_copy_without_a_usable_id_writes_nothing(  # noqa: F811
    pool: asyncpg.Pool, answer: dict[str, Any]
) -> None:
    drive = Drive()
    drive.add_source(L1)
    drive.copy_result = answer
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["copy_unidentified"] == counts["returned_unusable"] == 1
    assert counts["done"] == 0 and len(drive.copies) == 1 and not drive.trashed
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1
    assert "doc_proof" not in await _profile(pool, "tm-a")


_API = Path(__file__).resolve().parents[3]  # apps/backend-rag


def test_a_sealed_run_cannot_leak_through_any_channel() -> None:
    script = (
        "import logging, os, sys\n"
        "sys.path.insert(0, os.getcwd())\n"
        "import backend.scripts.migrate_hr_legacy_documents as job\n"
        f"NIK = {NIK!r}\n"
        "fd = job.seal_streams()\n"
        "print(NIK)\n"
        "sys.stderr.write(NIK)\n"
        "os.write(1, NIK.encode())\n"
        "os.write(2, NIK.encode())\n"
        "logging.getLogger('x').error(NIK)\n"
        "job.emit(fd, {'failed': 'internal_error'})\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_API,
        env=os.environ.copy(),
        capture_output=True,
        timeout=120,
    )
    assert done.stdout == b'{"failed": "internal_error"}\n'
    assert done.stderr == b""


def test_a_failed_import_prints_one_code_and_nothing_else(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("JWT_SECRET_KEY", "API_KEYS")}
    done = subprocess.run(
        [sys.executable, "-I", str(Path(job.__file__).resolve())],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=120,
    )
    assert done.returncode == 1
    assert done.stdout == b'{"failed": "import_failed"}\n'
    assert done.stderr == b""


def test_render_rebuilds_the_line_from_the_closed_grammar() -> None:
    fallback = b'{"failed": "internal_error"}\n'
    ok = {
        "acceptance5": "nothing_migrated",
        "manifest": 2,
        "mode": "dry-run",
        "outcomes": {"eligible": 1, "changed": 1, "unlisted": 4},
        "run": "interrupted",
    }
    line = job.render(ok)
    assert line == (
        b'{"acceptance5": "nothing_migrated", "manifest": 2, "mode": "dry-run",'
        b' "outcomes": {"changed": 1, "eligible": 1, "unlisted": 4}, "run": "interrupted"}\n'
    )
    assert job.exit_code(line) == 130
    assert job.exit_code(job.render({**ok, "run": "completed"})) == 0
    assert job.exit_code(job.render({**ok, "run": "deadline"})) == 124
    for bad in (
        {**ok, "run": "paused"},
        {**ok, "acceptance5": "maybe"},
        {**ok, "manifest": 3},  # the per-entry codes no longer sum to the manifest
        {**ok, "outcomes": {"eligible": 2, "1" + "A" * 32: 1}},
        {k: v for k, v in ok.items() if k != "acceptance5"},
    ):
        assert job.render(bad) == fallback and job.exit_code(job.render(bad)) == 1
    for code in sorted(job.FAILED_CODES):
        assert job.render({"failed": code}) == b'{"failed": "%s"}\n' % code.encode()
        assert job.exit_code(job.render({"failed": code})) == 1
    assert job.render({"failed": "anything"}) == fallback
    with_counts = {"failed": "internal_error", "outcomes": {"done": 1, "copy_left": 1}}
    assert job.render(with_counts, 3) == (
        b'{"failed": "internal_error", "outcomes": {"copy_left": 1, "done": 1}}\n'
    )
    assert job.render(with_counts, 0) == fallback  # more entries than the manifest
    assert job.render(with_counts) == fallback  # no manifest to bound them


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_another_manifest_source_with_the_same_tags_is_never_reused_or_trashed(  # noqa: F811
    pool: asyncpg.Pool, conflict: bool
) -> None:
    drive = Drive()
    drive.add_source(L1)
    drive.add_source(L2)
    person = drive.seed_person()
    drive.files[L2]["parent"] = person
    drive.files[L2]["appProperties"] = {
        hd.MEMBER_PROP: "tm-a",
        hd.SLOT_PROP: "ktp",
        job.SRC_PROP: job.sha256_hex(L1)[:16],
    }
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})

    async def door_lands(copy_id: str) -> None:
        await _door_write_after_the_copy(pool, copy_id)

    if conflict:
        drive.on_copy = door_lands
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1), (emp, "kk", L2)), apply=True)
    assert counts["reused"] == 0 and len(drive.copies) == 2  # a fresh copy for ktp, one for kk
    assert L2 not in drive.trashed and ("trash", L2) not in drive.calls
    docs = project_documents(await _profile(pool, "tm-a"))
    if conflict:
        assert counts["conflict"] == 1 and counts["done"] == 1
    else:
        assert counts["done"] == 2 and docs["ktp"]["file_id"] == drive.copies[0] != L2
        assert docs["kk"]["file_id"] == drive.copies[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("apply", [True, False])
async def test_a_person_folder_that_is_a_source_is_never_used_or_moved(  # noqa: F811
    pool: asyncpg.Pool, apply: bool
) -> None:
    drive = Drive()
    drive.add_source(L1)
    drive.folders["u-other"] = {
        "id": "u-other",
        "name": "OPS",
        "parents": [hd.settings.hr_documents_drive_root_id],
        "appProperties": {},
    }
    drive.folders[L2] = {  # the person folder IS the value of tm-a's kk slot
        "id": L2,
        "name": "Fake A",
        "parents": ["u-other"],
        "appProperties": {hd.MEMBER_PROP: "tm-a"},
    }
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})
    before = await _profile(pool, "tm-a")
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1), (emp, "kk", L2)), apply=apply)
    assert counts["folder:source_protected"] == 2 and counts["done"] == 0
    assert ("move", L2) not in drive.calls and drive.folders[L2]["parents"] == ["u-other"]
    assert not drive.copies and not drive.moves and not drive.trashed
    assert await _profile(pool, "tm-a") == before


@pytest.mark.asyncio
async def test_a_resume_needs_no_readable_source(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    manifest = _manifest((emp, "ktp", L1))
    real_commit = Transaction.commit

    async def lost_before_commit(self: Any) -> None:
        raise ConnectionResetError

    monkeypatch.setattr(Transaction, "commit", lost_before_commit)
    first = await job.run(pool, drive, manifest, apply=True)
    monkeypatch.setattr(Transaction, "commit", real_commit)
    assert first["uncertain"] == 1 and len(drive.copies) == 1

    del drive.files[L1]  # the source can no longer be read: its get_file_metadata is a 404
    second = await job.run(pool, drive, manifest, apply=True)
    assert second["reused"] == 1 and second["source_unreadable"] == 1
    assert job.LIVE.acceptance5 == "not_demonstrated"  # the reused copy's source was not observed
    assert second["drive_error:404"] == 0 and len(drive.copies) == 1 and not drive.trashed
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == drive.copies[0]


@pytest.mark.asyncio
async def test_trash_refuses_a_source_id() -> None:
    drive = Drive()
    drive.add_source(L1)
    protected = frozenset({job.sha256_hex(L1)})
    assert await job._trash(drive, L1, protected) is False
    assert L1 not in drive.trashed and ("trash", L1) not in drive.calls
    assert await job._trash(drive, L2, protected) is True and drive.trashed == [L2]


# ── SPEC v2.2 R5.1: one protected Drive boundary ──

_P = frozenset({job.sha256_hex(L1)})
_OK = "fo-ok"
_MUTATIONS = [
    ("trash_file", (L1,)),
    ("move_file", (L1, "u-a", "u-b")),
    ("move_file", ("pf", L1, "u-b")),
    ("move_file", ("pf", f"u-a,{L1}", "u-b")),
    ("move_file", ("pf", "", "u-b")),
    ("move_file", ("pf", "u-a", L1)),
    ("create_folder", ("x", L1)),
    ("create_folder", ("x", None)),
    ("copy_file", (L2, L1, "t", {})),
    ("copy_file", ("", _OK, "t", {})),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "args"), _MUTATIONS)
async def test_the_boundary_refuses_a_protected_id_in_every_position(
    method: str, args: tuple[Any, ...]
) -> None:
    inner = Drive()
    pd = job.ProtectedDrive(inner, _P)
    with pytest.raises(job.SourceProtected):
        await getattr(pd, method)(*args)
    assert pd.refusals == 1 and not inner.calls and not inner.moves and not inner.trashed
    assert not inner.copies and not inner.folders


@pytest.mark.asyncio
async def test_the_boundary_passes_unprotected_mutations_and_every_read() -> None:
    inner = Drive()
    inner.add_source(L1)
    pd = job.ProtectedDrive(inner, _P)
    made = await pd.create_folder("x", _OK)
    copied = await pd.copy_file(L1, made["id"], "t", {"k": "v"})  # a source may be READ by a copy
    await pd.move_file(made["id"], _OK, "u-b")
    await pd.trash_file(copied["id"])
    assert (await pd.get_file_metadata(L1))["id"] == L1
    assert await pd.find_folder("x", "u-b") is not None
    assert await pd.find_folders_by_app_property("k", "v") == []
    assert await pd.list_files_by_app_properties({"k": "v"}, made["id"], None) == []
    assert pd.refusals == 0 and L1 not in inner.trashed and inner.trashed == [copied["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [{"id": L1}, {"id": ""}, {"id": None}, {}, []])
async def test_the_boundary_rejects_an_unusable_or_protected_returned_id(returned: Any) -> None:
    inner = Drive()
    inner.add_source(L2)
    inner.copy_result = returned
    pd = job.ProtectedDrive(inner, _P)
    with pytest.raises(job.UnusableReturnedId):
        await pd.copy_file(L2, _OK, "t", {})
    assert pd.unusable == 1 and pd.refusals == 0 and ("copy", L2, _OK) in inner.calls


@pytest.mark.parametrize("name", ["upload_file_to_folder", "service", "update", "_n"])
def test_the_boundary_exposes_nothing_else(name: str) -> None:
    pd = job.ProtectedDrive(Drive(), _P)
    with pytest.raises(AttributeError):
        getattr(pd, name)


def _protected_folder_on_the_nth_lookup(drive: Drive, n: int, fid: str, unit_id: str) -> None:
    """The person-folder lookups disagree: the n-th returns a manifest source under `unit_id`."""
    real = drive.find_folders_by_app_property
    seen = {"n": 0}

    async def lookup(key: str, value: str, **kwargs: Any) -> Any:
        seen["n"] += 1
        if seen["n"] == n:
            return [
                {"id": fid, "name": "Fake A", "parents": [unit_id], "appProperties": {key: value}}
            ]
        return await real(key, value, **kwargs)

    drive.find_folders_by_app_property = lookup  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_lookups_that_disagree_never_move_a_source(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    drive.folders["u-other"] = {
        "id": "u-other",
        "name": "OPS",
        "parents": [hd.settings.hr_documents_drive_root_id],
        "appProperties": {},
    }
    # the job's pre-check sees no folder; ensure_folder's own lookup then returns a manifest
    # source (tm-a's kk value) under another unit, which the door would move
    _protected_folder_on_the_nth_lookup(drive, 2, L2, "u-other")
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})
    before = await _profile(pool, "tm-a")
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1), (emp, "kk", L2)), apply=True)
    assert counts["folder:source_protected"] == 2 and counts["protected_refusal"] == 1
    assert not drive.moves and ("move", L2) not in drive.calls
    assert not drive.copies and not drive.trashed
    assert await _profile(pool, "tm-a") == before


@pytest.mark.asyncio
async def test_the_unit_follow_up_never_moves_a_source(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    drive.seed_person("tm-a", "TAX")
    # lookups 1 and 2 (pre-check, folder step) find the real person folder; the follow-up after
    # the commit (3) returns a manifest source while the card's unit has changed meanwhile
    _protected_folder_on_the_nth_lookup(drive, 3, L2, "u-seed")

    async def unit_changes(copy_id: str) -> None:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE hr_employees SET profile = jsonb_set(profile, '{unit}', '\"Ops\"')"
                " WHERE team_member_id = 'tm-a'"
            )

    drive.on_copy = unit_changes
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1), (emp, "kk", L2)), apply=True)
    assert counts["done"] >= 1 and counts["protected_refusal"] >= 1
    assert not any(m[0] == L2 for m in drive.moves) and ("move", L2) not in drive.calls
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == drive.copies[0]
    _untouched(drive, L1)


# ── SPEC v2.2 R5.3: interruption is a drain; R5.4: baseline and audit ──

_PER_SLOT = re.compile(
    r"done|reused|changed|conflict|uncertain|copy_unidentified|not_started|source_folder"
    r"|eligible|lock_timeout|db_error|drive_error:.*|folder:.*"
)


def _per_slot(counts: Any) -> int:
    return sum(v for k, v in counts.items() if _PER_SLOT.fullmatch(k))


def _no_folder_step_for(drive: Drive, member: str) -> None:
    """After the stop flag, no folder step ran for a member not yet prepared."""
    mine = [f for f in drive.folders.values() if f["appProperties"].get(hd.MEMBER_PROP) == member]
    assert not mine


async def _three_slots(pool: asyncpg.Pool, drive: Drive) -> dict[tuple[int, str], str]:  # noqa: F811
    for src in (L1, L2, L3):
        drive.add_source(src)
    a = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})
    t = await _set(pool, "tm-t1", {"unit": "Ops", "docs": {"bpjs_tk": L3}})
    return _manifest((a, "ktp", L1), (a, "kk", L2), (t, "bpjs_tk", L3))


@pytest.mark.asyncio
async def test_the_job_and_the_door_hold_the_same_boundary(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    seen: list[Any] = []
    real = job.process_slot

    async def spy(svc: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(svc.drive)
        await real(svc, *args, **kwargs)

    monkeypatch.setattr(job, "process_slot", spy)
    await job.run(pool, drive, manifest, apply=True)
    assert len(seen) == 3 and all(d is seen[0] for d in seen)
    assert type(seen[0]) is job.ProtectedDrive and seen[0]._inner is drive


@pytest.mark.asyncio
async def test_an_unidentified_copy_is_reused_by_the_next_run(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    manifest = _manifest((emp, "ktp", L1))
    drive.copy_result = {"id": None}
    first = await job.run(pool, drive, manifest, apply=True)
    assert first["copy_unidentified"] == 1 and len(drive.copies) == 1
    drive.copy_result = None
    second = await job.run(pool, drive, manifest, apply=True)
    assert second["reused"] == 1 and len(drive.copies) == 1 and not drive.trashed
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == drive.copies[0]


@pytest.mark.asyncio
async def test_a_signal_during_a_copy_drains_after_that_slot(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    stop = job.Stop()

    async def signal_arrives(copy_id: str) -> None:
        stop.set()
        stop.set()  # a second signal changes nothing

    drive.on_copy = signal_arrives
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["done"] == 1 and counts["not_started"] == 2 and _per_slot(counts) == 3
    assert counts["source_baselined"] == counts["audit_unchanged"] == 3  # the audit still ran
    assert len(drive.copies) == 1 and not drive.trashed
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == drive.copies[0]
    assert (await _profile(pool, "tm-a"))["docs"]["kk"] == L2  # never started
    _no_folder_step_for(drive, "tm-t1")
    _untouched(drive, L1, L2, L3)


@pytest.mark.asyncio
async def test_a_signal_during_the_conflict_trash_lets_the_trash_finish(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    emp = await _seed_conflict(pool, drive)
    drive.add_source(L2)
    t = await _set(pool, "tm-t1", {"unit": "Ops", "docs": {"bpjs_tk": L2}})
    stop = job.Stop()

    async def signal_arrives(copy_id: str) -> None:
        stop.set()

    drive.on_trash = signal_arrives
    manifest = _manifest((emp, "ktp", L1), (t, "bpjs_tk", L2))
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["conflict"] == 1 and counts["copy_left"] == 0 and counts["not_started"] == 1
    assert drive.trashed == drive.copies and len(drive.copies) == 1
    _no_folder_step_for(drive, "tm-t1")
    _untouched(drive, L1, L2)


@pytest.mark.asyncio
async def test_a_signal_during_the_compensation_lets_it_finish(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    bad = "SELECT $1::text, $2::text, $3::text, $4::text, $5::text, $6::text, 1/0"
    monkeypatch.setitem(job.CAS_SQL, "ktp", bad)
    stop = job.Stop()

    async def signal_arrives(copy_id: str) -> None:
        stop.set()

    drive.on_trash = signal_arrives
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["db_error"] == 1 and counts["copy_left"] == 0 and counts["not_started"] == 2
    assert drive.trashed == drive.copies and len(drive.copies) == 1
    _no_folder_step_for(drive, "tm-t1")
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1


@pytest.mark.asyncio
async def test_a_failed_compensation_is_counted_as_copy_left(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    bad = "SELECT $1::text, $2::text, $3::text, $4::text, $5::text, $6::text, 1/0"
    monkeypatch.setitem(job.CAS_SQL, "ktp", bad)
    drive.fail_trash = True
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["db_error"] == 1 and counts["copy_left"] == 1 and not drive.trashed


@pytest.mark.asyncio
@pytest.mark.parametrize("lands", [True, False])
async def test_a_signal_during_a_lost_commit_still_reconciles(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, lands: bool
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    stop = job.Stop()
    real_commit = Transaction.commit
    calls: list[int] = []

    async def signal_then_lose(self: Any) -> None:
        if calls:
            return await real_commit(self)
        calls.append(1)
        stop.set()
        if lands:
            await real_commit(self)
        raise ConnectionResetError

    monkeypatch.setattr(Transaction, "commit", signal_then_lose)
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["not_started"] == 2 and _per_slot(counts) == 3
    if lands:
        assert counts["done"] == 1 and counts["uncertain"] == 0
        assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == drive.copies[0]
    else:
        assert counts["uncertain"] == 1 and counts["done"] == 0
        assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1
    assert len(drive.copies) == 1 and not drive.trashed  # an uncertain copy is never trashed


@pytest.mark.asyncio
async def test_an_unavailable_reconcile_is_uncertain_and_keeps_the_copy(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})

    async def lost(self: Any) -> None:
        raise ConnectionResetError

    async def unavailable(self: Any, *args: Any) -> Any:
        return None, None

    monkeypatch.setattr(Transaction, "commit", lost)
    monkeypatch.setattr(hd.HRDocumentService, "_reconcile", unavailable)
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["uncertain"] == 1 and len(drive.copies) == 1 and not drive.trashed


@pytest.mark.asyncio
async def test_a_signal_during_the_follow_up_keeps_the_slot_done(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    stop = job.Stop()
    real = hd.HRDocumentService._follow_unit_change

    async def signal_in_follow_up(self: Any, *args: Any) -> None:
        stop.set()
        await real(self, *args)

    monkeypatch.setattr(hd.HRDocumentService, "_follow_unit_change", signal_in_follow_up)
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["done"] == 1 and counts["not_started"] == 2 and _per_slot(counts) == 3


class _Abort(BaseException):
    pass


@pytest.mark.asyncio
async def test_a_success_is_counted_before_the_follow_up(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})

    async def aborts(self: Any, *args: Any) -> None:
        raise _Abort

    monkeypatch.setattr(hd.HRDocumentService, "_follow_unit_change", aborts)
    svc = hd.HRDocumentService(pool, job.ProtectedDrive(drive, _P))
    folder = await job.member_folder(svc, "tm-a", True, _P)
    item = job.Item(emp, "tm-a", "ktp", job.sha256_hex(L1))
    counts: Any = job.Counter()
    with pytest.raises(_Abort):
        await job.process_slot(svc, item, folder, True, _P, counts)
    assert counts["done"] == 1
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == drive.copies[0]


@pytest.mark.asyncio
async def test_a_base_exception_at_the_cas_step_compensates_then_propagates(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})

    class _Clock:
        @staticmethod
        def now(tz: Any) -> Any:
            raise _Abort  # the first statement of the CAS step

    monkeypatch.setattr(job, "datetime", _Clock)
    with pytest.raises(_Abort):
        await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert len(drive.copies) == 1 and drive.trashed == drive.copies
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1


@pytest.mark.asyncio
async def test_a_signal_during_the_audit_lets_it_finish(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    stop = job.Stop()
    real = job.audit

    async def signal_then_audit(*args: Any) -> Any:
        stop.set()
        return await real(*args)

    monkeypatch.setattr(job, "audit", signal_then_audit)
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["done"] == 3 and _per_slot(counts) == 3 and stop.is_set
    assert counts["source_baselined"] == counts["audit_unchanged"] == 3


@pytest.mark.asyncio
async def test_the_audit_sees_a_source_that_changed(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    manifest = await _three_slots(pool, drive)

    async def someone_moves_a_source(copy_id: str) -> None:
        drive.files[L3]["parent"] = "elsewhere"  # outside the job: Drive itself, not the boundary

    drive.on_copy = someone_moves_a_source
    counts = await job.run(pool, drive, manifest, apply=True)
    assert counts["source_baselined"] == 3
    assert counts["audit_changed"] == 1 and counts["audit_unchanged"] == 2
    assert job.LIVE.acceptance5 == "not_demonstrated"


@pytest.mark.asyncio
async def test_baseline_and_audit_count_distinct_sources(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L1}})
    t = await _set(pool, "tm-t1", {"unit": "Ops", "docs": {"bpjs_tk": L2}})  # unreadable
    manifest = _manifest((emp, "ktp", L1), (emp, "kk", L1), (t, "bpjs_tk", L2))
    counts = await job.run(pool, drive, manifest, apply=True)
    assert counts["done"] == 2 and counts["drive_error:404"] == 1
    assert counts["source_baselined"] == 1 and counts["source_unreadable"] == 1
    assert counts["audit_unchanged"] == 1 and counts["audit_unreadable"] == 0
    assert job.LIVE.acceptance5 == "observed"  # both migrated slots share the one observed source


_HARNESS = """
import asyncio, json, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from backend.scripts import migrate_hr_legacy_documents as job
from backend.app.services.hr import hr_documents as hd
from backend.tests.migrations.test_migrate_hr_legacy_documents import Drive, L1, L2, L3

job.MANIFEST = Path(sys.argv[1])
scenario, state = sys.argv[2], Path(sys.argv[3])
drive = Drive()
for src in (L1, L2, L3):
    drive.add_source(src)

def dump():
    state.write_text(json.dumps({
        "copies": drive.copies, "files": drive.files, "folders": drive.folders,
        "trashed": drive.trashed, "n": drive._n,
    }))

class Abort(BaseException):
    pass

if scenario.startswith("SIG"):
    sig = getattr(signal, scenario)

    async def two_signals(copy_id):
        os.kill(os.getpid(), sig)
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), sig)  # the second, during the same slot
        await asyncio.sleep(0.05)

    drive.on_copy = two_signals
elif scenario == "deadline_copy":
    job.SLOT_S = 1

    async def blocked(copy_id):
        dump()  # the copy exists in Drive; its request never returns
        await asyncio.to_thread(time.sleep, 60)

    drive.on_copy = blocked
elif scenario == "deadline_reread":
    job.SLOT_S = 1
    real = job._eligible_under_lock
    calls = []

    async def blocked_reread(conn, item):
        calls.append(1)
        if len(calls) > 3:  # after the three baseline reads: the first slot's own re-read
            await conn.execute("SELECT pg_sleep(30)")
        return await real(conn, item)

    job._eligible_under_lock = blocked_reread
elif scenario == "fatal_cas":
    class Clock:
        @staticmethod
        def now(tz):
            raise Abort  # the first statement of the CAS step

    job.datetime = Clock
    drive.fail_trash = True
elif scenario == "fatal_follow":
    async def aborts(self, *args):
        raise Abort

    hd.HRDocumentService._follow_unit_change = aborts
elif scenario == "stdout_full":
    job.SHUTDOWN_S = 1

async def the_fake():
    return drive

hd.get_hr_drive_service = the_fake
fd = job.seal_streams()
if scenario == "stdout_full":  # a reader that never reads: the line's write blocks
    os.set_blocking(fd, False)
    for size in (4096, 1):  # to the last byte: a short line must not fit either
        try:
            while True:
                os.write(fd, b" " * size)
        except BlockingIOError:
            pass
    os.set_blocking(fd, True)
code = job.main(["--apply"], fd)
dump()
os._exit(code)
"""


async def _entry_path(  # noqa: F811
    pool: asyncpg.Pool,
    tmp_path: Path,
    scenario: str,
    read: bool = True,
) -> tuple[int, Any, Any]:
    """The real entry path (`main`, sealed streams, signal handlers, watchdog) in a subprocess,
    on the throwaway Postgres and the fake Drive; returns (exit code, printed line, Drive state).
    Without `read` nothing reads stdout until the process has ended (asyncio's pipe transport
    would drain it in the background, so a plain Popen); the line is then None."""
    import asyncio

    from backend.tests.migrations.test_hr_card_write_path import SCHEMA, _dsn

    drive = Drive()
    manifest = await _three_slots(pool, drive)
    entries = [[i, slot, digest] for (i, slot), digest in manifest.items()]
    (tmp_path / "m.json").write_text(json.dumps({"entries": entries}))
    sep = "&" if "?" in _dsn() else "?"
    env = {**os.environ, "DATABASE_URL": f"{_dsn()}{sep}search_path={SCHEMA}"}
    argv = [sys.executable, "-c", _HARNESS, str(tmp_path / "m.json"), scenario]
    argv.append(str(tmp_path / "drive.json"))
    state_file = tmp_path / "drive.json"
    if not read:
        pipe = subprocess.PIPE
        with subprocess.Popen(argv, cwd=_API, env=env, stdout=pipe, stderr=pipe) as popen:
            code = await asyncio.to_thread(popen.wait, 120)
            _, err = popen.communicate()
        assert err == b""
        return code, None, json.loads(state_file.read_text()) if state_file.exists() else None
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=_API,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 120)
    assert err == b""
    state = json.loads(state_file.read_text()) if state_file.exists() else None
    return proc.returncode, json.loads(out), state


async def _no_lock_left(pool: asyncpg.Pool) -> None:  # noqa: F811
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT pg_try_advisory_lock(hashtext('hr_docs_folder:tm-a'))")
        await conn.execute("SELECT pg_advisory_unlock_all()")


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM", "SIGHUP"])
async def test_a_real_signal_drains_the_real_entry_path(  # noqa: F811
    pool: asyncpg.Pool, tmp_path: Path, sig: str
) -> None:
    code, line, state = await _entry_path(pool, tmp_path, sig)
    assert code == 130 and line["run"] == "interrupted" and line["manifest"] == 3
    o = line["outcomes"]
    assert o["done"] == 1 and o["not_started"] == 2 and job.per_entry(o) == 3
    assert o["source_baselined"] == o["audit_unchanged"] == 3 and o["copy_held"] == 1
    assert line["acceptance5"] == "observed"
    assert len(state["copies"]) == 1 and state["trashed"] == []
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == state["copies"][0]
    assert (await _profile(pool, "tm-a"))["docs"]["kk"] == L2
    await _no_lock_left(pool)


@pytest.mark.asyncio
async def test_a_request_blocked_past_the_slot_deadline_ends_the_process(  # noqa: F811
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    import time

    began = time.monotonic()
    code, line, state = await _entry_path(pool, tmp_path, "deadline_copy")
    assert time.monotonic() - began < 30  # the worker sleeps 60 s: the watchdog did not wait
    assert code == 124 and line["run"] == "deadline"
    assert line["acceptance5"] == "not_demonstrated"
    o = line["outcomes"]
    assert o["uncertain"] == 1 and o["not_started"] == 2 and job.per_entry(o) == 3
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1  # unwritten: rolled back
    await _no_lock_left(pool)

    drive = Drive()  # the Drive the dead process left: its copy exists, tagged
    drive.files.update(state["files"])
    drive.folders.update(state["folders"])
    drive.copies.extend(state["copies"])
    drive._n = state["n"]
    manifest = await _three_slots_manifest(pool)
    counts = await job.run(pool, drive, manifest, apply=True)
    assert counts["reused"] == 1 and counts["done"] == 2 and len(drive.copies) == 3
    assert counts["copy_held"] == 3 and counts["copy_residual"] == 0


@pytest.mark.asyncio
async def test_a_deadline_in_the_re_read_counts_deadline(  # noqa: F811
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    code, line, _ = await _entry_path(pool, tmp_path, "deadline_reread")
    assert code == 124 and line["run"] == "deadline"
    o = line["outcomes"]
    assert o["deadline"] == 1 and o["not_started"] == 2 and job.per_entry(o) == 3
    await _no_lock_left(pool)


@pytest.mark.asyncio
async def test_a_fatal_after_a_failed_compensation_keeps_copy_left_on_the_line(  # noqa: F811
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    code, line, state = await _entry_path(pool, tmp_path, "fatal_cas")
    assert code == 1 and set(line) == {"failed", "outcomes"}
    assert line["failed"] == "internal_error"
    assert line["outcomes"]["copy_left"] == 1 and line["outcomes"]["db_error"] == 1
    assert len(state["copies"]) == 1 and state["trashed"] == []
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1


@pytest.mark.asyncio
async def test_a_fatal_in_the_follow_up_keeps_the_confirmed_done_on_the_line(  # noqa: F811
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    code, line, state = await _entry_path(pool, tmp_path, "fatal_follow")
    assert code == 1 and line["failed"] == "internal_error" and line["outcomes"]["done"] == 1
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == state["copies"][0]


async def _three_slots_manifest(pool: asyncpg.Pool) -> dict[tuple[int, str], str]:  # noqa: F811
    async with pool.acquire() as conn:
        a = await conn.fetchval("SELECT id FROM hr_employees WHERE team_member_id = 'tm-a'")
        t = await conn.fetchval("SELECT id FROM hr_employees WHERE team_member_id = 'tm-t1'")
    return _manifest((a, "ktp", L1), (a, "kk", L2), (t, "bpjs_tk", L3))


# ── SPEC v2.2 R5.3 precedence and R5.4 census, in-process ──


@pytest.mark.asyncio
async def test_a_signal_during_the_baseline_starts_no_slot(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    stop = job.Stop()
    real = job.baseline

    async def signal_in_baseline(*args: Any) -> Any:
        stop.set()  # no slot in flight
        return await real(*args)

    monkeypatch.setattr(job, "baseline", signal_in_baseline)
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    assert counts["not_started"] == 3 and job.per_entry(counts) == 3 and not drive.copies
    assert counts["source_baselined"] == counts["audit_unchanged"] == 3
    assert job.LIVE.acceptance5 == "nothing_migrated" and not drive.folders


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_a_signal_during_a_folder_step_and_its_precedence(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    drive = Drive()
    manifest = await _three_slots(pool, drive)
    stop = job.Stop()
    real = hd.HRDocumentService.ensure_folder

    async def signal_in_folder_step(self: Any, member_id: str, **kwargs: Any) -> Any:
        stop.set()
        if fails:
            raise hd.HRDocError(hd.HRDocCode.DRIVE_UNAVAILABLE)
        return await real(self, member_id, **kwargs)

    monkeypatch.setattr(hd.HRDocumentService, "ensure_folder", signal_in_folder_step)
    counts = await job.run(pool, drive, manifest, apply=True, stop=stop)
    # tm-a's folder step ENDED: its two slots keep its outcome; tm-t1's never ran
    if fails:
        assert counts["folder:drive_unavailable"] == 2 and counts["not_started"] == 1
    else:
        assert counts["not_started"] == 3
    assert job.per_entry(counts) == 3 and not drive.copies


@pytest.mark.asyncio
async def test_the_census_counts_every_copy_of_an_entry(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    drive.add_source(L2)
    person = drive.seed_person()
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1, "kk": L2}})
    manifest = _manifest((emp, "ktp", L1), (emp, "kk", L2))
    for slot, src, cid in (
        ("ktp", L1, "aa-copy-1"),
        ("ktp", L1, "ab-copy-2"),
        ("kk", L2, "kk-copy"),
    ):
        drive.files[cid] = {  # copies a transport retry and a dead run left behind
            "name": "x",
            "parent": person,
            "mime": "application/pdf",
            "appProperties": {
                hd.MEMBER_PROP: "tm-a",
                hd.SLOT_PROP: slot,
                job.SRC_PROP: job.sha256_hex(src)[:16],
            },
        }
    dry = await job.run(pool, drive, manifest, apply=False)
    assert dry["copy_reusable"] == 2 and dry["copy_residual"] == 1 and dry["eligible"] == 2
    async with pool.acquire() as conn:  # kk changes under the job: its copy becomes residual
        await conn.execute(
            "UPDATE hr_employees SET profile = jsonb_set(profile, '{docs,kk}', '\"other\"')"
            " WHERE team_member_id = 'tm-a'"
        )
    counts = await job.run(pool, drive, manifest, apply=True)
    assert counts["reused"] == 1 and counts["changed"] == 1 and not drive.copies
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == "aa-copy-1"
    assert counts["copy_held"] == 1 and counts["copy_residual"] == 2 and not drive.trashed


@pytest.mark.asyncio
async def test_the_census_skips_an_ambiguous_person_folder(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    drive.seed_person()
    drive.folders["pf-twin"] = {
        "id": "pf-twin",
        "name": "Fake A",
        "parents": ["u-seed"],
        "appProperties": {hd.MEMBER_PROP: "tm-a"},
    }
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["folder:drive_unavailable"] == 1 and counts["census_skipped"] == 1
    assert not drive.copies and not drive.moves


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("where", "outcome", "fatal"),
    [
        ("lock", "db_error", False),
        ("lookup", "drive_error:other", False),
        ("copy", "drive_error:other", False),
        ("lookup_base", "drive_error:other", True),
        ("commit_base", "uncertain", True),
    ],
)
async def test_each_failing_step_counts_exactly_one_outcome(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, where: str, outcome: str, fatal: bool
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    exc: BaseException = _Abort() if fatal else RuntimeError("boom")

    async def raises(*args: Any, **kwargs: Any) -> Any:
        raise exc

    if where == "lock":
        monkeypatch.setattr(job, "_LOCK_SQL", "SELECT 1/0, $1::text")
    elif where.startswith("lookup"):
        monkeypatch.setattr(drive, "list_files_by_app_properties", raises)
    elif where == "copy":
        drive.on_copy = raises
    else:
        monkeypatch.setattr(Transaction, "commit", raises)
    manifest = _manifest((emp, "ktp", L1))
    if fatal:
        with pytest.raises(_Abort):
            await job.run(pool, drive, manifest, apply=True)
    else:
        await job.run(pool, drive, manifest, apply=True)
    counts = job.LIVE.counts
    assert counts[outcome] == 1 and job.per_entry(counts) == 1
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1
    assert not drive.trashed


# ── owner ruling 2026-11-13 (SPEC v2.2 rev 4) ──


@pytest.mark.asyncio
async def test_empty_metadata_is_unreadable_and_not_observed(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    real = drive.get_file_metadata

    async def answers_without_fields(file_id: str) -> Any:
        meta = await real(file_id)
        return {"id": meta["id"], "mimeType": meta["mimeType"]}  # no modifiedTime, no parents

    monkeypatch.setattr(drive, "get_file_metadata", answers_without_fields)
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["done"] == 1 and counts["source_unreadable"] == 1
    assert counts["source_baselined"] == 0 and job.LIVE.acceptance5 == "not_demonstrated"


@pytest.mark.asyncio
async def test_the_census_never_counts_a_tagged_source_as_a_copy(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    person = drive.seed_person()
    drive.files[L1]["parent"] = person
    drive.files[L1]["appProperties"] = {
        hd.MEMBER_PROP: "tm-a",
        hd.SLOT_PROP: "ktp",
        job.SRC_PROP: job.sha256_hex(L1)[:16],
    }
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    dry = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=False)
    assert dry["copy_reusable"] == dry["copy_residual"] == dry["copy_held"] == 0


@pytest.mark.asyncio
async def test_a_truncated_listing_is_census_skipped_and_never_reused(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    person = drive.seed_person()
    drive.files["aa-copy-1"] = {
        "name": "x",
        "parent": person,
        "mime": "application/pdf",
        "appProperties": {
            hd.MEMBER_PROP: "tm-a",
            hd.SLOT_PROP: "ktp",
            job.SRC_PROP: job.sha256_hex(L1)[:16],
        },
    }
    drive.truncated = True
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    dry = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=False)
    assert dry["census_skipped"] == 1 and dry["copy_reusable"] == dry["copy_residual"] == 0
    assert dry["drive_error:other"] == 1 and job.per_entry(dry) == 1  # no reuse from a part


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("lookup", ["files", "folders"])
async def test_the_real_client_signals_a_listing_cut_at_its_page_cap(
    strict: bool, lookup: str
) -> None:
    from backend.tests.migrations.test_hr_document_door_path import _real_drive

    pages = [  # sparse: one folder or file in twenty pages, a page still pending at the cap
        (
            200,
            {
                "files": [{"id": "Only01", "parents": ["pf1"]}] if n == 7 else [],
                "nextPageToken": f"t{n}",
            },
        )
        for n in range(20)
    ]
    drive, http = _real_drive(pages)

    async def call(**kwargs: Any) -> Any:
        if lookup == "folders":
            return await drive.find_folders_by_app_property("k", "v", **kwargs)
        return await drive.list_files_by_app_properties({"k": "v"}, "pf1", None, **kwargs)

    if strict:
        with pytest.raises(RuntimeError):
            await call(strict=True)
    else:
        assert [f["id"] for f in await call()] == ["Only01"]  # the default: a part, unchanged
    assert len(http.request_sequence) == 20


@pytest.mark.asyncio
async def test_a_failed_rollback_before_commit_still_compensates(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    bad = "SELECT $1::text, $2::text, $3::text, $4::text, $5::text, $6::text, 1/0"
    monkeypatch.setitem(job.CAS_SQL, "ktp", bad)
    real_rollback = Transaction.rollback

    async def rollback_fails(self: Any) -> None:
        monkeypatch.setattr(Transaction, "rollback", real_rollback)
        raise ConnectionResetError  # unconfirmed: no COMMIT was ever sent, the server rolls back

    monkeypatch.setattr(Transaction, "rollback", rollback_fails)
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["db_error"] == 1 and job.per_entry(counts) == 1
    assert counts["copy_left"] == 0 and drive.trashed == drive.copies  # re-locked, re-read, trashed
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1


# ── final gate round 4 (5c379990b47a) ──


class _ReleaseFailsOnce:
    """The pool, except that the first connection release raises after it ran."""

    def __init__(self, pool: asyncpg.Pool) -> None:  # noqa: F811
        self._pool, self.failed = pool, False

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        outer, inner = self, self._pool.acquire(*args, **kwargs)

        class Ctx:
            async def __aenter__(self) -> Any:
                return await inner.__aenter__()

            async def __aexit__(self, *exc: Any) -> None:
                await inner.__aexit__(*exc)
                if not outer.failed:
                    outer.failed = True
                    raise ConnectionResetError

        return Ctx()


async def _slot_of(pool: asyncpg.Pool, drive: Drive, emp: int, db_pool: Any = None) -> Any:  # noqa: F811
    svc = hd.HRDocumentService(pool, job.ProtectedDrive(drive, _P))
    folder = await job.member_folder(svc, "tm-a", True, _P)
    if db_pool is not None:
        svc = hd.HRDocumentService(db_pool, job.ProtectedDrive(drive, _P))
    return svc, folder, job.Item(emp, "tm-a", "ktp", job.sha256_hex(L1))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["changed", "conflict"])
async def test_a_failed_release_after_the_outcome_does_not_reclassify_it(  # noqa: F811
    pool: asyncpg.Pool, outcome: str
) -> None:
    drive = Drive()
    if outcome == "conflict":
        emp = await _seed_conflict(pool, drive)
    else:
        drive.add_source(L1)
        emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L2}})  # not the manifest's
    svc, folder, item = await _slot_of(pool, drive, emp, _ReleaseFailsOnce(pool))
    counts: Any = job.Counter()
    job.LIVE.reset(None, "apply")
    await job.process_slot(svc, item, folder, True, _P, counts)
    assert svc.db_pool.failed and counts[outcome] == 1 and job.per_entry(counts) == 1
    assert job.LIVE.step == "settled"  # what a deadline would read: nothing in flight
    assert drive.trashed == drive.copies and counts["copy_left"] == 0


def test_a_deadline_after_the_outcome_adds_nothing_for_that_entry() -> None:
    job.LIVE.reset(3, "apply")
    job.LIVE.counts["conflict"] += 1
    job.LIVE.step = "settled"
    out = job.LIVE.deadline_result()["outcomes"]
    assert out == {"conflict": 1, "not_started": 2}


@pytest.mark.asyncio
async def test_a_fatal_in_the_compensation_keeps_the_outcome_and_copy_left(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    bad = "SELECT $1::text, $2::text, $3::text, $4::text, $5::text, $6::text, 1/0"
    monkeypatch.setitem(job.CAS_SQL, "ktp", bad)

    async def aborts(file_id: str) -> None:
        raise _Abort

    drive.on_trash = aborts
    svc, folder, item = await _slot_of(pool, drive, emp)
    counts: Any = job.Counter()
    with pytest.raises(_Abort):
        await job.process_slot(svc, item, folder, True, _P, counts)
    assert counts["db_error"] == 1 and counts["copy_left"] == 1 and job.per_entry(counts) == 1
    assert (await _profile(pool, "tm-a"))["docs"]["ktp"] == L1


@pytest.mark.asyncio
async def test_a_fatal_in_the_reconcile_keeps_the_entry_uncertain(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = Drive()
    drive.add_source(L1)
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})

    async def lost(self: Any) -> None:
        raise ConnectionResetError

    async def aborts(self: Any, *args: Any) -> Any:
        raise _Abort

    svc, folder, item = await _slot_of(pool, drive, emp)
    monkeypatch.setattr(Transaction, "commit", lost)
    monkeypatch.setattr(hd.HRDocumentService, "_reconcile", aborts)
    counts: Any = job.Counter()
    with pytest.raises(_Abort):
        await job.process_slot(svc, item, folder, True, _P, counts)
    assert counts["uncertain"] == 1 and job.per_entry(counts) == 1
    assert len(drive.copies) == 1 and not drive.trashed  # the next run reuses it


@pytest.mark.asyncio
async def test_a_fatal_in_the_conflict_trash_stays_a_conflict_and_compensates(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    drive = Drive()
    emp = await _seed_conflict(pool, drive)
    calls: list[str] = []

    async def first_aborts(file_id: str) -> None:
        calls.append(file_id)
        if len(calls) == 1:
            raise _Abort

    drive.on_trash = first_aborts
    svc, folder, item = await _slot_of(pool, drive, emp)
    counts: Any = job.Counter()
    with pytest.raises(_Abort):
        await job.process_slot(svc, item, folder, True, _P, counts)
    assert counts["conflict"] == 1 and job.per_entry(counts) == 1 and counts["copy_left"] == 0
    assert len(calls) == 2 and drive.trashed == drive.copies  # compensation trashed it


@pytest.mark.parametrize(
    ("meta", "valid"),
    [
        ({"modifiedTime": "t", "parents": ["p"], "trashed": False}, True),
        ({"modifiedTime": "t", "parents": ["p"]}, True),
        ({"modifiedTime": "t", "parents": ["p"], "trashed": None}, False),
        ({"modifiedTime": "t", "parents": ["p"], "trashed": "false"}, False),
    ],
)
def test_a_present_trashed_field_must_be_a_real_bool(meta: Any, valid: bool) -> None:
    assert (job._fingerprint(meta) is not None) is valid


@pytest.mark.asyncio
async def test_a_line_the_channel_never_takes_still_ends_with_124(  # noqa: F811
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    code, line, _ = await _entry_path(pool, tmp_path, "stdout_full", read=False)
    assert code == 124 and line is None
    await _no_lock_left(pool)


# ── final gate round 5 (6d97f80ba9e1) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["missing", "mismatched"])
async def test_a_copy_the_slot_holds_is_held_whatever_its_proof(  # noqa: F811
    pool: asyncpg.Pool, proof: str
) -> None:
    drive = Drive()
    drive.add_source(L1)
    person = drive.seed_person()
    held = "CopyHeldBySlot0000000000000000001"
    for cid in (held, "CopyResidual00000000000000000002"):
        drive.files[cid] = {
            "name": "x",
            "parent": person,
            "mime": "application/pdf",
            "appProperties": {
                hd.MEMBER_PROP: "tm-a",
                hd.SLOT_PROP: "ktp",
                job.SRC_PROP: job.sha256_hex(L1)[:16],
            },
        }
    profile: dict[str, Any] = {"unit": "Tax", "docs": {"ktp": held}}
    if proof == "mismatched":
        profile["doc_proof"] = {
            "ktp": {"id": "AnotherId0000000000000000000003", "by": "x", "at": "t"}
        }
    emp = await _set(pool, "tm-a", profile)
    assert project_documents(profile)["ktp"]["file_id"] is None  # not verified
    dry = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=False)
    assert dry["copy_held"] == 1 and dry["copy_residual"] == 1 and dry["copy_reusable"] == 0


@pytest.mark.asyncio
async def test_a_truncated_folder_lookup_is_census_skipped(pool: asyncpg.Pool) -> None:  # noqa: F811
    drive = Drive()
    drive.add_source(L1)
    person = drive.seed_person()
    drive.files["aa-copy-1"] = {
        "name": "x",
        "parent": person,
        "mime": "application/pdf",
        "appProperties": {
            hd.MEMBER_PROP: "tm-a",
            hd.SLOT_PROP: "ktp",
            job.SRC_PROP: job.sha256_hex(L1)[:16],
        },
    }
    drive.truncated_folders = True
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    dry = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=False)
    assert dry["census_skipped"] == 1 and dry["copy_reusable"] == dry["copy_residual"] == 0
    assert dry["folder:drive_unavailable"] == 1 and job.per_entry(dry) == 1
