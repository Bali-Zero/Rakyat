"""Rule E2 (brief 85bec76a) for every ``except`` clause the stage-timings PR adds.

* Writer side (``_analytics_clock``, ``_stage_timings_or_none``, the S4 guard in ``log_query``):
  the handler sets its fallback and does nothing else: ZERO logging calls.
* Consumer and endpoint (``get_stage_latency``, the ``/latency`` route): the fallback is decided
  first, then exactly ONE constant, argument-free log call; no value derived from the exception
  reaches a log record (msg, args, exc_info), a response body or a bound parameter.

The hostile exception is the one gate 9213 r2 used: a metaclass whose ``__name__`` RETURNS an
exception carrying the sentinel, with ``str``/``repr`` raising.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.query_analytics as router_module
from backend.db.repositories import query_analytics_repository as repo_module
from backend.services.rag.agentic import orchestrator_streaming_core

SENTINEL = "SENTINEL-EXC-TEXT-7f3a"
CONSTANT_MESSAGE = "Failed to get stage latency"


def _hostile() -> BaseException:
    # built at call time: pytest must not read the class name at collection
    class _Meta(type):
        @property
        def __name__(cls) -> Any:  # RETURNS an exception carrying the message
            return Exception(SENTINEL)

    class _Hostile(Exception, metaclass=_Meta):
        def __init__(self) -> None:
            super().__init__(SENTINEL)

        def __str__(self) -> str:
            raise ValueError("unreadable str")

        def __repr__(self) -> str:
            raise ValueError("unreadable repr")

    return _Hostile()


def _named() -> BaseException:
    return RuntimeError(SENTINEL)


EXCEPTIONS = [pytest.param(_hostile, id="metaclass"), pytest.param(_named, id="plain")]


class _Recorder:
    """A logger double that records every call and can raise from the Nth call on."""

    def __init__(self, raise_from: int | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._raise_from = raise_from

    def __getattr__(self, name: str) -> Any:
        def rec(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))
            if self._raise_from is not None and len(self.calls) >= self._raise_from:
                raise OSError("logger down")

        return rec


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _no_sentinel(records: list[logging.LogRecord]) -> bool:
    return not any(
        SENTINEL in repr((r.msg, r.args, r.exc_info, r.exc_text, r.getMessage())) for r in records
    )


def _pool(fetch_error: BaseException | None = None) -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    if fetch_error is not None:
        conn.fetch.side_effect = fetch_error
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


# ----------------------------------------------------------------- consumer


@pytest.mark.asyncio
@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_get_stage_latency_returns_empty_with_one_constant_log_call(
    monkeypatch: pytest.MonkeyPatch, make: Any
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(repo_module, "logger", rec)
    pool, _ = _pool(make())

    assert await repo_module.QueryAnalyticsRepository(pool).get_stage_latency(days=7) == []

    assert len(rec.calls) == 1
    name, args, kwargs = rec.calls[0]
    assert (name, args) == ("warning", (CONSTANT_MESSAGE,))
    assert not kwargs.get("exc_info") and "extra" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_get_stage_latency_survives_a_logger_that_raises(
    monkeypatch: pytest.MonkeyPatch, make: Any
) -> None:
    monkeypatch.setattr(repo_module, "logger", _Recorder(raise_from=1))
    pool, _ = _pool(make())
    assert await repo_module.QueryAnalyticsRepository(pool).get_stage_latency(days=7) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_get_stage_latency_leaks_nothing_into_a_real_log_record(
    caplog: pytest.LogCaptureFixture, make: Any
) -> None:
    capture = _Capture()
    repo_module.logger.addHandler(capture) if hasattr(repo_module.logger, "addHandler") else None
    try:
        with caplog.at_level(logging.DEBUG):
            pool, _ = _pool(make())
            assert await repo_module.QueryAnalyticsRepository(pool).get_stage_latency(days=7) == []
    finally:
        if hasattr(repo_module.logger, "removeHandler"):
            repo_module.logger.removeHandler(capture)
    assert _no_sentinel(caplog.records) and _no_sentinel(capture.records)
    assert SENTINEL not in caplog.text


# ----------------------------------------------------------------- endpoint


def _client(error: BaseException) -> TestClient:
    repo = MagicMock()
    repo.get_stage_latency = AsyncMock(side_effect=error)
    app = FastAPI()
    app.include_router(router_module.router)
    app.dependency_overrides[router_module._verify_founder_access] = lambda: {"role": "admin"}
    app.dependency_overrides[router_module._get_repo] = lambda: repo
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("make", EXCEPTIONS)
@pytest.mark.parametrize("raise_from", [None, 1])
def test_endpoint_answers_a_fixed_500_with_one_constant_log_call(
    monkeypatch: pytest.MonkeyPatch, make: Any, raise_from: int | None
) -> None:
    rec = _Recorder(raise_from=raise_from)
    monkeypatch.setattr(router_module, "logger", rec)

    response = _client(make()).get("/api/analytics/query-insights/latency?days=7")

    assert response.status_code == 500
    assert response.json() == {"detail": CONSTANT_MESSAGE}
    assert SENTINEL not in response.text
    assert len(rec.calls) == 1
    name, args, kwargs = rec.calls[0]
    assert (name, args) == ("warning", (CONSTANT_MESSAGE,))
    assert not kwargs.get("exc_info")


@pytest.mark.parametrize("make", EXCEPTIONS)
def test_endpoint_leaks_nothing_into_a_real_log_record(
    caplog: pytest.LogCaptureFixture, make: Any
) -> None:
    capture = _Capture()
    router_module.logger.addHandler(capture)
    try:
        with caplog.at_level(logging.DEBUG):
            response = _client(make()).get("/api/analytics/query-insights/latency?days=7")
    finally:
        router_module.logger.removeHandler(capture)
    assert response.status_code == 500 and SENTINEL not in response.text
    assert _no_sentinel(caplog.records) and _no_sentinel(capture.records)


# ------------------------------------------------------------- writer side


@pytest.mark.parametrize("make", EXCEPTIONS)
def test_stage_timings_or_none_returns_none_and_never_logs(
    monkeypatch: pytest.MonkeyPatch, make: Any
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(orchestrator_streaming_core, "logger", rec)

    def boom(**_kw: Any) -> None:
        raise make()

    manager = SimpleNamespace(extract_timings_from_state=boom)
    owner = SimpleNamespace(core=SimpleNamespace(metrics_manager=manager))

    assert (
        orchestrator_streaming_core._stage_timings_or_none(owner, object(), 1.0, 2.0, 0.0, 0.0)
        is None
    )
    assert rec.calls == []


@pytest.mark.parametrize("make", EXCEPTIONS)
def test_analytics_clock_returns_none_and_never_logs(
    monkeypatch: pytest.MonkeyPatch, make: Any
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(orchestrator_streaming_core, "logger", rec)

    def boom() -> float:
        raise make()

    monkeypatch.setattr(orchestrator_streaming_core, "_ANALYTICS_CLOCK", boom)

    assert orchestrator_streaming_core._analytics_clock() is None
    assert rec.calls == []


class _ItemsRaise(dict):
    def __init__(self, error: BaseException) -> None:
        super().__init__(total=1.0)
        self._error = error

    def items(self) -> Any:
        raise self._error


@pytest.mark.asyncio
@pytest.mark.parametrize("make", EXCEPTIONS)
async def test_log_query_guard_keeps_the_row_binds_no_exception_text_and_never_logs(
    monkeypatch: pytest.MonkeyPatch, make: Any
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(repo_module, "logger", rec)
    pool, conn = _pool()
    conn.fetchrow.return_value = {"id": "00000000-0000-0000-0000-000000000001"}

    query_id = await repo_module.QueryAnalyticsRepository(pool).log_query(
        query_text="synthetic", stage_timings=_ItemsRaise(make())
    )

    assert query_id == "00000000-0000-0000-0000-000000000001"
    bound = conn.fetchrow.call_args[0][1:]
    assert SENTINEL not in repr(bound)
    assert not any(isinstance(a, dict) and "timings" in a for a in bound)
    # BASE's own debug line is the only call the logger may see; the guard adds none.
    assert [c[0] for c in rec.calls if c[0] != "debug"] == []
