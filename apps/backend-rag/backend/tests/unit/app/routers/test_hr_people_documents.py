"""POST/DELETE /api/hr/people/{id}/documents/{slot} (STAFF_CARD.md C13): authz, refusals, no echo."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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


# ── the body is touched only after authz, and only up to a hard cap ──

CAP = 4 * 1024 * 1024 + 64 * 1024


def _app(user: dict[str, Any], doc: Any = None) -> tuple[FastAPI, Any]:
    doc = doc or MagicMock()
    doc.upload = AsyncMock(return_value={"slot": "ktp", "file_id": "f9", "previous": "none"})
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(hr_people.router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_database_pool] = lambda: MagicMock()
    return app, doc


class _Body:
    """An async request body that counts what the server pulled from it."""

    def __init__(self, chunks: int, size: int = 64 * 1024) -> None:
        self.chunks, self.size, self.pulled = chunks, size, 0

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for _ in range(self.chunks):
            self.pulled += 1
            yield b"x" * self.size


async def _raw(user: dict[str, Any], body: _Body, headers: dict[str, str] | None = None):
    app, doc = _app(user)
    people = MagicMock()
    people.member_email = AsyncMock(return_value="dea@balizero.com")
    with (
        patch.object(hr_people, "_get_people_service", return_value=people),
        patch.object(hr_people, "HRDocumentService", return_value=doc),
        patch.object(hr_people, "get_hr_drive_service", AsyncMock(return_value=object())),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            res = await c.post(
                _url(),
                content=body,
                headers={"content-type": "multipart/form-data; boundary=zz", **(headers or {})},
            )
    return res, doc


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [ADIT, SCOPED])
async def test_a_non_writer_with_a_huge_body_is_refused_without_reading_it(user):
    body = _Body(chunks=500)  # 32 MB
    res, doc = await _raw(user, body)
    assert res.status_code == 403
    assert body.pulled <= 1
    doc.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_declared_length_over_the_cap_is_413_before_any_byte_is_read():
    body = _Body(chunks=500)
    res, doc = await _raw(OWNER, body, {"content-length": str(CAP + 1)})
    assert res.status_code == 413 and body.pulled == 0
    doc.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_chunked_body_with_no_length_is_cut_off_at_the_cap():
    body = _Body(chunks=500)  # 32 MB, no Content-Length
    res, doc = await _raw(OWNER, body)
    assert res.status_code == 413
    assert body.pulled <= CAP // (64 * 1024) + 2  # stopped the moment it passed the cap
    doc.upload.assert_not_awaited()


def test_a_valid_file_still_goes_through(make_client):
    client, doc = make_client(OWNER)
    assert _post(client).status_code == 200
    doc.upload.assert_awaited_once()


def test_exactly_one_part_named_file_is_required(make_client):
    client, doc = make_client(OWNER)
    two = client.post(_url(), files=[("file", ("a.pdf", PDF)), ("file", ("b.pdf", PDF))])
    assert two.status_code == 422 and two.json()["detail"][0]["code"] == "invalid_upload"
    other = client.post(_url(), files={"upload": ("a.pdf", PDF)})
    assert other.status_code == 422 and other.json()["detail"][0]["code"] == "invalid_upload"
    notmp = client.post(_url(), json={"file": "x"})
    assert notmp.status_code == 422
    doc.upload.assert_not_awaited()


def test_each_request_builds_its_own_drive_client():
    import asyncio

    from backend.app.services.hr import hr_documents as hd

    made: list[object] = []

    class _Fake:
        def __init__(self, **kw: Any) -> None:
            assert kw["delegated_user"] == hd.settings.hr_documents_drive_subject
            assert kw["validate_folders"] is False
            made.append(self)

    with patch(
        "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService",
        _Fake,
    ):
        a = asyncio.run(hd.get_hr_drive_service())
        b = asyncio.run(hd.get_hr_drive_service())
    assert a is not b and len(made) == 2


# ── parser holes: one file part named `file`, nothing else, a closed body, a short boundary ──


def _raw_post(client: TestClient, body: bytes, boundary: str = "zz", ctype: str | None = None):
    return client.post(
        _url(),
        content=body,
        headers={"content-type": ctype or f"multipart/form-data; boundary={boundary}"},
    )


def _part(name: str, filename: str | None, payload: bytes, b: str = "zz") -> bytes:
    disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
    return f"--{b}\r\nContent-Disposition: {disp}\r\n\r\n".encode() + payload + b"\r\n"


def _closed(*parts: bytes, b: str = "zz") -> bytes:
    return b"".join(parts) + f"--{b}--\r\n".encode()


def _refused(res) -> None:
    assert res.status_code == 422 and res.json()["detail"][0]["code"] == "invalid_upload"


def test_a_well_formed_single_file_body_is_accepted(make_client):
    client, doc = make_client(OWNER)
    assert _raw_post(client, _closed(_part("file", "a.pdf", PDF))).status_code == 200
    doc.upload.assert_awaited_once()


def test_an_extra_field_is_refused(make_client):
    client, doc = make_client(OWNER)
    _refused(_raw_post(client, _closed(_part("note", None, b"x"), _part("file", "a.pdf", PDF))))
    _refused(_raw_post(client, _closed(_part("file", "a.pdf", PDF), _part("note", None, b"x"))))
    doc.upload.assert_not_awaited()


def test_a_field_named_file_without_a_filename_is_refused(make_client):
    client, doc = make_client(OWNER)
    _refused(_raw_post(client, _closed(_part("file", None, PDF))))
    doc.upload.assert_not_awaited()


def test_a_body_without_its_closing_boundary_is_refused(make_client):
    client, doc = make_client(OWNER)
    _refused(_raw_post(client, _part("file", "a.pdf", PDF)))  # no closing boundary at all
    _refused(_raw_post(client, _part("file", "a.pdf", PDF) + b"--zz\r\n"))  # opened, never closed
    doc.upload.assert_not_awaited()


@pytest.mark.parametrize(
    "ctype",
    [
        "multipart/form-data",  # no boundary
        "multipart/form-data; boundary=",
        "multipart/form-data; boundary=" + "b" * 71,  # over RFC 2046's 70
        'multipart/form-data; boundary="' + "b" * 200 + '"',
    ],
)
def test_a_missing_or_overlong_boundary_is_a_422_not_a_500(make_client, ctype):
    client, doc = make_client(OWNER)
    _refused(_raw_post(client, _closed(_part("file", "a.pdf", PDF)), ctype=ctype))
    doc.upload.assert_not_awaited()


def test_a_70_character_boundary_is_fine(make_client):
    client, _ = make_client(OWNER)
    b = "b" * 70
    res = _raw_post(client, _closed(_part("file", "a.pdf", PDF, b), b=b), boundary=b)
    assert res.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        b"--zz\r\nContent-Disposition form-data name=file\r\n\r\nx\r\n--zz--\r\n",  # malformed header
        b'--zz\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n',  # truncated
        b"\x00\xff\xfe--zz--",
        b"--zz--",
    ],
)
def test_malformed_multipart_never_reaches_a_500(make_client, body):
    client, doc = make_client(OWNER)
    res = _raw_post(client, body)
    assert res.status_code == 422
    doc.upload.assert_not_awaited()
