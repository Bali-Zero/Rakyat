"""HR document door service (STAFF_CARD.md C12) with a fake Drive. Synthetic data only."""

from __future__ import annotations

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

    async def get_app_properties(self, file_id):
        f = self.files.get(file_id)
        return None if f is None or file_id in self.trashed else dict(f["appProperties"])

    async def trash_file(self, file_id):
        self.trashed.append(file_id)


class _Conn:
    async def execute(self, *a: Any) -> None:
        return None

    @asynccontextmanager
    async def transaction(self):
        yield


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield _Conn()


def _svc(drive: FakeDrive, unit: str = "Tax", name: str = "Made Contoh") -> HRDocumentService:
    svc = HRDocumentService(_Pool(), drive)
    svc._read_context = AsyncMock(return_value=hd._Context(full_name=name, unit=unit))  # type: ignore[method-assign]
    return svc


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
async def test_replace_trashes_our_previous_and_keeps_a_foreign_one():
    d = FakeDrive()
    d.files["old-ours"] = {
        "name": "o",
        "parent": "p",
        "mime": "x",
        "appProperties": {hd.MEMBER_PROP: MEMBER},
    }
    d.files["old-foreign"] = {"name": "o", "parent": "p", "mime": "x", "appProperties": {}}
    d.files["old-other"] = {
        "name": "o",
        "parent": "p",
        "mime": "x",
        "appProperties": {hd.MEMBER_PROP: "someone-else"},
    }
    for prev, want, trashed in (
        ("old-ours", "trashed", ["old-ours"]),
        ("old-foreign", "kept", []),
        ("old-other", "kept", []),
    ):
        d.trashed.clear()
        svc = _svc(d)
        svc._db_step = AsyncMock(return_value=prev)  # type: ignore[method-assign]
        res = await _up(svc)
        assert res["previous"] == want
        assert d.trashed == trashed


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
    d.files["f1"] = {
        "name": "o",
        "parent": "p",
        "mime": "x",
        "appProperties": {hd.MEMBER_PROP: MEMBER},
    }
    svc = _svc(d)
    svc._db_step = AsyncMock(return_value="f1")  # type: ignore[method-assign]
    res = await svc.remove(actor_id="a", member_id=MEMBER, email="e", slot="kk")
    assert res == {"slot": "kk", "file_id": None, "previous": "trashed"}
    svc._db_step = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert (await svc.remove(actor_id="a", member_id=MEMBER, email="e", slot="kk"))[
        "previous"
    ] == "none"


def test_slot_set_is_the_five_docs_plus_contract():
    assert hd.ALL_SLOTS == ("ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract")
    assert set(hd.SLOT_LABELS) == set(hd.ALL_SLOTS)
