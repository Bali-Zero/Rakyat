"""Shared client pools: staff groups whose members hold each other's client ownership."""

from __future__ import annotations

from typing import Any

from backend.core.env_lists import crm_client_pools


def client_pool_peers(email: str | None) -> frozenset[str]:
    """Emails whose ownership ``email`` holds: itself plus every pool member sharing a pool."""
    own = (email or "").strip().lower()
    peers = {own}
    if own:
        for pool in crm_client_pools():
            if own in pool:
                peers |= pool
    return frozenset(peers)


def can_write_client(
    current_user: dict[str, Any], assigned_to: str | None, created_by: str | None
) -> bool:
    """Write rule for a client: admin, or the caller's peer set holds an owner (assigned_to / created_by)."""
    from backend.app.utils.crm_utils import is_crm_admin

    if is_crm_admin(current_user):
        return True
    owners = {(assigned_to or "").strip().lower(), (created_by or "").strip().lower()}
    owners.discard("")
    return bool(owners & client_pool_peers(current_user.get("email")))
