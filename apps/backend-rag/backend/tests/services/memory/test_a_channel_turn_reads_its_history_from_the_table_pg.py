"""A channel turn reads its history from the table.

The engine used to load a session's context once, cache it, and write the same
snapshot back to the cache at the end of every turn with a fresh hour to live.
Every later turn of the chat was a cache hit on that first snapshot: the
assistant never saw the user's later messages, nor any of its own replies.

These tests run turns the way the channel router runs them (the inbound row is
stored before the engine is called, the reply after it) against a real
``conversation_messages`` table, so the statement the engine sends is the one
that is judged. Invented rows.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

import backend.conversation.engine as engine_module
from backend.channels.base import ChannelMessage
from backend.conversation.engine import ConversationEngine

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

# The table as migration 070 creates it.
_TABLE = """
CREATE TABLE conversation_messages (
    id BIGSERIAL PRIMARY KEY,
    client_id INTEGER,
    channel TEXT NOT NULL,
    direction TEXT NOT NULL,
    sender_id TEXT,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_SESSION = "synthetic-session-one"
_KEY = f"zantara:session_ctx:{_SESSION}"


class _Cache:
    """The session cache: records every call made to it; can hold a snapshot, or fail."""

    def __init__(self, *, fails: bool = False) -> None:
        self.values: dict[str, Any] = {}
        self.calls: list[tuple[str, str]] = []
        self.fails = fails

    async def _called(self, what: str, key: str) -> None:
        self.calls.append((what, key))
        if self.fails:
            raise RuntimeError("the cache is down")

    async def get(self, key: str) -> Any:
        await self._called("get", key)
        return self.values.get(key)

    async def set(self, key: str, value: Any, **_: Any) -> bool:
        await self._called("set", key)
        self.values[key] = value
        return True

    async def delete(self, key: str) -> bool:
        await self._called("delete", key)
        return self.values.pop(key, None) is not None


class _Assistant:
    """Stands in for the orchestrator: keeps the history each turn was given."""

    def __init__(self) -> None:
        self.given: list[list[dict[str, str]]] = []
        self.reply = "a reply"

    async def stream_query(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.given.append([dict(entry) for entry in kwargs["conversation_history"]])
        yield {"type": "answer", "data": {"text": self.reply}}


class _Table:
    """The pool the engine holds: the real one, counting the statements sent through it."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.statements: list[str] = []
        self.broken = False

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        self.statements.append(query)
        if self.broken:
            raise asyncpg.PostgresConnectionError("the table cannot be read")
        return await self._pool.fetch(query, *args)


@pytest_asyncio.fixture
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    admin = await asyncpg.connect(_DB_URL)
    schema = "channel_history_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            await c.execute(_TABLE)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


class _Chat:
    """One engine, its table, its cache, and turns run as the channel router runs them."""

    def __init__(self, pool: asyncpg.Pool, cache: _Cache | None = None) -> None:
        self.pool = pool
        self.table = _Table(pool)
        self.cache = cache or _Cache()
        self.assistant = _Assistant()
        self.engine = ConversationEngine(self.assistant)  # type: ignore[arg-type]
        self.engine._db_pool = self.table
        self._clock = 0

    async def stores(self, direction: str, text: str, session: str | None = _SESSION) -> None:
        """A row as the router stores it: the session id inside the metadata."""
        self._clock += 1
        metadata = "{}" if session is None else f'{{"session_id": "{session}"}}'
        await self.pool.execute(
            "INSERT INTO conversation_messages (channel, direction, content, metadata, created_at)"
            " VALUES ('instagram', $1, $2, $3::jsonb, now() + make_interval(secs => $4))",
            direction,
            text,
            metadata,
            float(self._clock),
        )

    async def turn(self, text: str, session: str = _SESSION) -> list[str]:
        """Inbound row, then the engine, then the reply row. Returns what the engine answered."""
        await self.stores("inbound", text, session)
        message = ChannelMessage(
            user_id="synthetic-user", session_id=session, text=text, channel="instagram"
        )
        with patch("backend.core.cache.get_cache_service", return_value=self.cache):
            answers = [r.text async for r in self.engine.process_message(message, {})]
        await self.stores("outbound", self.assistant.reply, session)
        return answers

    def was_given(self) -> list[dict[str, str]]:
        """The history the last turn was given."""
        return self.assistant.given[-1]


def _said(*turns: tuple[str, str]) -> list[dict[str, str]]:
    return [{"role": role, "content": content} for role, content in turns]


async def test_each_turn_is_given_the_turns_before_it(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)

    await chat.turn("first question")
    assert chat.was_given() == []

    await chat.turn("second question")
    assert chat.was_given() == _said(("user", "first question"), ("assistant", "a reply"))

    await chat.turn("third question")
    assert chat.was_given() == _said(
        ("user", "first question"),
        ("assistant", "a reply"),
        ("user", "second question"),
        ("assistant", "a reply"),
    )


async def test_a_snapshot_left_in_the_cache_is_not_what_a_turn_reads(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)
    await chat.turn("first question")
    # What the code before left behind for this session, with an hour to live.
    chat.cache.values[_KEY] = {"history": _said(("user", "stale")), "user_state": {}}

    await chat.turn("second question")

    assert chat.was_given() == _said(("user", "first question"), ("assistant", "a reply"))


async def test_a_turn_neither_reads_nor_writes_the_cache(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)

    await chat.turn("first question")
    await chat.turn("second question")
    await chat.turn("third question")

    assert chat.cache.calls == []
    assert chat.cache.values == {}


async def test_a_cache_that_is_down_changes_nothing(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool, _Cache(fails=True))

    await chat.turn("first question")
    answers = await chat.turn("second question")

    assert answers == ["a reply"]
    assert chat.was_given() == _said(("user", "first question"), ("assistant", "a reply"))


async def test_a_chat_is_given_its_own_turns_and_no_other_chat_s(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)
    await chat.turn("mine, one")
    await chat.turn("theirs, one", session="synthetic-session-two")
    await chat.turn("mine, two")
    assert chat.was_given() == _said(("user", "mine, one"), ("assistant", "a reply"))

    await chat.turn("theirs, two", session="synthetic-session-two")
    assert chat.was_given() == _said(("user", "theirs, one"), ("assistant", "a reply"))


async def test_a_long_chat_is_given_its_newest_turns_in_the_order_they_were_said(
    pool: asyncpg.Pool,
) -> None:
    chat = _Chat(pool)
    limit = engine_module._MAX_HISTORY_ENTRIES
    for number in range(limit):
        await chat.stores("inbound", f"question {number}")
        await chat.stores("outbound", f"answer {number}")

    await chat.turn("the last question")

    # The newest rows, the current message among them and trimmed away.
    expected: list[tuple[str, str]] = []
    for number in range(limit):
        expected += [("user", f"question {number}"), ("assistant", f"answer {number}")]
    assert chat.was_given() == _said(*expected[-(limit - 1) :])
    assert len(chat.was_given()) == limit - 1


async def test_a_repeated_question_is_trimmed_once_not_twice(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)
    await chat.turn("the same question")

    await chat.turn("the same question")

    assert chat.was_given() == _said(("user", "the same question"), ("assistant", "a reply"))


async def test_rows_with_no_session_in_their_metadata_belong_to_no_chat(
    pool: asyncpg.Pool,
) -> None:
    chat = _Chat(pool)
    await chat.stores("inbound", "a row with empty metadata", session=None)
    await pool.execute(
        "INSERT INTO conversation_messages (channel, direction, content, metadata)"
        " VALUES ('instagram', 'inbound', 'a row whose metadata is a string', to_jsonb('text'::text)),"
        "        ('instagram', 'inbound', 'a row with no metadata', NULL)"
    )

    await chat.turn("first question")

    assert chat.was_given() == []


async def test_a_message_with_no_session_is_given_no_history_and_asks_nothing(
    pool: asyncpg.Pool,
) -> None:
    chat = _Chat(pool)
    await chat.turn("first question")
    chat.table.statements.clear()

    answers = await chat.turn("a question from nowhere", session="")

    assert answers == ["a reply"]
    assert chat.was_given() == []
    assert chat.table.statements == []


async def test_a_table_that_cannot_be_read_gives_no_history_and_the_turn_still_answers(
    pool: asyncpg.Pool,
) -> None:
    chat = _Chat(pool)
    await chat.turn("first question")
    chat.table.broken = True

    answers = await chat.turn("second question")

    assert answers == ["a reply"]
    assert chat.was_given() == []


async def test_every_turn_asks_the_table_once(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)

    await chat.turn("first question")
    await chat.turn("second question")

    assert len(chat.table.statements) == 2
    assert all("metadata->>'session_id' = $1" in sent for sent in chat.table.statements)


async def test_an_engine_with_no_table_is_given_no_history(pool: asyncpg.Pool) -> None:
    chat = _Chat(pool)
    chat.engine._db_pool = None

    answers = await chat.turn("first question")

    assert answers == ["a reply"]
    assert chat.was_given() == []
    assert chat.cache.calls == []
