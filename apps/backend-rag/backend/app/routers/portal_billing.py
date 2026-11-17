"""
Portal Billing Router.

Exposes invoice data to client portal via PortalService.
"""

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from backend.app.dependencies import get_database_pool
from backend.app.routers.portal import get_current_client
from backend.app.utils.logging_utils import get_logger, sanitize_for_log
from backend.services.portal.upload_validation import (
    PORTAL_UPLOAD_MIME_TYPES,
    DuplicateDocumentError,
    read_upload_bounded,
    validate_upload_type,
)

if TYPE_CHECKING:
    from backend.services.portal import PortalService
else:
    PortalService = Any

logger = get_logger(__name__)

router = APIRouter(prefix="/api/portal/billing", tags=["portal-billing"])


def _get_portal_service(db_pool: asyncpg.Pool = Depends(get_database_pool)) -> PortalService:
    from backend.services.portal import PortalService as _PortalService

    return _PortalService(db_pool)


@router.get("")
async def get_billing(
    client: dict = Depends(get_current_client),
    portal_service: PortalService = Depends(_get_portal_service),
) -> dict[str, Any]:
    """Get all invoices and billing summary for the authenticated client."""
    try:
        result = await portal_service.get_billing(
            client["client_id"],
            current_user=client,
        )
        return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Failed to get billing for client {client['client_id']}: {e}")
        raise HTTPException(status_code=500, detail="Failed to load billing data")


@router.get("/{invoice_id}/pdf-url")
async def get_invoice_pdf_url(
    invoice_id: int,
    client: dict = Depends(get_current_client),
    portal_service: PortalService = Depends(_get_portal_service),
) -> dict[str, Any]:
    """Get the client-safe portal proxy URL for an invoice PDF."""
    try:
        result = await portal_service.get_invoice_pdf_url(
            client["client_id"],
            invoice_id,
            current_user=client,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="Invoice not found or PDF not available")
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as error:
        logger.error(
            "Failed to resolve invoice PDF %s for client %s: %s",
            invoice_id,
            client["client_id"],
            error,
        )
        raise HTTPException(status_code=500, detail="Failed to load invoice PDF") from error


@router.get("/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: int,
    client: dict = Depends(get_current_client),
    portal_service: PortalService = Depends(_get_portal_service),
) -> Response:
    """Download an invoice PDF through the portal proxy."""
    try:
        invoice = await portal_service.download_invoice_pdf(
            client["client_id"],
            invoice_id,
            current_user=client,
        )
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found or PDF not available")

        file_name = invoice["file_name"]
        return Response(
            content=invoice["content"],
            media_type=invoice["mime_type"],
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(file_name)}",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Failed to download invoice PDF {invoice_id} for client {client['client_id']}: {e}"
        )
        raise HTTPException(status_code=500, detail="Failed to download invoice PDF") from e


@router.post("/{invoice_id}/payment-proof")
async def upload_payment_proof(
    invoice_id: int,
    file: UploadFile = File(...),
    client: dict = Depends(get_current_client),
    portal_service: PortalService = Depends(_get_portal_service),
) -> dict[str, Any]:
    """Upload a payment proof file for an owned client invoice."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    file_ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if file_ext not in PORTAL_UPLOAD_MIME_TYPES:
        allowed_extensions = ", ".join(PORTAL_UPLOAD_MIME_TYPES)
        raise HTTPException(
            status_code=400,
            detail=f"File type not allowed. Allowed types: {allowed_extensions}",
        )

    try:
        content = await read_upload_bounded(file)
    except ValueError as exc:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum size is 10MB",
        ) from exc

    if not content:
        raise HTTPException(status_code=400, detail="File is empty")

    try:
        trusted_mime_type = validate_upload_type(
            file_ext=file_ext,
            declared_mime_type=file.content_type,
            content=content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    try:
        async with portal_service.pool.acquire() as conn:
            invoice_row = await conn.fetchrow(
                """
                SELECT id, invoice_number, practice_id
                FROM invoices
                WHERE id = $1 AND client_id = $2
                """,
                invoice_id,
                client["client_id"],
            )
        if not invoice_row:
            raise HTTPException(status_code=404, detail="Invoice not found")

        invoice_number = invoice_row["invoice_number"] or f"invoice #{invoice_id}"
        document = await portal_service.upload_document(
            client_id=client["client_id"],
            file_content=content,
            file_name=file.filename,
            document_type="payment_proof",
            mime_type=trusted_mime_type,
            practice_id=invoice_row["practice_id"],
            document_purpose=f"Payment proof for {invoice_number}",
            current_user=client,
        )
        created_at = (
            str(document["created_at"])
            if isinstance(document, dict) and document.get("created_at")
            else datetime.now(timezone.utc).isoformat()
        )
        proof_id = (
            int(document["id"])
            if isinstance(document, dict) and document.get("id") is not None
            else invoice_id
        )
        return {
            "success": True,
            "message": "Payment proof uploaded",
            "data": {
                "id": proof_id,
                "status": "received",
                "note": None,
                "created_at": created_at,
            },
        }
    except HTTPException:
        raise
    except DuplicateDocumentError as exc:
        logger.info(
            "Duplicate payment proof rejected for invoice %s client %s: %s",
            sanitize_for_log(invoice_id),
            sanitize_for_log(client["client_id"]),
            sanitize_for_log(exc),
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "A file with this name was uploaded less than an hour ago. "
                "Rename the file or wait before uploading it again."
            ),
        ) from exc
    except ValueError as exc:
        msg = str(exc)
        msg_lower = msg.lower()
        if "client" in msg_lower and "not found" in msg_lower:
            raise HTTPException(status_code=404, detail="Client not found") from exc
        if "rate limit" in msg_lower:
            raise HTTPException(status_code=429, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except Exception as exc:
        logger.error(
            "Failed to upload payment proof for invoice %s client %s: %s",
            sanitize_for_log(invoice_id),
            sanitize_for_log(client["client_id"]),
            sanitize_for_log(exc),
        )
        raise HTTPException(status_code=500, detail="Failed to upload payment proof") from exc
