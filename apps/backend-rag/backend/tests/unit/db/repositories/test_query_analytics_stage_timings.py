"""`query_analytics` must keep WHERE a query's time went, not only the total.

Measured on prod 2026-11-03: 52 of 827 rows in 90 days carried a per-stage
breakdown in ``metadata.timings``, all from the oracle writer, the last on
2026-09-01. The agentic writers had the dict in hand (``_log_query_analytics``
receives ``timings``; the streaming path has the state) and kept only
``execution_time_ms``. On the 52 rows that had it, p50 llm was 38.4 s against
4.4 s for search — the breakdown is what tells latency work where to look.

Halves, each tested on its own:

* the repository writes the breakdown, sanitised, under ``metadata.timings``;
* every writer hands it over;
* the consumer (``get_stage_latency`` behind ``GET .../latency``) reads it,
  proven against a real Postgres because the JSON guards are SQL semantics.
"""

from __future__ import annotations

import ast
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

_STAGES = {
    "total": 41.2,
    "embedding": 0.0,
    "search": 4.36,
    "rerank": 0.0,
    "llm": 33.1,
    "reasoning": 36.8,
    "tools": 4.4,
}


def _mock_pool() -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value = ctx
    return pool, conn


def _bound_metadata(conn: AsyncMock) -> Any:
    """Read the bound ``metadata`` by column NAME, never by a fixed index."""
    call = conn.fetchrow.call_args
    sql: str = call[0][0]
    assert "INSERT INTO query_analytics" in sql, "asserting against the wrong INSERT"
    columns_block = sql.split("(", 1)[1].split(")", 1)[0]
    columns = [c.strip() for c in columns_block.replace("\n", " ").split(",") if c.strip()]
    assert "metadata" in columns, f"the INSERT does not name metadata; columns={columns}"
    return call[0][columns.index("metadata") + 1]


class TestRepositoryWritesTheBreakdown:
    @pytest.mark.asyncio
    async def test_guilt_stage_timings_land_in_metadata_in_seconds(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            QueryAnalyticsRepository,
        )

        pool, conn = _mock_pool()
        conn.fetchrow = AsyncMock(return_value={"id": "00000000-0000-0000-0000-0000000000a1"})

        await QueryAnalyticsRepository(pool).log_query(
            query_text="q",
            user_id="u@example.com",
            execution_time_ms=41200,
            stage_timings=_STAGES,
        )

        metadata = _bound_metadata(conn)
        assert metadata == {"user_email": "u@example.com", "timings": _STAGES}

    @pytest.mark.asyncio
    async def test_innocence_no_breakdown_leaves_metadata_as_before(self) -> None:
        """A caller that never heard of the parameter writes the old shape."""
        from backend.db.repositories.query_analytics_repository import (
            QueryAnalyticsRepository,
        )

        for given in (None, {}):
            pool, conn = _mock_pool()
            conn.fetchrow = AsyncMock(return_value={"id": "00000000-0000-0000-0000-0000000000a2"})
            await QueryAnalyticsRepository(pool).log_query(
                query_text="q", user_id="u@example.com", stage_timings=given
            )
            assert _bound_metadata(conn) == {"user_email": "u@example.com"}, given

    def test_guilt_values_that_would_poison_a_percentile_are_dropped(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            stage_timings_payload,
        )

        payload = stage_timings_payload(
            {
                "search": 4,
                "llm": 33.123456,
                "flag": True,
                "nan": math.nan,
                "inf": math.inf,
                "negative": -1.0,
                "text": "4.2",
                "nested": {"a": 1.0},
                "none": None,
                "huge": 10**400,
                "over_a_day": 86400.0001,
                # W6: the range test runs on the UNROUNDED value; rounding first
                # would keep these two as 86400.0 and -0.0.
                "rounds_into_a_day": 86400.00004,
                "rounds_into_zero": -0.00004,
                "a_day": 86400,
                "tiny": 1e-9,
                "minus_inf": -math.inf,
                "decimal": Decimal("1.5"),
                1: 2.0,
            }
        )

        assert payload == {"search": 4.0, "llm": 33.1235, "a_day": 86400.0, "tiny": 0.0}
        assert all(type(v) is float for v in payload.values())

    @pytest.mark.asyncio
    async def test_guilt_one_unconvertible_stage_does_not_cost_the_row(self) -> None:
        """Gate 9191 r1: an int beyond float range raised inside log_query,
        which swallowed it and wrote NOTHING — answer and total lost too."""
        from backend.db.repositories.query_analytics_repository import (
            QueryAnalyticsRepository,
        )

        pool, conn = _mock_pool()
        conn.fetchrow = AsyncMock(return_value={"id": "00000000-0000-0000-0000-0000000000a4"})

        query_id = await QueryAnalyticsRepository(pool).log_query(
            query_text="q", stage_timings={"llm": 1.0, "search": 10**400}
        )

        assert query_id == "00000000-0000-0000-0000-0000000000a4"
        assert conn.fetchrow.await_count == 1
        assert _bound_metadata(conn) == {"timings": {"llm": 1.0}}


class _RaisingFloat(float):
    def __float__(self) -> float:
        raise ValueError("a subclass with its own __float__")


class _RaisingInt(int):
    def __float__(self) -> float:
        raise TypeError("a subclass with its own __float__")


class _ExplodingMapping(dict):
    def items(self):  # type: ignore[override]
        raise RuntimeError("items() raises")


class TestTheWriterIsTotal:
    """Gate 9198 r1: a float subclass whose __float__ raised cost the whole row."""

    def test_w3_numeric_subclasses_are_dropped_and_siblings_kept(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            stage_timings_payload,
        )

        payload = stage_timings_payload(
            {"good": 2.5, "f": _RaisingFloat(1.5), "i": _RaisingInt(3), "b": False}
        )

        assert payload == {"good": 2.5}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("given", "expected_metadata"),
        [
            ({"good": 2.5, "bad": _RaisingFloat(1.5)}, {"timings": {"good": 2.5}}),
            ({"good": 2.5, "bad": _RaisingInt(1)}, {"timings": {"good": 2.5}}),
            (_ExplodingMapping(good=2.5), {}),
        ],
    )
    async def test_w3_w9_a_hostile_value_or_mapping_never_costs_the_row(
        self, given: Any, expected_metadata: dict[str, Any]
    ) -> None:
        from backend.db.repositories.query_analytics_repository import (
            QueryAnalyticsRepository,
        )

        pool, conn = _mock_pool()
        conn.fetchrow = AsyncMock(return_value={"id": "00000000-0000-0000-0000-0000000000a5"})

        query_id = await QueryAnalyticsRepository(pool).log_query(
            query_text="q", execution_time_ms=1200, stage_timings=given
        )

        assert query_id == "00000000-0000-0000-0000-0000000000a5"
        assert conn.fetchrow.await_count == 1
        assert _bound_metadata(conn) == expected_metadata


_HOSTILE_KEYS = [
    "bad\x00key",
    "\ud800",
    "\udc00",
    "",
    "LLM",
    "a b",
    "caf\u00e9",
    "x" * 65,
    "1abc",
    "llm\n",
    "llm-time",
]


class TestTheKeyDomainIsClosed:
    """Gate 9198 r2: a NUL / lone-surrogate key reached the INSERT and Postgres
    jsonb rejected it, losing the row. W2 closes the key domain, W10 its size."""

    @pytest.mark.parametrize("key", _HOSTILE_KEYS)
    def test_w2_a_key_outside_the_identifier_domain_costs_its_stage(self, key: str) -> None:
        from backend.db.repositories.query_analytics_repository import (
            stage_timings_payload,
        )

        assert stage_timings_payload({"llm": 1.0, key: 2.0}) == {"llm": 1.0}

    def test_w2_every_stage_the_agentic_writers_emit_is_inside_the_domain(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            stage_timings_payload,
        )

        names = {**_STAGES, "a" * 64: 1.0, "tools_2": 1.0}
        assert stage_timings_payload(names) == names

    def test_w10_at_most_stage_items_max_stages_in_input_order(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            STAGE_ITEMS_MAX,
            stage_timings_payload,
        )

        given = {f"s{i:02d}": float(i) for i in range(STAGE_ITEMS_MAX + 8)}
        payload = stage_timings_payload(given)

        assert list(payload) == [f"s{i:02d}" for i in range(STAGE_ITEMS_MAX)]


class TestEveryWriterHandsOverTheBreakdown:
    """Read the call sites from the AST, as the answer-retention guard does.

    An enumeration, not a fixed list: a later writer inherits the guard.
    """

    @staticmethod
    def _calls_named(dotted: str, attr: str) -> list[ast.Call]:
        import importlib

        module = importlib.import_module(dotted)
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        return [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
        ]

    @pytest.mark.parametrize(
        "dotted",
        [
            "backend.services.rag.agentic.orchestrator_core",
            "backend.services.rag.agentic.orchestrator_streaming_core",
        ],
    )
    def test_guilt_every_log_query_call_passes_stage_timings(self, dotted: str) -> None:
        calls = self._calls_named(dotted, "log_query")
        assert calls, f"found no log_query call in {dotted}: the probe looks at the wrong name"
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            assert "stage_timings" in kwargs, (
                f"{dotted}:{call.lineno} calls log_query() without stage_timings — "
                f"the row keeps the total and loses where the time went"
            )


@pytest.mark.asyncio
async def test_guilt_the_orchestrator_forwards_the_real_breakdown() -> None:
    from backend.services.llm_clients.pricing import TokenUsage
    from backend.services.rag.agentic.orchestrator_core import OrchestratorCore

    pool, conn = _mock_pool()
    conn.fetchrow = AsyncMock(return_value={"id": "00000000-0000-0000-0000-0000000000a3"})
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.db_pool = pool

    await core._log_query_analytics(
        query="q",
        user_id=None,
        session_id=None,
        collections_used=set(),
        sources=[],
        model_used="gemini",
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, model="gemini"),
        timings=dict(_STAGES),
        response_generated=True,
        response_text="a",
    )

    assert _bound_metadata(conn) == {"timings": _STAGES}


class TestStageLatencyConsumer:
    @pytest.mark.asyncio
    async def test_rows_are_mapped_and_rounded(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            QueryAnalyticsRepository,
        )

        pool, conn = _mock_pool()
        conn.fetch = AsyncMock(
            return_value=[
                {"stage": "llm", "samples": 52, "p50_s": 38.36912, "p95_s": Decimal("78.3051")},
            ]
        )

        rows = await QueryAnalyticsRepository(pool).get_stage_latency(days=30)

        assert rows == [{"stage": "llm", "samples": 52, "p50_s": 38.369, "p95_s": 78.305}]

    @pytest.mark.asyncio
    async def test_a_database_error_returns_empty_not_raises(self) -> None:
        from backend.db.repositories.query_analytics_repository import (
            QueryAnalyticsRepository,
        )

        pool, conn = _mock_pool()
        conn.fetch = AsyncMock(side_effect=RuntimeError("boom"))

        assert await QueryAnalyticsRepository(pool).get_stage_latency() == []


# Gate 9191 r3: valid jsonb whose round(_, 4) overflows numeric weight (131072
# digits before the point is numeric's maximum; rounding carries into a 131073rd).
_R3_NINES = "9" * 131072 + ".99999"


def _dsn() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql://nuzantara@localhost:5432/nuzantara_test"
    )


@pytest.fixture
async def pg_pool() -> AsyncIterator[Any]:
    """One connection with a TEMP ``query_analytics`` that shadows any real one."""
    import asyncpg

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(
            "CREATE TEMP TABLE query_analytics (created_at timestamptz NOT NULL, metadata jsonb)"
        )

        class _Pool:
            @asynccontextmanager
            async def acquire(self) -> AsyncIterator[Any]:
                yield conn

        yield _Pool(), conn
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_consumer_reads_real_jsonb_and_skips_what_it_cannot_read(
    pg_pool: Any,
) -> None:
    """The guards are SQL semantics, so they are proven on Postgres.

    Rows the consumer must survive: metadata NULL, metadata a jsonb string
    scalar (the 2026-05-14 double-encoding regression left such rows) or an
    array, ``timings`` not an object / null / empty, a stage that is not a
    number, numbers outside [0, 86400] s — including 1e400, valid jsonb that
    overflows float8 (gate 9191 r1) — and a row older than the window. Only
    the three in-range ``llm`` samples and the one ``search`` sample inside
    the window may count. 86400.0001 is outside the domain and is dropped;
    1e-400 is inside it and rounds to 0 as numeric, never reaching a float8 cast.
    """
    from backend.db.repositories.query_analytics_repository import (
        QueryAnalyticsRepository,
    )

    pool, conn = pg_pool
    await conn.execute(
        """
        INSERT INTO query_analytics (created_at, metadata) VALUES
          (now(), '{"timings": {"llm": 10.0, "search": 2.0}}'),
          (now(), '{"timings": {"llm": 20.0, "search": "slow"}}'),
          (now(), '{"user_email": "u@example.com", "timings": {"llm": 30.0}}'),
          (now(), NULL),
          (now(), '"{\\"timings\\": {\\"llm\\": 999}}"'),
          (now(), '{"timings": [1, 2, 3]}'),
          (now(), '{"timings": 5}'),
          (now(), '{"timings": null}'),
          (now(), '{"timings": {}}'),
          (now(), '[1, 2]'),
          (now(), '{"timings": {"llm": 1e400}}'),
          (now(), '{"timings": {"llm": -5}}'),
          (now(), '{"timings": {"llm": 100000}}'),
          (now(), '{"timings": {"llm": 86400.0001}}'),
          (now(), '{"timings": {"rerank": 1e-400}}'),
          (now(), '{"timings": {"cap": 86400}}'),
          (now() - interval '30 days', '{"timings": {"llm": 999.0}}')
        """
    )
    for weight_overflow in (_R3_NINES, "-" + _R3_NINES):
        await conn.execute(
            "INSERT INTO query_analytics (created_at, metadata) VALUES (now(), $1::jsonb)",
            '{"timings": {"llm": ' + weight_overflow + "}}",
        )

    rows = await QueryAnalyticsRepository(pool).get_stage_latency(days=7)

    # 1e-400 underflows a direct float8 cast (gate 9191 r2); inside the domain it is 0 s at the domain's precision.
    assert rows == [
        {"stage": "cap", "samples": 1, "p50_s": 86400.0, "p95_s": 86400.0},
        {"stage": "llm", "samples": 3, "p50_s": 20.0, "p95_s": 29.0},
        {"stage": "rerank", "samples": 1, "p50_s": 0.0, "p95_s": 0.0},
        {"stage": "search", "samples": 1, "p50_s": 2.0, "p95_s": 2.0},
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_r3_a_value_that_cannot_be_rounded_costs_only_its_own_sample(
    pg_pool: Any,
) -> None:
    """Spec C3/C4: the range test is a pure comparison and comes first.

    #9191's SQL rounded before filtering, so this one value raised inside the
    aggregate and the endpoint answered [] for every stage of every row.
    """
    from backend.db.repositories.query_analytics_repository import (
        QueryAnalyticsRepository,
    )

    pool, conn = pg_pool
    for metadata in (
        '{"timings": {"llm": 12.5, "search": 1.5}}',
        '{"timings": {"llm": ' + _R3_NINES + ', "search": 2.5}}',
        '{"timings": {"llm": 14.5}}',
    ):
        await conn.execute(
            "INSERT INTO query_analytics (created_at, metadata) VALUES (now(), $1::jsonb)",
            metadata,
        )

    rows = await QueryAnalyticsRepository(pool).get_stage_latency(days=1)

    assert rows == [
        {"stage": "llm", "samples": 2, "p50_s": 13.5, "p95_s": 14.4},
        {"stage": "search", "samples": 2, "p50_s": 2.0, "p95_s": 2.45},
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_w11_whatever_the_writer_emits_is_accepted_by_postgres_jsonb(
    pg_pool: Any,
) -> None:
    """W11 on real Postgres, through the production jsonb codec.

    The hostile keys and values all reach the sanitizer; what comes out binds
    as a jsonb object and reads back unchanged. Without W2 the NUL and the lone
    surrogates make Postgres reject the INSERT (gate 9198 r2).
    """
    from backend.core.pg_json_codec import init_asyncpg_connection
    from backend.db.repositories.query_analytics_repository import (
        stage_timings_payload,
    )

    _pool, conn = pg_pool
    await init_asyncpg_connection(conn)
    given: dict[Any, Any] = dict.fromkeys(_HOSTILE_KEYS, 1.0)
    given.update(
        {"llm": 33.123456, "a_day": 86400, "tiny": 1e-9, "minus_zero": -0.0, "bad": 10**400}
    )
    payload = stage_timings_payload(given)

    await conn.execute(
        "INSERT INTO query_analytics (created_at, metadata) VALUES (now(), $1)",
        {"timings": payload},
    )
    stored = await conn.fetchval("SELECT metadata->'timings' FROM query_analytics")

    assert stored == payload == {"llm": 33.1235, "a_day": 86400.0, "tiny": 0.0, "minus_zero": -0.0}


class _IntRaises(int):
    def __float__(self) -> float:
        raise ValueError("synthetic")


class _StrKey(str):
    pass


class _ItemsRaises(dict):
    def items(self) -> Any:
        raise ValueError("synthetic")


class _BoolRaises(dict):
    def __bool__(self) -> bool:
        raise ValueError("synthetic")


class _LenRaises(dict):
    def __len__(self) -> int:
        raise ValueError("synthetic")


class _ItemsNotPairs(dict):
    def items(self) -> Any:
        return iter([1, 2, 3])


class _MillionRejected:
    """10**6 rejected keys; only the first STAGE_ITEMS_MAX may be read."""

    def __bool__(self) -> bool:
        return True

    def items(self) -> Any:
        return ((f"K{i}", 1.0) for i in range(10**6))


# Every mapping class of the brief's T1 (the ones extract_timings_from_state is patched to return).
_T1_MAPPINGS: dict[str, Any] = {
    "items-raises": lambda: _ItemsRaises(total=1.0),
    "bool-raises": lambda: _BoolRaises(total=1.0),
    "len-raises": lambda: _LenRaises(total=1.0),
    "items-not-pairs": lambda: _ItemsNotPairs(total=1.0),
    "not-a-mapping": lambda: 5,
    "million-rejected-keys": _MillionRejected,
    "nul-key": lambda: {"total": 1.0, "bad\x00key": 2.0},
    "high-surrogate": lambda: {"total": 1.0, "\ud800": 2.0},
    "low-surrogate": lambda: {"total": 1.0, "\udc00": 2.0},
    "str-subclass-key": lambda: {"total": 1.0, _StrKey("llm"): 2.0},
    "value-10**400": lambda: {"total": 1.0, "llm": 10**400},
    "float-subclass": lambda: {"total": 1.0, "llm": _RaisingFloat(2.0)},
    "int-subclass": lambda: {"total": 1.0, "llm": _IntRaises(2)},
    "value-true": lambda: {"total": 1.0, "llm": True},
    "value-decimal": lambda: {"total": 1.0, "llm": Decimal("1.5")},
    "nan-inf-negative": lambda: {"a": float("nan"), "b": float("inf"), "c": -1.0, "d": 86400.0001},
    "valid": lambda: {"total": 1.25, "llm": 0.5, "search": 0.25},
    "empty": dict,
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_T1_MAPPINGS))
async def test_w11_every_t1_mapping_binds_into_real_jsonb_through_the_codec(
    pg_pool: Any, name: str
) -> None:
    """T3: the sanitizer's output for each T1 mapping, with log_query's guard (S4), stores as jsonb."""
    from backend.core.pg_json_codec import init_asyncpg_connection
    from backend.db.repositories.query_analytics_repository import stage_timings_payload

    _pool, conn = pg_pool
    await init_asyncpg_connection(conn)
    try:
        payload = stage_timings_payload(_T1_MAPPINGS[name]())
    except Exception:
        payload = {}  # S4: the guard in log_query
    metadata: dict[str, Any] = {"user_email": "probe-user"}
    if payload:
        metadata["timings"] = payload

    await conn.execute(
        "INSERT INTO query_analytics (created_at, metadata) VALUES (now(), $1)", metadata
    )
    stored = await conn.fetchval("SELECT metadata FROM query_analytics")

    assert stored == metadata
    if "timings" in metadata:
        assert (
            await conn.fetchval("SELECT jsonb_typeof(metadata->'timings') FROM query_analytics")
            == "object"
        )


_C8_BAD_KEYS = ["LLM", "a b", "llm-time", "1abc", "k" * 65, "caf\u00e9", "llm\n"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_c8_keys_outside_the_writer_key_domain_are_never_counted(pg_pool: Any) -> None:
    """C8: a valid key's aggregate is unchanged by rows that carry keys outside W2 beside it."""
    import json

    from backend.db.repositories.query_analytics_repository import QueryAnalyticsRepository

    pool, conn = pg_pool
    valid = {"llm": 10.0, "k" * 64: 1.0}
    for llm in (10.0, 20.0, 30.0):
        timings = {**valid, "llm": llm, **dict.fromkeys(_C8_BAD_KEYS, 5.0)}
        await conn.execute(
            "INSERT INTO query_analytics (created_at, metadata) VALUES (now(), $1::jsonb)",
            json.dumps({"timings": timings}),
        )

    rows = await QueryAnalyticsRepository(pool).get_stage_latency(days=1)

    assert rows == [
        {"stage": "k" * 64, "samples": 3, "p50_s": 1.0, "p95_s": 1.0},
        {"stage": "llm", "samples": 3, "p50_s": 20.0, "p95_s": 29.0},
    ]
    assert not {r["stage"] for r in rows} & set(_C8_BAD_KEYS)


def test_c8_the_sql_carries_the_explicit_character_lists_and_no_range() -> None:
    import inspect

    from backend.db.repositories.query_analytics_repository import QueryAnalyticsRepository

    source = inspect.getsource(QueryAnalyticsRepository.get_stage_latency)
    assert (
        "t.key ~ '^[abcdefghijklmnopqrstuvwxyz][abcdefghijklmnopqrstuvwxyz0123456789_]{0,63}$'"
        in source
    )
    assert "a-z" not in source and "0-9" not in source
