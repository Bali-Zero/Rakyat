"""Read-only admin view of the related-subjects registry.

GET /api/crm/related-registry and GET /api/crm/related-registry/{subject_id}. CRM admins only
(same gate as ``compliance_obligations``); the routes stay behind HybridAuthMiddleware. The
prefix differs on purpose from the write router's, whose ``/register`` path is public.
Logs carry subject ids, counts and outcome words only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.utils.crm_utils import is_crm_admin
from backend.services.crm import related_registry_read

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crm/related-registry", tags=["crm-related-registry"])

Q_MIN = 2
Q_MAX = 64


class RelatedRegistryIdentifier(BaseModel):
    type: str
    value: str


NameSource = Literal["manual", "mrz_corrected", "registration", "ocr_corroborated", "ocr_single"]


class RelatedRegistryName(BaseModel):
    name: str
    source: NameSource
    created_at: datetime


class RelatedRegistrySubjectRow(BaseModel):
    id: int
    kind: str
    display_name: str | None
    name_source: NameSource | None
    date_of_birth: date | None
    nationality: str | None
    identifiers: list[RelatedRegistryIdentifier]
    document_count: int


class RelatedRegistryListOut(BaseModel):
    items: list[RelatedRegistrySubjectRow]
    total: int
    limit: int
    offset: int


class RelatedRegistryDocument(BaseModel):
    sha256: str
    doc_type: str | None
    mime_type: str | None
    byte_size: int
    first_seen_on: date | None
    drive_file_ids: list[str]


class RelatedRegistrySubjectDetail(BaseModel):
    id: int
    kind: str
    display_name: str | None
    name_source: NameSource | None
    names: list[RelatedRegistryName]
    names_total: int
    date_of_birth: date | None
    nationality: str | None
    sex: str | None
    created_at: datetime
    identifiers: list[RelatedRegistryIdentifier]
    documents: list[RelatedRegistryDocument]


def _require_admin(user: dict[str, Any]) -> None:
    if not is_crm_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CRM admin required")


def _search_text(q: str | None) -> str | None:
    """The stripped search text; None when absent or blank; 422 with a fixed message otherwise."""
    if q is None:
        return None
    text = q.strip()
    if not text:
        return None
    if not Q_MIN <= len(text) <= Q_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="q must be 2 to 64 characters",
        )
    return text


def _unavailable(outcome: str) -> HTTPException:
    logger.warning("related registry read failed: %s", outcome)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="registry_unavailable"
    )


@router.get("", response_model=RelatedRegistryListOut)
async def list_registry(
    q: str | None = Query(None),
    kind: Literal["person", "company"] | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100000),
    user: dict[str, Any] = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """List and search registry subjects (CRM admins only)."""
    _require_admin(user)
    text = _search_text(q)
    try:
        async with pool.acquire() as conn:
            result = await related_registry_read.list_subjects(
                conn, q=text, kind=kind, limit=limit, offset=offset
            )
    except (asyncpg.PostgresError, OSError, asyncpg.InterfaceError) as exc:
        raise _unavailable(type(exc).__name__) from None
    logger.info(
        "related registry list: returned=%d total=%d", len(result["items"]), result["total"]
    )
    return result


@router.get("/{subject_id}", response_model=RelatedRegistrySubjectDetail)
async def get_registry_subject(
    subject_id: int = Path(..., ge=1),
    user: dict[str, Any] = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """Open one registry subject with its identifiers and documents (CRM admins only)."""
    _require_admin(user)
    try:
        async with pool.acquire() as conn:
            result = await related_registry_read.get_subject(conn, subject_id)
    except (asyncpg.PostgresError, OSError, asyncpg.InterfaceError) as exc:
        raise _unavailable(type(exc).__name__) from None
    if result is None:
        logger.info("related registry detail: subject=%d outcome=not_found", subject_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subject not found")
    logger.info(
        "related registry detail: subject=%d documents=%d", subject_id, len(result["documents"])
    )
    return result
