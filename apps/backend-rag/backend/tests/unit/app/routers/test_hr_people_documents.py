"""POST/DELETE /api/hr/people/{id}/documents/{slot} (STAFF_CARD.md C12): authz, refusals, no echo."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr_people
from backend.app.services.hr.hr_documents import HRDocCode, HRDocError
from backend.app.setup.exception_handlers import register_exception_handlers

OWNER = {"user_id": "tm-owner", "email": "zero@balizero.com", "role": "member"}
ASYA = {"user_id": "tm-asya", "email": "asya@balizero.com", "role": "member"}
ADIT = {"user_id": "tm-adit", "email": "adit@balizero.com", "role": "member"}  # HR extra admin
SCOPED = {"user_id": "tm-scoped", "email": "ruslana@balizero.com", "role": "member"}

ID_IN = "11111111-1111-4111-8111-111111111111"
ID_NONE = "22222222-2222-4222-8222-222222222222"
PDF = b"%PDF-1.7\n synthetic"
SECRET_NAME = "Made-Contoh-Rahasia.pdf"


def _url(slot: str = "ktp", member: str = ID_IN) -> str:
    return f"/api/hr/people/{member}/documents/{slot}"


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(user: dict[str, Any], doc: Any = None) -> tuple[TestClient, Any]:
        doc = doc or MagicMock()
        if not isinstance(doc.upload, AsyncMock):
            doc.upload = AsyncMock(
                return_value={"slot": "ktp", "file_id": "f9", "previous": "none"}
            )
            doc.remove = AsyncMock(
                return_value={"slot": "ktp", "file_id": None, "previous": "none"}
            )
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(hr_people.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        people = MagicMock()
        people.member_email = AsyncMock(
            side_effect=lambda i: "dea@balizero.com" if i == ID_IN else None
        )
        for p in (
            patch.object(hr_people, "_get_people_service", return_value=people),
            patch.object(hr_people, "HRDocumentService", return_value=doc),
            patch.object(hr_people, "get_hr_drive_service", AsyncMock(return_value=object())),
        ):
            p.start()
            patchers.append(p)
        return TestClient(app), doc

    yield _make
    for p in patchers:
        p.stop()


def _post(client: TestClient, slot: str = "ktp", data: bytes = PDF, name: str = SECRET_NAME, **kw):
    return client.post(_url(slot, **kw), files={"file": (name, data, "image/png")})


@pytest.mark.parametrize("user", [OWNER, ASYA])
def test_writers_upload(make_client, user):
    client, doc = make_client(user)
    res = _post(client)
    assert res.status_code == 200
    assert res.json() == {"slot": "ktp", "file_id": "f9", "previous": "none"}
    kw = doc.upload.await_args.kwargs
    assert kw["mime"] == "application/pdf" and kw["ext"] == "pdf"  # bytes, not the client's png
    assert kw["actor_id"] == user["user_id"] and kw["email"] == "dea@balizero.com"


@pytest.mark.parametrize("user", [ADIT, SCOPED])
def test_non_writers_are_refused_before_anything(make_client, user):
    client, doc = make_client(user)
    assert _post(client).status_code == 403
    assert _post(client, member=ID_NONE).status_code == 403  # no lookup oracle either
    assert client.delete(_url()).status_code == 403
    doc.upload.assert_not_awaited()
    doc.remove.assert_not_awaited()


def test_unknown_person_is_404(make_client):
    client, doc = make_client(OWNER)
    assert _post(client, member=ID_NONE).status_code == 404
    assert client.delete(_url(member=ID_NONE)).status_code == 404
    doc.upload.assert_not_awaited()


@pytest.mark.parametrize("slot", ["ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract"])
def test_every_slot_is_accepted(make_client, slot):
    client, _ = make_client(OWNER)
    assert _post(client, slot=slot).status_code == 200


def test_unknown_slot_422(make_client):
    client, doc = make_client(OWNER)
    res = _post(client, slot="passport")
    assert res.status_code == 422 and res.json()["detail"][0]["code"] == "unknown_slot"
    assert client.delete(_url("passport")).status_code == 422
    doc.upload.assert_not_awaited()


@pytest.mark.parametrize(
    "data,code",
    [
        (b"", "empty_file"),
        (b"GIF89a" + b"x" * 10, "unsupported_type"),
        (b"<html>%PDF-</html>", "unsupported_type"),
        (b"%PDF-" + b"x" * (4 * 1024 * 1024 - 4), "file_too_large"),  # 4 MB + 1
    ],
)
def test_bad_files_422(make_client, data, code):
    client, doc = make_client(OWNER)
    res = _post(client, data=data)
    assert res.status_code == 422 and res.json()["detail"][0]["code"] == code
    assert SECRET_NAME not in res.text
    doc.upload.assert_not_awaited()


def test_exactly_4_mb_is_accepted_and_a_png_named_pdf_is_a_png(make_client):
    client, doc = make_client(OWNER)
    assert _post(client, data=b"%PDF-" + b"x" * (4 * 1024 * 1024 - 5)).status_code == 200
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 8
    assert _post(client, data=png, name="a.pdf").status_code == 200
    assert doc.upload.await_args.kwargs["ext"] == "png"


@pytest.mark.parametrize(
    "code,status,body",
    [
        (HRDocCode.HR_RECORD_REQUIRED, 409, "hr_record_required"),
        (HRDocCode.DRIVE_UNAVAILABLE, 502, "drive_unavailable"),
        (HRDocCode.LOCK_TIMEOUT, 409, "busy"),
        (HRDocCode.NOT_FOUND, 404, "No such person"),
        (HRDocCode.DB_ERROR, 500, "failed"),
    ],
)
def test_service_errors_are_fixed_answers(make_client, code, status, body):
    doc = MagicMock()
    doc.upload = AsyncMock(side_effect=HRDocError(code))
    doc.remove = AsyncMock(side_effect=HRDocError(code))
    client, _ = make_client(OWNER, doc)
    for res in (_post(client), client.delete(_url())):
        assert res.status_code == status and body in res.text
        assert SECRET_NAME not in res.text


def test_remove_and_logs_carry_no_filename(make_client, caplog):
    client, doc = make_client(OWNER)
    with caplog.at_level(logging.DEBUG):
        _post(client)
        _post(client, data=b"GIF89a....")
        assert client.delete(_url("kk")).status_code == 200
    doc.remove.assert_awaited_once()
    assert SECRET_NAME not in caplog.text and "Made" not in caplog.text
    assert "slot=ktp" in caplog.text and "actor=tm-owner" in caplog.text


def test_drive_init_failure_is_502(make_client):
    client, doc = make_client(OWNER)
    with patch.object(
        hr_people, "get_hr_drive_service", AsyncMock(side_effect=ValueError("creds Made"))
    ):
        res = _post(client)
    assert res.status_code == 502 and "creds" not in res.text
    doc.upload.assert_not_awaited()
