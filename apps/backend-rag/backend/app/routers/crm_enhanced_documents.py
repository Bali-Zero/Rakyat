"""
CRM Enhanced - Document Management Endpoints

Split from crm_enhanced.py for maintainability.
Provides: document CRUD, categories, OCR status, visa extraction,
document upload (Base64 + Google Drive), document soft-delete.
"""

import base64
import hashlib
import re
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, StrictBool, StrictInt
from pydantic_core import PydanticCustomError

from backend.app.core.config import settings
from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.deps.crm_service_write import verify_crm_write_key
from backend.app.modules.crm.tax_drive_documents import router as tax_drive_documents_router
from backend.app.routers.crm_enhanced import (
    DocumentCreate,
    DocumentUpdate,
    _auto_ocr_visa,
    _dispatch_ocr_by_folder,
)
from backend.app.routers.documents_proxy import assert_drive_file_belongs_to_client
from backend.app.utils.crm_utils import (
    can_change_document_visibility,
    is_crm_admin,
    verify_client_access,
)
from backend.app.utils.logging_utils import get_logger
from backend.core.cache import invalidate_cache
from backend.db.repositories.client_repository import CORE_OWNER_IDS_SQL
from backend.phone_lock import lock_cores, phone_core, phone_value_state
from backend.services.common.background import spawn
from backend.services.crm.admin_document_visibility import admin_document_not_deleted_clause
from backend.services.crm.document_categorizer import auto_categorize_document
from backend.services.crm.documents import validate_client_document_date
from backend.services.crm.drive_taxonomy import (
    folder_for_category,
    physical_root_layout,
)
from backend.services.integrations.service_account_drive_service import ServiceAccountDriveService

logger = get_logger(__name__)


def _access_not_preverified() -> bool:
    return False


def _no_delivery_repair_slot() -> None:
    # Set only by the authenticated, in-process final-permit repair operation.
    return None


def _team_upload_source() -> str:
    # documents.uploaded_source feeds Champion Round 2 scoring, so HTTP callers
    # can never set it; only an in-process caller that authenticated a portal
    # client (crm_practices.upload_client_document) passes 'client'.
    return "team"


def _parse_date_or_none(date_str: str | None) -> date | None:
    """Parse YYYY-MM-DD date string or return None."""
    if not date_str:
        return None
    try:
        return validate_client_document_date(datetime.strptime(date_str, "%Y-%m-%d").date())
    except ValueError:
        logger.debug(
            "crm_enhanced_documents.parse_date_invalid",
            extra={"raw": str(date_str)[:32]},
        )
        return None


def _row_get(row: Any, key: str) -> Any:
    """Subscript a DB row (asyncpg Record / dict), tolerating missing keys —
    test doubles built before a column was added must read as absent, not
    crash the gate."""
    try:
        return row[key]
    except (KeyError, TypeError):
        return None


router = APIRouter(prefix="/api/crm", tags=["crm-enhanced-documents"])
router.include_router(tax_drive_documents_router)

# ============================================
# DOCUMENTS ENDPOINTS
# ============================================


@router.get("/clients/{client_id}/documents")
async def get_client_documents(
    client_id: int,
    category: str | None = Query(
        None,
        description="Filter by category: immigration, pma, tax, personal",
    ),
    include_archived: bool = Query(False, description="Include archived documents"),
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[Any]:
    """
    Get all documents for a client, optionally filtered by category.
    """
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True)
        query = """
            SELECT
                d.id, d.document_type, d.document_category,
                d.file_name, d.file_id, d.file_url, d.google_drive_file_url,
                d.status, d.expiry_date, d.notes, d.is_archived,
                d.family_member_id, d.practice_id, d.client_visible,
                d.created_at, d.updated_at,
                fm.full_name as family_member_name,
                CASE
                    WHEN d.expiry_date <= CURRENT_DATE THEN 'expired'
                    WHEN d.expiry_date <= CURRENT_DATE + INTERVAL '8 months' THEN 'red'
                    WHEN d.expiry_date <= CURRENT_DATE + INTERVAL '12 months' THEN 'yellow'
                    ELSE 'green'
                END as alert_color
            FROM documents d
            LEFT JOIN client_family_members fm ON d.family_member_id = fm.id
            WHERE d.client_id = $1
        """
        # Deleted by the team (status) or by the client in the portal
        # (deleted_at) never shows here, archived or not.
        query += f" AND {admin_document_not_deleted_clause('d')}"
        params = [client_id]
        param_num = 2

        if category:
            query += f" AND d.document_category = ${param_num}"
            params.append(category)
            param_num += 1

        if not include_archived:
            query += " AND (d.is_archived IS NULL OR d.is_archived = false)"

        query += " ORDER BY d.document_category, d.document_type"

        docs = await conn.fetch(query, *params)
        return [dict(d) for d in docs]


@router.post("/clients/{client_id}/documents")
async def create_document(
    client_id: int,
    data: DocumentCreate,
    background_tasks: BackgroundTasks,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Add a document to a client. Auto-triggers OCR for passport documents.
    """
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)
        if data.file_id:
            await assert_drive_file_belongs_to_client(data.file_id, client_id, conn)
        # Sanitize date field - convert string to date object for asyncpg
        expiry_date = None
        if data.expiry_date:
            try:
                expiry_date = datetime.strptime(data.expiry_date, "%Y-%m-%d").date()
                validate_client_document_date(expiry_date)
            except ValueError:
                expiry_date = None

        doc_id = await conn.fetchval(
            """
            INSERT INTO documents (
                client_id, document_type, document_category,
                file_name, file_id, file_url, google_drive_file_url,
                expiry_date, notes, family_member_id, practice_id,
                status, storage_type
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'google_drive', 'google_drive')
            RETURNING id
            """,
            client_id,
            data.document_type,
            data.document_category,
            data.file_name,
            data.file_id,
            data.file_url,
            data.google_drive_file_url,
            expiry_date,  # Sanitized
            data.notes,
            data.family_member_id,
            data.practice_id,
        )

        # Auto-trigger OCR via dispatcher
        ocr_triggered = False
        if data.file_id and data.document_type:
            # Determine folder from category
            folder_hint = ""
            cat = (data.document_category or "").lower()
            if cat == "immigration":
                folder_hint = "01_Immigration"
            elif cat == "tax":
                folder_hint = "03_Tax"
            elif cat == "company":
                folder_hint = "02_Company"
            background_tasks.add_task(
                _dispatch_ocr_by_folder,
                pool,
                client_id,
                data.file_id,
                folder_hint,
                data.file_name or data.document_type,
                doc_id,
            )
            # Mark document as pending OCR
            await conn.execute(
                "UPDATE documents SET ocr_status = 'pending' WHERE id = $1",
                doc_id,
            )
            ocr_triggered = True

        # Notify client via portal that a new document was added
        try:
            from backend.services.portal.portal_notification_service import (
                PortalNotificationService,
            )

            notif_service = PortalNotificationService(pool)
            spawn(
                notif_service.notify_document_uploaded(
                    client_id=client_id,
                    document_type=data.document_type,
                    sent_by=current_user.get("email", "system"),
                    practice_id=data.practice_id,
                ),
            )
        except Exception as e:
            logger.error("Portal notification for doc upload failed: %s", e)

        await invalidate_cache("zantara:crm_clients_stats:*")
        return {
            "id": doc_id,
            "success": True,
            "ocr_triggered": ocr_triggered,
        }


class ClientArchiveDocumentCreate(BaseModel):
    """Body of the archive registration. Visibility has no default: the caller states it."""

    file_id: str
    document_type: str
    document_category: str | None = None
    file_name: str | None = None
    google_drive_file_url: str | None = None
    notes: str | None = None
    client_visible: StrictBool


@router.post("/clients/{client_id}/documents/archive")
async def register_archive_document(
    client_id: int,
    data: ClientArchiveDocumentCreate,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Register an existing Drive file as a client document (archive path, admin only).

    No OCR, no portal message, no write to clients; visibility is explicit and the
    registration is idempotent on (client_id, file_id).
    """
    if not is_crm_admin(current_user):
        raise HTTPException(status_code=403, detail="Admin access required")
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)
        await assert_drive_file_belongs_to_client(data.file_id, client_id, conn)
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(742021, hashtext($1))",
                f"{client_id}:{data.file_id}",
            )
            existing_id = await conn.fetchval(
                f"""
                SELECT d.id FROM documents d
                WHERE d.client_id = $1 AND d.file_id = $2
                  AND {admin_document_not_deleted_clause("d")}
                ORDER BY d.id LIMIT 1
                """,
                client_id,
                data.file_id,
            )
            if existing_id is not None:
                return {
                    "id": existing_id,
                    "success": True,
                    "deduplicated": True,
                    "ocr_triggered": False,
                }
            doc_id = await conn.fetchval(
                """
                INSERT INTO documents (
                    client_id, document_type, document_category, file_name, file_id,
                    google_drive_file_url, notes, status, storage_type, uploaded_by,
                    client_visible
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', 'google_drive', $8, $9)
                RETURNING id
                """,
                client_id,
                data.document_type,
                data.document_category,
                data.file_name,
                data.file_id,
                data.google_drive_file_url,
                data.notes,
                (current_user.get("email") or "system").lower(),
                data.client_visible,
            )
        await invalidate_cache("zantara:crm_clients_stats:*")
        return {"id": doc_id, "success": True, "deduplicated": False, "ocr_triggered": False}


@router.post("/clients/{client_id}/documents/bulk")
async def create_documents_bulk(
    client_id: int,
    documents: list[DocumentCreate],
    background_tasks: BackgroundTasks,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Bulk insert documents for a client - optimized for migration.

    This endpoint allows inserting multiple documents in a single transaction,
    significantly improving performance during large data migrations.

    Args:
        client_id: Client database ID
        documents: Array of documents to insert (max 100 per request)
        background_tasks: FastAPI background tasks for OCR
        pool: Database connection pool
        current_user: Authenticated user

    Returns:
        {
            "success": true,
            "inserted": 50,
            "document_ids": [123, 124, ...],
            "ocr_triggered": 2,
            "failed": 0
        }

    Raises:
        HTTPException: If max limit exceeded or client not found
    """
    user_email = current_user.get("email", "").lower()

    # Enforce maximum batch size
    MAX_BATCH_SIZE = 100
    if len(documents) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {MAX_BATCH_SIZE} documents per request. You provided {len(documents)}.",
        )

    if len(documents) == 0:
        raise HTTPException(status_code=400, detail="No documents provided")

    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)

        inserted_ids = []
        ocr_count = 0
        failed_count = 0

        # Use transaction for atomic bulk insert
        async with conn.transaction():
            for doc in documents:
                try:
                    if doc.file_id:
                        await assert_drive_file_belongs_to_client(doc.file_id, client_id, conn)
                    # Sanitize expiry_date: convert string to date object
                    expiry_date = None
                    if doc.expiry_date:
                        try:
                            expiry_date = datetime.strptime(doc.expiry_date, "%Y-%m-%d").date()
                            validate_client_document_date(expiry_date)
                        except ValueError:
                            logger.warning(
                                f"Invalid expiry_date format for {doc.file_name}: {doc.expiry_date}",
                            )
                            expiry_date = None

                    # Insert document
                    doc_id = await conn.fetchval(
                        """
                        INSERT INTO documents (
                            client_id, document_type, document_category,
                            file_name, file_id, file_url, google_drive_file_url,
                            expiry_date, notes, family_member_id, practice_id,
                            status, storage_type, uploaded_by
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active', 'google_drive', $12)
                        RETURNING id
                        """,
                        client_id,
                        doc.document_type,
                        doc.document_category,
                        doc.file_name,
                        doc.file_id,
                        doc.file_url,
                        doc.google_drive_file_url,
                        expiry_date,
                        doc.notes,
                        doc.family_member_id,
                        doc.practice_id,
                        user_email,  # Track who uploaded
                    )

                    inserted_ids.append(doc_id)

                    # Queue OCR for passport documents
                    if doc.file_id and doc.document_type:
                        folder_hint = ""
                        cat = (doc.document_category or "").lower()
                        if cat == "immigration":
                            folder_hint = "01_Immigration"
                        elif cat == "tax":
                            folder_hint = "03_Tax"
                        elif cat == "company":
                            folder_hint = "02_Company"
                        background_tasks.add_task(
                            _dispatch_ocr_by_folder,
                            pool,
                            client_id,
                            doc.file_id,
                            folder_hint,
                            doc.file_name or doc.document_type,
                            doc_id,
                        )
                        await conn.execute(
                            "UPDATE documents SET ocr_status = 'pending' WHERE id = $1",
                            doc_id,
                        )
                        ocr_count += 1

                except Exception as e:
                    logger.error(f"Failed to insert document {doc.file_name}: {e}")
                    failed_count += 1
                    # Continue with other documents

        logger.info(
            f"Bulk inserted {len(inserted_ids)} documents for client {client_id}. "
            f"OCR queued: {ocr_count}, Failed: {failed_count}",
        )

        await invalidate_cache("zantara:crm_clients_stats:*")
        return {
            "success": True,
            "inserted": len(inserted_ids),
            "document_ids": inserted_ids,
            "ocr_triggered": ocr_count,
            "failed": failed_count,
            "client_id": client_id,
        }


@router.patch("/clients/{client_id}/documents/{doc_id}")
async def update_document(
    client_id: int,
    doc_id: int,
    data: DocumentUpdate,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Update a document.

    `client_visible` is a separate gate: only CRM admins, Team Leader, Tax Lead and
    Supervisor may send it (403 otherwise, nothing written).
    """
    # Date field that needs string → date object conversion for asyncpg
    date_fields = {"expiry_date"}

    update_fields = []
    values = []
    param_num = 1
    payload = data.model_dump(exclude_unset=True)
    visibility_in_payload = "client_visible" in payload
    new_visible = payload.get("client_visible")

    for field, value in payload.items():
        # Convert date fields: empty string → None, valid string → date object
        if field in date_fields:
            if value == "" or value is None:
                value = None
            elif isinstance(value, str):
                try:
                    value = datetime.strptime(value, "%Y-%m-%d").date()
                    validate_client_document_date(value)
                except ValueError:
                    value = None

        if value is not None:
            update_fields.append(f"{field} = ${param_num}")
            values.append(value)
            param_num += 1

    if not update_fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    values.extend([doc_id, client_id])

    old_visible = None
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)
        if visibility_in_payload and not can_change_document_visibility(current_user):
            raise HTTPException(
                status_code=403,
                detail="You don't have permission to change document client visibility.",
            )
        if data.file_id:
            await assert_drive_file_belongs_to_client(data.file_id, client_id, conn)
        async with conn.transaction():
            if new_visible is not None:
                old_row = await conn.fetchrow(
                    "SELECT client_visible FROM documents WHERE id = $1 AND client_id = $2 FOR UPDATE",
                    doc_id,
                    client_id,
                )
                old_visible = old_row["client_visible"] if old_row else None
            result = await conn.execute(
                f"""
                UPDATE documents
                SET {", ".join(update_fields)}, updated_at = NOW()
                WHERE id = ${param_num} AND client_id = ${param_num + 1}
                """,
                *values,
            )

        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Document not found")

    if new_visible is not None and old_visible is not None and old_visible != new_visible:
        await _record_visibility_timeline_event(
            pool, client_id, doc_id, old_visible, new_visible, current_user
        )

    await invalidate_cache("zantara:crm_clients_stats:*")
    return {"success": True}


def _actor_id(actor: dict) -> str:
    """The auth dependency's `user_id`, which falls back to the email when no id
    is known: an email is PII, so it is never written to the Timeline."""
    raw = str(actor.get("user_id") or actor.get("id") or "")
    return "unresolved" if not raw or "@" in raw else raw


async def _record_visibility_timeline_event(
    pool: Any,
    client_id: int,
    doc_id: int,
    old_visible: bool,
    new_visible: bool,
    actor: dict,
) -> None:
    """Best-effort Timeline entry on a SEPARATE connection, after the change committed.

    Same shape as the portal's document soft-delete audit (event_type 'status_change',
    the only value the chk_timeline_event_type CHECK allows for this), but
    client_visible=false: the client portal must never read "hidden from you".
    Ids and role only, no file name, no email.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO timeline_events (
                    client_id, event_type, title, description, event_date, client_visible, color
                )
                VALUES ($1, 'status_change', $2, $3, NOW(), false, 'info')
                """,
                client_id,
                "Document client visibility changed",
                f"document_id={doc_id} client_visible: {str(old_visible).lower()} -> "
                f"{str(new_visible).lower()} by user_id={_actor_id(actor)} "
                f"role={(actor.get('role') or 'unknown')}",
            )
    except Exception as e:
        logger.warning("Could not record visibility timeline event for doc %s: %s", doc_id, e)


@router.delete("/clients/{client_id}/documents/{doc_id}")
async def archive_document(
    client_id: int,
    doc_id: int,
    permanent: bool = Query(False, description="Permanently delete instead of archive"),
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Archive or delete a document.
    """
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)
        if permanent:
            result = await conn.execute(
                "DELETE FROM documents WHERE id = $1 AND client_id = $2",
                doc_id,
                client_id,
            )
            action = "deleted"
        else:
            result = await conn.execute(
                "UPDATE documents SET is_archived = true, updated_at = NOW() WHERE id = $1 AND client_id = $2",
                doc_id,
                client_id,
            )
            action = "archived"

        if result in ("DELETE 0", "UPDATE 0"):
            raise HTTPException(status_code=404, detail="Document not found")

    await invalidate_cache("zantara:crm_clients_stats:*")
    return {"success": True, "action": action}


# ============================================
# DOCUMENT CATEGORIES ENDPOINT
# ============================================


@router.get("/document-categories")
async def get_document_categories(
    pool: Any = Depends(get_database_pool),
    _current_user: dict = Depends(get_current_user),
) -> list[Any]:
    """Get all document categories for dropdowns."""
    async with pool.acquire() as conn:
        categories = await conn.fetch(
            """
            SELECT code, name, category_group, description, has_expiry
            FROM document_categories
            WHERE active = true
            ORDER BY sort_order, name
            """,
        )
        return [dict(c) for c in categories]


# ============================================
# OCR STATUS ENDPOINT
# ============================================


@router.get("/clients/{client_id}/ocr-status")
async def get_client_ocr_status(
    client_id: int,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Get OCR processing status for a client's documents.
    Frontend polls this to know when OCR completes.
    """
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True)
        docs = await conn.fetch(
            f"""
            SELECT id, document_type, file_name, ocr_status, ocr_completed_at,
                   ocr_extracted_data
            FROM documents
            WHERE client_id = $1
              AND ocr_status IS NOT NULL
              AND {admin_document_not_deleted_clause()}
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            client_id,
        )

        pending = sum(1 for d in docs if d["ocr_status"] in ("pending", "processing"))
        completed = sum(1 for d in docs if d["ocr_status"] == "completed")

        return {
            "client_id": client_id,
            "pending_ocr": pending,
            "completed_ocr": completed,
            "documents": [dict(d) for d in docs],
        }


class ExtractVisaRequest(BaseModel):
    file_id: str = Field(..., min_length=1)  # keeps "" rejected, like the old `if not file_id`
    doc_id: StrictInt | None = None  # strict: lax int would turn `true` into document id 1


@router.post("/clients/{client_id}/extract-visa")
async def extract_visa_data(
    client_id: int,
    body: ExtractVisaRequest,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Extract visa/KITAS data from a document using Gemini Vision OCR.
    Updates the document's issue_date and expiry_date.

    Body: {"file_id": "...", "doc_id": 123}
    """
    async with pool.acquire() as conn:
        await verify_client_access(client_id, current_user, conn, allow_assigned=True, write=True)

    return await _auto_ocr_visa(pool, client_id, body.file_id, body.doc_id)


# ============================================
# EXPIRY ALERTS ENDPOINTS
# ============================================


# NEW ENDPOINTS (Frontend Integration)
# ============================================


_PRACTICE_ID_DIGITS = r"^[+-]?[0-9]{1,10}$"
_LegacyPracticeIdText = Annotated[
    str,
    Field(
        pattern=_PRACTICE_ID_DIGITS,
        json_schema_extra={"not": {"pattern": r"\s"}},
        description="Legacy decimal reference with up to ten digits and an optional sign.",
    ),
]


def _normalize_practice_number(value: Any) -> Any:
    if isinstance(value, str) and re.fullmatch(_PRACTICE_ID_DIGITS, value):
        return int(value)
    # JSON Schema treats a number with a zero fractional part as an integer.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


_WirePracticeRowId = Annotated[
    StrictInt,
    BeforeValidator(_normalize_practice_number, json_schema_input_type=int | _LegacyPracticeIdText),
]


def _require_hyphenated_uuid(value: Any) -> Any:
    # The producer sends canonical UUID text. A compact all-digit UUID must not
    # introduce a second spelling that can be confused with a decimal reference.
    if isinstance(value, str) and not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value
    ):
        raise PydanticCustomError("uuid_format", "Expected a hyphenated practice UUID")
    return value


_WirePracticeUuid = Annotated[UUID, BeforeValidator(_require_hyphenated_uuid)]


class DocumentUploadBase64(BaseModel):
    file: str  # Base64
    file_name: str
    document_type: str
    mime_type: str | None = None
    notes: str | None = None
    subfolder_hint: str | None = None
    document_category: str | None = None
    expiry_date: str | None = None
    family_member_id: int | None = None
    # Canonical numeric IDs are used by the frontend. Replicas send the
    # canonical UUID on this same key so older integer-only servers reject it.
    practice_id: _WirePracticeRowId | _WirePracticeUuid | None = None
    company_id: _WirePracticeRowId | _WirePracticeUuid | None = None
    intake_idempotency_key: str | None = None
    # Ownership token (Codex 2026-07-19 round 12, F12 gap 3): intake delivery
    # resolves the target client BY PHONE in a separate earlier request; this
    # carries the resolved phone core into the upload so the handler can prove,
    # atomically with the documents INSERT, that the target row still owns that
    # phone. Optional — human/frontend uploads address a reviewed client id and
    # don't send it.
    expected_phone_core: str | None = None
    # Explicit visibility of the new row on the client portal
    # (documents.client_visible). Absent (None) = the INSERT does not name the
    # column, so the database DEFAULT applies exactly as before; an explicit
    # value is written in the SAME INSERT. Intake delivery sends False for a
    # `recovery-v1` row (staff-only until the staff review).
    client_visible: bool | None = None


class DocumentUploadConflict(BaseModel):
    detail: str
    correlation_id: str


_UPLOAD_CONFLICT = {
    409: {
        "model": DocumentUploadConflict,
        "description": "Duplicate document content or unresolved client or practice ownership.",
    }
}


async def _resolve_upload_practice(
    conn: Any,
    client_id: int,
    reference: int | UUID | None,
    expected_phone_core: str | None,
    *,
    lock: bool = False,
) -> int | None:
    """Resolve a practice in the authorized client's namespace."""
    if reference is None:
        return None
    # Never bind an out-of-range value to the Postgres integer key. Like a
    # well-formed UUID absent from the table, it cannot identify a practice.
    if isinstance(reference, int) and (expected_phone_core or not -(2**31) <= reference < 2**31):
        raise HTTPException(status_code=409, detail="practice_identity_unresolved")
    column = "uuid" if isinstance(reference, UUID) else "id"
    row = await conn.fetchrow(
        f"SELECT id FROM practices WHERE {column} = $1 AND client_id = $2"
        + (" FOR SHARE" if lock else ""),
        str(reference) if isinstance(reference, UUID) else reference,
        client_id,
    )
    if row is None:
        raise HTTPException(status_code=409, detail="practice_identity_unresolved")
    return int(row["id"])


async def _documents_columns(conn: Any) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'documents' "
        "AND (table_schema = current_schema() OR table_schema LIKE 'pg_temp%')"
    )
    return {str(r["column_name"]) for r in rows}


async def _company_documents_columns(conn: Any) -> set[str]:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'company_documents' "
        "AND (table_schema = current_schema() OR table_schema LIKE 'pg_temp%')"
    )
    return {str(r["column_name"]) for r in rows}


def _intake_company_marker(idempotency_key: str) -> str:
    return f"[intake_key:{idempotency_key}]"


def _append_intake_marker(notes: str | None, idempotency_key: str) -> str:
    marker = _intake_company_marker(idempotency_key)
    if notes and marker in notes:
        return notes
    return f"{notes} {marker}".strip() if notes else marker


async def _probe_existing_client_intake_doc(
    conn: Any,
    client_id: int,
    idempotency_key: str,
    doc_cols: set[str],
) -> Any:
    if "intake_idempotency_key" in doc_cols:
        return await conn.fetchrow(
            """
            SELECT id, file_id, file_url
            FROM documents
            WHERE client_id = $1 AND intake_idempotency_key = $2
            ORDER BY id ASC LIMIT 1
            """,
            client_id,
            idempotency_key,
        )
    marker_like = f"%{_intake_company_marker(idempotency_key)}%"
    return await conn.fetchrow(
        """
        SELECT id, file_id, file_url
        FROM documents
        WHERE client_id = $1 AND notes LIKE $2
        ORDER BY id ASC LIMIT 1
        """,
        client_id,
        marker_like,
    )


async def _probe_existing_company_intake_doc(
    conn: Any,
    company_id: int,
    idempotency_key: str,
    comp_cols: set[str],
) -> Any:
    if "intake_idempotency_key" in comp_cols:
        return await conn.fetchrow(
            """
            SELECT id, google_drive_file_id, google_drive_file_url
            FROM company_documents
            WHERE company_id = $1 AND intake_idempotency_key = $2
            ORDER BY id ASC LIMIT 1
            """,
            company_id,
            idempotency_key,
        )
    marker_like = f"%{_intake_company_marker(idempotency_key)}%"
    return await conn.fetchrow(
        """
        SELECT id, google_drive_file_id, google_drive_file_url
        FROM company_documents
        WHERE company_id = $1 AND notes LIKE $2
        ORDER BY id ASC LIMIT 1
        """,
        company_id,
        marker_like,
    )


async def _resolve_upload_company(
    conn: Any,
    client_id: int,
    reference: int | UUID | None,
    expected_phone_core: str | None,
    *,
    lock: bool = False,
) -> int | None:
    """Resolve and verify a linked company on client upload (UUID on cross-DB leg)."""
    if reference is None:
        return None
    if isinstance(reference, int):
        if expected_phone_core or not -(2**31) <= reference < 2**31:
            raise HTTPException(
                status_code=409,
                detail="company_identity_unresolved: cross-DB delivery requires canonical company UUID",
            )
        if lock:
            return int(reference)
        company_link = await conn.fetchrow(
            """
            SELECT company_id
            FROM client_company_links
            WHERE client_id = $1 AND company_id = $2
            LIMIT 1
            """,
            client_id,
            reference,
        )
        if not company_link:
            raise HTTPException(status_code=400, detail="Company is not linked to this client")
        return int(company_link["company_id"])

    row = await conn.fetchrow(
        """
        SELECT c.id, c.status, l.client_id
        FROM companies c
        LEFT JOIN client_company_links l
               ON l.company_id = c.id AND l.client_id = $1
        WHERE c.uuid = $2
        """
        + (" FOR SHARE OF c" if lock else ""),
        client_id,
        str(reference),
    )
    if row is None or str(row["status"] or "").lower() == "deleted":
        raise HTTPException(status_code=409, detail="company_identity_unresolved")
    if row["client_id"] is None:
        raise HTTPException(status_code=400, detail="Company is not linked to this client")
    return int(row["id"])


async def _active_duplicate_document_id(
    conn: Any,
    client_id: int,
    content_hash: str,
    *,
    practice_id: int | None = None,
    family_member_id: int | None = None,
    missing_delivery_slot: str | None = None,
) -> int | None:
    """Id of a document this client already has with the same file content
    attached in the SAME context (practice, family member).

    "Active" is the PR 7967 predicate plus ``is_archived``: a document deleted
    by the team (status), by the client (deleted_at) or archived ("Delete
    passport" only archives) does not count, so it may be uploaded again.
    The key follows the attachment context (review 12 Oct): the same passport
    PDF attached to a second practice, or to a family member, is a new
    attachment, not a duplicate. Application-level check; index 074 stays
    non-unique.
    """
    exclusion = ""
    repair_args = []
    if missing_delivery_slot:
        # A lost pointer is still an active database row. Exempt only that
        # exact recorded case/slot during authenticated completed-permit repair.
        exclusion = """ AND NOT (NULLIF(d.file_id,'') IS NULL AND EXISTS (
            SELECT 1 FROM practice_delivery_slots s
            JOIN practice_delivery_completions c ON c.practice_id=s.practice_id AND c.service_code=s.service_code
            JOIN practices p ON p.id=s.practice_id AND p.status='completed'
            WHERE s.document_id=d.id AND s.practice_id=d.practice_id AND s.slot_key=$5))"""
        repair_args.append(missing_delivery_slot)
    return await conn.fetchval(
        "SELECT d.id FROM documents d"
        " WHERE d.client_id = $1 AND d.content_hash = $2"
        " AND d.practice_id IS NOT DISTINCT FROM $3"
        " AND d.family_member_id IS NOT DISTINCT FROM $4"
        " AND COALESCE(d.is_archived, false) = false"
        f" AND {admin_document_not_deleted_clause('d')}"
        f"{exclusion} ORDER BY d.id LIMIT 1",
        client_id,
        content_hash,
        practice_id,
        family_member_id,
        *repair_args,
    )


@router.post("/clients/{client_id}/documents/upload", responses=_UPLOAD_CONFLICT)
async def upload_document_base64(
    client_id: int,
    data: DocumentUploadBase64 = Body(...),
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    access_already_verified: bool = Depends(_access_not_preverified),
    uploaded_source: str = Depends(_team_upload_source),
    missing_delivery_slot: str | None = Depends(_no_delivery_repair_slot),
) -> dict[str, Any]:
    """
    Upload a document via Base64 (for frontend integration).
    Handles Google Drive upload and document creation.
    """
    # A direct call that omits the kwarg gets the unresolved Depends marker.
    source = "client" if uploaded_source == "client" else "team"
    # RBAC check before processing upload
    if access_already_verified is not True:
        async with pool.acquire() as _conn:
            await verify_client_access(
                client_id, current_user, _conn, allow_assigned=True, write=True
            )

    try:
        # Decode Base64
        try:
            file_content = base64.b64decode(data.file)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid base64 file content")

        # Compute content hash for dedup
        content_hash = hashlib.md5(file_content).hexdigest()

        # Determine category — use explicit override or auto-categorize from filename
        if data.document_category:
            category = data.document_category
        else:
            cat_result = auto_categorize_document(data.file_name)
            category = cat_result["document_category"]

        async with pool.acquire() as conn:
            client = await conn.fetchrow(
                "SELECT id, full_name, google_drive_folder_id, client_type,"
                "       phone, phone_normalized, whatsapp"
                "  FROM clients WHERE id = $1",
                client_id,
            )

            if not client:
                raise HTTPException(status_code=404, detail="Client not found")

            practice_id = await _resolve_upload_practice(
                conn, client_id, data.practice_id, data.expected_phone_core
            )

            def _owns_expected_core(row: Any) -> bool:
                # Column CONSISTENCY, not membership (Codex round 13, F12g3):
                # ownership is proven only when the row's phone columns agree
                # on EXACTLY the expected core. A divergent row (phone=A,
                # phone_normalized=B) is the same untrustworthy stale state the
                # delivery gate fails closed on — expected∈{A,B} must refuse.
                # Round 14 F19 + round 15 F23: absent/unusable/usable comes
                # from THE shared primitive (phone_value_state) — 'absent'
                # includes digit-free free-text like "n/a" (not a phone
                # CLAIM), 'unusable' (digit signal but no core) fails closed.
                # Round 16 F25: whatsapp joins the consistency set — the
                # resolver (UPSERT_MATCH_SQL) finds whatsapp-only owners, so
                # a row whose ONLY phone-ish value is whatsapp must be able
                # to prove ownership here, or the resolver arm can never
                # complete its own defining case.
                cores = set()
                for raw in (row["phone"], row["phone_normalized"], _row_get(row, "whatsapp")):
                    state, core = phone_value_state(raw)
                    if state == "absent":
                        continue
                    if state == "unusable":
                        return False
                    cores.add(core)
                return cores == {data.expected_phone_core}

            async def _sole_owner_is_target(_conn: Any) -> bool:
                # Round 14, F18: the token asserts SOLE ownership, not just
                # "the resolved row still owns the core" — a second legitimate
                # owner (allow_duplicate_phone create) appeared since the
                # resolve would make a fresh resolve ambiguous, so the upload
                # must refuse too. All cooperative phone writers serialize on
                # the phonecore advisory lock, making the authoritative in-TX
                # run of this check race-safe against them.
                rows = await _conn.fetch(CORE_OWNER_IDS_SQL, [data.expected_phone_core])
                return [r["id"] for r in rows] == [client_id]

            if data.expected_phone_core:
                # Fast pre-check (Codex round 12, F12 gap 3): the caller
                # resolved this client id BY PHONE in an earlier request; if
                # the row no longer owns that core — or no longer owns it
                # ALONE (F18) — uploading would attach the document to a
                # possibly wrong identity. Refuse before any Drive work. The
                # authoritative check re-runs under lock at INSERT.
                if not _owns_expected_core(client):
                    raise HTTPException(
                        status_code=409,
                        detail="phone_ownership_changed — re-resolve the client before upload",
                    )
                if not await _sole_owner_is_target(conn):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "phone_ownership_ambiguous — the phone core has "
                            "multiple live owners, re-resolve the client before upload"
                        ),
                    )

            company_id = await _resolve_upload_company(
                conn,
                client_id,
                data.company_id,
                data.expected_phone_core,
                lock=False,
            )

            # PR 2a: the same file (content_hash) already on this client, in
            # the same practice/family context, is refused BEFORE any Drive
            # work, so a refusal leaves no orphan. Company-tab attachments are
            # a company context and are not checked against client documents.
            existing_doc_id = None
            if company_id is None:
                existing_doc_id = await _active_duplicate_document_id(
                    conn,
                    client_id,
                    content_hash,
                    practice_id=practice_id,
                    family_member_id=data.family_member_id,
                    missing_delivery_slot=(
                        missing_delivery_slot
                        if access_already_verified is True
                        and source == "team"
                        and practice_id is not None
                        and isinstance(missing_delivery_slot, str)
                        and (data.intake_idempotency_key or "").startswith(
                            f"case-delivery:{practice_id}:0:{missing_delivery_slot}:"
                        )
                        else None
                    ),
                )
            if existing_doc_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Duplicate file: this client already has the same document "
                        f"(document id {existing_doc_id}). Nothing was uploaded."
                    ),
                )

            doc_cols: set[str] | None = None
            comp_cols: set[str] | None = None
            reused_existing_doc: Any = None
            if data.intake_idempotency_key:
                doc_cols = await _documents_columns(conn)
                if company_id is not None:
                    comp_cols = await _company_documents_columns(conn)
                if data.client_visible is False and (
                    "client_visible" not in doc_cols
                    or (comp_cols is not None and "client_visible" not in comp_cols)
                ):
                    raise HTTPException(status_code=409, detail="client_visible_column_missing")

                existing_intake_doc = await _probe_existing_client_intake_doc(
                    conn, client_id, data.intake_idempotency_key, doc_cols
                )
                if existing_intake_doc is not None:
                    if company_id is None:
                        return {
                            "success": True,
                            "document_id": int(existing_intake_doc["id"]),
                            "file_id": existing_intake_doc["file_id"],
                            "file_url": existing_intake_doc["file_url"],
                            "ocr_triggered": False,
                            "deduplicated": True,
                        }
                    existing_comp = await _probe_existing_company_intake_doc(
                        conn, company_id, data.intake_idempotency_key, comp_cols or set()
                    )
                    if existing_comp is not None:
                        return {
                            "success": True,
                            "document_id": int(existing_intake_doc["id"]),
                            "company_document_id": int(existing_comp["id"]),
                            "file_id": existing_intake_doc["file_id"],
                            "file_url": existing_intake_doc["file_url"],
                            "ocr_triggered": False,
                            "deduplicated": True,
                        }
                    reused_existing_doc = existing_intake_doc

            drive_service = ServiceAccountDriveService()
            folder_name = None
            upload_result: dict[str, Any]
            if reused_existing_doc is not None and reused_existing_doc["file_id"]:
                upload_result = {
                    "id": reused_existing_doc["file_id"],
                    "webViewLink": reused_existing_doc["file_url"],
                }
            else:
                # Ensure Root Folder Exists — MUST go through the idempotent
                # chokepoint. The old inline create raced against the POST
                # /api/clients background task (passport upload lands ~15-25s
                # after creation, while the 16 subfolders are still being built)
                # and produced a twin "{id}_{name}" root folder on every new
                # client created with a passport scan.
                root_folder_id = client["google_drive_folder_id"]
                if not root_folder_id:
                    try:
                        ensure_result = await drive_service.ensure_client_folder(
                            client_id=client["id"],
                            client_name=client["full_name"],
                            client_type=client["client_type"] or "individual",
                            db_pool=pool,
                            conn=conn,
                        )
                        root_folder_id = ensure_result["root_folder_id"]
                    except Exception as e:
                        logger.error("Failed to create root folder: %s", e)
                        raise HTTPException(
                            status_code=500,
                            detail=f"Failed to create root folder: {e}",
                        ) from e

                try:
                    root = await drive_service.get_file_metadata(root_folder_id)
                    layout = physical_root_layout(
                        root,
                        root_folder_id,
                        client["client_type"],
                        individuals_parent=settings.gdrive_individuals_folder_id,
                        companies_parent=settings.gdrive_companies_folder_id,
                    )
                    folder_name = folder_for_category(category, layout)
                    subfolder_id = await drive_service.resolve_canonical_folder(
                        folder_name, root_folder_id, conn=conn
                    )
                except Exception as e:
                    raise HTTPException(
                        status_code=409, detail="Cannot verify Drive destination"
                    ) from e

                # Handle subfolder_hint for nested subfolders (e.g. "Actual Visa" inside 01_Immigration)
                target_subfolder_id = subfolder_id

                if data.subfolder_hint and data.subfolder_hint in ("Actual Visa", "Previous Visa"):
                    try:
                        target_subfolder_id = await drive_service.resolve_canonical_folder(
                            data.subfolder_hint, subfolder_id, conn=conn
                        )
                    except Exception as e:
                        raise HTTPException(
                            status_code=409, detail="Cannot verify Drive destination"
                        ) from e

                    # Visa rotation: if uploading to "Actual Visa", move existing actual to "Previous Visa"
                    if data.subfolder_hint == "Actual Visa":
                        existing_actual = await conn.fetchrow(
                            """SELECT d.id, d.file_id FROM documents d
                               WHERE d.client_id = $1 AND d.subfolder = 'Actual Visa'
                                 AND (d.is_archived IS NOT TRUE)
                               ORDER BY d.created_at DESC LIMIT 1""",
                            client_id,
                        )
                        if existing_actual and existing_actual["file_id"]:
                            try:
                                prev_folder_id = await drive_service.resolve_canonical_folder(
                                    "Previous Visa", subfolder_id, conn=conn
                                )
                            except Exception as e:
                                logger.error(
                                    "Failed to verify Previous Visa folder: %s", type(e).__name__
                                )
                                prev_folder_id = None

                            if prev_folder_id:
                                try:
                                    await drive_service.move_file(
                                        user_email=current_user.get("email", ""),
                                        file_id=existing_actual["file_id"],
                                        new_parent_id=prev_folder_id,
                                        old_parent_id=target_subfolder_id,
                                    )
                                    await conn.execute(
                                        "UPDATE documents SET subfolder = 'Previous Visa', updated_at = NOW() WHERE id = $1",
                                        existing_actual["id"],
                                    )
                                    logger.info(
                                        f"Rotated visa doc {existing_actual['id']} to Previous Visa"
                                    )
                                except Exception as e:
                                    logger.error("Visa rotation failed: %s", e)

                # Upload File
                try:
                    upload_result = await drive_service.upload_file_to_folder(
                        folder_id=target_subfolder_id,
                        file_content=file_content,
                        file_name=data.file_name,
                        mime_type=data.mime_type,
                    )
                except Exception as e:
                    raise HTTPException(
                        status_code=500, detail=f"Failed to upload to drive: {e}"
                    ) from e

            # Create Document + optional Company Document atomically in ONE TX.
            subfolder_value_final = None
            if data.subfolder_hint and data.subfolder_hint in ("Actual Visa", "Previous Visa"):
                subfolder_value_final = data.subfolder_hint
            effective_file_id: str | None = upload_result.get("id")
            effective_file_url: str | None = upload_result.get("webViewLink")
            deduplicated_doc_in_tx = False
            deduplicated_comp_in_tx = False
            async with conn.transaction():
                if data.expected_phone_core:
                    await lock_cores(
                        conn,
                        data.expected_phone_core,
                        phone_core(client["phone"]),
                        phone_core(client["phone_normalized"]),
                    )
                    _recheck = await conn.fetchrow(
                        "SELECT phone, phone_normalized, whatsapp FROM clients"
                        " WHERE id = $1 FOR UPDATE",
                        client_id,
                    )
                    if (
                        _recheck is None
                        or not _owns_expected_core(_recheck)
                        or not await _sole_owner_is_target(conn)
                    ):
                        logger.error(
                            "orphaned_drive_file file_id=%s client_id=%s "
                            "reason=phone_ownership_changed",
                            upload_result.get("id"),
                            client_id,
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "phone_ownership_changed during upload — "
                                "document NOT attached, re-resolve the client"
                            ),
                        )
                try:
                    current_practice_id = await _resolve_upload_practice(
                        conn, client_id, data.practice_id, data.expected_phone_core, lock=True
                    )
                    if current_practice_id != practice_id:
                        raise HTTPException(status_code=409, detail="practice_identity_unresolved")
                except HTTPException:
                    logger.error(
                        "orphaned_drive_file file_id=%s client_id=%s "
                        "reason=practice_identity_unresolved",
                        upload_result.get("id"),
                        client_id,
                    )
                    raise
                if company_id is not None and (
                    isinstance(data.company_id, UUID) or data.expected_phone_core is not None
                ):
                    try:
                        current_company_id = await _resolve_upload_company(
                            conn, client_id, data.company_id, data.expected_phone_core, lock=True
                        )
                        if current_company_id != company_id:
                            raise HTTPException(
                                status_code=409, detail="company_identity_unresolved"
                            )
                    except HTTPException:
                        logger.error(
                            "orphaned_drive_file file_id=%s client_id=%s "
                            "reason=company_identity_unresolved",
                            upload_result.get("id"),
                            client_id,
                        )
                        raise

                doc_id: int | None = None
                if data.intake_idempotency_key:
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(742020, hashtext($1))",
                        f"{client_id}:{company_id or 0}:{data.intake_idempotency_key}",
                    )
                    if doc_cols is None:
                        doc_cols = await _documents_columns(conn)
                    existing_tx_doc = await _probe_existing_client_intake_doc(
                        conn, client_id, data.intake_idempotency_key, doc_cols
                    )
                    if existing_tx_doc is not None:
                        doc_id = int(existing_tx_doc["id"])
                        deduplicated_doc_in_tx = True
                        effective_file_id = existing_tx_doc["file_id"]
                        effective_file_url = existing_tx_doc["file_url"]
                        if (
                            reused_existing_doc is None
                            and upload_result.get("id")
                            and upload_result.get("id") != effective_file_id
                        ):
                            logger.warning(
                                "orphaned_drive_file file_id=%s client_id=%s "
                                "reason=concurrent_intake_dedup",
                                upload_result.get("id"),
                                client_id,
                            )

                if doc_id is None:
                    if not data.intake_idempotency_key:
                        insert_sql = """
                            INSERT INTO documents (
                                client_id, document_type, document_category,
                                file_name, file_id, file_url, google_drive_file_url,
                                status, storage_type, notes, subfolder, expiry_date, content_hash,
                                family_member_id, practice_id, uploaded_by, uploaded_source
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', 'google_drive', $8, $9, $10, $11, $12, $13, $14, $15)
                            RETURNING id
                            """
                        insert_args: list[Any] = [
                            client_id,
                            data.document_type,
                            category,
                            data.file_name,
                            upload_result["id"],
                            upload_result.get("webViewLink"),
                            upload_result.get("webViewLink"),
                            data.notes,
                            subfolder_value_final,
                            _parse_date_or_none(data.expiry_date),
                            content_hash,
                            data.family_member_id,
                            practice_id,
                            current_user.get("email"),
                            source,
                        ]
                        if data.client_visible is not None:
                            insert_sql = """
                            INSERT INTO documents (
                                client_id, document_type, document_category,
                                file_name, file_id, file_url, google_drive_file_url,
                                status, storage_type, notes, subfolder, expiry_date, content_hash,
                                family_member_id, practice_id, uploaded_by, uploaded_source,
                                client_visible
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', 'google_drive', $8, $9, $10, $11, $12, $13, $14, $15, $16)
                            RETURNING id
                            """
                            insert_args.append(data.client_visible)
                        doc_id = await conn.fetchval(insert_sql, *insert_args)
                    else:
                        doc_has_idem = "intake_idempotency_key" in (doc_cols or set())
                        doc_notes = (
                            data.notes
                            if doc_has_idem
                            else _append_intake_marker(data.notes, data.intake_idempotency_key)
                        )
                        doc_col_values: list[tuple[str, Any]] = [
                            ("client_id", client_id),
                            ("document_type", data.document_type),
                            ("document_category", category),
                            ("file_name", data.file_name),
                            ("file_id", upload_result["id"]),
                            ("file_url", upload_result.get("webViewLink")),
                            ("google_drive_file_url", upload_result.get("webViewLink")),
                            ("status", "active"),
                            ("storage_type", "google_drive"),
                            ("notes", doc_notes),
                            ("subfolder", subfolder_value_final),
                            ("expiry_date", _parse_date_or_none(data.expiry_date)),
                            ("content_hash", content_hash),
                            ("family_member_id", data.family_member_id),
                            ("practice_id", practice_id),
                            ("uploaded_by", current_user.get("email")),
                            ("uploaded_source", source),
                        ]
                        if data.client_visible is not None:
                            doc_col_values.append(("client_visible", data.client_visible))
                        if doc_has_idem:
                            doc_col_values.append(
                                ("intake_idempotency_key", data.intake_idempotency_key)
                            )
                        d_col_sql = ", ".join(name for name, _ in doc_col_values)
                        d_ph_sql = ", ".join(f"${i}" for i in range(1, len(doc_col_values) + 1))
                        d_args = [val for _, val in doc_col_values]
                        doc_id = await conn.fetchval(
                            f"INSERT INTO documents ({d_col_sql}) VALUES ({d_ph_sql}) RETURNING id",
                            *d_args,
                        )

                company_document_id = None
                if company_id is not None:
                    comp_drive_file_id = effective_file_id or upload_result["id"]
                    comp_drive_file_url = (
                        effective_file_url
                        if effective_file_id
                        else upload_result.get("webViewLink")
                    )
                    size_raw = upload_result.get("size")
                    file_size_kb = None
                    if size_raw:
                        try:
                            file_size_kb = max(1, int(size_raw) // 1024)
                        except (TypeError, ValueError):
                            file_size_kb = None

                    if data.client_visible is None and data.intake_idempotency_key is None:
                        company_document_id = await conn.fetchval(
                            """
                            SELECT id
                            FROM company_documents
                            WHERE company_id = $1 AND google_drive_file_id = $2
                            """,
                            company_id,
                            comp_drive_file_id,
                        )
                        if company_document_id is None:
                            try:
                                company_document_id = await conn.fetchval(
                                    """
                                    INSERT INTO company_documents (
                                        company_id, document_type, google_drive_file_id,
                                        google_drive_file_url, file_name, file_size_kb,
                                        mime_type, notes, uploaded_by, status, is_verified,
                                        created_at, updated_at
                                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'active', false, now(), now())
                                    RETURNING id
                                    """,
                                    company_id,
                                    data.document_type,
                                    comp_drive_file_id,
                                    comp_drive_file_url,
                                    data.file_name,
                                    file_size_kb,
                                    data.mime_type,
                                    data.notes,
                                    current_user.get("email", "system"),
                                )
                            except Exception:
                                logger.error(
                                    "orphaned_drive_file file_id=%s client_id=%s "
                                    "reason=company_document_insert_failed",
                                    comp_drive_file_id,
                                    client_id,
                                )
                                raise
                    else:
                        if comp_cols is None:
                            comp_cols = await _company_documents_columns(conn)
                        if data.client_visible is False and "client_visible" not in comp_cols:
                            raise HTTPException(
                                status_code=409, detail="client_visible_column_missing"
                            )
                        comp_has_idem = "intake_idempotency_key" in comp_cols
                        if data.intake_idempotency_key is not None:
                            existing_comp_row = await _probe_existing_company_intake_doc(
                                conn, company_id, data.intake_idempotency_key, comp_cols
                            )
                            if existing_comp_row is not None:
                                company_document_id = int(existing_comp_row["id"])
                                deduplicated_comp_in_tx = True
                        if company_document_id is None:
                            company_document_id = await conn.fetchval(
                                """
                                SELECT id
                                FROM company_documents
                                WHERE company_id = $1 AND google_drive_file_id = $2
                                """,
                                company_id,
                                comp_drive_file_id,
                            )
                        if company_document_id is None:
                            comp_notes = (
                                _append_intake_marker(data.notes, data.intake_idempotency_key)
                                if (data.intake_idempotency_key is not None and not comp_has_idem)
                                else data.notes
                            )
                            col_values: list[tuple[str, Any]] = [
                                ("company_id", company_id),
                                ("document_type", data.document_type),
                                ("google_drive_file_id", comp_drive_file_id),
                                ("google_drive_file_url", comp_drive_file_url),
                                ("file_name", data.file_name),
                                ("file_size_kb", file_size_kb),
                                ("mime_type", data.mime_type),
                                ("notes", comp_notes),
                                ("uploaded_by", current_user.get("email", "system")),
                                ("status", "active"),
                                ("is_verified", False),
                            ]
                            if "client_visible" in comp_cols and data.client_visible is not None:
                                col_values.append(("client_visible", bool(data.client_visible)))
                            if comp_has_idem and data.intake_idempotency_key is not None:
                                col_values.append(
                                    ("intake_idempotency_key", data.intake_idempotency_key)
                                )
                            col_parts = [name for name, _ in col_values]
                            ph_parts = [f"${i}" for i in range(1, len(col_values) + 1)]
                            if "created_at" in comp_cols:
                                col_parts.append("created_at")
                                ph_parts.append("now()")
                            if "updated_at" in comp_cols:
                                col_parts.append("updated_at")
                                ph_parts.append("now()")
                            col_sql = ", ".join(col_parts)
                            ph_sql = ", ".join(ph_parts)
                            args = [val for _, val in col_values]
                            try:
                                company_document_id = await conn.fetchval(
                                    f"INSERT INTO company_documents ({col_sql}) VALUES ({ph_sql}) RETURNING id",
                                    *args,
                                )
                            except Exception:
                                logger.error(
                                    "orphaned_drive_file file_id=%s client_id=%s "
                                    "reason=company_document_insert_failed",
                                    comp_drive_file_id,
                                    client_id,
                                )
                                raise

            if deduplicated_doc_in_tx and (company_id is None or deduplicated_comp_in_tx):
                dedup_resp: dict[str, Any] = {
                    "success": True,
                    "document_id": doc_id,
                    "file_id": effective_file_id,
                    "file_url": effective_file_url,
                    "ocr_triggered": False,
                    "deduplicated": True,
                }
                if company_id is not None:
                    dedup_resp["company_document_id"] = company_document_id
                return dedup_resp

            # Trigger OCR via dispatcher only for newly inserted client document rows.
            if not deduplicated_doc_in_tx:
                if folder_name is not None and effective_file_id is not None:
                    background_tasks.add_task(
                        _dispatch_ocr_by_folder,
                        pool,
                        client_id,
                        effective_file_id,
                        folder_name,
                        data.file_name,
                        doc_id,
                        data.document_type,
                    )
                await conn.execute(
                    "UPDATE documents SET ocr_status = 'pending' WHERE id = $1",
                    doc_id,
                )

            await invalidate_cache("zantara:crm_clients_stats:*")
            return {
                "success": True,
                "document_id": doc_id,
                "file_id": effective_file_id,
                "file_url": effective_file_url,
                "ocr_triggered": not deduplicated_doc_in_tx,
                "company_document_id": company_document_id,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Upload failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/internal/clients/{client_id}/documents/upload", responses=_UPLOAD_CONFLICT)
async def upload_document_base64_internal(
    client_id: int,
    data: DocumentUploadBase64 = Body(...),
    pool: Any = Depends(get_database_pool),
    actor: str = Depends(verify_crm_write_key),
    background_tasks: BackgroundTasks = BackgroundTasks(),
) -> dict[str, Any]:
    """Service-to-service intake upload path.

    Authenticated by ``X-CRM-Write-Key`` and hard-gated by
    ``WA_MIRROR_CRM_WRITE_ENABLED``. The actual upload implementation is the same
    path used by the frontend/manual reviewer; only the RBAC pre-check is skipped
    because Pro-side intake already resolved and gate-checked the target client.
    """
    return await upload_document_base64(
        client_id=client_id,
        data=data,
        pool=pool,
        current_user={
            "email": actor,
            "user_id": actor,
            "role": "service",
            "permissions": ["crm:write"],
        },
        background_tasks=background_tasks,
        access_already_verified=True,
    )


class CompanyDocumentUploadBase64(BaseModel):
    file: str  # Base64
    file_name: str
    document_type: str
    document_category: str | None = None
    document_number: str | None = None
    mime_type: str | None = None
    expiry_date: str | None = None
    notes: str | None = None
    client_visible: bool | None = None
    intake_idempotency_key: str | None = None


def _parse_company_ref(company_ref: str) -> int | UUID:
    if re.fullmatch(r"[0-9]{1,18}", company_ref):
        return int(company_ref)
    if re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        company_ref,
    ):
        return UUID(company_ref)
    raise HTTPException(
        status_code=422,
        detail="company_ref must be an integer ID or canonical hyphenated UUID",
    )


async def _upload_company_document_impl(
    *,
    company_ref: int | UUID,
    data: CompanyDocumentUploadBase64,
    pool: Any,
    current_user: dict[str, Any],
    require_uuid_identity: bool = False,
    access_already_verified: bool = False,
) -> dict[str, Any]:
    """Upload a standalone company document to Drive and ``company_documents``."""
    try:
        file_content = base64.b64decode(data.file)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 file content")

    if data.document_category:
        category = data.document_category
    else:
        cat_result = auto_categorize_document(data.file_name)
        category = cat_result["document_category"]

    async with pool.acquire() as conn:
        col = "uuid" if isinstance(company_ref, UUID) else "id"
        param: Any = str(company_ref) if isinstance(company_ref, UUID) else company_ref
        company = await conn.fetchrow(
            f"SELECT id, uuid, company_name, google_drive_folder_id, status "
            f"FROM companies WHERE {col} = $1",
            param,
        )
        if not company or str(company["status"] or "").lower() == "deleted":
            if require_uuid_identity:
                raise HTTPException(status_code=409, detail="company_identity_unresolved")
            raise HTTPException(status_code=404, detail="Company not found")

        company_id = int(company["id"])
        if access_already_verified is not True and not is_crm_admin(current_user):
            links = await conn.fetch(
                "SELECT client_id FROM client_company_links WHERE company_id = $1",
                company_id,
            )
            authorized = False
            for link in links:
                try:
                    await verify_client_access(
                        int(link["client_id"]),
                        current_user,
                        conn,
                        allow_assigned=True,
                        write=True,
                    )
                    authorized = True
                    break
                except HTTPException:
                    continue
            if not authorized:
                raise HTTPException(status_code=403, detail="Access denied to company")

        comp_cols = await _company_documents_columns(conn)
        if data.client_visible is False and "client_visible" not in comp_cols:
            raise HTTPException(status_code=409, detail="client_visible_column_missing")

        if data.intake_idempotency_key:
            existing = await _probe_existing_company_intake_doc(
                conn, company_id, data.intake_idempotency_key, comp_cols
            )
            if existing is not None:
                return {
                    "success": True,
                    "company_document_id": int(existing["id"]),
                    "company_id": company_id,
                    "file_id": existing["google_drive_file_id"],
                    "file_url": existing["google_drive_file_url"],
                    "deduplicated": True,
                }

        drive_service = ServiceAccountDriveService()
        root_folder_id = company["google_drive_folder_id"]
        if not root_folder_id:
            try:
                ensure_res = await drive_service.ensure_company_folder(
                    company_id=company_id,
                    company_name=company["company_name"],
                    db_pool=pool,
                    conn=conn,
                )
                root_folder_id = ensure_res["root_folder_id"]
            except Exception as exc:
                logger.error("Failed to create company root folder: %s", exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create company root folder: {exc}",
                ) from exc

        folder_name = folder_for_category(category, "company")
        try:
            target_subfolder_id = await drive_service.resolve_canonical_folder(
                folder_name, root_folder_id, conn=conn
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail="Cannot verify Drive destination") from exc

        try:
            upload_result = await drive_service.upload_file_to_folder(
                folder_id=target_subfolder_id,
                file_content=file_content,
                file_name=data.file_name,
                mime_type=data.mime_type,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to upload to drive: {exc}"
            ) from exc

        size_raw = upload_result.get("size")
        file_size_kb = None
        if size_raw:
            try:
                file_size_kb = max(1, int(size_raw) // 1024)
            except (TypeError, ValueError):
                file_size_kb = None

        effective_file_id: str | None = upload_result.get("id")
        effective_file_url: str | None = upload_result.get("webViewLink")
        deduplicated_in_tx = False
        async with conn.transaction():
            recheck = await conn.fetchrow(
                "SELECT id, status FROM companies WHERE id = $1 FOR SHARE",
                company_id,
            )
            if recheck is None or str(recheck["status"] or "").lower() == "deleted":
                logger.error(
                    "orphaned_drive_file file_id=%s company_id=%s reason=company_identity_unresolved",
                    upload_result.get("id"),
                    company_id,
                )
                raise HTTPException(status_code=409, detail="company_identity_unresolved")

            comp_has_idem = "intake_idempotency_key" in comp_cols
            company_doc_id: int | None = None
            if data.intake_idempotency_key:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(742019, hashtext($1))",
                    f"{company_id}:{data.intake_idempotency_key}",
                )
                existing_tx = await _probe_existing_company_intake_doc(
                    conn, company_id, data.intake_idempotency_key, comp_cols
                )
                if existing_tx is not None:
                    company_doc_id = int(existing_tx["id"])
                    deduplicated_in_tx = True
                    effective_file_id = existing_tx["google_drive_file_id"]
                    effective_file_url = existing_tx["google_drive_file_url"]
                    if upload_result.get("id") and upload_result.get("id") != effective_file_id:
                        logger.warning(
                            "orphaned_drive_file file_id=%s company_id=%s "
                            "reason=concurrent_intake_dedup",
                            upload_result.get("id"),
                            company_id,
                        )

            if company_doc_id is None:
                company_doc_id = await conn.fetchval(
                    """
                    SELECT id FROM company_documents
                    WHERE company_id = $1 AND google_drive_file_id = $2
                    """,
                    company_id,
                    upload_result["id"],
                )
            if company_doc_id is None:
                comp_notes = (
                    _append_intake_marker(data.notes, data.intake_idempotency_key)
                    if (data.intake_idempotency_key and not comp_has_idem)
                    else data.notes
                )
                col_values: list[tuple[str, Any]] = [
                    ("company_id", company_id),
                    ("document_type", data.document_type),
                    ("google_drive_file_id", upload_result["id"]),
                    ("google_drive_file_url", upload_result.get("webViewLink")),
                    ("file_name", data.file_name),
                    ("file_size_kb", file_size_kb),
                    ("mime_type", data.mime_type),
                    ("notes", comp_notes),
                    ("uploaded_by", current_user.get("email", "system")),
                    ("status", "active"),
                    ("is_verified", False),
                ]
                if "document_subtype" in comp_cols and data.document_category is not None:
                    col_values.append(("document_subtype", category))
                if "document_number" in comp_cols and data.document_number is not None:
                    col_values.append(("document_number", data.document_number))
                if "expiry_date" in comp_cols and data.expiry_date is not None:
                    col_values.append(("expiry_date", _parse_date_or_none(data.expiry_date)))
                if "client_visible" in comp_cols and data.client_visible is not None:
                    col_values.append(("client_visible", bool(data.client_visible)))
                if comp_has_idem and data.intake_idempotency_key:
                    col_values.append(("intake_idempotency_key", data.intake_idempotency_key))

                col_parts = [name for name, _ in col_values]
                ph_parts = [f"${i}" for i in range(1, len(col_values) + 1)]
                if "created_at" in comp_cols:
                    col_parts.append("created_at")
                    ph_parts.append("now()")
                if "updated_at" in comp_cols:
                    col_parts.append("updated_at")
                    ph_parts.append("now()")
                col_sql = ", ".join(col_parts)
                ph_sql = ", ".join(ph_parts)
                args = [val for _, val in col_values]
                company_doc_id = await conn.fetchval(
                    f"INSERT INTO company_documents ({col_sql}) VALUES ({ph_sql}) RETURNING id",
                    *args,
                )

    if deduplicated_in_tx:
        return {
            "success": True,
            "company_document_id": int(company_doc_id),
            "company_id": company_id,
            "file_id": effective_file_id,
            "file_url": effective_file_url,
            "deduplicated": True,
        }

    await invalidate_cache("zantara:crm_clients_stats:*")
    return {
        "success": True,
        "company_document_id": int(company_doc_id),
        "company_id": company_id,
        "file_id": effective_file_id,
        "file_url": effective_file_url,
    }


@router.post("/companies/{company_ref}/documents/upload", responses=_UPLOAD_CONFLICT)
async def upload_company_document_base64(
    company_ref: str,
    data: CompanyDocumentUploadBase64 = Body(...),
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Upload a standalone document directly to a company workspace."""
    parsed_ref = _parse_company_ref(company_ref)
    return await _upload_company_document_impl(
        company_ref=parsed_ref,
        data=data,
        pool=pool,
        current_user=current_user,
        require_uuid_identity=isinstance(parsed_ref, UUID),
        access_already_verified=False,
    )


@router.post("/internal/companies/{company_uuid}/documents/upload", responses=_UPLOAD_CONFLICT)
async def upload_company_document_base64_internal(
    company_uuid: str,
    data: CompanyDocumentUploadBase64 = Body(...),
    pool: Any = Depends(get_database_pool),
    actor: str = Depends(verify_crm_write_key),
) -> dict[str, Any]:
    """Service-to-service standalone company intake upload keyed by canonical UUID."""
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        company_uuid,
    ):
        raise HTTPException(
            status_code=422,
            detail="company_uuid must be a canonical hyphenated UUID",
        )
    return await _upload_company_document_impl(
        company_ref=UUID(company_uuid),
        data=data,
        pool=pool,
        current_user={
            "email": actor,
            "user_id": actor,
            "role": "service",
            "permissions": ["crm:write"],
        },
        require_uuid_identity=True,
        access_already_verified=True,
    )


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: int,
    permanent: bool = False,
    pool: Any = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Delete a document (Soft delete by default).
    """
    async with pool.acquire() as conn:
        # Lookup client_id from document for RBAC check
        doc_row = await conn.fetchrow("SELECT client_id FROM documents WHERE id = $1", doc_id)
        if doc_row:
            await verify_client_access(
                doc_row["client_id"], current_user, conn, allow_assigned=True, write=True
            )
        if permanent:
            result = await conn.execute("DELETE FROM documents WHERE id = $1", doc_id)
        else:
            result = await conn.execute(
                "UPDATE documents SET is_archived = true, updated_at = NOW() WHERE id = $1",
                doc_id,
            )

        if result == "DELETE 0" or result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="Document not found")

    await invalidate_cache("zantara:crm_clients_stats:*")
    return {"success": True, "action": "deleted" if permanent else "archived"}
