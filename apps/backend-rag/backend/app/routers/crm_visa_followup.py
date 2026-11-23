"""Private post-visa declarations and explicitly submitted owner escalations."""

from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, StringConstraints

from backend.app.dependencies import get_current_user, get_database_pool
from backend.services.crm import visa_followup

router = APIRouter(prefix="/api/crm/practices", tags=["crm-practices"])
Practice = Annotated[int, Path(gt=0, le=2**31 - 1)]
DB = Annotated[asyncpg.Pool, Depends(get_database_pool)]
Staff = Annotated[dict, Depends(get_current_user)]


class OfficialNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=2000)]


@router.post("/{practice_id}/delivery/visa-followup/postpone")
async def submit_non_entry(
    practice_id: Practice, request: OfficialNote, user: Staff, pool: DB
) -> dict[str, Any]:
    return await visa_followup.postpone(pool, practice_id, user, request.note)


@router.post("/{practice_id}/delivery/visa-followup/escalate")
async def send_reasons_to_owner(
    practice_id: Practice, request: OfficialNote, user: Staff, pool: DB
) -> dict[str, Any]:
    return await visa_followup.escalate(pool, practice_id, user, request.note)
