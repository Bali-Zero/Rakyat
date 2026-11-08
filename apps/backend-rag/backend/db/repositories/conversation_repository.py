"""
Conversation Repository
Handles all database operations for conversation persistence
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg

from backend.app.utils.json_utils import to_jsonb
from backend.app.utils.logging_utils import get_logger, log_success
from backend.core.retention_policy import (
    ALLOW_CLOCK_DELETE_ENV,
    RETENTION_MIN_DAYS,
    RetentionPolicyViolation,
    clock_delete_allowed,
    enforce_retention_floor,
)
from backend.db.base_repository import BaseRepository
from backend.services.common.cache import cache_invalidating

logger = get_logger(__name__)

# Two-int advisory keys are distinct from the migration runner's bigint keys.
# "CVNS" namespaces cooperating conversation writers. The complete opaque
# session value is the observed aggregate identity; user_id is mutable data.
_CONVERSATION_LOCK_NAMESPACE = 0x43564E53
_NULL_SESSION_LOCK_NAMESPACE = 0x43564E30
# "CVNC" coordinates bulk writes without one advisory key per candidate row.
_CONVERSATION_COORDINATION_NAMESPACE = 0x43564E43
_BULK_LOCK_TIMEOUT_SECONDS = 5.0


class ConversationCommitOutcomeUnknown(RuntimeError):
    """The transaction body finished but commit acknowledgement was lost."""


async def lock_conversation_session(
    conn: asyncpg.Connection,
    session_id: str | None,
    *,
    row_id: int | None = None,
) -> None:
    """Join the shared bulk barrier, then lock one session or legacy row."""
    if session_id is None and row_id is None:
        raise ValueError("row_id is required for a legacy NULL session")
    await conn.execute("SET LOCAL lock_timeout = '5s'")
    # Always take the barrier first. Different sessions can hold it together;
    # an exclusive bulk holder waits for every cooperating single writer.
    await conn.execute(
        "SELECT pg_advisory_xact_lock_shared($1::integer, 0::integer)",
        _CONVERSATION_COORDINATION_NAMESPACE,
    )
    if session_id is not None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1::integer, hashtext($2::text))",
            _CONVERSATION_LOCK_NAMESPACE,
            session_id,
        )
        return
    await conn.execute(
        "SELECT pg_advisory_xact_lock($1::integer, $2::integer)",
        _NULL_SESSION_LOCK_NAMESPACE,
        row_id,
    )


async def lock_conversation_identities(
    conn: asyncpg.Connection,
    identities: list[tuple[str | None, int]],
) -> None:
    """Exclude cooperating writers with one key, regardless of candidate count.

    Call before any row lock, and never upgrade a shared session lock in the
    same transaction. Callers retain candidate row IDs and revalidate predicates
    under row locks after this barrier; the scan itself is not a snapshot fence.
    """
    if any(session_id is None and row_id is None for session_id, row_id in identities):
        raise ValueError("row_id is required for a legacy NULL session")
    async with asyncio.timeout(_BULK_LOCK_TIMEOUT_SECONDS):
        # Retain a statement bound for the row locks and mutations that follow
        # this helper, including a legacy writer holding rows without our lock.
        await conn.execute(
            "SELECT set_config('statement_timeout', $1, true)",
            f"{_BULK_LOCK_TIMEOUT_SECONDS}s",
        )
        await conn.execute("SET LOCAL lock_timeout = '5s'")
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1::integer, 0::integer)",
            _CONVERSATION_COORDINATION_NAMESPACE,
        )


@asynccontextmanager
async def conversation_write_transaction(
    conn: asyncpg.Connection,
) -> AsyncIterator[None]:
    """Preserve body failures and classify an unacknowledged commit."""
    transaction = conn.transaction()
    await transaction.__aenter__()

    body_error: BaseException | None = None
    body_traceback = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        body_traceback = exc.__traceback__

    if body_error is not None:
        try:
            await transaction.__aexit__(type(body_error), body_error, body_traceback)
        except asyncio.CancelledError:
            raise
        except Exception as rollback_error:
            logger.error(
                "Conversation rollback cleanup failed: %s",
                type(rollback_error).__name__,
            )
        raise body_error.with_traceback(body_traceback) from body_error.__cause__

    try:
        await transaction.__aexit__(None, None, None)
    except asyncio.CancelledError:
        logger.warning("Conversation commit cancelled; outcome unknown")
        raise
    except Exception as exc:
        raise ConversationCommitOutcomeUnknown("conversation commit outcome is unknown") from exc


@asynccontextmanager
async def conversation_write_connection(
    pool: asyncpg.Pool,
) -> AsyncIterator[asyncpg.Connection]:
    """Preserve transaction outcome across pool release/reset failures."""
    acquire_context = pool.acquire()
    conn = await acquire_context.__aenter__()
    committed = False
    primary_error: BaseException | None = None
    primary_traceback = None
    try:
        async with conversation_write_transaction(conn):
            yield conn
        committed = True
    except BaseException as exc:
        primary_error = exc
        primary_traceback = exc.__traceback__

    try:
        await acquire_context.__aexit__(
            type(primary_error) if primary_error is not None else None,
            primary_error,
            primary_traceback,
        )
    except asyncio.CancelledError:
        raise
    except Exception as cleanup_error:
        if primary_error is not None:
            logger.error(
                "Conversation connection cleanup failed after %s: %s",
                type(primary_error).__name__,
                type(cleanup_error).__name__,
            )
            raise primary_error.with_traceback(primary_traceback) from primary_error.__cause__
        if not committed:
            raise
        logger.error(
            "Conversation connection cleanup failed after confirmed commit: %s",
            type(cleanup_error).__name__,
        )
        return

    if primary_error is not None:
        raise primary_error.with_traceback(primary_traceback) from primary_error.__cause__


class ConversationRepository(BaseRepository):
    """Stage-1 cooperative persistence with the existing public adapter."""

    @cache_invalidating(["zantara:conversations:*"])
    async def _save_messages_locked(
        self,
        session_id: str,
        user_id: str,
        messages: list[dict],
        metadata: dict | None = None,
    ) -> int | None:
        """Execute the legacy append under the cooperative write protocol."""
        async with conversation_write_connection(self.db_pool) as conn:
            await lock_conversation_session(conn, session_id)
            # Check if conversation exists for this session
            existing = await conn.fetchrow(
                """
            SELECT id, messages FROM conversations
            WHERE session_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT 1 FOR UPDATE
            """,
                session_id,
            )

            if existing:
                # Update existing conversation (append new messages)
                conversation_id = existing["id"]
                existing_messages = existing["messages"] or []

                # Merge messages (avoid duplicates by checking last message)
                merged_messages = list(existing_messages)
                for msg in messages:
                    # Simple deduplication: only add if not identical to last message
                    if not merged_messages or merged_messages[-1] != msg:
                        merged_messages.append(msg)

                await conn.execute(
                    """
                UPDATE conversations
                SET messages = $1::text::jsonb,
                    metadata = COALESCE(metadata, '{}'::jsonb) || $2::text::jsonb
                WHERE id = $3
                """,
                    # ::text::jsonb — the app pools register a jsonb
                    # codec (encoder=json.dumps); a param inferred as
                    # jsonb double-encodes to_jsonb()'s already-
                    # serialized string into a jsonb string scalar.
                    to_jsonb(merged_messages),
                    to_jsonb(metadata or {}),
                    conversation_id,
                )

            else:
                # Create new conversation
                row = await conn.fetchrow(
                    """
                INSERT INTO conversations (user_id, session_id, messages, metadata, created_at)
                VALUES ($1, $2, $3::text::jsonb, $4::text::jsonb, $5)
                RETURNING id
                """,
                    user_id,
                    session_id,
                    to_jsonb(messages),
                    to_jsonb(metadata or {}),
                    datetime.now(tz=timezone.utc),
                )

                conversation_id = row["id"] if row else None
            if conversation_id is None:
                raise RuntimeError("conversation insert returned no row")

        log_success(logger, "Saved conversation messages")
        return conversation_id

    async def save_messages(
        self,
        session_id: str,
        user_id: str,
        messages: list[dict],
        metadata: dict | None = None,
    ) -> int | None:
        """Keep legacy failure-as-None behavior without publishing failed writes."""
        try:
            return await self._save_messages_locked(session_id, user_id, messages, metadata)
        except ConversationCommitOutcomeUnknown:
            logger.error("Conversation persistence outcome unknown after commit")
            return None
        except Exception as exc:
            logger.error("Conversation persistence failed: %s", type(exc).__name__)
            return None

    async def get_messages(
        self,
        session_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """
        Retrieve conversation messages for a session

        Args:
            session_id: Session identifier
            limit: Maximum number of messages to return (0 = all)

        Returns:
            List of message dicts
        """
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT messages FROM conversations
                    WHERE session_id = $1
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    session_id,
                )

                if row and row["messages"]:
                    messages = row["messages"]
                    if limit > 0 and len(messages) > limit:
                        # Return last N messages
                        return messages[-limit:]
                    return messages

                return []

        except Exception as e:
            logger.error("Conversation retrieval failed: %s", type(e).__name__)
            return []

    @cache_invalidating(
        [
            "zantara:conversations:*",
        ]
    )
    async def cleanup_old_conversations(self, days: int = RETENTION_MIN_DAYS) -> int:
        """
        Delete conversations older than specified days

        Args:
            days: Number of days to keep (default: the 5-year retention floor)

        Returns:
            Number of conversations deleted

        Raises:
            RetentionPolicyViolation: if `days` is below the floor, or if timed
                deletion is disabled (it is, by default — see
                backend/core/retention_policy.py). This is the ONLY thing here
                that escapes: the `except Exception` below returns 0, so a
                refusal raised inside it would reach the caller as a successful
                "0 rows deleted". The guards therefore sit above the try.
        """
        enforce_retention_floor("days", days)
        if not clock_delete_allowed():
            raise RetentionPolicyViolation(
                "Clock-driven deletion of conversations is disabled by policy "
                f"('non cancellare', 2026-08-08). Set {ALLOW_CLOCK_DELETE_ENV}=1 to re-enable "
                "deliberately. Per-subject erasure (DELETE /api/conversations/clear) is a "
                "separate path and is NOT affected by this."
            )

        try:
            cutoff_date = datetime.now(tz=timezone.utc) - timedelta(days=days)

            async with conversation_write_connection(self.db_pool) as conn:
                identities = await conn.fetch(
                    """
                    SELECT id, session_id FROM conversations
                    WHERE created_at < $1
                    """,
                    cutoff_date,
                )
                await lock_conversation_identities(
                    conn,
                    [(row["session_id"], row["id"]) for row in identities],
                )
                row_ids = [row["id"] for row in identities]
                await conn.fetch(
                    """
                    SELECT id FROM conversations
                    WHERE created_at < $1 AND id = ANY($2) ORDER BY id FOR UPDATE
                    """,
                    cutoff_date,
                    row_ids,
                )
                result = await conn.execute(
                    "DELETE FROM conversations WHERE created_at < $1 AND id = ANY($2)",
                    cutoff_date,
                    row_ids,
                )

                # Extract count from "DELETE N" string
                deleted_count = int(result.split()[-1]) if result else 0

            log_success(
                logger,
                "Cleaned up old conversations",
                deleted_count=deleted_count,
                cutoff_days=days,
            )

            return deleted_count

        except ConversationCommitOutcomeUnknown:
            raise
        except Exception as e:
            logger.error("Conversation cleanup failed: %s", type(e).__name__)
            return 0

    @cache_invalidating(
        [
            "zantara:conversations:*",
        ]
    )
    async def anonymize_user_data(self, days: int = RETENTION_MIN_DAYS) -> int:
        """
        Anonymize user_id for conversations older than specified days

        Args:
            days: Number of days before anonymization (default: the 5-year floor)

        Returns:
            Number of conversations anonymized

        Raises:
            RetentionPolicyViolation: if `days` is below the floor. Anonymising
                at 7 days — the previous default — destroys the five-year
                reconstruction requirement within a week, which is why this is
                floored even though it is not a deletion. Raised above the try
                for the same reason as the sibling method.
        """
        enforce_retention_floor("days", days)

        try:
            cutoff_date = datetime.now(tz=timezone.utc) - timedelta(days=days)

            async with conversation_write_connection(self.db_pool) as conn:
                identities = await conn.fetch(
                    """
                    SELECT id, session_id FROM conversations
                    WHERE created_at < $1
                    AND user_id NOT LIKE 'anonymized_%'
                    """,
                    cutoff_date,
                )
                await lock_conversation_identities(
                    conn,
                    [(row["session_id"], row["id"]) for row in identities],
                )
                row_ids = [row["id"] for row in identities]
                await conn.fetch(
                    """
                    SELECT id FROM conversations
                    WHERE created_at < $1
                    AND user_id NOT LIKE 'anonymized_%'
                    AND id = ANY($2)
                    ORDER BY id FOR UPDATE
                    """,
                    cutoff_date,
                    row_ids,
                )
                result = await conn.execute(
                    """
                    UPDATE conversations
                    SET user_id = 'anonymized_' || id::text
                    WHERE created_at < $1
                    AND user_id NOT LIKE 'anonymized_%'
                    AND id = ANY($2)
                    """,
                    cutoff_date,
                    row_ids,
                )

                # Extract count from "UPDATE N" string
                anonymized_count = int(result.split()[-1]) if result else 0

            log_success(
                logger,
                "Anonymized user data",
                anonymized_count=anonymized_count,
                cutoff_days=days,
            )

            return anonymized_count

        except ConversationCommitOutcomeUnknown:
            raise
        except Exception as e:
            logger.error("Conversation anonymization failed: %s", type(e).__name__)
            return 0
