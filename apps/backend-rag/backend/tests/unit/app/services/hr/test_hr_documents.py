"""HR document door service (STAFF_CARD.md C13) with a fake Drive. Synthetic data only."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.app.services.hr import hr_documents as hd
from backend.app.services.hr.hr_documents import (
    HRDocCode,
    HRDocError,
    HRDocumentService,
    detect_type,
    document_title,
    unit_folder_name,
)

MEMBER = "11111111-1111-4111-8111-111111111111"
ROOT = hd.settings.hr_documents_drive_root_id


class FakeDrive:
    def __init__(self) -> None:
        self.folders: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.trashed: list[str] = []
        self.moves: list[tuple[str, str, str]] = []
        self.fail_upload = False
        self._n = 0

    def _id(self, p: str) -> str:
        self._n += 1
        return f"{p}{self._n}"

    async def find_folder(self, name: str, parent_id: str) -> dict[str, Any] | None:
        for f in self.folders.values():
            if f["name"] == name and parent_id in f["parents"]:
                return f
        return None

    async def create_folder(self, name, parent_id=None, user_id=None, app_properties=None):
        fid = self._id("fo")
        self.folders[fid] = {
            "id": fid,
            "name": name,
            "parents": [parent_id],
            "appProperties": dict(app_properties or {}),
        }
        return self.folders[fid]

    async def find_folders_by_app_property(self, key: str, value: str):
        return [f for f in self.folders.values() if f["appProperties"].get(key) == value]

    async def move_file(self, file_id, from_parent_id, to_parent_id):
        self.moves.append((file_id, from_parent_id, to_parent_id))
        self.folders[file_id]["parents"] = [to_parent_id]
        return {}

    async def upload_file_to_folder(
        self, folder_id, file_content, file_name, mime_type=None, user_id=None, app_properties=None
    ):
        if self.fail_upload:
            raise RuntimeError("boom Made Contoh")
        fid = self._id("fi")
        self.files[fid] = {
            "name": file_name,
            "parent": folder_id,
            "mime": mime_type,
            "appProperties": dict(app_properties or {}),
        }
        return {"id": fid}

    async def get_document_meta(self, file_id):
        f = self.files.get(file_id)
        if f is None:
            return None
        if file_id in self.folders:  # a folder id is a real Drive object too
            return None
        return {
            "id": file_id,
            "mimeType": f["mime"],
            "trashed": file_id in self.trashed or bool(f.get("trashed")),
            "parents": [f["parent"]],
            "appProperties": dict(f["appProperties"]),
        }

    def seed_person_folder(self, unit: str = "TAX") -> str:
        """The unit folder and the member's person folder, as an earlier upload left them."""
        self.folders["u-seed"] = {
            "id": "u-seed",
            "name": unit,
            "parents": [ROOT],
            "appProperties": {},
        }
        self.folders["pf-seed"] = {
            "id": "pf-seed",
            "name": "Made Contoh",
            "parents": ["u-seed"],
            "appProperties": {hd.MEMBER_PROP: MEMBER},
        }
        return "pf-seed"

    def add_file(self, fid, *, member=MEMBER, slot="kk", mime="application/pdf", parent="pf-seed"):
        props = {}
        if member:
            props[hd.MEMBER_PROP] = member
        if slot:
            props[hd.SLOT_PROP] = slot
        self.files[fid] = {"name": "o", "parent": parent, "mime": mime, "appProperties": props}

    async def trash_file(self, file_id):
        self.trashed.append(file_id)


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool

    async def execute(self, sql: str, *a: Any) -> None:
        self.pool.events.append(
            "lock:member"
            if "hr_docs_folder" in sql
            else "lock:unit"
            if "hr_docs_unit" in sql
            else "x"
        )

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.pool.events.append("context")
        row = self.pool.row
        self.pool.after_context()
        return row

    @asynccontextmanager
    async def transaction(self):
        yield


class _Pool:
    def __init__(self, unit: str, name: str) -> None:
        self.row: Any = {"full_name": name, "employee_id": 1, "unit": unit}
        self.events: list[str] = []
        self.after_context = lambda: None

    @asynccontextmanager
    async def acquire(self):
        yield _Conn(self)


def _svc(drive: FakeDrive, unit: str = "Tax", name: str = "Made Contoh") -> HRDocumentService:
    return HRDocumentService(_Pool(unit, name), drive)


# ── magic bytes ──


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"%PDF-1.7 x", ("application/pdf", "pdf")),
        (b"\xff\xd8\xff\xe0 x", ("image/jpeg", "jpg")),
        (b"\x89PNG\r\n\x1a\nx", ("image/png", "png")),
        (b"GIF89a", None),
        (b"<html>", None),
        (b"", None),
        (b"\x89PNG\r\n", None),
    ],
)
def test_detect_type_reads_the_bytes(data, expected):
    assert detect_type(data) == expected


def test_a_pdf_is_a_pdf_whatever_the_client_called_it():
    # the router never passes a filename or MIME: only bytes reach detect_type
    assert detect_type(b"%PDF-1.4 renamed.png content")[1] == "pdf"


# ── names ──


def test_unit_folder_name_upper_strip_and_fallback():
    assert unit_folder_name("  tax ") == "TAX"
    assert unit_folder_name(None) == "LAINNYA"
    assert unit_folder_name("   ") == "LAINNYA"
    assert unit_folder_name("a\nb") == "A B"


def test_title():
    assert (
        document_title("bpjs_kes", "Made Contoh", date(2026, 11, 12), "pdf")
        == "BPJS Kesehatan - Made Contoh - 2026-11-12.pdf"
    )
    assert document_title("contract", "X", date(2026, 1, 2), "png").startswith("Kontrak - X - ")


# ── folders ──


@pytest.mark.asyncio
async def test_unit_folder_found_is_reused_not_created():
    d = FakeDrive()
    d.folders["u1"] = {"id": "u1", "name": "TAX", "parents": [ROOT], "appProperties": {}}
    fid, _ = await _svc(d).ensure_folder(MEMBER)
    assert not any(f["name"] == "TAX" and f["id"] != "u1" for f in d.folders.values())
    assert d.folders[fid]["parents"] == ["u1"]
    assert d.folders[fid]["name"] == "Made Contoh"
    assert d.folders[fid]["appProperties"] == {hd.MEMBER_PROP: MEMBER}


@pytest.mark.asyncio
async def test_unit_folder_missing_is_created_under_root():
    d = FakeDrive()
    await _svc(d, unit="marketing").ensure_folder(MEMBER)
    unit = [f for f in d.folders.values() if f["name"] == "MARKETING"]
    assert len(unit) == 1 and unit[0]["parents"] == [ROOT]


@pytest.mark.asyncio
async def test_missing_unit_goes_to_lainnya():
    d = FakeDrive()
    await _svc(d, unit="").ensure_folder(MEMBER)
    assert any(f["name"] == "LAINNYA" for f in d.folders.values())


@pytest.mark.asyncio
async def test_existing_person_folder_is_found_and_unit_change_moves_it():
    d = FakeDrive()
    svc = _svc(d, unit="Tax")
    first, _ = await svc.ensure_folder(MEMBER)
    assert d.moves == []
    again, _ = await svc.ensure_folder(MEMBER)
    assert again == first and d.moves == []  # no create, no move
    moved, _ = await _svc(d, unit="Management").ensure_folder(MEMBER)
    assert moved == first and len(d.moves) == 1
    new_unit = d.folders[first]["parents"][0]
    assert d.folders[new_unit]["name"] == "MANAGEMENT"
    assert sum(1 for f in d.folders.values() if f["appProperties"]) == 1


@pytest.mark.asyncio
async def test_two_folders_for_one_member_is_a_drive_error_not_a_guess():
    d = FakeDrive()
    for i in ("a", "b"):
        d.folders[i] = {
            "id": i,
            "name": "x",
            "parents": [ROOT],
            "appProperties": {hd.MEMBER_PROP: MEMBER},
        }
    with pytest.raises(HRDocError) as exc:
        await _svc(d).ensure_folder(MEMBER)
    assert exc.value.code is HRDocCode.DRIVE_UNAVAILABLE


# ── upload flow ──


async def _up(svc: HRDocumentService, **kw: Any) -> dict[str, Any]:
    return await svc.upload(
        actor_id="tm-owner",
        member_id=MEMBER,
        email="dea@balizero.com",
        slot=kw.get("slot", "ktp"),
        data=b"%PDF-1.7",
        mime="application/pdf",
        ext="pdf",
        today=date(2026, 11, 12),
    )


@pytest.mark.asyncio
async def test_upload_title_properties_and_result(monkeypatch):
    d = FakeDrive()
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value=None)  # type: ignore[method-assign]
    res = await _up(svc)
    f = d.files[res["file_id"]]
    assert f["name"] == "KTP - Made Contoh - 2026-11-12.pdf"
    assert f["appProperties"] == {hd.MEMBER_PROP: MEMBER, hd.SLOT_PROP: "ktp"}
    assert f["mime"] == "application/pdf"
    assert res == {"slot": "ktp", "file_id": res["file_id"], "previous": "none"}


@pytest.mark.asyncio
async def test_a_real_previous_upload_of_the_same_slot_is_trashed():
    d = FakeDrive()
    d.seed_person_folder()
    d.add_file("prevfile12345", slot="kk")
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value="prevfile12345")  # type: ignore[method-assign]
    res = await _up(svc, slot="kk")
    assert res["previous"] == "trashed" and d.trashed == ["prevfile12345"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        "folder",
        "other_slot",
        "other_folder",
        "foreign",
        "other_member",
        "trashed",
        "bad_mime",
        "unknown",
    ],
)
async def test_a_previous_that_is_not_exactly_our_file_for_this_slot_is_kept(kind):
    d = FakeDrive()
    pf = d.seed_person_folder()
    d.folders["other-folder"] = {
        "id": "other-folder",
        "name": "z",
        "parents": [ROOT],
        "appProperties": {},
    }
    if kind == "folder":  # a person-folder id pasted into the slot: it carries nz_hr_member too
        d.files["prevfile12345"] = {
            "name": "f",
            "parent": "u-seed",
            "mime": "application/vnd.google-apps.folder",
            "appProperties": {hd.MEMBER_PROP: MEMBER, hd.SLOT_PROP: "kk"},
        }
    elif kind == "other_slot":
        d.add_file("prevfile12345", slot="ktp")
    elif kind == "other_folder":
        d.add_file("prevfile12345", slot="kk", parent="other-folder")
    elif kind == "foreign":
        d.add_file("prevfile12345", member=None, slot=None)
    elif kind == "other_member":
        d.add_file("prevfile12345", member="someone-else", slot="kk")
    elif kind == "trashed":
        d.add_file("prevfile12345", slot="kk")
        d.files["prevfile12345"]["trashed"] = True
    elif kind == "bad_mime":
        d.add_file("prevfile12345", slot="kk", mime="text/html")
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value="prevfile12345")  # type: ignore[method-assign]
    res = await _up(svc, slot="kk")
    assert res["previous"] == "kept" and d.trashed == []
    assert pf in d.folders and "prevfile12345" not in d.trashed


@pytest.mark.asyncio
async def test_db_failure_trashes_the_new_file_and_raises():
    d = FakeDrive()
    svc = _svc(d)
    svc._db_step = AsyncMock(side_effect=HRDocError(HRDocCode.DB_ERROR))  # type: ignore[method-assign]
    with pytest.raises(HRDocError) as exc:
        await _up(svc)
    assert exc.value.code is HRDocCode.DB_ERROR
    assert len(d.trashed) == 1 and d.trashed[0] in d.files


@pytest.mark.asyncio
async def test_drive_upload_error_is_a_fixed_code_without_the_message():
    d = FakeDrive()
    d.fail_upload = True
    svc = _svc(d)
    svc._db_step = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(HRDocError) as exc:
        await _up(svc)
    assert exc.value.code is HRDocCode.DRIVE_UNAVAILABLE
    assert "Contoh" not in str(exc.value) and exc.value.__cause__ is None
    svc._db_step.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_trashes_ours_only():
    d = FakeDrive()
    d.seed_person_folder()
    d.add_file("fileid1234567", slot="kk")
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value="fileid1234567")  # type: ignore[method-assign]
    res = await svc.remove(actor_id="a", member_id=MEMBER, email="e", slot="kk")
    assert res == {"slot": "kk", "file_id": None, "previous": "trashed"}
    d.add_file("fileid7654321", slot="ktp")  # same member, other slot
    svc._db_step = AsyncMock(return_value="fileid7654321")  # type: ignore[method-assign]
    assert (await svc.remove(actor_id="a", member_id=MEMBER, email="e", slot="kk"))[
        "previous"
    ] == "kept"
    svc._db_step = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert (await svc.remove(actor_id="a", member_id=MEMBER, email="e", slot="kk"))[
        "previous"
    ] == "none"


def test_slot_set_is_the_five_docs_plus_contract():
    assert hd.ALL_SLOTS == ("ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract")
    assert set(hd.SLOT_LABELS) == set(hd.ALL_SLOTS)


# ── lock order: member, then context (same transaction), then unit ──


@pytest.mark.asyncio
async def test_member_lock_then_context_then_unit_lock():
    d = FakeDrive()
    svc = _svc(d)
    await svc.ensure_folder(MEMBER)
    ev = [e for e in svc.db_pool.events if e != "x"]
    assert ev == ["lock:member", "context", "lock:unit"]


@pytest.mark.asyncio
async def test_an_upload_that_resumes_after_a_unit_change_uses_the_new_unit():
    d = FakeDrive()
    svc = _svc(d, unit="Tax")

    def change_unit_while_waiting_for_the_lock() -> None:
        pass

    # the unit changes BEFORE this request gets the member lock: the context read (after the lock)
    # must see it
    svc.db_pool.row = {"full_name": "Made Contoh", "employee_id": 1, "unit": "Management"}
    fid, ctx = await svc.ensure_folder(MEMBER)
    assert ctx.unit == "Management"
    assert d.folders[d.folders[fid]["parents"][0]]["name"] == "MANAGEMENT"


@pytest.mark.asyncio
async def test_context_errors_surface_from_inside_the_lock():
    svc = _svc(FakeDrive())
    svc.db_pool.row = None
    with pytest.raises(HRDocError) as e1:
        await svc.ensure_folder(MEMBER)
    assert e1.value.code is HRDocCode.NOT_FOUND
    svc.db_pool.row = {"full_name": "x", "employee_id": None, "unit": ""}
    with pytest.raises(HRDocError) as e2:
        await svc.ensure_folder(MEMBER)
    assert e2.value.code is HRDocCode.HR_RECORD_REQUIRED


# ── nothing unverified reaches the audit or the logs ──

NIKISH = "3171234567890001"


def test_value_sha_is_16_hex_of_sha256():
    import hashlib

    assert hd.value_sha(NIKISH) == hashlib.sha256(NIKISH.encode()).hexdigest()[:16]
    assert len(hd.value_sha("x")) == 16


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [NIKISH, "KTP Made Contoh.pdf", "short", "a" * 300])
async def test_a_previous_value_drive_does_not_know_is_kept_without_a_trace(previous, caplog):
    d = FakeDrive()  # knows no such file: get_document_meta -> None
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value=previous)  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        res = await _up(svc)
    assert res["previous"] == "kept" and d.trashed == []
    assert previous not in caplog.text and previous not in str(res)


@pytest.mark.asyncio
async def test_a_drive_error_on_the_previous_is_kept_and_not_logged(caplog):
    d = FakeDrive()
    prev = "A" * 33

    async def boom(_fid):
        raise RuntimeError(f"drive says {prev} {NIKISH}")

    d.get_document_meta = boom  # type: ignore[method-assign]
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value=prev)  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG):
        res = await _up(svc)
    assert res["previous"] == "kept"
    assert NIKISH not in caplog.text and prev not in caplog.text


# ── commit outcome unknown ──


def _unknown(svc: HRDocumentService, previous: str | None, holds: bool | None) -> None:
    async def step(**kw: Any) -> None:
        kw["state"]["previous"] = previous
        raise HRDocError(HRDocCode.COMMIT_UNKNOWN)

    svc._db_step = step  # type: ignore[method-assign,assignment]
    svc._slot_holds = AsyncMock(return_value=holds)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_commit_unknown_and_the_write_landed_is_a_success_and_trashes_ours():
    d = FakeDrive()
    d.seed_person_folder()
    d.add_file("oldfile12345", slot="ktp")
    svc = _svc(d)
    _unknown(svc, "oldfile12345", True)
    res = await _up(svc)
    assert res["previous"] == "trashed" and res["file_id"] in d.files
    assert d.trashed == ["oldfile12345"]  # the NEW file was not trashed


@pytest.mark.asyncio
async def test_commit_unknown_and_the_write_did_not_land_trashes_the_new_file():
    d = FakeDrive()
    svc = _svc(d)
    _unknown(svc, None, False)
    with pytest.raises(HRDocError):
        await _up(svc)
    assert len(d.trashed) == 1 and d.trashed[0] in d.files


@pytest.mark.asyncio
async def test_commit_unknown_and_unverifiable_leaves_the_upload_in_place():
    d = FakeDrive()
    svc = _svc(d)
    _unknown(svc, None, None)
    with pytest.raises(HRDocError):
        await _up(svc)
    assert d.trashed == []


@pytest.mark.asyncio
async def test_a_certain_rollback_still_compensates():
    d = FakeDrive()
    svc = _svc(d)
    svc._db_step = AsyncMock(side_effect=HRDocError(HRDocCode.LOCK_TIMEOUT))  # type: ignore[method-assign]
    with pytest.raises(HRDocError):
        await _up(svc)
    assert len(d.trashed) == 1
