"""Related-subjects registry, slice 4C-1: the registry's reads follow a merge, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362, 365, 367 and 369
forward parts, a minimal ``clients`` table and the audit table), seeds invented rows (``Zz`` / ``ZZ``
conventions, client ids from 424242 up) and calls the real read service and routers behind the real
middleware stack. Nothing here is a real person, company, client or document.

There is no write route in this pull request: the pointer (``related_subjects.merged_into``) and the
history rows are seeded with plain SQL inside ``unmeasured``. Cases M1 and M12 to M15 are those of the
specification; the module is extended by the pull request that adds the merge route. Every catalog
read is scoped to ``current_schema()``.
"""

from __future__ import annotations

import ast
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
from backend.app.core.config import settings
from backend.db.migration_base import split_migration_sql
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm import related_registry_read
from backend.services.crm.related_identifiers import canonical_form
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

BACKEND = Path(route_module.__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "db" / "migrations_v2"
MIGRATION_MERGES = MIGRATIONS / "369_related_subject_merges.sql"
READ = "/api/crm/related-registry"
URL_DOCUMENTS = "/api/crm/related-subjects/documents/register"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
PLAN = "cd34" * 16

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}
AUDIT_INSERT = ("api_audit_trail", "INSERT")

LIVE1, LIVE2, GONE = 424242, 424243, 424244
NAME_LIVE1 = "ZzClientOne"

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)

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
    schema = "related_merges_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    # Production-shaped: the same `init` hook every production pool registers (the jsonb codec),
    # `statement_cache_size=0`, `max_inactive_connection_lifetime=30.0` and the API pool's
    # `command_timeout=60`. The pool is built here from the factory's own init hook because the
    # factory takes no `server_settings` and this module needs its per-test schema.
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
                "369_related_subject_merges.sql",
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


async def seed_subject(c, kind, name, ids, *, merged=None, at=T0):
    async with unmeasured(c):
        sid = await c.fetchval(
            "INSERT INTO related_subjects (kind, display_name, merged_into, evidence, created_by, "
            "created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4) RETURNING id",
            kind, name, merged, at,
        )  # fmt: skip
        for id_type, raw in ids:
            await c.execute(
                "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, "
                "norm_version, subject_id) VALUES ($1, $2, $3, 1, $4)",
                id_type, canonical_form(id_type, raw), raw, sid,
            )  # fmt: skip
    return sid


async def seed_name(c, sid, name, source="ocr_single", *, at=T0):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_names (subject_id, name, source, evidence, created_by, "
            "created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
            sid, name, source, at,
        )  # fmt: skip


def sha_of(tag: int) -> str:
    return f"{tag:064x}"


async def seed_document(c, sid, tag, *, at=T0, doc_type="passport", ref=None):
    sha = sha_of(tag)
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
            "first_channel, evidence, created_by) VALUES ($1, 1234, 'application/pdf', "
            "DATE '2026-01-05', 'drive', '{}'::jsonb, 'zz-seed') ON CONFLICT DO NOTHING",
            sha,
        )
        if ref is not None:
            await c.execute(
                "INSERT INTO related_document_sightings (kind, ref, sha256, seen_on, created_by) "
                "VALUES ('drive', $1, $2, DATE '2026-01-06', 'zz-seed') ON CONFLICT DO NOTHING",
                ref, sha,
            )  # fmt: skip
        await c.execute(
            "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
            "created_by, created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
            sid, sha, doc_type, at,
        )  # fmt: skip


async def seed_client(c, client_id, name, *, deleted=False):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO clients (id, full_name, deleted_at) OVERRIDING SYSTEM VALUE "
            "VALUES ($1, $2, CASE WHEN $3 THEN now() END)",
            client_id, name, deleted,
        )  # fmt: skip


async def seed_link(c, sid, client_id, basis="identity", *, at=T0):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_links (subject_id, client_id, basis, evidence, "
            "created_by, created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
            sid, client_id, basis, at,
        )  # fmt: skip


async def seed_history(c, source, target, basis="permit_stamp", *, at=T0):
    """A history row, with the evidence bound as a NATIVE dict (the production codec encodes it)."""
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_merges (source_id, target_id, basis, evidence, "
            "created_by, created_at) VALUES ($1, $2, $3, $4, 'zz-seed', $5)",
            source, target, basis, {"plan_sha256": PLAN, "files": 1}, at,
        )  # fmt: skip


@pytest_asyncio.fixture
async def clients(conn):
    await seed_client(conn, LIVE1, NAME_LIVE1)
    await seed_client(conn, LIVE2, "ZzClientTwo")
    await seed_client(conn, GONE, "ZzClientGone", deleted=True)


# ── a real-stack app ──────────────────────────────────────────────────────────


class _Alerts:
    async def send_http_error_alert(self, **kwargs):
        return None

    async def send_latency_alert(self, **kwargs):
        return None


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True, raising=False)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)


def as_identity(monkeypatch, identity):
    async def fake(self, request):
        return dict(identity) if identity else None

    monkeypatch.setattr(HybridAuthMiddleware, "authenticate_request", fake)


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
async def admin(pool, auth_on, audit_logger, monkeypatch):
    as_identity(monkeypatch, ADMIN)
    async with make_client(pool) as c:
        yield c


@pytest_asyncio.fixture
async def writer(pool, enabled, auth_on, audit_logger, monkeypatch):
    as_identity(monkeypatch, None)
    async with make_client(pool) as c:
        yield c


def ids_of(resp):
    return [r["id"] for r in resp.json()["items"]]


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


# ── M1: the migration ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m1_migration_table_constraints_indexes_cascade_and_rollback(pool, conn):
    survivor = await seed_subject(conn, "person", "ZzM1 Survivor", [("passport", "ZZ4242001")])
    source = await seed_subject(
        conn, "person", None, [("permit", "ZZ42PERMIT0001Z")], merged=survivor
    )
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'related_subject_merges'"
        )
    }
    assert cols == {
        "id": ("bigint", "NO"),
        "source_id": ("bigint", "NO"),
        "target_id": ("bigint", "NO"),
        "basis": ("text", "NO"),
        "evidence": ("jsonb", "NO"),
        "created_by": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    }
    identity = await conn.fetchrow(
        "SELECT is_identity, identity_generation FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'related_subject_merges' "
        "AND column_name = 'id'"
    )
    assert (identity["is_identity"], identity["identity_generation"]) == ("YES", "ALWAYS")

    # plain indexes on source_id and target_id; no unique index but the primary key
    indexes = {
        r["indexname"]: r["indexdef"]
        for r in await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = 'related_subject_merges'"
        )
    }
    assert indexes["idx_related_subject_merges_source"].endswith("(source_id)")
    assert indexes["idx_related_subject_merges_target"].endswith("(target_id)")
    unique = await conn.fetch(
        "SELECT c.relname, pg_get_indexdef(i.indexrelid) AS def FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = current_schema() AND t.relname = 'related_subject_merges' "
        "AND i.indisunique"
    )
    assert [r["def"].rsplit("(", 1)[1] for r in unique] == ["id)"], [r["def"] for r in unique]
    assert not any("UNIQUE" in d for n, d in indexes.items() if n.startswith("idx_"))

    # the partial index of the pointer, its predicate read from pg_index in this schema
    partial = await conn.fetchrow(
        "SELECT i.indisunique, pg_get_expr(i.indpred, i.indrelid) AS predicate, "
        "pg_get_indexdef(i.indexrelid) AS def FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = current_schema() AND t.relname = 'related_subjects' "
        "AND c.relname = 'idx_related_subjects_merged_into'"
    )
    assert partial is not None and partial["indisunique"] is False
    assert partial["predicate"] == "(merged_into IS NOT NULL)"
    assert "(merged_into)" in partial["def"]

    # foreign keys: both ends point at the registry, with ON DELETE CASCADE; the pointer's own
    # foreign key (361) has no ON DELETE action
    fks = {
        (r["col"], r["target"]): r["action"]
        for r in await conn.fetch(
            "SELECT a.attname AS col, r.relname AS target, c.confdeltype::text AS action "
            "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_class r ON r.oid = c.confrelid "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.contype = 'f' AND n.nspname = current_schema() "
            "AND t.relname = 'related_subject_merges'"
        )
    }
    assert fks == {
        ("source_id", "related_subjects"): "c",
        ("target_id", "related_subjects"): "c",
    }
    pointer_fk = await conn.fetchval(
        "SELECT c.confdeltype::text FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_attribute a "
        "ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] WHERE c.contype = 'f' "
        "AND n.nspname = current_schema() AND t.relname = 'related_subjects' "
        "AND a.attname = 'merged_into'"
    )
    assert pointer_fk == "a"

    insert = (
        "INSERT INTO related_subject_merges (source_id, target_id, basis, evidence, created_by) "
        "VALUES ($1, $2, $3, $4::jsonb, 'zz')"
    )
    other = await seed_subject(conn, "person", "ZzM1 Other", [("passport", "ZZ4242002")])
    async with unmeasured(conn):
        await conn.execute(insert, source, survivor, "permit_stamp", {})
        # history: a second row for the same source, with the same or another target, is accepted
        await conn.execute(insert, source, survivor, "permit_stamp", {"again": 1})
        await conn.execute(insert, source, other, "same_identity", {})
        # the database admits what a later slice will write
        await conn.execute(insert, other, survivor, "manual", {})
        refused = [
            (asyncpg.CheckViolationError, (source, survivor, "guessed", {})),
            (asyncpg.CheckViolationError, (source, survivor, "permit_stamp", "a string")),
            (asyncpg.CheckViolationError, (source, survivor, "permit_stamp", [])),
            (asyncpg.CheckViolationError, (source, source, "permit_stamp", {})),
            (asyncpg.ForeignKeyViolationError, (source + 999, survivor, "permit_stamp", {})),
            (asyncpg.ForeignKeyViolationError, (source, survivor + 999, "permit_stamp", {})),
        ]
        for error, args in refused:
            with pytest.raises(error):
                await conn.execute(insert, *args)
        assert await conn.fetchval("SELECT count(*) FROM related_subject_merges") == 4

        # deleting a survivor that still has a pointing record is REFUSED by 361's foreign key;
        # the history row survives
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute("DELETE FROM related_subjects WHERE id = $1", survivor)
        assert await conn.fetchval("SELECT count(*) FROM related_subject_merges") == 4
        assert await conn.fetchval("SELECT count(*) FROM related_subjects WHERE id = $1", survivor)

        # deleting a merged-away source cascades its history rows (and only those)
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", source)
        rows = await conn.fetch("SELECT source_id, target_id FROM related_subject_merges")
        assert [(r["source_id"], r["target_id"]) for r in rows] == [(other, survivor)]


@pytest.mark.asyncio
async def test_m1_rollback_drops_the_table_and_the_partial_index_and_nothing_else(pool, conn):
    survivor = await seed_subject(conn, "person", "ZzM1b Survivor", [("passport", "ZZ4242011")])
    async with unmeasured(conn):
        # the column, its foreign key and CHECK and a seeded pointer stay
        member = await conn.fetchval(
            "INSERT INTO related_subjects (kind, merged_into, evidence, created_by) "
            "VALUES ('person', $1, '{}'::jsonb, 'zz') RETURNING id",
            survivor,
        )
        _, rollback = split_migration_sql(MIGRATION_MERGES.read_text())
        assert rollback
        # it removes the partial index and the table, and nothing else: no ALTER, no UPDATE of a
        # pointer, no touch of the column or its constraints
        assert re.findall(r"DROP\s+(\w+)\s+IF\s+EXISTS\s+(\w+)", rollback) == [
            ("INDEX", "idx_related_subjects_merged_into"),
            ("TABLE", "related_subject_merges"),
        ]
        assert not re.search(r"\b(ALTER|UPDATE|DELETE|TRUNCATE)\b", rollback, re.I)
        await conn.execute(rollback)
        present = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE 'related\\_%'"
            )
        }
        assert "related_subject_merges" not in present
        assert {"related_subjects", "related_subject_names", "related_subject_links"} <= present
        left = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = 'idx_related_subjects_merged_into'"
        )
        assert left == 0
        assert (
            await conn.fetchval("SELECT merged_into FROM related_subjects WHERE id = $1", member)
            == survivor
        )
        constraints = await conn.fetchval(
            "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace WHERE n.nspname = current_schema() "
            "AND t.relname = 'related_subjects' AND c.contype IN ('f', 'c')"
        )
        assert constraints >= 2
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE related_subjects SET merged_into = id WHERE id = $1", member)


# ── M12: the list follows a merge ─────────────────────────────────────────────

REF1 = "ZzDriveFileMerge0001"


async def list_world(conn):
    """Three survivors with one merged-in record each, and a control subject in no merge."""
    w: dict[str, int] = {}
    w["s1"] = await seed_subject(conn, "person", "ZzSurvivor One", [("passport", "ZZ4242101")])
    w["m1"] = await seed_subject(
        conn, "person", "ZzMemberAlpha Stamp", [("permit", "ZZ42STAMP0001Z")],
        merged=w["s1"], at=T0 + DAY,
    )  # fmt: skip
    await seed_name(conn, w["m1"], "ZzRecordedAlpha")
    w["s2"] = await seed_subject(conn, "person", "ZzSurvivor Two", [("passport", "ZZ4242102")])
    w["m2"] = await seed_subject(
        conn, "person", "ZzWild%_Member", [("permit", "ZZ42STAMP0002Z")], merged=w["s2"]
    )
    w["s3"] = await seed_subject(conn, "company", "ZzCompany Three", [("nib", "9994242001")])
    w["m3"] = await seed_subject(
        conn, "company", "ZzCompanyMember", [("nib", "9994242002")], merged=w["s3"]
    )
    w["t"] = await seed_subject(conn, "person", "ZzOther Control", [("passport", "ZZ4242104")])
    await seed_document(conn, w["s1"], 1)
    await seed_document(conn, w["s1"], 2)
    await seed_document(conn, w["m1"], 2)
    await seed_document(conn, w["m1"], 3)
    await seed_link(conn, w["s1"], LIVE1, "identity")
    await seed_link(conn, w["m1"], LIVE1, "identity")
    await seed_link(conn, w["m1"], GONE, "drive_folder")
    await seed_link(conn, w["m2"], LIVE2, "identity")
    return w


# By effective name: S3, T, S1, S2
ORDER = ["s3", "t", "s1", "s2"]


@pytest.mark.asyncio
async def test_m12_the_survivor_is_found_by_a_name_on_a_merged_in_record(admin, conn, clients):
    w = await list_world(conn)
    for q in ("ZzMemberAlpha", "zzmemberalpha stamp", "ZZMEMBERALPHA"):
        resp = await admin.get(READ, params={"q": q})
        assert ids_of(resp) == [w["s1"]], q
        assert resp.json()["total"] == 1
    # a recorded name of the merged-in record
    resp = await admin.get(READ, params={"q": "zzrecordedalpha"})
    assert ids_of(resp) == [w["s1"]] and resp.json()["total"] == 1
    # wildcards are literal: only the record whose name holds `%_` matches
    resp = await admin.get(READ, params={"q": "%_"})
    assert ids_of(resp) == [w["s2"]] and resp.json()["total"] == 1
    resp = await admin.get(READ, params={"q": "%%"})
    assert resp.json()["total"] == 0
    # the survivor's own name keeps finding it; the merged-away records are never listed
    resp = await admin.get(READ, params={"q": "ZzSurvivor One"})
    assert ids_of(resp) == [w["s1"]]


@pytest.mark.asyncio
async def test_m12_the_survivor_is_found_by_an_identifier_of_a_merged_in_record(
    admin, conn, clients
):
    w = await list_world(conn)
    for q in ("ZZ42STAMP0001Z", "zz42stamp0001z", "ZZ 42-STAMP0001", "ZZ42STA"):
        resp = await admin.get(READ, params={"q": q, "limit": 100})
        found = ids_of(resp)
        assert w["s1"] in found, q
        assert w["m1"] not in found and w["m2"] not in found
    resp = await admin.get(READ, params={"q": "ZZ42STAMP0001Z"})
    assert ids_of(resp) == [w["s1"]] and resp.json()["total"] == 1
    resp = await admin.get(READ, params={"q": "ZZ42STAMP0002Z"})
    assert ids_of(resp) == [w["s2"]]
    # the survivor's own identifier still finds it
    resp = await admin.get(READ, params={"q": "ZZ4242101"})
    assert ids_of(resp) == [w["s1"]]


@pytest.mark.asyncio
async def test_m12_merged_away_records_stay_out_of_items_and_total(admin, conn, clients):
    w = await list_world(conn)
    resp = await admin.get(READ, params={"limit": 100})
    assert ids_of(resp) == [w[k] for k in ORDER] and resp.json()["total"] == 4
    for key in ("m1", "m2", "m3"):
        assert w[key] not in ids_of(resp)
    kind = await admin.get(READ, params={"kind": "person", "limit": 100})
    assert ids_of(kind) == [w["t"], w["s1"], w["s2"]] and kind.json()["total"] == 3


@pytest.mark.asyncio
async def test_m12_row_identifiers_are_the_union_with_from_subject_id(admin, conn, clients):
    w = await list_world(conn)
    rows = {r["id"]: r for r in (await admin.get(READ, params={"limit": 100})).json()["items"]}
    assert rows[w["s1"]]["identifiers"] == [
        {"type": "passport", "value": "ZZ4242101", "from_subject_id": None},
        {"type": "permit", "value": "ZZ42STAMP0001Z", "from_subject_id": w["m1"]},
    ]
    assert rows[w["s3"]]["identifiers"] == [
        {"type": "nib", "value": "9994242001", "from_subject_id": None},
        {"type": "nib", "value": "9994242002", "from_subject_id": w["m3"]},
    ]
    assert rows[w["t"]]["identifiers"] == [
        {"type": "passport", "value": "ZZ4242104", "from_subject_id": None}
    ]
    assert rows[w["s1"]]["kind"] == "person" and rows[w["s3"]]["kind"] == "company"


@pytest.mark.asyncio
async def test_m12_counts_a_shared_document_and_a_shared_link_once(admin, conn, clients):
    w = await list_world(conn)
    rows = {r["id"]: r for r in (await admin.get(READ, params={"limit": 100})).json()["items"]}
    # sha 1 and 2 on the survivor, 2 and 3 on the merged-in record: three distinct documents
    assert rows[w["s1"]]["document_count"] == 3
    # (LIVE1, identity) on both, (GONE, drive_folder) on the merged-in one: two distinct links,
    # the soft-deleted client's one counted as well
    assert rows[w["s1"]]["links_count"] == 2
    assert rows[w["s2"]]["links_count"] == 1 and rows[w["s2"]]["document_count"] == 0
    assert rows[w["t"]]["links_count"] == 0 and rows[w["s3"]]["links_count"] == 0


@pytest.mark.asyncio
async def test_m12_linked_keeps_a_survivor_whose_only_link_is_on_a_merged_in_record(
    admin, conn, clients
):
    w = await list_world(conn)
    yes = await admin.get(READ, params={"linked": "true", "limit": 100})
    assert ids_of(yes) == [w["s1"], w["s2"]] and yes.json()["total"] == 2
    no = await admin.get(READ, params={"linked": "false", "limit": 100})
    assert ids_of(no) == [w["s3"], w["t"]] and no.json()["total"] == 2
    both = await admin.get(READ, params={"linked": "true", "q": "wild", "kind": "person"})
    assert ids_of(both) == [w["s2"]]
    none = await admin.get(READ, params={"linked": "true", "kind": "company"})
    assert ids_of(none) == [] and none.json()["total"] == 0


@pytest.mark.asyncio
async def test_m12_q_and_kind_combine_and_total_and_paging_agree(admin, conn, clients):
    w = await list_world(conn)
    resp = await admin.get(READ, params={"kind": "company", "q": "zzcompanymember"})
    assert ids_of(resp) == [w["s3"]]
    resp = await admin.get(READ, params={"kind": "person", "q": "zzcompanymember"})
    assert ids_of(resp) == [] and resp.json()["total"] == 0
    pages = []
    for offset in range(5):
        page = await admin.get(READ, params={"limit": 1, "offset": offset})
        assert page.json()["total"] == 4
        pages.extend(ids_of(page))
    assert pages == [w[k] for k in ORDER]
    two = await admin.get(READ, params={"limit": 2, "offset": 1})
    assert ids_of(two) == [w["t"], w["s1"]] and two.json()["total"] == 4


# ── M13: the detail follows a merge ───────────────────────────────────────────


async def detail_of(conn, sid):
    return await related_registry_read.get_subject(conn, sid)


async def detail_world(conn):
    w: dict[str, int] = {}
    w["s"] = await seed_subject(conn, "person", "ZzDetail Survivor", [("passport", "ZZ4242301")])
    w["a"] = await seed_subject(
        conn, "person", "ZzDetail MemberA", [("permit", "ZZ42PERMIT0301Z")],
        merged=w["s"], at=T0 + DAY,
    )  # fmt: skip
    w["b"] = await seed_subject(
        conn, "person", None, [("passport", "ZZ4242302")], merged=w["s"], at=T0 + 2 * DAY
    )
    return w


@pytest.mark.asyncio
async def test_m13_identifiers_are_the_union_with_from_subject_id(conn, clients):
    w = await detail_world(conn)
    detail = await detail_of(conn, w["s"])
    assert detail["identifiers"] == [
        {"type": "passport", "value": "ZZ4242301", "from_subject_id": None},
        {"type": "permit", "value": "ZZ42PERMIT0301Z", "from_subject_id": w["a"]},
        {"type": "passport", "value": "ZZ4242302", "from_subject_id": w["b"]},
    ]


@pytest.mark.asyncio
async def test_m13_documents_one_per_sha256_own_link_kept_else_the_earliest(conn, clients):
    w = await detail_world(conn)
    # 1: on the survivor (late) and a member (early): the survivor's own link is kept
    await seed_document(conn, w["s"], 1, at=T0 + 5 * DAY, ref=REF1)
    await seed_document(conn, w["a"], 1, at=T0)
    # 2: on two members: the earliest link (member b) is kept
    await seed_document(conn, w["a"], 2, at=T0 + 3 * DAY)
    await seed_document(conn, w["b"], 2, at=T0 + 2 * DAY)
    # 3: on two members at the same instant: the lowest subject id (member a)
    await seed_document(conn, w["b"], 3, at=T0 + 4 * DAY)
    await seed_document(conn, w["a"], 3, at=T0 + 4 * DAY)
    detail = await detail_of(conn, w["s"])
    assert [(d["sha256"], d["from_subject_id"]) for d in detail["documents"]] == [
        (sha_of(1), None),
        (sha_of(2), w["b"]),
        (sha_of(3), w["a"]),
    ]
    assert detail["documents"][0]["drive_file_ids"] == [REF1]
    assert w["a"] < w["b"]


@pytest.mark.asyncio
async def test_m13_documents_are_ordered_by_type_then_hash_and_capped_at_200(conn, clients):
    w = await detail_world(conn)
    await seed_document(conn, w["a"], 9, doc_type="visa")
    await seed_document(conn, w["s"], 8, doc_type="passport")
    await seed_document(conn, w["b"], 7, doc_type="passport")
    detail = await detail_of(conn, w["s"])
    assert [(d["doc_type"], d["sha256"]) for d in detail["documents"]] == [
        ("passport", sha_of(7)),
        ("passport", sha_of(8)),
        ("visa", sha_of(9)),
    ]
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
            "first_channel, evidence, created_by) SELECT lpad(to_hex(1000 + g), 64, '0'), 1, "
            "'application/pdf', DATE '2026-01-05', 'drive', '{}'::jsonb, 'zz' "
            "FROM generate_series(1, 230) g"
        )
        await conn.execute(
            "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
            "created_by) SELECT $1, lpad(to_hex(1000 + g), 64, '0'), 'passport', '{}'::jsonb, 'zz' "
            "FROM generate_series(1, 230) g",
            w["a"],
        )
    detail = await detail_of(conn, w["s"])
    assert len(detail["documents"]) == 200


@pytest.mark.asyncio
async def test_m13_names_are_not_deduplicated_registration_first_then_recorded(conn, clients):
    w = await detail_world(conn)
    await seed_name(conn, w["s"], "ZzRecSurv", "ocr_single", at=T0 + 7 * DAY)
    await seed_name(conn, w["a"], "ZzRecA", "ocr_corroborated", at=T0 + 8 * DAY)
    await seed_name(conn, w["b"], "ZzRecB", "ocr_single", at=T0 + 6 * DAY)
    # the same text on a member too: not de-duplicated
    await seed_name(conn, w["a"], "ZzRecSurv", "ocr_single", at=T0 + 9 * DAY)
    detail = await detail_of(conn, w["s"])
    assert [(n["name"], n["source"], n["from_subject_id"]) for n in detail["names"]] == [
        ("ZzDetail Survivor", "registration", None),
        ("ZzDetail MemberA", "registration", w["a"]),
        ("ZzRecSurv", "ocr_single", w["a"]),
        ("ZzRecA", "ocr_corroborated", w["a"]),
        ("ZzRecSurv", "ocr_single", None),
        ("ZzRecB", "ocr_single", w["b"]),
    ]
    assert detail["names_total"] == 6
    assert detail["names"][0]["created_at"] == detail["created_at"]
    assert detail["names"][1]["created_at"] == T0 + DAY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "members,shown_registration,shown_recorded",
    [(5, 6, 14), (18, 19, 1), (19, 20, 0), (20, 20, 0)],
)
async def test_m13_names_cap_at_twenty_and_the_total_counts_every_candidate(
    conn, clients, members, shown_registration, shown_recorded
):
    survivor = await seed_subject(conn, "person", "ZzCap Survivor", [("passport", "ZZ4242401")])
    member_ids = []
    for i in range(members):
        member_ids.append(
            await seed_subject(
                conn,
                "person",
                f"ZzCap Member{i:02d}",
                [("passport", f"ZZ42425{i:02d}")],
                merged=survivor,
                at=T0 + (i + 1) * DAY,
            )  # fmt: skip
        )
    for i in range(25):
        await seed_name(conn, survivor, f"ZzCap Recorded{i:02d}", "ocr_single", at=T0 + i * DAY)
    detail = await detail_of(conn, survivor)
    registration = [n for n in detail["names"] if n["source"] == "registration"]
    recorded = [n for n in detail["names"] if n["source"] != "registration"]
    assert len(detail["names"]) == 20
    assert len(registration) == shown_registration and len(recorded) == shown_recorded
    # only the FIRST twenty registration entries are kept, the survivor's first, then the members'
    expected = [None] + member_ids
    assert [n["from_subject_id"] for n in registration] == expected[:shown_registration]
    # the total counts ALL registration entries plus ALL recorded names, with zero slots left too
    assert detail["names_total"] == (members + 1) + 25
    if shown_recorded:
        assert detail["names"][shown_registration]["name"] == "ZzCap Recorded24"


@pytest.mark.asyncio
async def test_m13_links_one_per_client_and_basis_earliest_kept_total_after_dedup(conn, clients):
    w = await detail_world(conn)
    await seed_link(conn, w["s"], LIVE1, "identity", at=T0 + 3 * DAY)
    await seed_link(conn, w["a"], LIVE1, "identity", at=T0 + DAY)
    await seed_link(conn, w["a"], LIVE2, "drive_folder", at=T0 + 2 * DAY)
    await seed_link(conn, w["b"], GONE, "whatsapp_phone", at=T0 + 4 * DAY)
    detail = await detail_of(conn, w["s"])
    assert [(x["client_id"], x["basis"], x["from_subject_id"]) for x in detail["links"]] == [
        (LIVE1, "identity", w["a"]),
        (LIVE2, "drive_folder", w["a"]),
        (GONE, "whatsapp_phone", w["b"]),
    ]
    assert detail["links_total"] == 3
    assert detail["links"][0]["client_name"] == NAME_LIVE1 and detail["links"][0]["client_live"]
    assert detail["links"][2]["client_name"] is None and not detail["links"][2]["client_live"]


@pytest.mark.asyncio
async def test_m13_links_show_twenty_and_the_total_is_after_deduplication(conn, clients):
    w = await detail_world(conn)
    for i in range(25):
        await seed_link(conn, w["a"], 424500 + i, "identity", at=T0 + i * DAY)
    for i in range(5):
        await seed_link(conn, w["b"], 424500 + i, "identity", at=T0 + (40 + i) * DAY)
    detail = await detail_of(conn, w["s"])
    assert detail["links_total"] == 25 and len(detail["links"]) == 20
    assert {x["from_subject_id"] for x in detail["links"]} == {w["a"]}


@pytest.mark.asyncio
async def test_m13_merged_from_is_ordered_with_the_latest_history_row_of_the_pair(conn, clients):
    s = await seed_subject(conn, "person", "ZzMF Survivor", [("passport", "ZZ4242601")])
    other = await seed_subject(conn, "person", "ZzMF Other", [("passport", "ZZ4242602")])
    a = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0601Z")], merged=s)
    b = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0602Z")], merged=s)
    c = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0603Z")], merged=s)
    d = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0604Z")], merged=s)
    await seed_history(conn, a, s, "manual", at=T0 + DAY)
    await seed_history(conn, a, s, "permit_stamp", at=T0 + 3 * DAY)
    await seed_history(conn, a, other, "same_identity", at=T0 + 9 * DAY)
    await seed_history(conn, b, s, "permit_stamp", at=T0 + 2 * DAY)
    # c and d: a pointer without a history row (an operator's statement); d has a stale row for a
    # pair that is not (d, its pointer)
    await seed_history(conn, d, other, "permit_stamp", at=T0 + 4 * DAY)
    detail = await detail_of(conn, s)
    assert detail["merged_from"] == [
        {"subject_id": b, "basis": "permit_stamp", "merged_at": T0 + 2 * DAY},
        {"subject_id": a, "basis": "permit_stamp", "merged_at": T0 + 3 * DAY},
        {"subject_id": c, "basis": None, "merged_at": None},
        {"subject_id": d, "basis": None, "merged_at": None},
    ]
    assert detail["merged_into"] is None


# the effective name over the union: rank first, then the survivor's OWN, then newest, highest id


@pytest.mark.asyncio
async def test_m13_a_merged_in_manual_name_beats_the_survivors_registration_name(conn, clients):
    s = await seed_subject(conn, "person", "ZzEff Own Registration", [("passport", "ZZ4242701")])
    m = await seed_subject(
        conn, "person", None, [("permit", "ZZ42PERMIT0701Z")], merged=s, at=T0 + DAY
    )
    await seed_name(conn, m, "ZzEff Manual On Member", "manual", at=T0 + 2 * DAY)
    detail = await detail_of(conn, s)
    assert (detail["display_name"], detail["name_source"]) == ("ZzEff Manual On Member", "manual")
    page = await related_registry_read.list_subjects(conn, q=None, kind=None, limit=10, offset=0)
    row = next(r for r in page["items"] if r["id"] == s)
    assert (row["display_name"], row["name_source"]) == ("ZzEff Manual On Member", "manual")


@pytest.mark.asyncio
async def test_m13_the_survivors_registration_name_beats_a_newer_merged_in_registration(
    conn, clients
):
    s = await seed_subject(conn, "person", "ZzEff Zeta Own", [("passport", "ZZ4242711")], at=T0)
    await seed_subject(
        conn, "person", "ZzEff Alpha Newer", [("permit", "ZZ42PERMIT0711Z")], merged=s,
        at=T0 + 5 * DAY,
    )  # fmt: skip
    detail = await detail_of(conn, s)
    assert (detail["display_name"], detail["name_source"]) == ("ZzEff Zeta Own", "registration")
    page = await related_registry_read.list_subjects(conn, q=None, kind=None, limit=10, offset=0)
    assert next(r for r in page["items"] if r["id"] == s)["display_name"] == "ZzEff Zeta Own"


@pytest.mark.asyncio
async def test_m13_the_survivors_own_ocr_single_name_loses_to_a_merged_in_registration(
    conn, clients
):
    s = await seed_subject(conn, "person", None, [("passport", "ZZ4242721")])
    await seed_name(conn, s, "ZzEff Own Ocr", "ocr_single", at=T0 + 9 * DAY)
    await seed_subject(
        conn, "person", "ZzEff Member Registration", [("permit", "ZZ42PERMIT0721Z")], merged=s,
        at=T0,
    )  # fmt: skip
    detail = await detail_of(conn, s)
    assert (detail["display_name"], detail["name_source"]) == (
        "ZzEff Member Registration",
        "registration",
    )


@pytest.mark.asyncio
async def test_m13_among_merged_in_ties_the_newest_then_the_highest_member_id_wins(conn, clients):
    newest_first = await seed_subject(conn, "person", None, [("passport", "ZZ4242731")])
    await seed_subject(
        conn, "person", "ZzTie Newest", [("permit", "ZZ42PERMIT0731Z")], merged=newest_first,
        at=T0 + 2 * DAY,
    )  # fmt: skip
    await seed_subject(
        conn, "person", "ZzTie Older Higher Id", [("permit", "ZZ42PERMIT0732Z")],
        merged=newest_first, at=T0 + DAY,
    )  # fmt: skip
    assert (await detail_of(conn, newest_first))["display_name"] == "ZzTie Newest"

    same_stamp = await seed_subject(conn, "person", None, [("passport", "ZZ4242741")])
    low = await seed_subject(
        conn, "person", "ZzTie Same Low", [("permit", "ZZ42PERMIT0741Z")], merged=same_stamp
    )
    high = await seed_subject(
        conn, "person", "ZzTie Same High", [("permit", "ZZ42PERMIT0742Z")], merged=same_stamp
    )
    assert low < high
    assert (await detail_of(conn, same_stamp))["display_name"] == "ZzTie Same High"
    page = await related_registry_read.list_subjects(conn, q=None, kind=None, limit=10, offset=0)
    assert next(r for r in page["items"] if r["id"] == same_stamp)["display_name"] == (
        "ZzTie Same High"
    )


@pytest.mark.asyncio
async def test_m13_a_merged_away_record_opened_by_id_answers_200_with_its_own_rows(
    admin, conn, clients
):
    s = await seed_subject(conn, "person", "ZzAway Survivor", [("passport", "ZZ4242801")])
    m = await seed_subject(
        conn, "person", "ZzAway Member", [("permit", "ZZ42PERMIT0801Z")], merged=s, at=T0 + DAY
    )
    await seed_document(conn, m, 31)
    await seed_name(conn, m, "ZzAway Recorded", "ocr_single")
    await seed_link(conn, m, LIVE1, "identity")
    resp = await admin.get(f"{READ}/{m}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["merged_into"] == s and body["merged_from"] == []
    assert [i["value"] for i in body["identifiers"]] == ["ZZ42PERMIT0801Z"]
    assert [i["from_subject_id"] for i in body["identifiers"]] == [None]
    assert [d["sha256"] for d in body["documents"]] == [sha_of(31)]
    assert [d["from_subject_id"] for d in body["documents"]] == [None]
    assert [n["name"] for n in body["names"]] == ["ZzAway Member", "ZzAway Recorded"]
    assert [x["from_subject_id"] for x in body["links"]] == [None]
    assert body["display_name"] == "ZzAway Member"
    survivor = (await admin.get(f"{READ}/{s}")).json()
    assert survivor["merged_into"] is None
    assert [x["subject_id"] for x in survivor["merged_from"]] == [m]
    assert survivor["merged_from"][0] == {"subject_id": m, "basis": None, "merged_at": None}


@pytest.mark.asyncio
async def test_m13_the_router_serialises_merged_from_dates_and_bases(admin, conn, clients):
    s = await seed_subject(conn, "person", "ZzSer Survivor", [("passport", "ZZ4242811")])
    m = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0811Z")], merged=s)
    await seed_history(conn, m, s, "permit_stamp", at=T0 + 3 * DAY)
    body = (await admin.get(f"{READ}/{s}")).json()
    assert body["merged_from"] == [
        {"subject_id": m, "basis": "permit_stamp", "merged_at": "2026-01-04T00:00:00Z"}
    ]


@pytest.mark.asyncio
async def test_m13_a_document_registered_after_the_merge_shows_under_the_survivor(
    writer, conn, clients, writes, monkeypatch
):
    s = await seed_subject(conn, "person", "ZzReg Survivor", [("passport", "ZZ4242821")])
    m = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0821Z")], merged=s)
    writes.update({("related_documents", "INSERT"), ("related_subject_documents", "INSERT")})
    body = {
        "subject": {
            "kind": "person",
            "identifiers": [{"type": "permit", "value": "ZZ42PERMIT0821Z"}],
        },
        "documents": [
            {
                "sha256": sha_of(77),
                "byte_size": 4096,
                "mime_type": "application/pdf",
                "doc_type": "permit",
                "first_seen_on": "2026-01-05",
                "first_channel": "whatsapp",
                "drive": [],
            }
        ],
        "evidence": {"plan_sha256": PLAN, "subject_ref": "REF-4242"},
        "dry_run": False,
    }
    resp = await writer.post(URL_DOCUMENTS, content=json.dumps(body), headers=HEADERS)
    assert resp.status_code == 200, resp.text
    as_identity(monkeypatch, ADMIN)
    detail = (await writer.get(f"{READ}/{s}")).json()
    assert [(d["sha256"], d["from_subject_id"]) for d in detail["documents"]] == [(sha_of(77), m)]
    assert detail["merged_from"][0]["subject_id"] == m


# ── M14: the read SQL budget ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [1, 50])
@pytest.mark.parametrize("params", [{}, {"linked": True}, {"linked": False}, {"q": "zz row"}])
async def test_m14_a_populated_list_page_runs_at_most_four_selects(conn, clients, rows, params):
    for i in range(rows):
        sid = await seed_subject(conn, "person", f"ZZ ROW {i:02d}", [("passport", f"ZZ77{i:05d}")])
        member = await seed_subject(
            conn, "person", None, [("permit", f"ZZ77PERMIT{i:05d}Z")], merged=sid
        )
        await seed_link(conn, member, LIVE1 if i % 2 == 0 else GONE)
        await seed_document(conn, member, 500 + i)
    counting = _CountingConn(conn)
    result = await related_registry_read.list_subjects(
        counting, q=params.get("q"), kind=None, limit=100, offset=0, linked=params.get("linked")
    )
    assert counting.statements <= 4
    if params.get("linked") is False:
        assert result["items"] == []
    else:
        assert len(result["items"]) == rows
        assert all(r["links_count"] == 1 and r["document_count"] == 1 for r in result["items"])
        assert all(len(r["identifiers"]) == 2 for r in result["items"])


@pytest.mark.asyncio
async def test_m14_the_detail_statement_counts(conn, clients):
    survivor = await seed_subject(conn, "person", "ZzB14 Survivor", [("passport", "ZZ4242901")])
    member = await seed_subject(
        conn, "person", None, [("permit", "ZZ42PERMIT0901Z")], merged=survivor
    )
    plain = await seed_subject(conn, "person", "ZzB14 Plain", [("passport", "ZZ4242902")])
    bare = await seed_subject(conn, "person", None, [("passport", "ZZ4242903")])

    async def count(sid):
        counting = _CountingConn(conn)
        assert await related_registry_read.get_subject(counting, sid) is not None
        return counting.statements

    # no documents: subject, members, identifiers, documents, names, links
    assert await count(survivor) == 6
    await seed_document(conn, member, 61)
    # a survivor with documents: one more (the sightings)
    assert await count(survivor) == 7
    # a subject in no merge keeps today's 5 and 6; so does a merged-away record opened by id
    assert await count(bare) == 5
    await seed_document(conn, plain, 62)
    assert await count(plain) == 6
    assert await count(member) == 6


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
WRITING_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|ALTER|DROP|CREATE)\b", re.I)


def test_m14_static_census_is_ten_plain_selects_without_the_merge_word_or_evidence():
    service = related_registry_read
    for sql in _sql_constants(service):
        assert not WRITING_SQL.search(sql), sql[:60]
        assert not re.search(r"\bWITH\b", sql, re.I), sql[:60]
        assert not re.search(r"\b(evidence|created_by)\b", sql, re.I), sql[:60]
    statements = {
        attr: value
        for attr, value in vars(service).items()
        if attr.endswith("_SQL") and attr not in FRAGMENTS
    }
    assert len(statements) == 10
    for attr, sql in statements.items():
        assert sql.lstrip().upper().startswith("SELECT"), attr
        assert not re.search(r"\bMERGE\b", sql, re.I), attr
    # the history table is read by one statement only, and never its evidence or its actor
    reading = [a for a, s in statements.items() if "related_subject_merges" in s]
    assert reading == ["_MERGES_SQL"]
    # the executed statements are exactly the module's constants (no inline SQL in a call)
    source = Path(service.__file__).read_text()
    assert not re.search(r"\.(execute|executemany|fetch|fetchrow|fetchval)\(\s*f?[\"']", source)
    # only the read module and its router name the history table in the application code
    named = [
        p.relative_to(BACKEND).as_posix()
        for base in ("app", "services")
        for p in (BACKEND / base).rglob("*.py")
        if "related_subject_merges" in p.read_text()
    ]
    assert named == ["services/crm/related_registry_read.py"]


# ── M15: a subject in no merge answers exactly as before, plus empty new fields ──


@pytest.mark.asyncio
async def test_m15_golden_bodies_of_a_subject_in_no_merge(admin, conn, clients):
    async with unmeasured(conn):
        subject = await conn.fetchval(
            "INSERT INTO related_subjects (kind, display_name, date_of_birth, nationality, sex, "
            "evidence, created_by, created_at) VALUES ('person', 'ZzGolden Person', "
            "DATE '1990-01-02', 'ZZ-land', 'F', '{}'::jsonb, 'zz-seed', $1) RETURNING id",
            T0,
        )
        await conn.execute(
            "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, "
            "subject_id) VALUES ('passport', $1, 'ZZ4242991', 1, $2)",
            canonical_form("passport", "ZZ4242991"), subject,
        )  # fmt: skip
    await seed_document(conn, subject, 1, ref=REF1)
    await seed_name(conn, subject, "ZzGolden Recorded", "manual", at=T0 + DAY)
    await seed_link(conn, subject, LIVE1, "identity", at=T0 + 2 * DAY)
    identifier = {"type": "passport", "value": "ZZ4242991", "from_subject_id": None}
    listed = await admin.get(READ, params={"limit": 100})
    assert listed.status_code == 200
    assert listed.json() == {
        "items": [
            {
                "id": subject,
                "kind": "person",
                "display_name": "ZzGolden Recorded",
                "name_source": "manual",
                "date_of_birth": "1990-01-02",
                "nationality": "ZZ-land",
                "identifiers": [identifier],
                "document_count": 1,
                "links_count": 1,
            }
        ],
        "total": 1,
        "limit": 100,
        "offset": 0,
    }
    detail = await admin.get(f"{READ}/{subject}")
    assert detail.status_code == 200
    assert detail.json() == {
        "id": subject,
        "kind": "person",
        "display_name": "ZzGolden Recorded",
        "name_source": "manual",
        "names": [
            {
                "name": "ZzGolden Person",
                "source": "registration",
                "created_at": "2026-01-01T00:00:00Z",
                "from_subject_id": None,
            },
            {
                "name": "ZzGolden Recorded",
                "source": "manual",
                "created_at": "2026-01-02T00:00:00Z",
                "from_subject_id": None,
            },
        ],
        "names_total": 2,
        "links": [
            {
                "client_id": LIVE1,
                "client_name": NAME_LIVE1,
                "client_live": True,
                "basis": "identity",
                "relation": "unknown",
                "created_at": "2026-01-03T00:00:00Z",
                "from_subject_id": None,
            }
        ],
        "links_total": 1,
        "date_of_birth": "1990-01-02",
        "nationality": "ZZ-land",
        "sex": "F",
        "created_at": "2026-01-01T00:00:00Z",
        "identifiers": [identifier],
        "documents": [
            {
                "sha256": sha_of(1),
                "doc_type": "passport",
                "mime_type": "application/pdf",
                "byte_size": 1234,
                "first_seen_on": "2026-01-05",
                "drive_file_ids": [REF1],
                "from_subject_id": None,
            }
        ],
        "merged_from": [],
        "merged_into": None,
    }


@pytest.mark.asyncio
async def test_m15_the_read_routes_log_counts_and_ids_only(admin, conn, clients, caplog):
    s = await seed_subject(conn, "person", "ZzLog Survivor", [("passport", "ZZ4242995")])
    m = await seed_subject(
        conn, "person", "ZzLog Member", [("permit", "ZZ42PERMIT0995Z")], merged=s
    )
    await seed_link(conn, m, LIVE1, "identity")
    caplog.set_level(logging.DEBUG)
    assert (await admin.get(f"{READ}/{s}")).status_code == 200
    assert (await admin.get(READ, params={"q": "ZzLog Member"})).status_code == 200
    mine = [r for r in caplog.records if r.name == read_module.__name__]
    assert len(mine) >= 2
    text = " ".join(logging.Formatter().format(r) for r in mine).casefold()
    for protected in ("zzlog", "zz4242995", "zz42permit0995z", NAME_LIVE1.casefold()):
        assert protected not in text
    assert "merged=1" in text
