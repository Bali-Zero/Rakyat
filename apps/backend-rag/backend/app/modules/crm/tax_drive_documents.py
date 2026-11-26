"""Client Tax Documents (Google Drive) CRUD endpoints.

Scoped to the client's own Drive folder tree and those of its linked companies
(`clients.google_drive_folder_id`, `clients.drive_folder_id`, and each linked
company's `google_drive_folder_id` / `tax_dept_folder_id`).

Security invariants:
1. Authorization BEFORE any Drive call or file body read: only human CRM admins
   or human team members associated with the client profile (`assigned_to`,
   `tax_consultant`, `created_by`, or their validated client-pool peers) using a
   human JWT session may mutate documents. Machine keys (`admin_key`,
   `internal_key`, `api_key`) and non-human/portal roles (`monitoring`,
   `internal`, `client`, `partner`, `service`) are rejected even if their email
   is on an admin allowlist.
2. Folder/file ancestry verified with `supportsAllDrives=True` before every
   mutation; an item outside the client's allowed roots returns 403 (fail
   closed, no Workspace existence oracle).
3. Folders and root folders cannot be renamed, replaced, or trashed here;
   deletion moves the file to Drive Trash (recoverable, never hard delete).
4. DB pool connection is released BEFORE Drive HTTP calls (no pool deadlock),
   then reacquired after Drive mutations to keep CRM registrations (`documents`
   and `company_documents`), content hashes, and OCR evidence in sync.
5. Logs carry only numeric `client_id` / `doc_id` and Drive IDs — never client
   PII or raw filenames.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Request,
    UploadFile,
)
from googleapiclient.errors import HttpError
from pydantic import BaseModel, Field

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers.documents_proxy import _DRIVE_FILE_ID_RE
from backend.app.utils.client_pools import (
    HUMAN_SESSION_AUTH_METHODS,
    can_manage_client_tax_documents,
    ensure_client_pools_validated,
)
from backend.app.utils.service_accounts import is_human_team_member
from backend.core.cache import invalidate_cache
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService

logger = logging.getLogger(__name__)

# Matches Vercel's ~4.5 MB proxy body cap (same limit as hr_documents.py).
TAX_DOC_MAX_BYTES = 4 * 1024 * 1024

_FOLDER_MIME = "application/vnd.google-apps.folder"
_SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
_MAX_ANCESTOR_HOPS = 6
_MAX_ANCESTOR_LOOKUPS = 10
_MAX_FILENAME_LEN = 200
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_AUTHZ_DENIED_DETAIL = "You do not have permission to manage tax documents for this client."

router = APIRouter(tags=["crm-tax-documents"])


@dataclass(frozen=True)
class _ClientDriveScope:
    allowed_roots: set[str]
    linked_company_ids: list[int]


class TaxDriveFileItem(BaseModel):
    """Normalized Drive file returned after upload / rename / replace."""

    id: str
    name: str
    mime_type: str = ""
    size: int = 0
    modified_time: str | None = None
    web_view_link: str | None = None
    is_folder: bool = False


class TaxDriveFileResponse(BaseModel):
    """Response for upload, rename, and content replacement."""

    success: bool = True
    file: TaxDriveFileItem


class TaxDriveDeleteResponse(BaseModel):
    """Response for trashing a tax document."""

    success: bool = True
    file_id: str
    trashed: bool = True


class TaxDriveRenameRequest(BaseModel):
    """Payload for renaming a tax document."""

    name: str = Field(..., min_length=1, max_length=_MAX_FILENAME_LEN)


def get_tax_drive_service() -> ServiceAccountDriveService:
    """Return a DWD ServiceAccountDriveService without boot-time folder probes."""
    try:
        return ServiceAccountDriveService(validate_folders=False)
    except Exception as exc:
        logger.error("tax_drive.service_init_failed error=%s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Google Drive integration is unavailable"
        ) from None


def get_tax_drive_service_factory() -> Callable[[], ServiceAccountDriveService]:
    """Deferred factory dependency so auth/input validation runs before Drive client init."""
    return get_tax_drive_service


def get_tax_documents_user(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Enforce human JWT session and human team role before any DB or Drive work.

    ``get_current_user`` intentionally drops ``auth_method`` from ``request.state.user``.
    Reading it here rejects ``X-Debug-Key`` (``admin_key``), ``X-Internal-Key``
    (``internal_key``), ``X-API-Key`` (``api_key``), and ``public`` principals even
    when they carry an ``admin`` role or allowlisted email.
    """
    state_user = getattr(request.state, "user", None)
    state_auth_type = getattr(request.state, "auth_type", None)

    auth_method: str | None = None
    if isinstance(state_user, dict):
        raw_method = state_user.get("auth_method")
        if not isinstance(raw_method, str) or not raw_method.strip():
            raw_method = state_auth_type if isinstance(state_auth_type, str) else None
        if (
            not isinstance(raw_method, str)
            or raw_method.strip().lower() not in HUMAN_SESSION_AUTH_METHODS
        ):
            raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)
        auth_method = raw_method.strip().lower()
    elif isinstance(state_auth_type, str) and state_auth_type.strip():
        if state_auth_type.strip().lower() not in HUMAN_SESSION_AUTH_METHODS:
            raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)
        auth_method = state_auth_type.strip().lower()
    else:
        raw_method = (current_user or {}).get("auth_method")
        if raw_method is not None:
            normalized = str(raw_method).strip().lower()
            if normalized not in HUMAN_SESSION_AUTH_METHODS:
                raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)
            auth_method = normalized

    if not is_human_team_member((current_user or {}).get("role")):
        raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)

    if auth_method is not None:
        return {**current_user, "auth_method": auth_method}
    return current_user


def _record_get(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _assert_drive_id(raw_id: str, label: str = "Drive id") -> str:
    value = (raw_id or "").strip()
    if not _DRIVE_FILE_ID_RE.match(value):
        raise HTTPException(status_code=400, detail=f"Malformed {label}")
    return value


def _sanitize_upload_filename(raw_name: str | None) -> str:
    """Extract a safe basename from an uploaded filename."""
    base = (raw_name or "").replace("\\", "/").split("/")[-1]
    cleaned = _CONTROL_CHARS_RE.sub("", base).strip()
    if not cleaned or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid file name")
    return cleaned[:_MAX_FILENAME_LEN]


def _validate_explicit_name(raw_name: str) -> str:
    """Validate an explicit rename/override name without silently rewriting invalid paths."""
    cleaned = raw_name.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or _CONTROL_CHARS_RE.search(cleaned)
    ):
        raise HTTPException(status_code=400, detail="Invalid file name")
    if len(cleaned) > _MAX_FILENAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"File name must be at most {_MAX_FILENAME_LEN} characters",
        )
    return cleaned


def _resolve_mime_type(content_type: str | None, file_name: str) -> str:
    raw = (content_type or "").split(";", 1)[0].strip().lower()
    if raw and raw != "application/octet-stream":
        return raw
    guessed, _ = mimetypes.guess_type(file_name)
    return guessed or "application/octet-stream"


def _to_file_item(raw: dict[str, Any], *, fallback_mime: str = "") -> TaxDriveFileItem:
    mime = str(raw.get("mimeType") or fallback_mime or "")
    raw_size = raw.get("size")
    try:
        size = int(raw_size) if raw_size is not None else 0
    except (TypeError, ValueError):
        size = 0
    return TaxDriveFileItem(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        mime_type=mime,
        size=size,
        modified_time=raw.get("modifiedTime"),
        web_view_link=raw.get("webViewLink"),
        is_folder=mime == _FOLDER_MIME,
    )


def _is_drive_not_found(exc: Exception) -> bool:
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in (403, 404)
    return False


async def _read_bounded_upload(upload: UploadFile) -> bytes:
    raw_size = getattr(upload, "size", None)
    if isinstance(raw_size, int) and raw_size > TAX_DOC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds maximum upload size (4 MB)")
    content = await upload.read(TAX_DOC_MAX_BYTES + 1)
    if len(content) > TAX_DOC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds maximum upload size (4 MB)")
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return content


async def _authorize_and_load_scope(
    client_id: int,
    current_user: dict[str, Any],
    pool: Any,
) -> _ClientDriveScope:
    """Verify caller may manage this client's tax documents and return allowed Drive roots + companies.

    Releases the DB connection before returning so Drive I/O never holds a pool slot.
    """
    auth_method = (current_user or {}).get("auth_method")
    if (
        auth_method is not None
        and str(auth_method).strip().lower() not in HUMAN_SESSION_AUTH_METHODS
    ):
        raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)
    if not is_human_team_member((current_user or {}).get("role")):
        raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)

    async with pool.acquire() as conn:
        await ensure_client_pools_validated(conn)
        client_row = await conn.fetchrow(
            """
            SELECT id, assigned_to, created_by, tax_consultant,
                   google_drive_folder_id, drive_folder_id
            FROM clients
            WHERE id = $1 AND deleted_at IS NULL
            """,
            client_id,
        )
        if not client_row:
            raise HTTPException(status_code=404, detail="Client not found")

        if not can_manage_client_tax_documents(
            current_user,
            _record_get(client_row, "assigned_to"),
            _record_get(client_row, "created_by"),
            _record_get(client_row, "tax_consultant"),
        ):
            logger.warning("tax_drive.authz_denied client_id=%s", client_id)
            raise HTTPException(status_code=403, detail=_AUTHZ_DENIED_DETAIL)

        company_rows = await conn.fetch(
            """
            SELECT ccl.company_id, co.google_drive_folder_id, co.tax_dept_folder_id
            FROM client_company_links ccl
            JOIN companies co ON co.id = ccl.company_id
            WHERE ccl.client_id = $1
            """,
            client_id,
        )

    roots: set[str] = set()
    for key in ("google_drive_folder_id", "drive_folder_id"):
        val = _record_get(client_row, key)
        if isinstance(val, str) and _DRIVE_FILE_ID_RE.match(val.strip()):
            roots.add(val.strip())

    company_ids: set[int] = set()
    for row in company_rows or ():
        cid = _record_get(row, "company_id")
        if isinstance(cid, int):
            company_ids.add(cid)
        for key in ("google_drive_folder_id", "tax_dept_folder_id"):
            val = _record_get(row, key)
            if isinstance(val, str) and _DRIVE_FILE_ID_RE.match(val.strip()):
                roots.add(val.strip())

    if not roots:
        raise HTTPException(
            status_code=403,
            detail="No Google Drive folder is linked to this client.",
        )
    return _ClientDriveScope(allowed_roots=roots, linked_company_ids=sorted(company_ids))


async def _invalidate_tax_doc_caches() -> None:
    """Invalidate Drive folder listing and dependent CRM client/document views."""
    await invalidate_cache("zantara:team_drive:*")
    await invalidate_cache("zantara:crm_clients_documents:*")
    await invalidate_cache("zantara:crm_clients_stats:*")


async def _dispatch_ocr_reprocessing(
    pool: Any,
    client_id: int,
    file_id: str,
    folder_name: str,
    filename: str,
    doc_id: int,
    document_type: str | None,
) -> None:
    """Re-run OCR dispatcher for a replaced registered document and settle pending state."""
    from backend.services.documents.ocr_dispatcher_service import dispatch_ocr_by_folder

    try:
        result = await dispatch_ocr_by_folder(
            db_pool=pool,
            client_id=client_id,
            file_id=file_id,
            folder_name=folder_name,
            filename=filename,
            doc_id=doc_id,
            document_type=document_type,
        )
        if not isinstance(result, dict) or not result.get("dispatched"):
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE documents
                    SET ocr_status = NULL, updated_at = NOW()
                    WHERE id = $1 AND ocr_status = 'pending'
                    """,
                    doc_id,
                )
    except Exception as exc:
        logger.error(
            "tax_drive.ocr_reprocess_failed client_id=%s file_id=%s doc_id=%s error=%s",
            client_id,
            file_id,
            doc_id,
            type(exc).__name__,
        )
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE documents
                    SET ocr_status = 'failed', updated_at = NOW()
                    WHERE id = $1 AND ocr_status = 'pending'
                    """,
                    doc_id,
                )
        except Exception as inner_exc:
            logger.error(
                "tax_drive.ocr_status_update_failed doc_id=%s error=%s",
                doc_id,
                type(inner_exc).__name__,
            )


async def _sync_renamed_registrations(
    pool: Any,
    client_id: int,
    linked_company_ids: list[int],
    file_id: str,
    new_name: str,
) -> None:
    """Propagate Drive rename to active client and linked-company document registrations."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE documents
            SET file_name = $3,
                updated_at = NOW()
            WHERE client_id = $1
              AND file_id = $2
              AND COALESCE(is_archived, false) = false
              AND deleted_at IS NULL
              AND COALESCE(status, '') <> 'deleted'
            """,
            client_id,
            file_id,
            new_name,
        )
        if linked_company_ids:
            await conn.execute(
                """
                UPDATE company_documents
                SET file_name = $3,
                    updated_at = NOW()
                WHERE company_id = ANY($1::int[])
                  AND google_drive_file_id = $2
                  AND status = 'active'
                  AND COALESCE(is_archived, false) = false
                  AND deleted_at IS NULL
                """,
                linked_company_ids,
                file_id,
                new_name,
            )


async def _sync_replaced_registrations(
    pool: Any,
    client_id: int,
    linked_company_ids: list[int],
    file_id: str,
    *,
    file_name: str | None,
    mime_type: str,
    content_bytes: bytes,
    web_view_link: str | None,
) -> list[dict[str, Any]]:
    """Update metadata/content_hash and clear stale OCR evidence for replaced registrations.

    Returns updated ``documents`` rows so the caller can enqueue OCR reprocessing.
    """
    content_hash = hashlib.md5(content_bytes).hexdigest()
    file_size_kb = max(1, len(content_bytes) // 1024)
    effective_name = file_name or None

    async with pool.acquire() as conn:
        updated_doc_rows = await conn.fetch(
            """
            UPDATE documents
            SET file_name = COALESCE($3, file_name),
                file_size_kb = $4,
                mime_type = $5,
                content_hash = $6,
                file_url = COALESCE($7, file_url),
                google_drive_file_url = COALESCE($7, google_drive_file_url),
                ocr_status = 'pending',
                ocr_completed_at = NULL,
                ocr_extracted_data = NULL,
                drive_verified_at = NOW(),
                updated_at = NOW()
            WHERE client_id = $1
              AND file_id = $2
              AND COALESCE(is_archived, false) = false
              AND deleted_at IS NULL
              AND COALESCE(status, '') <> 'deleted'
            RETURNING id, document_type, subfolder, file_name
            """,
            client_id,
            file_id,
            effective_name,
            file_size_kb,
            mime_type,
            content_hash,
            web_view_link,
        )
        if linked_company_ids:
            await conn.execute(
                """
                UPDATE company_documents
                SET file_name = COALESCE($3, file_name),
                    file_size_kb = $4,
                    mime_type = $5,
                    content_hash = $6,
                    google_drive_file_url = COALESCE($7, google_drive_file_url),
                    ocr_status = NULL,
                    ocr_completed_at = NULL,
                    ocr_extracted_data = NULL,
                    is_verified = false,
                    updated_at = NOW()
                WHERE company_id = ANY($1::int[])
                  AND google_drive_file_id = $2
                  AND status = 'active'
                  AND COALESCE(is_archived, false) = false
                  AND deleted_at IS NULL
                """,
                linked_company_ids,
                file_id,
                effective_name,
                file_size_kb,
                mime_type,
                content_hash,
                web_view_link,
            )

    return [dict(row) for row in (updated_doc_rows or ())]


async def _sync_trashed_registrations(
    pool: Any,
    client_id: int,
    linked_company_ids: list[int],
    file_id: str,
) -> None:
    """Mark matching CRM client/company document registrations deleted & archived after Drive trash."""
    async with pool.acquire() as conn:
        # One transaction: both registrations retire together or not at all, so a
        # retried delete never meets a half-synced pair.
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE documents
                SET status = 'deleted',
                    is_archived = true,
                    updated_at = NOW()
                WHERE client_id = $1
                  AND file_id = $2
                  AND (COALESCE(is_archived, false) = false OR COALESCE(status, '') <> 'deleted')
                """,
                client_id,
                file_id,
            )
            if linked_company_ids:
                await conn.execute(
                    """
                    UPDATE company_documents
                    SET status = 'deleted',
                        is_archived = true,
                        updated_at = NOW()
                    WHERE company_id = ANY($1::int[])
                      AND google_drive_file_id = $2
                      AND (COALESCE(is_archived, false) = false OR COALESCE(status, '') <> 'deleted')
                    """,
                    linked_company_ids,
                    file_id,
                )


async def _fetch_metadata_or_deny(
    drive: ServiceAccountDriveService,
    item_id: str,
    deny_detail: str,
) -> dict[str, Any]:
    try:
        return await drive.get_file_metadata(item_id)
    except Exception as exc:
        if _is_drive_not_found(exc):
            raise HTTPException(status_code=403, detail=deny_detail) from None
        logger.error("tax_drive.metadata_failed item_id=%s error=%s", item_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Google Drive request failed") from None


async def _descends_from_roots(
    drive: ServiceAccountDriveService,
    parents: list[str],
    allowed_roots: set[str],
) -> bool:
    """Walk Drive parent folders (Shared Drive safe) within hop and call budgets."""
    if any(p in allowed_roots for p in parents):
        return True

    frontier = [p for p in parents if isinstance(p, str) and _DRIVE_FILE_ID_RE.match(p)]
    seen: set[str] = set()
    lookups = 0

    for _ in range(_MAX_ANCESTOR_HOPS):
        if not frontier or lookups >= _MAX_ANCESTOR_LOOKUPS:
            break
        next_level: list[str] = []
        for parent_id in frontier:
            if parent_id in seen or lookups >= _MAX_ANCESTOR_LOOKUPS:
                continue
            seen.add(parent_id)
            lookups += 1
            try:
                meta = await drive.get_file_metadata(parent_id)
            except Exception:
                continue
            grandparents = [
                gp
                for gp in (meta.get("parents") or [])
                if isinstance(gp, str) and _DRIVE_FILE_ID_RE.match(gp)
            ]
            if any(gp in allowed_roots for gp in grandparents):
                return True
            next_level.extend(gp for gp in grandparents if gp not in seen)
        frontier = next_level

    return False


async def _verify_target_folder(
    drive: ServiceAccountDriveService,
    folder_id: str,
    allowed_roots: set[str],
) -> dict[str, Any]:
    """Confirm `folder_id` is an active folder at or under `allowed_roots`."""
    meta = await _fetch_metadata_or_deny(
        drive, folder_id, "Target folder does not belong to this client."
    )
    in_scope = folder_id in allowed_roots or await _descends_from_roots(
        drive, list(meta.get("parents") or []), allowed_roots
    )
    if not in_scope:
        raise HTTPException(
            status_code=403,
            detail="Target folder does not belong to this client.",
        )
    if meta.get("trashed") is True:
        raise HTTPException(status_code=404, detail="Target folder not found")
    if meta.get("mimeType") != _FOLDER_MIME:
        raise HTTPException(status_code=400, detail="Target is not a folder")
    return meta


async def _verify_target_file(
    drive: ServiceAccountDriveService,
    file_id: str,
    allowed_roots: set[str],
    *,
    allow_trashed: bool = False,
) -> dict[str, Any]:
    """Confirm `file_id` is an active non-folder file strictly under `allowed_roots`.

    `allow_trashed` lets the DELETE route retry a delete whose Drive trash succeeded but
    whose CRM sync failed; the scope checks above it are unchanged.
    """
    if file_id in allowed_roots:
        raise HTTPException(status_code=400, detail="Folders cannot be modified or deleted here")

    meta = await _fetch_metadata_or_deny(drive, file_id, "Document does not belong to this client.")
    in_scope = await _descends_from_roots(drive, list(meta.get("parents") or []), allowed_roots)
    if not in_scope:
        raise HTTPException(
            status_code=403,
            detail="Document does not belong to this client.",
        )
    if meta.get("trashed") is True and not allow_trashed:
        raise HTTPException(status_code=404, detail="Document not found")
    mime = meta.get("mimeType")
    if mime == _FOLDER_MIME:
        raise HTTPException(status_code=400, detail="Folders cannot be modified or deleted here")
    if mime == _SHORTCUT_MIME:
        raise HTTPException(status_code=400, detail="Drive shortcuts cannot be modified here")
    return meta


@router.post(
    "/clients/{client_id}/tax-documents/upload",
    response_model=TaxDriveFileResponse,
)
async def upload_client_tax_document(
    client_id: Annotated[int, Path(ge=1)],
    file: UploadFile = File(...),
    folder_id: str = Form(...),
    pool: Any = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_tax_documents_user),
    drive_factory: Callable[[], ServiceAccountDriveService] = Depends(
        get_tax_drive_service_factory
    ),
) -> TaxDriveFileResponse:
    """Upload a tax document into one of the client's or linked company's Drive folders."""
    scope = await _authorize_and_load_scope(client_id, current_user, pool)
    clean_folder_id = _assert_drive_id(folder_id, "folder_id")
    safe_name = _sanitize_upload_filename(file.filename)
    content = await _read_bounded_upload(file)
    mime_type = _resolve_mime_type(file.content_type, safe_name)

    drive = drive_factory()
    await _verify_target_folder(drive, clean_folder_id, scope.allowed_roots)

    try:
        uploaded = await drive.upload_file_to_folder(
            folder_id=clean_folder_id,
            file_content=content,
            file_name=safe_name,
            mime_type=mime_type,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "tax_drive.upload_failed client_id=%s folder_id=%s error=%s",
            client_id,
            clean_folder_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to upload document to Drive") from None

    await _invalidate_tax_doc_caches()
    item = _to_file_item(uploaded, fallback_mime=mime_type)
    logger.info(
        "tax_drive.uploaded client_id=%s folder_id=%s file_id=%s size=%s",
        client_id,
        clean_folder_id,
        item.id,
        len(content),
    )
    return TaxDriveFileResponse(success=True, file=item)


@router.patch(
    "/clients/{client_id}/tax-documents/{file_id}",
    response_model=TaxDriveFileResponse,
)
async def rename_client_tax_document(
    client_id: Annotated[int, Path(ge=1)],
    file_id: str,
    body: TaxDriveRenameRequest,
    pool: Any = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_tax_documents_user),
    drive_factory: Callable[[], ServiceAccountDriveService] = Depends(
        get_tax_drive_service_factory
    ),
) -> TaxDriveFileResponse:
    """Rename an existing tax document in Google Drive and sync CRM registrations."""
    scope = await _authorize_and_load_scope(client_id, current_user, pool)
    clean_file_id = _assert_drive_id(file_id, "file_id")
    safe_name = _validate_explicit_name(body.name)

    drive = drive_factory()
    await _verify_target_file(drive, clean_file_id, scope.allowed_roots)

    try:
        updated = await drive.rename_file(clean_file_id, safe_name)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "tax_drive.rename_failed client_id=%s file_id=%s error=%s",
            client_id,
            clean_file_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to rename document in Drive") from None

    item = _to_file_item(updated)
    try:
        await _sync_renamed_registrations(
            pool,
            client_id,
            scope.linked_company_ids,
            clean_file_id,
            item.name or safe_name,
        )
    except Exception as exc:
        logger.error(
            "tax_drive.rename_db_sync_failed client_id=%s file_id=%s error=%s",
            client_id,
            clean_file_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Document renamed in Drive, but failed to synchronize CRM records",
        ) from None

    await _invalidate_tax_doc_caches()
    logger.info("tax_drive.renamed client_id=%s file_id=%s", client_id, clean_file_id)
    return TaxDriveFileResponse(success=True, file=item)


@router.put(
    "/clients/{client_id}/tax-documents/{file_id}/content",
    response_model=TaxDriveFileResponse,
)
async def replace_client_tax_document(
    client_id: Annotated[int, Path(ge=1)],
    file_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    name: str | None = Form(None),
    pool: Any = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_tax_documents_user),
    drive_factory: Callable[[], ServiceAccountDriveService] = Depends(
        get_tax_drive_service_factory
    ),
) -> TaxDriveFileResponse:
    """Replace an existing tax document's content in-place, sync DB evidence, and re-run OCR."""
    scope = await _authorize_and_load_scope(client_id, current_user, pool)
    clean_file_id = _assert_drive_id(file_id, "file_id")
    explicit_name = _validate_explicit_name(name) if name is not None and name.strip() else None
    upload_name = _sanitize_upload_filename(file.filename)
    content = await _read_bounded_upload(file)
    effective_name = explicit_name or upload_name
    mime_type = _resolve_mime_type(file.content_type, effective_name)

    drive = drive_factory()
    await _verify_target_file(drive, clean_file_id, scope.allowed_roots)

    try:
        updated = await drive.update_file_content(
            clean_file_id,
            content,
            file_name=explicit_name,
            mime_type=mime_type,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "tax_drive.replace_failed client_id=%s file_id=%s error=%s",
            client_id,
            clean_file_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Failed to replace document in Drive") from None

    item = _to_file_item(updated, fallback_mime=mime_type)
    try:
        updated_docs = await _sync_replaced_registrations(
            pool,
            client_id,
            scope.linked_company_ids,
            clean_file_id,
            file_name=item.name or explicit_name,
            mime_type=item.mime_type or mime_type,
            content_bytes=content,
            web_view_link=item.web_view_link,
        )
    except Exception as exc:
        logger.error(
            "tax_drive.replace_db_sync_failed client_id=%s file_id=%s error=%s",
            client_id,
            clean_file_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Document replaced in Drive, but failed to synchronize CRM records",
        ) from None

    for doc_row in updated_docs:
        doc_id = _record_get(doc_row, "id")
        if isinstance(doc_id, int):
            background_tasks.add_task(
                _dispatch_ocr_reprocessing,
                pool,
                client_id,
                clean_file_id,
                _record_get(doc_row, "subfolder") or "03_Tax",
                _record_get(doc_row, "file_name") or item.name or effective_name,
                doc_id,
                _record_get(doc_row, "document_type"),
            )

    await _invalidate_tax_doc_caches()
    logger.info(
        "tax_drive.replaced client_id=%s file_id=%s size=%s synced_docs=%s",
        client_id,
        clean_file_id,
        len(content),
        len(updated_docs),
    )
    return TaxDriveFileResponse(success=True, file=item)


@router.delete(
    "/clients/{client_id}/tax-documents/{file_id}",
    response_model=TaxDriveDeleteResponse,
)
async def delete_client_tax_document(
    client_id: Annotated[int, Path(ge=1)],
    file_id: str,
    pool: Any = Depends(get_database_pool),
    current_user: dict[str, Any] = Depends(get_tax_documents_user),
    drive_factory: Callable[[], ServiceAccountDriveService] = Depends(
        get_tax_drive_service_factory
    ),
) -> TaxDriveDeleteResponse:
    """Move a client's tax document to Google Drive Trash and retire CRM registrations."""
    scope = await _authorize_and_load_scope(client_id, current_user, pool)
    clean_file_id = _assert_drive_id(file_id, "file_id")

    drive = drive_factory()
    meta = await _verify_target_file(drive, clean_file_id, scope.allowed_roots, allow_trashed=True)

    # Already trashed: an earlier delete trashed it but its CRM sync failed. Skip the Drive
    # call and only re-run the sync.
    if meta.get("trashed") is not True:
        try:
            await drive.trash_file(clean_file_id)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "tax_drive.trash_failed client_id=%s file_id=%s error=%s",
                client_id,
                clean_file_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=502, detail="Failed to delete document in Drive"
            ) from None

    try:
        await _sync_trashed_registrations(
            pool,
            client_id,
            scope.linked_company_ids,
            clean_file_id,
        )
    except Exception as exc:
        logger.error(
            "tax_drive.trash_db_sync_failed client_id=%s file_id=%s error=%s",
            client_id,
            clean_file_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="Document moved to Drive Trash, but failed to synchronize CRM records",
        ) from None

    await _invalidate_tax_doc_caches()
    logger.info("tax_drive.trashed client_id=%s file_id=%s", client_id, clean_file_id)
    return TaxDriveDeleteResponse(success=True, file_id=clean_file_id, trashed=True)
