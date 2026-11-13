"""R5 (SPEC v2.1, STAFF_CARD.md C13): the legacy-paper migration against a real Postgres and a fake
Drive. Synthetic data only; the schema has no `security_audit_log`, as production."""

from __future__ import annotations

import json
import logging
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
NIK2 = "3171234567890002"
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
        return {"id": cid, "mimeType": self.files[cid]["mime"], "parents": [parent_id]}

    async def list_files_by_app_properties(self, props, parent_id, mimes):
        self.calls.append(("list", tuple(sorted(props.items())), parent_id))
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
        await super().trash_file(file_id)

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
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": NIK, "kk": NIK2, "npwp": L1}})
    # kk is IN the manifest by hash, but digits only: the second fence keeps it from Drive too
    manifest = _manifest((emp, "kk", NIK2), (emp, "npwp", L1))
    with caplog.at_level(logging.DEBUG):
        counts = await job.run(pool, drive, manifest, apply=True)
    assert counts["outside_manifest"] == 2 and counts["done"] == 1
    assert NIK not in repr(drive.calls) and NIK2 not in repr(drive.calls)
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

    drive.on_copy = door_upload_lands
    counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["conflict"] == 1 and counts["done"] == 0
    assert drive.trashed == drive.copies  # the copy, by its own Drive id; never the source
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == door_id
    assert counts["source_unchanged"] == 1
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
    assert {c[0] for c in drive.calls} == {"get"} and not drive.folders
    assert (await _profile(pool, "tm-a"), await _profile(pool, "tm-t1")) == before

    first = await job.run(pool, drive, manifest, apply=True)
    assert first["done"] == 3 and first["source_unchanged"] == 3
    after = (await _profile(pool, "tm-a"), await _profile(pool, "tm-t1"))
    second = await job.run(pool, drive, manifest, apply=True)
    assert second["verified"] == 3 and second["done"] == second["reused"] == 0
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
            (200, source),
            (200, unit),
            (200, person),
            (200, {"files": []}),
            (200, copied),
            (200, source),
        ]
    )
    emp = await _set(pool, "tm-a", {"unit": "Tax", "docs": {"ktp": L1}})
    with caplog.at_level(logging.DEBUG):
        counts = await job.run(pool, drive, _manifest((emp, "ktp", L1)), apply=True)
    assert counts["done"] == 1 and counts["source_unchanged"] == 1
    sent = [(method, unquote_plus(uri), body) for uri, method, body, _ in http.request_sequence]
    assert [m for m, _, _ in sent] == ["GET", "GET", "GET", "GET", "POST", "GET"]
    on_source = [(m, u) for m, u, _ in sent if f"/files/{L1}" in u]
    assert [m for m, _ in on_source] == ["GET", "POST", "GET"]  # read, copy, read: never a write
    assert f"/files/{L1}/copy?" in on_source[1][1] and "supportsAllDrives=true" in on_source[1][1]
    assert "mimeType != 'application/vnd.google-apps.folder'" in sent[3][1]
    assert f"{job.SRC_PROP}' and value='{job.sha256_hex(L1)[:16]}'" in sent[3][1]
    body = json.loads(sent[4][2])
    assert body["parents"] == ["pf1"] and body["name"].startswith("KTP - Fake A - ")
    assert body["name"].endswith(".jpg")
    assert body["appProperties"] == {
        hd.MEMBER_PROP: "tm-a",
        hd.SLOT_PROP: "ktp",
        job.SRC_PROP: job.sha256_hex(L1)[:16],
    }
    assert project_documents(await _profile(pool, "tm-a"))["ktp"]["file_id"] == copied["id"]
    assert L1 not in caplog.text and copied["id"] not in caplog.text
