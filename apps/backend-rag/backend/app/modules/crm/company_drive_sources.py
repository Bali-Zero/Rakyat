"""Staff metadata only. These links never authorize a proxy, sync or ingestion."""

import json
import re
from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from backend.app.dependencies import get_database_pool, require_team_member

router = APIRouter()
MAX_COMPANY_ID = 2**31 - 1
SourceKind = Literal["primary", "restricted_tax", "legacy"]


class CompanyDriveSource(BaseModel):
    url: str
    kinds: list[SourceKind]


class CompanyDriveSources(BaseModel):
    company_id: int
    sources: list[CompanyDriveSource] = Field(default_factory=list)
    lineage_status: Literal["none", "valid", "invalid"] = "none"
    merged_into_company_id: int | None = None
    historical_record_ids: list[int] = Field(default_factory=list)


def _custom_fields(raw: Any) -> dict | None:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw if isinstance(raw, dict) else None


def _folder_id(value: Any) -> bool:
    # Syntax only: existence and the viewer's access remain Google's decision.
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{20,100}", value) is not None
        and re.search(r"[A-Za-z]", value) is not None
        and re.fullmatch(r"(?:AHU|NPWP|NIB|KBLI|NIK)[_0-9-]+", value, re.I) is None
    )


def _company_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= MAX_COMPANY_ID


def _merge_target(value: Any) -> int | None:
    if _company_id(value):
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        normalized = int(value)
        return normalized if _company_id(normalized) else None
    return None


@router.get("/{company_id}/drive-sources", response_model=CompanyDriveSources)
async def get_company_drive_sources(
    company_id: int = Path(ge=1, le=MAX_COMPANY_ID),
    _current_user: dict = Depends(require_team_member),
    db: asyncpg.Pool = Depends(get_database_pool),
) -> CompanyDriveSources:
    """Return validated direct links, never documents or raw custom fields."""
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT google_drive_folder_id, tax_dept_folder_id, custom_fields "
            "FROM companies WHERE id = $1",
            company_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Company not found")
        fields = _custom_fields(row["custom_fields"])
        legacy = (fields or {}).get("legacy_drive_folder_ids")
        candidates: list[tuple[Any, SourceKind]] = [
            (row["google_drive_folder_id"], "primary"),
            (row["tax_dept_folder_id"], "restricted_tax"),
        ]
        if isinstance(legacy, list):
            candidates.extend((value, "legacy") for value in legacy)
        sources: dict[str, CompanyDriveSource] = {}
        for value, kind in candidates:
            if not _folder_id(value):
                continue
            if value not in sources:
                sources[value] = CompanyDriveSource(
                    url=f"https://drive.google.com/drive/folders/{value}", kinds=[]
                )
            if kind not in sources[value].kinds:
                sources[value].kinds.append(kind)
        result = CompanyDriveSources(company_id=company_id, sources=list(sources.values()))
        ref = (fields or {}).get("merged_into")
        if fields is None or ref is not None:
            result.lineage_status = "invalid"
            target_id = _merge_target(ref)
            if target_id is not None and target_id != company_id:
                target = await conn.fetchrow(
                    "SELECT custom_fields FROM companies WHERE id = $1", target_id
                )
                target_fields = _custom_fields(target["custom_fields"]) if target else None
                # One verified terminal hop only. Reject chains/cycles, never walk them.
                if target_fields is not None and target_fields.get("merged_into") is None:
                    result.lineage_status = "valid"
                    result.merged_into_company_id = target_id
        else:
            historical = await conn.fetch(
                "SELECT id, custom_fields FROM companies "
                "WHERE ((jsonb_typeof(custom_fields->'merged_into') = 'number' "
                "AND custom_fields->'merged_into' = to_jsonb($1::int)) "
                "OR (jsonb_typeof(custom_fields->'merged_into') = 'string' "
                "AND custom_fields->'merged_into' = to_jsonb(($1::int)::text))) "
                "AND id <> $1 ORDER BY id LIMIT 100",
                company_id,
            )
            result.historical_record_ids = [
                record["id"]
                for record in historical
                if _company_id(record["id"])
                and record["id"] != company_id
                and _merge_target(
                    (_custom_fields(record["custom_fields"]) or {}).get("merged_into")
                )
                == company_id
            ]
        return result
