"""
Google Drive API Router

Handles OAuth flow and file operations for Google Drive integration.
"""

import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from backend.app.core.config import settings
from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers.team_activity import get_admin_user
from backend.app.utils.crm_utils import is_super_admin
from backend.core.redis_manager import RedisManager
from backend.services.integrations.google_drive_service import GoogleDriveService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/google-drive", tags=["Google Drive"])

OAUTH_STATE_TTL_SECONDS = 600
_STATE_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
_ATOMIC_CONSUME_LUA = (
    "local v = redis.call('GET', KEYS[1]); "
    "if v then redis.call('DEL', KEYS[1]) end; "
    "return v"
)
# Bounded in-process fallback used ONLY in non-production environments (e.g. unit tests)
# when RedisManager has not been initialized. Production ALWAYS fails closed without Redis.
_NONPROD_OAUTH_STATES: dict[str, tuple[str, float]] = {}


def _is_production_env() -> bool:
    env = str(getattr(settings, "environment", "") or os.environ.get("ENVIRONMENT", "")).strip().lower()
    return env in {"production", "prod"}


def _resolve_user_id(current_user: dict[str, Any]) -> str:
    user_id = current_user.get("user_id") or current_user.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID missing from token")
    return str(user_id)


async def issue_oauth_state(
    provider: str,
    *,
    mode: str,
    user_id: str,
    ttl_seconds: int = OAUTH_STATE_TTL_SECONDS,
) -> str:
    """Generate and persist a one-time cryptographic OAuth state nonce (TTL=600s)."""
    nonce = secrets.token_urlsafe(32)
    key = f"oauth_state:{provider}:{nonce}"
    payload = json.dumps(
        {"nonce": nonce, "provider": provider, "mode": mode, "user_id": str(user_id)},
        separators=(",", ":"),
    )

    redis_client = RedisManager.get_instance().get_async_client()
    if redis_client is not None:
        try:
            await redis_client.set(key, payload, ex=ttl_seconds)
            return nonce
        except Exception as exc:
            logger.error("[OAUTH] Failed to persist OAuth state for %s: %s", provider, exc)
            raise HTTPException(status_code=503, detail="OAuth state store unavailable") from exc

    if _is_production_env():
        logger.error("[OAUTH] Redis unavailable in production; refusing to issue %s state", provider)
        raise HTTPException(status_code=503, detail="OAuth state store unavailable")

    now = time.monotonic()
    if len(_NONPROD_OAUTH_STATES) >= 512:
        expired = [k for k, (_, exp) in _NONPROD_OAUTH_STATES.items() if exp <= now]
        for k in expired:
            _NONPROD_OAUTH_STATES.pop(k, None)
    _NONPROD_OAUTH_STATES[key] = (payload, now + ttl_seconds)
    return nonce


async def consume_oauth_state(
    provider: str,
    state: str | None,
    *,
    expected_modes: tuple[str, ...],
) -> dict[str, str]:
    """Atomically validate and one-time consume a server-issued OAuth state nonce."""
    if not isinstance(state, str) or not _STATE_NONCE_RE.fullmatch(state):
        raise HTTPException(status_code=400, detail="Invalid or missing OAuth state")

    key = f"oauth_state:{provider}:{state}"
    redis_client = RedisManager.get_instance().get_async_client()
    raw: str | bytes | None = None

    if redis_client is not None:
        try:
            getdel = getattr(redis_client, "getdel", None)
            if callable(getdel):
                raw = await getdel(key)
            else:
                raw = await redis_client.eval(_ATOMIC_CONSUME_LUA, 1, key)
        except Exception as exc:
            logger.error("[OAUTH] Failed to consume OAuth state for %s: %s", provider, exc)
            raise HTTPException(status_code=403, detail="OAuth state store unavailable") from exc
    elif _is_production_env():
        logger.error("[OAUTH] Redis unavailable in production; rejecting %s OAuth callback", provider)
        raise HTTPException(status_code=403, detail="OAuth state store unavailable")
    else:
        entry = _NONPROD_OAUTH_STATES.pop(key, None)
        if entry is not None:
            payload_str, expires_at = entry
            if time.monotonic() < expires_at:
                raw = payload_str

    if raw is None:
        raise HTTPException(status_code=400, detail="Invalid, expired, or already used OAuth state")

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    try:
        record = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed OAuth state record") from exc

    stored_nonce = str(record.get("nonce") or "")
    stored_provider = str(record.get("provider") or "")
    stored_mode = str(record.get("mode") or "")
    stored_user_id = str(record.get("user_id") or "")

    if (
        not stored_nonce
        or not hmac.compare_digest(stored_nonce, state)
        or stored_provider != provider
        or stored_mode not in expected_modes
        or not stored_user_id
    ):
        raise HTTPException(status_code=403, detail="OAuth state context mismatch")

    return {"mode": stored_mode, "user_id": stored_user_id, "provider": stored_provider}


# =========================================================================
# RESPONSE MODELS
# =========================================================================


class ConnectionStatus(BaseModel):
    """Google Drive connection status."""

    connected: bool
    configured: bool
    root_folder_id: str | None = None


class SystemConnectionStatus(BaseModel):
    """System-wide Google Drive connection status."""

    oauth_connected: bool
    configured: bool
    connected_as: str | None = None  # Email of connected account
    root_folder_id: str | None = None


class FileItem(BaseModel):
    """File/folder item from Google Drive."""

    id: str
    name: str
    mime_type: str
    size: int | None = None
    modified_time: str | None = None
    icon_link: str | None = None
    web_view_link: str | None = None
    thumbnail_link: str | None = None
    is_folder: bool = False


class FileListResponse(BaseModel):
    """Response for file listing."""

    files: list[FileItem]
    next_page_token: str | None = None
    breadcrumb: list[dict[str, str]] = []


# =========================================================================
# OAUTH ENDPOINTS
# =========================================================================


@router.get("/status")
async def get_connection_status(
    current_user: dict = Depends(get_current_user),
    db_pool=Depends(get_database_pool),
) -> ConnectionStatus:
    """
    Check if user has connected Google Drive.
    """
    service = GoogleDriveService(db_pool)

    return ConnectionStatus(
        connected=await service.is_connected(_resolve_user_id(current_user)),
        configured=service.is_configured(),
        root_folder_id=service.root_folder_id,
    )


@router.get("/auth/url")
async def get_auth_url(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db_pool=Depends(get_database_pool),
) -> dict[str, str]:
    """
    Get OAuth authorization URL for connecting Google Drive.

    Returns URL to redirect user to for Google consent.
    """
    service = GoogleDriveService(db_pool)

    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Google Drive OAuth not configured. Contact administrator.",
        )

    user_id = _resolve_user_id(current_user)
    state = await issue_oauth_state("google_drive", mode="user", user_id=user_id)
    auth_url = service.get_authorization_url(state)

    return {"auth_url": auth_url, "state": state}


@router.get("/callback")
async def oauth_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db_pool=Depends(get_database_pool),
) -> RedirectResponse:
    """
    OAuth callback endpoint.

    Google redirects here after user grants consent.
    """
    frontend_url = "https://kita.balizero.com"

    if error:
        logger.error("[GDRIVE] OAuth error received in callback")
        return RedirectResponse(
            url=f"{frontend_url}/settings/integrations?{urlencode({'error': 'oauth_denied'})}",
            status_code=302,
        )

    if not isinstance(code, str) or not code or not isinstance(state, str) or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code or state")

    record = await consume_oauth_state(
        "google_drive",
        state,
        expected_modes=("user", "system", "admin"),
    )
    is_system_flow = record["mode"] in ("system", "admin")
    target_user_id = (
        GoogleDriveService.SYSTEM_USER_ID if is_system_flow else record["user_id"]
    )

    service = GoogleDriveService(db_pool)

    try:
        await service.exchange_code(code, target_user_id)
        success_flag = (
            "system_drive_connected" if is_system_flow else "google_drive_connected"
        )
        logger.info("[GDRIVE] OAuth token stored (system_flow=%s)", is_system_flow)
        return RedirectResponse(
            url=f"{frontend_url}/settings/integrations?success={success_flag}",
            status_code=302,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[GDRIVE] Token exchange failed: %s", e)
        error_flag = (
            "system_token_failed" if is_system_flow else "token_exchange_failed"
        )
        return RedirectResponse(
            url=f"{frontend_url}/settings/integrations?error={error_flag}",
            status_code=302,
        )


@router.post("/disconnect")
async def disconnect(
    current_user: dict = Depends(get_current_user),
    db_pool=Depends(get_database_pool),
) -> dict[str, bool]:
    """
    Disconnect Google Drive for current user.
    """
    service = GoogleDriveService(db_pool)
    success = await service.disconnect(_resolve_user_id(current_user))
    return {"success": success}


# =========================================================================
# SYSTEM-WIDE OAUTH (for admin to connect the owner's 30TB system account)
# =========================================================================


@router.get("/system/status")
async def get_system_status(
    _admin_user: dict = Depends(get_admin_user),
    db_pool=Depends(get_database_pool),
) -> SystemConnectionStatus:
    """
    Check if system-wide Google Drive OAuth is connected (Admin only).
    """
    service = GoogleDriveService(db_pool)

    # Check if SYSTEM token exists
    token = await service.get_valid_token(GoogleDriveService.SYSTEM_USER_ID)
    is_connected = token is not None

    # Get connected account email if available
    connected_as = None
    if is_connected:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://www.googleapis.com/drive/v3/about",
                    params={"fields": "user(emailAddress,displayName)"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 200:
                    data = response.json()
                    connected_as = data.get("user", {}).get("emailAddress")
        except Exception as e:
            logger.warning("[GDRIVE] Could not get connected account info: %s", e)

    return SystemConnectionStatus(
        oauth_connected=is_connected,
        configured=service.is_configured(),
        connected_as=connected_as,
        root_folder_id=service.root_folder_id,
    )


@router.get("/system/authorize")
async def get_system_auth_url(
    current_user: dict = Depends(get_admin_user),
    db_pool=Depends(get_database_pool),
) -> dict[str, str]:
    """
    Get OAuth authorization URL for connecting SYSTEM Google Drive.

    ADMIN ONLY: Only super admins can authorize.
    This connects the owner's 30TB system account for all team members.
    """
    if not is_super_admin(current_user):
        raise HTTPException(
            status_code=403,
            detail="Solo gli admin possono connettere il Drive di sistema",
        )

    service = GoogleDriveService(db_pool)

    if not service.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Google Drive OAuth non configurato. Imposta GOOGLE_DRIVE_CLIENT_ID e GOOGLE_DRIVE_CLIENT_SECRET.",
        )

    state = await issue_oauth_state(
        "google_drive",
        mode="system",
        user_id=GoogleDriveService.SYSTEM_USER_ID,
    )
    auth_url = service.get_authorization_url(state)

    logger.info("[GDRIVE] Super-admin requested SYSTEM OAuth URL")
    return {"auth_url": auth_url, "state": state}


@router.post("/system/disconnect")
async def disconnect_system(
    current_user: dict = Depends(get_admin_user),
    db_pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Disconnect system-wide Google Drive OAuth.

    ADMIN ONLY: Only admin emails can disconnect the SYSTEM account.
    """
    if not is_super_admin(current_user):
        raise HTTPException(
            status_code=403,
            detail="Solo gli admin possono disconnettere il Drive di sistema",
        )

    service = GoogleDriveService(db_pool)
    success = await service.disconnect(GoogleDriveService.SYSTEM_USER_ID)

    logger.info("[GDRIVE] Super-admin disconnected SYSTEM OAuth")
    return {"success": success, "message": "Sistema Google Drive disconnesso"}


# =========================================================================
# FILE OPERATIONS (ADMIN ONLY — raw SYSTEM Drive view)
# =========================================================================


@router.get("/files")
async def list_files(
    folder_id: str | None = Query(None, description="Folder ID (default: root)"),
    page_token: str | None = Query(None, description="Pagination token"),
    page_size: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_admin_user),
    db_pool=Depends(get_database_pool),
) -> FileListResponse:
    """
    List files in a folder (Admin only on direct SYSTEM Drive router).
    """
    service = GoogleDriveService(db_pool)

    if not await service.is_connected("SYSTEM"):
        raise HTTPException(
            status_code=401,
            detail="Google Drive not connected. Please connect first.",
        )

    try:
        result = await service.list_files(
            user_id="SYSTEM",
            folder_id=folder_id,
            page_token=page_token,
            page_size=page_size,
        )

        # Convert to response model
        files = [
            FileItem(
                id=f["id"],
                name=f["name"],
                mime_type=f["mimeType"],
                size=f.get("size"),
                modified_time=f.get("modifiedTime"),
                icon_link=f.get("iconLink"),
                web_view_link=f.get("webViewLink"),
                thumbnail_link=f.get("thumbnailLink"),
                is_folder=f["mimeType"] == "application/vnd.google-apps.folder",
            )
            for f in result["files"]
        ]

        # Get breadcrumb path
        breadcrumb = []
        if folder_id:
            actor_id = str(current_user.get("user_id") or current_user.get("id") or "SYSTEM")
            breadcrumb = await service.get_folder_path(actor_id, folder_id)

        return FileListResponse(
            files=files,
            next_page_token=result.get("next_page_token"),
            breadcrumb=breadcrumb,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/files/{file_id}")
async def get_file(
    file_id: str,
    current_user: dict = Depends(get_admin_user),
    db_pool=Depends(get_database_pool),
) -> FileItem:
    """
    Get file metadata (Admin only on direct SYSTEM Drive router).
    """
    service = GoogleDriveService(db_pool)

    if not await service.is_connected("SYSTEM"):
        raise HTTPException(status_code=401, detail="Google Drive not connected")

    try:
        f = await service.get_file("SYSTEM", file_id)

        return FileItem(
            id=f["id"],
            name=f["name"],
            mime_type=f["mimeType"],
            size=f.get("size"),
            modified_time=f.get("modifiedTime"),
            icon_link=f.get("iconLink"),
            web_view_link=f.get("webViewLink"),
            thumbnail_link=f.get("thumbnailLink"),
            is_folder=f["mimeType"] == "application/vnd.google-apps.folder",
        )

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.get("/search")
async def search_files(
    q: str = Query(..., min_length=2, description="Search query"),
    page_size: int = Query(20, ge=1, le=50),
    current_user: dict = Depends(get_admin_user),
    db_pool=Depends(get_database_pool),
) -> list[FileItem]:
    """
    Search files by name (Admin only on direct SYSTEM Drive router).
    """
    service = GoogleDriveService(db_pool)

    if not await service.is_connected("SYSTEM"):
        raise HTTPException(status_code=401, detail="Google Drive not connected")

    files = await service.search_files(
        user_id="SYSTEM",
        query=q,
        page_size=page_size,
    )

    return [
        FileItem(
            id=f["id"],
            name=f["name"],
            mime_type=f["mimeType"],
            size=f.get("size"),
            modified_time=f.get("modifiedTime"),
            icon_link=f.get("iconLink"),
            web_view_link=f.get("webViewLink"),
            is_folder=f["mimeType"] == "application/vnd.google-apps.folder",
        )
        for f in files
    ]


@router.get("/my-folder")
async def get_my_folder(
    current_user: dict = Depends(get_current_user),
    db_pool=Depends(get_database_pool),
) -> dict[str, Any] | None:
    """
    Get current user's personal folder.

    Returns the folder matching user's name in the team structure.
    """
    service = GoogleDriveService(db_pool)

    if not await service.is_connected("SYSTEM"):
        raise HTTPException(status_code=401, detail="Google Drive not connected")

    folder = await service.get_user_folder(
        user_id="SYSTEM",
        user_email=current_user["email"],
    )

    if not folder:
        return {"found": False, "message": "Personal folder not found"}

    return {
        "found": True,
        "folder_id": folder["id"],
        "folder_name": folder["name"],
    }
