"""Every session-keyed conversation lookup is bound to its owner (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
Synthetic data only. Each site has a guilt case (another owner asks) and an
innocence case (the owner asks).
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.core.pg_json_codec import init_asyncpg_connection
from backend.db.repositories.conversation_repository import ConversationRepository
from backend.services.common import cache

pytestmark = pytest.mark.integration

ALICE = "alice@example.test"
BOB = "bob@example.test"
PHONE = "+10000000001"
WA_USER = f"whatsapp_{PHONE}"
WA_SESSION = f"wa_session_{PHONE}"
ALICE_MSG = [{"role": "user", "content": "alice private text"}]
BOB_MSG = [{"role": "user", "content": "bob text"}]
WA_MSG = [{"role": "user", "content": "whatsapp client text"}]


@pytest_asyncio.fixture
async def pool(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[asyncpg.Pool]:
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
        schema = "owner_bind_" + uuid4().hex
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=10,
            init=init_asyncpg_connection,
            server_settings={"search_path": schema + ",public"},
        )
        try:
            async with created.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE conversations (
                        id SERIAL PRIMARY KEY,
                        user_id varchar(255) NOT NULL, session_id varchar(255),
                        messages jsonb, metadata jsonb,
                        created_at timestamptz DEFAULT now()
                    );
                    CREATE INDEX ON conversations(session_id);
                    CREATE TABLE clients (
                        id SERIAL PRIMARY KEY, full_name text, email text, status text,
                        first_contact_date timestamptz, created_by text,
                        last_interaction_date timestamptz
                    );
                    CREATE TABLE interactions (
                        id SERIAL PRIMARY KEY, client_id int, conversation_id int,
                        interaction_type text, channel text, summary text,
                        full_content text, team_member text, direction text,
                        interaction_date timestamptz
                    );
                """)

            async def ignore_cache(_pattern: str) -> int:
                return 0

            from backend.app.routers import conversations as conv_router
            from backend.app.routers import crm_interactions as crm_router

            monkeypatch.setattr(cache, "_invalidate_cache", ignore_cache)
            monkeypatch.setattr(conv_router, "invalidate_cache", ignore_cache)
            monkeypatch.setattr(crm_router, "invalidate_cache", ignore_cache)
            monkeypatch.setattr(conv_router, "spawn", lambda coro, **kw: coro.close())
            monkeypatch.setattr(conv_router, "get_memory_cache", lambda: MagicMock())
            yield created
        finally:
            await created.close()
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    finally:
        await admin.close()


async def seed(pool: asyncpg.Pool, user: str, session: str, messages: list[dict]) -> int:
    return await pool.fetchval(
        "INSERT INTO conversations(user_id, session_id, messages, metadata) "
        "VALUES ($1, $2, $3::text::jsonb, '{}') RETURNING id",
        user,
        session,
        json.dumps(messages),
    )


async def row_of(pool: asyncpg.Pool, row_id: int) -> tuple[str, str]:
    row = await pool.fetchrow(
        "SELECT user_id, messages::text AS m FROM conversations WHERE id = $1", row_id
    )
    return row["user_id"], row["m"]


# ------------------------------------------------------------ repository writer


@pytest.mark.asyncio
async def test_save_messages_never_appends_to_another_owners_row(pool) -> None:
    repo = ConversationRepository(pool)
    alice_id = await repo.save_messages("shared-session", ALICE, ALICE_MSG)
    before = await row_of(pool, alice_id)

    bob_id = await repo.save_messages("shared-session", BOB, BOB_MSG)

    assert bob_id is not None and bob_id != alice_id
    assert await row_of(pool, alice_id) == before
    assert (await row_of(pool, bob_id))[0] == BOB
    assert json.loads((await row_of(pool, bob_id))[1]) == BOB_MSG

    # innocence: the owner's second save still appends to the owner's row
    second = [{"role": "assistant", "content": "alice second"}]
    assert await repo.save_messages("shared-session", ALICE, second) == alice_id
    assert json.loads((await row_of(pool, alice_id))[1]) == ALICE_MSG + second
    assert await pool.fetchval("SELECT count(*) FROM conversations") == 2


@pytest.mark.asyncio
async def test_web_user_with_wa_session_id_leaves_the_whatsapp_row_alone(pool) -> None:
    wa_id = await seed(pool, WA_USER, WA_SESSION, WA_MSG)
    before = await row_of(pool, wa_id)

    web_id = await ConversationRepository(pool).save_messages(WA_SESSION, ALICE, ALICE_MSG)

    assert web_id != wa_id
    assert await row_of(pool, wa_id) == before

    # innocence: the WhatsApp context builder still reads only the WhatsApp row
    from backend.services.whatsapp_context_builder import build_context

    ctx = await build_context(PHONE, "Synthetic", "hello", pool)
    assert ctx["conversation_history"] == WA_MSG


# ------------------------------------------------------------------ get_messages


@pytest.mark.asyncio
async def test_get_messages_is_bound_to_the_owner(pool) -> None:
    await seed(pool, ALICE, "s-get", ALICE_MSG)
    repo = ConversationRepository(pool)
    assert await repo.get_messages("s-get", BOB) == []
    assert await repo.get_messages("s-get", ALICE) == ALICE_MSG


# ----------------------------------------------------- agentic history (session)


@pytest.mark.asyncio
async def test_agentic_history_by_session_is_bound_to_the_owner(pool) -> None:
    from backend.app.routers.agentic_rag import get_conversation_history_for_agentic

    await seed(pool, ALICE, "s-agentic", ALICE_MSG)
    assert await get_conversation_history_for_agentic(None, "s-agentic", BOB, pool) == []
    assert await get_conversation_history_for_agentic(None, "s-agentic", ALICE, pool) == ALICE_MSG


# --------------------------------------------------------------- /save row lock


@pytest.mark.asyncio
async def test_save_route_locks_only_the_callers_row(pool) -> None:
    from backend.app.routers import conversations

    alice_id = await seed(pool, ALICE, "s-route", ALICE_MSG)
    # Hold Alice's row: a lookup that is not bound to the caller would block on it.
    async with pool.acquire() as holder:
        async with holder.transaction():
            await holder.fetchrow("SELECT id FROM conversations WHERE id = $1 FOR UPDATE", alice_id)
            request = conversations.SaveConversationRequest(session_id="s-route", messages=BOB_MSG)
            out = await conversations.save_conversation(
                request, current_user={"email": BOB}, db_pool=pool
            )
    assert out["success"] is True and out["conversation_id"] != alice_id
    assert (
        await pool.fetchval(
            "SELECT user_id FROM conversations WHERE id = $1", out["conversation_id"]
        )
        == BOB
    )
    assert json.loads((await row_of(pool, alice_id))[1]) == ALICE_MSG


@pytest.mark.asyncio
async def test_save_route_lookup_sql_carries_the_owner() -> None:
    from backend.app.routers import conversations

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 7})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(conversations, "conversation_write_connection", return_value=cm),
        patch.object(conversations, "lock_conversation_session", new=AsyncMock()),
        patch.object(conversations, "invalidate_cache", new=AsyncMock()),
        patch.object(conversations, "spawn", lambda coro, **kw: coro.close()),
        patch.object(conversations, "get_memory_cache", lambda: MagicMock()),
    ):
        await conversations.save_conversation(
            conversations.SaveConversationRequest(session_id="s-x", messages=BOB_MSG),
            current_user={"email": BOB},
            db_pool=MagicMock(),
        )
    lookup_sql, *lookup_args = conn.fetchrow.await_args_list[0].args
    assert "user_id = $2" in lookup_sql and lookup_args == ["s-x", BOB]


# ------------------------------------------------------- CRM interaction import


async def import_content(pool, conversation_id: int, user: dict, team_member: str) -> str:
    from backend.app.routers.crm_interactions import create_interaction_from_conversation

    out = await create_interaction_from_conversation(
        conversation_id=conversation_id,
        client_email="client@example.test",
        team_member=team_member,
        summary="synthetic summary",
        current_user=user,
        db_pool=pool,
    )
    return await pool.fetchval(
        "SELECT full_content FROM interactions WHERE id = $1", out["interaction_id"]
    )


@pytest.mark.asyncio
async def test_crm_import_non_owner_gets_empty_owner_and_admin_get_transcript(pool) -> None:
    conv_id = await seed(pool, ALICE, "s-crm", ALICE_MSG)
    assert await import_content(pool, conv_id, {"email": BOB, "role": "member"}, BOB) == ""
    owner = await import_content(pool, conv_id, {"email": ALICE, "role": "member"}, ALICE)
    assert owner == "USER: alice private text"
    admin = await import_content(pool, conv_id, {"email": BOB, "role": "admin"}, BOB)
    assert admin == "USER: alice private text"


@pytest.mark.asyncio
async def test_crm_import_owner_match_is_case_insensitive(pool) -> None:
    conv_id = await seed(pool, "Alice@Example.Test", "s-crm-case", ALICE_MSG)
    got = await import_content(pool, conv_id, {"email": ALICE, "role": "member"}, ALICE)
    assert got == "USER: alice private text"


# -------------------------------------------------------------- WhatsApp routes


@pytest.mark.asyncio
async def test_whatsapp_routes_never_serve_a_web_look_alike(pool) -> None:
    from backend.app.routers.whatsapp_conversations import (
        get_whatsapp_conversations,
        get_whatsapp_messages,
    )

    user = {"email": ALICE}
    # Look-alike only: a web user chose the WhatsApp-shaped session id.
    await seed(pool, ALICE, WA_SESSION, ALICE_MSG)
    assert await get_whatsapp_messages(PHONE, 100, pool, user) == []
    assert await get_whatsapp_conversations(50, 0, pool, user) == []

    # A WhatsApp-shaped user_id on a web session id is not a chat either.
    await seed(pool, WA_USER, "plain-session", WA_MSG)
    assert await get_whatsapp_conversations(50, 0, pool, user) == []

    # innocence: the real WhatsApp row is served, the look-alike is not
    await seed(pool, WA_USER, WA_SESSION, WA_MSG)
    messages = await get_whatsapp_messages(PHONE, 100, pool, user)
    assert [m["message_text"] for m in messages] == ["whatsapp client text"]
    listed = await get_whatsapp_conversations(50, 0, pool, user)
    assert len(listed) == 1 and listed[0]["last_message"] == "whatsapp client text"


# ------------------------------------------------------------------ neural pulse


@pytest.mark.asyncio
async def test_neural_pulse_last_activity_carries_no_session_id() -> None:
    from backend.app.routers.dashboard_summary import get_neural_pulse

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=WA_SESSION)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    memory = AsyncMock()
    memory.get_stats = AsyncMock(return_value={"total_facts": 0})
    qdrant = AsyncMock()
    qdrant.get_stats = AsyncMock(return_value={"total_documents": 0})
    with (
        patch("backend.app.routers.dashboard_summary._cache") as mock_cache,
        patch("backend.app.routers.dashboard_summary.CollectiveMemoryService", return_value=memory),
        patch("backend.app.routers.dashboard_summary.QdrantClient", return_value=qdrant),
    ):
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()
        result = await get_neural_pulse(db_pool=pool)
    assert result["last_activity"] == "Last chat recorded"
    assert PHONE not in json.dumps(result) and "wa_session" not in json.dumps(result)
