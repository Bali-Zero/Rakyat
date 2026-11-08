"""Direct-router cache effects must follow acknowledged conversation commits."""

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

PATTERN = "zantara:conversations:*"
SESSION = "synthetic-cache-ack-session"
USER = {"email": "synthetic-cache-ack@example.invalid"}


class _Transaction:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        self.state.transaction_entries += 1
        return None

    async def __aexit__(self, exc_type, exc, traceback):
        if exc is not None:
            self.state.rollbacks += 1
            return False
        self.state.commit_entered.set()
        await self.state.allow_commit.wait()
        if self.state.outcome == "unknown":
            raise RuntimeError("synthetic lost commit acknowledgement")
        if self.state.outcome == "cancelled":
            raise self.state.cancellation
        self.state.commits += 1
        return False


class _Connection:
    def __init__(self, state):
        self.state = state

    def transaction(self):
        return _Transaction(self.state)

    async def fetchrow(self, query, *args):
        if self.state.outcome == "body":
            raise RuntimeError("synthetic pre-commit failure")
        normalized = " ".join(query.split()).upper()
        if normalized.startswith(("INSERT ", "UPDATE ")):
            self.state.mutations += 1
            return {"id": 91}
        self.state.row_reads += 1
        if self.state.endpoint == "save":
            return None
        if self.state.missing == "initial" and self.state.row_reads == 1:
            return None
        if self.state.missing == "locked" and self.state.row_reads == 2:
            return None
        return {"id": 91, "session_id": SESSION}

    async def fetch(self, query, *args):
        if self.state.outcome == "body":
            raise RuntimeError("synthetic pre-commit failure")
        self.state.row_reads += 1
        return [{"id": 91, "session_id": SESSION}]

    async def fetchval(self, query, *args):
        return 13

    async def execute(self, query, *args):
        normalized = " ".join(query.split()).upper()
        if normalized.startswith("DELETE "):
            self.state.mutations += 1
            return self.state.delete_result
        return "SELECT 1"


class _Acquire:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        self.state.acquires += 1
        return self.state.conn

    async def __aexit__(self, exc_type, exc, traceback):
        self.state.releases += 1
        return False


class _State:
    def __init__(self, endpoint, *, outcome="committed", delete_result="DELETE 1", missing=None):
        self.endpoint = endpoint
        self.outcome = outcome
        self.delete_result = delete_result
        self.missing = missing
        self.commit_entered = asyncio.Event()
        self.allow_commit = asyncio.Event()
        self.cancellation = asyncio.CancelledError("synthetic pre-ack cancellation")
        self.acquires = self.releases = self.transaction_entries = 0
        self.mutations = self.commits = self.rollbacks = self.row_reads = 0
        self.conn = _Connection(self)
        self.calls = []
        self.memory = MagicMock()
        self.metrics = MagicMock()
        self.titles = []

    def acquire(self):
        return _Acquire(self)


def _install(monkeypatch, endpoint, **kwargs):
    from backend.app.routers import conversations

    state = _State(endpoint, **kwargs)

    async def invalidate(pattern):
        state.calls.append((pattern, state.commits))
        return 0

    async def no_delay(_delay):
        return None

    def spawn(coro, *, name):
        state.titles.append(name)
        coro.close()

    # raising=False makes the original source fail behaviorally, not on patch setup.
    monkeypatch.setattr(conversations, "invalidate_cache", invalidate, raising=False)
    monkeypatch.setattr(conversations, "get_memory_cache", lambda: state.memory)
    monkeypatch.setattr(conversations, "metrics_collector", state.metrics)
    monkeypatch.setattr(conversations, "spawn", spawn)
    monkeypatch.setattr(conversations.settings, "conversation_title_generation_enabled", True)
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    return conversations, state


async def _invoke(router, state, *, no_pool=False):
    pool = None if no_pool else state
    if state.endpoint == "save":
        return await router.save_conversation(
            router.SaveConversationRequest(
                session_id=SESSION,
                messages=[{"role": "user", "content": "synthetic cache acknowledgement"}],
            ),
            current_user=USER,
            db_pool=pool,
        )
    if state.endpoint == "clear":
        return await router.clear_conversation_history(
            session_id=SESSION, current_user=USER, db_pool=pool
        )
    return await router.delete_conversation(91, current_user=USER, db_pool=pool)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint,delete_result",
    [("save", "DELETE 1"), ("clear", "DELETE 1"), ("delete", "DELETE 1"), ("clear", "DELETE 0")],
    ids=["save", "clear", "delete", "empty-clear"],
)
async def test_router_invalidation_waits_for_ack(monkeypatch, endpoint, delete_result):
    router, state = _install(monkeypatch, endpoint, delete_result=delete_result)
    task = asyncio.create_task(_invoke(router, state))
    try:
        await asyncio.wait_for(state.commit_entered.wait(), timeout=1)
        assert state.mutations == 1
        assert state.commits == 0
        assert state.calls == []
        assert not task.done()
        state.allow_commit.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert result["success"] is True
        assert state.calls == [(PATTERN, 1)]
        assert state.acquires == state.releases == state.commits == 1
    finally:
        state.allow_commit.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["save", "clear", "delete"])
async def test_cache_exception_does_not_change_ack_or_other_effects(monkeypatch, caplog, endpoint):
    router, state = _install(monkeypatch, endpoint)
    marker = "PRIVATE_SYNTHETIC_CACHE_EXCEPTION_VALUE"

    async def fail(pattern):
        state.calls.append((pattern, state.commits))
        raise RuntimeError(marker)

    monkeypatch.setattr(router, "invalidate_cache", fail)
    state.allow_commit.set()
    with caplog.at_level(logging.WARNING, logger=router.logger.name):
        result = await _invoke(router, state)
    assert result["success"] is True
    assert state.calls == [(PATTERN, 1)]
    assert state.mutations == state.acquires == state.commits == 1
    state.metrics.record_cache_db_consistency_error.assert_not_called()
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Conversation cache invalidation failed after commit:")
    ]
    assert len(records) == 1
    assert (
        records[0].getMessage()
        == "Conversation cache invalidation failed after commit: RuntimeError"
    )
    assert records[0].exc_info is None or records[0].exc_info is False
    assert marker not in caplog.text
    assert SESSION not in records[0].getMessage()
    assert USER["email"] not in records[0].getMessage()
    if endpoint == "save":
        assert result["persistence_mode"] == "db"
        state.memory.mark_synced.assert_called_once()
        assert state.titles == ["conversation_title"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["save", "clear", "delete"])
async def test_cache_cancellation_after_ack_propagates_without_another_write(monkeypatch, endpoint):
    router, state = _install(monkeypatch, endpoint)
    cancellation = asyncio.CancelledError("synthetic after-ack cancellation")

    async def cancel(pattern):
        state.calls.append((pattern, state.commits))
        raise cancellation

    monkeypatch.setattr(router, "invalidate_cache", cancel)
    state.allow_commit.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await _invoke(router, state)
    assert raised.value is cancellation
    assert state.calls == [(PATTERN, 1)]
    assert state.mutations == state.acquires == state.commits == 1
    state.metrics.record_cache_db_consistency_error.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["save", "clear", "delete"])
@pytest.mark.parametrize("outcome", ["body", "unknown", "cancelled"])
async def test_unacknowledged_paths_do_not_invalidate(monkeypatch, endpoint, outcome):
    router, state = _install(monkeypatch, endpoint, outcome=outcome)
    state.allow_commit.set()
    if endpoint == "save" and outcome == "body":
        result = await _invoke(router, state)
        assert result["persistence_mode"] == "memory_fallback"
        assert result["conversation_id"] == 0
        assert state.acquires == state.rollbacks == 3
    else:
        expected = asyncio.CancelledError if outcome == "cancelled" else HTTPException
        with pytest.raises(expected) as raised:
            await _invoke(router, state)
        if outcome == "cancelled":
            assert raised.value is state.cancellation
        if endpoint == "save" and outcome == "unknown":
            assert raised.value.status_code == 503
            assert raised.value.detail == {
                "code": "conversation_commit_unknown",
                "retryable": False,
                "message": "Save outcome could not be confirmed. Check history before retrying.",
            }
            assert not raised.value.headers
        assert state.acquires == 1
    assert state.calls == []
    assert state.commits == 0
    state.memory.mark_synced.assert_not_called()
    assert state.titles == []


@pytest.mark.asyncio
async def test_save_without_pool_does_not_invalidate(monkeypatch):
    router, state = _install(monkeypatch, "save")
    result = await _invoke(router, state, no_pool=True)
    assert result["persistence_mode"] == "memory_fallback"
    assert result["conversation_id"] == 0
    assert state.calls == []
    assert state.acquires == state.mutations == state.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["initial", "locked", "delete-zero"])
async def test_all_delete_404_paths_do_not_invalidate(monkeypatch, missing):
    router, state = _install(
        monkeypatch,
        "delete",
        missing=missing,
        delete_result="DELETE 0" if missing == "delete-zero" else "DELETE 1",
    )
    state.allow_commit.set()
    with pytest.raises(HTTPException) as raised:
        await _invoke(router, state)
    assert raised.value.status_code == 404
    assert state.calls == []
    assert state.acquires == 1
    assert state.commits == (1 if missing == "delete-zero" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["memory", "title"])
async def test_other_post_commit_failure_cannot_suppress_invalidation(monkeypatch, effect):
    router, state = _install(monkeypatch, "save")
    if effect == "memory":
        state.memory.add_message.side_effect = RuntimeError("synthetic memory failure")
    else:

        def fail_spawn(coro, *, name):
            state.titles.append(name)
            coro.close()
            raise RuntimeError("synthetic title failure")

        monkeypatch.setattr(router, "spawn", fail_spawn)
    state.allow_commit.set()
    result = await _invoke(router, state)
    assert result["persistence_mode"] == "db"
    assert state.calls == [(PATTERN, 1)]
    assert state.mutations == state.acquires == state.commits == 1
