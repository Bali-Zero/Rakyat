"""Run one document-bound passport extraction through the CRM's configured OCR provider."""

import asyncio
import hashlib
import io
import re
import unicodedata
from datetime import date, datetime
from typing import Any

from fastapi import HTTPException
from google.auth.exceptions import GoogleAuthError
from googleapiclient.errors import HttpError
from httpx import HTTPError

from backend.app.utils.crm_utils import extract_json_from_llm_response
from backend.services.crm.gender import normalize_gender_lenient
from backend.services.documents import passport_ocr_jobs as jobs
from backend.services.garuda_documents.byte_validation import (
    MAX_IMAGE_PIXELS,
    MAX_UPLOAD_BYTES,
    is_readable_image,
)
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService

_PROMPT = (
    "Read the passport biodata page. Return a single JSON object containing passport_number, "
    "full_name, expiry_date, date_of_birth, gender, birthplace and nationality. "
    "Use ISO dates and null for unreadable or absent fields. Do not guess."
)


class NeedsReview(ValueError):
    """Carries a fixed reason safe to display to staff."""


def _name_tokens(value: str) -> list[str]:
    return sorted(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold()))


def verified_fields(response: str, owner: str) -> dict[str, str]:
    result = extract_json_from_llm_response(response)
    if not isinstance(result, dict):
        raise NeedsReview("unreadable_passport")
    name, number = result.get("full_name"), result.get("passport_number")
    if not isinstance(name, str) or not isinstance(number, str) or not number.strip():
        raise NeedsReview("missing_identity_fields")
    if not owner or not _name_tokens(owner) or _name_tokens(name) != _name_tokens(owner):
        raise NeedsReview("passport_holder_mismatch")
    fields = {"full_name": name.strip(), "passport_number": number.strip()}
    for key in ("birthplace", "nationality"):
        if isinstance(result.get(key), str) and result[key].strip():
            fields[key] = result[key].strip()
    if isinstance(result.get("gender"), str) and (
        gender := normalize_gender_lenient(result["gender"])
    ):
        fields["gender"] = gender
    for key in ("date_of_birth", "expiry_date"):
        value = result.get(key)
        if not value:
            continue
        if not isinstance(value, str):
            raise NeedsReview("unreadable_date")
        try:
            fields[key] = date.fromisoformat(value).isoformat()
        except ValueError:
            try:
                fields[key] = datetime.strptime(value, "%d-%m-%Y").date().isoformat()
            except ValueError as exc:
                raise NeedsReview("unreadable_date") from exc
    return fields


def _image_for_vision(data: bytes, mime: str) -> tuple[bytes, str]:
    if mime != "application/pdf":
        if not is_readable_image(data):
            raise NeedsReview("unreadable_image")
        return data, mime

    import pypdfium2 as pdfium

    try:
        with pdfium.PdfDocument(data) as pdf:
            if len(pdf) != 1:
                raise NeedsReview("multi_page_passport")
            page = pdf[0]
            width, height = page.get_size()
            if min(width, height) <= 0 or width * height * 4 > MAX_IMAGE_PIXELS:
                raise NeedsReview("unsupported_page_size")
            image = page.render(scale=2).to_pil()
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue(), "image/png"
    except pdfium.PdfiumError as exc:
        raise NeedsReview("unreadable_pdf") from exc


async def extract(job: dict[str, Any]) -> dict[str, str]:
    from backend.app.routers.crm_enhanced import _gemini_ocr

    drive = await asyncio.to_thread(ServiceAccountDriveService)
    metadata = await drive.get_file_metadata(job["file_id"])
    if metadata.get("id") != job["file_id"] or metadata.get("trashed") is not False:
        raise NeedsReview("drive_file_unavailable")
    mime = metadata.get("mimeType")
    if mime not in {"image/jpeg", "image/png", "image/webp", "application/pdf"}:
        raise NeedsReview("unsupported_media")
    try:
        size = int(metadata.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise NeedsReview("unsupported_file_size") from exc
    if not 0 < size <= MAX_UPLOAD_BYTES:
        raise NeedsReview("unsupported_file_size")
    data = await drive.download_file_content(job["file_id"])
    if len(data) != size:
        raise NeedsReview("drive_file_changed")
    expected_hash = job.get("content_hash")
    if expected_hash:
        digest = (
            hashlib.md5(data, usedforsecurity=False).hexdigest()
            if len(expected_hash) == 32
            else hashlib.sha256(data).hexdigest()
        )
        if digest != expected_hash.lower():
            raise NeedsReview("drive_file_changed")
    data, mime = await asyncio.to_thread(_image_for_vision, data, mime)
    response = await _gemini_ocr(data, mime, _PROMPT)
    fields = verified_fields(response, job["full_name"])
    current = await drive.get_file_metadata(job["file_id"])
    if current.get("trashed") is not False or any(
        current.get(key) != metadata.get(key) for key in ("id", "modifiedTime", "parents")
    ):
        raise NeedsReview("drive_file_changed")
    return fields


async def process_one(pool: Any, **binding: Any) -> dict[str, Any]:
    job = await jobs.claim(pool, **binding)
    if not job:
        return {"success": False, "status": "not_claimed"}
    try:
        from backend.app.routers.documents_proxy import assert_drive_file_belongs_to_client

        async with asyncio.timeout(jobs.LEASE_SECONDS - 10):
            async with pool.acquire() as conn:
                await assert_drive_file_belongs_to_client(
                    job["file_id"], job["client_id"], conn, require_ancestry=True
                )
            fields = await extract(job)
    except NeedsReview as exc:
        status = await jobs.finish(pool, job, reason=str(exc))
    except HttpError as exc:
        code = exc.resp.status
        retry = code in (408, 429) or code >= 500
        status = await jobs.finish(pool, job, reason="drive_read_failed", retryable=retry)
    except HTTPException as exc:
        retry = exc.status_code >= 500
        status = await jobs.finish(pool, job, reason="ownership_check_failed", retryable=retry)
    except (GoogleAuthError, HTTPError, OSError, RuntimeError):
        status = await jobs.finish(pool, job, reason="ocr_unavailable", retryable=True)
    else:
        status = await jobs.finish(pool, job, extracted=fields)
        if status == "completed":
            return {"success": True, "status": status, "extracted": fields}
    return {"success": False, "status": status}
