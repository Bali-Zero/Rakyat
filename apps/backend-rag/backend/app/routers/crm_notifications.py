"""
CRM Notifications Endpoints

GET /api/crm/notifications — returns notification_alerts for the current team member.
Used by the NotificationBell component in the CRM header.

Surfaces: portal document uploads, portal profile updates, WhatsApp clients who asked
to speak to a human, and future alert types from notification_alerts — see BELL_ALERT_TYPES.
"""

from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.deps.crm_access import get_crm_user_filter
from backend.app.utils.logging_utils import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/crm", tags=["crm-notifications"])

# The bell's guest list. A writer of `notification_alerts` whose alert_type is
# absent from this tuple writes a row nobody ever sees — the row exists, the
# notification does not. Kept as ONE constant read by both queries below so a
# new alert type cannot be admitted to the list and forgotten by the counter.
BELL_ALERT_TYPES: tuple[str, ...] = (
    "portal_document_upload",
    "portal_profile_update",
    "wa_human_handoff",
)


@router.get("/notifications")
async def get_crm_notifications(
    unread_only: bool = Query(False, description="Return only unread (pending) notifications"),
    limit: int = Query(50, le=200),
    pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """
    Return notification_alerts for the current team member.

    Filters by clients assigned_to the current user (or all clients for admins).
    Notifications are sorted newest first.
    """
    assigned_filter = get_crm_user_filter(current_user)

    async with pool.acquire() as conn:
        query = """
            SELECT
                na.id, na.client_id, na.alert_type, na.status,
                na.message, na.created_at, na.sent_at,
                c.full_name AS client_name, c.assigned_to
            FROM notification_alerts na
            JOIN clients c ON c.id = na.client_id AND c.deleted_at IS NULL
            WHERE na.alert_type = ANY($1::text[])
        """
        params: list = [list(BELL_ALERT_TYPES)]
        idx = 2

        if assigned_filter:
            query += f" AND LOWER(c.assigned_to) = ANY(${idx}::text[])"
            params.append(assigned_filter)
            idx += 1

        query += f" AND (${idx} = false OR na.status = 'pending')"
        params.append(unread_only)
        idx += 1

        query += f" ORDER BY na.created_at DESC LIMIT ${idx}"
        params.append(limit)

        rows = await conn.fetch(query, *params)

    return [
        {
            "id": r["id"],
            "client_id": r["client_id"],
            "client_name": r["client_name"],
            "alert_type": r["alert_type"],
            "status": r["status"],
            "message": r["message"],
            "created_at": r["created_at"].isoformat(),
            "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None,
        }
        for r in rows
    ]


@router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: int,
    pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Mark a bell notification as read (status = 'sent').

    Applies exactly the visibility of ``GET /notifications``: a BELL_ALERT_TYPES row of a
    live client the caller may see. Anything else — a delivery-queue row (email/WhatsApp
    alert not yet sent), another member's client, an unknown id — is a 404 and stays
    untouched: ``status = 'pending'`` is also the queue the notification service delivers.
    """
    assigned_filter = get_crm_user_filter(current_user)
    scope = ""
    params: list = [notification_id, list(BELL_ALERT_TYPES)]
    if assigned_filter:
        scope = " AND LOWER(c.assigned_to) = ANY($3::text[])"
        params.append(assigned_filter)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE notification_alerts na
                SET status = 'sent', sent_at = NOW()
                FROM clients c
                WHERE na.id = $1 AND na.status = 'pending'
                  AND na.alert_type = ANY($2::text[])
                  AND c.id = na.client_id AND c.deleted_at IS NULL{scope}
                RETURNING na.id, na.status
                """,
                *params,
            )
            visible = None
            if row is None:
                visible = await conn.fetchval(
                    f"""
                    SELECT 1 FROM notification_alerts na
                    JOIN clients c ON c.id = na.client_id AND c.deleted_at IS NULL
                    WHERE na.id = $1 AND na.alert_type = ANY($2::text[]){scope}
                    """,
                    *params,
                )
    except Exception as e:
        logger.error("Failed to mark notification %s as read: %s", notification_id, e)
        raise HTTPException(status_code=500, detail="Database error")
    if row:
        return {"id": row["id"], "status": row["status"]}
    if visible:
        return {"id": notification_id, "status": "already_read"}
    raise HTTPException(status_code=404, detail="Notification not found")


@router.get("/notifications/count")
async def get_unread_notifications_count(
    pool: asyncpg.Pool = Depends(get_database_pool),
    current_user: dict = Depends(get_current_user),
) -> dict[str, int]:
    """Return count of unread (pending) portal notifications for current user."""
    assigned_filter = get_crm_user_filter(current_user)

    async with pool.acquire() as conn:
        query = """
            SELECT COUNT(*)
            FROM notification_alerts na
            JOIN clients c ON c.id = na.client_id AND c.deleted_at IS NULL
            WHERE na.alert_type = ANY($1::text[])
              AND na.status = 'pending'
        """
        params: list = [list(BELL_ALERT_TYPES)]
        if assigned_filter:
            query += " AND LOWER(c.assigned_to) = ANY($2::text[])"
            params.append(assigned_filter)

        count = await conn.fetchval(query, *params)

    return {"unread": int(count or 0)}
