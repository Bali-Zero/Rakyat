from datetime import datetime, timezone
from typing import Any

import asyncpg

from backend.app.utils.logging_utils import get_logger
from backend.app.utils.logging_utils import log_success_simple as log_success
from backend.db.repositories.conversation_repository import (
    ConversationCommitOutcomeUnknown,
    conversation_write_connection,
    lock_conversation_session,
)
from backend.services.memory.memory_fallback import get_memory_cache, user_session_key

logger = get_logger(__name__)


class ConversationService:
    """
    Service for managing conversation persistence and retrieval.
    Handles saving to PostgreSQL and Memory Cache fallback.
    """

    def __init__(self, db_pool: asyncpg.Pool) -> None:
        self.db_pool = db_pool

    async def save_conversation(
        self,
        user_email: str,
        messages: list[dict[str, Any]],
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Save conversation messages to PostgreSQL and Memory Cache.
        Triggers Auto-CRM if successful.
        """
        if not session_id:
            session_id = f"session-{datetime.now(tz=timezone.utc).strftime('%Y%m%d-%H%M%S')}"

        metadata = metadata or {}

        conversation_id = 0
        db_success = False

        try:
            if self.db_pool:
                async with conversation_write_connection(self.db_pool) as conn:
                    await lock_conversation_session(conn, session_id)
                    await conn.fetchrow(
                        """
                        SELECT id FROM conversations
                        WHERE session_id = $1
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1 FOR UPDATE
                        """,
                        session_id,
                    )
                    row = await conn.fetchrow(
                        """
                        INSERT INTO conversations
                            (user_id, session_id, messages, metadata, created_at)
                        VALUES ($1, $2, $3, $4, $5)
                        RETURNING id
                        """,
                        user_email,
                        session_id,
                        messages,
                        metadata,
                        datetime.now(tz=timezone.utc),
                    )
                conversation_id = row["id"] if row else 0
                db_success = True
                log_success("Saved conversation to DB", messages_count=len(messages))
            else:
                logger.warning("⚠️ DB Pool unavailable, skipping DB save")

        except ConversationCommitOutcomeUnknown:
            logger.error("DB save outcome unknown after commit; refusing blind retry")
            raise
        except Exception as e:
            logger.error("DB save failed: %s", type(e).__name__)
            if not db_success:
                logger.info("⚠️ Continuing with memory-only persistence")

        try:
            mem_cache = get_memory_cache()
            scoped_key = user_session_key(user_email, session_id)
            for msg in messages:
                mem_cache.add_message(
                    scoped_key,
                    msg.get("role", "unknown"),
                    msg.get("content", ""),
                )
            logger.info("Saved %s messages to memory cache", len(messages))
        except Exception as e:
            logger.warning("Failed to save to memory cache: %s", type(e).__name__)

        return {
            "success": True,
            "conversation_id": conversation_id,
            "messages_saved": len(messages),
            "user_email": user_email,
            "persistence_mode": "db" if db_success else "memory_fallback",
            "session_id": session_id,
        }

    async def get_history(
        self,
        user_email: str,
        limit: int = 20,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Retrieve conversation history from DB or Memory Cache.
        """
        messages = []
        source = "db"

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    if session_id:
                        row = await conn.fetchrow(
                            """
                            SELECT messages FROM conversations
                            WHERE user_id = $1 AND session_id = $2
                            ORDER BY created_at DESC LIMIT 1
                            """,
                            user_email,
                            session_id,
                        )
                    else:
                        row = await conn.fetchrow(
                            """
                            SELECT messages FROM conversations
                            WHERE user_id = $1
                            ORDER BY created_at DESC LIMIT 1
                            """,
                            user_email,
                        )

                    if row and row["messages"]:
                        messages = row["messages"]
                        if isinstance(messages, str):
                            import json

                            messages = json.loads(messages)
            except Exception as e:
                logger.error("Failed to fetch history from DB: %s", type(e).__name__)
                source = "fallback_failed"

        if not messages:
            try:
                mem_cache = get_memory_cache()
                if session_id:
                    cache_key = user_session_key(user_email, session_id)
                    reader = getattr(mem_cache, "get_conversation", None) or mem_cache.get_messages
                    cached = reader(cache_key)
                    if cached:
                        messages = cached
                        source = "memory_cache"
            except Exception as e:
                logger.warning("Failed to fetch history from memory cache: %s", type(e).__name__)

        return {
            "messages": messages[-limit:] if limit else messages,
            "source": source,
            "total": len(messages),
        }
