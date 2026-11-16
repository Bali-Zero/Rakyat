"""Related-subject registry, slice 1: every acceptance criterion on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database, applies migration 361's forward part
and reduced CRM tables, and runs the registry service or the real route (with the real
middleware stack) against it. Synthetic placeholders only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import backend.app.routers.crm_related_subjects as route_module
from backend.app.core.config import settings
from backend.app.dependencies import get_database_pool
from backend.db.migration_base import split_migration_sql
from backend.middleware import request_tracing
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware, role_boundary_refuses
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm.related_identifiers import NORM_VERSION, canonical_form, sql_canonical
from backend.services.crm.related_subjects import (
    REGISTRY_ACTOR,
    RegistryUnavailable,
    SubjectInput,
    prepare_identifiers,
    register_subject,
)
from backend.services.monitoring.activity_logger import activity_logger

MIGRATIONS = Path(route_module.__file__).resolve().parents[2] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "361_related_subjects_registry.sql"
URL = "/api/crm/related-subjects/register"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
SHA = "a" * 64
NEW_TABLES = {"related_subjects", "related_subject_identifiers"}
AUDIT_TABLES = {"api_audit_trail", "session_tracking"}
SENTINEL_NAME = "Zz Sentinel Name"
SENTINEL_ID = "ZZSENT0001"

CRM_SCHEMA = """
CREATE TABLE clients (
    id serial PRIMARY KEY, full_name text, passport_number varchar(100), deleted_at timestamptz
);
CREATE TABLE client_family_members (
    id serial PRIMARY KEY, client_id int, full_name text, passport_number varchar(100)
);
CREATE TABLE companies (
    id serial PRIMARY KEY, company_name text, npwp_company varchar(100), nib varchar(100)
);
CREATE TABLE api_audit_trail (
    id serial PRIMARY KEY, user_email varchar(255), method varchar(10) NOT NULL,
    endpoint varchar(500) NOT NULL, query_params jsonb DEFAULT '{}', request_body jsonb,
    response_status integer, response_body jsonb, response_time_ms integer, error_message text,
    ip_address varchar(50), user_agent text, correlation_id varchar(100),
    session_id varchar(255), created_at timestamp DEFAULT now()
);
CREATE TABLE session_tracking (
    id serial PRIMARY KEY, session_id varchar(255) UNIQUE NOT NULL,
    user_email varchar(255) NOT NULL, ip_address varchar(50), user_agent text,
    device_type varchar(50), browser varchar(100), os varchar(100),
    login_at timestamp DEFAULT now(), logout_at timestamp,
    last_activity_at timestamp DEFAULT now(), duration_seconds integer,
    actions_count integer DEFAULT 0, is_active boolean DEFAULT true
);
"""


# Every row written to any table of the schema is recorded (table, operation), same-value
# updates and insert-then-delete included. Row-level, so a statement that touches no row
# records nothing. `await clear_writes(conn)` arms a measurement window.
WRITE_LOG_SQL = """
CREATE TABLE write_log (tbl text NOT NULL, op text NOT NULL);
CREATE FUNCTION log_write() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    INSERT INTO write_log VALUES (TG_TABLE_NAME, TG_OP);
    RETURN NULL;
END
$f$;
DO $do$
DECLARE t text;
BEGIN
    FOR t IN SELECT table_name FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name <> 'write_log'
    LOOP
        EXECUTE format(
            'CREATE TRIGGER log_write AFTER INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION log_write()', t);
    END LOOP;
END
$do$;
"""


async def clear_writes(c) -> None:
    await c.execute("DELETE FROM write_log")


async def written(c) -> set[str]:
    return {r["tbl"] for r in await c.fetch("SELECT DISTINCT tbl FROM write_log")}


# ── database fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def pool():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for the real PostgreSQL registry proof")
    admin = await asyncpg.connect(dsn)
    name = await admin.fetchval("SELECT current_database()")
    if not re.search(r"_test(?:_run_[0-9]+_[0-9]+)?(?:_gw[0-9]+)?$", name):
        await admin.close()
        pytest.fail("A test database or isolated test worker is required")
    schema = "related_subjects_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn, min_size=1, max_size=6, server_settings={"search_path": schema}
    )
    try:
        forward, _ = split_migration_sql(MIGRATION.read_text())
        async with created.acquire() as conn:
            await conn.execute(forward)
            await conn.execute(CRM_SCHEMA)
            await conn.execute(WRITE_LOG_SQL)
        yield created
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest_asyncio.fixture
async def conn(pool):
    async with pool.acquire() as c:
        yield c


async def snapshot(c, *, exclude: set[str] = frozenset()) -> dict[str, tuple[int, str]]:
    """Row count and content hash of every table in the test schema."""
    names = [
        r["table_name"]
        for r in await c.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY 1"
        )
    ]
    out = {}
    for name in names:
        if name in exclude or name == "write_log":
            continue
        row = await c.fetchrow(
            f"SELECT count(*) AS n, coalesce(md5(string_agg(t::text, $$|$$ ORDER BY t::text)), $$$$) "
            f'AS h FROM "{name}" t'
        )
        out[name] = (row["n"], row[1])
    return out


def changed(before: dict, after: dict) -> set[str]:
    return {name for name in after if after[name] != before.get(name)}


# ── builders ──────────────────────────────────────────────────────────────────


def subj(kind="person", ids=(("passport", "ZZ0000001"),), dob=None, name=None, **kw):
    return SubjectInput(
        kind=kind,
        identifiers=prepare_identifiers(kind, [(t, v, {}) for t, v in ids]),
        evidence={"plan_sha256": SHA, "subject_ref": "s-1", "tier": "T1"},
        display_name=name,
        date_of_birth=dob,
        **kw,
    )


def body(kind="person", ids=(("passport", "ZZ0000001"),), **over):
    data = {
        "kind": kind,
        "identifiers": [{"type": t, "value": v} for t, v in ids],
        "evidence": {"plan_sha256": SHA, "subject_ref": "s-1", "tier": "T1"},
        "dry_run": False,
    }
    data.update(over)
    return data


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)


# ── a real-stack app, with spies where the generic handlers would speak ──────


class _Alerts:
    def __init__(self):
        self.calls = []

    async def send_http_error_alert(self, **kwargs):
        self.calls.append(kwargs)

    async def send_latency_alert(self, **kwargs):
        self.calls.append(kwargs)


class _Probe:
    """Innermost pure-ASGI middleware: records what reaches the middleware layer."""

    def __init__(self, app, sink):
        self.app = app
        self.sink = sink

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            self.sink["raised"].append(type(exc).__name__)
            raise
        finally:
            if "request_body" in (scope.get("state") or {}):
                self.sink["request_body_set"] = True


class Stack:
    def __init__(self, pool, *, middleware=True):
        self.alerts = _Alerts()
        self.sink = {"raised": [], "handlers": [], "request_body_set": False}
        app = FastAPI()
        app.state.db_pool = pool
        app.include_router(route_module.router)

        def spy(label):
            async def handler(request, exc):
                self.sink["handlers"].append(label)
                return JSONResponse(status_code=500, content={"detail": "generic"})

            return handler

        app.add_exception_handler(HTTPException, spy("http"))
        app.add_exception_handler(StarletteHTTPException, spy("starlette"))
        app.add_exception_handler(RequestValidationError, spy("validation"))
        app.add_exception_handler(Exception, spy("general"))
        if middleware:
            app.add_middleware(_Probe, sink=self.sink)
            app.add_middleware(HybridAuthMiddleware)
            app.add_middleware(RequestTracingMiddleware)
            app.add_middleware(ErrorMonitoringMiddleware, alert_service=self.alerts)
            app.add_middleware(ActivityLoggingMiddleware)
        self.app = app

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=True),
            base_url="http://test",
        )


@pytest.fixture
def audit_logger(pool):
    previous = (activity_logger.pool, activity_logger._initialized)
    activity_logger.pool, activity_logger._initialized = pool, True
    yield activity_logger
    activity_logger.pool, activity_logger._initialized = previous


async def post(stack, payload, headers=HEADERS):
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    async with stack.client() as http:
        return await http.post(URL, content=raw, headers=headers)


# ── criterion 1: service level, one connection, counters on the two tables only ──


@pytest.mark.asyncio
async def test_c1_batch_counters_cover_the_two_new_tables_only(conn):
    tr = conn.transaction()
    await tr.start()
    try:
        for n in range(3):
            out = await register_subject(
                conn, subj(ids=(("passport", f"ZZ000000{n}"),), name=f"Zz Probe {n}"), dry_run=False
            )
            assert out["outcome"] == "created"
        await register_subject(conn, subj(ids=(("passport", "ZZ0000000"),)), dry_run=False)
        rows = await conn.fetch(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables "
            "WHERE schemaname = current_schema()"
        )
    finally:
        await tr.rollback()
    # write_log is the measuring instrument (trigger target), not a table the registry writes.
    touched = {
        r["relname"]
        for r in rows
        if r["relname"] != "write_log" and r["n_tup_ins"] + r["n_tup_upd"] + r["n_tup_del"] > 0
    }
    assert touched == NEW_TABLES
    assert all(r["n_tup_ins"] > 0 for r in rows if r["relname"] in NEW_TABLES)
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 0


# ── criterion 2: HTTP level, what a call writes ──────────────────────────────


@pytest.mark.asyncio
async def test_c2_http_call_writes_two_tables_and_the_audit_row_only(
    pool, conn, enabled, audit_logger
):
    stack = Stack(pool)
    before = await snapshot(conn)
    await clear_writes(conn)
    resp = await post(stack, body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID),)))
    assert resp.status_code == 200 and resp.json()["outcome"] == "created"
    after = await snapshot(conn)
    assert changed(before, after) == NEW_TABLES | {"api_audit_trail"}
    assert await written(conn) == NEW_TABLES | {"api_audit_trail"}
    audit = await conn.fetch("SELECT * FROM api_audit_trail")
    assert len(audit) == 1 and audit[0]["request_body"] is None
    assert SENTINEL_NAME not in str(dict(audit[0])) and SENTINEL_ID not in str(dict(audit[0]))
    assert stack.sink["request_body_set"] is False


@pytest.mark.asyncio
async def test_c2_session_header_and_user_email_also_update_session_tracking(
    pool, conn, enabled, audit_logger
):
    stack = Stack(pool)
    await conn.execute(
        "INSERT INTO session_tracking (session_id, user_email) "
        "VALUES ('sess-probe-1', 'staff@example.test')"
    )
    before = await snapshot(conn)
    await clear_writes(conn)
    headers = {**HEADERS, "X-Session-ID": "sess-probe-1", "X-User-Email": "staff@example.test"}
    resp = await post(
        stack, body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID),)), headers
    )
    assert resp.status_code == 200
    after = await snapshot(conn)
    assert changed(before, after) == NEW_TABLES | AUDIT_TABLES
    assert await written(conn) == NEW_TABLES | AUDIT_TABLES
    dump = str(await conn.fetch("SELECT * FROM session_tracking")) + str(
        await conn.fetch("SELECT * FROM api_audit_trail")
    )
    assert SENTINEL_NAME not in dump and SENTINEL_ID not in dump
    assert all(r["request_body"] is None for r in await conn.fetch("SELECT * FROM api_audit_trail"))


# ── criterion 3: replay ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c3_replay_is_already_registered_and_changes_no_row(pool, conn, enabled):
    stack = Stack(pool, middleware=False)
    payload = body(
        kind="person", ids=(("passport", "ZZ0000001"), ("nik", "3201234567890123")),
        date_of_birth="1990-01-02", display_name="Zz Probe Subject",
    )  # fmt: skip
    first = await post(stack, payload)
    assert first.json()["outcome"] == "created"
    before = await snapshot(conn)
    again = await post(stack, payload)
    assert again.json()["outcome"] == "already_registered"
    assert again.json()["match_basis"] == "identifiers_and_dob"
    assert again.json()["subject_id"] == first.json()["subject_id"]
    assert await snapshot(conn) == before


# ── criterion 4: the key-set rule ────────────────────────────────────────────


async def _seed(conn, ids=(("nik", "3201234567890123"), ("passport", "ZZ0000001")), dob=None):
    out = await register_subject(
        conn, subj(ids=ids, dob=dob, name="Zz Probe Subject"), dry_run=False
    )
    assert out["outcome"] == "created"
    return out["subject_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "reason"),
    [
        ((("passport", "ZZ0000001"),), "set_differs"),
        (
            (("nik", "3201234567890123"), ("passport", "ZZ0000001"), ("passport", "ZZ0000002")),
            "set_differs",
        ),
        ((("passport", "ZZ0000001"), ("passport", "ZZ0000003")), "partial_overlap"),
    ],
    ids=["subset", "superset", "partial-overlap-with-new-key"],
)
async def test_c4_key_set_differs_is_conflict_and_writes_nothing(conn, ids, reason):
    owner = await _seed(conn)
    before = await snapshot(conn)
    out = await register_subject(conn, subj(ids=ids), dry_run=False)
    assert out["outcome"] == "conflict" and out["reason"] == reason
    assert out["subject_id"] == owner
    assert await snapshot(conn) == before


@pytest.mark.asyncio
async def test_c4_keys_of_two_owners_conflict(conn):
    await _seed(conn)
    await _seed(conn, ids=(("passport", "ZZ0000005"),))
    before = await snapshot(conn)
    out = await register_subject(
        conn, subj(ids=(("passport", "ZZ0000001"), ("passport", "ZZ0000005"))), dry_run=False
    )
    assert (out["outcome"], out["reason"]) == ("conflict", "two_owners")
    assert await snapshot(conn) == before


@pytest.mark.asyncio
async def test_c4_equal_set_with_a_different_birth_date_is_a_contradiction(conn):
    await _seed(conn, dob=date(1990, 1, 2))
    before = await snapshot(conn)
    out = await register_subject(conn, subj(ids=(("nik", "3201234567890123"), ("passport", "ZZ0000001")), dob=date(1991, 1, 2)), dry_run=False)  # fmt: skip
    assert (out["outcome"], out["reason"]) == ("conflict", "contradiction")
    assert await snapshot(conn) == before


@pytest.mark.asyncio
async def test_c4_equal_set_match_basis_follows_the_birth_dates(conn):
    ids = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    ids2 = (("passport", "ZZ0000009"),)
    await _seed(conn, ids=ids, dob=date(1990, 1, 2))
    await _seed(conn, ids=ids2)
    before = await snapshot(conn)
    await clear_writes(conn)
    both = await register_subject(conn, subj(ids=ids, dob=date(1990, 1, 2)), dry_run=False)
    request_without = await register_subject(conn, subj(ids=ids), dry_run=False)
    owner_without = await register_subject(
        conn, subj(ids=ids2, dob=date(1990, 1, 2)), dry_run=False
    )
    assert (both["outcome"], both["match_basis"]) == ("already_registered", "identifiers_and_dob")
    assert (request_without["outcome"], request_without["match_basis"]) == (
        "already_registered",
        "identifiers_only",
    )
    assert (owner_without["outcome"], owner_without["match_basis"]) == (
        "already_registered",
        "identifiers_only",
    )
    assert await snapshot(conn) == before
    assert await written(conn) == set()


@pytest.mark.asyncio
async def test_c4_kind_change_keeping_the_identifiers_is_422_and_writes_nothing(
    pool, conn, enabled
):
    stack = Stack(pool, middleware=False)
    await _seed(conn)
    before = await snapshot(conn)
    resp = await post(stack, body(kind="company", ids=(("passport", "ZZ0000001"),)))
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["code"] == "kind_type_mismatch"
    assert await snapshot(conn) == before


@pytest.mark.asyncio
async def test_c4_creation_stores_actor_canonical_keys_and_norm_version(conn):
    out = await register_subject(
        conn,
        subj(
            ids=(("passport", "zz 000-0001"), ("nik", "3201 2345 6789 0123")),
            name="Zz Probe Subject",
        ),
        dry_run=False,  # fmt: skip
    )
    sid = out["subject_id"]
    row = await conn.fetchrow("SELECT * FROM related_subjects WHERE id = $1", sid)
    assert row["created_by"] == REGISTRY_ACTOR == "system:related-subject-registry"
    assert row["merged_into"] is None and row["kind"] == "person"
    assert json.loads(row["evidence"]) == {"plan_sha256": SHA, "subject_ref": "s-1", "tier": "T1"}
    keys = await conn.fetch(
        "SELECT id_type, id_norm, id_raw, norm_version FROM related_subject_identifiers "
        "WHERE subject_id = $1 ORDER BY id_type",
        sid,
    )
    assert [(k["id_type"], k["id_norm"], k["norm_version"]) for k in keys] == [
        ("nik", "3201234567890123", NORM_VERSION),
        ("passport", "ZZ0000001", NORM_VERSION),
    ]
    assert {k["id_raw"] for k in keys} == {"zz 000-0001", "3201 2345 6789 0123"}


# ── criterion 5: concurrency ─────────────────────────────────────────────────


class _Conn:
    """Connection proxy that can wait on a barrier or fail before a given statement."""

    def __init__(self, real, hook=None):
        self._real = real
        self._hook = hook

    def transaction(self, *a, **kw):
        return self._real.transaction(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def _run(self, method, sql, args):
        if self._hook is not None:
            await self._hook(sql)
        return await getattr(self._real, method)(sql, *args)

    async def fetch(self, sql, *args):
        return await self._run("fetch", sql, args)

    async def fetchrow(self, sql, *args):
        return await self._run("fetchrow", sql, args)

    async def fetchval(self, sql, *args):
        return await self._run("fetchval", sql, args)

    async def execute(self, sql, *args):
        return await self._run("execute", sql, args)


def barrier_hook(barrier):
    state = {"waited": False}

    async def hook(sql):
        if sql.startswith("INSERT INTO related_subjects ") and not state["waited"]:
            state["waited"] = True
            await asyncio.wait_for(barrier.wait(), timeout=20)

    return hook


async def _race(pool, subjects):
    barrier = asyncio.Barrier(len(subjects))

    async def one(s):
        async with pool.acquire() as c:
            return await register_subject(_Conn(c, barrier_hook(barrier)), s, dry_run=False)

    return await asyncio.gather(*(one(s) for s in subjects))


async def _assert_no_subject_without_identifiers(conn):
    orphans = await conn.fetchval(
        "SELECT count(*) FROM related_subjects s WHERE NOT EXISTS "
        "(SELECT 1 FROM related_subject_identifiers i WHERE i.subject_id = s.id)"
    )
    assert orphans == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("callers", [2, 3])
async def test_c5_identical_requests_make_exactly_one_subject(pool, conn, callers):
    ids = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    results = await _race(pool, [subj(ids=ids, name="Zz Probe Subject") for _ in range(callers)])
    outcomes = sorted(r["outcome"] for r in results)
    assert outcomes == ["already_registered"] * (callers - 1) + ["created"]
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1
    assert await conn.fetchval("SELECT count(*) FROM related_subject_identifiers") == 2
    await _assert_no_subject_without_identifiers(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("callers", [2, 3])
async def test_c5_shared_key_with_distinct_extra_keys_gives_one_created_rest_conflict(
    pool, conn, callers
):
    subjects = [
        subj(ids=(("passport", "ZZ0000001"), ("nik", f"320123456789012{n}")))
        for n in range(callers)
    ]
    results = await _race(pool, subjects)
    assert sorted(r["outcome"] for r in results) == ["conflict"] * (callers - 1) + ["created"]
    assert all(r["reason"] == "partial_overlap" for r in results if r["outcome"] == "conflict")
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1
    assert await conn.fetchval("SELECT count(*) FROM related_subject_identifiers") == 2
    await _assert_no_subject_without_identifiers(conn)


@pytest.mark.asyncio
async def test_c5_identifiers_are_inserted_in_sorted_key_order(conn):
    seen = []

    class _Spy(_Conn):
        async def fetchval(self, sql, *args):
            if sql.startswith("INSERT INTO related_subject_identifiers"):
                seen.append((args[0], args[1]))
            return await super().fetchval(sql, *args)

    ids = (("passport", "ZZ0000009"), ("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    await register_subject(_Spy(conn), subj(ids=ids), dry_run=False)
    assert seen == [
        ("nik", "3201234567890123"),
        ("passport", "ZZ0000001"),
        ("passport", "ZZ0000009"),
    ]


def deadlock_hook(times):
    state = {"left": times}

    async def hook(sql):
        if sql.startswith("INSERT INTO related_subject_identifiers") and state["left"] > 0:
            state["left"] -= 1
            raise asyncpg.DeadlockDetectedError("deadlock detected")

    return hook


@pytest.mark.asyncio
async def test_c5_one_forced_deadlock_is_retried_to_the_right_outcome(conn):
    ids = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    out = await register_subject(_Conn(conn, deadlock_hook(1)), subj(ids=ids), dry_run=False)
    assert out["outcome"] == "created"
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1
    assert await conn.fetchval("SELECT count(*) FROM related_subject_identifiers") == 2


@pytest.mark.asyncio
async def test_c5_a_second_forced_deadlock_is_the_fixed_503_and_leaves_nothing(pool, conn, enabled):
    with pytest.raises(RegistryUnavailable) as caught:
        await register_subject(_Conn(conn, deadlock_hook(2)), subj(), dry_run=False)
    assert caught.value.code == "registry_busy"
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 0

    class _Pool:
        def acquire(self_inner):
            class _Ctx:
                async def __aenter__(_s):
                    _s.c = await pool.acquire()
                    return _Conn(_s.c, deadlock_hook(2))

                async def __aexit__(_s, *exc):
                    await pool.release(_s.c)
                    return False

            return _Ctx()

    resp = await post(Stack(_Pool(), middleware=False), body())
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_busy"})
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 0


@pytest.mark.asyncio
async def test_c5_a_lost_race_that_loses_again_is_the_fixed_503(conn):
    state = {"n": 0}

    async def hook(sql):
        if sql.startswith("INSERT INTO related_subject_identifiers"):
            state["n"] += 1
            if state["n"] <= 2:
                raise_race(conn)

    def raise_race(_c):
        from backend.services.crm import related_subjects as mod

        raise mod._LostRace

    with pytest.raises(RegistryUnavailable):
        await register_subject(_Conn(conn, hook), subj(), dry_run=False)
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 0


# ── criterion 6: CRM matches ─────────────────────────────────────────────────


async def _crm_fixtures(conn):
    await conn.execute(
        "INSERT INTO clients (full_name, passport_number, deleted_at) VALUES "
        "('Fixture live', 'ab-123 456', NULL), ('Fixture gone', 'CD 000111', now()), "
        "('Fixture blank', NULL, NULL)"
    )
    await conn.execute(
        "INSERT INTO client_family_members (client_id, full_name, passport_number) "
        "VALUES (1, 'Fixture family', 'ef/777888')"
    )
    await conn.execute(
        "INSERT INTO companies (company_name, npwp_company, nib) VALUES "
        "('Fixture Co A', '12.345.678.9-012.345', NULL), "
        "('Fixture Co B', '0987654321098765', NULL), "
        "('Fixture Co C', '123456789012', '1234-5678-90123'), "
        "('Fixture Co D', '11223344556677', NULL)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "ids", "outcome"),
    [
        ("person", (("passport", "AB123456"),), "existing_client"),
        ("person", (("passport", "ab 123-456"),), "existing_client"),
        ("person", (("passport", "EF777888"),), "existing_family_member"),
        ("company", (("npwp", "0123456789012345"),), "existing_company"),
        ("company", (("npwp", "123456789012345"),), "existing_company"),
        ("company", (("npwp", "0987654321098765"),), "existing_company"),
        ("company", (("npwp", "987654321098765"),), "existing_company"),
        ("company", (("nib", "1234567890123"),), "existing_company"),
        ("company", (("nib", "1234 5678 90123"),), "existing_company"),
        ("company", (("npwp", "5555555555555555"), ("nib", "1234567890123")), "existing_company"),
        ("person", (("passport", "CD000111"),), "created"),
        ("person", (("passport", "ZZ0000001"),), "created"),
        ("company", (("npwp", "5555555555555555"),), "created"),
        ("company", (("npwp", "0011223344556677"),), "created"),
    ],
)
async def test_c6_crm_matches_in_every_stored_spelling(conn, kind, ids, outcome):
    await _crm_fixtures(conn)
    before = await snapshot(conn)
    out = await register_subject(conn, subj(kind=kind, ids=ids), dry_run=False)
    assert out["outcome"] == outcome
    if outcome != "created":
        assert await snapshot(conn) == before


@pytest.mark.asyncio
async def test_c6_python_and_sql_canonical_forms_agree_on_the_vectors(conn):
    vectors = {
        "passport": ["ab 123-456", "ZZ0000001", "ß1234", "éx12", "x-1.2/3", "ＡＢ123456", ""],
        "nib": ["1234-5678-90123", "٣٢1234567890123", "abc", "", "12 34"],
        "npwp": [
            "12.345.678.9-012.345", "0123456789012345", "12345678901234", "１" * 15,
            "1234567890123456789", "", "9" * 15, "9" * 16,
        ],
    }  # fmt: skip
    for id_type, values in vectors.items():
        for value in values:
            sql_value = await conn.fetchval(f"SELECT {sql_canonical(id_type, '$1::text')}", value)
            assert sql_value == canonical_form(id_type, value), (id_type, len(value))


# ── criterion 7: validation ──────────────────────────────────────────────────

INVALID_BODIES = {
    "kind_type_mismatch": body(kind="company", ids=(("passport", SENTINEL_ID),), display_name=SENTINEL_NAME),
    "person_npwp": body(kind="person", ids=(("npwp", "0123456789012345"), ("passport", SENTINEL_ID)), display_name=SENTINEL_NAME),
    "failing_validator": body(ids=(("passport", "ZZ12"),), display_name=SENTINEL_NAME),
    "yearless_ahu": body(kind="company", ids=(("ahu", "AHU-1234.AH.01.01"),), display_name=SENTINEL_NAME),
    "duplicate_key": body(ids=(("passport", SENTINEL_ID), ("passport", "zzsent-0001")), display_name=SENTINEL_NAME),
    "nested_invalid": {
        **body(ids=(("passport", SENTINEL_ID),), display_name=SENTINEL_NAME),
        "identifiers": [
            {"type": "passport", "value": SENTINEL_ID, "evidence": {"files": SENTINEL_NAME}}
        ],
    },
    "nested_unknown_key": {
        **body(ids=(("passport", SENTINEL_ID),), display_name=SENTINEL_NAME),
        "identifiers": [{"type": "passport", "value": SENTINEL_ID, SENTINEL_NAME: 1}],
    },
    "unknown_top_key": {**body(ids=(("passport", SENTINEL_ID),)), SENTINEL_NAME: SENTINEL_ID},
    "bad_date": body(ids=(("passport", SENTINEL_ID),), date_of_birth=SENTINEL_NAME),
    "not_an_object": [SENTINEL_NAME, SENTINEL_ID],
    "malformed_json": '{"kind": "person", "display_name": "' + SENTINEL_NAME + '", "identifiers": [',
}  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(INVALID_BODIES))
async def test_c7_invalid_bodies_are_422_write_nothing_and_echo_nothing(pool, conn, enabled, case):
    stack = Stack(pool, middleware=False)
    before = await snapshot(conn)
    resp = await post(stack, INVALID_BODIES[case])
    assert resp.status_code == 422, resp.text
    assert SENTINEL_NAME not in resp.text and SENTINEL_ID not in resp.text
    assert "zzsent" not in resp.text.lower() and "Sentinel" not in resp.text
    detail = resp.json()["detail"]
    assert detail and all(set(item) == {"loc", "code"} for item in detail)
    assert await snapshot(conn) == before


@pytest.mark.asyncio
async def test_c7_codes_name_the_failure(pool, enabled):
    stack = Stack(pool, middleware=False)
    codes = {}
    for case in ("kind_type_mismatch", "failing_validator", "duplicate_key", "malformed_json"):
        codes[case] = (await post(stack, INVALID_BODIES[case])).json()["detail"][0]["code"]
    assert codes == {
        "kind_type_mismatch": "kind_type_mismatch",
        "failing_validator": "invalid_identifier",
        "duplicate_key": "duplicate_identifier",
        "malformed_json": "json_invalid",
    }
    odd = (await post(stack, INVALID_BODIES["nested_unknown_key"])).json()["detail"][0]
    assert odd["loc"][-1] == "_" and odd["code"] == "extra_forbidden"


# ── criterion 8: auth and dry run ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c8_flag_off_is_503_even_with_a_valid_key(pool, monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_enabled", True, raising=False)
    resp = await post(Stack(pool, middleware=False), body())
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_unavailable"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Type": "application/json"},
        {"Content-Type": "application/json", "X-CRM-Write-Key": "nope"},
        {"Content-Type": "application/json", "X-CRM-Write-Key": ""},
        {"Content-Type": "application/json", "Authorization": "Bearer portal-token-probe"},
    ],
)
async def test_c8_wrong_or_missing_key_is_401(pool, enabled, headers):
    resp = await post(Stack(pool, middleware=False), body(), headers)
    assert (resp.status_code, resp.json()) == (401, {"detail": "unauthorized"})


@pytest.mark.asyncio
async def test_c8_key_comparison_is_constant_time(pool, enabled, monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    resp = await post(Stack(pool, middleware=False), body(dry_run=True))
    assert resp.status_code == 200 and calls == [1]


@pytest.mark.asyncio
async def test_c8_unconfigured_key_is_401_not_open(pool, monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", None, raising=False)
    resp = await post(Stack(pool, middleware=False), body(), {"X-CRM-Write-Key": ""})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_c8_dry_run_absent_and_true_write_nothing_on_every_outcome(pool, conn, enabled):
    stack = Stack(pool, middleware=False)
    await _crm_fixtures(conn)
    owner = ("nik", "3201234567890123"), ("passport", "ZZ0000001")

    def variants(payload):
        absent = {k: v for k, v in payload.items() if k != "dry_run"}
        return [absent, {**payload, "dry_run": True}]

    empty = await snapshot(conn)
    await clear_writes(conn)
    for payload in variants(body(ids=owner)):  # empty registry
        resp = await post(stack, payload)
        assert resp.json() == {"outcome": "would_create", "dry_run": True}
    assert await snapshot(conn) == empty
    assert await written(conn) == set()

    await _seed(conn)
    seeded = await snapshot(conn)
    await clear_writes(conn)
    cases = {
        "already_registered": body(ids=owner),
        "conflict": body(ids=(("passport", "ZZ0000001"),)),
        "existing_client": body(ids=(("passport", "AB123456"),)),
        "existing_family_member": body(ids=(("passport", "EF777888"),)),
        "existing_company": body(kind="company", ids=(("npwp", "0123456789012345"),)),
        "would_create": body(ids=(("passport", "ZZ0000077"),)),
    }
    for outcome, payload in cases.items():
        for variant in variants(payload):
            resp = await post(stack, variant)
            assert resp.json()["outcome"] == outcome and resp.json()["dry_run"] is True
    assert await snapshot(conn) == seeded
    assert await written(conn) == set()


# ── criterion 9: privacy ─────────────────────────────────────────────────────


class _Boom:
    """Pool whose connections fail like a database that echoes the statement values."""

    def __init__(self, exc):
        self.exc = exc

    def acquire(self):
        exc = self.exc

        class _Ctx:
            async def __aenter__(_s):
                class _C:
                    def transaction(self):
                        raise exc

                return _C()

            async def __aexit__(_s, *a):
                return False

        return _Ctx()


_STANDARD_RECORD_FIELDS = frozenset(
    vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", (), None))
) | {"message", "asctime", "taskName"}


def _everything_observable(stack, caplog, trace_before):
    traces = {k: v for k, v in request_tracing._trace_storage.items() if k not in trace_before}
    logs = " ".join(
        r.getMessage()
        + " "
        + str(r.args)
        + " "
        + (str(r.exc_info) if r.exc_info else "")
        + " "
        + (r.stack_info or "")
        + " "
        + str({k: v for k, v in vars(r).items() if k not in _STANDARD_RECORD_FIELDS})
        for r in caplog.records
    )
    return logs + " " + str(traces) + " " + str(stack.alerts.calls)


def _leaks(text):
    return [
        s for s in (SENTINEL_NAME, SENTINEL_ID, "zzsent", "Sentinel") if s.lower() in text.lower()
    ]


async def _privacy_run(pool, conn, caplog, setup, payload, *, expect_status):
    caplog.set_level(logging.DEBUG)
    stack = Stack(pool)
    setup(stack)
    trace_before = set(request_tracing._trace_storage)
    resp = await post(stack, payload)
    assert resp.status_code == expect_status, resp.text
    audit = str(await conn.fetch("SELECT * FROM api_audit_trail"))
    observed = resp.text + " " + audit + " " + _everything_observable(stack, caplog, trace_before)
    assert _leaks(observed) == [], _leaks(observed)
    assert stack.sink["raised"] == [] and stack.sink["handlers"] == []
    return resp, stack


SECRET_BODY = body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID),))


@pytest.mark.asyncio
async def test_c9_privacy_on_success_and_conflict(pool, conn, enabled, audit_logger, caplog):
    await _privacy_run(pool, conn, caplog, lambda s: None, SECRET_BODY, expect_status=200)
    resp, _ = await _privacy_run(
        pool, conn, caplog, lambda s: None,
        body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID), ("passport", "ZZ0000002"))),
        expect_status=200,
    )  # fmt: skip
    assert resp.json()["outcome"] == "conflict"


@pytest.mark.asyncio
async def test_c9_privacy_on_422(pool, conn, enabled, audit_logger, caplog):
    for case in ("kind_type_mismatch", "malformed_json", "nested_unknown_key", "bad_date"):
        await _privacy_run(
            pool, conn, caplog, lambda s: None, INVALID_BODIES[case], expect_status=422
        )


@pytest.mark.asyncio
async def test_c9_privacy_on_a_forced_database_error(pool, conn, enabled, audit_logger, caplog):
    leaky = asyncpg.UniqueViolationError(
        f"duplicate key value violates unique constraint: Key (id_norm)=({SENTINEL_ID}) {SENTINEL_NAME}"
    )

    def setup(stack):
        stack.app.state.db_pool = _Boom(leaky)

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=503)
    assert resp.json() == {"detail": "database_error"}


@pytest.mark.asyncio
async def test_c9_privacy_on_an_unexpected_service_failure(
    pool, conn, enabled, audit_logger, caplog
):
    resp, stack = await _privacy_run(
        pool, conn, caplog,
        lambda s: setattr(s.app.state, "db_pool", _Boom(RuntimeError(SENTINEL_NAME + SENTINEL_ID))),
        SECRET_BODY, expect_status=500,
    )  # fmt: skip
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_c9_privacy_on_injected_body_parsing_failure(
    pool, conn, enabled, audit_logger, caplog, monkeypatch
):
    def boom(*a, **kw):
        raise RuntimeError(f"{SENTINEL_NAME} {SENTINEL_ID}")

    monkeypatch.setattr(route_module.RelatedSubjectRegisterRequest, "model_validate_json", boom)
    resp, _ = await _privacy_run(pool, conn, caplog, lambda s: None, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_c9_privacy_on_injected_auth_dependency_failure(
    pool, conn, enabled, audit_logger, caplog
):
    async def broken():
        raise RuntimeError(f"{SENTINEL_NAME} {SENTINEL_ID}")

    def setup(stack):
        stack.app.dependency_overrides[route_module.verify_related_subject_write] = broken

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http_503_with_detail", "runtime_error"])
async def test_c9_privacy_on_injected_pool_dependency_failure(
    pool, conn, enabled, audit_logger, caplog, kind
):
    def broken():
        if kind == "runtime_error":
            raise RuntimeError(f"{SENTINEL_NAME} {SENTINEL_ID}")
        raise HTTPException(
            status_code=503,
            detail=f"The database connection pool is not initialized. Initialization error: {SENTINEL_NAME} {SENTINEL_ID}",
            headers={"X-Error-Type": SENTINEL_ID},
        )

    def setup(stack):
        stack.app.dependency_overrides[get_database_pool] = broken

    resp, _ = await _privacy_run(
        pool, conn, caplog, setup, SECRET_BODY,
        expect_status=500 if kind == "runtime_error" else 503,
    )  # fmt: skip
    assert SENTINEL_ID not in str(resp.headers)


@pytest.mark.asyncio
async def test_c9_oversized_body_is_refused_with_a_fixed_code(pool, enabled):
    resp = await post(Stack(pool, middleware=False), "x" * (route_module.MAX_BODY_BYTES + 1))
    assert resp.status_code == 422 and resp.json()["detail"][0]["code"] == "too_long"


# ── criterion 10: portal ─────────────────────────────────────────────────────


def test_c10_router_is_not_a_portal_route_and_portal_roles_are_refused():
    paths = {r.path for r in route_module.router.routes}
    assert paths == {URL}
    assert not any(p.startswith("/api/portal") for p in paths)
    for role in ("client", "partner"):
        assert role_boundary_refuses("POST", URL, role) is True


def test_c10_no_portal_router_module_includes_the_registry():
    routers = MIGRATIONS.parents[1] / "app" / "routers"
    for source in routers.glob("portal*.py"):
        assert "crm_related_subjects" not in source.read_text()


# ── criterion 11: migration ──────────────────────────────────────────────────


def test_c11_migration_is_361_alone_and_rollback_drops_both_tables():
    numbered = sorted(p.name for p in MIGRATIONS.glob("361_*.sql"))
    assert numbered == ["361_related_subjects_registry.sql"]
    _, rollback = split_migration_sql(MIGRATION.read_text())
    assert rollback is not None
    assert "DROP TABLE IF EXISTS related_subject_identifiers" in rollback
    assert "DROP TABLE IF EXISTS related_subjects" in rollback


@pytest.mark.asyncio
async def test_c11_database_refuses_short_duplicate_and_self_merged_rows(conn):
    sid = await conn.fetchval(
        "INSERT INTO related_subjects (kind, evidence, created_by) VALUES ('person', '{}', 'x') RETURNING id"
    )
    insert = (
        "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, subject_id) "
        "VALUES ($1, $2, $2, 1, $3)"
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(insert, "passport", "ABC", sid)
    await conn.execute(insert, "passport", "ABCD1234", sid)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(insert, "passport", "ABCD1234", sid)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(insert, "visa", "ABCD1234", sid)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("UPDATE related_subjects SET merged_into = id WHERE id = $1", sid)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO related_subjects (kind, evidence, created_by) VALUES ('robot', '{}', 'x')"
        )
    await conn.execute("DELETE FROM related_subjects WHERE id = $1", sid)
    assert await conn.fetchval("SELECT count(*) FROM related_subject_identifiers") == 0


@pytest.mark.asyncio
async def test_c11_rollback_section_drops_both_tables(pool):
    _, rollback = split_migration_sql(MIGRATION.read_text())
    async with pool.acquire() as c:
        await c.execute(rollback)
        left = await c.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
            sorted(NEW_TABLES),
        )
    assert left == 0
