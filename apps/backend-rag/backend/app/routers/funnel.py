"""
Funnel session tracking — cross-funnel lead capture before auth.

Endpoints:
- POST /api/funnel/session/touch   upsert session + update last_touched_at
- POST /api/funnel/session/convert  set converted_to_client_id at SSO login
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from enum import Enum
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from backend.app.deps.auth import get_current_user
from backend.app.deps.database import get_database_pool
from backend.app.utils.crm_utils import is_crm_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/funnel", tags=["funnel"])

_MAX_FUNNEL_JSON_BYTES = 8 * 1024


async def require_convert_caller(request: Request) -> dict[str, Any]:
    """Authenticate caller for /session/convert via Request without altering OpenAPI."""
    app = getattr(request, "app", None)
    overrides = getattr(app, "dependency_overrides", None)
    if isinstance(overrides, dict):
        override = overrides.get(get_current_user)
        if callable(override):
            user = override()
            if inspect.isawaitable(user):
                user = await user
            if isinstance(user, dict):
                return user

    headers = getattr(request, "headers", None)
    auth_header = headers.get("Authorization") if headers is not None else None
    creds: HTTPAuthorizationCredentials | None = None
    if isinstance(auth_header, str) and auth_header.startswith("Bearer "):
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=auth_header[7:].strip(),
        )

    state_user = getattr(getattr(request, "state", None), "user", None)
    if isinstance(state_user, dict) and state_user:
        resolved: dict[str, Any] = {
            "email": state_user.get("email"),
            "user_id": state_user.get("id") or state_user.get("user_id") or state_user.get("email"),
            "role": state_user.get("role", "user"),
            "permissions": state_user.get("permissions", []),
        }
        if state_user.get("client_id") is not None:
            resolved["client_id"] = state_user["client_id"]
    else:
        auth_req = request
        if hasattr(getattr(request, "state", None), "user"):
            auth_req = SimpleNamespace(state=SimpleNamespace(user=None))
        resolved = get_current_user(auth_req, credentials=creds)

    # client_id comes only from the identity that authenticated this request.
    # Never read it from another credential on the same request: a Bearer token
    # decoded here is neither bound to that identity nor checked for revocation.
    return resolved


class FunnelType(str, Enum):
    VISA = "visa"
    KBLI = "kbli"
    TAX = "tax"
    PROPERTY = "property"
    HOME = "home"


class TouchRequest(BaseModel):
    session_id: str = Field(min_length=32, max_length=64)
    funnel: FunnelType
    step_state: dict[str, Any] = Field(default_factory=dict)
    lead_profile: dict[str, Any] = Field(default_factory=dict)


def _ip_hash(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for") or ""
    client_host = request.client.host if request.client else ""
    ip = (xff.split(",")[0].strip() or client_host).strip()
    return hashlib.sha256(ip.encode()).hexdigest()[:64] if ip else ""


@router.post("/session/touch")
async def touch_session(
    req: TouchRequest,
    request: Request,
    pool=Depends(get_database_pool),
) -> dict[str, bool]:
    # ::text::jsonb — the app pools register a jsonb codec whose encoder
    # is json.dumps; a param inferred as jsonb would get the
    # pre-serialized string dumped AGAIN and land as a jsonb string
    # scalar (funnel_sessions had 6674/3508 polluted rows, live-probe
    # 2026-07-16). Typing the param as text makes the server parse it.
    step_state_json = json.dumps(req.step_state)
    lead_profile_json = json.dumps(req.lead_profile)
    if (
        len(step_state_json.encode("utf-8")) > _MAX_FUNNEL_JSON_BYTES
        or len(lead_profile_json.encode("utf-8")) > _MAX_FUNNEL_JSON_BYTES
    ):
        raise HTTPException(
            status_code=413,
            detail="Funnel payload exceeds 8 KB limit",
        )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO funnel_sessions (session_id, funnel, step_state, lead_profile, ip_hash)
            VALUES ($1, $2, $3::text::jsonb, $4::text::jsonb, $5)
            ON CONFLICT (session_id) DO UPDATE SET
                funnel = EXCLUDED.funnel,
                step_state = funnel_sessions.step_state || EXCLUDED.step_state,
                lead_profile = funnel_sessions.lead_profile || EXCLUDED.lead_profile,
                last_touched_at = NOW()
            """,
            req.session_id,
            req.funnel.value,
            step_state_json,
            lead_profile_json,
            _ip_hash(request),
        )
    return {"ok": True}


class ConvertRequest(BaseModel):
    session_id: str = Field(min_length=32, max_length=64)
    client_id: str


@router.post("/session/convert")
async def convert_session(
    req: ConvertRequest,
    current_user: dict[str, Any] = Depends(require_convert_caller),
    pool=Depends(get_database_pool),
) -> dict[str, bool]:
    role = (current_user.get("role") or "").strip().lower()
    if not (is_crm_admin(current_user) or role in {"internal", "service"}):
        caller_cid = str(current_user.get("client_id") or "").strip()
        if not caller_cid or caller_cid != req.client_id.strip():
            raise HTTPException(
                status_code=403,
                detail="client_id does not match authenticated caller",
            )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE funnel_sessions
            SET converted_to_client_id = $2
            WHERE session_id = $1
            RETURNING funnel, first_touched_at
            """,
            req.session_id,
            req.client_id,
        )
        if row:
            await conn.execute(
                """
                INSERT INTO funnel_attributions (client_id, session_id, first_funnel, first_touch_at)
                VALUES ($1, $2, $3, $4)
                """,
                req.client_id,
                req.session_id,
                row["funnel"],
                row["first_touched_at"],
            )
    return {"ok": True}
