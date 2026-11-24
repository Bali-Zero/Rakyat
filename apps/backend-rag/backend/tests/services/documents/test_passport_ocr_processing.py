"""Document-bound passport OCR processing contract."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.documents import passport_ocr_processing as processing


def test_verified_fields_normalizes_canonical_gender_and_dates() -> None:
    response = """{"full_name":"Jane Doe","passport_number":"SYNTHETIC", "gender":"female", "expiry_date":"01-06-2030", "date_of_birth":"1990-03-15"}"""
    fields = processing.verified_fields(response, "Doe Jane")
    assert fields["gender"] == "F"
    assert fields["expiry_date"] == "2030-06-01"
    assert fields["date_of_birth"] == "1990-03-15"


@pytest.mark.parametrize(
    ("response", "owner", "reason"),
    [
        ('{"full_name":"Jane Doe"}', "Jane Doe", "missing_identity_fields"),
        (
            '{"full_name":"Other Person","passport_number":"SYNTHETIC"}',
            "Jane Doe",
            "passport_holder_mismatch",
        ),
    ],
)
def test_verified_fields_rejects_missing_or_mismatched_identity(
    response: str, owner: str, reason: str
) -> None:
    with pytest.raises(processing.NeedsReview, match=reason):
        processing.verified_fields(response, owner)


@pytest.mark.asyncio
async def test_extract_does_not_download_a_trashed_file() -> None:
    drive = AsyncMock()
    drive.get_file_metadata.return_value = {"id": "f1", "trashed": True}
    with patch.object(processing, "ServiceAccountDriveService", return_value=drive):
        with pytest.raises(processing.NeedsReview, match="drive_file_unavailable"):
            await processing.extract({"file_id": "f1", "full_name": "Jane Doe"})
    drive.download_file_content.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (processing.NeedsReview("passport_holder_mismatch"), "passport_holder_mismatch"),
        (processing.HTTPException(status_code=400), "provider_request_failed"),
    ],
)
async def test_process_one_records_the_terminal_reason_after_ownership_succeeds(error, reason):
    job = {"document_id": 9, "client_id": 7, "file_id": "f1", "full_name": "Jane Doe"}
    pool = MagicMock()
    with (
        patch.object(processing.jobs, "claim", new=AsyncMock(return_value=job)),
        patch(
            "backend.app.routers.documents_proxy.assert_drive_file_belongs_to_client", AsyncMock()
        ),
        patch.object(
            processing,
            "extract",
            new=AsyncMock(side_effect=error),
        ),
        patch.object(processing.jobs, "finish", new=AsyncMock(return_value="review")) as finish,
    ):
        result = await processing.process_one(pool, doc_id=9, client_id=7, file_id="f1")
    assert result == {"success": False, "status": "review"}
    finish.assert_awaited_once_with(pool, job, reason=reason)


@pytest.mark.asyncio
async def test_process_one_returns_not_claimed_without_extraction() -> None:
    with (
        patch.object(processing.jobs, "claim", new=AsyncMock(return_value=None)),
        patch.object(processing, "extract", new=AsyncMock()) as extract,
    ):
        result = await processing.process_one(object(), doc_id=9, client_id=7, file_id="f1")
    assert result == {"success": False, "status": "not_claimed"}
    extract.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [None, "source_sha", "folder", "trashed", "missing_storage_hash"]
)
async def test_local_receipt_verifies_metadata_without_downloading_media(change):
    from backend.app.routers import documents_proxy
    from backend.services.documents import passport_ocr_receipt as local
    from backend.services.integrations import service_account_drive_service

    source = local.AppleReceipt(1, 2, "sample-file", "a" * 64, "Sample Holder", "T10000001")
    meta = {
        "id": "sample-file",
        "parents": ["folder-a"],
        "trashed": False,
        "mimeType": "image/jpeg",
        "modifiedTime": "2020-01-01T00:00:00Z",
        "md5Checksum": "b" * 32,
        "sha256Checksum": "a" * 64,
    }
    after = dict(meta)
    if change == "source_sha":
        meta["sha256Checksum"] = after["sha256Checksum"] = "c" * 64
    if change == "folder":
        after["parents"] = ["folder-b"]
    if change == "trashed":
        meta["trashed"] = after["trashed"] = True
    service = AsyncMock()
    service.get_file_metadata.side_effect = [meta, after]
    conn = object()
    with (
        patch.object(
            service_account_drive_service, "ServiceAccountDriveService", return_value=service
        ),
        patch.object(
            documents_proxy, "assert_drive_file_belongs_to_client", new_callable=AsyncMock
        ) as owner,
    ):
        if change == "missing_storage_hash":
            await local._verify_drive(conn, source, None)
        elif change:
            with pytest.raises(local.ReceiptRejected):
                await local._verify_drive(conn, source, "b" * 32)
        else:
            await local._verify_drive(conn, source, "b" * 32)
        owner.assert_awaited_once_with("sample-file", 2, conn, require_ancestry=True)
    service.download_file_content.assert_not_awaited()


@pytest.mark.parametrize(
    "header,name", [("PNUTOHOLDER<<SAMPLE", "SAMPLE HOLDER"), ("P<D<<HOLDER<<", "HOLDER")]
)
def test_local_receipt_recognizes_td3_subtypes_and_mononyms(header, name):
    from backend.services.documents.passport_ocr_receipt import AppleReceipt, ReceiptRejected
    from backend.utils.passport_normalize import mrz_check_digit

    number, birth, expiry = "T00000001", "900101", "350101"
    lower = f"{number}{mrz_check_digit(number)}UTO{birth}{mrz_check_digit(birth)}F{expiry}{mrz_check_digit(expiry)}"
    receipt = {
        "document_id": 1,
        "client_id": 1,
        "file_id": "synthetic-file",
        "source_sha256": "a" * 64,
        "mrz_line1": header,
        "mrz_line2": lower,
    }
    assert AppleReceipt.parse(receipt).full_name == name
    with pytest.raises(ReceiptRejected, match="invalid_receipt"):
        AppleReceipt.parse({**receipt, "file_id": "too-short"})


def test_operator_receipt_import_is_independent_of_intake_model_configuration():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['backend.services.intake.extract'] = None; "
            "from backend.services.documents.passport_ocr_receipt import AppleReceipt",
        ],
        check=True,
        capture_output=True,
    )
