"""Shared client pools: staff groups whose members hold each other's client ownership."""

from __future__ import annotations

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
