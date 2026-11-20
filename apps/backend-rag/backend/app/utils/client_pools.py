"""Shared client pools: staff groups whose members hold each other's client ownership."""

from __future__ import annotations

import logging
import time
from typing import Any

from backend.app.utils.service_accounts import is_human_team_member
from backend.core.env_lists import crm_client_pools

logger = logging.getLogger(__name__)

#: Pools that passed entity validation. ``None`` = not validated yet, or validation failed:
#: no pools apply (fail closed, today's behaviour). The environment is immutable for a
#: process (Fly restarts machines on a secrets change), so one successful run is final.
_VALIDATED: tuple[frozenset[str], ...] | None = None
_LAST_ATTEMPT: float | None = None
_RETRY_SECONDS = 60.0


async def validate_client_pools(conn: Any) -> None:
    """Accept the configured pools only if EVERY entry is an active human ``team_members`` row.

    One invalid entry anywhere refuses the whole config (the warning carries the variable
    name and a count, never a value). A DB error leaves the state at ``None`` (no pools).
    """
    global _VALIDATED
    pools = crm_client_pools()
    if not pools:
        _VALIDATED = ()
        return
    entries = sorted({email for pool in pools for email in pool})
    try:
        rows = await conn.fetch(
            "SELECT lower(email) AS email, role FROM team_members "
            "WHERE active AND lower(email) = ANY($1::text[])",
            entries,
        )
    except Exception as exc:
        _VALIDATED = None
        logger.warning(
            "CRM_CLIENT_POOLS_JSON validation failed (%s) - no pools", type(exc).__name__
        )
        return
    valid = {row["email"] for row in rows if is_human_team_member(row["role"])}
    invalid = [email for email in entries if email not in valid]
    if invalid:
        _VALIDATED = ()
        logger.warning(
            "CRM_CLIENT_POOLS_JSON has %d entries that are not active human staff - no pools",
            len(invalid),
        )
        return
    _VALIDATED = pools


async def validate_client_pools_on_pool(db_pool: Any) -> None:
    """Startup hook: validate with a connection from ``db_pool``; never raises."""
    try:
        async with db_pool.acquire() as conn:
            await validate_client_pools(conn)
    except Exception as exc:
        logger.warning(
            "CRM_CLIENT_POOLS_JSON validation skipped (%s) - no pools", type(exc).__name__
        )


async def ensure_client_pools_validated(conn: Any) -> None:
    """Lazy retry for a failed or missed startup validation: at most once per 60 s."""
    global _LAST_ATTEMPT
    if _VALIDATED is not None:
        return
    now = time.monotonic()
    if _LAST_ATTEMPT is not None and now - _LAST_ATTEMPT < _RETRY_SECONDS:
        return
    _LAST_ATTEMPT = now
    await validate_client_pools(conn)


def client_pool_peers(email: str | None) -> frozenset[str]:
    """Emails whose ownership ``email`` holds: itself plus every pool member sharing a pool."""
    own = (email or "").strip().lower()
    peers = {own}
    if own:
        for pool in _VALIDATED or ():
            if own in pool:
                peers |= pool
    return frozenset(peers)


def caller_pool_peers(current_user: dict[str, Any] | None) -> frozenset[str]:
    """Peers of the caller: pools expand only for a human team member; machines keep {own}."""
    user = current_user or {}
    if is_human_team_member(user.get("role")):
        return client_pool_peers(user.get("email"))
    return frozenset({(user.get("email") or "").strip().lower()})


def can_write_client(
    current_user: dict[str, Any], assigned_to: str | None, created_by: str | None
) -> bool:
    """Write rule for a client: admin, or the caller's peer set holds an owner (assigned_to / created_by)."""
    from backend.app.utils.crm_utils import is_crm_admin

    if is_crm_admin(current_user):
        return True
    owners = {(assigned_to or "").strip().lower(), (created_by or "").strip().lower()}
    owners.discard("")
    return bool(owners & caller_pool_peers(current_user))


def caller_in_client_pool(current_user: dict[str, Any] | None) -> bool:
    """True for a human team member whose email sits in a validated pool of two or more."""
    return len(caller_pool_peers(current_user)) > 1


def can_edit_company(
    current_user: dict[str, Any],
    company_created_by: str | None,
    linked_client_owners: Any,
) -> bool:
    """Edit rule for a company: admin exactly as ``_require_crm_admin``, else the caller's peers manage it.

    CRM admins: any identity ``is_crm_admin`` accepts, unchanged from before this rule. The
    human-only rule below applies to everyone else.

    Managing = the company's ``created_by``, or any linked client's ``assigned_to`` /
    ``created_by`` / ``tax_consultant``, is the caller or one of the caller's pool peers.
    Machine principals never edit.
    """
    from backend.app.utils.crm_utils import is_crm_admin

    if is_crm_admin(current_user):
        return True
    if not is_human_team_member((current_user or {}).get("role")):
        return False
    owners = {(company_created_by or "").strip().lower()}
    for row in linked_client_owners or ():
        for key in ("assigned_to", "created_by", "tax_consultant"):
            owners.add((row[key] or "").strip().lower())
    owners.discard("")
    return bool(owners & caller_pool_peers(current_user))


def can_manage_client_tax_documents(
    current_user: dict[str, Any],
    assigned_to: str | None,
    created_by: str | None,
    tax_consultant: str | None,
) -> bool:
    """Write rule for a client's Tax Documents (Drive): CRM admin, else an associated human team member.

    Associated = the caller or one of their validated client-pool peers is the client's
    ``assigned_to`` (Setup), ``tax_consultant``, or ``created_by``. Machine principals and
    portal/external roles never manage tax documents.
    """
    from backend.app.utils.crm_utils import is_crm_admin

    if is_crm_admin(current_user):
        return True
    if not is_human_team_member((current_user or {}).get("role")):
        return False
    owners = {
        (assigned_to or "").strip().lower(),
        (created_by or "").strip().lower(),
        (tax_consultant or "").strip().lower(),
    }
    owners.discard("")
    return bool(owners & caller_pool_peers(current_user))
