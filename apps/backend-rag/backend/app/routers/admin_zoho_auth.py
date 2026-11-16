"""Admin Zoho Auth - Endpoint to reconnect Zoho Invoice + Mail."""

import hmac
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from backend.app.core.config import settings
from backend.app.dependencies import get_database_pool
from backend.app.routers.google_drive import consume_oauth_state, issue_oauth_state
from backend.services.integrations.zoho_oauth_service import ZohoOAuthService

logger = logging.getLogger(__name__)
router = APIRouter()

ADMIN_ZOHO_USER_ID = "7dfe56b2-ff63-4d40-b78b-90c018127a02"


@router.get("/admin/zoho/auth")
async def admin_zoho_auth(
    x_admin_secret: str | None = Header(None, alias="X-Admin-Secret"),
) -> dict[str, Any]:
    """
    Get Zoho OAuth URL for admin reconnection.

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

    client_id = settings.zoho_client_id
    redirect_uri = settings.zoho_redirect_uri

    if not client_id:
        raise HTTPException(status_code=500, detail="Zoho not configured")

    # The scope comes from ZohoOAuthService.SCOPES, never from a list retyped
    # here, and that is the whole point of this line.
    scope = ",".join([*ZohoOAuthService.SCOPES, "ZohoInvoice.fullaccess.all"])
    state = await issue_oauth_state("zoho", mode="admin", user_id=ADMIN_ZOHO_USER_ID)

    auth_url = (
        f"https://accounts.zoho.com/oauth/v2/auth?"
        f"client_id={client_id}&"
        f"response_type=code&"
        f"scope={scope}&"
        f"redirect_uri={redirect_uri}&"
        f"access_type=offline&"
        f"prompt=consent&"
        f"state={state}"
    )

    return {
        "message": "Zoho re-consent. Read `client_type` first — the two flows are "
        "not interchangeable.",
        "scope": scope,
        "auth_url": auth_url,
        "state": state,
        "client_type_note": (
            "A SELF CLIENT has no redirect URI, so `auth_url` cannot work for it: "
            "the browser answers 'Invalid Redirect Uri' before any consent screen "
            "appears. Zoho hands the grant code straight to the operator in the "
            "API console instead. Check which kind this client is at "
            "https://api-console.zoho.com before using either flow."
        ),
        "instructions_self_client": [
            "1. Open https://api-console.zoho.com and pick this client",
            "2. Go to the 'Generate Code' tab",
            "3. Paste the `scope` value above verbatim into Scope",
            "4. Pick a duration (10 minutes is plenty) and Create",
            "5. Copy the code and exchange it server-side WITHOUT a redirect_uri "
            "— the code is single-use and expires in minutes",
        ],
        "instructions_web_client": [
            "1. Visit the auth_url in your browser",
            "2. Login with Zoho",
            "3. Accept ALL permissions (Invoice + Mail)",
            "4. If not auto-redirected, copy 'code' and 'state' from redirect URL",
            "5. Call: GET /admin/zoho/callback?code=XXX&state=<state>",
        ],
    }


@router.get("/admin/zoho/callback")
async def admin_zoho_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    db_pool: Any = Depends(get_database_pool),
) -> dict[str, Any]:
    """Handle Zoho OAuth callback using a one-time server-side state nonce."""
    if not isinstance(code, str) or not code or not isinstance(state, str) or not state:
        raise HTTPException(status_code=400, detail="Missing OAuth code or state")

    record = await consume_oauth_state("zoho", state, expected_modes=("admin",))

    import asyncpg

    owned_pool = None
    pool = db_pool
    try:
        if pool is None or not hasattr(pool, "acquire"):
            owned_pool = await asyncpg.create_pool(
                os.environ.get("DATABASE_URL"), min_size=1, max_size=2
            )
            pool = owned_pool

        oauth_service = ZohoOAuthService(pool)
        result = await oauth_service.exchange_code(
            code=code,
            user_id=record["user_id"],
        )

        return {
            "success": True,
            "message": "Zoho Invoice reconnected successfully!",
            "account": result.get("email"),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Zoho OAuth callback failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Zoho OAuth failed: {e!s}") from e
    finally:
        if owned_pool is not None:
            await owned_pool.close()
