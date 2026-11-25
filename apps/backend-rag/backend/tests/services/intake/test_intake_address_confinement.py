"""Both intake writers store only the addresses that name the payload's own file."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from backend.services.intake import writer as intake_writer

X = "abcdefghijK"
Y = "lmnopqrstuV"
D = "https://drive.google.com"
OWN_URL = f"{D}/file/d/{X}/view"
FOREIGN_URL = f"{D}/file/d/{Y}/view"
PROPOSAL = 4711


class _Conn:
    """Records the one INSERT a writer issues; no database. ``has_ocr`` picks which of the
    client writer's two INSERTs runs (the ``ocr_status`` column present or absent)."""

    def __init__(self, has_ocr: bool = True) -> None:
        self.insert_sql, self.insert_args = "", ()
        self.has_ocr = has_ocr

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "information_schema.columns" in sql:
            return 1 if self.has_ocr else None
        if "INSERT INTO" in sql:
            self.insert_sql, self.insert_args = " ".join(sql.split()), args
            return 99
        return None  # the company writer's idempotency probe finds nothing

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        if "information_schema.columns" in sql:
            return [{"column_name": "id"}]
        return []

    async def execute(self, *_: Any) -> str:
        return "OK"


def _payload(**extra: Any) -> dict[str, Any]:
    return {
        "document_type": "visa",
        "document_category": "immigration",
        "file_name": "visa.pdf",
        "notes": "intake:test",
        "ocr_status": "completed",
        **extra,
    }


@pytest.fixture(params=[True, False], ids=["ocr_column", "no_ocr_column"])
def has_ocr(request: pytest.FixtureRequest) -> bool:
    return request.param


async def _client_row(has_ocr: bool, **extra: Any) -> tuple[Any, Any]:
    conn = _Conn(has_ocr)
    await intake_writer.write_client_document(
        conn,  # type: ignore[arg-type]
        501,
        _payload(**extra),
        proposal_id=PROPOSAL,
        idempotency_key="ik:test",
    )
    columns = conn.insert_sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    # $1..$10 bind the first ten columns in order; the literals and the rest follow them.
    row = dict(zip(columns[:10], conn.insert_args[:10], strict=True))
    assert row["file_id"] == extra.get("file_id")  # the row keeps its own file_id
    return row["file_url"], row["google_drive_file_url"]


async def _company_row(**extra: Any) -> tuple[Any, Any]:
    conn = _Conn()
    await intake_writer.write_company_document(
        conn,  # type: ignore[arg-type]
        7,
        _payload(**extra),
        proposal_id=PROPOSAL,
        idempotency_key="ik:test",
        committed_by="tester",
    )
    columns = conn.insert_sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    row = dict(zip(columns, conn.insert_args, strict=False))
    assert row["google_drive_file_id"] == extra.get("file_id")
    return row["google_drive_file_url"]


@pytest.mark.asyncio
async def test_client_an_address_naming_only_its_file_is_stored(has_ocr):
    assert await _client_row(
        has_ocr, file_id=X, file_url=OWN_URL, google_drive_file_url=OWN_URL
    ) == (
        OWN_URL,
        OWN_URL,
    )


@pytest.mark.asyncio
async def test_client_an_address_naming_another_run_is_stored_null(has_ocr):
    got = await _client_row(has_ocr, file_id=X, file_url=FOREIGN_URL, google_drive_file_url=OWN_URL)
    assert got == (None, OWN_URL)
    got = await _client_row(
        has_ocr, file_id=X, file_url=f"{OWN_URL}?id={Y}", google_drive_file_url=FOREIGN_URL
    )
    assert got == (None, None)


@pytest.mark.asyncio
async def test_client_without_a_file_id_an_address_naming_a_run_is_stored_null(has_ocr):
    assert await _client_row(has_ocr, file_url=OWN_URL, google_drive_file_url=OWN_URL) == (
        None,
        None,
    )
    assert await _client_row(has_ocr, file_id="", file_url=OWN_URL) == (None, None)


@pytest.mark.asyncio
async def test_client_an_address_naming_no_run_is_stored(has_ocr):
    url = "https://files.example.test/report.pdf"
    assert await _client_row(has_ocr, file_id=X, file_url=url, google_drive_file_url=url) == (
        url,
        url,
    )
    assert await _client_row(has_ocr, file_url=url) == (url, None)


@pytest.mark.asyncio
async def test_client_the_repair_callers_canonical_url_is_stored(has_ocr):
    url = f"{D}/file/d/{X}/view"
    assert await _client_row(has_ocr, file_id=X, file_url=url, google_drive_file_url=url) == (
        url,
        url,
    )


@pytest.mark.asyncio
async def test_company_standalone_a_foreign_google_drive_url_is_stored_null():
    assert await _company_row(file_id=X, google_drive_file_url=FOREIGN_URL) is None


@pytest.mark.asyncio
async def test_company_standalone_the_fallback_is_taken_after_confinement():
    assert (
        await _company_row(file_id=X, google_drive_file_url=FOREIGN_URL, file_url=OWN_URL)
        == OWN_URL
    )


@pytest.mark.asyncio
async def test_company_standalone_both_foreign_stores_null():
    assert (
        await _company_row(file_id=X, google_drive_file_url=FOREIGN_URL, file_url=FOREIGN_URL)
        is None
    )


@pytest.mark.asyncio
async def test_company_an_own_address_is_stored():
    assert await _company_row(file_id=X, google_drive_file_url=OWN_URL) == OWN_URL


@pytest.mark.asyncio
async def test_company_both_own_addresses_store_the_drive_address_first():
    other_own = f"{D}/open?id={X}"
    assert (
        await _company_row(file_id=X, google_drive_file_url=OWN_URL, file_url=other_own) == OWN_URL
    )


@pytest.mark.parametrize("file_id", ["absent", ""], ids=["file_id_absent", "file_id_empty"])
@pytest.mark.asyncio
async def test_company_standalone_without_a_file_id_an_address_naming_a_run_is_stored_null(file_id):
    extra = {} if file_id == "absent" else {"file_id": file_id}
    assert await _company_row(**extra, google_drive_file_url=OWN_URL) is None
    assert await _company_row(**extra, file_url=OWN_URL) is None  # the fallback is confined too
    assert await _company_row(**extra, google_drive_file_url=FOREIGN_URL, file_url=OWN_URL) is None


@pytest.mark.parametrize("file_id", ["absent", ""], ids=["file_id_absent", "file_id_empty"])
@pytest.mark.asyncio
async def test_company_standalone_without_a_file_id_an_address_naming_no_run_is_stored(file_id):
    extra = {} if file_id == "absent" else {"file_id": file_id}
    url = "https://files.example.test/company-deed.pdf"
    assert await _company_row(**extra, google_drive_file_url=url) == url
    assert await _company_row(**extra, file_url=url) == url


@pytest.mark.asyncio
async def test_a_dropped_field_logs_one_line_with_the_proposal_and_the_field_only(caplog, has_ocr):
    caplog.set_level(logging.WARNING, logger="zantara.intake.writer")
    await _client_row(has_ocr, file_id=X, file_url=FOREIGN_URL, google_drive_file_url=OWN_URL)

    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 1
    assert str(PROPOSAL) in lines[0] and "file_url" in lines[0]
    assert Y not in lines[0] and X not in lines[0] and "drive.google.com" not in lines[0]


DIGITS_URL = "https://x.example.test/open?id=12345678901"


@pytest.mark.asyncio
async def test_client_an_all_digit_run_beside_another_file_id_is_stored_null(has_ocr):
    got = await _client_row(
        has_ocr, file_id=X, file_url=DIGITS_URL, google_drive_file_url=DIGITS_URL
    )
    assert got == (None, None)  # `_client_row` also asserts the row keeps file_id X


@pytest.mark.asyncio
async def test_company_an_all_digit_run_beside_another_file_id_is_stored_null():
    assert await _company_row(file_id=X, google_drive_file_url=DIGITS_URL) is None
    assert await _company_row(file_id=X, file_url=DIGITS_URL) is None  # the fallback too


def test_confine_addresses_keeps_both_when_nothing_is_foreign():
    payload = _payload(file_id=X, file_url=OWN_URL, google_drive_file_url=None)
    assert intake_writer.confine_addresses(payload) == (OWN_URL, None)
