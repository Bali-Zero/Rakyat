"""Self-differential of the streaming writer (brief 85bec76a, DIFF-1..3, T2).

Every input is run twice on HEAD: as is, and with ``_stage_timings_or_none`` forced to None
and the ``stage_timings`` keyword dropped from ``log_query`` (= what origin/main wrote). The
stream, the ``log_query`` arguments (except ``stage_timings``) and the INSERT parameters
(except ``metadata.timings``) must be equal. The cross-check against the real origin/main is
the committed differential_probe.py.
"""

import asyncio
import copy
import math
import re
import time
from collections.abc import Mapping
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from backend.db.repositories import query_analytics_repository as repo_module
from backend.services.rag.agentic import orchestrator_metrics, orchestrator_streaming_core
from backend.services.rag.agentic.orchestrator_metrics import OrchestratorMetricsManager
from backend.services.rag.agentic.orchestrator_streaming_core import OrchestratorStreamingCore
from backend.services.tools.definitions import AgentState, AgentStep, ToolCall

KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class _Manager:
    def create_done_event(self, *, execution_time, route_used, **kwargs):
        return {
            "type": "done",
            "data": {"execution_time": execution_time, "route_used": route_used, **kwargs},
        }

    def create_initial_status_event(self, correlation_id):
        return {"type": "status", "data": {"correlation_id": correlation_id}}

    def create_error_event(self, *, error_type, message, correlation_id):
        return {"type": "error", "data": {"error_type": error_type, "message": message}}

    async def process_event_stream(self, *, event_generator, **_kwargs):
        async for event in event_generator:
            yield event


class _Engine:
    tool_map: dict = {}

    async def execute_react_loop_stream(self, **_kwargs):
        yield {"type": "token", "data": "tok"}


class _Pool:
    def __init__(self, fail=False):
        self.fail = fail
        self.inserts: list = []

    def acquire(self):
        pool = self

        class Ctx:
            async def __aenter__(self):
                class Conn:
                    async def fetchrow(_c, _sql, *args):
                        pool.inserts.append(copy.deepcopy(args))
                        if pool.fail:
                            raise RuntimeError("synthetic db failure")
                        return {"id": "00000000-0000-0000-0000-000000000001"}

                return Conn()

            async def __aexit__(self, *exc):
                return False

        return Ctx()


class _Core:
    def __init__(self, state, pool, metrics, fail_at=None):
        self.reasoning_engine = _Engine()
        self.llm_gateway = SimpleNamespace(create_chat_with_history=lambda **_k: object())
        self._mm = metrics
        self._mm_reads = 0
        self._fail_at = fail_at
        self.db_pool = pool
        self._state = state

    @property
    def metrics_manager(self):
        self._mm_reads += 1
        if self._fail_at is not None and self._mm_reads >= self._fail_at:
            raise RuntimeError("synthetic late failure")
        return self._mm

    async def prepare_query_context(self, **_k):
        return ({"profile": None}, [], {}, "", None)

    async def check_gates_and_cache(self, **_k):
        return None

    async def prepare_react_execution(self, **_k):
        return ("tier-x", False, self._state, "system prompt")


class _Call:
    """A ToolCall lookalike with a programmable execution_time."""

    def __init__(self, execution_time):
        self.tool_name = "vector_search"
        self.arguments = {"collection": "col_a"}
        self.result = "r"
        self._et = execution_time

    @property
    def execution_time(self):
        if isinstance(self._et, BaseException):
            raise self._et
        return self._et


class _RadRaises:
    def __radd__(self, other):
        raise ValueError("synthetic")


class _RadNonNumber:
    def __radd__(self, other):
        return "not-a-number"


class _FloatRaises(float):
    def __float__(self):
        raise ValueError("synthetic")


class _IntRaises(int):
    def __float__(self):
        raise ValueError("synthetic")


class _UnnamableMeta(type):
    @property
    def __name__(cls):
        raise ValueError("synthetic unreadable type name")


class _Unnamable(Exception, metaclass=_UnnamableMeta):
    pass


class _ItemsUnnamable(dict):
    def items(self):
        raise _Unnamable()


class _StrKey(str):
    pass


class _ItemsRaises(dict):
    def items(self):
        raise ValueError("synthetic")


class _BoolRaises(dict):
    def __bool__(self):
        raise ValueError("synthetic")


class _LenRaises(dict):
    def __len__(self):
        raise ValueError("synthetic")


class _Counting(Mapping):
    """10**6 rejected keys; ``reads`` counts the items actually pulled."""

    def __init__(self):
        self.reads = 0

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 10**6

    def items(self):
        for i in range(10**6):
            self.reads += 1
            yield (f"K{i}", 1.0)


def _w_valid(timings) -> bool:
    if type(timings) is not dict or not timings or len(timings) > repo_module.STAGE_ITEMS_MAX:
        return False
    return all(
        type(k) is str
        and KEY_RE.fullmatch(k) is not None
        and type(v) is float
        and math.isfinite(v)
        and 0 <= v <= 86400
        and v == round(v, 4)
        for k, v in timings.items()
    )


def _drive(
    monkeypatch,
    *,
    steps,
    extract=None,
    db="ok",
    suppress=False,
    clock=None,
    metrics_time=None,
    core_fail_at=None,
):
    ticks = {"n": 0}

    def fake_time():
        ticks["n"] += 1
        return 1000.0 + 0.25 * ticks["n"]

    monkeypatch.setattr(time, "time", fake_time)
    if clock is not None:
        monkeypatch.setattr(orchestrator_streaming_core, "_ANALYTICS_CLOCK", clock)
    if metrics_time is not None:
        monkeypatch.setattr(orchestrator_metrics, "time", SimpleNamespace(time=metrics_time))
    state = AgentState(query="synthetic probe query", intent_type="business_complex")
    state.evidence_score = 0.8
    state.trusted_tools_used = True
    state.final_answer = "synthetic answer"
    state.steps = [AgentStep(step_number=1, thought="t", action=a) for a in steps]
    metrics = OrchestratorMetricsManager()
    if extract is not None:
        if extract == "raise":

            def boom(*_a, **_k):
                raise ValueError("synthetic")

            monkeypatch.setattr(metrics, "extract_timings_from_state", boom)
        else:
            monkeypatch.setattr(metrics, "extract_timings_from_state", lambda *_a, **_k: extract())
    pool = None if db == "none" else _Pool(fail=db == "dberror")
    core = _Core(state, pool, metrics, core_fail_at)
    sc = OrchestratorStreamingCore(core=core, streaming_manager=_Manager())
    monkeypatch.setattr(sc, "_mark_react_stream_unfinalized", lambda *a, **k: None)

    calls: list = []
    orig = repo_module.QueryAnalyticsRepository.log_query

    async def spy(self, *a, **k):
        calls.append(dict(k))
        if suppress:
            k.pop("stage_timings", None)
        return await orig(self, *a, **k)

    monkeypatch.setattr(repo_module.QueryAnalyticsRepository, "log_query", spy)
    if suppress:
        monkeypatch.setattr(
            orchestrator_streaming_core, "_stage_timings_or_none", lambda *a, **k: None
        )
    events: list = []

    async def go():
        async for ev in sc.stream_query_core(
            query="synthetic probe query",
            user_id="probe-user",
            conversation_history=None,
            session_id=None,
            images=None,
            tool_execution_counter={"count": 0},
            correlation_id="probe-corr",
        ):
            events.append(copy.deepcopy(ev))

    asyncio.run(go())
    for call in calls:
        call.pop("stage_timings", None)
    return events, calls, (pool.inserts if pool else [])


def _assert_differential(monkeypatch, **kw):
    with monkeypatch.context() as m:
        head = _drive(m, suppress=False, **kw)
    with monkeypatch.context() as m:
        base = _drive(m, suppress=True, **kw)
    assert head[0] == base[0]  # DIFF-1
    assert head[1] == base[1]  # DIFF-2: same log_query calls and arguments
    assert len(head[2]) == len(base[2])  # DIFF-2: same INSERT count
    for h, b in zip(head[2], base[2], strict=True):  # DIFF-3
        assert len(h) == len(b)
        for i, (hp, bp) in enumerate(zip(h, b, strict=True)):
            if i == 3:
                extra = {k: v for k, v in hp.items() if k not in bp}
                assert {k: v for k, v in hp.items() if k in bp} == bp
                assert not extra or (list(extra) == ["timings"] and _w_valid(extra["timings"]))
            else:
                assert hp == bp
    return head


def _one(et):
    return [_Call(et)]


TOOL_TIMES = {
    "10**400": 10**400,
    "None": None,
    "str": "4.2",
    "nan": float("nan"),
    "inf": float("inf"),
    "neg": -1,
    "prop-raises": ValueError("synthetic"),
    "radd-raises": _RadRaises(),
    "radd-nonnumber": _RadNonNumber(),
    "normal": 1.5,
}


@pytest.mark.parametrize("name", list(TOOL_TIMES))
def test_diff_hostile_tool_execution_time(monkeypatch, name):
    # r3 of #9198: the row was lost here (HEAD 0 INSERT vs 1).
    _, calls, inserts = _assert_differential(monkeypatch, steps=_one(TOOL_TIMES[name]))
    assert len(calls) == 1 and len(inserts) == 1


MAPPINGS = {
    "items-raises": lambda: _ItemsRaises(total=1.0),
    "bool-raises": lambda: _BoolRaises(total=1.0),
    "len-raises": lambda: _LenRaises(total=1.0),
    "int-not-mapping": lambda: 5,
    "nul-key": lambda: {"total": 1.0, "bad\x00key": 2.0},  # r2 of #9198
    "lone-high-surrogate": lambda: {"total": 1.0, "\ud800": 2.0},
    "lone-low-surrogate": lambda: {"total": 1.0, "\udc00": 2.0},
    "str-subclass-key": lambda: {"total": 1.0, _StrKey("llm"): 2.0},
    "value-10**400": lambda: {"total": 1.0, "llm": 10**400},
    "float-subclass": lambda: {"total": 1.0, "llm": _FloatRaises(2.0)},  # r1 of #9198
    "int-subclass": lambda: {"total": 1.0, "llm": _IntRaises(2)},
    "bool-value": lambda: {"total": 1.0, "llm": True},
    "decimal-value": lambda: {"total": 1.0, "llm": Decimal("1.5")},
    "valid": lambda: {"total": 1.25, "llm": 0.5},
    "empty": lambda: {},
}


@pytest.mark.parametrize("name", list(MAPPINGS))
def test_diff_hostile_extracted_mapping(monkeypatch, name):
    _, calls, inserts = _assert_differential(monkeypatch, steps=_one(0.5), extract=MAPPINGS[name])
    assert len(calls) == 1 and len(inserts) == 1


def test_s7_metrics_manager_failing_on_the_third_access_keeps_the_row(monkeypatch):
    # BASE reads core.metrics_manager twice (collections, sources); HEAD's read is inside the helper.
    _, calls, inserts = _assert_differential(monkeypatch, steps=_one(0.5), core_fail_at=3)
    assert len(calls) == 1 and len(inserts) == 1 and "timings" not in inserts[0][3]


def test_s7_unnamable_exception_from_the_extraction_keeps_the_row(monkeypatch):
    def boom(*_a, **_k):
        raise _Unnamable()

    with monkeypatch.context() as m:
        m.setattr(OrchestratorMetricsManager, "extract_timings_from_state", boom)
        _, calls, inserts = _drive(m, steps=_one(0.5))
    assert len(calls) == 1 and len(inserts) == 1 and "timings" not in inserts[0][3]


def test_s7_unnamable_exception_from_the_mapping_keeps_the_row(monkeypatch):
    _, calls, inserts = _assert_differential(
        monkeypatch, steps=_one(0.5), extract=lambda: _ItemsUnnamable(total=1.0)
    )
    assert len(calls) == 1 and len(inserts) == 1 and "timings" not in inserts[0][3]


def test_diff_extraction_raises(monkeypatch):
    _assert_differential(monkeypatch, steps=_one(0.5), extract="raise")


@pytest.mark.parametrize("failing", [1, 2])
def test_diff_analytics_clock_failure(monkeypatch, failing):
    reads = {"n": 0}

    def clock():
        reads["n"] += 1
        if reads["n"] == failing:
            raise OSError("synthetic")
        return float(reads["n"])

    _, calls, inserts = _assert_differential(monkeypatch, steps=_one(0.5), clock=clock)
    assert len(inserts) == 1 and "timings" not in inserts[0][3]


def test_diff_metrics_clock_failure(monkeypatch):
    def bad():
        raise OSError("synthetic")

    _, _, inserts = _assert_differential(monkeypatch, steps=_one(0.5), metrics_time=bad)
    assert len(inserts) == 1 and "timings" not in inserts[0][3]


def test_diff_db_pool_none_writes_nothing(monkeypatch):
    _, calls, inserts = _assert_differential(monkeypatch, steps=_one(0.5), db="none")
    assert calls == [] and inserts == []


def test_diff_db_error_both_return_none(monkeypatch):
    _, calls, inserts = _assert_differential(monkeypatch, steps=_one(0.5), db="dberror")
    assert len(calls) == 1 and len(inserts) == 1


def test_normal_run_adds_exactly_a_valid_timings_key(monkeypatch):
    _, _, inserts = _assert_differential(monkeypatch, steps=_one(1.5))
    timings = inserts[0][3]["timings"]
    assert _w_valid(timings) and {"total", "llm", "search", "tools"} <= set(timings)


def test_r3_hostile_execution_time_keeps_the_row(monkeypatch):
    """Named r3 of #9198: 10**400, None and "4.2" each used to cost the row."""
    for value in (10**400, None, "4.2"):
        with monkeypatch.context() as m:
            _, calls, inserts = _drive(m, steps=_one(value))
        assert len(calls) == 1 and len(inserts) == 1


def test_stage_timings_or_none_is_total_and_skips_extraction_without_a_reading():
    called = []

    class M:
        def extract_timings_from_state(self, **kw):
            called.append(kw)
            return {"total": 1.0}

    fn = orchestrator_streaming_core._stage_timings_or_none

    def owner(manager):
        return SimpleNamespace(core=SimpleNamespace(metrics_manager=manager))

    assert fn(owner(M()), object(), None, 2.0, 0.0, 0.0) is None
    assert fn(owner(M()), object(), 1.0, None, 0.0, 0.0) is None
    assert called == []
    assert fn(owner(M()), object(), 1.0, 2.0, 0.0, 0.0) == {"total": 1.0}
    assert (
        fn(owner(M()), object(), "x", 2.0, 0.0, 0.0) is None
    )  # subtraction fails -> None, not raise
    assert fn(owner(None), object(), 1.0, 2.0, 0.0, 0.0) is None
    assert fn(owner(M()), object(), 1.0, 2.0, 0.0, None) is None  # M3: a lost pause reading


def test_w10_a_million_rejected_keys_are_read_only_up_to_the_item_bound():
    mapping = _Counting()
    assert repo_module.stage_timings_payload(mapping) == {}
    assert mapping.reads == repo_module.STAGE_ITEMS_MAX


def test_toolcall_is_the_real_dataclass_shape():
    # guards the lookalike above: the real ToolCall still carries these fields
    call = ToolCall(tool_name="vector_search", arguments={})
    assert call.execution_time == 0.0


# --------------------------------------------------------------- T4: measurement (M1-M3)


class _FakeClock:
    """A deterministic _ANALYTICS_CLOCK; ``fail_at`` makes the Nth read return None or raise."""

    def __init__(self, fail_at: int | None = None, mode: str = "none") -> None:
        self.t = 100.0
        self.reads = 0
        self.fail_at = fail_at
        self.mode = mode

    def __call__(self) -> Any:
        self.reads += 1
        if self.reads == self.fail_at:
            if self.mode == "raise":
                raise OSError("synthetic clock failure")
            return None
        return self.t


class _TimedEngine:
    """Advances the fake clock by ``produce`` for every event it produces."""

    tool_map: dict = {}

    def __init__(self, clock: _FakeClock, events: list[dict], produce: float) -> None:
        self._clock, self._events, self._produce = clock, events, produce

    async def execute_react_loop_stream(self, **_kwargs):
        for event in self._events:
            self._clock.t += self._produce
            yield event


class _TimedManager(_Manager):
    """Building each processed event costs ``q`` of producer time (M2: not a pause)."""

    def __init__(self, clock: _FakeClock, q: float) -> None:
        self._clock, self._q = clock, q

    async def process_event_stream(self, *, event_generator, **_kwargs):
        async for event in event_generator:
            self._clock.t += self._q
            yield event


_TOKEN = {"type": "token", "data": "tok"}
_TOOL_EVENT = {"type": "tool_call", "data": {"tool_name": "vector_search"}}


def _run_t4(
    monkeypatch,
    *,
    events,
    produce,
    delay,
    q=0.0,
    tool_time=None,
    fail_at=None,
    mode="none",
    suppress=False,
):
    ticks = {"n": 0}

    def fake_time() -> float:
        ticks["n"] += 1
        return 1000.0 + 0.25 * ticks["n"]

    monkeypatch.setattr(time, "time", fake_time)
    clock = _FakeClock(fail_at, mode)
    monkeypatch.setattr(orchestrator_streaming_core, "_ANALYTICS_CLOCK", clock)
    real_thinking = orchestrator_streaming_core.ThinkingIndicatorService.create_thinking_event

    def timed_thinking(self, phase, *a, **k):
        if phase == orchestrator_streaming_core.ThinkingPhase.TOOL_CALLING:
            clock.t += q  # constructing the TOOL_CALLING event is producer work (M2)
        return real_thinking(self, phase, *a, **k)

    monkeypatch.setattr(
        orchestrator_streaming_core.ThinkingIndicatorService,
        "create_thinking_event",
        timed_thinking,
    )
    if suppress:
        monkeypatch.setattr(
            orchestrator_streaming_core, "_stage_timings_or_none", lambda *a, **k: None
        )
    state = AgentState(query="synthetic probe query", intent_type="business_complex")
    state.evidence_score = 0.8
    state.trusted_tools_used = True
    state.final_answer = "synthetic answer"
    state.steps = (
        [
            AgentStep(
                step_number=1,
                thought="t",
                action=ToolCall("vector_search", {}, "r", True, None, tool_time),
            )
        ]
        if tool_time is not None
        else []
    )
    pool = _Pool()
    core = _Core(state, pool, OrchestratorMetricsManager())
    core.reasoning_engine = _TimedEngine(clock, events, produce)
    sc = OrchestratorStreamingCore(core=core, streaming_manager=_TimedManager(clock, q))
    monkeypatch.setattr(sc, "_mark_react_stream_unfinalized", lambda *a, **k: None)
    collected: list = []

    async def go():
        gen = sc.stream_query_core(
            query="synthetic probe query",
            user_id="probe-user",
            conversation_history=None,
            session_id=None,
            images=None,
            tool_execution_counter={"count": 0},
            correlation_id="c",
        )
        while True:
            try:
                collected.append(copy.deepcopy(await gen.__anext__()))
            except StopAsyncIteration:
                return
            clock.t += delay  # the consumer's time between anext() calls

    asyncio.run(go())
    return collected, pool.inserts


@pytest.mark.parametrize("q", [0.0, 0.08])
@pytest.mark.parametrize("delay", [0.0, 0.08, 1.5])
@pytest.mark.parametrize(
    ("events", "tool_time", "yields"),
    [([_TOKEN, _TOKEN, _TOKEN], None, 3), ([_TOOL_EVENT, _TOKEN], 0.4, 3)],
    ids=["no-tool", "tool"],
)
def test_t4_reasoning_is_production_time_only_both_sides_of_the_yield(
    monkeypatch, events, tool_time, yields, delay, q
):
    """M1/M2: reasoning == total P (engine) + total Q (building each yield operand);
    the consumer's D is excluded. llm follows M1 (reasoning - tools when a tool ran)."""
    _, inserts = _run_t4(
        monkeypatch, events=events, produce=0.5, delay=delay, q=q, tool_time=tool_time
    )
    timings = inserts[0][3]["timings"]
    reasoning = 0.5 * len(events) + q * yields
    assert timings["reasoning"] == pytest.approx(reasoning, abs=1e-4), (delay, q)
    assert timings["llm"] == pytest.approx(max(0.0, reasoning - (tool_time or 0.0)), abs=1e-4), (
        delay,
        q,
    )


@pytest.mark.parametrize(
    "events", [[_TOKEN, _TOKEN], [_TOOL_EVENT, _TOKEN]], ids=["no-tool", "tool"]
)
@pytest.mark.parametrize("mode", ["none", "raise"])
def test_t4_a_failing_per_yield_read_writes_the_row_without_timings(monkeypatch, events, mode):
    yields = len(events) + sum(1 for e in events if e["type"] == "tool_call")
    reads_total = 2 + 2 * yields  # start, 2 per yield, end
    base_events, base_inserts = _run_t4(
        monkeypatch, events=events, produce=0.5, delay=0.08, q=0.08, suppress=True
    )
    for fail_at in range(1, reads_total + 1):
        with monkeypatch.context() as m:
            got_events, got_inserts = _run_t4(
                m, events=events, produce=0.5, delay=0.08, q=0.08, fail_at=fail_at, mode=mode
            )
        assert got_events == base_events, fail_at  # same stream
        assert len(got_inserts) == 1, fail_at  # the row survives
        assert "timings" not in got_inserts[0][3], fail_at  # but without timings
        for i, (g, b) in enumerate(zip(got_inserts[0], base_inserts[0], strict=True)):
            assert i == 3 or g == b, (fail_at, i)
