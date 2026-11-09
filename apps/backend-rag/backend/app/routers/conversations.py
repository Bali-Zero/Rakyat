"""
ZANTARA Conversations Router
Endpoints for persistent conversation history with PostgreSQL
+ Auto-CRM population from conversations
+ Async LLM-generated conversation titles

SECURITY: All endpoints require JWT authentication (added 2025-12-03)
User identity is taken from JWT token, NOT from request parameters.

Refactored: Migrated to asyncpg with connection pooling (2025-12-07)
"""

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.utils.error_handlers import handle_database_error
from backend.app.utils.logging_utils import get_logger, log_error, log_success, log_warning
from backend.core.cache import invalidate_cache
from backend.db.repositories.conversation_repository import (
    conversation_write_connection,
    lock_conversation_identities,
    lock_conversation_session,
)
from backend.services.memory import MemoryOrchestrator, get_memory_cache
from backend.services.memory.memory_fallback import user_session_key

logger = get_logger(__name__)

router = APIRouter(prefix="/api/bali-zero/conversations", tags=["conversations"])

# Constants
DEFAULT_LIMIT = 20
MAX_LIMIT = 1000
DEFAULT_CONVERSATION_MESSAGES_LIMIT = 20  # Default messages to return in history
SUMMARY_MAX_LENGTH = 200  # Max length for auto-generated summaries


# Pydantic models
class ConversationHistoryResponse(BaseModel):
    success: bool
    messages: list[dict] = []
    total_messages: int = 0
    session_id: str | None = None
    error: str | None = None


class ConversationListItem(BaseModel):
    """Single conversation item for list view"""

    id: int
    title: str
    preview: str
    message_count: int
    created_at: str
    updated_at: str | None = None
    session_id: str | None = None


class ConversationListResponse(BaseModel):
    """List of conversations"""

    success: bool
    conversations: list[ConversationListItem] = []
    total: int = 0
    error: str | None = None


class SingleConversationResponse(BaseModel):
    """Single conversation with full messages"""

    success: bool
    id: int | None = None
    messages: list[dict] = []
    message_count: int = 0
    created_at: str | None = None
    session_id: str | None = None
    metadata: dict | None = None
    error: str | None = None


class UserMemoryContextResponse(BaseModel):
    """User memory context with profile facts"""

    success: bool
    user_id: str
    profile_facts: list[str] = []
    summary: str | None = None
    counters: dict[str, int] = {}
    has_data: bool = False
    error: str | None = None


# Global memory orchestrator instance (lazy initialized)
_memory_orchestrator: MemoryOrchestrator | None = None


async def get_memory_orchestrator(db_pool: asyncpg.Pool | None = None) -> MemoryOrchestrator | None:
    """Get or initialize the global memory orchestrator"""
    global _memory_orchestrator
    if _memory_orchestrator is None:
        try:
            _memory_orchestrator = MemoryOrchestrator(db_pool=db_pool)
            await _memory_orchestrator.initialize()
            log_success(logger, "MemoryOrchestrator initialized for conversations router")
        except Exception as e:
            log_warning(logger, f"Failed to initialize MemoryOrchestrator: {type(e).__name__}")
            return None
    return _memory_orchestrator


async def _invalidate_committed_conversation_cache() -> None:
    """Preserve cache effects without changing an acknowledged database outcome."""
    try:
        await invalidate_cache("zantara:conversations:*")
    except Exception as exc:
        logger.warning(
            "Conversation cache invalidation failed after commit: %s",
            type(exc).__name__,
            exc_info=False,
        )


@router.get("/history")
async def get_conversation_history(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    session_id: str | None = Query(None, description="Optional session filter"),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool | None = Depends(get_database_pool),
) -> ConversationHistoryResponse:
    """
    Get conversation history for the authenticated user

    SECURITY: User identity is extracted from JWT token.

    Query params:
    - limit: Max number of messages to return (default: 20)
    - session_id: Optional session filter
    """
    # Get user email from JWT token (prevents spoofing)
    user_email = current_user["email"]

    messages = []
    session_id_result = session_id
    total_messages = 0

    # Try DB first
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                # Get most recent conversation for user
                if session_id:
                    row = await conn.fetchrow(
                        """
                        SELECT messages, created_at, session_id
                        FROM conversations
                        WHERE user_id = $1 AND session_id = $2
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        user_email,
                        session_id,
                    )
                else:
                    row = await conn.fetchrow(
                        """
                        SELECT messages, created_at, session_id
                        FROM conversations
                        WHERE user_id = $1
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        user_email,
                    )

                if row:
                    raw_messages = row.get("messages", [])
                    if isinstance(raw_messages, str):
                        try:
                            messages = json.loads(raw_messages)
                        except json.JSONDecodeError:
                            messages = []
                    elif isinstance(raw_messages, list):
                        messages = raw_messages
                    else:
                        messages = []
                    session_id_result = row.get("session_id")
                    log_success(
                        logger,
                        "Retrieved conversation history from DB",
                        messages_count=len(messages),
                    )
        except Exception as e:
            log_error(
                logger,
                "Failed to retrieve conversation history from DB",
                error_type=type(e).__name__,
            )
            # Fallback to memory cache below

    # Fallback to memory cache if DB failed or returned nothing (and we have a session_id)
    if not messages and session_id:
        try:
            mem_cache = get_memory_cache()
            messages = mem_cache.get_messages(user_session_key(user_email, session_id), limit=limit)
            if messages:
                log_success(
                    logger,
                    "Retrieved conversation history from Memory Cache",
                    messages_count=len(messages),
                )
        except Exception as e:
            logger.warning("⚠️ Memory cache retrieval failed: %s", type(e).__name__)

    # Limit messages if needed
    total_messages = len(messages)
    if len(messages) > limit:
        messages = messages[-limit:]

    return ConversationHistoryResponse(
        success=True,
        messages=messages,
        total_messages=total_messages,
        session_id=session_id_result,
    )


@router.delete("/clear")
async def clear_conversation_history(
    session_id: str | None = Query(None, description="Optional session filter"),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Clear conversation history for the authenticated user

    SECURITY: User identity is extracted from JWT token.

    Query params:
    - session_id: Optional session filter (if omitted, clears ALL conversations for user)
    """
    # Get user email from JWT token (prevents spoofing)
    user_email = current_user["email"]

    try:
        async with conversation_write_connection(db_pool) as conn:
            if session_id:
                identities = await conn.fetch(
                    """
                    SELECT id, session_id FROM conversations
                    WHERE user_id = $1 AND session_id = $2
                    """,
                    user_email,
                    session_id,
                )
                where = "user_id = $1 AND session_id = $2"
                args = (user_email, session_id)
            else:
                identities = await conn.fetch(
                    "SELECT id, session_id FROM conversations WHERE user_id = $1",
                    user_email,
                )
                where = "user_id = $1"
                args = (user_email,)
            await lock_conversation_identities(
                conn,
                [(row["session_id"], row["id"]) for row in identities],
            )
            row_ids = [row["id"] for row in identities]
            where += f" AND id = ANY(${len(args) + 1})"
            args = (*args, row_ids)
            await conn.fetch(
                f"SELECT id FROM conversations WHERE {where} ORDER BY id FOR UPDATE",
                *args,
            )
            deleted_count = await conn.execute(
                f"DELETE FROM conversations WHERE {where}",
                *args,
            )
        deleted_count = int(deleted_count.split()[-1]) if deleted_count else 0
        await _invalidate_committed_conversation_cache()
        log_success(logger, "Cleared conversations", deleted_count=deleted_count)
        return {"success": True, "deleted_count": deleted_count}

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/stats")
async def get_conversation_stats(
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Get conversation statistics for the authenticated user

    SECURITY: User identity is extracted from JWT token.
    """
    # Get user email from JWT token (prevents spoofing)
    user_email = current_user["email"]

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total_conversations,
                    SUM(jsonb_array_length(messages)) as total_messages,
                    MAX(created_at) as last_conversation
                FROM conversations
                WHERE user_id = $1
                """,
                user_email,
            )

            if not row:
                return {
                    "success": True,
                    "user_email": user_email,
                    "total_conversations": 0,
                    "total_messages": 0,
                    "last_conversation": None,
                }

            return {
                "success": True,
                "user_email": user_email,
                "total_conversations": row["total_conversations"] or 0,
                "total_messages": row["total_messages"] or 0,
                "last_conversation": (
                    row["last_conversation"].isoformat() if row["last_conversation"] else None
                ),
            }

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/list", response_model=ConversationListResponse)
async def list_conversations(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> ConversationListResponse:
    """
    List all conversations for the authenticated user

    SECURITY: User identity is extracted from JWT token.

    Query params:
    - limit: Max number of conversations to return (default: 20)
    - offset: Offset for pagination (default: 0)

    Returns list with title (first user message), preview, and message count.
    """
    user_email = current_user["email"]

    try:
        async with db_pool.acquire() as conn:
            # Get total count
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) as total FROM conversations WHERE user_id = $1",
                user_email,
            )
            total = count_row["total"] if count_row else 0

            # Get conversations ordered by most recent
            rows = await conn.fetch(
                """
                SELECT id, session_id, messages, metadata, created_at
                FROM conversations
                WHERE user_id = $1
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3
                """,
                user_email,
                limit,
                offset,
            )

            conversations = []
            for row in rows:
                messages = row["messages"] or []
                metadata = row["metadata"] or {}

                # Fallback chain: generated_title → first_message → default
                # Priority 1: Use LLM-generated title (best quality)
                title = None
                if metadata and isinstance(metadata, dict) and "generated_title" in metadata:
                    title = metadata["generated_title"]

                # Priority 2: Fallback to first message excerpt
                if not title:
                    for msg in messages:
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            content = msg.get("content", "")
                            title = content[:50] + "..." if len(content) > 50 else content
                            break

                # Priority 3: Final fallback to default
                if not title:
                    title = "New Conversation"

                # Extract preview from last assistant message
                preview = ""

                # Extract preview from last assistant message
                for msg in reversed(messages):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        preview = content[:100] + "..." if len(content) > 100 else content
                        break

                conversations.append(
                    ConversationListItem(
                        id=row["id"],
                        title=title or "New Conversation",
                        preview=preview,
                        message_count=len(messages),
                        created_at=row["created_at"].isoformat(),
                        session_id=row["session_id"],
                    ),
                )

            log_success(logger, "Listed conversations", count=len(conversations), total=total)

            return ConversationListResponse(
                success=True,
                conversations=conversations,
                total=total,
            )

    except Exception as e:
        log_error(
            logger, "Failed to list conversations", error_type=type(e).__name__, exc_info=False
        )
        return ConversationListResponse(success=False, error="Request could not be completed.")


@router.get("/{conversation_id}", response_model=SingleConversationResponse)
async def get_conversation(
    conversation_id: int,
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> SingleConversationResponse:
    """
    Get a single conversation by ID

    SECURITY: Only returns conversation if it belongs to authenticated user.
    """
    user_email = current_user["email"]

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, session_id, messages, metadata, created_at
                FROM conversations
                WHERE id = $1 AND user_id = $2
                """,
                conversation_id,
                user_email,
            )

            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Conversation {conversation_id} not found",
                )

            messages = row["messages"] or []

            log_success(logger, "Retrieved conversation", message_count=len(messages))

            return SingleConversationResponse(
                success=True,
                id=row["id"],
                messages=messages,
                message_count=len(messages),
                created_at=row["created_at"].isoformat(),
                session_id=row["session_id"],
                metadata=row["metadata"],
            )

    except HTTPException:
        raise
    except Exception as e:
        log_error(logger, "Failed to get conversation", error_type=type(e).__name__, exc_info=False)
        return SingleConversationResponse(success=False, error="Request could not be completed.")


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool = Depends(get_database_pool),
) -> dict[str, Any]:
    """
    Delete a single conversation by ID

    SECURITY: Only deletes conversation if it belongs to authenticated user.
    """
    user_email = current_user["email"]

    try:
        async with conversation_write_connection(db_pool) as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, session_id FROM conversations
                WHERE id = $1 AND user_id = $2
                """,
                conversation_id,
                user_email,
            )

            if not existing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Conversation {conversation_id} not found",
                )
            await lock_conversation_session(
                conn,
                existing["session_id"],
                row_id=existing["id"],
            )
            locked = await conn.fetchrow(
                """
                SELECT id FROM conversations
                WHERE id = $1 AND user_id = $2 FOR UPDATE
                """,
                conversation_id,
                user_email,
            )
            if not locked:
                raise HTTPException(
                    status_code=404,
                    detail=f"Conversation {conversation_id} not found",
                )
            deleted_result = await conn.execute(
                "DELETE FROM conversations WHERE id = $1 AND user_id = $2",
                conversation_id,
                user_email,
            )
        deleted = deleted_result == "DELETE 1"
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Conversation {conversation_id} not found",
            )
        await _invalidate_committed_conversation_cache()
        log_success(logger, "Deleted conversation")
        return {"success": True, "deleted_id": conversation_id}

    except HTTPException:
        raise
    except Exception as e:
        raise handle_database_error(e)


@router.get("/memory/context", response_model=UserMemoryContextResponse)
async def get_user_memory_context(
    current_user: dict = Depends(get_current_user),
    db_pool: asyncpg.Pool | None = Depends(get_database_pool),
) -> UserMemoryContextResponse:
    """
    Get user memory context (profile facts, summary, counters)

    SECURITY: User identity is extracted from JWT token.

    Returns the user's stored memory facts and context that the AI uses
    to personalize responses.
    """
    user_email = current_user["email"]

    try:
        orchestrator = await get_memory_orchestrator(db_pool)
        if not orchestrator:
            return UserMemoryContextResponse(
                success=True,
                user_id=user_email,
                has_data=False,
                error="Memory service not available",
            )

        context = await orchestrator.get_user_context(user_email)

        log_success(
            logger,
            "Retrieved user memory context",
            facts_count=len(context.profile_facts),
            has_data=context.has_data,
        )

        return UserMemoryContextResponse(
            success=True,
            user_id=context.user_id,
            profile_facts=context.profile_facts,
            summary=context.summary,
            counters=context.counters,
            has_data=context.has_data,
        )

    except Exception as e:
        log_error(
            logger, "Failed to get user memory context", error_type=type(e).__name__, exc_info=False
        )
        return UserMemoryContextResponse(
            success=False,
            user_id=user_email,
            error="Request could not be completed.",
        )
