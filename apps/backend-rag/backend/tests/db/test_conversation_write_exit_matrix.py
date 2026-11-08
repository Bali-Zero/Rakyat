"""Exit-matrix tests for the phase-2 conversation write boundary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from backend.app.routers import conversations as conversations_router
from backend.core.retention_policy import ALLOW_CLOCK_DELETE_ENV, RETENTION_MIN_DAYS
from backend.db.repositories import conversation_repository as repository_module
from backend.db.repositories.conversation_repository import (
    ConversationCommitOutcomeUnknown,
    ConversationRepository,
    conversation_write_connection,
)
from backend.services.common import cache

PrimaryState = Literal["body", "unknown", "committed", "commit_cancel"]
ReleaseState = Literal["ok", "exception", "cancelled"]


class _BodyFailure(RuntimeError):
    pass


class _BeginFailure(RuntimeError):
    pass


class _RollbackFailure(RuntimeError):
    pass


class _ReleaseFailure(RuntimeError):
    pass


class _CommitFailure(RuntimeError):
    pass


class _MatrixTransaction:
    def __init__(self, pool: _MatrixPool) -> None:
        self.pool = pool

    async def __aenter__(self) -> None:
        if self.pool.begin_error is not None:
            raise self.pool.begin_error

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.pool.transaction_exit_errors.append(exc)
            if self.pool.rollback_error is not None:
                raise self.pool.rollback_error
            return False
        if self.pool.primary == "unknown":
            raise self.pool.commit_error
        if self.pool.primary == "commit_cancel":
            raise asyncio.CancelledError
        return False


class _MatrixConnection:
    def __init__(self, pool: _MatrixPool) -> None:
        self.pool = pool
        self.fetchrow_count = 0

    def transaction(self) -> _MatrixTransaction:
        return _MatrixTransaction(self.pool)

    async def fetchval(self, *_args: Any) -> int:
        if self.pool.primary == "body":
            raise self.pool.body_error
        return 19

    async def fetchrow(self, *_args: Any) -> dict[str, int] | None:
        self.fetchrow_count += 1
        return None if self.fetchrow_count % 2 else {"id": 91}

    async def fetch(self, *_args: Any) -> list[Any]:
        return []

    async def execute(self, sql: str, *_args: Any) -> str:
        if self.pool.primary == "body":
            raise self.pool.body_error
        normalized = " ".join(sql.split()).upper()
        if normalized.startswith("DELETE FROM CONVERSATIONS"):
            return "DELETE 4"
        if normalized.startswith("UPDATE CONVERSATIONS"):
            return "UPDATE 5"
        return "OK"


class _MatrixAcquire:
    def __init__(self, pool: _MatrixPool) -> None:
        self.pool = pool

    async def __aenter__(self) -> _MatrixConnection:
        if self.pool.acquire_error is not None:
            raise self.pool.acquire_error
        return self.pool.connection

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.pool.release_primary_errors.append(exc)
        if self.pool.release_base_error is not None:
            raise self.pool.release_base_error
        if self.pool.release == "exception":
            raise self.pool.release_error
        if self.pool.release == "cancelled":
            raise asyncio.CancelledError
        return False


class _MatrixPool:
    def __init__(
        self,
        primary: PrimaryState,
        release: ReleaseState,
        *,
        acquire_error: BaseException | None = None,
        begin_error: BaseException | None = None,
        rollback_error: BaseException | None = None,
        release_base_error: BaseException | None = None,
    ) -> None:
        self.primary = primary
        self.release = release
        self.acquire_error = acquire_error
        self.begin_error = begin_error
        self.rollback_error = rollback_error
        self.release_base_error = release_base_error
        self.body_error = _BodyFailure("BODY_DETAIL")
        self.commit_error = _CommitFailure("COMMIT_DETAIL")
        self.release_error = _ReleaseFailure("RELEASE_DETAIL")
        self.connection = _MatrixConnection(self)
        self.acquire_count = 0
        self.release_primary_errors: list[BaseException | None] = []
        self.transaction_exit_errors: list[BaseException] = []

    def acquire(self) -> _MatrixAcquire:
        self.acquire_count += 1
        return _MatrixAcquire(self)


class _MemoryProbe:
    def __init__(self) -> None:
        self.added: list[tuple[Any, ...]] = []
        self.synced: list[tuple[str, int]] = []

    def add_message(self, *args: Any) -> None:
        self.added.append(args)

    def mark_synced(self, key: str, conversation_id: int) -> None:
        self.synced.append((key, conversation_id))


@dataclass
class _RouteOutcome:
    response: dict[str, Any] | None
    error: BaseException | None
    pool: _MatrixPool
    invalidations: list[str]
    router_invalidations: list[str]
    memory: _MemoryProbe
    titles: list[str]
    success_logs: list[str]
    sleeps: list[int]
    repository_records: list[logging.LogRecord]


async def _run_route_cell(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    primary: PrimaryState,
    release: ReleaseState,
) -> _RouteOutcome:
    session_marker = "MATRIX_SESSION_MARKER"
    email_marker = "matrix-email-marker@example.invalid"
    content_marker = "MATRIX_CONTENT_MARKER"
    pool = _MatrixPool(primary, release)
    memory = _MemoryProbe()
    invalidations: list[str] = []
    router_invalidations: list[str] = []
    titles: list[str] = []
    success_logs: list[str] = []
    sleeps: list[int] = []

    async def invalidate(pattern: str) -> int:
        invalidations.append(pattern)
        return 0

    async def record_sleep(delay: int) -> None:
        sleeps.append(delay)

    def record_spawn(coro: Any, *, name: str) -> None:
        titles.append(name)
        coro.close()

    def record_success(_logger: Any, message: str, **_context: Any) -> None:
        success_logs.append(message)

    monkeypatch.setattr(conversations_router, "get_memory_cache", lambda: memory)
    monkeypatch.setattr(conversations_router, "spawn", record_spawn)
    monkeypatch.setattr(conversations_router, "log_success", record_success)
    monkeypatch.setattr(conversations_router, "metrics_collector", MagicMock())

    async def invalidate_router(pattern: str) -> int:
        router_invalidations.append(pattern)
        return 0

    monkeypatch.setattr(cache, "_invalidate_cache", invalidate)
    monkeypatch.setattr(conversations_router, "invalidate_cache", invalidate_router)
    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    response: dict[str, Any] | None = None
    error: BaseException | None = None
    with caplog.at_level(logging.DEBUG, logger=repository_module.logger.name):
        try:
            response = await conversations_router.save_conversation(
                conversations_router.SaveConversationRequest(
                    session_id=session_marker,
                    messages=[{"role": "user", "content": content_marker}],
                ),
                current_user={"email": email_marker},
                db_pool=pool,  # type: ignore[arg-type]
            )
        except BaseException as exc:
            error = exc

    records = [record for record in caplog.records if record.name == repository_module.logger.name]
    for record in records:
        observed = " ".join(
            (
                record.getMessage(),
                repr(record.args),
                repr(getattr(record, "context", None)),
            )
        )
        assert session_marker not in observed
        assert email_marker not in observed
        assert content_marker not in observed

    return _RouteOutcome(
        response,
        error,
        pool,
        invalidations,
        router_invalidations,
        memory,
        titles,
        success_logs,
        sleeps,
        records,
    )


async def _assert_matrix_cell(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    primary: PrimaryState,
    release: ReleaseState,
) -> None:
    outcome = await _run_route_cell(monkeypatch, caplog, primary, release)
    cancelled = release == "cancelled"
    confirmed = primary == "committed" and not cancelled

    if cancelled:
        assert isinstance(outcome.error, asyncio.CancelledError)
        assert outcome.response is None
    elif primary == "unknown":
        assert isinstance(outcome.error, HTTPException)
        assert outcome.error.status_code == 503
        assert outcome.error.detail == {
            "code": "conversation_commit_unknown",
            "retryable": False,
            "message": "Save outcome could not be confirmed. Check history before retrying.",
        }
        assert not outcome.error.headers
        assert outcome.response is None
    else:
        assert outcome.error is None
        assert outcome.response is not None
        expected_mode = "db" if confirmed else "memory_fallback"
        assert outcome.response["persistence_mode"] == expected_mode
        assert outcome.response["conversation_id"] == (91 if confirmed else 0)

    expected_acquires = 3 if primary == "body" and not cancelled else 1
    assert outcome.pool.acquire_count == expected_acquires
    assert outcome.sleeps == ([1, 2] if expected_acquires == 3 else [])
    assert outcome.invalidations == []  # The direct route does not use the repository hook.
    assert outcome.router_invalidations == (["zantara:conversations:*"] if confirmed else [])
    publishes_memory = not cancelled and primary != "unknown"
    assert len(outcome.memory.added) == (1 if publishes_memory else 0)
    assert len(outcome.memory.synced) == (1 if confirmed else 0)
    assert len(outcome.titles) == (1 if confirmed else 0)
    assert len(outcome.success_logs) == (1 if confirmed else 0)

    if primary == "body" and not cancelled:
        assert all(
            error is outcome.pool.body_error for error in outcome.pool.release_primary_errors
        )
    if primary == "unknown":
        assert isinstance(
            outcome.pool.release_primary_errors[0],
            ConversationCommitOutcomeUnknown,
        )


@pytest.mark.asyncio
async def test_matrix_body_failure_release_ok(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "body", "ok")


@pytest.mark.asyncio
async def test_matrix_body_failure_release_exception(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "body", "exception")


@pytest.mark.asyncio
async def test_matrix_body_failure_release_cancelled(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "body", "cancelled")


@pytest.mark.asyncio
async def test_matrix_commit_unknown_release_ok(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "unknown", "ok")


@pytest.mark.asyncio
async def test_matrix_commit_unknown_release_exception(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "unknown", "exception")


@pytest.mark.asyncio
async def test_matrix_commit_unknown_release_cancelled(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "unknown", "cancelled")


@pytest.mark.asyncio
async def test_matrix_confirmed_commit_release_ok(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "committed", "ok")


@pytest.mark.asyncio
async def test_matrix_confirmed_commit_release_exception(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "committed", "exception")


@pytest.mark.asyncio
async def test_matrix_confirmed_commit_release_cancelled(monkeypatch, caplog) -> None:
    await _assert_matrix_cell(monkeypatch, caplog, "committed", "cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["ok", "exception", "cancelled"])
async def test_commit_phase_cancellation_never_retries_or_publishes(
    release: ReleaseState,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcome = await _run_route_cell(monkeypatch, caplog, "commit_cancel", release)
    assert isinstance(outcome.error, asyncio.CancelledError)
    assert outcome.pool.acquire_count == 1
    assert outcome.sleeps == []
    assert outcome.invalidations == []
    assert outcome.router_invalidations == []
    assert outcome.memory.synced == []
    assert outcome.titles == []
    assert outcome.success_logs == []


@pytest.mark.asyncio
async def test_begin_failure_survives_release_failure_by_identity() -> None:
    begin_error = _BeginFailure("BEGIN_DETAIL")
    pool = _MatrixPool("committed", "exception", begin_error=begin_error)
    with pytest.raises(_BeginFailure) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            pytest.fail("body ran after begin failure")
    assert caught.value is begin_error
    assert pool.acquire_count == 1


@pytest.mark.asyncio
async def test_body_failure_survives_rollback_and_release_failures_by_identity() -> None:
    body_error = _BodyFailure("BODY_DETAIL")
    pool = _MatrixPool(
        "committed",
        "exception",
        rollback_error=_RollbackFailure("ROLLBACK_DETAIL"),
    )
    with pytest.raises(_BodyFailure) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            raise body_error
    assert caught.value is body_error
    assert pool.release_primary_errors == [body_error]


@pytest.mark.asyncio
async def test_commit_unknown_survives_release_failure_by_identity() -> None:
    pool = _MatrixPool("unknown", "exception")
    with pytest.raises(ConversationCommitOutcomeUnknown) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            pass
    assert caught.value is pool.release_primary_errors[0]
    assert caught.value.__cause__ is pool.commit_error
    assert caught.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_body_cancellation_survives_cleanup_failures() -> None:
    cancellation = asyncio.CancelledError()
    pool = _MatrixPool(
        "committed",
        "exception",
        rollback_error=_RollbackFailure("ROLLBACK_DETAIL"),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            raise cancellation
    assert caught.value is cancellation
    assert pool.acquire_count == 1


@pytest.mark.asyncio
async def test_acquisition_failure_and_cancellation_propagate_by_identity() -> None:
    for error in (_BeginFailure("ACQUIRE_DETAIL"), asyncio.CancelledError()):
        pool = _MatrixPool("committed", "ok", acquire_error=error)
        with pytest.raises(type(error)) as caught:
            async with conversation_write_connection(pool):  # type: ignore[arg-type]
                pytest.fail("body ran after acquisition failure")
        assert caught.value is error
        assert pool.acquire_count == 1
        assert pool.release_primary_errors == []


@pytest.mark.asyncio
async def test_non_exception_base_failure_from_release_propagates() -> None:
    system_exit = SystemExit(7)
    pool = _MatrixPool("committed", "ok", release_base_error=system_exit)
    with pytest.raises(SystemExit) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            pass
    assert caught.value is system_exit


@pytest.mark.asyncio
async def test_non_exception_body_failure_survives_release_failure() -> None:
    class _StopSignal(BaseException):
        pass

    stop_signal = _StopSignal()
    pool = _MatrixPool("committed", "exception")
    with pytest.raises(_StopSignal) as caught:
        async with conversation_write_connection(pool):  # type: ignore[arg-type]
            raise stop_signal
    assert caught.value is stop_signal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name,expected_count",
    [("cleanup_old_conversations", 4), ("anonymize_user_data", 5)],
)
@pytest.mark.parametrize(
    "primary,release",
    [("committed", "exception"), ("unknown", "ok"), ("commit_cancel", "ok")],
)
async def test_bulk_success_is_published_only_after_confirmed_helper_return(
    method_name: str,
    expected_count: int,
    primary: PrimaryState,
    release: ReleaseState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _MatrixPool(primary, release)
    successes: list[dict[str, Any]] = []
    invalidations: list[str] = []

    async def invalidate(pattern: str) -> int:
        invalidations.append(pattern)
        return 0

    def record_success(_logger: Any, _message: str, **context: Any) -> None:
        successes.append(context)

    monkeypatch.setenv(ALLOW_CLOCK_DELETE_ENV, "1")
    monkeypatch.setattr(cache, "_invalidate_cache", invalidate)
    monkeypatch.setattr(repository_module, "log_success", record_success)
    method = getattr(ConversationRepository(pool), method_name)

    if primary == "committed":
        assert await method(days=RETENTION_MIN_DAYS) == expected_count
        assert len(successes) == 1
        assert invalidations == ["zantara:conversations:*"]
    elif primary == "unknown":
        with pytest.raises(ConversationCommitOutcomeUnknown):
            await method(days=RETENTION_MIN_DAYS)
        assert successes == []
        assert invalidations == []
    else:
        with pytest.raises(asyncio.CancelledError):
            await method(days=RETENTION_MIN_DAYS)
        assert successes == []
        assert invalidations == []
