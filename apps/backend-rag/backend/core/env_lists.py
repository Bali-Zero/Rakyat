"""Identity lists supplied through the environment, never through git.

Each list is a JSON document held in an env var (a Fly secret in prod, the host secrets
file for operator scripts). An unset, blank or malformed value yields an EMPTY list: an
empty allowlist admits nobody, an empty alias list matches nothing. A malformed value is
logged by variable name only, never echoed (the content is personal data).
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

ENV_TEAM_NAME_ALIASES = "TEAM_NAME_ALIASES_JSON"
ENV_CRM_EXTRA_ADMIN_EMAILS = "CRM_EXTRA_ADMIN_EMAILS_JSON"
ENV_INTAKE_DRIVE_ROOT_EXTRA = "INTAKE_DRIVE_ROOT_EXTRA_JSON"
ENV_CRM_CLIENT_POOLS = "CRM_CLIENT_POOLS_JSON"
ENV_OWNER_TELEGRAM_CHAT_ID = "TELEGRAM_OWNER_CHAT_ID"


def env_json_str_list(name: str) -> list[str]:
    """Parse ``name`` as a JSON list of strings; ``[]`` when unset, blank or malformed."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("%s is not valid JSON - treated as empty", name)
        return []
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        logger.warning("%s is not a JSON list of strings - treated as empty", name)
        return []
    return [item for item in data if item.strip()]


def team_name_aliases() -> tuple[str, ...]:
    """Extra lowercase staff name tokens for the WhatsApp copilot heuristics."""
    return tuple(item.strip().lower() for item in env_json_str_list(ENV_TEAM_NAME_ALIASES))


def crm_extra_admin_emails() -> frozenset[str]:
    """CRM-domain admin emails on top of the global allowlist; empty = no extra admins."""
    return frozenset(item.strip().lower() for item in env_json_str_list(ENV_CRM_EXTRA_ADMIN_EMAILS))


def intake_drive_root_extra() -> frozenset[str]:
    """Drive root folder names added to the intake allowlist; empty = no extra roots."""
    return frozenset(item.strip() for item in env_json_str_list(ENV_INTAKE_DRIVE_ROOT_EXTRA))


def crm_client_pools() -> tuple[frozenset[str], ...]:
    """Parsed pool structure (a JSON list of lists of strings); unset/blank/malformed = no pools.

    Whether each entry is an active human staff member is decided against ``team_members`` by
    ``client_pools.validate_client_pools``, never by how the entry is spelled.
    """
    raw = os.environ.get(ENV_CRM_CLIENT_POOLS)
    if raw is None or not raw.strip():
        return ()
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("%s is not valid JSON - treated as empty", ENV_CRM_CLIENT_POOLS)
        return ()
    if not isinstance(data, list) or not all(
        isinstance(group, list) and all(isinstance(item, str) for item in group) for group in data
    ):
        logger.warning(
            "%s is not a JSON list of lists of strings - treated as empty", ENV_CRM_CLIENT_POOLS
        )
        return ()
    if not all(group and all(item.strip() for item in group) for group in data):
        logger.warning("%s holds an empty group or entry - treated as empty", ENV_CRM_CLIENT_POOLS)
        return ()
    return tuple(frozenset(item.strip().lower() for item in group) for group in data)


def owner_telegram_chat_id() -> str:
    """Owner Telegram chat id from the environment, read at call time; "" = not configured (callers skip the send)."""
    return os.environ.get(ENV_OWNER_TELEGRAM_CHAT_ID, "").strip()
