"""LLM cost recorder — indestructible triple-write ledger.

Every LLM call in the backend (and any Pro/Air cron that calls the
``/api/admin/llm-costs/record`` endpoint) funnels through
:func:`record_llm_call`. The recorder writes the same event to three
places in parallel; each write is independent, so the failure of one
does NOT prevent the others from landing:

1. **Prometheus counters** (``zantara_llm_prompt_tokens_total``,
   ``zantara_llm_completion_tokens_total``, ``zantara_llm_cost_usd_total``)
   — real-time dashboards.
2. **Postgres table** ``llm_cost_events`` (see migration 112) —
   queryable audit trail, per-endpoint aggregation, reconciliation with
   provider invoices.
3. **JSONL append-only log** on the Fly ``/data`` volume, rotated
   daily (``/data/llm_cost_log.YYYY-MM-DD.jsonl``) — last-resort record
   that survives DB outages and OOM kills. An O_APPEND + fsync write is
   atomic at the POSIX level on modern filesystems for payloads under
   PIPE_BUF (4 KiB); our events are ~400 bytes, well under that bound.

If ALL THREE fail, the call logs a CRITICAL warning but does NOT raise:
we never want cost tracking to break an actual user-facing response.

Project policy:
- ``memory/feedback_claude_oauth_only.md`` — no ANTHROPIC_API_KEY.
- ``memory/feedback_claude_cli_linux_hang.md`` — Claude CLI path is
  broken on Fly; this module still records whatever the CLI reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import json
import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Default JSONL root. Fly api process mounts /data as a persistent volume
# (see fly.toml [mounts]). Override via env for local dev / tests.
JSONL_ROOT: Final[Path] = Path(
    os.getenv("LLM_COST_JSONL_ROOT", "/data"),
)
JSONL_FILENAME_TEMPLATE: Final[str] = "llm_cost_log.{date}.jsonl"

# Retention: we keep rotated files locally for this many days.
# Older files are still queryable from Postgres; the JSONL is only
# "last resort" when DB is down.
DEFAULT_RETENTION_DAYS: Final[int] = int(
    os.getenv("LLM_COST_JSONL_RETENTION_DAYS", "90"),
)

# How long one recording waits for a free connection of the ledger's pool. The model
# call being recorded waits for this write and its answer is already complete: when
# no connection frees up in this time the Postgres row is given up for that call,
# and its JSONL line remains.
PG_ACQUIRE_TIMEOUT_S: float = 2.0

# How long ``record()`` waits for its Postgres and JSONL writes, together, once they have
# started. A write still running then goes on, owned by the recorder, and its outcome is
# logged when it ends; the caller is told False for it.
SINK_WAIT_BUDGET_S: float = 2.0

# How many writes of one sink may be outstanding on one event loop. Past it the next event
# skips that sink (no task, no queue) until a write ends; an admitted write is never cancelled.
# The pool has five connections and a stuck opening is shared, so this many waiting writes
# means the sink is down; production records some tens to a few hundred calls a day.
MAX_OUTSTANDING_WRITES: int = 32

_SINKS: Final[tuple[str, str]] = ("postgres", "jsonl")

# The key of the event being recorded, carried by every log line about it. Tasks and threads
# started while it is set inherit it.
_EVENT_KEY: contextvars.ContextVar[str] = contextvars.ContextVar("llm_cost_event", default="-")


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCallEvent:
    """One LLM call worth recording.

    All fields are required except where a default is given. The
    recorder does NOT validate pricing — callers are expected to
    compute ``cost_usd`` using :mod:`backend.services.llm_clients.pricing`.
    """

    provider: str  # 'gemini' | 'deepseek' | 'claude_oauth' | 'openrouter' | …
    model: str  # exact slug the provider sees
    input_tokens: int
    output_tokens: int
    cost_usd: float
    success: bool
    latency_ms: int
    endpoint: str | None = None  # caller module, e.g. 'article_composer'
    request_id: str | None = None  # correlation id if available
    error_class: str | None = None  # populated when success=False
    cache_hit_tokens: int = 0
    ts_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to JSON-safe dict (datetime → ISO string)."""
        d = asdict(self)
        d["ts_utc"] = self.ts_utc.isoformat()
        return d


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class _LoopState:
    """The ledger on one event loop: its Postgres pool, the one attempt to open it, and the
    writes still running there, per sink, each held until it ends."""

    __slots__ = ("loop", "opening", "pool", "skipped", "writes")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.pool: Any = None
        self.opening: asyncio.Future[Any] | None = None
        self.writes: dict[str, set[asyncio.Task[None]]] = {sink: set() for sink in _SINKS}
        self.skipped: dict[str, int] = dict.fromkeys(_SINKS, 0)


class _Outcome:
    """What is known about one recorded event. It only moves forward: a sink that is
    still running keeps the CRITICAL line back, one that succeeded rules it out for good."""

    __slots__ = ("alarm_settled", "event", "key", "late", "prometheus", "started", "states")

    def __init__(self, event: LLMCallEvent, key: str, prometheus: bool, started: float) -> None:
        self.event = event
        self.key = key
        self.prometheus = prometheus
        self.started = started
        self.states: dict[str, str] = {}  # sink -> running | ok | failed | skipped
        self.late: set[str] = set()
        self.alarm_settled = False


def _error_of(task: asyncio.Task[None]) -> BaseException | None:
    """The error a finished write ended with, or None. Cancellation is read first: asking a
    cancelled task for its exception raises."""
    if task.cancelled():
        return asyncio.CancelledError()
    return task.exception()


def _retrieve(opening: asyncio.Future[Any]) -> None:
    """Read the outcome of an opening.

    Every write that waited may have been cancelled by then, and ``asyncio.shield``
    drops its own reading of the outcome when its waiter goes: without this the
    failure of an opening nobody waits for any more is reported as an exception
    that was never retrieved.
    """
    if not opening.cancelled():
        opening.exception()


def _log(level: int, msg: str, *args: object) -> None:
    """Log one line about a recorded event; never raises.

    A logging filter or handler that raises would otherwise leave the event half handled: a
    state not set, the CRITICAL line not evaluated, an exception for the caller. The line is
    dropped instead, and one plain line says so, with the logging error's traceback; if that
    line cannot be emitted either, nothing more is tried.
    """
    try:
        logger.log(level, msg, *args, stacklevel=2)
    except Exception as exc:
        with contextlib.suppress(Exception):
            logger.error(
                "a llm_cost log line for event=%s could not be logged: %r",
                _EVENT_KEY.get(),
                exc,
                exc_info=exc,
            )


class LLMCostRecorder:
    """Singleton that handles the triple-write. Never raises to callers."""

    def __init__(
        self,
        *,
        jsonl_root: Path = JSONL_ROOT,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.jsonl_root = jsonl_root
        self.retention_days = retention_days
        self._jsonl_lock = asyncio.Lock()
        self._jsonl_ready: bool | None = (
            None  # tri-state: None=unchecked, True=ok, False=unavailable
        )

        # One Postgres pool per event loop, opened at that loop's first write
        # and kept, with the writes still running on that loop. A pool works
        # only on the loop that opened it, and the synchronous client entry
        # runs every call on a loop of its own.
        self._loops: dict[int, _LoopState] = {}
        self._loops_guard = threading.Lock()

        # Lazy refs so import-time cycles stay impossible.
        self._pg_pool_getter = None
        self._prom_metrics = None

    # -------- setup -------------------------------------------------------

    def _prepare_jsonl_dir(self) -> bool:
        """Ensure the JSONL output directory exists and is writable.

        Runs in a thread, never on the event loop. Returns True on success. On
        failure, caches False to avoid spamming filesystem errors on every call;
        a process restart re-probes.
        """
        if self._jsonl_ready is not None:
            return self._jsonl_ready
        try:
            self.jsonl_root.mkdir(parents=True, exist_ok=True)
            probe = self.jsonl_root / ".llm_cost_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            self._jsonl_ready = True
        except OSError as exc:
            _log(
                logging.ERROR,
                "⚠️ LLM cost JSONL root %s is not writable (event=%s): %s — "
                "JSONL persistence disabled for this process",
                self.jsonl_root,
                _EVENT_KEY.get(),
                exc,
            )
            self._jsonl_ready = False
        return self._jsonl_ready

    # -------- persistence sinks ------------------------------------------

    def _current_jsonl_path(self, event: LLMCallEvent) -> Path:
        date_str = event.ts_utc.strftime("%Y-%m-%d")
        return self.jsonl_root / JSONL_FILENAME_TEMPLATE.format(date=date_str)

    async def _write_jsonl(self, event: LLMCallEvent) -> None:
        line = json.dumps(event.to_json_dict(), ensure_ascii=False) + "\n"
        path = self._current_jsonl_path(event)
        # O_APPEND + fsync is the POSIX-atomic recipe for a single line
        # under PIPE_BUF. We serialise with a lock so the per-line fsync
        # stays cheap even under high concurrency.
        async with self._jsonl_lock:
            # The directory is prepared once, in a thread: a slow filesystem holds
            # this write, never the event loop.
            if self._jsonl_ready is None:
                await asyncio.to_thread(self._prepare_jsonl_dir)
            if not self._jsonl_ready:
                raise OSError(
                    f"JSONL root {self.jsonl_root} is not writable",
                )
            try:
                fd = await asyncio.to_thread(
                    os.open,
                    str(path),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o640,
                )
                try:
                    await asyncio.to_thread(os.write, fd, line.encode("utf-8"))
                    await asyncio.to_thread(os.fsync, fd)
                finally:
                    await asyncio.to_thread(os.close, fd)
            except OSError as exc:
                _log(
                    logging.ERROR,
                    "⚠️ JSONL write failed for %s (event=%s): %s",
                    path,
                    _EVENT_KEY.get(),
                    exc,
                )
                raise

    def _loop_state(self) -> _LoopState:
        """The ledger state of the running event loop; loops that have closed are forgotten."""
        loop = asyncio.get_running_loop()
        with self._loops_guard:
            for key in [k for k, known in self._loops.items() if known.loop.is_closed()]:
                del self._loops[key]
            holder = self._loops.get(id(loop))
            if holder is None or holder.loop is not loop:
                holder = self._loops[id(loop)] = _LoopState(loop)
            return holder

    async def _open_pg_pool(self, holder: _LoopState) -> Any:
        try:
            # Lazy import to avoid pulling in asyncpg at module load (tests).
            from backend.app.core.database import get_db_pool

            holder.pool = await get_db_pool()
            return holder.pool
        finally:
            holder.opening = None

    async def _get_pg_pool(self) -> Any:
        """The pool of the running event loop, opened at its first use and kept.

        Writes that arrive while the pool is being opened wait on that one
        attempt and share its outcome: none of them opens a second pool, and
        none waits longer than the attempt takes. A failed attempt keeps
        nothing, so the next write tries again. A writer that is cancelled
        while it waits does not cancel the attempt.
        """
        holder = self._loop_state()
        if holder.pool is not None:
            return holder.pool
        if holder.opening is None:
            holder.opening = asyncio.ensure_future(self._open_pg_pool(holder))
            holder.opening.add_done_callback(_retrieve)
        return await asyncio.shield(holder.opening)

    async def _write_postgres(self, event: LLMCallEvent) -> None:
        pool = await self._get_pg_pool()
        if pool is None:
            raise RuntimeError("db pool not initialised")
        # The pool is shared by every recording on this loop and can be held full, so
        # the wait for a connection has a limit of its own. The command timeout of the
        # pool covers the statement, not this wait.
        acquired = False
        try:
            async with pool.acquire(timeout=PG_ACQUIRE_TIMEOUT_S) as conn:
                acquired = True
                await self._insert_event(conn, event)
        except asyncio.TimeoutError as exc:
            if acquired:
                raise
            raise RuntimeError(
                f"no free connection for the cost ledger within {PG_ACQUIRE_TIMEOUT_S:g} s"
            ) from exc

    async def _insert_event(self, conn: Any, event: LLMCallEvent) -> None:
        await conn.execute(
            """
            INSERT INTO llm_cost_events (
                ts_utc, provider, model,
                input_tokens, output_tokens, cache_hit_tokens, cost_usd,
                endpoint, request_id, success, error_class, latency_ms
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            event.ts_utc,
            event.provider,
            event.model,
            event.input_tokens,
            event.output_tokens,
            event.cache_hit_tokens,
            event.cost_usd,
            event.endpoint,
            event.request_id,
            event.success,
            event.error_class,
            event.latency_ms,
        )

    def _write_prometheus(self, event: LLMCallEvent) -> None:
        # Lazy import to avoid circular dependency with app/metrics.py.
        from backend.app import metrics as m

        endpoint_label = event.endpoint or "unknown"
        m.llm_prompt_tokens.labels(
            model=event.model,
            endpoint=endpoint_label,
        ).inc(event.input_tokens)
        m.llm_completion_tokens.labels(
            model=event.model,
            endpoint=endpoint_label,
        ).inc(event.output_tokens)
        m.llm_cost_usd.labels(model=event.model).inc(event.cost_usd)
        # Feed the request-size histogram too.
        if event.input_tokens:
            m.llm_request_tokens.labels(
                model=event.model,
                type="prompt",
            ).observe(event.input_tokens)
        if event.output_tokens:
            m.llm_request_tokens.labels(
                model=event.model,
                type="completion",
            ).observe(event.output_tokens)

    # -------- public API --------------------------------------------------

    async def record(self, event: LLMCallEvent) -> dict[str, bool]:
        """Record an LLM call to all three sinks.

        Returns a dict ``{"prometheus": bool, "postgres": bool, "jsonl": bool}``. True
        means the write succeeded before this call returned. False means it failed, was
        skipped because too many writes of that sink are outstanding, or was still running
        after ``SINK_WAIT_BUDGET_S``: the logs tell the three apart, every line carrying
        ``event=<key>``, and a write still running goes on, owned by the recorder, with its
        outcome logged when it ends. The budget times one asynchronous wait over both
        writes and holds while the event loop makes progress; the synchronous work around
        it (Prometheus, admission, logging, the first call's lazy import) is outside it.

        Never raises: a caller cancelled during the wait gets the dict of that moment, and
        no write is cancelled with it. Logs a CRITICAL warning when all three sinks failed,
        once, as soon as that is known.
        """
        key = secrets.token_hex(6)
        token = _EVENT_KEY.set(key)
        try:
            return await self._record(event, key)
        finally:
            _EVENT_KEY.reset(token)

    async def _record(self, event: LLMCallEvent, key: str) -> dict[str, bool]:
        results: dict[str, bool] = {"prometheus": False, "postgres": False, "jsonl": False}

        # Prometheus is sync and extremely fast; do it inline.
        try:
            self._write_prometheus(event)
            results["prometheus"] = True
        except Exception as exc:
            _log(logging.WARNING, "prometheus write failed for llm_cost event=%s: %s", key, exc)

        loop = asyncio.get_running_loop()
        state = self._loop_state()
        outcome = _Outcome(event, key, results["prometheus"], loop.time())
        tasks: dict[asyncio.Task[None], str] = {}
        writers = {"postgres": self._write_postgres, "jsonl": self._write_jsonl}
        for sink in _SINKS:
            if not self._admit(state, sink, key):
                outcome.states[sink] = "skipped"
                continue
            coroutine = writers[sink](event)
            try:
                task = loop.create_task(coroutine)
            except Exception as exc:
                coroutine.close()
                outcome.states[sink] = "failed"
                _log(
                    logging.WARNING,
                    "%s write for llm_cost event=%s could not be started: %s",
                    sink,
                    key,
                    exc,
                )
                continue
            # Owned from creation, before the first await: whatever happens to this
            # call, the callback reads the write's outcome and drops the reference.
            outcome.states[sink] = "running"
            state.writes[sink].add(task)
            task.add_done_callback(functools.partial(self._settle, state, sink, outcome))
            tasks[task] = sink

        if tasks:
            try:
                await asyncio.wait(tasks, timeout=SINK_WAIT_BUDGET_S)
            except asyncio.CancelledError:
                # As before, a cancelled caller is answered with what is known now.
                pass

        # What counts is what has ended when the wait resumes; nothing awaits from here.
        for task, sink in tasks.items():
            if not task.done():
                outcome.late.add(sink)
                continue
            error = _error_of(task)
            if error is None:
                outcome.states[sink] = "ok"
                results[sink] = True
            else:
                outcome.states[sink] = "failed"
                _log(
                    logging.WARNING,
                    "%s write failed for llm_cost event=%s (model=%s endpoint=%s): %s",
                    sink,
                    key,
                    event.model,
                    event.endpoint,
                    error,
                )
        if outcome.late:
            _log(
                logging.WARNING,
                "llm_cost event=%s: %s write not confirmed within %gs, it goes on",
                key,
                " and ".join(sorted(outcome.late)),
                SINK_WAIT_BUDGET_S,
            )
        self._alarm_if_all_failed(outcome)
        return results

    def _admit(self, state: _LoopState, sink: str, key: str) -> bool:
        """Admit a write of ``sink``, or skip it while too many are outstanding on this loop."""
        outstanding = len(state.writes[sink])
        if outstanding >= MAX_OUTSTANDING_WRITES:
            if not state.skipped[sink]:
                _log(
                    logging.WARNING,
                    "%s write skipped for llm_cost event=%s: admission is saturated, "
                    "%d writes outstanding",
                    sink,
                    key,
                    outstanding,
                )
            state.skipped[sink] += 1
            return False
        if state.skipped[sink]:
            _log(
                logging.WARNING,
                "%s write admitted for llm_cost event=%s after %d skipped "
                "while admission was saturated",
                sink,
                key,
                state.skipped[sink],
            )
            state.skipped[sink] = 0
        return True

    def _settle(
        self, state: _LoopState, sink: str, outcome: _Outcome, task: asyncio.Task[None]
    ) -> None:
        """Done callback of a write: drop its reference, read its outcome, and log the outcome
        of a write that ended after ``record()`` returned.

        Never raises: nothing here can, its lines go through ``_log``, and the CRITICAL line is
        evaluated whether or not they could be logged.
        """
        state.writes[sink].discard(task)
        if sink not in outcome.late:
            return  # ended within the budget: record() reports it
        error = _error_of(task)
        outcome.states[sink] = "ok" if error is None else "failed"
        elapsed = state.loop.time() - outcome.started
        if error is None:
            _log(
                logging.INFO,
                "%s write for llm_cost event=%s landed late, %.2fs after it started",
                sink,
                outcome.key,
                elapsed,
            )
        else:
            _log(
                logging.WARNING,
                "%s write failed late for llm_cost event=%s (model=%s endpoint=%s), "
                "%.2fs after it started: %s",
                sink,
                outcome.key,
                outcome.event.model,
                outcome.event.endpoint,
                elapsed,
                error,
            )
        self._alarm_if_all_failed(outcome)

    @staticmethod
    def _alarm_if_all_failed(outcome: _Outcome) -> None:
        """Log the CRITICAL line once, when all three sinks are known to have failed."""
        if outcome.alarm_settled:
            return
        states = outcome.states.values()
        if outcome.prometheus or "ok" in states:
            outcome.alarm_settled = True
            return
        if "running" in states:
            return
        outcome.alarm_settled = True
        event = outcome.event
        _log(
            logging.CRITICAL,
            "🚨 ALL THREE llm_cost sinks failed for event=%s "
            "(provider=%s model=%s endpoint=%s cost=$%.6f) — "
            "COST TRACKING GAP, check Prometheus/Postgres/JSONL health",
            outcome.key,
            event.provider,
            event.model,
            event.endpoint,
            event.cost_usd,
        )


# ---------------------------------------------------------------------------
# Module-level singleton + convenience wrapper
# ---------------------------------------------------------------------------

_recorder: LLMCostRecorder | None = None


def get_llm_cost_recorder() -> LLMCostRecorder:
    """Return the process-wide recorder singleton."""
    global _recorder
    if _recorder is None:
        _recorder = LLMCostRecorder()
    return _recorder


async def record_llm_call(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    success: bool,
    latency_ms: int,
    endpoint: str | None = None,
    request_id: str | None = None,
    error_class: str | None = None,
    cache_hit_tokens: int = 0,
) -> dict[str, bool]:
    """Record one LLM call. Thin convenience wrapper over the singleton.

    Example:
        >>> t0 = time.monotonic()
        >>> try:
        ...     resp = await client.post(...)
        ...     success = True
        ...     err = None
        ... except Exception as exc:
        ...     resp = None
        ...     success = False
        ...     err = type(exc).__name__
        ...     raise
        ... finally:
        ...     await record_llm_call(
        ...         provider="deepseek", model="deepseek-chat",
        ...         input_tokens=..., output_tokens=..., cost_usd=...,
        ...         success=success, latency_ms=int((time.monotonic()-t0)*1000),
        ...         endpoint="article_composer", error_class=err,
        ...     )
    """
    event = LLMCallEvent(
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        success=success,
        latency_ms=latency_ms,
        endpoint=endpoint,
        request_id=request_id,
        error_class=error_class,
        cache_hit_tokens=cache_hit_tokens,
    )
    return await get_llm_cost_recorder().record(event)
