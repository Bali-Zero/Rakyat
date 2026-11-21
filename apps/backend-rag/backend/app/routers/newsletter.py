"""
Newsletter Router
Handles newsletter subscriptions for Bali Zero Insights blog

Endpoints:
- POST /api/blog/newsletter/subscribe - Subscribe to newsletter
- POST /api/blog/newsletter/confirm - Confirm subscription
- POST /api/blog/newsletter/unsubscribe - Unsubscribe
- PATCH /api/blog/newsletter/preferences - Update preferences
- GET /api/blog/newsletter/subscribers - List subscribers (admin)
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, field_validator

from backend.app.dependencies import get_database_pool
from backend.app.deps.auth import get_current_user_optional
from backend.app.routers.team_activity import get_admin_user
from backend.app.services.internal_email import send_internal_email
from backend.app.utils.crm_utils import is_crm_admin
from backend.app.utils.logging_utils import get_logger
from backend.core.cache import invalidate_cache

logger = get_logger(__name__)

router = APIRouter(prefix="/api/blog/newsletter", tags=["newsletter"])


def generate_newsletter_token(
    *,
    subscriber_id: str | None = None,
    email: str | None = None,
) -> str:
    """Generate an HMAC-SHA256 subscriber action token (unsubscribe / preferences)."""
    from backend.app.core.config import settings

    secret = (settings.jwt_secret_key or "").encode()
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY is required for newsletter token signing")
    if subscriber_id:
        msg = f"newsletter:v1:id:{subscriber_id}".encode()
    elif email:
        msg = f"newsletter:v1:email:{email.strip().lower()}".encode()
    else:
        raise ValueError("subscriber_id or email is required")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _verify_subscriber_token(
    token: str | None,
    *,
    subscriber_id: str | None,
    email: str | None,
    confirmation_token: str | None = None,
) -> bool:
    """Constant-time verification of HMAC subscriber token or DB confirmation_token."""
    if not token or not isinstance(token, str) or not token.strip():
        return False
    provided = token.strip()
    if subscriber_id:
        expected_id = generate_newsletter_token(subscriber_id=str(subscriber_id))
        if secrets.compare_digest(provided, expected_id):
            return True
    if email:
        expected_email = generate_newsletter_token(email=email)
        if secrets.compare_digest(provided, expected_email):
            return True
    if confirmation_token and secrets.compare_digest(provided, str(confirmation_token)):
        return True
    return False


# ---------------------------------------------------------------------------
# Double-opt-in confirmation email helper (TODO #80)
# ---------------------------------------------------------------------------


async def send_confirmation_email(
    *,
    email: str,
    token: str,
) -> None:
    """Send the double-opt-in confirmation email through the internal
    Brevo adapter.

    Closes TODO(#80). The sender (from=zantara@balizero.com, name=Zantara)
    is configured centrally in :mod:`backend.app.services.internal_email`
    via the ``/api/notifications/send-email`` endpoint — do NOT
    re-implement the sender here.

    Fire-and-forget: any Brevo failure is swallowed and logged so the
    surrounding ``subscribe()`` call still returns 200 to the client.
    The subscriber row is already persisted with a fresh token; admin
    can trigger a manual re-send via the existing
    resend-confirmation path.
    """
    base = os.getenv("PUBLIC_HOST", "https://balizero.com").rstrip("/")
    safe_token = quote(token, safe="")
    confirm_link = f"{base}/api/blog/newsletter/confirm?token={safe_token}"

    subject = "Confirm your Bali Zero Insights subscription"
    body = f"""
    <p>Hi{" there" if not email else ""},</p>
    <p>Thanks for subscribing to <strong>Bali Zero Insights</strong>.</p>
    <p>Please confirm your email to start receiving our editorial
    updates by clicking the link below:</p>
    <p><a href="{confirm_link}"
          style="display:inline-block;padding:12px 24px;
                 background-color:#0a0a0a;color:#ffffff;
                 text-decoration:none;border-radius:4px;">Confirm subscription</a></p>
    <p>If the button does not work, copy this link into your browser:</p>
    <p><a href="{confirm_link}">{confirm_link}</a></p>
    <hr/>
    <p style="font-size:12px;color:#666;">
      Terima kasih telah berlangganan. Klik tautan di atas untuk mengonfirmasi
      langganan Anda.
    </p>
    <p style="font-size:12px;color:#666;">
      You received this email because someone (hopefully you) entered this
      address on balizero.com. If this wasn't you, simply ignore this email
      — your address will not be subscribed.
    </p>
    """

    try:
        await send_internal_email(
            to=email,
            subject=subject,
            body=body,
            log_context="newsletter-double-optin",
        )
    except Exception as e:
        logger.warning(
            "Newsletter confirmation email failed (swallowed): %s",
            e,
        )


# ============================================================================
# Pydantic Models
# ============================================================================


class SubscribeRequest(BaseModel):
    """Subscribe request model"""

    email: EmailStr
    name: str | None = None
    categories: list[str] = []
    frequency: str = "weekly"
    language: str = "en"

    @field_validator("frequency")
    @classmethod
    def validate_frequency(cls, v: str) -> str:
        allowed = ["instant", "daily", "weekly", "monthly"]
        if v not in allowed:
            raise ValueError(f"frequency must be one of: {allowed}")
        return v


class SubscribeResponse(BaseModel):
    """Subscribe response model"""

    success: bool
    message: str
    subscriberId: str | None = None


class ConfirmRequest(BaseModel):
    """Confirm subscription request"""

    subscriberId: str
    token: str


class UnsubscribeRequest(BaseModel):
    """Unsubscribe request"""

    subscriberId: str | None = None
    email: str | None = None
    token: str | None = None


class PreferencesRequest(BaseModel):
    """Update preferences request"""

    subscriberId: str | None = None
    email: str | None = None
    token: str | None = None
    categories: list[str] | None = None
    frequency: str | None = None
    language: str | None = None


class SubscriberResponse(BaseModel):
    """Subscriber response model"""

    id: str
    email: str
    name: str | None = None
    categories: list[str] = []
    frequency: str = "weekly"
    language: str = "en"
    confirmed: bool = False
    created_at: datetime | None = None


# ============================================================================
# Endpoints
# ============================================================================


@router.post("/subscribe", response_model=SubscribeResponse)
async def subscribe(
    request: SubscribeRequest,
    pool=Depends(get_database_pool),
) -> SubscribeResponse:
    """
    Subscribe to the newsletter.
    Creates a new subscriber and sends confirmation email.
    """
    async with pool.acquire() as conn:
        # Check if email already exists
        existing = await conn.fetchrow(
            "SELECT id, confirmed, unsubscribed_at FROM newsletter_subscribers WHERE email = $1",
            request.email,
        )

        if existing:
            if existing["confirmed"] and not existing["unsubscribed_at"]:
                # Already confirmed and active
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "This email is already subscribed",
                        "code": "ALREADY_SUBSCRIBED",
                    },
                )
            if existing["unsubscribed_at"]:
                # Resubscribing
                confirmation_token = secrets.token_urlsafe(32)
                await conn.execute(
                    """
                    UPDATE newsletter_subscribers
                    SET confirmed = FALSE,
                        unsubscribed_at = NULL,
                        confirmation_token = $2,
                        confirmation_sent_at = NOW(),
                        categories = $3,
                        frequency = $4,
                        language = $5,
                        name = $6,
                        updated_at = NOW()
                    WHERE email = $1
                    """,
                    request.email,
                    confirmation_token,
                    request.categories,
                    request.frequency,
                    request.language,
                    request.name,
                )
                logger.info(f"Resubscribed: {request.email}")
                await invalidate_cache("zantara:newsletter:*")
                # TODO(#80) closed: double-opt-in confirmation email via Brevo.
                await send_confirmation_email(
                    email=request.email,
                    token=confirmation_token,
                )
                return SubscribeResponse(
                    success=True,
                    message="Please check your email to confirm your subscription.",
                    subscriberId=existing["id"],
                )
            # Exists but not confirmed - resend confirmation
            confirmation_token = secrets.token_urlsafe(32)
            await conn.execute(
                """
                    UPDATE newsletter_subscribers
                    SET confirmation_token = $2,
                        confirmation_sent_at = NOW(),
                        categories = $3,
                        frequency = $4,
                        language = $5,
                        name = $6,
                        updated_at = NOW()
                    WHERE email = $1
                    """,
                request.email,
                confirmation_token,
                request.categories,
                request.frequency,
                request.language,
                request.name,
            )
            logger.info(f"Resent confirmation: {request.email}")
            await invalidate_cache("zantara:newsletter:*")
            # TODO(#80) closed: double-opt-in confirmation email via Brevo.
            await send_confirmation_email(
                email=request.email,
                token=confirmation_token,
            )
            return SubscribeResponse(
                success=True,
                message="Confirmation email resent. Please check your inbox.",
                subscriberId=existing["id"],
            )

        # New subscriber
        confirmation_token = secrets.token_urlsafe(32)
        row = await conn.fetchrow(
            """
            INSERT INTO newsletter_subscribers (
                email, name, categories, frequency, language,
                confirmation_token, confirmation_sent_at, source
            ) VALUES ($1, $2, $3, $4, $5, $6, NOW(), 'blog')
            RETURNING id
            """,
            request.email,
            request.name,
            request.categories,
            request.frequency,
            request.language,
            confirmation_token,
        )

        subscriber_id = row["id"]
        logger.info(f"New subscriber: {request.email} (ID: {subscriber_id})")
        await invalidate_cache("zantara:newsletter:*")

        # TODO(#80) closed: double-opt-in confirmation email via Brevo
        # (internal /api/notifications/send-email adapter sets from=zantara@balizero.com).
        await send_confirmation_email(
            email=request.email,
            token=confirmation_token,
        )

        return SubscribeResponse(
            success=True,
            message="Please check your email to confirm your subscription.",
            subscriberId=subscriber_id,
        )


@router.post("/confirm")
async def confirm_subscription(
    request: ConfirmRequest,
    pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Confirm a newsletter subscription using the token from email.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, confirmed FROM newsletter_subscribers
            WHERE id = $1 AND confirmation_token = $2
            """,
            request.subscriberId,
            request.token,
        )

        if not row:
            raise HTTPException(status_code=404, detail="Invalid confirmation link")

        if row["confirmed"]:
            return {"success": True, "message": "Subscription already confirmed"}

        await conn.execute(
            """
            UPDATE newsletter_subscribers
            SET confirmed = TRUE,
                confirmed_at = NOW(),
                confirmation_token = NULL,
                updated_at = NOW()
            WHERE id = $1
            """,
            request.subscriberId,
        )

        logger.info(f"Confirmed subscription: {row['email']}")
        await invalidate_cache("zantara:newsletter:*")
        return {"success": True, "message": "Subscription confirmed successfully"}


@router.get("/confirm")
async def confirm_subscription_via_link(
    token: str = Query(..., description="Confirmation token from the email"),
    pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """Token-only confirm endpoint for the email-link flow.

    The double-opt-in email (TODO #80) sends a link containing only the
    token — the mouth frontend redirect (`apps/mouth/src/app/api/blog/
    newsletter/confirm/route.ts`) forwards that link here. The POST
    variant above still works for clients that have both subscriberId
    and token.

    Confirmation tokens are 256-bit URL-safe random strings
    (``secrets.token_urlsafe(32)`` ~43 chars) and migration 179 enforces
    a partial unique index, so token-only lookup is collision-safe.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, confirmed FROM newsletter_subscribers
            WHERE confirmation_token = $1
            """,
            token,
        )

        if not row:
            raise HTTPException(status_code=404, detail="Invalid or expired confirmation link")

        if row["confirmed"]:
            return {"success": True, "message": "Subscription already confirmed"}

        await conn.execute(
            """
            UPDATE newsletter_subscribers
            SET confirmed = TRUE,
                confirmed_at = NOW(),
                confirmation_token = NULL,
                updated_at = NOW()
            WHERE id = $1
            """,
            row["id"],
        )

        logger.info(f"Confirmed subscription via link: {row['email']}")
        await invalidate_cache("zantara:newsletter:*")
        return {"success": True, "message": "Subscription confirmed successfully"}


@router.post("/unsubscribe")
async def unsubscribe(
    request: UnsubscribeRequest,
    current_user: dict[str, Any] | None = Depends(get_current_user_optional),
    pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Unsubscribe from the newsletter (requires valid subscriber token or admin/owner auth).
    """
    has_token = bool(request.token and request.token.strip())
    if current_user is None and not has_token:
        raise HTTPException(status_code=401, detail="Unsubscribe token required")
    if not request.subscriberId and not request.email:
        raise HTTPException(status_code=400, detail="Email or subscriberId required")

    is_admin = bool(current_user and is_crm_admin(current_user))

    async with pool.acquire() as conn:
        if request.subscriberId:
            row = await conn.fetchrow(
                "SELECT id, email, confirmation_token FROM newsletter_subscribers WHERE id = $1",
                request.subscriberId,
            )
        else:
            row = await conn.fetchrow(
                "SELECT id, email, confirmation_token FROM newsletter_subscribers WHERE email = $1",
                request.email,
            )

        if not row:
            if is_admin:
                raise HTTPException(status_code=404, detail="Subscriber not found")
            raise HTTPException(status_code=403, detail="Invalid subscriber token")

        row_data = dict(row)
        token_valid = _verify_subscriber_token(
            request.token,
            subscriber_id=str(row_data["id"]),
            email=row_data.get("email"),
            confirmation_token=row_data.get("confirmation_token"),
        )
        owner_match = (
            not has_token
            and current_user is not None
            and (current_user.get("email") or "").strip().lower()
            == (row_data.get("email") or "").strip().lower()
        )
        if not (is_admin or token_valid or owner_match):
            raise HTTPException(status_code=403, detail="Invalid subscriber token")

        await conn.execute(
            """
            UPDATE newsletter_subscribers
            SET unsubscribed_at = NOW(), updated_at = NOW()
            WHERE id = $1
            """,
            row_data["id"],
        )

        logger.info("Unsubscribed subscriber id=%s", row_data["id"])
        await invalidate_cache("zantara:newsletter:*")
        return {"success": True, "message": "Successfully unsubscribed"}


@router.patch("/preferences")
async def update_preferences(
    request: PreferencesRequest,
    current_user: dict[str, Any] | None = Depends(get_current_user_optional),
    pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Update newsletter preferences (requires valid subscriber token or admin/owner auth).
    """
    has_token = bool(request.token and request.token.strip())
    if current_user is None and not has_token:
        raise HTTPException(status_code=401, detail="Subscriber token required")
    if not request.subscriberId and not request.email:
        raise HTTPException(status_code=400, detail="Email or subscriberId required")

    is_admin = bool(current_user and is_crm_admin(current_user))

    async with pool.acquire() as conn:
        if request.subscriberId:
            row = await conn.fetchrow(
                "SELECT id, email, confirmation_token FROM newsletter_subscribers WHERE id = $1",
                request.subscriberId,
            )
        else:
            row = await conn.fetchrow(
                "SELECT id, email, confirmation_token FROM newsletter_subscribers WHERE email = $1",
                request.email,
            )

        if not row:
            if is_admin:
                raise HTTPException(status_code=404, detail="Subscriber not found")
            raise HTTPException(status_code=403, detail="Invalid subscriber token")

        row_data = dict(row)
        token_valid = _verify_subscriber_token(
            request.token,
            subscriber_id=str(row_data["id"]),
            email=row_data.get("email"),
            confirmation_token=row_data.get("confirmation_token"),
        )
        owner_match = (
            not has_token
            and current_user is not None
            and (current_user.get("email") or "").strip().lower()
            == (row_data.get("email") or "").strip().lower()
        )
        if not (is_admin or token_valid or owner_match):
            raise HTTPException(status_code=403, detail="Invalid subscriber token")

        # Build dynamic update
        updates = []
        params = [row_data["id"]]
        param_idx = 2

        if request.categories is not None:
            updates.append(f"categories = ${param_idx}")
            params.append(request.categories)
            param_idx += 1

        if request.frequency is not None:
            updates.append(f"frequency = ${param_idx}")
            params.append(request.frequency)
            param_idx += 1

        if request.language is not None:
            updates.append(f"language = ${param_idx}")
            params.append(request.language)
            param_idx += 1

        if not updates:
            return {"success": True, "message": "No changes"}

        updates.append("updated_at = NOW()")
        query = f"UPDATE newsletter_subscribers SET {', '.join(updates)} WHERE id = $1"
        await conn.execute(query, *params)

        logger.info("Updated preferences for subscriber: %s", row_data["id"])
        await invalidate_cache("zantara:newsletter:*")
        return {"success": True, "message": "Preferences updated"}


@router.get("/subscribers")
async def list_subscribers(
    category: str | None = Query(None),
    frequency: str | None = Query(None),
    confirmed: bool | None = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    _admin: dict[str, Any] = Depends(get_admin_user),
    pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """
    List newsletter subscribers (admin endpoint).
    """
    async with pool.acquire() as conn:
        # Build query with filters
        conditions = ["unsubscribed_at IS NULL"]
        params = []
        param_idx = 1

        if category:
            conditions.append(f"${param_idx} = ANY(categories)")
            params.append(category)
            param_idx += 1

        if frequency:
            conditions.append(f"frequency = ${param_idx}")
            params.append(frequency)
            param_idx += 1

        if confirmed is not None:
            conditions.append(f"confirmed = ${param_idx}")
            params.append(confirmed)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        params.extend([limit, offset])

        query = f"""
            SELECT id, email, name, categories, frequency, language, confirmed, created_at
            FROM newsletter_subscribers
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """

        rows = await conn.fetch(query, *params)

        # Get total count
        count_query = f"SELECT COUNT(*) FROM newsletter_subscribers WHERE {where_clause}"
        total = await conn.fetchval(count_query, *params[:-2])

        subscribers = [
            SubscriberResponse(
                id=row["id"],
                email=row["email"],
                name=row["name"],
                categories=row["categories"] or [],
                frequency=row["frequency"] or "weekly",
                language=row["language"] or "en",
                confirmed=row["confirmed"] or False,
                created_at=row["created_at"],
            )
            for row in rows
        ]

        return {
            "subscribers": [s.model_dump() for s in subscribers],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


class NewsletterLogRequest(BaseModel):
    """Newsletter send log payload (supports both snake_case and camelCase)."""

    model_config = ConfigDict(populate_by_name=True)

    article_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("article_id", "articleId"),
    )
    recipient_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("recipient_count", "recipientCount"),
    )
    sent_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("sent_count", "sentCount"),
    )
    failed_count: int | None = Field(
        default=None,
        validation_alias=AliasChoices("failed_count", "failedCount"),
    )


@router.post("/log")
async def log_newsletter_send(
    article_id: str | None = Query(default=None),
    recipient_count: int | None = Query(default=None),
    sent_count: int | None = Query(default=None),
    failed_count: int | None = Query(default=None),
    payload: NewsletterLogRequest | None = Body(default=None),
    _admin: dict[str, Any] = Depends(get_admin_user),
    pool=Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Log a newsletter send event (admin endpoint).
    Accepts legacy snake_case query parameters or JSON body (camelCase or snake_case).
    """
    resolved_article_id = (
        article_id if article_id is not None else (payload.article_id if payload else None)
    )
    resolved_recipient_count = (
        recipient_count
        if recipient_count is not None
        else (payload.recipient_count if payload else None)
    )
    resolved_sent_count = (
        sent_count if sent_count is not None else (payload.sent_count if payload else None)
    )
    resolved_failed_count = (
        failed_count if failed_count is not None else (payload.failed_count if payload else None)
    )

    missing = [
        name
        for name, val in (
            ("article_id", resolved_article_id),
            ("recipient_count", resolved_recipient_count),
            ("sent_count", resolved_sent_count),
            ("failed_count", resolved_failed_count),
        )
        if val is None or (name == "article_id" and not str(val).strip())
    ]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required newsletter log fields: {', '.join(missing)}",
        )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO newsletter_send_log (article_id, recipient_count, sent_count, failed_count)
            VALUES ($1, $2, $3, $4)
            """,
            resolved_article_id,
            resolved_recipient_count,
            resolved_sent_count,
            resolved_failed_count,
        )

        await invalidate_cache("zantara:newsletter:*")
        return {"success": True}
