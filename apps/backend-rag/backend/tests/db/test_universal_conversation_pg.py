"""Real PG acceptance for the existing stream consumer's persistence seam.

Synthetic fixtures only. Run through this mandate's run_disposable_pg.py.
The target identity guard runs before any schema write.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.core.pg_json_codec import init_asyncpg_connection
from backend.db.repositories.conversation_repository import (
    ConversationRepository,
    conversation_write_connection,
    conversation_write_transaction,
    lock_conversation_identities,
    lock_conversation_session,
)
from backend.services.common import cache

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_bulk_lock_deadline_rolls_back_and_releases_waiter(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.db.repositories.conversation_repository._BULK_LOCK_TIMEOUT_SECONDS", 0.1
    )
    async with conversation_pool.acquire() as blocker:
        async with blocker.transaction():
            await lock_conversation_session(blocker, "synthetic-peer")
            with pytest.raises((TimeoutError, asyncpg.QueryCanceledError)):
                async with conversation_write_connection(conversation_pool) as writer:
                    writer_pid = await writer.fetchval("SELECT pg_backend_pid()")
                    await writer.execute(
                        "INSERT INTO conversations(user_id, session_id, messages) "
                        "VALUES ('synthetic-timeout', 'synthetic-timeout', '[]'::jsonb)"
                    )
                    await lock_conversation_identities(writer, [("synthetic-peer", 1)])
            assert (
                await blocker.fetchval(
                    "SELECT count(*) FROM pg_locks WHERE pid=$1 AND locktype='advisory'",
                    writer_pid,
                )
                == 0
            )
            assert (
                await blocker.fetchval(
                    "SELECT count(*) FROM conversations WHERE user_id='synthetic-timeout'"
                )
                == 0
            )
            assert await blocker.fetchval("SELECT 1") == 1
    # The timed-out exclusive waiter must not obstruct a subsequent save.
    assert (
        await ConversationRepository(conversation_pool)._save_messages_locked(
            "synthetic-peer", "synthetic-user", []
        )
        is not None
    )


@pytest.mark.asyncio
async def test_bulk_lock_cardinality_is_constant_above_cluster_capacity(
    conversation_pool: asyncpg.Pool,
) -> None:
    async with conversation_pool.acquire() as peer:
        capacity = await peer.fetchval(
            "SELECT current_setting('max_locks_per_transaction')::int * "
            "(current_setting('max_connections')::int + "
            "current_setting('max_prepared_transactions')::int)"
        )
        requested = 3 * capacity
        assert 0 < requested <= 20_000, "Disposable cluster exceeds test resource bound"
        async with conversation_write_connection(conversation_pool) as writer:
            writer_pid = await writer.fetchval("SELECT pg_backend_pid()")
            # Includes legacy NULL rows, empty/text sessions, and duplicates.
            await lock_conversation_identities(
                writer, [(None, key) for key in range(requested)] + [("", 1), ("alpha", 2)] * 2
            )
            locks = await peer.fetch(
                "SELECT mode, granted FROM pg_locks WHERE pid=$1 AND locktype='advisory'",
                writer_pid,
            )
            assert [dict(row) for row in locks] == [{"mode": "ExclusiveLock", "granted": True}]
            assert await peer.fetchval("SELECT 1") == 1
        assert (
            await peer.fetchval(
                "SELECT count(*) FROM pg_locks WHERE pid=$1 AND locktype='advisory'", writer_pid
            )
            == 0
        )


@pytest.mark.asyncio
async def test_different_sessions_can_write_while_peer_transaction_remains_open(
    conversation_pool: asyncpg.Pool,
) -> None:
    async with conversation_write_connection(conversation_pool) as first:
        await lock_conversation_session(first, "synthetic-first")
        identifier = await asyncio.wait_for(
            ConversationRepository(conversation_pool)._save_messages_locked(
                "synthetic-second", "synthetic-user", []
            ),
            timeout=2,
        )
        assert identifier is not None
        assert first.is_in_transaction()


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_bulk", [False, True])
@pytest.mark.parametrize("session", [None, "", "synthetic-text"])
async def test_bulk_and_single_writers_exclude_each_other(
    conversation_pool: asyncpg.Pool,
    holder_bulk: bool,
    session: str | None,
) -> None:
    async def lock(conn, bulk):
        if bulk:
            await lock_conversation_identities(conn, [(session, 73)])
        else:
            await lock_conversation_session(conn, session, row_id=73)

    async def attempt():
        async with conversation_write_connection(conversation_pool) as conn:
            await lock(conn, not holder_bulk)

    task = None
    try:
        async with conversation_write_connection(conversation_pool) as holder:
            await lock(holder, holder_bulk)
            task = asyncio.create_task(attempt())
            async with conversation_pool.acquire() as observer:
                await _observe_advisory_wait(observer, task, session, 73, bulk=True)
                assert not task.done(), "conflicting writer passed the held barrier"
        await asyncio.wait_for(task, timeout=2)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_bulk_holder_releases_barrier_and_rolls_back(
    conversation_pool: asyncpg.Pool,
) -> None:
    ready = asyncio.Event()

    async def bulk():
        async with conversation_write_connection(conversation_pool) as conn:
            await lock_conversation_identities(conn, [("synthetic-cancel", 1)])
            await conn.execute(
                "INSERT INTO conversations(user_id,session_id,messages) "
                "VALUES ('synthetic-cancel','synthetic-cancel','[]')"
            )
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(bulk())
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
    finally:
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    async with conversation_pool.acquire() as observer:
        assert await observer.fetchval("SELECT count(*) FROM conversations") == 0
    assert (
        await asyncio.wait_for(
            ConversationRepository(conversation_pool)._save_messages_locked(
                "synthetic-cancel", "synthetic-user", []
            ),
            timeout=2,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_bulk_row_wait_is_bounded_and_rolled_back(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.routers import conversations

    repository = ConversationRepository(conversation_pool)
    identifier = await repository._save_messages_locked("synthetic-held-row", "synthetic-user", [])
    monkeypatch.setattr(
        "backend.db.repositories.conversation_repository._BULK_LOCK_TIMEOUT_SECONDS", 0.1
    )
    async with conversation_pool.acquire() as blocker:
        async with blocker.transaction():
            # Deliberately model an uncooperative old row holder. This proves a
            # bounded wait only; it does not establish rollout compatibility.
            await blocker.fetchrow(
                "SELECT id FROM conversations WHERE id = $1 FOR UPDATE", identifier
            )
            with pytest.raises(HTTPException) as raised:
                await asyncio.wait_for(
                    conversations.clear_conversation_history(
                        session_id=None,
                        current_user={"email": "synthetic-user"},
                        db_pool=conversation_pool,
                    ),
                    timeout=1,
                )
            assert raised.value.status_code == 503
            assert raised.value.detail == "Database service temporarily unavailable"
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT id FROM conversations") == identifier


@pytest.mark.asyncio
async def test_user_erasure_does_not_mutate_unlocked_new_session(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.routers import conversations

    repository = ConversationRepository(conversation_pool)
    await repository._save_messages_locked("synthetic-old", "synthetic-user", [])
    new_identifier = None

    async def insert_after_identity_scan(conn, identities):
        nonlocal new_identifier
        new_identifier = await repository._save_messages_locked(
            "synthetic-new", "synthetic-user", [{"content": "after-snapshot"}]
        )
        await lock_conversation_identities(conn, identities)

    monkeypatch.setattr(
        conversations,
        "lock_conversation_identities",
        insert_after_identity_scan,
    )
    result = await conversations.clear_conversation_history(
        session_id=None, current_user={"email": "synthetic-user"}, db_pool=conversation_pool
    )
    assert result["deleted_count"] == 1
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT id FROM conversations") == new_identifier


@pytest_asyncio.fixture
async def conversation_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[asyncpg.Pool]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    try:
        identity = await admin.fetchrow(
            "SELECT current_user AS role, current_database() AS db, "
            "system_identifier::text AS system_id FROM pg_control_system()"
        )
        if dict(identity) != {
            "role": "universal_db_test_owner",
            "db": "universal_db_test",
            "system_id": expected,
        }:
            pytest.fail("Refusing a database outside this disposable test cluster")
        schema = "universal_test_" + uuid4().hex
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=10,
            init=init_asyncpg_connection,
            server_settings={"search_path": schema + ",public"},
        )
        try:
            async with pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE conversations (
                        id SERIAL PRIMARY KEY,
                        user_id varchar(255) NOT NULL, session_id varchar(255),
                        messages jsonb, metadata jsonb,
                        created_at timestamptz DEFAULT now()
                    );
                    CREATE INDEX ON conversations(session_id);
                    CREATE FUNCTION universal_delay() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN PERFORM pg_sleep(0.03); RETURN NEW; END $$;
                    CREATE TRIGGER force_overlap BEFORE INSERT OR UPDATE ON conversations
                    FOR EACH ROW EXECUTE FUNCTION universal_delay();
                """)

            async def ignore_cache(_pattern: str) -> int:
                return 0

            from backend.app.routers import conversations as cache_router

            monkeypatch.setattr(cache, "_invalidate_cache", ignore_cache)
            monkeypatch.setattr(cache_router, "invalidate_cache", ignore_cache)
            yield pool
        finally:
            await pool.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin.close()


@pytest.mark.asyncio
async def test_reader_uses_same_tied_timestamp_order_as_writer(
    conversation_pool: asyncpg.Pool,
) -> None:
    async with conversation_pool.acquire() as conn:
        await conn.execute("""INSERT INTO conversations(user_id, session_id, messages, created_at) VALUES
            ('synthetic-user', 'synthetic-tie', '[{"content":"older-id"}]', '2026-01-01T00:00:00Z'),
            ('synthetic-user', 'synthetic-tie', '[{"content":"newer-id"}]', '2026-01-01T00:00:00Z')""")
    assert await ConversationRepository(conversation_pool).get_messages(
        "synthetic-tie", "synthetic-user"
    ) == [{"content": "newer-id"}]


@pytest.mark.asyncio
async def test_sequential_save_preserves_id_and_messages(conversation_pool: asyncpg.Pool) -> None:
    repo = ConversationRepository(conversation_pool)
    first = await repo.save_messages(
        "synthetic-session", "synthetic-user", [{"role": "user", "content": "first"}]
    )
    second = await repo.save_messages(
        "synthetic-session", "synthetic-user", [{"role": "assistant", "content": "second"}]
    )
    assert first is not None and first == second
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT jsonb_array_length(messages) FROM conversations") == 2


@pytest.mark.asyncio
async def test_concurrent_first_saves_have_one_id(conversation_pool: asyncpg.Pool) -> None:
    repo = ConversationRepository(conversation_pool)
    ids = await asyncio.gather(
        *(
            repo.save_messages(
                "synthetic-session",
                "synthetic-user",
                [{"role": "user", "content": f"item-{i}"}],
            )
            for i in range(8)
        )
    )
    assert None not in ids
    assert len(set(ids)) == 1
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations") == 1
        assert await conn.fetchval("SELECT jsonb_array_length(messages) FROM conversations") == 8


@pytest.mark.asyncio
async def test_pool_drain_waits_for_inflight_writer_and_preserves_commit(
    conversation_pool: asyncpg.Pool,
) -> None:
    """Application pool shutdown drains an admitted writer; this is not fleet proof."""
    pool = conversation_pool
    blocker = await pool.acquire()
    transaction = blocker.transaction()
    transaction_open = False
    observer = None
    writer = None
    drain = None
    try:
        await transaction.start()
        transaction_open = True
        observer = await asyncpg.connect(
            os.environ["NUZ_UNIVERSAL_PG_DSN"],
            server_settings={"search_path": await blocker.fetchval("SHOW search_path")},
        )
        await lock_conversation_session(blocker, "synthetic-drain")
        writer = asyncio.create_task(
            ConversationRepository(pool)._save_messages_locked(
                "synthetic-drain", "synthetic-user", [{"role": "user", "content": "drain"}], {}
            )
        )
        deadline = asyncio.get_running_loop().time() + 3
        while not await observer.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND NOT granted AND database = (SELECT oid FROM pg_database "
            "WHERE datname = current_database()))"
        ):
            assert asyncio.get_running_loop().time() < deadline, "writer did not wait for lock"
            await asyncio.sleep(0.01)
        drain = asyncio.create_task(pool.close())
        await asyncio.sleep(0)
        assert not drain.done()
        await transaction.commit()
        transaction_open = False
        await pool.release(blocker)
        blocker = None
        row_id = await asyncio.wait_for(writer, timeout=5)
        await asyncio.wait_for(drain, timeout=5)
        assert row_id > 0
        assert (
            await observer.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id = 'synthetic-drain'"
            )
            == 1
        )
    finally:
        if blocker is not None:
            try:
                if transaction_open:
                    await transaction.rollback()
            finally:
                await pool.release(blocker)
        for task in (writer, drain):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (writer, drain) if task is not None), return_exceptions=True
        )
        if observer is not None:
            await observer.close()


@pytest.mark.asyncio
async def test_bulk_mutation_covers_null_empty_and_text_sessions(
    conversation_pool: asyncpg.Pool,
) -> None:
    async with conversation_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            INSERT INTO conversations(user_id, session_id, messages, metadata) VALUES
                ('legacy-null', NULL, '[]', '{}'),
                ('empty-session', '', '[]', '{}'),
                ('text-session', 'normal', '[]', '{}')
            RETURNING id, session_id
            """
        )
        async with conversation_write_transaction(conn):
            await lock_conversation_identities(
                conn,
                [(row["session_id"], row["id"]) for row in reversed(rows)],
            )
            await conn.fetch(
                "SELECT id FROM conversations WHERE id = ANY($1::int[]) FOR UPDATE",
                [row["id"] for row in rows],
            )
            await conn.execute(
                """
                UPDATE conversations SET metadata = '{"phase_one":true}'::jsonb
                WHERE id = ANY($1::int[])
                """,
                [row["id"] for row in rows],
            )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE metadata->>'phase_one' = 'true'"
            )
            == 3
        )


class _CommitAckLost:
    def __init__(self, transaction: object) -> None:
        self.transaction = transaction

    async def __aenter__(self) -> object:
        return await self.transaction.__aenter__()  # type: ignore[attr-defined,no-any-return]

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        result = await self.transaction.__aexit__(exc_type, exc, tb)  # type: ignore[attr-defined]
        if exc_type is None:
            raise RuntimeError("synthetic lost commit acknowledgement")
        return bool(result)


class _AckLostConnection:
    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)

    def transaction(self) -> _CommitAckLost:
        return _CommitAckLost(self.connection.transaction())


class _AckLostAcquire:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.connection: asyncpg.Connection | None = None

    async def __aenter__(self) -> _AckLostConnection:
        self.connection = await self.pool.acquire()
        return _AckLostConnection(self.connection)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self.connection is not None
        await self.pool.release(self.connection)


class _AckLostPool:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool
        self.acquire_count = 0

    def acquire(self) -> _AckLostAcquire:
        self.acquire_count += 1
        return _AckLostAcquire(self.pool)


class _TransactionExitFault:
    def __init__(self, transaction: object, error: BaseException) -> None:
        self.transaction = transaction
        self.error = error

    async def __aenter__(self) -> object:
        return await self.transaction.__aenter__()  # type: ignore[attr-defined,no-any-return]

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        result = await self.transaction.__aexit__(exc_type, exc, tb)  # type: ignore[attr-defined]
        if exc_type is None:
            raise self.error
        return bool(result)


class _CompoundFaultConnection:
    def __init__(self, connection: asyncpg.Connection, transaction_error: BaseException) -> None:
        self.connection = connection
        self.transaction_error = transaction_error

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)

    def transaction(self) -> _TransactionExitFault:
        return _TransactionExitFault(self.connection.transaction(), self.transaction_error)


class _CompoundFaultAcquire:
    def __init__(self, pool: _CompoundFaultPool) -> None:
        self.pool = pool
        self.connection: asyncpg.Connection | None = None

    async def __aenter__(self) -> _CompoundFaultConnection:
        self.connection = await self.pool.pool.acquire()
        return _CompoundFaultConnection(self.connection, self.pool.transaction_error)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self.connection is not None
        await self.pool.pool.release(self.connection)
        raise self.pool.release_error


class _CompoundFaultPool:
    def __init__(
        self,
        pool: asyncpg.Pool,
        transaction_error: BaseException,
        release_error: BaseException,
    ) -> None:
        self.pool = pool
        self.transaction_error = transaction_error
        self.release_error = release_error
        self.acquire_count = 0

    def acquire(self) -> _CompoundFaultAcquire:
        self.acquire_count += 1
        return _CompoundFaultAcquire(self)


class _ReleaseFailsAcquire:
    def __init__(self, pool: asyncpg.Pool, marker: str) -> None:
        self.pool = pool
        self.marker = marker
        self.connection: asyncpg.Connection | None = None

    async def __aenter__(self) -> asyncpg.Connection:
        self.connection = await self.pool.acquire()
        return self.connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self.connection is not None
        await self.pool.release(self.connection)
        if exc_type is None:
            raise RuntimeError(self.marker)


class _ReleaseFailsPool:
    def __init__(self, pool: asyncpg.Pool, marker: str) -> None:
        self.pool = pool
        self.marker = marker
        self.acquire_count = 0

    def acquire(self) -> _ReleaseFailsAcquire:
        self.acquire_count += 1
        return _ReleaseFailsAcquire(self.pool, self.marker)


@pytest.mark.asyncio
async def test_lost_commit_ack_is_indeterminate_and_not_retried(
    conversation_pool: asyncpg.Pool,
) -> None:
    pool = _AckLostPool(conversation_pool)
    repo = ConversationRepository(pool)  # type: ignore[arg-type]
    result = await repo.save_messages(
        "ack-lost-session",
        "synthetic-user",
        [{"role": "user", "content": "committed once"}],
    )
    assert result is None
    assert pool.acquire_count == 1
    async with conversation_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id = $1",
                "ack-lost-session",
            )
            == 1
        )


@pytest.mark.asyncio
async def test_lost_commit_ack_plus_release_failure_keeps_one_route_write(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import AsyncMock

    from backend.app.routers import conversations as conversations_router

    marker = "COMPOUND_FAULT_DETAIL"
    pool = _CompoundFaultPool(
        conversation_pool,
        RuntimeError("lost ack " + marker),
        OSError("release failed " + marker),
    )
    invalidated: list[str] = []
    router_invalidation = AsyncMock(return_value=0)
    sleeps: list[int] = []
    spawned: list[str] = []

    class _MemoryProbe:
        def __init__(self) -> None:
            self.synced: list[tuple[str, int]] = []

        def add_message(self, *_args: Any) -> None:
            return None

        def mark_synced(self, key: str, conversation_id: int) -> None:
            self.synced.append((key, conversation_id))

    memory = _MemoryProbe()

    async def record_invalidation(pattern: str) -> int:
        invalidated.append(pattern)
        return 0

    async def record_sleep(delay: int) -> None:
        sleeps.append(delay)

    def close_spawn(coro: Any, *, name: str) -> None:
        spawned.append(name)
        coro.close()

    monkeypatch.setattr(conversations_router, "get_memory_cache", lambda: memory)
    monkeypatch.setattr(conversations_router, "spawn", close_spawn)
    monkeypatch.setattr(cache, "_invalidate_cache", record_invalidation)
    monkeypatch.setattr(conversations_router, "invalidate_cache", router_invalidation)
    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    with caplog.at_level("ERROR"), pytest.raises(HTTPException) as raised:
        await conversations_router.save_conversation(
            conversations_router.SaveConversationRequest(
                session_id="compound-ack-release",
                messages=[{"role": "user", "content": "committed once"}],
            ),
            current_user={"email": "synthetic-user@example.invalid"},
            db_pool=pool,  # type: ignore[arg-type]
        )

    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "conversation_commit_unknown"
    assert raised.value.detail["retryable"] is False
    assert pool.acquire_count == 1
    assert sleeps == []
    assert invalidated == []
    router_invalidation.assert_not_awaited()
    assert memory.synced == []
    assert spawned == []
    assert "ConversationCommitOutcomeUnknown" in caplog.text
    assert "OSError" in caplog.text
    assert marker not in caplog.text
    async with conversation_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id = $1",
                "compound-ack-release",
            )
            == 1
        )


@pytest.mark.asyncio
async def test_commit_cancellation_plus_release_failure_keeps_one_route_write(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from backend.app.routers import conversations as conversations_router

    pool = _CompoundFaultPool(
        conversation_pool,
        asyncio.CancelledError(),
        OSError("RELEASE_DETAIL"),
    )
    invalidated: list[str] = []
    router_invalidation = AsyncMock(return_value=0)
    sleeps: list[int] = []
    spawned: list[str] = []

    class _MemoryProbe:
        def __init__(self) -> None:
            self.synced: list[tuple[str, int]] = []

        def add_message(self, *_args: Any) -> None:
            return None

        def mark_synced(self, key: str, conversation_id: int) -> None:
            self.synced.append((key, conversation_id))

    memory = _MemoryProbe()

    async def record_invalidation(pattern: str) -> int:
        invalidated.append(pattern)
        return 0

    async def record_sleep(delay: int) -> None:
        sleeps.append(delay)

    def close_spawn(coro: Any, *, name: str) -> None:
        spawned.append(name)
        coro.close()

    monkeypatch.setattr(conversations_router, "get_memory_cache", lambda: memory)
    monkeypatch.setattr(conversations_router, "spawn", close_spawn)
    monkeypatch.setattr(cache, "_invalidate_cache", record_invalidation)
    monkeypatch.setattr(conversations_router, "invalidate_cache", router_invalidation)
    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    with pytest.raises(asyncio.CancelledError):
        await conversations_router.save_conversation(
            conversations_router.SaveConversationRequest(
                session_id="compound-cancel-release",
                messages=[{"role": "user", "content": "committed once"}],
            ),
            current_user={"email": "synthetic-user@example.invalid"},
            db_pool=pool,  # type: ignore[arg-type]
        )

    assert pool.acquire_count == 1
    assert sleeps == []
    assert invalidated == []
    router_invalidation.assert_not_awaited()
    assert memory.synced == []
    assert spawned == []
    async with conversation_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id = $1",
                "compound-cancel-release",
            )
            == 1
        )


@pytest.mark.asyncio
async def test_known_body_error_plus_release_failure_preserves_identity(
    conversation_pool: asyncpg.Pool,
) -> None:
    class _KnownBodyError(RuntimeError):
        pass

    body_error = _KnownBodyError("BODY_DETAIL")
    pool = _CompoundFaultPool(
        conversation_pool,
        RuntimeError("unused transaction fault"),
        OSError("RELEASE_DETAIL"),
    )
    with pytest.raises(_KnownBodyError) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            raise body_error
    assert caught.value is body_error
    assert pool.acquire_count == 1
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations") == 0


@pytest.mark.asyncio
async def test_body_cancellation_plus_release_failure_remains_cancelled(
    conversation_pool: asyncpg.Pool,
) -> None:
    cancellation = asyncio.CancelledError()
    pool = _CompoundFaultPool(
        conversation_pool,
        RuntimeError("unused transaction fault"),
        OSError("RELEASE_DETAIL"),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            raise cancellation
    assert caught.value is cancellation
    assert pool.acquire_count == 1
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations") == 0


@pytest.mark.asyncio
async def test_release_failure_after_commit_keeps_route_in_db_mode(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.app.routers import conversations as conversations_router

    marker = "SYNTHETIC_RELEASE_DETAIL"
    pool = _ReleaseFailsPool(conversation_pool, marker)

    class _MemoryProbe:
        def __init__(self) -> None:
            self.synced: list[tuple[str, int]] = []

        def add_message(self, *_args: Any) -> None:
            return None

        def mark_synced(self, key: str, conversation_id: int) -> None:
            self.synced.append((key, conversation_id))

    memory = _MemoryProbe()
    spawned: list[str] = []

    def close_spawn(coro: Any, *, name: str) -> None:
        spawned.append(name)
        coro.close()

    monkeypatch.setattr(conversations_router, "get_memory_cache", lambda: memory)
    monkeypatch.setattr(conversations_router, "spawn", close_spawn)
    monkeypatch.setattr(
        conversations_router.settings, "conversation_title_generation_enabled", True
    )

    with caplog.at_level("ERROR"):
        response = await conversations_router.save_conversation(
            conversations_router.SaveConversationRequest(
                session_id="synthetic-release-route",
                messages=[{"role": "user", "content": "safe"}],
            ),
            current_user={"email": "synthetic-user@example.invalid"},
            db_pool=pool,  # type: ignore[arg-type]
        )

    assert response["persistence_mode"] == "db"
    assert response["conversation_id"] > 0
    assert pool.acquire_count == 1
    assert memory.synced == [
        (
            conversations_router.user_session_key(
                "synthetic-user@example.invalid",
                "synthetic-release-route",
            ),
            response["conversation_id"],
        )
    ]
    assert len(spawned) == 1
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text
    async with conversation_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id = $1",
                "synthetic-release-route",
            )
            == 1
        )


@pytest.mark.asyncio
async def test_release_failure_after_commit_preserves_append_and_cache(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _ReleaseFailsPool(conversation_pool, "SYNTHETIC_APPEND_RELEASE_DETAIL")
    invalidated: list[str] = []

    async def record_invalidation(pattern: str) -> int:
        invalidated.append(pattern)
        return 0

    monkeypatch.setattr(cache, "_invalidate_cache", record_invalidation)
    identifier = await ConversationRepository(pool)._save_messages_locked(  # type: ignore[arg-type]
        "synthetic-release-append",
        "synthetic-user",
        [{"role": "user", "content": "committed"}],
    )

    assert identifier > 0
    assert pool.acquire_count == 1
    assert invalidated == [
        "zantara:conversations:*",
    ]
    async with conversation_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id = $1",
                "synthetic-release-append",
            )
            == 1
        )


@pytest.mark.asyncio
async def test_concurrent_appends_do_not_lose_updates(conversation_pool: asyncpg.Pool) -> None:
    repo = ConversationRepository(conversation_pool)
    first = await repo.save_messages(
        "synthetic-session", "synthetic-user", [{"role": "user", "content": "seed"}]
    )
    ids = await asyncio.gather(
        *(
            repo.save_messages(
                "synthetic-session", "synthetic-user", [{"role": "user", "content": f"item-{i}"}]
            )
            for i in range(8)
        )
    )
    assert all(identifier == first for identifier in ids)
    async with conversation_pool.acquire() as conn:
        messages = await conn.fetchval("SELECT messages FROM conversations")
    assert {message["content"] for message in messages} == {"seed"} | {
        f"item-{i}" for i in range(8)
    }


@pytest.mark.asyncio
async def test_adjacent_replay_preserves_legacy_dedup(conversation_pool: asyncpg.Pool) -> None:
    repo = ConversationRepository(conversation_pool)
    payload = [{"role": "user", "content": "repeat"}]
    first = await repo.save_messages("synthetic-session", "synthetic-user", payload)
    assert await repo.save_messages("synthetic-session", "synthetic-user", payload) == first
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT jsonb_array_length(messages) FROM conversations") == 1


@pytest.mark.asyncio
async def test_database_rejection_does_not_log_payload_or_leave_partial_write(
    conversation_pool: asyncpg.Pool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = ConversationRepository(conversation_pool)
    async with conversation_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE conversations ADD CHECK (messages::text NOT LIKE '%synthetic-private-sentinel%')"
        )
    with caplog.at_level("WARNING"):
        assert (
            await repo.save_messages(
                "synthetic-secret-session",
                "synthetic-user",
                [{"content": "synthetic-private-sentinel"}],
            )
            is None
        )
    assert "synthetic-private-sentinel" not in caplog.text
    assert "synthetic-secret-session" not in caplog.text
    assert "CheckViolationError" in caplog.text
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations") == 0
    assert (
        await repo.save_messages(
            "synthetic-secret-session", "synthetic-user", [{"content": "safe"}]
        )
        is not None
    )


@pytest.mark.asyncio
async def test_cache_outage_keeps_committed_data_and_safe_logs(
    conversation_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def cache_failure(pattern: str) -> None:
        raise RuntimeError("synthetic-private-cache-value " + pattern)

    monkeypatch.setattr(cache, "_invalidate_cache", cache_failure)
    with caplog.at_level("WARNING"):
        identifier = await ConversationRepository(conversation_pool).save_messages(
            "synthetic-secret-session",
            "synthetic-user",
            [{"content": "safe"}],
        )
    assert identifier is not None
    assert "synthetic-private-cache-value" not in caplog.text
    assert "synthetic-secret-session" not in caplog.text
    async with conversation_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM conversations") == 1


@pytest.mark.asyncio
async def test_cancelled_transaction_releases_session_lock(conversation_pool: asyncpg.Pool) -> None:
    repo = ConversationRepository(conversation_pool)
    async with conversation_pool.acquire() as conn:
        await conn.execute("""CREATE OR REPLACE FUNCTION universal_delay() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(10); RETURN NEW; END $$""")
    task = asyncio.create_task(
        repo.save_messages("synthetic-session", "synthetic-user", [{"content": "cancelled"}])
    )
    try:
        async with conversation_pool.acquire() as conn:
            for _ in range(100):
                locked = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=$1 AND granted)",
                    0x43564E53,
                )
                if locked:
                    break
                await asyncio.sleep(0.01)
            assert locked, "Writer did not reach its transaction lock"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with conversation_pool.acquire() as conn:
            assert await conn.fetchval("SELECT count(*) FROM conversations") == 0
            await conn.execute("DROP TRIGGER force_overlap ON conversations")
        assert (
            await asyncio.wait_for(
                repo.save_messages(
                    "synthetic-session", "synthetic-user", [{"content": "after-cancel"}]
                ),
                timeout=2,
            )
            is not None
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_synthetic_backup_restore_preserves_post_change_records(
    conversation_pool: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    if not os.environ.get("NUZ_UNIVERSAL_PG_BIN"):
        pytest.skip("Requires disposable cluster backup binaries")
    repo = ConversationRepository(conversation_pool)
    identifier = await repo.save_messages(
        "synthetic-session", "synthetic-user", [{"content": "before"}]
    )
    await repo.save_messages(
        "synthetic-session", "synthetic-user", [{"content": "after-candidate"}]
    )
    async with conversation_pool.acquire() as conn:
        schema = await conn.fetchval("SELECT current_schema()")
        before = dict(
            await conn.fetchrow(
                "SELECT id, user_id, session_id, messages, metadata FROM conversations"
            )
        )
    binary = Path(os.environ["NUZ_UNIVERSAL_PG_BIN"])
    dump = tmp_path / "synthetic.dump"
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    # The harness creates a credential-free DSN; do not put any DSN in argv.
    target = urlsplit(os.environ["NUZ_UNIVERSAL_PG_DSN"])
    query = parse_qs(target.query)
    socket_path = Path(query["host"][0]).resolve()
    assert target.hostname is None and target.password is None
    assert socket_path == Path(os.environ["NUZ_UNIVERSAL_PG_ARTIFACT_DIR"]).resolve()
    env.update(
        PGHOST=str(socket_path),
        PGPORT=query["port"][0],
        PGUSER=target.username,
        PGDATABASE="universal_db_test",
        PGPASSFILE=os.devnull,
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [str(binary / "pg_dump"), "--format=custom", "--schema", schema, "--file", str(dump)],
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, "Synthetic backup failed"
    async with conversation_pool.acquire() as conn:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    result = await asyncio.to_thread(
        subprocess.run,
        [str(binary / "pg_restore"), "--dbname", "universal_db_test", "--exit-on-error", str(dump)],
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, "Synthetic restore failed"
    await conversation_pool.expire_connections()
    async with conversation_pool.acquire() as conn:
        after = dict(
            await conn.fetchrow(
                "SELECT id, user_id, session_id, messages, metadata FROM conversations"
            )
        )
    assert after == before and after["id"] == identifier
    assert [message["content"] for message in after["messages"]] == ["before", "after-candidate"]


@pytest.mark.asyncio
async def test_title_scheduling_failure_leaves_exactly_one_committed_snapshot(conversation_pool):
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.routers import conversations

    request = conversations.SaveConversationRequest(
        session_id="synthetic-postcommit-physical",
        messages=[{"role": "user", "content": "synthetic"}],
    )
    with (
        patch.object(conversations, "get_memory_cache", return_value=MagicMock()),
        patch.object(
            conversations, "spawn", side_effect=RuntimeError("synthetic task factory failure")
        ),
        patch.object(conversations.settings, "conversation_title_generation_enabled", True),
        patch.object(
            conversations.metrics_collector, "record_cache_db_consistency_error"
        ) as metric,
        patch.object(cache, "_invalidate_cache", new_callable=AsyncMock),
        patch.object(
            conversations, "invalidate_cache", new_callable=AsyncMock
        ) as router_invalidation,
        patch("asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        result = await conversations.save_conversation(
            request, current_user={"email": "synthetic@example.invalid"}, db_pool=conversation_pool
        )
    assert result["persistence_mode"] == "db"
    router_invalidation.assert_awaited_once_with("zantara:conversations:*")
    async with conversation_pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM conversations") == 1
    metric.assert_not_called()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_stage_one_whatsapp_and_repository_share_absent_session_lock(
    conversation_pool, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.app.routers import whatsapp_chat

    monkeypatch.setattr(whatsapp_chat, "_persist_audit_messages", AsyncMock())
    repository = ConversationRepository(conversation_pool)
    append = repository._save_messages_locked
    session = "synthetic-cross-stage"
    await asyncio.gather(
        whatsapp_chat._save_conversation(
            db_pool=conversation_pool,
            wa_user_id="synthetic-wa-user",
            session_id=session,
            existing_row_id=None,
            message_text="wa-in",
            response_text="wa-out",
            client_profile={"source": "wa"},
            sender_name="synthetic",
            phone="synthetic-phone",
        ),
        append(
            session,
            "synthetic-wa-user",
            [{"role": "user", "content": "web-in"}, {"role": "assistant", "content": "web-out"}],
        ),
    )
    async with conversation_pool.acquire() as conn:
        rows = await conn.fetch("SELECT messages FROM conversations WHERE session_id=$1", session)
    assert len(rows) == 1
    assert {message["content"] for message in rows[0]["messages"]} == {
        "wa-in",
        "wa-out",
        "web-in",
        "web-out",
    }
    assert len(rows[0]["messages"]) == 4
    whatsapp_chat._persist_audit_messages.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["route", "service"])
async def test_actual_snapshot_consumer_and_legacy_append_preserve_messages(
    conversation_pool, monkeypatch, consumer
):
    from unittest.mock import MagicMock

    from backend.app.routers import conversations
    from backend.services.misc import conversation_service

    monkeypatch.setattr(conversations, "get_memory_cache", MagicMock())
    monkeypatch.setattr(conversation_service, "get_memory_cache", MagicMock())
    messages = [{"role": "assistant", "content": "snapshot"}]
    if consumer == "route":
        snapshot = conversations.save_conversation(
            conversations.SaveConversationRequest(
                session_id="synthetic-snapshot", messages=messages
            ),
            current_user={"email": "synthetic-user"},
            db_pool=conversation_pool,
        )
    else:
        snapshot = conversation_service.ConversationService(conversation_pool).save_conversation(
            "synthetic-user", messages, session_id="synthetic-snapshot"
        )
    result, _identifier = await asyncio.gather(
        snapshot,
        ConversationRepository(conversation_pool)._save_messages_locked(
            "synthetic-snapshot", "synthetic-user", [{"role": "assistant", "content": "append"}]
        ),
    )
    assert result["persistence_mode"] == "db"
    async with conversation_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT messages FROM conversations WHERE session_id=$1", "synthetic-snapshot"
        )
    assert sorted(m["content"] for row in rows for m in row["messages"]) == ["append", "snapshot"]


@pytest.mark.asyncio
async def test_actual_title_writer_supports_legacy_null_session(conversation_pool, monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app.routers import conversations

    async with conversation_pool.acquire() as conn:
        identifier = await conn.fetchval(
            "INSERT INTO conversations(user_id, session_id, messages, metadata) VALUES ('synthetic-user',NULL,'[]','{}') RETURNING id"
        )
    monkeypatch.setattr(
        conversations, "generate_conversation_title", AsyncMock(return_value="synthetic title")
    )
    await conversations._generate_and_update_title(
        identifier, "synthetic first message", conversation_pool
    )
    async with conversation_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT metadata->>'generated_title' FROM conversations WHERE id=$1", identifier
            )
            == "synthetic title"
        )


@pytest.mark.asyncio
async def test_actual_administrator_retention_preserves_atomic_window(
    conversation_pool, monkeypatch
):
    from backend.app.routers.admin_conversation_cleanup import run_conversation_cleanup
    from backend.core.retention_policy import ALLOW_CLOCK_DELETE_ENV, RETENTION_MIN_DAYS

    monkeypatch.setenv(ALLOW_CLOCK_DELETE_ENV, "1")
    now = datetime.now(timezone.utc)
    async with conversation_pool.acquire() as conn:
        for session, age in [
            ("synthetic-delete", RETENTION_MIN_DAYS + 60),
            ("synthetic-anonymize", RETENTION_MIN_DAYS + 15),
            ("synthetic-fresh", 10),
        ]:
            await conn.execute(
                "INSERT INTO conversations(user_id,session_id,messages,metadata,created_at) VALUES ($1,$2,$3::text::jsonb,'{}',$4)",
                "synthetic-user",
                session,
                '[{"content":"synthetic"}]',
                now - timedelta(days=age),
            )
    result = await run_conversation_cleanup(
        delete_after_days=RETENTION_MIN_DAYS + 30,
        anonymize_after_days=RETENTION_MIN_DAYS,
        dry_run=False,
        _access=True,
        pool=conversation_pool,
    )
    assert (result.deleted_count, result.anonymized_count) == (1, 1)
    async with conversation_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT session_id,messages,metadata FROM conversations ORDER BY session_id"
        )
    assert len(rows) == 2
    by_session = {row["session_id"]: row for row in rows}
    assert by_session["synthetic-anonymize"]["messages"] == []
    assert by_session["synthetic-anonymize"]["metadata"]["anonymized"] is True
    assert by_session["synthetic-fresh"]["messages"] == [{"content": "synthetic"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_owner", ["synthetic-foreign", "anonymized_9000"])
@pytest.mark.parametrize("owner_exists", [True, False])
async def test_actual_whatsapp_append_preserves_owner_scope(
    conversation_pool, monkeypatch, foreign_owner, owner_exists
):
    from unittest.mock import AsyncMock

    from backend.app.routers import whatsapp_chat

    session = "wa_session_synthetic-owner"
    owner = "whatsapp_synthetic-owner"
    before_messages = [{"role": "assistant", "content": "owner-before"}]
    async with conversation_pool.acquire() as conn:
        if owner_exists:
            await conn.execute(
                "INSERT INTO conversations(user_id,session_id,messages,metadata,created_at) "
                "VALUES ($1,$2,$3::text::jsonb,'{}',now()-interval '1 minute')",
                owner,
                session,
                json.dumps(before_messages),
            )
        foreign_id = await conn.fetchval(
            "INSERT INTO conversations(user_id,session_id,messages,metadata) "
            "VALUES ($1,$2,$3::text::jsonb,$4::text::jsonb) RETURNING id",
            foreign_owner,
            session,
            '[{"content":"foreign-before"}]',
            '{"keep":"foreign"}',
        )
        foreign_before = dict(
            await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", foreign_id)
        )
        owner_id = await conn.fetchval(
            "SELECT id FROM conversations WHERE user_id=$1 AND session_id=$2", owner, session
        )
    audit = AsyncMock()
    monkeypatch.setattr(whatsapp_chat, "_persist_audit_messages", audit)
    profile = {"marker": "synthetic-owner-private-profile"}
    await whatsapp_chat._save_conversation(
        db_pool=conversation_pool,
        wa_user_id=owner,
        session_id=session,
        existing_row_id=owner_id,
        message_text="owner-in",
        response_text="owner-out",
        client_profile=profile,
        sender_name="synthetic",
        phone="synthetic-owner",
    )
    async with conversation_pool.acquire() as conn:
        foreign_after = dict(
            await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", foreign_id)
        )
        owned = await conn.fetch(
            "SELECT messages,metadata FROM conversations WHERE user_id=$1 AND session_id=$2",
            owner,
            session,
        )
    assert foreign_after == foreign_before, "OWNER_SCOPE_FOREIGN_ROW_CHANGED"
    assert len(owned) == 1
    expected = (before_messages if owner_exists else []) + [
        {"role": "user", "content": "owner-in"},
        {"role": "assistant", "content": "owner-out"},
    ]
    assert owned[0]["messages"] == expected
    assert owned[0]["metadata"] == profile
    audit.assert_awaited_once()


async def _observe_advisory_wait(observer, task, session_id, row_id, *, bulk=False):
    namespace = 0x43564E30 if session_id is None else 0x43564E53
    key = (
        row_id
        if session_id is None
        else await observer.fetchval("SELECT hashtext($1::text)", session_id)
    )
    if bulk:
        namespace, key = 0x43564E43, 0
    async with asyncio.timeout(2):
        while True:
            waiting = await observer.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND classid=$1::oid AND objid=$2::oid AND objsubid=2 AND NOT granted)",
                namespace,
                key & 0xFFFFFFFF,
            )
            if waiting:
                assert not task.done()
                return
            if task.done():
                await task
                raise AssertionError("Writer completed without waiting for its advisory identity")
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("session", [None, "synthetic-owned-delete"])
async def test_actual_owned_delete_waits_for_its_physical_identity(conversation_pool, session):
    from backend.app.routers import conversations

    user = "synthetic-delete@example.invalid"
    async with conversation_pool.acquire() as conn:
        identifier = await conn.fetchval(
            "INSERT INTO conversations(user_id,session_id,messages,metadata) "
            "VALUES ($1,$2,'[]','{}') RETURNING id",
            user,
            session,
        )
    task = None
    try:
        async with conversation_pool.acquire() as blocker:
            async with blocker.transaction():
                await lock_conversation_session(blocker, session, row_id=identifier)
                task = asyncio.create_task(
                    conversations.delete_conversation(
                        identifier, current_user={"email": user}, db_pool=conversation_pool
                    )
                )
                async with conversation_pool.acquire() as observer:
                    await _observe_advisory_wait(observer, task, session, identifier)
                    late_id = await observer.fetchval(
                        "INSERT INTO conversations(user_id,session_id,messages,metadata) "
                        "VALUES ($1,'synthetic-delete-late','[]','{}') RETURNING id",
                        user,
                    )
                    late_before = dict(
                        await observer.fetchrow("SELECT * FROM conversations WHERE id=$1", late_id)
                    )
            result = await asyncio.wait_for(task, timeout=2)
        assert result["success"] is True
        async with conversation_pool.acquire() as conn:
            assert (
                await conn.fetchval("SELECT count(*) FROM conversations WHERE id=$1", identifier)
                == 0
            )
            assert (
                dict(await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", late_id))
                == late_before
            )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_actual_context_profile_update_waits_and_preserves_late_row(
    conversation_pool, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.services import whatsapp_context_builder, whatsapp_identity

    phone = "synthetic-context"
    session, user = f"wa_session_{phone}", f"whatsapp_{phone}"
    monkeypatch.setattr(
        whatsapp_identity, "resolve_sender_identity", AsyncMock(return_value={"role": "unknown"})
    )
    async with conversation_pool.acquire() as conn:
        identifier = await conn.fetchval(
            "INSERT INTO conversations(user_id,session_id,messages,metadata) "
            "VALUES ($1,$2,'[]','{\"keep\":true,\"message_count\":4}') RETURNING id",
            user,
            session,
        )
        before = dict(await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", identifier))
    task = None
    try:
        async with conversation_pool.acquire() as blocker:
            async with blocker.transaction():
                await lock_conversation_session(blocker, session)
                task = asyncio.create_task(
                    whatsapp_context_builder.build_context(
                        phone=phone,
                        message_text="hello",
                        sender_name="synthetic",
                        db_pool=conversation_pool,
                    )
                )
                async with conversation_pool.acquire() as observer:
                    await _observe_advisory_wait(observer, task, session, identifier)
                    late_id = await observer.fetchval(
                        "INSERT INTO conversations(user_id,session_id,messages,metadata) "
                        "VALUES ($1,$2,'[]','{\"late\":true}') RETURNING id",
                        user,
                        session,
                    )
                    late_before = dict(
                        await observer.fetchrow("SELECT * FROM conversations WHERE id=$1", late_id)
                    )
            result = await asyncio.wait_for(task, timeout=2)
        async with conversation_pool.acquire() as conn:
            after = dict(await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", identifier))
            late_after = dict(
                await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", late_id)
            )
        assert after == {**before, "metadata": result["client_profile"]}
        assert after["metadata"]["keep"] is True
        assert after["metadata"]["message_count"] == 5
        assert late_after == late_before
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["cleanup_old_conversations", "anonymize_user_data"])
async def test_actual_repository_retention_waits_and_preserves_late_row(
    conversation_pool, monkeypatch, method
):
    from backend.core.retention_policy import ALLOW_CLOCK_DELETE_ENV, RETENTION_MIN_DAYS

    monkeypatch.setenv(ALLOW_CLOCK_DELETE_ENV, "1")
    session = "synthetic-maintenance-lock"
    old = datetime.now(timezone.utc) - timedelta(days=RETENTION_MIN_DAYS + 10)
    async with conversation_pool.acquire() as conn:
        identifier = await conn.fetchval(
            "INSERT INTO conversations(user_id,session_id,messages,metadata,created_at) "
            "VALUES ('synthetic-maintenance',$1,'[{\"content\":\"keep\"}]','{\"keep\":true}',$2) RETURNING id",
            session,
            old,
        )
        before = dict(await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", identifier))
    task = None
    try:
        async with conversation_pool.acquire() as blocker:
            async with blocker.transaction():
                await lock_conversation_session(blocker, session)
                operation = getattr(ConversationRepository(conversation_pool), method)
                task = asyncio.create_task(operation(days=RETENTION_MIN_DAYS))
                async with conversation_pool.acquire() as observer:
                    await _observe_advisory_wait(observer, task, session, identifier, bulk=True)
                    late_id = await observer.fetchval(
                        "INSERT INTO conversations(user_id,session_id,messages,metadata,created_at) "
                        "VALUES ('synthetic-late','synthetic-late-lock','[]','{\"late\":true}',$1) RETURNING id",
                        old,
                    )
                    late_before = dict(
                        await observer.fetchrow("SELECT * FROM conversations WHERE id=$1", late_id)
                    )
            assert await asyncio.wait_for(task, timeout=2) == 1
        async with conversation_pool.acquire() as conn:
            target = await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", identifier)
            assert (
                dict(await conn.fetchrow("SELECT * FROM conversations WHERE id=$1", late_id))
                == late_before
            )
        if method == "cleanup_old_conversations":
            assert target is None
        else:
            assert dict(target) == {**before, "user_id": f"anonymized_{identifier}"}
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
