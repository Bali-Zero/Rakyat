"""Conversation writers under a production-shaped role: SELECT+UPDATE, no INSERT, no DELETE.

Production fact (catalog read 2026-11-09): on public.conversations the app role
backend_rag_v2 holds SELECT and UPDATE but not INSERT and not DELETE. This file
drives every real writer against a disposable PG17 with a role of that shape and
records, per case, what the USER would see. It is written to run UNCHANGED on
Rakyat main and on the advisory-lock candidate: it imports nothing that exists on
only one of them and observes behaviour from outside.

Every test prints its CASE lines (use -s) and then pins the outcome that main and
the candidate share. The guilt cases at the bottom prove the checks can fail.

Synthetic data only. Run through backend/tests/db/run_disposable_conversation_pg.py
from <checkout>/apps/backend-rag, with -s to see the CASE table.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.core.pg_json_codec import init_asyncpg_connection

try:  # candidate-only; the test never needs it to exist
    from backend.db.repositories.conversation_repository import (
        ConversationCommitOutcomeUnknown as _Unknown,
    )
except ImportError:  # main
    _Unknown = None

pytestmark = pytest.mark.integration

ROLE = "synthetic_app_limited"
USER = "synthetic-denied@example.invalid"
RESULTS: dict[str, dict[str, Any]] = {}


# --------------------------------------------------------------------------- env


class Env:
    def __init__(self, pool: asyncpg.Pool, admin: asyncpg.Connection, schema: str) -> None:
        self.pool, self.admin, self.schema = pool, admin, schema

    async def rows(self, where: str = "TRUE", *args: Any) -> int:
        return await self.admin.fetchval(f"SELECT count(*) FROM conversations WHERE {where}", *args)

    async def seed(
        self,
        session: str | None,
        user: str = USER,
        messages: list[dict] | None = None,
        age_days: int = 0,
    ) -> int:
        return await self.admin.fetchval(
            "INSERT INTO conversations(user_id,session_id,messages,metadata,created_at) "
            "VALUES ($1,$2,$3::text::jsonb,'{}',$4) RETURNING id",
            user,
            session,
            json.dumps(messages if messages is not None else [{"role": "user", "content": "seed"}]),
            datetime.now(timezone.utc) - timedelta(days=age_days),
        )

    async def message_count(self, where: str, *args: Any) -> int | None:
        value = await self.admin.fetchval(
            f"SELECT jsonb_array_length(messages) FROM conversations WHERE {where} LIMIT 1", *args
        )
        return value


@pytest_asyncio.fixture
async def open_env(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Callable[..., Awaitable[Env]]]:
    import os
    from unittest.mock import MagicMock

    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    identity = await admin.fetchrow(
        "SELECT current_user AS role, current_database() AS db, "
        "system_identifier::text AS system_id FROM pg_control_system()"
    )
    if dict(identity) != {
        "role": "universal_db_test_owner",
        "db": "universal_db_test",
        "system_id": expected,
    }:
        await admin.close()
        pytest.fail("Refusing a database outside this disposable test cluster")

    from backend.app.routers import conversations as router_module
    from backend.services.common import cache

    async def ignore_cache(_pattern: str) -> int:
        return 0

    def close_spawn(coro: Any, *, name: str = "") -> None:
        coro.close()  # never run the LLM title task

    monkeypatch.setattr(cache, "_invalidate_cache", ignore_cache)
    monkeypatch.setattr(router_module, "invalidate_cache", ignore_cache)
    monkeypatch.setattr(router_module, "spawn", close_spawn)
    monkeypatch.setattr(router_module, "get_memory_cache", lambda: MagicMock())

    await admin.execute(f'DROP ROLE IF EXISTS "{ROLE}"')
    await admin.execute(f'CREATE ROLE "{ROLE}" NOLOGIN')
    schemas: list[str] = []
    pools: list[asyncpg.Pool] = []

    async def open_(privs: tuple[str, ...] = ()) -> Env:
        schema = "denied_test_" + uuid4().hex
        schemas.append(schema)
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'SET search_path = "{schema}", public')
        await admin.execute("""
            CREATE TABLE conversations (
                id SERIAL PRIMARY KEY,
                user_id varchar(255) NOT NULL, session_id varchar(255),
                messages jsonb, metadata jsonb,
                created_at timestamptz DEFAULT now()
            );
            CREATE INDEX ON conversations(session_id);
        """)
        # Production shape: SELECT + UPDATE only on the table; sequence usable.
        await admin.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{ROLE}"')
        await admin.execute(f'GRANT SELECT, UPDATE ON conversations TO "{ROLE}"')
        await admin.execute(f'GRANT USAGE, SELECT, UPDATE ON conversations_id_seq TO "{ROLE}"')
        for priv in privs:
            await admin.execute(f'GRANT {priv} ON conversations TO "{ROLE}"')

        async def assume_role(conn: asyncpg.Connection) -> None:
            await conn.execute(f'SET ROLE "{ROLE}"')

        async def init(conn: asyncpg.Connection) -> None:
            await init_asyncpg_connection(conn)
            await assume_role(conn)

        pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=10,
            init=init,
            setup=assume_role,  # pool reset runs RESET ALL, which drops SET ROLE
            server_settings={"search_path": schema + ",public"},
        )
        pools.append(pool)
        return Env(pool, admin, schema)

    try:
        yield open_
    finally:
        for pool in pools:
            await pool.close()
        await admin.execute("RESET search_path")
        for schema in schemas:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.execute(f'DROP OWNED BY "{ROLE}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{ROLE}"')
        await admin.close()


# ------------------------------------------------------------------- instrument


async def observe(
    name: str,
    call: Callable[[], Awaitable[Any]],
    env: Env,
    where: str = "TRUE",
    *args: Any,
    extra: str = "",
) -> dict[str, Any]:
    """Run one writer as the app calls it and print one compact CASE line."""
    started = time.perf_counter()
    body: Any = None
    status: str = "return"
    exc_type = "-"
    try:
        body = await asyncio.wait_for(call(), timeout=60)
    except HTTPException as exc:
        status, exc_type, body = str(exc.status_code), type(exc).__name__, exc.detail
    except Exception as exc:  # noqa: BLE001 - the instrument records every outcome
        status, exc_type = "raised", type(exc).__name__
        body = str(exc)[:80] if not isinstance(exc, asyncpg.PostgresError) else exc.sqlstate
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    mode = body.get("persistence_mode", "-") if isinstance(body, dict) else "-"
    if status == "return" and isinstance(body, dict):
        status = "200"
    rows = await env.rows(where, *args)
    outcome = {
        "status": status,
        "mode": mode,
        "ms": elapsed_ms,
        "rows": rows,
        "exc": exc_type,
        "body": body,
    }
    RESULTS[name] = outcome
    print(
        f"CASE {name} {status} {mode} {elapsed_ms} {rows} {exc_type}"
        + (f" {extra}" if extra else "")
        + (
            f" body={body!r}"
            if not isinstance(body, dict) or "persistence_mode" not in body
            else ""
        ),
        flush=True,
    )
    return outcome


def user() -> dict[str, str]:
    return {"email": USER}


def save_request(session: str, text: str = "synthetic hello") -> Any:
    from backend.app.routers import conversations

    return conversations.SaveConversationRequest(
        session_id=session, messages=[{"role": "user", "content": text}]
    )


# ---------------------------------------------------------------------- sanity


@pytest.mark.asyncio
async def test_instrument_role_is_limited_and_survives_pool_reuse(open_env) -> None:
    env = await open_env()
    for _ in range(5):  # release/reacquire cycles run the pool's RESET ALL
        async with env.pool.acquire() as conn:
            assert await conn.fetchval("SELECT current_user") == ROLE
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user,'conversations','SELECT')"
                " AND has_table_privilege(current_user,'conversations','UPDATE')"
            )
            assert not await conn.fetchval(
                "SELECT has_table_privilege(current_user,'conversations','INSERT')"
                " OR has_table_privilege(current_user,'conversations','DELETE')"
            )


# ------------------------------------------------------------------------ cases
# Bounds are deliberately loose upper limits: the denied /save path sleeps 1s + 2s
# between its three attempts on BOTH checkouts (measured ~3.0s), nothing else waits.

SAVE_BOUND_MS = 6000
FAST_BOUND_MS = 2000


def assert_memory_fallback(out: dict[str, Any]) -> None:
    assert out["exc"] == "-" and out["status"] == "200", out
    assert out["mode"] == "memory_fallback"
    assert out["body"]["success"] is True and out["body"]["conversation_id"] == 0
    assert out["ms"] < SAVE_BOUND_MS


@pytest.mark.asyncio
async def test_denied_web_save_and_title(open_env) -> None:
    from backend.app.routers import conversations

    env = await open_env()
    out = await observe(
        "1_web_save_new",
        lambda: conversations.save_conversation(
            save_request("syn-web-new"), current_user=user(), db_pool=env.pool
        ),
        env,
        "session_id=$1",
        "syn-web-new",
    )
    assert_memory_fallback(out)  # chat stays up, no 5xx, no commit_unknown
    assert out["rows"] == 0

    existing = await env.seed("syn-web-existing")
    out = await observe(
        "2a_web_save_existing_row",
        lambda: conversations.save_conversation(
            save_request("syn-web-existing", "second"), current_user=user(), db_pool=env.pool
        ),
        env,
        "session_id=$1",
        "syn-web-existing",
    )
    assert_memory_fallback(out)  # /save always INSERTs, so an existing row does not help
    assert out["rows"] == 1 and await env.message_count("id=$1", existing) == 1

    async def fake_title(**_kw: Any) -> str:
        return "synthetic title"

    original = conversations.generate_conversation_title
    conversations.generate_conversation_title = fake_title  # type: ignore[assignment]
    try:
        out = await observe(
            "2b_title_update_existing_row",
            lambda: conversations._generate_and_update_title(existing, "synthetic", env.pool),
            env,
            "id=$1",
            existing,
        )
    finally:
        conversations.generate_conversation_title = original  # type: ignore[assignment]
    assert out["status"] == "return" and out["exc"] == "-" and out["ms"] < FAST_BOUND_MS
    title = await env.admin.fetchval(
        "SELECT metadata->>'generated_title' FROM conversations WHERE id=$1", existing
    )
    assert title == "synthetic title"  # the UPDATE privilege is enough


@pytest.mark.asyncio
async def test_denied_whatsapp_insert_is_swallowed_and_update_still_works(
    open_env, monkeypatch
) -> None:
    from unittest.mock import AsyncMock

    from backend.app.routers import whatsapp_chat

    audit = AsyncMock()
    monkeypatch.setattr(whatsapp_chat, "_persist_audit_messages", audit)
    env = await open_env()

    def wa(session: str, row_id: int | None) -> Callable[[], Awaitable[Any]]:
        return lambda: whatsapp_chat._save_conversation(
            db_pool=env.pool,
            wa_user_id="whatsapp_synthetic",
            session_id=session,
            existing_row_id=row_id,
            message_text="wa-in",
            response_text="wa-out",
            client_profile={"source": "wa"},
            sender_name="synthetic",
            phone="synthetic-phone",
        )

    out = await observe(
        "3a_wa_absent_session", wa("wa_s_absent", None), env, "session_id=$1", "wa_s_absent"
    )
    assert out["status"] == "return" and out["exc"] == "-" and out["body"] is None
    assert out["rows"] == 0 and out["ms"] < FAST_BOUND_MS  # no retry loop, no hang
    assert audit.await_count == 1  # the reply flow still reaches the audit step

    row = await env.seed("wa_s_exist", user="whatsapp_synthetic")
    out = await observe(
        "3b_wa_existing_row", wa("wa_s_exist", row), env, "session_id=$1", "wa_s_exist"
    )
    assert out["status"] == "return" and out["exc"] == "-" and out["ms"] < FAST_BOUND_MS
    assert await env.message_count("id=$1", row) == 3  # 1 seeded + user + assistant
    assert (
        await env.admin.fetchval("SELECT metadata->>'source' FROM conversations WHERE id=$1", row)
        == "wa"
    )
    assert audit.await_count == 2


@pytest.mark.asyncio
async def test_denied_clear_and_delete_are_503_and_remove_nothing(open_env) -> None:
    from backend.app.routers import conversations

    env = await open_env()
    for s in ("syn-c1", "syn-c2"):
        await env.seed(s)
    unavailable = "Database service temporarily unavailable"

    out = await observe(
        "4a_clear_session",
        lambda: conversations.clear_conversation_history(
            session_id="syn-c1", current_user=user(), db_pool=env.pool
        ),
        env,
        "user_id=$1",
        USER,
    )
    assert (out["status"], out["body"], out["rows"]) == ("503", unavailable, 2)
    assert out["ms"] < FAST_BOUND_MS
    out = await observe(
        "4b_clear_all",
        lambda: conversations.clear_conversation_history(
            session_id=None, current_user=user(), db_pool=env.pool
        ),
        env,
        "user_id=$1",
        USER,
    )
    assert (out["status"], out["body"], out["rows"]) == ("503", unavailable, 2)
    ident = await env.seed("syn-d1")
    out = await observe(
        "4c_delete_by_id",
        lambda: conversations.delete_conversation(ident, current_user=user(), db_pool=env.pool),
        env,
        "id=$1",
        ident,
    )
    assert (out["status"], out["body"], out["rows"]) == ("503", unavailable, 1)
    out = await observe(
        "4d_delete_missing_id",
        lambda: conversations.delete_conversation(987654, current_user=user(), db_pool=env.pool),
        env,
    )
    assert out["status"] == "404"  # an unknown id is still 404, not masked by the privilege error
    assert out["ms"] < FAST_BOUND_MS


@pytest.mark.asyncio
async def test_denied_service_and_repository(open_env) -> None:
    from backend.db.repositories.conversation_repository import ConversationRepository
    from backend.services.misc import conversation_service

    env = await open_env()
    service = conversation_service.ConversationService(env.pool)
    repo = ConversationRepository(env.pool)
    msgs = [{"role": "user", "content": "synthetic"}]

    out = await observe(
        "5a_service_save_new",
        lambda: service.save_conversation(USER, msgs, session_id="syn-svc-new"),
        env,
        "session_id=$1",
        "syn-svc-new",
    )
    assert out["mode"] == "memory_fallback" and out["exc"] == "-" and out["rows"] == 0
    assert out["ms"] < FAST_BOUND_MS  # the service has no retry loop
    await env.seed("syn-svc-exist")
    out = await observe(
        "5b_service_save_existing_row",
        lambda: service.save_conversation(USER, msgs, session_id="syn-svc-exist"),
        env,
        "session_id=$1",
        "syn-svc-exist",
    )
    assert out["mode"] == "memory_fallback" and out["exc"] == "-" and out["rows"] == 1

    # ConversationRepository has no save_conversation; save_messages is the writer the
    # SSE chat stream (agentic_rag) calls, so it is the one that matters for users.
    out = await observe(
        "5c_repo_save_messages_new",
        lambda: repo.save_messages("syn-repo-new", USER, msgs),
        env,
        "session_id=$1",
        "syn-repo-new",
    )
    assert out["status"] == "return" and out["body"] is None and out["rows"] == 0
    assert out["exc"] == "-" and out["ms"] < FAST_BOUND_MS

    await env.seed("syn-repo-exist")
    out = await observe(
        "5d_repo_save_messages_existing",
        lambda: repo.save_messages("syn-repo-exist", USER, msgs),
        env,
        "session_id=$1",
        "syn-repo-exist",
    )
    assert isinstance(out["body"], int) and out["rows"] == 1
    assert await env.message_count("session_id=$1", "syn-repo-exist") == 2


@pytest.mark.asyncio
async def test_denied_retention_paths(open_env, monkeypatch) -> None:
    from backend.app.routers.admin_conversation_cleanup import run_conversation_cleanup
    from backend.core.retention_policy import ALLOW_CLOCK_DELETE_ENV, RETENTION_MIN_DAYS
    from backend.db.repositories.conversation_repository import ConversationRepository

    monkeypatch.setenv(ALLOW_CLOCK_DELETE_ENV, "1")
    env = await open_env()
    await env.seed("syn-old-delete", age_days=RETENTION_MIN_DAYS + 60)
    await env.seed("syn-old-anon", age_days=RETENTION_MIN_DAYS + 15)
    out = await observe(
        "6a_admin_cleanup_dry_run_false",
        lambda: run_conversation_cleanup(
            delete_after_days=RETENTION_MIN_DAYS + 30,
            anonymize_after_days=RETENTION_MIN_DAYS,
            dry_run=False,
            _access=True,
            pool=env.pool,
        ),
        env,
    )
    # Admin-only endpoint: the raw privilege error escapes (a 500 for the operator),
    # nothing is deleted, nothing is half-anonymized.
    assert (out["status"], out["exc"], out["body"], out["rows"]) == (
        "raised",
        "InsufficientPrivilegeError",
        "42501",
        2,
    )
    assert await env.rows("user_id LIKE 'anonymized_%'") == 0
    repo = ConversationRepository(env.pool)
    out = await observe(
        "6b_repo_cleanup_old", lambda: repo.cleanup_old_conversations(RETENTION_MIN_DAYS), env
    )
    assert out["body"] == 0 and out["rows"] == 2 and out["exc"] == "-"  # failure swallowed as 0
    out = await observe(
        "6c_repo_anonymize", lambda: repo.anonymize_user_data(RETENTION_MIN_DAYS), env
    )
    assert out["body"] == 2 and await env.rows("user_id LIKE 'anonymized_%'") == 2  # UPDATE works


@pytest.mark.asyncio
async def test_denied_concurrent_saves_do_not_wedge_pool(open_env) -> None:
    from backend.app.routers import conversations

    env = await open_env()
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            conversations.save_conversation(
                save_request(f"syn-conc-{i}"), current_user=user(), db_pool=env.pool
            )
            for i in range(12)  # more than the pool's max_size of 10
        ),
        return_exceptions=True,
    )
    wall = int((time.perf_counter() - started) * 1000)
    assert all(isinstance(r, dict) for r in results), results
    assert {r["persistence_mode"] for r in results} == {"memory_fallback"}
    print(
        f"CASE 7_concurrent_12_denied_saves 200 memory_fallback {wall} {await env.rows()} -",
        flush=True,
    )
    assert wall < 12000  # retries overlap; they do not queue behind each other or hold connections
    assert await env.rows() == 0
    async with env.pool.acquire() as conn:
        assert await conn.fetchval("SELECT 1") == 1
        assert await conn.fetchval("SELECT current_user") == ROLE


# ------------------------------------------------------------------------ guilt


async def assert_saved_in_db(env: Env) -> None:
    """The success-shaped checks the denied tests must NOT satisfy."""
    from backend.app.routers import conversations

    started = time.perf_counter()
    result = await conversations.save_conversation(
        save_request("syn-guilt"), current_user=user(), db_pool=env.pool
    )
    elapsed = (time.perf_counter() - started) * 1000
    assert result["persistence_mode"] == "db"
    assert result["conversation_id"] > 0
    assert await env.rows("session_id=$1", "syn-guilt") == 1
    assert elapsed < 1500, "a granted INSERT must not pay the retry sleeps"


@pytest.mark.asyncio
async def test_guilt_with_insert_granted_every_insert_writer_reports_db(
    open_env, monkeypatch
) -> None:
    from unittest.mock import AsyncMock

    from backend.app.routers import whatsapp_chat
    from backend.db.repositories.conversation_repository import ConversationRepository
    from backend.services.misc import conversation_service

    monkeypatch.setattr(whatsapp_chat, "_persist_audit_messages", AsyncMock())
    env = await open_env(("INSERT",))
    await assert_saved_in_db(env)
    msgs = [{"role": "user", "content": "synthetic"}]
    out = await conversation_service.ConversationService(env.pool).save_conversation(
        USER, msgs, session_id="syn-guilt-svc"
    )
    assert out["persistence_mode"] == "db" and out["conversation_id"] > 0
    assert await ConversationRepository(env.pool).save_messages("syn-guilt-repo", USER, msgs) > 0
    await whatsapp_chat._save_conversation(
        db_pool=env.pool,
        wa_user_id="whatsapp_synthetic",
        session_id="wa_guilt",
        existing_row_id=None,
        message_text="in",
        response_text="out",
        client_profile={},
        sender_name="synthetic",
        phone="synthetic-phone",
    )
    assert await env.rows("session_id IN ('syn-guilt-svc','syn-guilt-repo','wa_guilt')") == 3


@pytest.mark.asyncio
async def test_guilt_with_delete_granted_clear_and_delete_succeed(open_env) -> None:
    from backend.app.routers import conversations

    env = await open_env(("DELETE",))
    one = await env.seed("syn-g-del-1")
    await env.seed("syn-g-del-2")
    out = await conversations.delete_conversation(one, current_user=user(), db_pool=env.pool)
    assert out == {"success": True, "deleted_id": one}
    out = await conversations.clear_conversation_history(
        session_id=None, current_user=user(), db_pool=env.pool
    )
    assert out == {"success": True, "deleted_count": 1}
    assert await env.rows("user_id=$1", USER) == 0


@pytest.mark.asyncio
async def test_guilt_control_the_db_checks_fail_under_the_denied_role(open_env) -> None:
    """The same success checks must FAIL when INSERT is missing, or they prove nothing."""
    env = await open_env()
    with pytest.raises(AssertionError):
        await assert_saved_in_db(env)
