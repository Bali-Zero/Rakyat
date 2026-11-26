"""An oversized upload type or message is refused before anything is written.

``documents.document_type`` is 100 characters wide and ``portal_messages.subject`` 255. The
routes checked neither, so a longer value failed at the INSERT with a 500: for an upload,
after the file was already in the client's Drive folder, and a retry wrote another copy. A
message body had no bound at all.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from backend.app.dependencies import get_database_pool
from backend.app.routers import portal
from backend.app.routers.portal import get_current_client, get_portal_service


def _one_page_pdf() -> bytes:
    payload = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(payload)
    return payload.getvalue()


PDF = _one_page_pdf()


@pytest.fixture
def service() -> AsyncMock:
    stub = AsyncMock()
    stub.upload_document.return_value = {"id": 7, "document_type": "stored"}
    stub.send_message.return_value = {"id": 9}
    return stub


@pytest.fixture
def api(service: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(portal.router)
    app.dependency_overrides[get_current_client] = lambda: {"client_id": 42}
    app.dependency_overrides[get_portal_service] = lambda: service
    app.dependency_overrides[get_database_pool] = lambda: MagicMock()
    return TestClient(app)


def _upload(api: TestClient, document_type: str) -> Any:
    return api.post(
        "/api/portal/documents/upload",
        data={"document_type": document_type},
        files={"file": ("zz.pdf", PDF, "application/pdf")},
    )


def test_the_bounds_are_the_widths_of_the_two_columns() -> None:
    assert (portal.DOCUMENT_TYPE_MAX_LENGTH, portal.MESSAGE_SUBJECT_MAX_LENGTH) == (100, 255)


@pytest.mark.parametrize("length", [101, 120, 5000])
def test_an_upload_type_longer_than_its_column_is_refused_before_the_upload(
    api: TestClient, service: AsyncMock, length: int
) -> None:
    answer = _upload(api, "t" * length)

    assert answer.status_code == 422
    service.upload_document.assert_not_awaited()


@pytest.mark.parametrize("document_type", ["passport", "t" * 100, "é" * 100])
def test_an_upload_type_that_fits_its_column_reaches_the_upload(
    api: TestClient, service: AsyncMock, document_type: str
) -> None:
    answer = _upload(api, document_type)

    assert answer.status_code == 200
    assert service.upload_document.await_args.kwargs["document_type"] == document_type


@pytest.mark.parametrize(
    "body",
    [
        {"content": "c" * 20_001},
        {"content": "c" * 2_000_000},
        {"content": "hello", "subject": "s" * 256},
    ],
    ids=["body one over the bound", "a two megabyte body", "subject one over its column"],
)
def test_an_oversized_message_is_refused_before_it_is_stored(
    api: TestClient, service: AsyncMock, body: dict[str, str]
) -> None:
    answer = api.post("/api/portal/messages", json=body)

    assert answer.status_code == 422
    service.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"content": "hello"},
        {"content": "c" * 20_000},
        {"content": "hello", "subject": "s" * 255},
        {"content": "hello", "subject": None},
    ],
    ids=["a short body", "a body at the bound", "a subject as wide as its column", "no subject"],
)
def test_a_message_within_its_bounds_is_stored_as_sent(
    api: TestClient, service: AsyncMock, body: dict[str, Any]
) -> None:
    answer = api.post("/api/portal/messages", json=body)

    assert answer.status_code == 200
    sent = service.send_message.await_args.kwargs
    assert (sent["content"], sent["subject"]) == (body["content"], body.get("subject"))
