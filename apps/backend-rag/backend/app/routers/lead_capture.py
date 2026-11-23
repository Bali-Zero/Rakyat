"""POST /api/lead/capture — shared WhatsApp handoff endpoint.

Called by all 4 homepage apps (visa, kbli, tax, zoning) when the user
clicks the final "Continue on WhatsApp" CTA. Persists the lead intent,
builds the wa.me deeplink, and returns the URL.

Target latency: p95 < 200ms (single INSERT + URL build, no external calls).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator

from backend.app.dependencies import get_database_pool
from backend.app.utils.logging_utils import get_logger
from backend.services.lead_capture.repository import LeadIntentRepository, new_intent_id
from backend.services.lead_capture.source import PublicLeadSource
from backend.services.lead_capture.whatsapp_deeplink import build_whatsapp_url

logger = get_logger(__name__)


class _LeadRoute(APIRoute):
    """Answer a refused public payload with 422 and no echo of its input.

    FastAPI's default validation answer echoes each error's `input`; a NaN or
    Infinity there cannot be rendered as JSON and the refusal became a 500.
    Only the location, message and type of each error are returned.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        downstream = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await downstream(request)
            except RequestValidationError as exc:
                detail = [
                    {
                        "loc": list(e.get("loc", ())),
                        "msg": str(e.get("msg", "")),
                        "type": e.get("type"),
                    }
                    for e in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": detail})

        return handler


router = APIRouter(prefix="/api/lead", tags=["lead-capture"], route_class=_LeadRoute)


# ============================================================
# Pydantic models
# ============================================================


def _contains_nul(value: Any) -> bool:
    """Return whether a JSON-compatible value contains a PostgreSQL-invalid NUL."""
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(
            _contains_nul(key) or _contains_nul(nested_value) for key, nested_value in value.items()
        )
    if isinstance(value, list):
        return any(_contains_nul(item) for item in value)
    return False


class ContextLine(BaseModel):
    """One bullet row under 'Context:' in the WA message."""

    label: str = Field(..., min_length=1, max_length=64)
    value: str = Field(..., min_length=1, max_length=160)


class LeadCaptureRequest(BaseModel):
    source: PublicLeadSource
    # Free-form context for the lead_intents.context JSONB column.
    # The WA deeplink message uses the separate `whatsapp_context` list.
    context: dict[str, Any] = Field(default_factory=dict)
    whatsapp_context: list[ContextLine] = Field(default_factory=list, max_length=6)
    # Optional shareable result page hash (e.g. visa_checks.hash).
    result_hash: str | None = Field(default=None, max_length=20)
    utm: dict[str, Any] | None = None
    client_fingerprint: str | None = Field(default=None, max_length=32)

    @field_validator("context", "utm")
    @classmethod
    def _cap_nested_size(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject payloads > 8KB as JSON to keep lead_intents.context small."""
        if v is None:
            return v

        if len(json.dumps(v, allow_nan=False)) > 8192:
            raise ValueError("context/utm too large (>8KB)")
        if _contains_nul(v):
            raise ValueError("context/utm must not contain NUL characters")
        return v

    @field_validator("client_fingerprint")
    @classmethod
    def _reject_fingerprint_nul(cls, v: str | None) -> str | None:
        if v is not None and "\x00" in v:
            raise ValueError("client_fingerprint must not contain NUL characters")
        return v


class LeadCaptureResponse(BaseModel):
    lead_intent_id: str
    whatsapp_url: str
    expires_at: datetime


# ============================================================
# Endpoint
# ============================================================


@router.post("/capture", response_model=LeadCaptureResponse, status_code=201)
async def capture_lead(
    payload: LeadCaptureRequest,
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> LeadCaptureResponse:
    """Persist a lead intent and return the pre-filled WhatsApp URL.

    Flow
    ----
    1. Generate intent_id up-front so the WA body can reference it.
    2. Build the wa.me URL (includes intent_id in the body).
    3. INSERT the row (single round-trip).
    4. Return JSON.

    A failed INSERT raises 500; the user sees a generic error and can
    still WhatsApp us via the footer link — we don't block on our DB.
    """
    repo = LeadIntentRepository(db_pool)
    intent_id = new_intent_id()
    source = payload.source.to_persisted()

    try:
        wa_url = build_whatsapp_url(
            source=source,
            context_lines=[(line.label, line.value) for line in payload.whatsapp_context],
            result_hash=payload.result_hash,
            lead_intent_id=intent_id,
        )
    except Exception:
        logger.exception("lead_capture: deeplink build failed source=%s", source.value)
        raise HTTPException(status_code=500, detail="Could not build WhatsApp link")

    try:
        intent = await repo.insert(
            source=source,
            context=payload.context,
            utm=payload.utm,
            fingerprint=payload.client_fingerprint,
            whatsapp_url=wa_url,
            intent_id=intent_id,
        )
    except (asyncpg.PostgresError, asyncpg.InterfaceError):
        logger.exception("lead_capture: DB insert failed source=%s", source.value)
        raise HTTPException(status_code=500, detail="Could not persist lead")

    logger.info(
        "lead_capture.ok source=%s intent_id=%s",
        source.value,
        intent.id,
    )
    return LeadCaptureResponse(
        lead_intent_id=intent.id,
        whatsapp_url=intent.whatsapp_url,
        expires_at=intent.expires_at,
    )
