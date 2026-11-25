"""
Company Router - API endpoints for Company-Centric CRM
Uses asyncpg like other CRM routers
"""

import asyncio
import logging
import os
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.modules.crm.company_drive_sources import router as drive_sources_router
from backend.app.modules.crm.company_schemas import (
    ClientCompanyLinkCreate,
    CompanyCreate,
    CompanyDocumentCreate,
    CompanyUpdate,
)
from backend.app.routers.documents_proxy import assert_drive_file_belongs_to_company
from backend.app.utils.client_pools import can_edit_company, ensure_client_pools_validated
from backend.app.utils.crm_utils import can_create_company, is_crm_admin, verify_client_access
from backend.services.common.background import spawn
from backend.services.documents.drive_address import address_drive_ids
from backend.services.crm.entity_search import (
    build_company_search,
    escape_like,
    smart_search_available,
)
from backend.services.invoicing.invoice_company import link_lock_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crm/companies", tags=["CRM Companies"])
router.include_router(drive_sources_router)


# ========== HELPER FUNCTIONS ==========


def _require_crm_admin(current_user: dict) -> None:
    """Require CRM admin privileges for company-level mutations."""
    if not is_crm_admin(current_user):
        raise HTTPException(status_code=403, detail="CRM admin access required")


# companies.nib and companies.npwp_company are unique. The constraint is named
# companies_<col>_key (plain UNIQUE) or ix_companies_<col> (SQLModel unique index),
# so it is matched on the column stem. NPWP first: "nib" is not a substring of it.
_COMPANY_IDENTIFIERS = (
    (
        "npwp",
        "npwp_company",
        "NPWP",
        "SELECT id FROM companies WHERE npwp_company = $1 ORDER BY id LIMIT 1",
    ),
    ("nib", "nib", "NIB", "SELECT id FROM companies WHERE nib = $1 ORDER BY id LIMIT 1"),
)


def _identifier_in_violation(exc: asyncpg.UniqueViolationError) -> tuple[str, str, str] | None:
    """(column, label, owner query) of the NIB/NPWP unique key the insert hit, else None."""
    constraint = (getattr(exc, "constraint_name", None) or "").lower()
    for stem, column, label, owner_sql in _COMPANY_IDENTIFIERS:
        if stem in constraint:
            return column, label, owner_sql
    return None


async def _require_company_editor(conn, company_id: int, current_user: dict, action: str) -> None:
    """403/404 unless the caller may change this company (``client_pools.can_edit_company``)."""
    await ensure_client_pools_validated(conn)
    if is_crm_admin(current_user):
        return
    company = await conn.fetchrow("SELECT created_by FROM companies WHERE id = $1", company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    owners = await conn.fetch(
        "SELECT c.assigned_to, c.created_by, c.tax_consultant "
        "FROM client_company_links l JOIN clients c ON c.id = l.client_id "
        "WHERE l.company_id = $1 AND c.deleted_at IS NULL",
        company_id,
    )
    if not can_edit_company(current_user, company["created_by"], owners):
        raise HTTPException(status_code=403, detail=f"Not allowed to {action} this company")


def company_record_to_dict(record: asyncpg.Record) -> dict:
    """Convert asyncpg record to dict for Company"""
    return {
        "id": record["id"],
        "uuid": record["uuid"],
        "company_name": record["company_name"],
        "company_type": record["company_type"],
        "brand_name": record["brand_name"],
        "kbli_code": record["kbli_code"],
        "kbli_description": record["kbli_description"],
        "nib": record["nib"],
        "npwp_company": record["npwp_company"],
        "akta_pendirian_no": record["akta_pendirian_no"],
        "akta_pendirian_date": record["akta_pendirian_date"],
        "akta_perubahan_no": record["akta_perubahan_no"],
        "akta_perubahan_date": record["akta_perubahan_date"],
        "sk_menhumkam_no": record["sk_menhumkam_no"],
        "sk_menhumkam_date": record["sk_menhumkam_date"],
        "registered_address": record["registered_address"],
        "office_address": record["office_address"],
        "city": record["city"],
        "province": record["province"],
        "postal_code": record["postal_code"],
        "company_phone": record["company_phone"],
        "company_email": record["company_email"],
        "status": record["status"],
        "setup_progress": record["setup_progress"],
        "google_drive_folder_id": record["google_drive_folder_id"],
        "custom_fields": record["custom_fields"],
        "created_at": record["created_at"].isoformat() if record["created_at"] else None,
        "updated_at": record["updated_at"].isoformat() if record["updated_at"] else None,
        "created_by": record["created_by"],
    }


# ========== COMPANY ENDPOINTS ==========


@router.get("", response_model=list[dict[str, Any]])
async def list_companies(
    request: Request,
    search: str | None = Query(None, description="Search by name, NIB, or NPWP"),
    status: str | None = Query(None, description="Filter by status"),
    kbli: str | None = Query(None, description="Filter by KBLI code"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all companies with optional filters"""
    query = """
        SELECT c.*, COUNT(ccl.id) AS associates_count
        FROM companies c
        LEFT JOIN client_company_links ccl ON ccl.company_id = c.id
        WHERE 1=1
    """
    params: list = []

    async with db.acquire() as conn:
        if search and search.strip():
            # Smart search (case, accents, word order, PT/CV affixes, NIB/NPWP digits); the
            # legacy substring search answers when every token is short or migration 358 is absent.
            smart = (
                await build_company_search(conn, search, 1)
                if await smart_search_available(conn)
                else None
            )
            if smart is not None:
                query += smart.clause
                params.extend(smart.params)
            else:
                query += (
                    " AND (c.company_name ILIKE $1 OR c.nib ILIKE $1 OR c.npwp_company ILIKE $1)"
                )
                params.append(f"%{escape_like(search.strip())}%")

        if status:
            query += f" AND c.status = ${len(params) + 1}"
            params.append(status)

        if kbli:
            query += f" AND c.kbli_code = ${len(params) + 1}"
            params.append(kbli)

        query += (
            f" GROUP BY c.id ORDER BY c.company_name"
            f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}"
        )
        params.extend([limit, skip])

        rows = await conn.fetch(query, *params)

        companies = []
        for row in rows:
            comp = company_record_to_dict(row)
            comp["associates_count"] = row["associates_count"]
            companies.append(comp)

        return companies


@router.post("", response_model=dict[str, Any])
async def create_company(
    request: Request,
    data: CompanyCreate,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new company"""
    # CRM admins and the tax team; edit/delete stay admin-only.
    if not can_create_company(current_user):
        raise HTTPException(status_code=403, detail="Company creation not allowed for this role")
    user_email = current_user.get("email", "system")

    query = """
        INSERT INTO companies (
            company_name, company_type, kbli_code, nib, npwp_company,
            registered_address, city, province, company_email, company_phone,
            created_by, updated_by
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
        RETURNING id, uuid, company_name, company_type, status
    """

    async with db.acquire() as conn:
        try:
            row = await conn.fetchrow(
                query,
                data.company_name,
                data.company_type,
                data.kbli_code,
                data.nib,
                data.npwp_company,
                data.registered_address,
                data.city,
                data.province,
                data.company_email,
                data.company_phone,
                user_email,
            )
        except asyncpg.UniqueViolationError as exc:
            identifier = _identifier_in_violation(exc)
            if identifier is None:
                raise
            column, label, owner_sql = identifier
            value = getattr(data, column)
            owner_id = await conn.fetchval(owner_sql, value) if value else None
            # Column and owner id only: the NIB/NPWP value and the name stay out of the log.
            logger.info("create_company: %s already in use by company id=%s", column, owner_id)
            owner = f"company #{owner_id}" if owner_id is not None else "another company"
            raise HTTPException(
                status_code=409,
                detail={
                    "error": f"duplicate_{column}",
                    "field": column,
                    "message": (
                        f"This {label} is already used by {owner}. "
                        "Open that company instead of creating a new one."
                    ),
                    "existing_company_id": owner_id,
                },
            ) from None
        except asyncpg.StringDataRightTruncationError:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "value_too_long",
                    "message": "One of the values is longer than the field allows.",
                },
            ) from None

        return {
            "id": row["id"],
            "uuid": row["uuid"],
            "company_name": row["company_name"],
            "company_type": row["company_type"],
            "status": row["status"],
            "message": "Company created successfully",
        }


@router.get("/by-name", response_model=dict[str, Any] | None)
async def get_company_by_name_early(
    request: Request,
    name: str = Query(..., description="Company name to search for"),
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any] | None:
    """Find a company by name (ILIKE match) — must be before /{company_id} route"""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM companies WHERE company_name ILIKE $1 ORDER BY id LIMIT 1",
            f"%{name}%",
        )
        if not row:
            return None

        company = company_record_to_dict(row)

        associates = await conn.fetch(
            """
            SELECT ccl.id as link_id, ccl.client_id, c.full_name as client_name,
                   ccl.role, ccl.is_primary, ccl.ownership_percentage,
                   ccl.shares_count, ccl.share_nominal_value, ccl.start_date, ccl.status
            FROM client_company_links ccl
            JOIN clients c ON ccl.client_id = c.id
            WHERE ccl.company_id = $1
              AND c.deleted_at IS NULL
            """,
            row["id"],
        )

        company["associates"] = [
            {
                "link_id": a["link_id"],
                "client_id": a["client_id"],
                "client_name": a["client_name"],
                "role": a["role"],
                "is_primary": a["is_primary"],
                "ownership_percentage": a["ownership_percentage"],
                "shares_count": a["shares_count"],
                "share_nominal_value": a["share_nominal_value"],
                "start_date": a["start_date"].isoformat() if a["start_date"] else None,
                "status": a["status"],
            }
            for a in associates
        ]

        return company


@router.get("/by-client/{client_id}", response_model=list[dict[str, Any]])
async def get_client_companies_early(
    request: Request,
    client_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Get all companies linked to a client — must be before /{company_id} route"""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.*, ccl.id as link_id, ccl.role, ccl.is_primary,
                   ccl.ownership_percentage, ccl.shares_count,
                   ccl.share_nominal_value, ccl.start_date as link_start_date,
                   ccl.status as link_status
            FROM client_company_links ccl
            JOIN companies c ON ccl.company_id = c.id
            WHERE ccl.client_id = $1
            ORDER BY COALESCE(ccl.is_primary, FALSE) DESC, ccl.id
            """,
            client_id,
        )

        results = []
        for r in rows:
            comp = company_record_to_dict(r)
            comp["company_id"] = comp.pop("id")
            comp["company_status"] = comp.pop("status")
            comp["link_id"] = r["link_id"]
            comp["role"] = r["role"]
            comp["is_primary"] = r["is_primary"]
            comp["ownership_percentage"] = r["ownership_percentage"]
            comp["shares_count"] = r["shares_count"]
            comp["share_nominal_value"] = r["share_nominal_value"]
            comp["start_date"] = r["link_start_date"].isoformat() if r["link_start_date"] else None
            comp["link_status"] = r["link_status"]
            results.append(comp)

        return results


@router.get("/{company_id}", response_model=dict[str, Any])
async def get_company(
    request: Request,
    company_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Get company details with associates"""
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM companies WHERE id = $1", company_id)
        if not row:
            raise HTTPException(status_code=404, detail="Company not found")

        company = company_record_to_dict(row)

        # Get associates
        associates = await conn.fetch(
            """
            SELECT ccl.*, c.full_name as client_name
            FROM client_company_links ccl
            JOIN clients c ON ccl.client_id = c.id
            WHERE ccl.company_id = $1
              AND c.deleted_at IS NULL
            """,
            company_id,
        )

        # Get documents
        documents = await conn.fetch(
            """
            SELECT id, uuid, document_type, document_subtype, document_number,
                   document_title, issue_date, expiry_date, status,
                   google_drive_file_id, file_name, created_at
            FROM company_documents
            WHERE company_id = $1
            ORDER BY created_at DESC
            """,
            company_id,
        )

        # Get tax record
        tax_row = await conn.fetchrow(
            "SELECT * FROM tax_records WHERE entity_type = 'company' AND entity_id = $1",
            company_id,
        )

        company["associates"] = [
            {
                "link_id": a["id"],
                "client_id": a["client_id"],
                "client_name": a["client_name"],
                "role": a["role"],
                "is_primary": a["is_primary"],
                "ownership_percentage": a["ownership_percentage"],
                "shares_count": a["shares_count"],
                "start_date": a["start_date"].isoformat() if a["start_date"] else None,
                "status": a["status"],
            }
            for a in associates
        ]

        company["documents"] = [
            {
                "id": d["id"],
                "uuid": d["uuid"],
                "document_type": d["document_type"],
                "document_subtype": d["document_subtype"],
                "document_number": d["document_number"],
                "document_title": d["document_title"],
                "issue_date": d["issue_date"].isoformat() if d["issue_date"] else None,
                "expiry_date": d["expiry_date"].isoformat() if d["expiry_date"] else None,
                "status": d["status"],
                "google_drive_file_id": d["google_drive_file_id"],
                "file_name": d["file_name"],
            }
            for d in documents
        ]

        if tax_row:
            company["tax_record"] = {
                "id": tax_row["id"],
                "npwp": tax_row["npwp"],
                "npwp_status": tax_row["npwp_status"],
                "tax_center": tax_row["tax_center"],
                "compliance_status": tax_row["compliance_status"],
                "next_filing_date": tax_row["next_filing_date"].isoformat()
                if tax_row["next_filing_date"]
                else None,
            }

        return company


@router.patch("/{company_id}", response_model=dict[str, Any])
async def update_company(
    request: Request,
    company_id: int,
    data: CompanyUpdate,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Update company details (admin, or staff who manage the company's clients)"""
    user_email = current_user.get("email", "system")

    async with db.acquire() as conn:
        # Authorize first: a denied caller gets 403/404 whatever the payload holds.
        await _require_company_editor(conn, company_id, current_user, "edit")

        # Only fields the client actually sent are written. CompanyUpdate has
        # already validated and converted them: a date string arrives as
        # datetime.date, and an explicit null clears a nullable column.
        payload = data.model_dump(exclude_unset=True)

        updates = []
        params = []
        for field, value in payload.items():
            updates.append(f"{field} = ${len(params) + 1}")
            params.append(value)

        if not updates:
            raise HTTPException(status_code=400, detail="No valid fields to update")

        updates.append(f"updated_by = ${len(params) + 1}")
        updates.append("updated_at = NOW()")
        params.append(user_email)
        params.append(company_id)

        query = f"UPDATE companies SET {', '.join(updates)} WHERE id = ${len(params)} RETURNING id, company_name"

        row = await conn.fetchrow(query, *params)
        if not row:
            raise HTTPException(status_code=404, detail="Company not found")

        return {
            "id": row["id"],
            "company_name": row["company_name"],
            "message": "Company updated successfully",
        }


@router.delete("/{company_id}")
async def delete_company(
    request: Request,
    company_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a company"""
    _require_crm_admin(current_user)
    async with db.acquire() as conn:
        result = await conn.execute("DELETE FROM companies WHERE id = $1", company_id)
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Company not found")

        return {"message": "Company deleted successfully"}


# ========== CLIENT-COMPANY LINK ENDPOINTS ==========


@router.get("/{company_id}/clients", response_model=list[dict[str, Any]])
async def get_company_clients(
    request: Request,
    company_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Get all clients associated with a company"""
    async with db.acquire() as conn:
        # Verify company exists
        exists = await conn.fetchval("SELECT 1 FROM companies WHERE id = $1", company_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Company not found")

        rows = await conn.fetch(
            """
            SELECT ccl.*, c.full_name as client_name
            FROM client_company_links ccl
            JOIN clients c ON ccl.client_id = c.id
            WHERE ccl.company_id = $1
              AND c.deleted_at IS NULL
            """,
            company_id,
        )

        return [
            {
                "link_id": r["id"],
                "client_id": r["client_id"],
                "client_name": r["client_name"],
                "role": r["role"],
                "is_primary": r["is_primary"],
                "ownership_percentage": r["ownership_percentage"],
                "shares_count": r["shares_count"],
                "start_date": r["start_date"].isoformat() if r["start_date"] else None,
                "status": r["status"],
            }
            for r in rows
        ]


@router.post("/{company_id}/clients/{client_id}/link", response_model=dict[str, Any])
async def link_client_to_company(
    request: Request,
    company_id: int,
    client_id: int,
    data: ClientCompanyLinkCreate,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Link a client to a company with a specific role"""
    async with db.acquire() as conn:
        # Verify company exists
        exists = await conn.fetchval("SELECT 1 FROM companies WHERE id = $1", company_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Company not found")

        await verify_client_access(
            client_id,
            current_user,
            conn,
            allow_assigned=True,
            write=True,
        )

        # One transaction with the same advisory lock the practice form's resolver takes,
        # so the check and the insert cannot interleave with it.
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", link_lock_key(client_id, company_id)
            )
            existing = await conn.fetchrow(
                "SELECT 1 FROM client_company_links WHERE client_id = $1 AND company_id = $2",
                client_id,
                company_id,
            )
            if existing:
                raise HTTPException(
                    status_code=400, detail="Client is already linked to this company"
                )

            row = await conn.fetchrow(
                """
                INSERT INTO client_company_links (
                    client_id, company_id, role, is_primary,
                    ownership_percentage, shares_count, start_date
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                client_id,
                company_id,
                data.role,
                data.is_primary,
                data.ownership_percentage,
                data.shares_count,
                data.start_date,
            )

        return {
            "link_id": row["id"],
            "message": "Client linked to company successfully",
        }


@router.delete("/{company_id}/clients/{client_id}/link")
async def unlink_client_from_company(
    request: Request,
    company_id: int,
    client_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Remove link between client and company"""
    async with db.acquire() as conn:
        await verify_client_access(
            client_id,
            current_user,
            conn,
            allow_assigned=True,
            write=True,
        )

        result = await conn.execute(
            "DELETE FROM client_company_links WHERE client_id = $1 AND company_id = $2",
            client_id,
            company_id,
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Link not found")

        return {"message": "Client unlinked from company successfully"}


# ========== COMPANY DOCUMENT ENDPOINTS ==========


@router.get("/{company_id}/documents", response_model=list[dict[str, Any]])
async def get_company_documents(
    request: Request,
    company_id: int,
    doc_type: str | None = Query(None, description="Filter by document type"),
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """Get documents for a company"""
    async with db.acquire() as conn:
        query = """
            SELECT id, uuid, document_type, document_subtype, document_number,
                   document_title, description, issue_date, expiry_date,
                   reminder_date, status, is_verified, google_drive_file_id,
                   google_drive_file_url, file_name, file_size_kb, mime_type,
                   notes, created_at
            FROM company_documents
            WHERE company_id = $1
        """
        params = [company_id]

        if doc_type:
            query += " AND document_type = $2"
            params.append(doc_type)

        query += " ORDER BY created_at DESC"

        rows = await conn.fetch(query, *params)

        return [
            {
                "id": r["id"],
                "uuid": r["uuid"],
                "document_type": r["document_type"],
                "document_subtype": r["document_subtype"],
                "document_number": r["document_number"],
                "document_title": r["document_title"],
                "description": r["description"],
                "issue_date": r["issue_date"].isoformat() if r["issue_date"] else None,
                "expiry_date": r["expiry_date"].isoformat() if r["expiry_date"] else None,
                "reminder_date": r["reminder_date"].isoformat() if r["reminder_date"] else None,
                "status": r["status"],
                "is_verified": r["is_verified"],
                "google_drive_file_id": r["google_drive_file_id"],
                "google_drive_file_url": r["google_drive_file_url"],
                "file_name": r["file_name"],
                "file_size_kb": r["file_size_kb"],
                "mime_type": r["mime_type"],
                "notes": r["notes"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]


@router.post("/{company_id}/documents", response_model=dict[str, Any])
async def create_company_document(
    request: Request,
    company_id: int,
    data: CompanyDocumentCreate,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Add a document to a company"""
    _require_crm_admin(current_user)
    user_email = current_user.get("email", "system")

    async with db.acquire() as conn:
        # Verify company exists
        exists = await conn.fetchval("SELECT 1 FROM companies WHERE id = $1", company_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Company not found")

        google_drive_file_id = data.google_drive_file_id
        if google_drive_file_id:
            await assert_drive_file_belongs_to_company(google_drive_file_id, company_id, conn)
        # The Drive proxy reads every run its address names as this row's file: each
        # one other than the explicit id is checked like it (a run equal to it, once).
        for drive_id in address_drive_ids(data.google_drive_file_url):
            if drive_id != google_drive_file_id:
                await assert_drive_file_belongs_to_company(drive_id, company_id, conn)

        row = await conn.fetchrow(
            """
            INSERT INTO company_documents (
                company_id, document_type, document_subtype, document_number,
                document_title, description, issue_date, expiry_date,
                google_drive_file_id, google_drive_file_url, file_name,
                file_size_kb, mime_type, uploaded_by
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING id, uuid
            """,
            company_id,
            data.document_type,
            data.document_subtype,
            data.document_number,
            data.document_title,
            data.description,
            data.issue_date,
            data.expiry_date,
            data.google_drive_file_id,
            data.google_drive_file_url,
            data.file_name,
            data.file_size_kb,
            data.mime_type,
            user_email,
        )

        return {
            "id": row["id"],
            "uuid": row["uuid"],
            "message": "Document added successfully",
        }


@router.delete("/{company_id}/documents/{doc_id}")
async def delete_company_document(
    request: Request,
    company_id: int,
    doc_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a company document"""
    _require_crm_admin(current_user)
    async with db.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM company_documents WHERE id = $1 AND company_id = $2",
            doc_id,
            company_id,
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Document not found")

        return {"message": "Document deleted successfully"}


# ========== TAX RECORD ENDPOINTS ==========


@router.get("/{company_id}/tax", response_model=dict[str, Any])
async def get_company_tax_record(
    request: Request,
    company_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Get tax record for a company"""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tax_records WHERE entity_type = 'company' AND entity_id = $1",
            company_id,
        )

        if not row:
            return {"message": "No tax record found for this company"}

        # Get tax documents
        docs = await conn.fetch(
            """
            SELECT id, document_type, tax_type, tax_year, tax_period,
                   document_number, filing_date, reported_amount, paid_amount,
                   status, file_name
            FROM tax_documents
            WHERE entity_type = 'company' AND entity_id = $1
            ORDER BY created_at DESC
            """,
            company_id,
        )

        return {
            "id": row["id"],
            "uuid": row["uuid"],
            "npwp": row["npwp"],
            "npwp_status": row["npwp_status"],
            "tax_center": row["tax_center"],
            "is_pph21_registered": row["is_pph21_registered"],
            "is_pph23_registered": row["is_pph23_registered"],
            "is_pph25_registered": row["is_pph25_registered"],
            "is_ppn_registered": row["is_ppn_registered"],
            "is_pph29_registered": row["is_pph29_registered"],
            "tax_year": row["tax_year"],
            "reporting_period": row["reporting_period"],
            "last_filing_date": row["last_filing_date"].isoformat()
            if row["last_filing_date"]
            else None,
            "next_filing_date": row["next_filing_date"].isoformat()
            if row["next_filing_date"]
            else None,
            "compliance_status": row["compliance_status"],
            "custom_fields": row["custom_fields"],
            "tax_documents": [
                {
                    "id": d["id"],
                    "document_type": d["document_type"],
                    "tax_type": d["tax_type"],
                    "tax_year": d["tax_year"],
                    "tax_period": d["tax_period"],
                    "filing_date": d["filing_date"].isoformat() if d["filing_date"] else None,
                    "reported_amount": d["reported_amount"],
                    "paid_amount": d["paid_amount"],
                    "status": d["status"],
                }
                for d in docs
            ],
        }


# ========== CLIENT-SPECIFIC COMPANY ENDPOINTS ==========
# NOTE: by-name and by-client routes are defined BEFORE /{company_id} above to avoid routing conflicts


# ========== DRIVE SYNC ENDPOINT ==========

# Map filename keywords → document_type
_DRIVE_FILENAME_TO_DOCTYPE: list[tuple[list[str], str]] = [
    (["akta pendirian", "akta_pendirian", "deed of establishment"], "akta_pendirian"),
    (["akta perubahan", "akta_perubahan", "amendment"], "akta_perubahan"),
    (
        [
            "sk kemenkumham",
            "sk pendirian",
            "sk_pendirian",
            "sk_decree",
            "sk kemenkum",
            "sk menkumham",
        ],
        "sk_decree",
    ),
    (["npwp"], "npwp"),
    (["nib", "nomor induk berusaha"], "nib"),
    (["company profile", "profil perseroan", "profil pt", "profile perseroan"], "company_profile"),
    (["wlkp"], "wlkp"),
    (["bpjs ketenagakerjaan", "bpjs"], "bpjs"),
    (["organogram", "bagan organisasi"], "organogram"),
    (["rekening koran"], "rekening_koran"),
    (["izin usaha", "siup"], "siup"),
    (["tdp"], "tdp"),
    (["domisili"], "domisili"),
    (["imta", "rptka"], "imta"),
]


def _infer_doc_type(filename: str) -> str:
    """Infer document_type from filename keywords."""
    fn = filename.lower()
    for keywords, doc_type in _DRIVE_FILENAME_TO_DOCTYPE:
        if any(kw in fn for kw in keywords):
            return doc_type
    return "other"


@router.post("/{company_id}/sync-drive", response_model=dict[str, Any])
async def sync_company_drive_folder(
    company_id: int,
    db: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Allowed to whoever may edit the company (admin, or staff who manage its clients).
    Scan the company's Google Drive folder and upsert files into company_documents.
    Files already tracked (by google_drive_file_id) are skipped.
    Returns a summary: {added, skipped, total, docs}.
    """
    async with db.acquire() as conn:
        await _require_company_editor(conn, company_id, current_user, "sync")
        row = await conn.fetchrow(
            "SELECT id, company_name, google_drive_folder_id FROM companies WHERE id = $1",
            company_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Company not found")
        folder_id: str | None = row["google_drive_folder_id"]
        if not folder_id:
            raise HTTPException(status_code=400, detail="Company has no Google Drive folder linked")

    # Build Drive service using SA credentials
    def _build_drive_service() -> Any:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

        if sa_json:
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                f.write(sa_json)
                sa_path = f.name
        elif not sa_path:
            raise HTTPException(
                status_code=503,
                detail="Drive credentials not configured (set GOOGLE_CREDENTIALS_JSON)",
            )

        creds = service_account.Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        return build("drive", "v3", credentials=creds)

    # The Drive client is synchronous: reading the credentials, building the client
    # and every listing call below block the thread they run on. This handler runs
    # on the API's event loop, so they go to a worker thread; run inline, one slow
    # Drive answer held every other request of the worker.
    try:
        service = await asyncio.to_thread(_build_drive_service)
    except Exception as e:
        logger.error("[sync-drive] Failed to build Drive service: %s", e)
        raise HTTPException(status_code=503, detail=f"Drive auth error: {e}")

    def _list_files_recursive(fid: str, depth: int = 0) -> list[dict]:
        """Recursively list all non-folder files under a Drive folder (max depth 4)."""
        if depth > 4:
            return []
        all_files: list[dict] = []
        page_token = None
        while True:
            kwargs: dict = {
                "q": f"'{fid}' in parents and trashed=false",
                "fields": "nextPageToken, files(id, name, mimeType, webViewLink)",
                "pageSize": 100,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.files().list(**kwargs).execute()
            for item in resp.get("files", []):
                mime = item.get("mimeType")
                if mime == "application/vnd.google-apps.shortcut":
                    continue
                if mime == "application/vnd.google-apps.folder":
                    all_files.extend(_list_files_recursive(item["id"], depth + 1))
                else:
                    all_files.append(item)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return all_files

    # Collect all files recursively
    try:
        files = await asyncio.to_thread(_list_files_recursive, folder_id)
    except Exception as e:
        logger.error("[sync-drive] Drive list error for folder %s: %s", folder_id, e)
        raise HTTPException(status_code=503, detail=f"Drive list error: {e}")

    added: list[dict] = []
    skipped: list[str] = []
    ocr_tasks: list[tuple[str, str, str, int]] = []  # (file_id, filename, doc_type, doc_id)

    async with db.acquire() as conn:
        # Find primary client_id for OCR dispatcher (needs client context)
        primary_client_id: int | None = await conn.fetchval(
            "SELECT client_id FROM client_company_links WHERE company_id = $1 AND is_primary = true LIMIT 1",
            company_id,
        ) or await conn.fetchval(
            "SELECT client_id FROM client_company_links WHERE company_id = $1 LIMIT 1",
            company_id,
        )

        for f in files:
            file_id = f["id"]
            filename = f["name"]
            web_url = f.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

            # Check if already tracked
            existing = await conn.fetchval(
                "SELECT id FROM company_documents WHERE company_id = $1 AND google_drive_file_id = $2",
                company_id,
                file_id,
            )
            if existing:
                skipped.append(filename)
                continue

            doc_type = _infer_doc_type(filename)

            doc_id = await conn.fetchval(
                """
                INSERT INTO company_documents
                  (company_id, document_type, file_name, google_drive_file_id, google_drive_file_url, status, is_verified)
                VALUES ($1, $2, $3, $4, $5, 'active', false)
                RETURNING id
                """,
                company_id,
                doc_type,
                filename,
                file_id,
                web_url,
            )
            added.append({"id": doc_id, "file_name": filename, "document_type": doc_type})
            logger.info(
                "[sync-drive] Added doc %s (%s) → company %s", filename, doc_type, company_id
            )

            # Queue for OCR if applicable
            if doc_type in ("npwp", "nib", "company_profile") and primary_client_id:
                ocr_tasks.append((file_id, filename, doc_type, doc_id))

    # Trigger OCR in background for newly added docs
    if ocr_tasks and primary_client_id:

        async def _run_ocr_tasks() -> None:
            try:
                from backend.app.routers.crm_enhanced import _dispatch_ocr_by_folder

                ocr_pool = db
                for fid, fname, dtype, _did in ocr_tasks:
                    try:
                        await _dispatch_ocr_by_folder(
                            db_pool=ocr_pool,
                            client_id=primary_client_id,
                            file_id=fid,
                            folder_name="02_Company",  # Semantic OCR hint, not a Drive destination.
                            filename=fname,
                            doc_id=None,  # company_documents id, not documents id
                            document_type=dtype,
                        )
                        logger.info("[sync-drive] OCR dispatched for %s (%s)", fname, dtype)
                    except Exception as e:
                        logger.warning("[sync-drive] OCR failed for %s: %s", fname, e)
            except Exception as e:
                logger.error("[sync-drive] OCR background task error: %s", e)

        spawn(_run_ocr_tasks(), name="company_router_ocr_tasks")
        logger.info(f"[sync-drive] Queued {len(ocr_tasks)} OCR tasks in background")

    return {
        "company_id": company_id,
        "folder_id": folder_id,
        "added": len(added),
        "skipped": len(skipped),
        "total_in_folder": len(files),
        "docs": added,
        "ocr_queued": len(ocr_tasks),
    }
