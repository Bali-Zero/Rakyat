"""Staff delivery checklist and the authenticated client's delivered documents."""

from datetime import date
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers.crm_visa_followup import router as visa_followup_router
from backend.app.routers.portal import get_current_client
from backend.services.crm import practice_delivery as delivery

router = APIRouter()
staff = APIRouter(prefix="/api/crm/practices", tags=["crm-practices"])
portal_router = APIRouter(prefix="/api/portal/process", tags=["portal-process"])
PracticeId = Annotated[int, Path(gt=0, le=2**31 - 1)]
Pool = Annotated[asyncpg.Pool, Depends(get_database_pool)]
Staff = Annotated[dict, Depends(get_current_user)]
Client = Annotated[dict, Depends(get_current_client)]
SlotKey = Annotated[str, Path(min_length=1, max_length=100)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=140)]


class DeliveryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_name: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=255, pattern=r"^[^/\\\x00-\x1f]+$"
        ),
    ]
    file_content: str = Field(min_length=4, max_length=delivery.MAX_BYTES * 4 // 3)
    mime_type: str = Field(min_length=1, max_length=150)
    issued_on: date | None = None
    company_id: int | None = Field(default=None, gt=0, le=2**31 - 1)


class ReuseFile(BaseModel):
    document_id: int = Field(gt=0, le=2**31 - 1)


class Waiver(BaseModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class DeliveryTitles(BaseModel):
    titles: list[Title] = Field(min_length=1, max_length=30)


@staff.get("/{practice_id}/delivery")
async def get_delivery(practice_id: PracticeId, user: Staff, pool: Pool) -> dict[str, Any]:
    async with pool.acquire() as conn:
        p = await delivery.practice(conn, practice_id, user)
        return await delivery.state(conn, p, user)


@staff.post("/{practice_id}/delivery/titles")
async def name_outputs(
    practice_id: PracticeId, request: DeliveryTitles, user: Staff, pool: Pool
) -> dict[str, Any]:
    return await delivery.set_titles(pool, practice_id, user, request.titles)


@staff.post("/{practice_id}/delivery/slots/{key}/reuse")
async def use_combined_file(
    practice_id: PracticeId, key: SlotKey, request: ReuseFile, user: Staff, pool: Pool
) -> dict[str, Any]:
    return await delivery.reuse(pool, practice_id, key, user, request.document_id)


@staff.post("/{practice_id}/delivery/slots/{key}/waive")
async def waive_slot(
    practice_id: PracticeId, key: SlotKey, request: Waiver, user: Staff, pool: Pool
) -> dict[str, Any]:
    return await delivery.waive(pool, practice_id, key, user, request.reason)


@staff.post("/{practice_id}/delivery/slots/{key}")
async def upload_slot(
    practice_id: PracticeId,
    key: SlotKey,
    request: DeliveryFile,
    user: Staff,
    pool: Pool,
    tasks: BackgroundTasks,
) -> dict[str, Any]:
    return await delivery.upload(pool, practice_id, key, user, request, tasks)


@staff.post("/{practice_id}/delivery/complete")
async def complete_delivery(
    practice_id: PracticeId, user: Staff, pool: Pool, tasks: BackgroundTasks
) -> dict[str, Any]:
    return await delivery.complete(pool, practice_id, user, tasks)


@portal_router.get("/{practice_id}/delivery-documents")
async def get_delivered_documents(
    practice_id: PracticeId, client: Client, pool: Pool
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        return await delivery.portal_files(conn, practice_id, client["client_id"])


router.include_router(visa_followup_router)
router.include_router(staff)
router.include_router(portal_router)
