"""Related-subjects registry, slice 4B: proposed client links, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362, 365 and 367 forward
parts, a minimal ``clients`` table and the audit table), seeds invented rows (``Zz`` / ``ZZ``
conventions, client ids from 424242 up) and calls the real routers behind the real middleware
stack. Nothing here is a real person, company, client or document.

The ids L1 to L16 are those of the specification. Write measurement: during the execution of the
links route the only writes are INSERTs into ``related_subject_links`` and the audit table; during
the read routes there is no write at all. Seeding, the migration, its rollback and the deliberate
cascade delete of L1 run inside ``unmeasured``. Every catalog read is scoped to
``current_schema()``.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import backend.app.routers.crm_related_registry_read as read_module
import backend.app.routers.crm_related_subjects as route_module
import backend.services.crm.related_links as links_module
from backend.app.auth.public_endpoints import PUBLIC_ENDPOINTS
from backend.app.core.config import settings
from backend.app.setup.router_manifest import ROUTER_MANIFEST
from backend.db.migration_base import split_migration_sql
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm import related_registry_read
from backend.services.crm.related_identifiers import canonical_form
from backend.services.crm.related_links import LinkEntry, LinksInput, record_links
from backend.services.crm.related_subjects import REGISTRY_ACTOR, prepare_identifiers
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

BACKEND = Path(route_module.__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "db" / "migrations_v2"
MIGRATION_LINKS = MIGRATIONS / "367_related_subject_links.sql"
URL = "/api/crm/related-subjects/links/register"
URL_SUBJECT = "/api/crm/related-subjects/register"
URL_DOCUMENTS = "/api/crm/related-subjects/documents/register"
URL_NAMES = "/api/crm/related-subjects/names/register"
READ = "/api/crm/related-registry"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
PLAN = "ab12" * 16

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}

LINKS_INSERT = ("related_subject_links", "INSERT")
AUDIT_INSERT = ("api_audit_trail", "INSERT")

# The probe world of the clients: a live client, a second live client, a soft-deleted client, and
# ids no client has. The names all carry the probe word, so a leak of STORED data is caught.
LIVE1, LIVE2, LIVE3, GONE = 424242, 424243, 424245, 424244
MISSING1, MISSING2 = 424299, 424298
PROBE_CLIENT_WORD = "ZzProbeClientName"
NAME_LIVE1, NAME_LIVE2 = PROBE_CLIENT_WORD, PROBE_CLIENT_WORD + "Two"
NAME_LIVE3, NAME_GONE = PROBE_CLIENT_WORD + "Three", PROBE_CLIENT_WORD + "Gone"

AUDIT_SCHEMA = """
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

# The minimal shape of `clients` (the synthetic gate schema's), created BEFORE the write-log
# triggers so that a write to it is measured. `client_family_members` exists only so that a write
# to the table the client portal displays would SUCCEED here and be seen by the measurement.
CLIENTS_SCHEMA = """
CREATE TABLE clients (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL,
    deleted_at timestamptz
);
CREATE TABLE client_family_members (
    id serial PRIMARY KEY,
    client_id integer NOT NULL,
    full_name text NOT NULL,
    relationship text NOT NULL DEFAULT 'zz'
);
"""

# Every row write to any table of the schema is recorded; the teardown of the `pool` fixture
# accepts nothing but the audit-trail INSERT and the writes a test declares through `writes`.
WRITE_LOG_SQL = """
CREATE TABLE write_log (id bigserial PRIMARY KEY, tbl text NOT NULL, op text NOT NULL);
CREATE FUNCTION log_write() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    INSERT INTO write_log (tbl, op) VALUES (TG_TABLE_NAME, TG_OP);
    RETURN NULL;
END
$f$;
CREATE FUNCTION log_truncate() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    INSERT INTO write_log (tbl, op) VALUES (TG_TABLE_NAME, 'TRUNCATE');
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
        EXECUTE format(
            'CREATE TRIGGER log_truncate BEFORE TRUNCATE ON %I '
            'FOR EACH STATEMENT EXECUTE FUNCTION log_truncate()', t);
    END LOOP;
END
$do$;
"""


# ── database fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def writes():
    """The writes a test declares on top of the audit-trail INSERT (a set of (table, op))."""
    return set()


@pytest_asyncio.fixture
async def pool(writes):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL required for the real PostgreSQL registry proof")
    admin = await asyncpg.connect(dsn)
    name = await admin.fetchval("SELECT current_database()")
    if not re.search(r"_test(?:_run_[0-9]+_[0-9]+)?(?:_gw[0-9]+)?$", name):
        await admin.close()
        pytest.fail("A test database or isolated test worker is required")
    schema = "related_links_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    # Production-shaped: the same `init` hook every production pool registers (the jsonb codec),
    # `statement_cache_size=0` and `max_inactive_connection_lifetime=30.0`, plus the API pool's
    # `command_timeout=60` (backend/app/setup/service_initializer.py, initialize_services_light).
    # Not reproduced: pool sizes, TLS. The pool is built here, from the factory's own init hook,
    # because the factory takes no `server_settings` and this module needs its per-test schema.
    created = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=6,
        server_settings={"search_path": schema},
        init=init_prod_shaped_connection,
        statement_cache_size=0,
        max_inactive_connection_lifetime=30.0,
        command_timeout=60,
    )
    try:
        async with created.acquire() as c:
            for migration in (
                "361_related_subjects_registry.sql",
                "362_related_documents.sql",
                "365_related_subject_names.sql",
                "367_related_subject_links.sql",
            ):
                forward, _ = split_migration_sql((MIGRATIONS / migration).read_text())
                await c.execute(forward)
            await c.execute(AUDIT_SCHEMA)
            await c.execute(CLIENTS_SCHEMA)
            await c.execute(WRITE_LOG_SQL)
        yield created
        async with created.acquire() as c:
            rows = await c.fetch("SELECT DISTINCT tbl, op FROM write_log")
        observed = {(r["tbl"], r["op"]) for r in rows}
        assert observed <= {AUDIT_INSERT} | writes, "undeclared write: " + str(sorted(observed))
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest_asyncio.fixture
async def conn(pool):
    async with pool.acquire() as c:
        yield c


@asynccontextmanager
async def unmeasured(c):
    """Seeding and cleanup inside the block are taken out of the write measurement."""
    mark = await c.fetchval("SELECT coalesce(max(id), 0) FROM write_log")
    yield
    await c.execute("DELETE FROM write_log WHERE id > $1", mark)


async def seed_subject(c, kind, name, ids, *, merged=None):
    async with unmeasured(c):
        sid = await c.fetchval(
            "INSERT INTO related_subjects (kind, display_name, merged_into, evidence, created_by) "
            "VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed') RETURNING id",
            kind, name, merged,
        )  # fmt: skip
        for id_type, raw in ids:
            await c.execute(
                "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, "
                "norm_version, subject_id) VALUES ($1, $2, $3, 1, $4)",
                id_type, canonical_form(id_type, raw), raw, sid,
            )  # fmt: skip
    return sid


async def seed_client(c, client_id, name, *, deleted=False):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO clients (id, full_name, deleted_at) OVERRIDING SYSTEM VALUE "
            "VALUES ($1, $2, CASE WHEN $3 THEN now() END)",
            client_id, name, deleted,
        )  # fmt: skip


async def seed_link(c, sid, client_id, basis="drive_folder", *, at=None, relation="unknown"):
    async with unmeasured(c):
        return await c.fetchval(
            "INSERT INTO related_subject_links (subject_id, client_id, basis, relation, evidence, "
            "created_by, created_at) VALUES ($1, $2, $3, $4, '{}'::jsonb, 'zz-seed', "
            "coalesce($5, now())) RETURNING id",
            sid, client_id, basis, relation, at,
        )  # fmt: skip


async def link_rows(c, sid=None):
    where = "" if sid is None else " WHERE subject_id = " + str(int(sid))
    return await c.fetch(
        "SELECT id, subject_id, client_id, basis, relation, evidence, created_by "
        "FROM related_subject_links" + where + " ORDER BY id"
    )


@pytest_asyncio.fixture
async def clients(conn):
    await seed_client(conn, LIVE1, NAME_LIVE1)
    await seed_client(conn, LIVE2, NAME_LIVE2)
    await seed_client(conn, LIVE3, NAME_LIVE3)
    await seed_client(conn, GONE, NAME_GONE, deleted=True)


# ── request builders ──────────────────────────────────────────────────────────

P1, P2, P3, P4 = "ZZ0000001", "ZZ0000002", "ZZ0000003", "ZZ0000004"
NIK1 = "3201 0000 0000 0001"
UNOWNED = "ZZ9999991"


def entry(client_id, basis="drive_folder", files=3):
    return {
        "client_id": client_id,
        "basis": basis,
        "evidence": {"plan_sha256": PLAN, "files": files},
    }


def payload(links, ids=(("passport", P1),), kind="person", **over):
    data = {
        "subject": {"kind": kind, "identifiers": [{"type": t, "value": v} for t, v in ids]},
        "links": list(links),
        "dry_run": False,
    }
    data.update(over)
    return data


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True, raising=False)


def as_identity(monkeypatch, identity):
    async def fake(self, request):
        return dict(identity) if identity else None

    monkeypatch.setattr(HybridAuthMiddleware, "authenticate_request", fake)


class _Alerts:
    async def send_http_error_alert(self, **kwargs):
        return None

    async def send_latency_alert(self, **kwargs):
        return None


def make_client(pool) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.db_pool = pool
    app.include_router(route_module.router)
    app.include_router(read_module.router)
    app.add_middleware(HybridAuthMiddleware)
    app.add_middleware(RequestTracingMiddleware)
    app.add_middleware(ErrorMonitoringMiddleware, alert_service=_Alerts())
    app.add_middleware(ActivityLoggingMiddleware)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://test",
    )


@pytest.fixture
def audit_logger(pool):
    previous = (activity_logger.pool, activity_logger._initialized)
    activity_logger.pool, activity_logger._initialized = pool, True
    yield activity_logger
    activity_logger.pool, activity_logger._initialized = previous


@pytest_asyncio.fixture
async def client(pool, enabled, auth_on, audit_logger, monkeypatch, writes):
    as_identity(monkeypatch, None)
    async with make_client(pool) as c:
        yield c


@pytest_asyncio.fixture
async def admin(pool, auth_on, audit_logger, monkeypatch):
    as_identity(monkeypatch, ADMIN)
    async with make_client(pool) as c:
        yield c


async def post(client, data, *, headers=HEADERS, url=URL):
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return await client.post(url, content=raw, headers=headers)


def errors_of(resp):
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def loc_codes(resp):
    return [(e["loc"], e["code"]) for e in errors_of(resp)]


class _SpyConn:
    """Delegates to the real connection the route acquired and records, in order, the SQL text of
    every call. It is the independent proof of statement order: an INSERT attempted and then
    rolled back leaves neither a link nor a write-log row."""

    def __init__(self, conn, spy):
        self._conn, self._spy = conn, spy

    def __getattr__(self, attr):
        target = getattr(self._conn, attr)
        if attr in {"fetch", "fetchrow", "fetchval", "execute"}:

            async def recorded(sql, *args, **kwargs):
                self._spy.calls.append(sql)
                out = await target(sql, *args, **kwargs)
                if sql == links_module._CLIENTS_SQL and self._spy.after_clients is not None:
                    await self._spy.after_clients()
                return out

            return recorded
        return target


class _SpyPool:
    """The pool the app holds for one test: counts the acquisitions, wraps each connection."""

    def __init__(self, pool, after_clients=None):
        self._pool, self.acquired, self.calls = pool, 0, []
        self.after_clients = after_clients

    @asynccontextmanager
    async def _connection(self):
        async with self._pool.acquire() as c:
            yield _SpyConn(c, self)

    def acquire(self):
        self.acquired += 1
        return self._connection()

    def inserts(self):
        return [s for s in self.calls if s.lstrip().upper().startswith("INSERT")]


# ── L1: the migration ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l1_migration_table_constraints_cascade_and_rollback(pool, conn):
    sid = await seed_subject(conn, "person", "ZzL1 Person", [("passport", P1)])
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'related_subject_links'"
        )
    }
    assert cols == {
        "id": ("bigint", "NO"),
        "subject_id": ("bigint", "NO"),
        "client_id": ("integer", "NO"),
        "basis": ("text", "NO"),
        "relation": ("text", "NO"),
        "evidence": ("jsonb", "NO"),
        "created_by": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    }
    identity = await conn.fetchrow(
        "SELECT is_identity, identity_generation FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'related_subject_links' "
        "AND column_name = 'id'"
    )
    assert (identity["is_identity"], identity["identity_generation"]) == ("YES", "ALWAYS")
    indexes = [
        r["indexdef"]
        for r in await conn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = 'related_subject_links'"
        )
    ]
    assert any(d.endswith("(subject_id)") for d in indexes), indexes
    assert any(d.endswith("(client_id)") for d in indexes), indexes

    # the only foreign key of the table points at the registry; none in the schema mentions clients
    fks = await conn.fetch(
        "SELECT t.relname AS tbl, r.relname AS target FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_class r ON r.oid = c.confrelid "
        "WHERE c.contype = 'f' AND n.nspname = current_schema() "
        "AND t.relname = 'related_subject_links'"
    )
    assert [(r["tbl"], r["target"]) for r in fks] == [("related_subject_links", "related_subjects")]
    to_clients = await conn.fetchval(
        "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_class r ON r.oid = c.confrelid "
        "WHERE c.contype = 'f' AND n.nspname = current_schema() AND r.relname = 'clients'"
    )
    assert to_clients == 0

    insert = (
        "INSERT INTO related_subject_links (subject_id, client_id, basis, relation, evidence, "
        "created_by) VALUES ($1, $2, $3, $4, $5::jsonb, 'zz')"
    )
    async with unmeasured(conn):
        await conn.execute(insert, sid, LIVE1, "drive_folder", "unknown", {})
        # the database accepts what a later slice will write: `manual` and every relation
        await conn.execute(insert, sid, LIVE1, "manual", "unknown", {})
        relations = (
            "unknown self spouse child parent sibling other_family director commissioner "
            "shareholder employee sponsor"
        ).split()
        for i, relation in enumerate(relations):
            await conn.execute(insert, sid, 424400 + i, "identity", relation, {})
        refused = [
            (asyncpg.CheckViolationError, (sid, LIVE2, "guessed", "unknown", {})),
            (asyncpg.CheckViolationError, (sid, LIVE2, "identity", "cousin", {})),
            (asyncpg.CheckViolationError, (sid, LIVE2, "identity", "unknown", "a string")),
            (asyncpg.CheckViolationError, (sid, LIVE2, "identity", "unknown", [])),
            (asyncpg.CheckViolationError, (sid, 0, "identity", "unknown", {})),
            (asyncpg.CheckViolationError, (sid, -5, "identity", "unknown", {})),
            (asyncpg.UniqueViolationError, (sid, LIVE1, "drive_folder", "self", {})),
            (asyncpg.ForeignKeyViolationError, (sid + 999, LIVE2, "identity", "unknown", {})),
            (asyncpg.DataError, (sid, 2147483648, "identity", "unknown", {})),
        ]
        for error, args in refused:
            with pytest.raises(error):
                await conn.execute(insert, *args)
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO related_subject_links (id, subject_id, client_id, basis, evidence, "
                "created_by) VALUES (7, $1, $2, 'identity', '{}'::jsonb, 'zz')",
                sid, LIVE2,
            )  # fmt: skip
        assert await conn.fetchval("SELECT count(*) FROM related_subject_links") == 2 + len(
            relations
        )
        # the relation column defaults to `unknown`
        await conn.execute(
            "INSERT INTO related_subject_links (subject_id, client_id, basis, evidence, "
            "created_by) VALUES ($1, $2, 'whatsapp_phone', '{}'::jsonb, 'zz')",
            sid, LIVE3,
        )  # fmt: skip
        assert (
            await conn.fetchval(
                "SELECT relation FROM related_subject_links WHERE client_id = $1", LIVE3
            )
            == "unknown"
        )

        # cascade: deleting the subject deletes its links, nothing else
        other = await conn.fetchval(
            "INSERT INTO related_subjects (kind, evidence, created_by) "
            "VALUES ('person', '{}'::jsonb, 'zz') RETURNING id"
        )
        await conn.execute(insert, other, LIVE1, "identity", "unknown", {})
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", sid)
        assert [(r["subject_id"], r["client_id"]) for r in await link_rows(conn)] == [
            (other, LIVE1)
        ]

        # rollback drops the new table and nothing else
        _, rollback = split_migration_sql(MIGRATION_LINKS.read_text())
        assert rollback and "related_subject_links" in rollback
        await conn.execute(rollback)
        present = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name LIKE 'related\\_%'"
        )
        assert {r["table_name"] for r in present} == {
            "related_subjects",
            "related_subject_identifiers",
            "related_documents",
            "related_document_sightings",
            "related_subject_documents",
            "related_subject_names",
        }
        assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1


# ── L2: the production-shaped pool and the evidence shape ─────────────────────


@pytest.mark.asyncio
async def test_l2_guard_the_module_pool_carries_the_production_jsonb_codec(conn):
    # a native container round-trips as itself; a pre-serialised string comes back as a STRING,
    # which is the double-encoding signature of the 2026-11-18 incident
    assert await conn.fetchval("SELECT $1::jsonb", {"a": 1}) == {"a": 1}
    assert await conn.fetchval("SELECT $1::jsonb", '{"a": 1}') == '{"a": 1}'
    assert await conn.fetchval("SELECT jsonb_typeof($1::jsonb)", '{"a": 1}') == "string"


@pytest.mark.asyncio
async def test_l2_a_written_link_holds_a_jsonb_object_with_the_evidence_sent(
    client, conn, clients, writes
):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client,
        payload([entry(LIVE1, "drive_folder", files=5), entry(LIVE2, "identity", files=7)]),
    )
    assert resp.json()["recorded"] == 2
    rows = await conn.fetch(
        "SELECT client_id, basis, relation, created_by, evidence, jsonb_typeof(evidence) AS kind, "
        "evidence->>'plan_sha256' AS plan, (evidence->>'files')::int AS files "
        "FROM related_subject_links WHERE subject_id = $1 ORDER BY client_id",
        sid,
    )
    assert [
        (
            r["client_id"],
            r["basis"],
            r["kind"],
            r["plan"],
            r["files"],
            r["relation"],
            r["created_by"],
        )
        for r in rows
    ] == [
        (LIVE1, "drive_folder", "object", PLAN, 5, "unknown", REGISTRY_ACTOR),
        (LIVE2, "identity", "object", PLAN, 7, "unknown", REGISTRY_ACTOR),
    ]
    assert rows[0]["evidence"] == {"plan_sha256": PLAN, "files": 5}
    assert rows[1]["evidence"] == {"plan_sha256": PLAN, "files": 7}


# ── L3: record ────────────────────────────────────────────────────────────────


def _recorded(sid, recorded, already, dry_run):
    return {
        "outcome": "recorded",
        "subject_id": sid,
        "recorded": recorded,
        "already_recorded": already,
        "dry_run": dry_run,
    }


@pytest.mark.asyncio
async def test_l3_dry_run_writes_nothing_and_counts_as_the_apply_does(
    client, conn, clients, writes
):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    links = [entry(LIVE1, "drive_folder"), entry(LIVE2, "whatsapp_phone")]
    dry = await post(client, payload(links, dry_run=True))
    assert dry.json() == _recorded(sid, 2, 0, True)
    assert await link_rows(conn) == []
    omitted = payload(links)
    del omitted["dry_run"]  # the default is a dry run
    assert (await post(client, omitted)).json() == _recorded(sid, 2, 0, True)
    assert await link_rows(conn) == []
    apply = await post(client, payload(links))
    assert apply.json() == _recorded(sid, 2, 0, False)
    assert len(await link_rows(conn, sid)) == 2


@pytest.mark.asyncio
async def test_l3_a_second_identical_call_records_zero(client, conn, clients, writes):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    links = [entry(LIVE1, "drive_folder"), entry(LIVE2, "identity")]
    assert (await post(client, payload(links))).json() == _recorded(sid, 2, 0, False)
    assert (await post(client, payload(links))).json() == _recorded(sid, 0, 2, False)
    assert (await post(client, payload(links, dry_run=True))).json() == _recorded(sid, 0, 2, True)
    assert len(await link_rows(conn)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_l3_mixed_batch_counts_one_and_one_in_both_modes(
    client, conn, clients, writes, dry_run
):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await post(client, payload([entry(LIVE1, "drive_folder")]))
    resp = await post(
        client,
        payload([entry(LIVE1, "drive_folder"), entry(LIVE2, "drive_folder")], dry_run=dry_run),
    )
    assert resp.json() == _recorded(sid, 1, 1, dry_run)
    assert len(await link_rows(conn)) == (1 if dry_run else 2)


@pytest.mark.asyncio
async def test_l3_entries_are_inserted_in_client_then_basis_order(client, conn, clients, writes):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client,
        payload(
            [
                entry(LIVE2, "whatsapp_phone"),
                entry(LIVE1, "identity"),
                entry(LIVE1, "drive_folder"),
                entry(LIVE3, "identity"),
            ]
        ),
    )
    assert resp.json()["recorded"] == 4
    rows = await link_rows(conn, sid)
    assert [(r["client_id"], r["basis"]) for r in rows] == [
        (LIVE1, "drive_folder"),
        (LIVE1, "identity"),
        (LIVE2, "whatsapp_phone"),
        (LIVE3, "identity"),
    ]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)


@pytest.mark.asyncio
async def test_l3_statement_counts_per_mode_stay_within_the_budget(
    pool, enabled, auth_on, audit_logger, monkeypatch, conn, clients, writes
):
    writes.add(LINKS_INSERT)
    as_identity(monkeypatch, None)
    await seed_subject(conn, "person", None, [("passport", P1)])
    links = [entry(LIVE1, "drive_folder"), entry(LIVE2, "identity"), entry(LIVE3, "identity")]

    dry = _SpyPool(pool)
    async with make_client(dry) as c:
        assert (await post(c, payload(links, dry_run=True))).status_code == 200
    # at most 3 statements to resolve the subject, 1 for the clients, exactly 1 read
    assert len(dry.calls) == 3 + 1 + 1
    assert dry.calls[3] == links_module._CLIENTS_SQL
    assert dry.calls[4] == links_module._EXISTING_SQL
    assert dry.inserts() == []

    apply = _SpyPool(pool)
    async with make_client(apply) as c:
        assert (await post(c, payload(links))).status_code == 200
    # the same resolution and clients, then one INSERT per entry
    assert len(apply.calls) == 3 + 1 + len(links)
    assert apply.calls[3] == links_module._CLIENTS_SQL
    assert apply.calls[4:] == [links_module._INSERT_LINK_SQL] * len(links)


# ── L4: identity ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l4_the_same_client_under_two_bases_is_two_rows(client, conn, clients, writes):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(client, payload([entry(LIVE1, "drive_folder"), entry(LIVE1, "identity")]))
    assert resp.json() == _recorded(sid, 2, 0, False)
    assert len(await link_rows(conn, sid)) == 2


@pytest.mark.asyncio
async def test_l4_the_same_client_and_basis_twice_is_duplicate_link_even_with_other_evidence(
    client, conn, clients
):
    await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client, payload([entry(LIVE1, "identity", files=1), entry(LIVE1, "identity", files=9)])
    )
    assert loc_codes(resp) == [([], "duplicate_link")]
    assert await link_rows(conn) == []


@pytest.mark.asyncio
async def test_l4_a_repeat_with_other_evidence_numbers_leaves_the_stored_row(
    client, conn, clients, writes
):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await post(client, payload([entry(LIVE1, "drive_folder", files=2)]))
    stored = (await link_rows(conn, sid))[0]
    again = await post(client, payload([entry(LIVE1, "drive_folder", files=9)]))
    assert again.json()["recorded"] == 0 and again.json()["already_recorded"] == 1
    after = (await link_rows(conn, sid))[0]
    assert after["evidence"] == stored["evidence"] == {"plan_sha256": PLAN, "files": 2}
    assert after["relation"] == stored["relation"] == "unknown"
    assert after["id"] == stored["id"]


# ── L5: all or nothing ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_l5_one_invalid_entry_among_valid_ones_writes_nothing(client, conn, clients):
    await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client,
        payload([entry(LIVE1, "drive_folder"), entry(LIVE2, "identity"), entry(0, "identity")]),
    )
    assert loc_codes(resp) == [(["links", 2, "client_id"], "greater_than_equal")]
    assert await link_rows(conn) == []


@pytest.mark.asyncio
async def test_l5_one_missing_client_among_valid_ones_writes_nothing(client, conn, clients):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client,
        payload([entry(LIVE1, "drive_folder"), entry(MISSING1, "identity"), entry(LIVE2)]),
    )
    assert resp.json() == {"outcome": "client_not_found", "subject_id": sid, "indexes": [1]}
    assert await link_rows(conn) == []


class _FailOnInsert:
    """Delegates to a real connection; the n-th INSERT of a link raises."""

    def __init__(self, conn, fail_at):
        self._conn, self._fail_at, self.inserts = conn, fail_at, 0

    def __getattr__(self, attr):
        return getattr(self._conn, attr)

    async def fetchval(self, sql, *args):
        if sql == links_module._INSERT_LINK_SQL:
            self.inserts += 1
            if self.inserts == self._fail_at:
                raise RuntimeError("zz injected failure")
        return await self._conn.fetchval(sql, *args)


def _request(links, *, kind="person", ids=(("passport", P1),)):
    return LinksInput(
        kind=kind,
        identifiers=prepare_identifiers(kind, [(t, v, {}) for t, v in ids]),
        links=tuple(LinkEntry(c, b, {"plan_sha256": PLAN, "files": 1}) for c, b in links),
    )


@pytest.mark.asyncio
async def test_l5_a_failure_after_the_first_insert_leaves_no_row(conn, clients, writes):
    writes.add(LINKS_INSERT)
    await seed_subject(conn, "person", None, [("passport", P1)])
    flaky = _FailOnInsert(conn, fail_at=2)
    request = _request([(LIVE1, "drive_folder"), (LIVE2, "drive_folder")])
    with pytest.raises(RuntimeError):
        await record_links(flaky, request, dry_run=False)
    assert flaky.inserts == 2
    assert await link_rows(conn) == []
    ok = await record_links(conn, request, dry_run=False)
    assert ok["recorded"] == 2 and len(await link_rows(conn)) == 2


# ── L6: concurrency ───────────────────────────────────────────────────────────


class _Gate:
    """Delegates to a real connection; the first INSERT waits until both callers reached it."""

    def __init__(self, conn, barrier):
        self._conn, self._barrier, self._waited = conn, barrier, False

    def __getattr__(self, attr):
        return getattr(self._conn, attr)

    async def fetchval(self, sql, *args):
        if sql == links_module._INSERT_LINK_SQL and not self._waited:
            self._waited = True
            await asyncio.wait_for(self._barrier.wait(), 10)
        return await self._conn.fetchval(sql, *args)


@pytest.mark.asyncio
async def test_l6_concurrent_identical_requests_leave_one_row_per_triple(pool, writes):
    writes.add(LINKS_INSERT)
    async with pool.acquire() as seed:
        sid = await seed_subject(seed, "person", None, [("passport", P1)])
        await seed_client(seed, LIVE1, NAME_LIVE1)
        await seed_client(seed, LIVE2, NAME_LIVE2)
    request = _request([(LIVE1, "drive_folder"), (LIVE1, "identity"), (LIVE2, "drive_folder")])
    barrier = asyncio.Barrier(2)
    async with pool.acquire() as c1, pool.acquire() as c2:
        out = await asyncio.gather(
            record_links(_Gate(c1, barrier), request, dry_run=False),
            record_links(_Gate(c2, barrier), request, dry_run=False),
        )
        rows = await link_rows(c1, sid)
    assert sum(o["recorded"] for o in out) == 3
    assert sum(o["already_recorded"] for o in out) == 3
    assert len(rows) == 3 and len({(r["client_id"], r["basis"]) for r in rows}) == 3


# ── L7: subject refusals ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def owners(conn):
    ids = {}
    ids["a"] = await seed_subject(conn, "person", None, [("passport", P1), ("nik", NIK1)])
    ids["b"] = await seed_subject(conn, "person", None, [("passport", P2)])
    ids["company"] = await seed_subject(conn, "company", None, [("passport", P3)])
    ids["merged"] = await seed_subject(conn, "person", None, [("passport", P4)], merged=ids["a"])
    return ids


SUBJECT_REFUSALS = [
    ((("passport", UNOWNED),), "person", "none_owned"),
    ((("passport", P1), ("passport", P2)), "person", "two_owners"),
    ((("passport", P1),), "person", "set_differs"),
    ((("passport", P1), ("nik", NIK1), ("passport", UNOWNED)), "person", "set_differs"),
    ((("passport", P1), ("passport", UNOWNED)), "person", "partial_overlap"),
    ((("passport", P3),), "person", "kind_differs"),
    ((("passport", P4),), "person", "merged"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("ids,kind,reason", SUBJECT_REFUSALS)
async def test_l7_subject_refusals_each_with_its_reason(
    client, conn, clients, owners, ids, kind, reason, dry_run
):
    resp = await post(client, payload([entry(LIVE1)], ids=ids, kind=kind, dry_run=dry_run))
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "subject_not_registered", "reason": reason}
    assert await link_rows(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_l7_a_bad_subject_and_a_missing_client_answers_the_subject_refusal(
    client, conn, clients, owners, dry_run
):
    resp = await post(
        client,
        payload([entry(MISSING1), entry(LIVE1)], ids=(("passport", UNOWNED),), dry_run=dry_run),
    )
    assert resp.json() == {"outcome": "subject_not_registered", "reason": "none_owned"}


@pytest.mark.asyncio
async def test_l7_the_owner_with_its_complete_key_set_is_accepted(
    client, conn, clients, owners, writes
):
    writes.add(LINKS_INSERT)
    resp = await post(client, payload([entry(LIVE1)], ids=(("nik", NIK1), ("passport", P1))))
    assert resp.json() == _recorded(owners["a"], 1, 0, False)


# ── L8: clients ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "links,indexes",
    [
        ([entry(LIVE1), entry(MISSING1, "identity")], [1]),
        ([entry(MISSING1), entry(LIVE1), entry(MISSING2, "identity")], [0, 2]),
        ([entry(LIVE1), entry(GONE, "identity")], [1]),
        ([entry(GONE), entry(MISSING2), entry(LIVE2), entry(LIVE1, "identity")], [0, 1]),
        ([entry(MISSING2, "whatsapp_phone"), entry(MISSING1)], [0, 1]),
    ],
)
async def test_l8_client_not_found_names_the_positions_as_submitted(
    client, conn, clients, links, indexes, dry_run
):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(client, payload(links, dry_run=dry_run))
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "client_not_found", "subject_id": sid, "indexes": indexes}
    assert "4242" not in resp.text  # no client id is echoed
    assert await link_rows(conn) == []


@pytest.mark.asyncio
async def test_l8_indexes_follow_the_request_not_the_sorted_write_order(client, conn, clients):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client,
        payload(
            [entry(MISSING1), entry(LIVE1), entry(MISSING2), entry(GONE)],
        ),
    )
    assert resp.json() == {"outcome": "client_not_found", "subject_id": sid, "indexes": [0, 2, 3]}


@pytest.mark.asyncio
async def test_l8_a_soft_deleted_client_is_refused_and_a_live_one_accepted(
    client, conn, clients, writes
):
    writes.add(LINKS_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    refused = await post(client, payload([entry(GONE)]))
    assert refused.json() == {"outcome": "client_not_found", "subject_id": sid, "indexes": [0]}
    accepted = await post(client, payload([entry(LIVE1)]))
    assert accepted.json() == _recorded(sid, 1, 0, False)
    # `clients` is never written (the teardown of the pool fixture proves it for the whole test)
    assert await conn.fetchval("SELECT count(*) FROM clients") == 4


# ── L9: validation ────────────────────────────────────────────────────────────


def _mut(fn):
    def apply(data):
        data = copy.deepcopy(data)
        fn(data)
        return data

    return apply


def _set_evidence(**kw):
    return _mut(lambda d: d["links"][0]["evidence"].update(kw))


def _set_entry(**kw):
    return _mut(lambda d: d["links"][0].update(kw))


PA = "ZZ7373730"
NIK_A = "3201 0000 0000 0002"
FULL_A = (("passport", PA), ("nik", NIK_A))
# entry 0 is the one the mutators change; entry 1 is a valid link of another client and carries
# the probe client id
BASE_BODY = payload([entry(LIVE2, "drive_folder"), entry(LIVE1, "identity", files=2)], ids=FULL_A)

L9_CASES = {
    "client_id_is_a_string": (
        _set_entry(client_id="424243"),
        [(["links", 0, "client_id"], "int_type")],
    ),
    "client_id_is_a_boolean": (
        _set_entry(client_id=True),
        [(["links", 0, "client_id"], "int_type")],
    ),
    "client_id_is_a_float": (
        _set_entry(client_id=4242.5),
        [(["links", 0, "client_id"], "int_type")],
    ),
    "client_id_zero": (
        _set_entry(client_id=0),
        [(["links", 0, "client_id"], "greater_than_equal")],
    ),
    "client_id_negative": (
        _set_entry(client_id=-5),
        [(["links", 0, "client_id"], "greater_than_equal")],
    ),
    "client_id_above_limit": (
        _set_entry(client_id=2147483648),
        [(["links", 0, "client_id"], "less_than_equal")],
    ),
    "dry_run_is_a_boolean": (
        _mut(lambda d: d.update(dry_run="true")),
        [(["dry_run"], "bool_type")],
    ),
    "files_is_an_integer": (
        _set_evidence(files=1.5),
        [(["links", 0, "evidence", "files"], "int_type")],
    ),
    "files_is_not_a_boolean": (
        _set_evidence(files=True),
        [(["links", 0, "evidence", "files"], "int_type")],
    ),
    "files_zero": (
        _set_evidence(files=0),
        [(["links", 0, "evidence", "files"], "greater_than_equal")],
    ),
    "files_above_limit": (
        _set_evidence(files=10001),
        [(["links", 0, "evidence", "files"], "less_than_equal")],
    ),
    "files_missing": (
        _mut(lambda d: d["links"][0]["evidence"].pop("files")),
        [(["links", 0, "evidence", "files"], "missing")],
    ),
    "plan_hash_missing": (
        _mut(lambda d: d["links"][0]["evidence"].pop("plan_sha256")),
        [(["links", 0, "evidence", "plan_sha256"], "missing")],
    ),
    "plan_hash_short": (
        _set_evidence(plan_sha256="ab12" * 15),
        [(["links", 0, "evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "plan_hash_not_hex": (
        _set_evidence(plan_sha256="g" * 64),
        [(["links", 0, "evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "plan_hash_upper_case": (
        _set_evidence(plan_sha256="A" * 64),
        [(["links", 0, "evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "evidence_extra_key": (
        _set_evidence(blobs=1),
        [(["links", 0, "evidence", "_"], "extra_forbidden")],
    ),
    "hostile_field_name": (
        _set_evidence(**{"ZzHostile<script>": 1}),
        [(["links", 0, "evidence", "_"], "extra_forbidden")],
    ),
    "no_links": (_mut(lambda d: d.update(links=[])), [(["links"], "too_short")]),
    "six_links": (
        _mut(lambda d: d.update(links=[entry(LIVE1 + i, "identity") for i in range(6)])),
        [(["links"], "too_long")],
    ),
    "links_is_not_a_list": (_mut(lambda d: d.update(links="zz")), [(["links"], "list_type")]),
    "manual_is_refused": (
        _set_entry(basis="manual"),
        [(["links", 0, "basis"], "literal_error")],
    ),
    "unknown_basis": (
        _set_entry(basis="guessed"),
        [(["links", 0, "basis"], "literal_error")],
    ),
    "unknown_field": (_mut(lambda d: d.update(zzextra=1)), [(["_"], "extra_forbidden")]),
    "relation_is_not_a_field": (
        _mut(lambda d: d.update(relation="spouse")),
        [(["_"], "extra_forbidden")],
    ),
    "relation_in_an_entry": (
        _set_entry(relation="spouse"),
        [(["links", 0, "_"], "extra_forbidden")],
    ),
    "created_by_in_an_entry": (
        _set_entry(created_by="zz"),
        [(["links", 0, "_"], "extra_forbidden")],
    ),
    "client_name_in_an_entry": (
        _set_entry(client_name="zz"),
        [(["links", 0, "_"], "extra_forbidden")],
    ),
    "missing_subject": (_mut(lambda d: d.pop("subject")), [(["subject"], "missing")]),
    "identifier_value": (
        _mut(lambda d: d["subject"]["identifiers"][0].update(value="!")),
        [(["subject", "identifiers", 0], "invalid_identifier")],
    ),
    "kind_type_mismatch": (
        _mut(lambda d: d["subject"].update(kind="company")),
        [(["subject"], "kind_type_mismatch")],
    ),
    "duplicate_identifier": (
        _mut(lambda d: d["subject"]["identifiers"].append(d["subject"]["identifiers"][0])),
        [(["subject"], "duplicate_identifier")],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(L9_CASES))
async def test_l9_exact_422_bodies(client, conn, clients, case):
    await seed_subject(conn, "person", None, list(FULL_A))
    mutate, expected = L9_CASES[case]
    resp = await post(client, mutate(BASE_BODY))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [{"loc": loc, "code": code} for loc, code in expected]}
    assert await link_rows(conn) == []


@pytest.mark.asyncio
async def test_l9_accepted_boundaries(client, conn, clients, writes):
    writes.add(LINKS_INSERT)
    await seed_client(conn, 2147483647, "ZzMaxClient")
    await seed_client(conn, 1, "ZzMinClient")
    await seed_subject(conn, "person", None, [("passport", P1)])
    links = [
        entry(2147483647, "identity", files=10000),
        entry(1, "whatsapp_phone", files=1),
        entry(LIVE1),
        entry(LIVE2),
        entry(LIVE3),
    ]
    resp = await post(client, payload(links))
    assert resp.status_code == 200 and resp.json()["recorded"] == 5


@pytest.mark.asyncio
async def test_l9_malformed_json_and_oversize_body(client, conn, clients):
    await seed_subject(conn, "person", None, [("passport", P1)])
    for raw in (b"{not json", b"", b"[1, 2]", b"null"):
        resp = await post(client, raw)
        assert resp.status_code == 422, raw
        body = resp.json()["detail"]
        assert (
            len(body) == 1
            and body[0]["loc"] == []
            and body[0]["code"] in {"json_invalid", "model_type"}
        )
    pad = "x" * (route_module.MAX_BODY_BYTES + 1)
    big = await post(client, pad.encode())
    assert big.status_code == 422
    assert big.json() == {"detail": [{"loc": ["body"], "code": "too_long"}]}
    exact = await post(client, b" " * route_module.MAX_BODY_BYTES)
    assert exact.json()["detail"][0]["loc"] == []  # at the limit the body is parsed, not refused


def _sanitised(errors):
    resp = route_module._invalid_links(errors)
    assert resp.status_code == 422
    return json.loads(resp.body)["detail"]


def test_l9_sanitiser_keeps_only_closed_names_and_indexes():
    out = _sanitised(
        [
            {"loc": ("links", 9, "client_id"), "type": "int_type"},
            {"loc": ("links", 4, "client_id"), "type": "int_type"},
            {"loc": ("subject", "identifiers", 19, "type"), "type": "literal_error"},
            {"loc": ("subject", "identifiers", 20, "type"), "type": "literal_error"},
            {"loc": ("links", True, "basis"), "type": "literal_error"},
            {"loc": ("links", -1), "type": "int_type"},
            {"loc": ("body", 0), "type": "too_long"},
            {"loc": ("links", 0, "ZzHostile"), "type": "extra_forbidden"},
            {"loc": ("evidence", 1), "type": "weird_code"},
            {"loc": (), "type": "duplicate_link"},
            {"loc": ("links", 0), "type": "value_error"},
            {"loc": ("links", 0, "names"), "type": "duplicate_name"},
        ]
    )
    assert out == [
        {"loc": ["links", "_", "client_id"], "code": "int_type"},
        {"loc": ["links", 4, "client_id"], "code": "int_type"},
        {"loc": ["subject", "identifiers", 19, "type"], "code": "literal_error"},
        {"loc": ["subject", "identifiers", "_", "type"], "code": "literal_error"},
        {"loc": ["links", "_", "basis"], "code": "literal_error"},
        {"loc": ["links", "_"], "code": "int_type"},
        {"loc": ["body", "_"], "code": "too_long"},
        {"loc": ["links", 0, "_"], "code": "extra_forbidden"},
        {"loc": ["evidence", "_"], "code": "invalid"},
        {"loc": [], "code": "duplicate_link"},
        {"loc": ["links", 0], "code": "invalid"},
        {"loc": ["links", 0, "_"], "code": "invalid"},
    ]


def test_l9_sanitiser_caps_the_list_at_fifty_errors():
    out = _sanitised([{"loc": ("links", 0, "client_id"), "type": "int_type"}] * 60)
    assert len(out) == 50


# ── L10: precedence ───────────────────────────────────────────────────────────

BAD_BODIES = [b"{not json", json.dumps(payload([entry(0)])).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_l10_the_flag_is_checked_before_the_body(pool, auth_on, monkeypatch, raw):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    as_identity(monkeypatch, None)
    spy = _SpyPool(pool)
    async with make_client(spy) as c:
        resp = await post(c, raw)
        no_key = await post(c, raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}
    assert no_key.status_code == 503 and no_key.json() == {"detail": "registry_unavailable"}
    assert spy.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
@pytest.mark.parametrize("key", [None, "wrong-key"])
async def test_l10_the_key_is_checked_before_the_body(
    pool, enabled, auth_on, monkeypatch, raw, key
):
    as_identity(monkeypatch, None)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-CRM-Write-Key"] = key
    spy = _SpyPool(pool)
    async with make_client(spy) as c:
        resp = await post(c, raw, headers=headers)
    assert resp.status_code == 401 and resp.json() == {"detail": "unauthorized"}
    assert spy.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_l10_no_pool_is_the_existing_503_before_the_body(enabled, auth_on, monkeypatch, raw):
    as_identity(monkeypatch, None)
    async with make_client(None) as c:
        resp = await post(c, raw)
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_l10_with_every_gate_open_an_invalid_body_is_422_and_runs_no_statement(
    pool, enabled, auth_on, audit_logger, monkeypatch, raw
):
    as_identity(monkeypatch, None)
    spy = _SpyPool(pool)
    async with make_client(spy) as c:
        resp = await post(c, raw)
    assert resp.status_code == 422
    assert spy.acquired == 0 and spy.calls == []


# ── L11: the read routes ──────────────────────────────────────────────────────

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)
LINK_FIELDS = {
    "client_id",
    "client_name",
    "client_live",
    "basis",
    "relation",
    "created_at",
    "from_subject_id",
}


def _ids(resp):
    return [r["id"] for r in resp.json()["items"]]


@pytest.mark.asyncio
async def test_l11_the_detail_carries_links_with_exactly_seven_fields_in_order(
    admin, conn, clients
):
    sid = await seed_subject(conn, "person", "ZzL11 Person", [("passport", P1)])
    third = await seed_link(conn, sid, LIVE3, "identity", at=T0 + 2 * DAY)
    first = await seed_link(conn, sid, LIVE1, "drive_folder", at=T0)
    tie_a = await seed_link(conn, sid, LIVE2, "whatsapp_phone", at=T0 + DAY)
    tie_b = await seed_link(conn, sid, LIVE2, "identity", at=T0 + DAY)
    assert tie_a < tie_b and first != third
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert detail["links_total"] == 4
    assert [(i["client_id"], i["basis"]) for i in detail["links"]] == [
        (LIVE1, "drive_folder"),
        (LIVE2, "whatsapp_phone"),
        (LIVE2, "identity"),
        (LIVE3, "identity"),
    ]
    for item in detail["links"]:
        assert set(item) == LINK_FIELDS
        assert item["relation"] == "unknown" and item["client_live"] is True
        assert item["from_subject_id"] is None
        assert datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")).tzinfo
    assert detail["links"][0]["client_name"] == NAME_LIVE1


@pytest.mark.asyncio
async def test_l11_twenty_are_shown_and_every_link_is_counted(admin, conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    for i in range(25):
        await seed_link(conn, sid, 424500 + i, "identity", at=T0 + i * DAY)
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert detail["links_total"] == 25 and len(detail["links"]) == 20
    assert [i["client_id"] for i in detail["links"]] == [424500 + i for i in range(20)]


@pytest.mark.asyncio
async def test_l11_the_client_name_is_read_at_request_time(admin, conn, clients):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_link(conn, sid, LIVE1)
    before = (await admin.get(f"{READ}/{sid}")).json()["links"][0]
    assert (before["client_name"], before["client_live"]) == (NAME_LIVE1, True)
    async with unmeasured(conn):
        await conn.execute("UPDATE clients SET full_name = 'ZzRenamedClient' WHERE id = $1", LIVE1)
    after = (await admin.get(f"{READ}/{sid}")).json()["links"][0]
    assert (after["client_name"], after["client_live"]) == ("ZzRenamedClient", True)


@pytest.mark.asyncio
async def test_l11_a_dead_client_has_a_null_name_and_the_link_is_still_listed(admin, conn, clients):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_link(conn, sid, GONE, "drive_folder", at=T0)
    await seed_link(conn, sid, LIVE1, "drive_folder", at=T0 + DAY)
    await seed_link(conn, sid, MISSING1, "identity", at=T0 + 2 * DAY)
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert detail["links_total"] == 3
    soft, live, absent = detail["links"]
    assert (soft["client_id"], soft["client_name"], soft["client_live"]) == (GONE, None, False)
    assert (live["client_name"], live["client_live"]) == (NAME_LIVE1, True)
    assert (absent["client_id"], absent["client_name"], absent["client_live"]) == (
        MISSING1,
        None,
        False,
    )
    assert NAME_GONE not in json.dumps(detail)


@pytest.mark.asyncio
async def test_l11_a_subject_without_links_has_an_empty_list_and_a_zero_total(admin, conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert detail["links"] == [] and detail["links_total"] == 0
    assert (await admin.get(READ)).json()["items"][0]["links_count"] == 0


@pytest.mark.asyncio
async def test_l11_list_rows_count_every_link_live_client_or_not(admin, conn, clients):
    one = await seed_subject(conn, "person", "ZzOne Person", [("passport", P1)])
    await seed_link(conn, one, LIVE1)
    await seed_link(conn, one, GONE)
    await seed_link(conn, one, MISSING1)
    two = await seed_subject(conn, "person", "ZzTwo Person", [("passport", P2)])
    none = await seed_subject(conn, "company", "ZzNone Company", [("npwp", "012345678901234")])
    rows = {r["id"]: r["links_count"] for r in (await admin.get(READ)).json()["items"]}
    assert rows == {one: 3, two: 0, none: 0}


@pytest.mark.asyncio
async def test_l11_the_linked_filter_with_q_kind_total_and_paging(admin, conn, clients):
    a = await seed_subject(conn, "person", "ZzAlpha Linked", [("passport", P1)])
    await seed_link(conn, a, LIVE1)
    b = await seed_subject(conn, "person", "ZzBeta Linked", [("passport", P2)])
    await seed_link(conn, b, LIVE2)
    await seed_link(conn, b, LIVE3)
    c = await seed_subject(conn, "person", "ZzGamma Alone", [("passport", P3)])
    d = await seed_subject(conn, "company", "ZzDelta Linked", [("npwp", "012345678901234")])
    await seed_link(conn, d, LIVE1, "identity")
    e = await seed_subject(conn, "company", "ZzEpsilon Alone", [("npwp", "012345678901235")])

    async def listed(**params):
        resp = await admin.get(READ, params={"limit": 100, **params})
        assert resp.status_code == 200, resp.text
        return resp

    assert _ids(await listed()) == [a, b, d, e, c]
    assert sorted(_ids(await listed(linked="true"))) == sorted([a, b, d])
    assert sorted(_ids(await listed(linked="false"))) == sorted([c, e])
    assert _ids(await listed(linked="true", q="beta")) == [b]
    assert _ids(await listed(linked="false", q="beta")) == []
    assert sorted(_ids(await listed(linked="true", kind="person"))) == sorted([a, b])
    assert _ids(await listed(linked="false", kind="company")) == [e]
    assert sorted(_ids(await listed(linked="true", q="zz"))) == sorted([a, b, d])
    # total, page and offset agree with the filter
    pages = [await listed(linked="true", limit=2, offset=off) for off in (0, 2, 4)]
    assert [r.json()["total"] for r in pages] == [3, 3, 3]
    assert [len(_ids(r)) for r in pages] == [2, 1, 0]
    assert sorted(_ids(pages[0]) + _ids(pages[1])) == sorted([a, b, d])
    assert (await listed(linked="false", limit=1)).json()["total"] == 2
    assert (await listed()).json()["total"] == 5


@pytest.mark.asyncio
async def test_l11_an_unknown_linked_value_is_a_422(admin, conn):
    await seed_subject(conn, "person", None, [("passport", P1)])
    for value in ("maybe", "1", "True", ""):
        resp = await admin.get(READ, params={"linked": value})
        assert resp.status_code == 422, value


# ── L12: the read SQL budget ──────────────────────────────────────────────────


class _CountingConn:
    """Delegates to a real connection and counts the statements the service runs."""

    def __init__(self, conn):
        self._conn, self.statements = conn, 0

    def __getattr__(self, attr):
        target = getattr(self._conn, attr)
        if attr in {"fetch", "fetchrow", "fetchval", "execute"}:

            async def counted(*args, **kwargs):
                self.statements += 1
                return await target(*args, **kwargs)

            return counted
        return target


async def _seed_document(c, sid, tag):
    sha = f"{tag:064x}"
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
            "first_channel, evidence, created_by) VALUES ($1, 10, 'application/pdf', "
            "'2026-01-01', 'drive', '{}'::jsonb, 'zz-seed')",
            sha,
        )
        await c.execute(
            "INSERT INTO related_document_sightings (kind, ref, sha256, seen_on, created_by) "
            "VALUES ('drive', $1, $2, '2026-01-01', 'zz-seed')",
            f"ZzDriveRef{tag:030d}", sha,
        )  # fmt: skip
        await c.execute(
            "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
            "created_by) VALUES ($1, $2, 'passport', '{}'::jsonb, 'zz-seed')",
            sid, sha,
        )  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [1, 50])
@pytest.mark.parametrize("params", [{}, {"linked": True}, {"linked": False}, {"q": "zz row"}])
async def test_l12_a_populated_list_page_runs_at_most_four_selects(conn, clients, rows, params):
    for i in range(rows):
        sid = await seed_subject(conn, "person", f"ZZ ROW {i:02d}", [("passport", f"ZZ77{i:05d}")])
        await seed_link(conn, sid, LIVE1 if i % 2 == 0 else GONE)
    counting = _CountingConn(conn)
    result = await related_registry_read.list_subjects(
        counting, q=params.get("q"), kind=None, limit=100, offset=0, linked=params.get("linked")
    )
    assert counting.statements <= 4
    if params.get("linked") is False:
        assert result["items"] == []
    else:
        assert len(result["items"]) == rows
        assert all(r["links_count"] == 1 for r in result["items"])


@pytest.mark.asyncio
async def test_l12_the_detail_runs_at_most_six_selects(conn, clients):
    sid = await seed_subject(conn, "person", "ZzL12 Person", [("passport", P1)])
    await _seed_document(conn, sid, 1)
    await seed_link(conn, sid, LIVE1)
    counting = _CountingConn(conn)
    detail = await related_registry_read.get_subject(counting, sid)
    assert detail["links_total"] == 1 and len(detail["documents"]) == 1
    assert counting.statements == 6  # subject, identifiers, documents, sightings, names, links
    bare = await seed_subject(conn, "person", None, [("passport", P2)])
    counting = _CountingConn(conn)
    await related_registry_read.get_subject(counting, bare)
    assert counting.statements == 5  # no documents, so no sightings statement


def _sql_constants(module) -> list[str]:
    tree = ast.parse(Path(module.__file__).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and re.search(r"\bFROM\b|\bSELECT\b", node.value)
    ]


FRAGMENTS = {"_MATCH_SQL", "_EFFECTIVE_SQL"}
FORBIDDEN_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|ALTER|DROP|CREATE)\b", re.I)
GUARDED_NAME = "CASE WHEN c.id IS NOT NULL AND c.deleted_at IS NULL THEN c.full_name END"


def test_l12_static_census_is_ten_plain_selects_and_the_guarded_name():
    service = related_registry_read
    for sql in _sql_constants(service):
        assert not FORBIDDEN_SQL.search(sql), sql[:60]
        assert not re.search(r"\bWITH\b", sql, re.I), sql[:60]
    statements = {
        attr: value
        for attr, value in vars(service).items()
        if attr.endswith("_SQL") and attr not in FRAGMENTS
    }
    assert len(statements) == 10
    for attr, sql in statements.items():
        assert sql.lstrip().upper().startswith("SELECT"), attr
        # no read statement selects the producer's evidence or the registry actor
        assert not re.search(r"\b(evidence|created_by)\b", sql, re.I), attr
    # the client name leaves the database only through the guarded CASE, and nowhere else
    links_sql = statements["_LINKS_SQL"]
    assert GUARDED_NAME in links_sql
    assert links_sql.replace(GUARDED_NAME, "").count("full_name") == 0
    assert "LEFT JOIN clients c ON c.id = l.client_id" in links_sql
    for attr, sql in statements.items():
        if attr != "_LINKS_SQL":
            assert "full_name" not in sql, attr


# ── L14: registration, public entry, existing routes ──────────────────────────

FIVE_PATHS = {
    ("/api/crm/related-subjects/register", ("POST",)),
    ("/api/crm/related-subjects/documents/register", ("POST",)),
    ("/api/crm/related-subjects/names/register", ("POST",)),
    ("/api/crm/related-subjects/links/register", ("POST",)),
    ("/api/crm/related-subjects/merges/register", ("POST",)),
}


def test_l14_the_route_is_registered_in_the_sealed_router():
    assert len([e for e in ROUTER_MANIFEST if e.name == "crm_related_subjects"]) == 1
    paths = {(r.path, tuple(sorted(r.methods))) for r in route_module.router.routes}
    assert paths == FIVE_PATHS
    assert route_module.router.route_class is route_module._SealedRoute


def test_l14_public_entry_is_exact_path_and_post_only():
    entries = [e for e in PUBLIC_ENDPOINTS if e.prefix == URL]
    assert len(entries) == 1
    assert entries[0].match == "exact" and tuple(entries[0].methods) == ("POST",)
    assert any(e.matches(URL, "POST") for e in PUBLIC_ENDPOINTS)
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert not any(e.matches(URL, method) for e in PUBLIC_ENDPOINTS), method
    for path in (
        URL + "/",
        URL + "/x",
        "/api/crm/related-subjects/links",
        "/api/crm/related-subjects",
    ):
        assert not any(e.matches(path, "POST") for e in PUBLIC_ENDPOINTS), path
    for path in (READ, f"{READ}/7"):
        assert not any(e.matches(path, "GET") for e in PUBLIC_ENDPOINTS), path
    # the three older entries are unchanged
    older_methods = {URL_SUBJECT: None, URL_DOCUMENTS: ("POST",), URL_NAMES: ("POST",)}
    for older, methods in older_methods.items():
        matching = [e for e in PUBLIC_ENDPOINTS if e.prefix == older]
        assert len(matching) == 1 and matching[0].match == "exact", older
        assert (None if matching[0].methods is None else tuple(matching[0].methods)) == methods


@pytest.mark.asyncio
async def test_l14_get_on_the_path_and_the_read_routes_stay_protected(client, conn):
    sid = await seed_subject(conn, "person", "ZzL14 Person", [("passport", P1)])
    for url in (URL, READ, f"{READ}/{sid}"):
        resp = await client.get(url)
        assert resp.status_code == 401, url
        assert "ZzL14" not in resp.text


@pytest.mark.asyncio
async def test_l14_the_three_existing_write_routes_answer_as_before(client, conn):
    first = await post(client, {}, url=URL_SUBJECT)
    assert first.status_code == 422
    assert first.json() == {
        "detail": [
            {"loc": ["kind"], "code": "missing"},
            {"loc": ["identifiers"], "code": "missing"},
            {"loc": ["evidence"], "code": "missing"},
        ]
    }
    second = await post(client, {}, url=URL_DOCUMENTS)
    assert second.status_code == 422
    assert second.json() == {
        "detail": [
            {"loc": ["subject"], "code": "missing"},
            {"loc": ["documents"], "code": "missing"},
            {"loc": ["evidence"], "code": "missing"},
        ]
    }
    third = await post(client, {}, url=URL_NAMES)
    assert third.status_code == 422
    assert third.json() == {
        "detail": [
            {"loc": ["subject"], "code": "missing"},
            {"loc": ["names"], "code": "missing"},
        ]
    }
    # a slice-4B field name is still "_" on the older routes
    fourth = await post(client, {"links": 1, "kind": "person"}, url=URL_SUBJECT)
    assert {"loc": ["_"], "code": "extra_forbidden"} in fourth.json()["detail"]


# ── L15: the slice writes nothing outside its table ───────────────────────────

TABLE = "related_subject_links"
ALLOWED_NAMING_THE_TABLE = {
    "services/crm/related_links.py",
    "app/routers/crm_related_subjects.py",
    "services/crm/related_registry_read.py",
    "app/routers/crm_related_registry_read.py",
}


def test_l15_literal_references_of_the_write_path():
    service_source = Path(links_module.__file__).read_text()
    assert "client_family_members" not in service_source
    sql_texts = {
        name: value
        for name, value in vars(links_module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert set(sql_texts) == {"_CLIENTS_SQL", "_EXISTING_SQL", "_INSERT_LINK_SQL"}
    with_clients = [name for name, sql in sql_texts.items() if re.search(r"\bclients\b", sql)]
    assert with_clients == ["_CLIENTS_SQL"]
    assert sql_texts["_CLIENTS_SQL"] == (
        "SELECT id FROM clients WHERE id = ANY($1::int[]) AND deleted_at IS NULL"
    )
    assert sql_texts["_INSERT_LINK_SQL"].startswith("INSERT INTO related_subject_links ")
    assert sql_texts["_EXISTING_SQL"].lstrip().upper().startswith("SELECT")
    handler = inspect.getsource(route_module.register_related_subject_links)
    assert "client_family_members" not in handler
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", handler)
    assert "clients" not in handler.replace("db_pool", "")


def test_l15_only_the_named_modules_name_the_table_and_no_portal_router_does():
    found = set()
    for root in (BACKEND / "app", BACKEND / "services"):
        for source in root.rglob("*.py"):
            if TABLE in source.read_text():
                found.add(str(source.relative_to(BACKEND)))
    assert found <= ALLOWED_NAMING_THE_TABLE, sorted(found - ALLOWED_NAMING_THE_TABLE)
    assert "services/crm/related_links.py" in found
    for source in (BACKEND / "app" / "routers").glob("portal*.py"):
        assert TABLE not in source.read_text(), source.name


@pytest.mark.asyncio
async def test_l15_the_write_measurement_sees_only_the_link_inserts(client, conn, clients, writes):
    writes.add(LINKS_INSERT)
    await seed_subject(conn, "person", None, [("passport", P1)])
    await post(client, payload([entry(LIVE1), entry(LIVE2, "identity")]))
    log = {(r["tbl"], r["op"]) for r in await conn.fetch("SELECT tbl, op FROM write_log")}
    assert log == {LINKS_INSERT, AUDIT_INSERT}
    assert await conn.fetchval("SELECT count(*) FROM client_family_members") == 0


# ── L16: a client deleted between the check and the insert ────────────────────


@pytest.mark.asyncio
async def test_l16_a_client_deleted_after_the_check_keeps_its_link(
    pool, enabled, auth_on, audit_logger, monkeypatch, conn, clients, writes
):
    writes.add(LINKS_INSERT)
    as_identity(monkeypatch, None)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])

    async def soft_delete_live1():
        async with pool.acquire() as other:
            async with unmeasured(other):
                await other.execute("UPDATE clients SET deleted_at = now() WHERE id = $1", LIVE1)

    spy = _SpyPool(pool, after_clients=soft_delete_live1)
    async with make_client(spy) as c:
        resp = await post(c, payload([entry(LIVE1, "drive_folder"), entry(LIVE2, "identity")]))
    assert resp.json() == _recorded(sid, 2, 0, False)
    assert len(await link_rows(conn, sid)) == 2
    # the clients SELECT precedes every INSERT
    first_insert = min(i for i, s in enumerate(spy.calls) if s == links_module._INSERT_LINK_SQL)
    assert spy.calls.index(links_module._CLIENTS_SQL) < first_insert
    detail = await related_registry_read.get_subject(conn, sid)
    by_client = {link["client_id"]: link for link in detail["links"]}
    assert by_client[LIVE1]["client_live"] is False and by_client[LIVE1]["client_name"] is None
    assert by_client[LIVE2]["client_live"] is True and by_client[LIVE2]["client_name"] == NAME_LIVE2


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_l16_a_client_refusal_attempts_no_insert(
    pool, enabled, auth_on, audit_logger, monkeypatch, conn, clients, dry_run
):
    as_identity(monkeypatch, None)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    spy = _SpyPool(pool)
    async with make_client(spy) as c:
        resp = await post(c, payload([entry(LIVE1), entry(MISSING1, "identity")], dry_run=dry_run))
    assert resp.json() == {"outcome": "client_not_found", "subject_id": sid, "indexes": [1]}
    assert spy.calls and spy.inserts() == []
    assert links_module._CLIENTS_SQL in spy.calls
    assert await link_rows(conn) == []


# ── L13: privacy matrix ───────────────────────────────────────────────────────

PROTOCOL_WORDS = frozenset(
    {
        "person", "company", "passport", "nik", "ocr_single", "ocr_corroborated", "mrz_corrected",
        "true", "false", "null", "manual", "kind", "subject", "identifiers", "type", "value", "names",
        "name", "source", "evidence", "plan_sha256", "files", "blobs", "mrz_valid", "dry_run",
        "links", "client_id", "basis", "drive_folder", "whatsapp_phone", "identity", "permit",
        "npwp", "nib", "ahu",
    }
)  # fmt: skip

SERVICE_LOG_WORDS = (
    "related", "links", "registry", "outcome", "subject_id", "recorded", "already_recorded",
    "dry_run", "missing",
)  # fmt: skip
ROUTE_FAILURE_WORDS = ("failure", "code")
PATH_SEGMENTS = ("api", "crm", "related-subjects", "links", "register")

# Words the answers, the fixed log lines and the audit endpoint legitimately contain: a request
# leaf that is one of them (or a part of one, like the field name "extra" in "extra_forbidden")
# cannot be a leak of a response or an audit row.
ANSWER_VOCABULARY = frozenset(
    {
        *route_module._LINKS_LOC, *route_module._LINKS_CODE, "detail", "loc", "code", "outcome",
        "reason", "recorded", "already_recorded", "subject_id", "indexes", "dry_run",
        "subject_not_registered", "none_owned", "two_owners", "set_differs", "partial_overlap",
        "kind_differs", "merged", "client_not_found", "registry_unavailable", "registry_busy",
        "unauthorized", "database_error", "internal_error", *SERVICE_LOG_WORDS,
        *ROUTE_FAILURE_WORDS, URL, *PATH_SEGMENTS,
    }
)  # fmt: skip
FIXED_WORDS = {w.casefold() for w in ANSWER_VOCABULARY} | {
    w.casefold() for w in re.split(r"[^A-Za-z0-9_]+", URL) if w
}

# The probe world, forbidden whatever the request holds (spec 7.3 step 5c): the decimal client ids
# and the stored client names.
PROBE_CLIENT_IDS = tuple(str(c) for c in (LIVE1, LIVE2, LIVE3, GONE, MISSING1, MISSING2))
PROBE_CLIENT_NAMES = (NAME_LIVE1, NAME_LIVE2, NAME_LIVE3, NAME_GONE)
UNCONDITIONAL = frozenset(t.casefold() for t in (*PROBE_CLIENT_IDS, *PROBE_CLIENT_NAMES))

PB, PC, PD = "ZZ7373731", "ZZ7373732", "ZZ7373733"
P_UNKNOWN, P_EXTRA = "ZZ7373799", "ZZ7373734"
ALL_PROBE_IDS = (PA, PB, PC, PD, P_UNKNOWN, P_EXTRA, NIK_A)
SENTINEL = "l13-sentinel"


def _leaves(node, strings, ints):
    """String leaves (dictionary keys included) and integer leaves (booleans excluded)."""
    if isinstance(node, dict):
        for k, v in node.items():
            _leaves(k, strings, ints)
            _leaves(v, strings, ints)
    elif isinstance(node, list):
        for v in node:
            _leaves(v, strings, ints)
    elif isinstance(node, bool):
        return
    elif isinstance(node, int):
        ints.add(node)
    elif isinstance(node, str):
        strings.add(node)


def _forbidden(data, *, answer_words: bool) -> set[str]:
    """The request-derived forbidden tokens: string leaves and integer leaves (as decimals) at
    least 4 characters long, that are not a protocol word or the plan hash; for responses and
    audit rows (`answer_words=True`) also not part of a word of the answer vocabulary. Raw,
    upper-cased-normalised and canonical forms."""
    strings: set[str] = set()
    ints: set[int] = set()
    _leaves(data, strings, ints)
    tokens = set()
    for leaf in strings | {str(i) for i in ints}:
        in_vocabulary = answer_words and any(leaf.casefold() in w for w in ANSWER_VOCABULARY)
        if len(leaf) >= 4 and leaf not in PROTOCOL_WORDS and leaf != PLAN and not in_vocabulary:
            tokens.add(leaf.casefold())
            tokens.add(" ".join(leaf.upper().split()).casefold())
            tokens.add(canonical_form("passport", leaf).casefold())
    tokens.discard("")
    return tokens


def _record_text(record: logging.LogRecord) -> str:
    rendered = logging.Formatter().format(record)
    return rendered + " " + " ".join(str(v) for v in record.__dict__.values())


def _forms(leaf: str) -> set[str]:
    """The raw, normalised and canonical forms of a string, case-folded (empty form dropped)."""
    return {
        leaf.casefold(),
        " ".join(leaf.upper().split()).casefold(),
        canonical_form("passport", leaf).casefold(),
    } - {""}


def _declared_probes(requests) -> set[str]:
    """The probes a case put into its request(s), found in the raw text whatever the shape of the
    request is (a parsed dictionary or raw bytes): probe identifiers and probe client ids."""
    out: set[str] = set()
    for request in requests:
        raw = _raw_text(request).casefold()
        for probe in (*ALL_PROBE_IDS, *PROBE_CLIENT_IDS):
            if probe.casefold() in raw:
                out |= _forms(probe)
    return out


def _case_sets(requests) -> tuple[set[str], set[str]]:
    """(log forbidden set, response/audit forbidden set) of a case: the stored probe world, the
    declared probes of every request and, for dictionary requests, the leaf tokens."""
    declared = _declared_probes(requests)
    log_set = set(UNCONDITIONAL) | declared
    answer_set = set(UNCONDITIONAL) | declared
    for request in requests:
        if isinstance(request, dict):
            log_set |= _forbidden(request, answer_words=False)
            answer_set |= _forbidden(request, answer_words=True)
    return log_set, answer_set


# Audit columns whose digits are measured by the server, not derived from a body: scanned never,
# chosen by TYPE read from the catalog (never by name). Integer and text columns are always scanned.
UNSCANNED_AUDIT_TYPES = frozenset(
    {
        "timestamp without time zone",
        "timestamp with time zone",
        "real",
        "double precision",
        "numeric",
    }
)


async def _audit_scan(conn) -> tuple[list, str, set[str]]:
    """Complete audit rows, the text of every scanned column, and the excluded column names."""
    columns = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'api_audit_trail' "
        "ORDER BY ordinal_position"
    )
    excluded = {c["column_name"] for c in columns if c["data_type"] in UNSCANNED_AUDIT_TYPES}
    rows = await conn.fetch("SELECT * FROM api_audit_trail ORDER BY id")
    text = " ".join(
        value if isinstance(value, str) else json.dumps(value, default=str)
        for row in rows
        for name, value in dict(row).items()
        if name not in excluded
    ).casefold()
    if rows:
        assert set(rows[0].keys()) == {c["column_name"] for c in columns}
    return rows, text, excluded


@pytest_asyncio.fixture
async def probe_world(conn):
    await seed_client(conn, LIVE1, NAME_LIVE1)
    await seed_client(conn, LIVE2, NAME_LIVE2)
    await seed_client(conn, LIVE3, NAME_LIVE3)
    await seed_client(conn, GONE, NAME_GONE, deleted=True)
    ids = {}
    ids["a"] = await seed_subject(conn, "person", None, list(FULL_A))
    ids["b"] = await seed_subject(conn, "person", None, [("passport", PB)])
    ids["c"] = await seed_subject(conn, "company", None, [("passport", PC)])
    ids["d"] = await seed_subject(conn, "person", None, [("passport", PD)], merged=ids["a"])
    return ids


def _two(ids, kind="person", **over):
    return payload(
        [entry(LIVE1, "drive_folder"), entry(LIVE2, "whatsapp_phone")], ids=ids, kind=kind, **over
    )


def _patch_parser(mp):
    def boom(cls, *a, **k):
        raise RuntimeError(f"boom {LIVE1} {PA}")

    mp.setattr(route_module.RelatedLinksRegisterRequest, "model_validate_json", classmethod(boom))


def _patch_database(mp):
    async def boom(*a, **k):
        raise asyncpg.PostgresError(f"boom {LIVE1} {PA}")

    mp.setattr(route_module, "record_links", boom)


def _refused(reason):
    return {"outcome": "subject_not_registered", "reason": reason}


def _not_found(sid, indexes):
    return {"outcome": "client_not_found", "subject_id": sid, "indexes": indexes}


def _probe_body(mutate):
    """The probe body of family B: the complete key set (two identifiers) and two links, entry 0
    the one a mutator changes, entry 1 a valid link of another client carrying the probe id."""
    return mutate(copy.deepcopy(BASE_BODY))


def _case(
    build, status, *, service=False, route=False, quiet=False, patch=None, client=True, ids=True
):
    """build(world) -> list of (request, exact expected JSON body). `client` / `ids` say whether the
    request carries the probe client id / a probe identifier; a case may declare ONE absent."""
    assert client or ids, "a case cannot lack both probes"
    return {
        "build": build, "status": status, "service": service, "route": route, "quiet": quiet,
        "patch": patch, "client": client, "ids": ids,
    }  # fmt: skip


def _a(build, **kw):
    return _case(build, 200, service=True, **kw)


def _validation(loc_codes_):
    return {"detail": [{"loc": loc, "code": code} for loc, code in loc_codes_]}


# Family A: cases that reach the service (12 cases, 13 calls)
FAMILY_A = {
    "a_success_dry_run": _a(
        lambda w: [(_two((("passport", PB),), dry_run=True), _recorded(w["b"], 2, 0, True))]
    ),
    "a_success_apply_then_again": _a(
        lambda w: [
            (_two((("passport", PB),)), _recorded(w["b"], 2, 0, False)),
            (_two((("passport", PB),)), _recorded(w["b"], 0, 2, False)),
        ]
    ),
    "a_none_owned": _a(lambda w: [(_two((("passport", P_UNKNOWN),)), _refused("none_owned"))]),
    "a_two_owners": _a(
        lambda w: [(_two((("passport", PA), ("passport", PB))), _refused("two_owners"))]
    ),
    "a_set_differs_subset": _a(lambda w: [(_two((("passport", PA),)), _refused("set_differs"))]),
    "a_set_differs_superset": _a(
        lambda w: [(_two((*FULL_A, ("passport", P_EXTRA))), _refused("set_differs"))]
    ),
    "a_partial_overlap": _a(
        lambda w: [(_two((("passport", PA), ("passport", P_EXTRA))), _refused("partial_overlap"))]
    ),
    "a_kind_differs": _a(lambda w: [(_two((("passport", PC),)), _refused("kind_differs"))]),
    "a_merged": _a(lambda w: [(_two((("passport", PD),)), _refused("merged"))]),
    "a_client_not_found_one": _a(
        lambda w: [
            (
                payload([entry(LIVE1), entry(MISSING1, "identity")], ids=(("passport", PB),)),
                _not_found(w["b"], [1]),
            )
        ]
    ),
    "a_client_not_found_several": _a(
        lambda w: [
            (
                payload(
                    [entry(MISSING1), entry(LIVE1, "identity"), entry(MISSING2)],
                    ids=(("passport", PB),),
                ),
                _not_found(w["b"], [0, 2]),
            )
        ]
    ),
    "a_client_not_found_soft_deleted": _a(
        lambda w: [
            (
                payload([entry(GONE), entry(LIVE1, "identity")], ids=(("passport", PB),)),
                _not_found(w["b"], [0]),
            )
        ]
    ),
}

# Family B: every entry of L9_CASES, applied to the probe body, plus the three raw/duplicate cases
FAMILY_B = {}
for _key, (_mutator, _expected) in L9_CASES.items():
    FAMILY_B["b_" + _key] = _case(
        lambda w, _m=_mutator, _e=_expected: [(_probe_body(_m), _validation(_e))],
        422,
        quiet=True,
        # these mutations remove every submitted link (no_links, links_is_not_a_list) or the whole
        # subject (missing_subject): the request cannot carry the client probe / an identifier probe
        client=_key not in {"no_links", "links_is_not_a_list"},
        ids=_key != "missing_subject",
    )
FAMILY_B["b_duplicate_link"] = _case(
    lambda w: [
        (
            payload(
                [entry(LIVE1, "identity", files=1), entry(LIVE1, "identity", files=9)], ids=FULL_A
            ),
            _validation([([], "duplicate_link")]),
        )
    ],
    422,
    quiet=True,
)
FAMILY_B["b_malformed_json"] = _case(
    lambda w: [
        (
            b'{"links": [{"client_id": '
            + str(LIVE1).encode()
            + b'}], "subject": "'
            + PA.encode()
            + b'"',
            _validation([([], "json_invalid")]),
        )
    ],
    422,
    quiet=True,
)
FAMILY_B["b_oversize_body"] = _case(
    lambda w: [
        (
            b"x" * (route_module.MAX_BODY_BYTES + 1)
            + b" "
            + str(LIVE1).encode()
            + b" "
            + PA.encode(),
            _validation([(["body"], "too_long")]),
        )
    ],
    422,
    quiet=True,
)

# Family C: injected failures (2)
FAMILY_C = {
    "c_parser_failure": _case(
        lambda w: [(_two(FULL_A), {"detail": "internal_error"})],
        500,
        route=True,
        patch=_patch_parser,
    ),
    "c_database_failure": _case(
        lambda w: [(_two(FULL_A), {"detail": "database_error"})],
        503,
        route=True,
        patch=_patch_database,
    ),
}

L13_CASES = {**FAMILY_A, **FAMILY_B, **FAMILY_C}
FAKE_WORLD = {"a": 1, "b": 2, "c": 3, "d": 4}


def test_l13_the_matrix_has_every_case_of_its_specification():
    assert len(FAMILY_A) == 12 and len(FAMILY_C) == 2
    assert len(FAMILY_B) == len(L9_CASES) + 3
    assert len(L13_CASES) == len(L9_CASES) + 17
    calls = sum(len(spec["build"](FAKE_WORLD)) for spec in L13_CASES.values())
    assert calls == 13 + len(L9_CASES) + 3 + 2
    for case, spec in L13_CASES.items():
        steps = spec["build"](FAKE_WORLD)
        requests = [request for request, _ in steps]
        # no word of the fixed answers, the fixed log lines or the endpoint path is forbidden
        tokens = set(UNCONDITIONAL)
        for request in requests:
            if isinstance(request, dict):
                tokens |= _forbidden(request, answer_words=False)
        assert tokens & FIXED_WORDS == set(), (case, sorted(tokens & FIXED_WORDS))
        # suppression guard: the case-folded substring match of the answer vocabulary must not
        # remove any non-protocol request string, or any probe identifier, from either forbidden set
        for request in requests:
            if not isinstance(request, dict):
                continue
            strings: set[str] = set()
            _leaves(request, strings, set())
            log_set = _forbidden(request, answer_words=False)
            answer_set = _forbidden(request, answer_words=True)
            for leaf in strings:
                if len(leaf) < 4 or leaf in PROTOCOL_WORDS or leaf == PLAN:
                    continue
                forms = {
                    leaf.casefold(),
                    " ".join(leaf.upper().split()).casefold(),
                    canonical_form("passport", leaf).casefold(),
                } - {""}
                assert forms <= log_set, (case, "log set lacks", leaf)
                assert forms <= answer_set, (case, "response/audit set lacks", leaf)
            for ident in ALL_PROBE_IDS:
                if ident.casefold() in json.dumps(request).casefold():
                    assert ident.casefold() in log_set and ident.casefold() in answer_set, (
                        case,
                        ident,
                    )
        # the declared probes (identifiers and client ids put into the body, dictionary or raw
        # bytes) are in BOTH forbidden sets of every case, in all three forms
        declared = _declared_probes(requests)
        log_set, answer_set = _case_sets(requests)
        assert declared and declared <= log_set and declared <= answer_set, case
        if spec["ids"]:
            assert any(_forms(i) <= declared for i in ALL_PROBE_IDS), (case, "no identifier probe")
        if spec["client"]:
            assert _forms(str(LIVE1)) <= declared, (case, "no client probe")
        # an unconditional probe is no legitimate value of its case and a substring of none
        legitimate: set[str] = {str(spec["status"])} | set(PROTOCOL_WORDS) | set(FIXED_WORDS)
        for _, expected in steps:
            strings: set[str] = set()
            ints: set[int] = set()
            _leaves(expected, strings, ints)
            legitimate |= {s.casefold() for s in strings} | {str(i) for i in ints}
            text = json.dumps(expected).casefold()
            for probe in UNCONDITIONAL:
                assert probe not in text, (case, probe)
        for probe in UNCONDITIONAL:
            # the identifier probes cannot hide a client probe, nor the other way round
            assert all(probe not in canonical_form("passport", i).casefold() for i in ALL_PROBE_IDS)
            for value in legitimate:
                assert probe != value and probe not in value, (case, probe, value)


def _raw_text(request) -> str:
    return request.decode("latin-1") if isinstance(request, bytes) else json.dumps(request)


def _spoke(records, module):
    return [r for r in records if r.name == module.__name__ and SENTINEL not in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(L13_CASES))
async def test_l13_links_route_logs_and_answers_carry_no_client_id_name_identifier_or_body(
    case, client, conn, probe_world, caplog, monkeypatch, writes
):
    writes.add(LINKS_INSERT)
    caplog.set_level(logging.DEBUG)
    spec = L13_CASES[case]
    steps = spec["build"](probe_world)
    requests = [request for request, _ in steps]

    # 1. the probes are really in the request text; the client NAME is in no request
    for request in requests:
        raw = _raw_text(request)
        assert spec["client"] == (str(LIVE1) in raw), (case, "client probe")
        assert spec["ids"] == any(i.casefold() in raw.casefold() for i in ALL_PROBE_IDS), (
            case,
            "identifier probe",
        )
        assert PROBE_CLIENT_WORD.casefold() not in raw.casefold(), (case, "client name in request")

    # 2. the capture is live for both modules, at any level
    logging.getLogger(route_module.__name__).debug(SENTINEL)
    logging.getLogger(links_module.__name__).debug(SENTINEL)
    live = {r.name for r in caplog.records if SENTINEL in r.getMessage()}
    assert live == {route_module.__name__, links_module.__name__}, case

    if spec["patch"]:
        spec["patch"](monkeypatch)
    responses = [await post(client, request) for request in requests]

    # 3. status and exact body of every response
    for resp, (_, expected) in zip(responses, steps, strict=True):
        assert resp.status_code == spec["status"], (case, resp.status_code, resp.text[:120])
        assert resp.json() == expected, case

    # 4. the logger that must have spoken did; a validation case speaks through neither
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    if spec["service"]:
        assert _spoke(records, links_module), (case, "service logger silent")
    if spec["route"]:
        assert _spoke(records, route_module), (case, "route logger silent")
    if spec["quiet"]:
        assert not _spoke(records, links_module) and not _spoke(records, route_module), case

    # 5. no forbidden token in any record, response or audit row; the log token set is populated
    log_leaf_tokens: set[str] = set()
    for request in requests:
        if isinstance(request, dict):
            log_leaf_tokens |= _forbidden(request, answer_words=False)
    if all(isinstance(r, dict) for r in requests):
        if spec["client"]:
            assert str(LIVE1) in log_leaf_tokens, case
        if spec["ids"]:
            assert any(i.casefold() in log_leaf_tokens for i in ALL_PROBE_IDS), case
    log_tokens, answer_tokens = _case_sets(requests)
    for resp in responses:
        response_text = resp.text.casefold()
        assert [t for t in answer_tokens if t in response_text] == [], case
    log_text = " ".join(
        _record_text(r) for r in records if SENTINEL not in r.getMessage()
    ).casefold()
    assert [t for t in log_tokens if t in log_text] == [], case

    # 6. one audit row per call, no request body, no token
    rows, audit_text, excluded = await _audit_scan(conn)
    assert excluded == {"created_at"}, (
        excluded
    )  # the one timestamp column; a new text column is scanned
    assert len(rows) == len(requests), case
    assert all(r["request_body"] is None for r in rows), case
    assert [t for t in answer_tokens if t in audit_text] == [], case


@pytest.mark.asyncio
async def test_l13_the_read_routes_log_no_client_id_or_client_name(admin, conn, caplog):
    caplog.set_level(logging.DEBUG)
    await seed_client(conn, 424250, "ZzReadProbeClient")
    sid = await seed_subject(conn, "person", "ZzReadProbeReg", [("passport", "ZZ7373750")])
    await seed_link(conn, sid, 424250)
    listed = await admin.get(READ, params={"linked": "true", "q": "zzreadprobereg"})
    detail = await admin.get(f"{READ}/{sid}")
    assert listed.status_code == 200 and detail.status_code == 200
    # the responses carry the client by contract (L11); the logs do not
    assert detail.json()["links"][0]["client_name"] == "ZzReadProbeClient"
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert any(r.name == read_module.__name__ for r in records)
    text = " ".join(_record_text(r) for r in records).casefold()
    assert "424250" not in text and "zzreadprobeclient" not in text
    audit = await conn.fetch("SELECT query_params, request_body FROM api_audit_trail")
    audit_text = " ".join(str(dict(r)) for r in audit).casefold()
    assert "424250" not in audit_text and "zzreadprobeclient" not in audit_text
