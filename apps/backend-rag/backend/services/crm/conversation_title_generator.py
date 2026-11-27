"""Backward-compat shim — all symbols moved to enrichment.py.

Note: generate_conversation_title is re-implemented here as a local wrapper
so that tests can patch _generate_via_ollama in this module's namespace.
"""

from __future__ import annotations

import logging

from backend.services.crm.enrichment import (  # noqa: F401
    _TITLE_PROMPT,
    _clean_title,
)
from backend.services.crm.enrichment import (
    _generate_title_via_ollama as _generate_via_ollama,
)

logger = logging.getLogger(__name__)


async def generate_conversation_title(
    conversation_id: str,
    first_user_message: str,
    max_length: int = 50,
) -> str | None:
    """Wrapper that delegates to module-local alias so tests can patch it."""
    if not first_user_message or len(first_user_message.strip()) < 10:
        logger.info(
            f"Skipping title generation for conv {conversation_id}: "
            f"message too short ({len(first_user_message)} chars)",
        )
        return None

    prompt = _TITLE_PROMPT.format(max_length=max_length, message=first_user_message[:200])

    title = await _generate_via_ollama(conversation_id, prompt, max_length)
    if title:
        return title

    logger.warning("All title generation methods failed for conv %s", conversation_id)
    return None
