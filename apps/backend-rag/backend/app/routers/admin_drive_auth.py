"""
Admin Drive Auth - Endpoint to authenticate Google Drive system user.
Requires X-Admin-Secret header on initiation and a one-time server-side state nonce on callback.
"""

import hmac
import os
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from backend.app.core.config import settings
from backend.app.dependencies import get_database_pool
from backend.app.routers.google_drive import consume_oauth_state, issue_oauth_state
from backend.services.integrations.google_drive_service import GoogleDriveService

router = APIRouter()


@router.get("/admin/google-drive/auth-system")
async def auth_system_user(
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
) -> dict[str, Any]:
    """
    Authenticate Google Drive system user.

    Headers:
        X-Admin-Secret: Must match ADMIN_SECRET_KEY env var
    """
    admin_secret = os.environ.get("ADMIN_SECRET_KEY")
    if not admin_secret:
        raise HTTPException(status_code=500, detail="ADMIN_SECRET_KEY not configured")

    if (
        not isinstance(x_admin_secret, str)
        or not x_admin_secret
        or not x_admin_secret.isascii()
        or not admin_secret.isascii()
        or not hmac.compare_digest(x_admin_secret, admin_secret)
    ):
        raise HTTPException(status_code=401, detail="Invalid admin secret")

    client_id = settings.google_drive_client_id
    redirect_uri = settings.google_drive_redirect_uri

    if not client_id:
        raise HTTPException(status_code=500, detail="Google Drive not configured")

    state = await issue_oauth_state(
        "google_drive",
        mode="admin",
        user_id=GoogleDriveService.SYSTEM_USER_ID,
    )
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri or "",
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/drive",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"

    return {
        "message": "Visit this URL to authenticate system user",
        "auth_url": auth_url,
        "state": state,
        "instructions": [
            "1. Visit the auth_url in your browser",
            "2. Login with Google account",
            "3. Accept permissions",
            "4. If not auto-redirected, copy 'code' and 'state' from redirect URL",
            "5. Call: GET /admin/google-drive/callback-system?code=XXX&state=<state>",
        ],
    }


@router.get("/admin/google-drive/callback-system")
async def auth_callback_system(
    code: str | None = Query(None),
    state: str | None = Query(None),
    db_pool: Any = Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Handle OAuth callback for system user using a one-time server-side state nonce.
    """
    if not isinstance(code, str) or not code or not isinstance(state, str) or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code or state")

    record = await consume_oauth_state(
        "google_drive",
        state,
        expected_modes=("admin", "system"),
    )

    import asyncpg

    owned_pool = None
    pool = db_pool
    try:
        if pool is None or not hasattr(pool, "acquire"):
            owned_pool = await asyncpg.create_pool(
                os.environ["DATABASE_URL"], min_size=1, max_size=2
            )
            pool = owned_pool

        service = GoogleDriveService(pool)
        token_data = await service.exchange_code(code, record["user_id"])
        return {
            "success": True,
            "message": "System user authenticated successfully!",
            "expires_in": token_data.get("expires_in", 3600),
        }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if owned_pool is not None:
            await owned_pool.close()
