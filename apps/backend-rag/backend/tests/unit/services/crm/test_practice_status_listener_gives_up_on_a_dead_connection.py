"""be-crm-76: the practice status listener must survive a dead connection.

Regression tests for three defects that let the listener die silently:
  - ``asyncpg.connect`` waited asyncpg's default 60 s, now tightened to 10 s;
  - the keep-alive ping had no timeout, so a half-open TCP path could hang it forever;
  - the reconnect path awaited an untimed UNLISTEN/close, which waits forever on asyncpg's
    cancel_waiter after a timed-out ping, so the listener never reconnected;
  - notification-handler tasks were dispatched with no retained reference and could
    be garbage-collected mid-email.

Fakes only — no real database. ``asyncpg.connect`` and ``asyncio.sleep`` are
monkeypatched inside the module under test so every test runs in well under a
second, and every await that could hang on a regression is wrapped in
``asyncio.wait_for(..., 2)`` so failures surface as assertions, never as hangs.
"""

import asyncio
import math

import pytest

import backend.services.crm.practice_status_listener as psl
from backend.services.crm.practice_status_listener import PracticeStatusListener

_HANG_GUARD_S = 2


class _FakeConnection:
    """Minimal asyncpg.Connection stand-in (synthetic, no DB)."""

    def __init__(
        self,
        ping_error: Exception | None = None,
        hang_on_close: bool = False,
        listen_error: Exception | None = None,
    ) -> None:
        self.ping_error = ping_error
        self.hang_on_close = hang_on_close
        self.listen_error = listen_error
        self.terminated = False
        self.graceful_calls: list[str] = []
        self.ping_timeouts: list[float | None] = []
        self.closed = False
        self.channels: list[str] = []

    async def add_listener(self, channel, callback) -> None:
        if self.listen_error is not None:
            raise self.listen_error
        self.channels.append(channel)

    async def remove_listener(self, channel, callback) -> None:
        self.graceful_calls.append("remove_listener")
        if self.hang_on_close:
            await asyncio.Event().wait()  # never set: asyncpg's cancel_waiter on a dead socket

    async def execute(self, query, *args, timeout=None):
        self.ping_timeouts.append(timeout)
        if self.ping_error is not None:
            raise self.ping_error
        return "SELECT 1"

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.graceful_calls.append("close")
        if self.hang_on_close:
            await asyncio.Event().wait()
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True
        self.closed = True


@pytest.fixture
def listener(monkeypatch):
    monkeypatch.setattr(psl, "ProcessAutomationService", lambda pool: object())
    return PracticeStatusListener(db_dsn="postgres://synthetic:5432/test", db_pool=None)


@pytest.fixture
def fast_clock(monkeypatch):
    """Collapse asyncio.sleep and the reconnect delay so tests run in milliseconds."""
    real_sleep = asyncio.sleep

    async def _fast_sleep(delay, result=None):
        await real_sleep(min(float(delay), 0.01))
        return result

    monkeypatch.setattr(psl.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(psl, "_RECONNECT_DELAY_S", 0.01)


async def _wait_until(predicate, timeout=_HANG_GUARD_S):
    """Spin until predicate() is true; raise TimeoutError (never hang) otherwise."""

    async def _spin():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_spin(), timeout)


# ── Guilt: connect timeout ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_is_given_a_finite_timeout(listener, fast_clock, monkeypatch):
    recorded: dict = {}

    async def fake_connect(dsn, **kwargs):
        recorded.update(kwargs)
        return _FakeConnection()

    monkeypatch.setattr(psl.asyncpg, "connect", fake_connect)

    listener._running = True
    task = asyncio.create_task(listener._connect_and_listen())
    try:
        await _wait_until(lambda: listener._conn is not None)
    finally:
        listener._running = False
        await asyncio.wait_for(task, _HANG_GUARD_S)

    timeout = recorded.get("timeout")
    assert timeout is not None, "asyncpg.connect called without a timeout kwarg"
    assert math.isfinite(timeout), f"connect timeout must be finite, got {timeout!r}"
    assert timeout == psl._CONNECT_TIMEOUT_S


@pytest.mark.asyncio
async def test_connect_timeout_is_retried(listener, fast_clock, monkeypatch):
    calls = 0

    async def flaky_connect(dsn, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.TimeoutError("synthetic half-open connect")
        return _FakeConnection()

    monkeypatch.setattr(psl.asyncpg, "connect", flaky_connect)

    listener._running = True
    task = asyncio.create_task(listener._run_loop())
    try:
        await _wait_until(lambda: calls >= 2)
    finally:
        listener._running = False
        await asyncio.wait_for(task, _HANG_GUARD_S)

    assert calls >= 2, "a connect that raises TimeoutError was never retried"


# ── Guilt: ping timeout ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_keepalive_ping_is_given_a_finite_timeout(listener, fast_clock, monkeypatch):
    conn = _FakeConnection()

    async def fake_connect(dsn, **kwargs):
        return conn

    monkeypatch.setattr(psl.asyncpg, "connect", fake_connect)

    listener._running = True
    task = asyncio.create_task(listener._connect_and_listen())
    try:
        await _wait_until(lambda: len(conn.ping_timeouts) > 0)
    finally:
        listener._running = False
        await asyncio.wait_for(task, _HANG_GUARD_S)

    assert conn.ping_timeouts, "keep-alive ping never ran"
    for timeout in conn.ping_timeouts:
        assert timeout is not None, "ping executed without a timeout kwarg"
        assert math.isfinite(timeout), f"ping timeout must be finite, got {timeout!r}"
    assert conn.ping_timeouts[0] == psl._PING_TIMEOUT_S


@pytest.mark.asyncio
async def test_ping_timeout_closes_connection_and_reconnects(listener, fast_clock, monkeypatch):
    dead = _FakeConnection(ping_error=asyncio.TimeoutError("synthetic dead connection"))
    healthy = _FakeConnection()
    conns = [dead, healthy]
    calls = 0

    async def fake_connect(dsn, **kwargs):
        nonlocal calls
        conn = conns[min(calls, len(conns) - 1)]
        calls += 1
        return conn

    monkeypatch.setattr(psl.asyncpg, "connect", fake_connect)

    listener._running = True
    task = asyncio.create_task(listener._run_loop())
    try:
        await _wait_until(lambda: calls >= 2)
    finally:
        listener._running = False
        await asyncio.wait_for(task, _HANG_GUARD_S)

    assert calls >= 2, "a ping that raises TimeoutError did not trigger a reconnect"
    assert dead.closed, "the dead connection was not closed before reconnecting"


async def _reconnects_despite_hanging_close(listener, monkeypatch, first: _FakeConnection):
    """Run the retry loop; return (connects, first) — never hangs, never raises on a regression."""
    healthy = _FakeConnection()
    connects = 0

    async def fake_connect(dsn, **kwargs):
        nonlocal connects
        connects += 1
        return first if connects == 1 else healthy

    monkeypatch.setattr(psl.asyncpg, "connect", fake_connect)

    listener._running = True
    task = asyncio.create_task(listener._run_loop())
    try:
        try:
            await _wait_until(lambda: connects >= 2)
        except asyncio.TimeoutError:
            pass  # the assertion below reports it
    finally:
        listener._running = False
        task.cancel()
        try:
            await asyncio.wait_for(task, _HANG_GUARD_S)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    return connects


@pytest.mark.asyncio
async def test_ping_timeout_reconnects_even_when_close_hangs(listener, fast_clock, monkeypatch):
    dead = _FakeConnection(ping_error=asyncio.TimeoutError("synthetic"), hang_on_close=True)
    connects = await _reconnects_despite_hanging_close(listener, monkeypatch, dead)
    assert connects >= 2, "listener stuck awaiting an untimed UNLISTEN/close on a dead socket"
    assert dead.terminated, "the dead connection was not terminated"


@pytest.mark.asyncio
async def test_failed_listen_reconnects_even_when_close_hangs(listener, fast_clock, monkeypatch):
    dead = _FakeConnection(listen_error=OSError("synthetic"), hang_on_close=True)
    connects = await _reconnects_despite_hanging_close(listener, monkeypatch, dead)
    assert connects >= 2, "listener stuck awaiting an untimed close after a failed LISTEN"
    assert dead.terminated, "the half-set-up connection was not terminated"


# ── Guilt: handler tasks are retained ────────────────────────────────────────


@pytest.mark.asyncio
async def test_handler_task_is_retained_until_it_finishes(listener):
    gate = asyncio.Event()

    async def blocking_handler(payload):
        await gate.wait()

    listener._handle_notification = blocking_handler

    listener._on_notification(None, 1, "practice_changed", "{}")

    assert len(listener._handler_tasks) == 1, "handler task was not retained"
    task = next(iter(listener._handler_tasks))
    assert not task.done()

    gate.set()
    await asyncio.wait_for(task, _HANG_GUARD_S)
    await _wait_until(lambda: len(listener._handler_tasks) == 0)
    assert listener._handler_tasks == set()


# ── Innocence: a healthy connection is left alone ────────────────────────────


@pytest.mark.asyncio
async def test_healthy_connection_never_reconnects(listener, fast_clock, monkeypatch):
    conn = _FakeConnection()
    calls = 0

    async def fake_connect(dsn, **kwargs):
        nonlocal calls
        calls += 1
        return conn

    monkeypatch.setattr(psl.asyncpg, "connect", fake_connect)

    listener._running = True
    task = asyncio.create_task(listener._run_loop())
    try:
        await _wait_until(lambda: len(conn.ping_timeouts) >= 3)
    finally:
        listener._running = False
        await asyncio.wait_for(task, _HANG_GUARD_S)

    assert calls == 1, f"healthy connection reconnected {calls - 1} extra time(s)"
    assert not conn.closed


@pytest.mark.asyncio
async def test_stop_closes_a_healthy_connection_gracefully(listener):
    conn = _FakeConnection()
    listener._conn = conn

    await asyncio.wait_for(listener.stop(), _HANG_GUARD_S)

    assert conn.graceful_calls == ["remove_listener", "close"]
    assert not conn.terminated
    assert listener._conn is None


@pytest.mark.asyncio
async def test_stop_on_a_dead_socket_returns_within_the_bound(listener, monkeypatch):
    monkeypatch.setattr(psl, "_CLOSE_TIMEOUT_S", 0.05)
    conn = _FakeConnection(hang_on_close=True)
    listener._conn = conn

    await asyncio.wait_for(listener.stop(), _HANG_GUARD_S)

    assert conn.terminated, "stop() did not fall back to terminate()"
    assert listener._conn is None
