"""Apply a trusted operator's local Apple Vision receipt without sending media to an OCR service."""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.services.crm.related_identifiers import sql_canonical
from backend.services.documents import passport_ocr_jobs as jobs
from backend.services.documents.passport_ocr_processing import _name_tokens
from backend.utils.passport_normalize import mrz_check_ok


class ReceiptRejected(ValueError):
    """Fixed reason for an operator hold. Never include receipt contents."""


@dataclass(frozen=True, repr=False)
class AppleReceipt:
    document_id: int
    client_id: int
    file_id: str
    source_sha256: str
    full_name: str
    passport_number: str

    @classmethod
    def parse(cls, data: dict) -> "AppleReceipt":
        required = {
            "document_id",
            "client_id",
            "file_id",
            "source_sha256",
            "mrz_line1",
            "mrz_line2",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise ReceiptRejected("invalid_receipt")
        for key in ("document_id", "client_id"):
            if type(data[key]) is not int or not 0 < data[key] < 2**63:
                raise ReceiptRejected("invalid_binding")
        for key, pattern in (
            ("file_id", r"[a-zA-Z0-9_-]{10,256}"),
            ("source_sha256", r"[0-9a-f]{64}"),
            ("mrz_line1", r"P[A-Z<][A-Z][A-Z<]{2}[A-Z<]{1,39}"),
            ("mrz_line2", r"[A-Z0-9<]{28,44}"),
        ):
            if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
                raise ReceiptRejected("invalid_receipt")
        line = data["mrz_line2"]
        if not all(mrz_check_ok(line, *pos) for pos in ((0, 9, 9), (13, 19, 19), (21, 27, 27))):
            raise ReceiptRejected("invalid_mrz_checks")
        if not re.fullmatch(r"[A-Z0-9]{5,9}<*", line[:9]) or not re.fullmatch(
            "[A-Z<]{3}", line[10:13]
        ):
            raise ReceiptRejected("invalid_number_line")
        for value in (line[13:19], line[21:27]):
            try:
                datetime.strptime(value, "%y%m%d")
            except ValueError:
                raise ReceiptRejected("invalid_mrz_date") from None
        family, separator, given = data["mrz_line1"][5:44].partition("<<")
        if not family.strip("<") or (not separator and len(data["mrz_line1"]) != 44):
            raise ReceiptRejected("invalid_holder")
        name = " ".join((given + "<" + family).replace("<", " ").split())
        return cls(
            data["document_id"],
            data["client_id"],
            data["file_id"],
            data["source_sha256"],
            name,
            line[:9].rstrip("<"),
        )

    def fields(self) -> dict[str, str]:
        return {
            "full_name": self.full_name,
            "passport_number": self.passport_number,
            "ocr_provider": "apple_vision_pro",
            "source_sha256": self.source_sha256,
        }


@dataclass
class _PinnedConnection:
    """Run the existing job APIs on savepoints in one outer transaction."""

    conn: Any

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def _verify_drive(conn: Any, receipt: AppleReceipt, stored_hash: str | None) -> None:
    from fastapi import HTTPException
    from googleapiclient.errors import HttpError

    from backend.app.routers.documents_proxy import assert_drive_file_belongs_to_client
    from backend.services.integrations.service_account_drive_service import (
        ServiceAccountDriveService,
    )

    drive = await asyncio.to_thread(ServiceAccountDriveService, validate_folders=False)
    try:
        before = await drive.get_file_metadata(receipt.file_id, checksums=True)
        await assert_drive_file_belongs_to_client(
            receipt.file_id, receipt.client_id, conn, require_ancestry=True
        )
        after = await drive.get_file_metadata(receipt.file_id, checksums=True)
    except (HTTPException, HttpError) as exc:
        status = exc.status_code if isinstance(exc, HTTPException) else exc.resp.status
        if status in (408, 429) or status >= 500:
            raise ReceiptRejected("drive_temporarily_unavailable") from None
        raise ReceiptRejected("drive_ownership_or_access_refused") from None
    keys = ("id", "parents", "trashed", "mimeType", "modifiedTime", "md5Checksum", "sha256Checksum")
    if any(before.get(k) != after.get(k) for k in keys):
        raise ReceiptRejected("drive_file_changed")
    if (
        after.get("id") != receipt.file_id
        or after.get("trashed") is not False
        or not after.get("parents")
        or after.get("sha256Checksum") != receipt.source_sha256
        or after.get("mimeType") not in {"image/jpeg", "image/png", "image/webp", "application/pdf"}
        or (
            stored_hash
            and stored_hash.lower() not in (after.get("md5Checksum"), receipt.source_sha256)
        )
    ):
        raise ReceiptRejected("source_mismatch")


async def apply_apple_receipt(pool: Any, receipt: AppleReceipt) -> str:
    # A crash or refusal rolls back enrollment too. The ordinary poller can never
    # observe pending work from this operation or send its media to another provider.
    async with (
        asyncio.timeout(60),
        pool.acquire() as conn,
        conn.transaction(isolation="serializable"),
    ):
        doc = await conn.fetchrow(
            """SELECT d.client_id,d.file_id,d.content_hash,d.ocr_status,d.ocr_extracted_data,
                      (d.deleted_at IS NULL AND NOT coalesce(d.is_archived,false)
                       AND d.status IN ('active','google_drive') AND d.verified_at IS NULL
                       AND d.verified_by IS NULL AND d.rejection_reason IS NULL
                       AND d.intake_idempotency_key IS NULL AND d.intake_proposal_id IS NULL
                       AND crm_passport_ocr_is_passport(d.document_type)
                       AND crm_passport_ocr_review_code(d) IS NULL) AS allowed,
                      j.status AS job_status,j.client_id AS job_client,j.file_id AS job_file,
                      j.content_hash AS job_hash
                 FROM documents d LEFT JOIN crm_passport_ocr_jobs j ON j.document_id=d.id
                WHERE d.id=$1 FOR UPDATE OF d""",
            receipt.document_id,
        )
        if not doc or (doc["client_id"], doc["file_id"]) != (receipt.client_id, receipt.file_id):
            raise ReceiptRejected("binding_mismatch")
        if not doc["allowed"]:
            raise ReceiptRejected("ineligible_document")
        owners = await conn.fetch(
            f"""SELECT id,full_name,status,deleted_at FROM clients
               WHERE {sql_canonical('passport', 'passport_number')}=$1
               FOR SHARE""",
            receipt.passport_number,
        )
        if (
            len(owners) != 1
            or owners[0]["id"] != receipt.client_id
            or owners[0]["status"] != "active"
            or owners[0]["deleted_at"] is not None
            or _name_tokens(owners[0]["full_name"] or "") != _name_tokens(receipt.full_name)
        ):
            raise ReceiptRejected("identity_mismatch")
        fields = receipt.fields()
        await _verify_drive(conn, receipt, doc["content_hash"])
        if doc["job_status"] is not None:
            payload = doc["ocr_extracted_data"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (
                doc["job_status"] == doc["ocr_status"] == "completed"
                and payload == fields
                and (doc["job_client"], doc["job_file"], doc["job_hash"])
                == (receipt.client_id, receipt.file_id, doc["content_hash"] or "")
            ):
                return "already_applied"
            raise ReceiptRejected("existing_work")
        transaction = _PinnedConnection(conn)
        if not await jobs.enqueue_selected(transaction, [receipt.document_id]):
            raise ReceiptRejected("ineligible_document")
        job = await jobs.claim(
            transaction,
            doc_id=receipt.document_id,
            client_id=receipt.client_id,
            file_id=receipt.file_id,
        )
        if not job or await jobs.finish(transaction, job, extracted=fields) != "completed":
            raise ReceiptRejected("settlement_refused")
        return "completed"
