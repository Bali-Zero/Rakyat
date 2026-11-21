"""Every session-keyed conversation lookup is bound to its owner (real PostgreSQL).

Run through backend/tests/db/run_disposable_conversation_pg.py (skips without it).
Synthetic data only. Each site has a guilt case (another owner asks) and an
innocence case (the owner asks).
"""

from __future__ import annotations

import inspect
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
                        id SERIAL PRIMARY KEY, client_id int,
                        conversation_id int REFERENCES conversations(id) ON DELETE SET NULL,
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


# ------------------------------------------------------------------ get_messages


@pytest.mark.asyncio
async def test_get_messages_is_bound_to_the_owner(pool) -> None:
    await seed(pool, ALICE, "s-get", ALICE_MSG)
    repo = ConversationRepository(pool)
    # on the unbound signature the owner cannot even be passed
    assert "user_id" in inspect.signature(repo.get_messages).parameters
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


async def crm_counts(pool) -> tuple[int, int]:
    return (
        await pool.fetchval("SELECT count(*) FROM clients"),
        await pool.fetchval("SELECT count(*) FROM interactions"),
    )


async def refused_status(pool, conversation_id: int, user: dict, team_member: str) -> int:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await import_content(pool, conversation_id, user, team_member)
    assert caught.value.detail == "Conversation not found"
    return caught.value.status_code


@pytest.mark.asyncio
async def test_crm_import_non_owner_gets_404_and_creates_nothing(pool) -> None:
    conv_id = await seed(pool, ALICE, "s-crm", ALICE_MSG)
    member_bob = {"email": BOB, "role": "member"}
    assert await refused_status(pool, conv_id, member_bob, BOB) == 404
    assert await crm_counts(pool) == (0, 0)  # no prospect auto-created
    # a missing id is the same answer, for a member and for an admin
    assert await refused_status(pool, conv_id + 999, member_bob, BOB) == 404
    assert await refused_status(pool, conv_id + 999, {"email": BOB, "role": "admin"}, BOB) == 404
    assert await crm_counts(pool) == (0, 0)


@pytest.mark.asyncio
async def test_crm_import_owner_and_admin_get_the_transcript(pool) -> None:
    conv_id = await seed(pool, ALICE, "s-crm-ok", ALICE_MSG)
    owner = await import_content(pool, conv_id, {"email": ALICE, "role": "member"}, ALICE)
    assert owner == "USER: alice private text"
    admin = await import_content(pool, conv_id, {"email": BOB, "role": "admin"}, BOB)
    assert admin == "USER: alice private text"
    # the real FK is in force: deleting the conversation nulls the link, keeps the interaction
    await pool.execute("DELETE FROM conversations WHERE id = $1", conv_id)
    assert await pool.fetchval("SELECT count(*) FROM interactions") == 2
    assert (
        await pool.fetchval("SELECT count(*) FROM interactions WHERE conversation_id IS NULL") == 2
    )


@pytest.mark.asyncio
async def test_crm_import_member_without_token_email_is_403_before_any_db_work() -> None:
    from fastapi import HTTPException

    from backend.app.routers.crm_interactions import create_interaction_from_conversation

    class NoDatabase:
        def acquire(self):
            raise AssertionError("database touched")

    for user in ({"role": "member"}, {"email": None, "role": "member"}, {"email": ""}):
        with pytest.raises(HTTPException) as caught:
            await create_interaction_from_conversation(
                conversation_id=1,
                client_email="client@example.test",
                team_member="",
                summary=None,
                current_user=user,
                db_pool=NoDatabase(),
            )
        assert caught.value.status_code == 403


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


# ------------------------------------------------------ Instagram readers (G1)


@pytest.mark.asyncio
async def test_instagram_never_serves_a_web_look_alike(pool) -> None:
    from backend.app.routers.instagram_chat import (
        get_instagram_conversations,
        get_instagram_messages,
    )

    await seed(pool, ALICE, "ig_session_88", ALICE_MSG)
    assert await get_instagram_messages("88", 100, pool) == []
    assert await get_instagram_conversations(50, 0, pool) == []

    # innocence: rows owned by the channel are served
    await seed(pool, "instagram_88", "ig_session_88", WA_MSG)
    ig = await get_instagram_messages("88", 100, pool)
    assert [m["message_text"] for m in ig] == ["whatsapp client text"]
    assert len(await get_instagram_conversations(50, 0, pool)) == 1


# --------------------------------------- agentic history compatibility (G5)


@pytest.mark.asyncio
async def test_agentic_history_finds_email_rows_by_profile_id_and_any_case(pool) -> None:
    from backend.app.routers.agentic_rag import get_conversation_history_for_agentic

    await pool.execute("CREATE TABLE user_profiles (id uuid PRIMARY KEY, email text)")
    profile_id = "00000000-0000-0000-0000-000000000042"
    await pool.execute("INSERT INTO user_profiles VALUES ($1::uuid, $2)", profile_id, ALICE)
    await seed(pool, ALICE, "s-profile", ALICE_MSG)
    await seed(pool, "Carol@Example.Test", "s-mixed", BOB_MSG)

    assert (
        await get_conversation_history_for_agentic(None, "s-profile", profile_id, pool) == ALICE_MSG
    )
    assert (
        await get_conversation_history_for_agentic(None, "s-mixed", "carol@example.test", pool)
        == BOB_MSG
    )
    assert await get_conversation_history_for_agentic(None, "s-mixed", BOB, pool) == []


# ------------------------------------------------------- API key principal (M1)
