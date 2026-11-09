"""Exit-matrix tests for the phase-2 conversation write boundary."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest

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
