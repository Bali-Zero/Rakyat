"""Which assigned practices the staff workspace may show. Client ownership is separate."""

import json
import logging
import os
from typing import Any

from fastapi import Depends, HTTPException, Path, Request

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.deps.owner import OWNER_EMAILS
from backend.app.utils.client_pools import HUMAN_SESSION_AUTH_METHODS
from backend.app.utils.service_accounts import TEAM_ROLES, is_human_team_member

logger = logging.getLogger(__name__)
_GRANTS_ENV = "PROCESS_VIEW_GRANTS_JSON"


def process_view_grant(user: dict | None) -> str:
    """Return the configured scope for a human staff identity, with personal access by default."""
    if not isinstance(user, dict) or not is_human_team_member(user.get("role")):
        return "none"
    if user.get("auth_method") not in HUMAN_SESSION_AUTH_METHODS | {None}:
        return "none"
    email = (user.get("email") or "").strip().lower()
    if not email:
        return "none"
    if email in OWNER_EMAILS:
        return "all"
    try:
        grants = json.loads(os.environ.get(_GRANTS_ENV) or "{}")
    except ValueError:
        logger.warning("%s is malformed", _GRANTS_ENV)
        return "own"
    if not isinstance(grants, dict) or not all(
        isinstance(key, str)
        and key == key.strip().lower()
        and key
        and value in ("all", "setup", "tax")
        for key, value in grants.items()
    ):
        return "own"
    return grants.get(email, "own")


async def process_assignees(conn: Any, user: dict | None) -> list[str] | None:
    """None means unrestricted. An empty list matches no assignment, including unassigned."""
    grant = process_view_grant(user)
    if grant == "none":
        return []
    email = user["email"].strip().lower()
    if email in OWNER_EMAILS:
        return None
    if grant == "own":
        return [email]
    # A configured delegation follows active human roster entries only. A deactivated lead
    # loses the delegation even if their session was minted before the roster change.
    rows = await conn.fetch(
        """SELECT LOWER(BTRIM(email)) AS email FROM team_members
           WHERE active AND LOWER(BTRIM(role)) = ANY($3::text[])
             AND (LOWER(BTRIM(email)) = $1 OR LOWER(BTRIM(department)) = $2)""",
        email,
        grant,
        sorted(TEAM_ROLES),
    )
    members = {r["email"] for r in rows}
    if email not in members:
        return []
    return None if grant == "all" else sorted(members)


def assignment_clause(owners: list[str] | None, params: list, column: str = "p.assigned_to") -> str:
    """Append a bound predicate using a trusted column expression."""
    if owners is None:
        return ""
    params.append(owners)
    return f" AND LOWER(BTRIM({column})) = ANY(${len(params)}::text[])"


async def require_visible_process(conn: Any, practice_id: int, user: dict) -> None:
    params = [practice_id]
    clause = assignment_clause(await process_assignees(conn, user), params)
    if not await conn.fetchval("SELECT 1 FROM practices p WHERE p.id=$1" + clause, *params):
        raise HTTPException(404, "Practice not found")


def process_session(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Preserve the authentication channel from trusted middleware for this policy."""
    method = getattr(request.state, "auth_type", None)
    return {**user, "auth_method": method} if method else user


async def visible_process_target(
    practice_id: int = Path(..., gt=0),
    user: dict = Depends(process_session),
    pool: Any = Depends(get_database_pool),
) -> None:
    """Check visibility at the HTTP entry before applying the independent write rules."""
    async with pool.acquire() as conn:
        await require_visible_process(conn, practice_id, user)
