"""Related-subjects registry, slice 4C: the registry's reads follow a merge (4C-1) and merges are
recorded through one sealed route (4C-2), on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362, 365, 367 and 369
forward parts, a minimal ``clients`` table and the audit table), seeds invented rows (``Zz`` / ``ZZ``
conventions, client ids from 424242 up) and calls the real read service and routers behind the real
middleware stack. Nothing here is a real person, company, client or document.

The read cases (M1, M12 to M15) seed the pointer (``related_subjects.merged_into``) and the history
rows with plain SQL inside ``unmeasured``. The route cases (M2 to M11, M16 to M18) call the sealed
merge route and the service behind the real middleware stack. Write measurement: the write log
records the table, the operation, the row id and, for an UPDATE, the sorted names of the columns
whose value changed, so a merge is proved to be ONE history INSERT and ONE one-column UPDATE of the
source's row; the teardown accepts only the writes a test declares through ``writes``. Every catalog
read is scoped to ``current_schema()``.
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
import backend.services.crm.related_merges as merges_module
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
from backend.services.crm.related_merges import RegistryInconsistent, record_merge
from backend.services.crm.related_names import NamesInput, _resolve_subject
from backend.services.crm.related_subjects import REGISTRY_ACTOR, prepare_identifiers
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures import registry_privacy as privacy
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

BACKEND = Path(route_module.__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "db" / "migrations_v2"
MIGRATION_MERGES = MIGRATIONS / "369_related_subject_merges.sql"
READ = "/api/crm/related-registry"
URL = "/api/crm/related-subjects/merges/register"
URL_SUBJECT = "/api/crm/related-subjects/register"
URL_DOCUMENTS = "/api/crm/related-subjects/documents/register"
URL_NAMES = "/api/crm/related-subjects/names/register"
URL_LINKS = "/api/crm/related-subjects/links/register"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
PLAN = "cd34" * 16

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}
AUDIT_INSERT = ("api_audit_trail", "INSERT")
MERGES_INSERT = ("related_subject_merges", "INSERT")
SUBJECTS_UPDATE = ("related_subjects", "UPDATE")

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
CREATE TABLE write_log (
    id bigserial PRIMARY KEY, tbl text NOT NULL, op text NOT NULL, rid text, changed text[]
);
CREATE FUNCTION log_write() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE
    row_id text;
    cols text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        row_id := to_jsonb(OLD) ->> 'id';
    ELSE
        row_id := to_jsonb(NEW) ->> 'id';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        SELECT array_agg(k.key ORDER BY k.key) INTO cols
        FROM jsonb_object_keys(to_jsonb(NEW)) AS k(key)
        WHERE to_jsonb(NEW) -> k.key IS DISTINCT FROM to_jsonb(OLD) -> k.key;
    END IF;
    INSERT INTO write_log (tbl, op, rid, changed) VALUES (TG_TABLE_NAME, TG_OP, row_id, cols);
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
    try:
        yield
    finally:
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


@pytest.mark.asyncio
async def test_m13_two_history_rows_of_one_pair_at_the_same_instant_the_higher_id_wins(
    conn, clients
):
    s = await seed_subject(conn, "person", "ZzTieHist Survivor", [("passport", "ZZ4242651")])
    m = await seed_subject(conn, "person", None, [("permit", "ZZ42PERMIT0651Z")], merged=s)
    await seed_history(conn, m, s, "manual", at=T0 + DAY)
    await seed_history(conn, m, s, "permit_stamp", at=T0 + DAY)
    detail = await detail_of(conn, s)
    assert detail["merged_from"] == [
        {"subject_id": m, "basis": "permit_stamp", "merged_at": T0 + DAY}
    ]


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
    m = await seed_subject(conn, "person", None, [("passport", "ZZ4242822")], merged=s)
    writes.update({("related_documents", "INSERT"), ("related_subject_documents", "INSERT")})
    body = {
        "subject": {
            "kind": "person",
            "identifiers": [{"type": "passport", "value": "ZZ4242822"}],
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


# ══ 4C-2: the sealed merge route ══════════════════════════════════════════════
#
# Synthetic values only: `ZZ` passports, `Z99ZZ` permits (a valid permit shape), ids from 424242 up.


def passport(n: int) -> str:
    return f"ZZ{n:07d}"


def permit(n: int) -> str:
    return f"Z99ZZ{n:011d}"


def nik(n: int) -> str:
    return f"32{n:014d}"


def npwp(n: int) -> str:
    return f"01{n:014d}"


def keys(ids) -> list[dict]:
    return [{"type": t, "value": v} for t, v in ids]


def src(n: int = 4242001):
    return (("permit", permit(n)),)


def tgt(n: int = 4242001):
    return (("passport", passport(n)),)


def req(source, target, *, dry_run=False, kind_s="person", kind_t="person", files=3, **over):
    data = {
        "source": {"kind": kind_s, "identifiers": keys(source)},
        "target": {"kind": kind_t, "identifiers": keys(target)},
        "basis": "permit_stamp",
        "evidence": {"plan_sha256": PLAN, "files": files},
        "dry_run": dry_run,
    }
    data.update(over)
    return data


def merge_input(data: dict):
    """The service input of a request dictionary, through the route's own strict model."""
    return route_module.RelatedMergesRegisterRequest.model_validate(data).to_input()


async def seed_pair(conn, n=4242001, *, source_first=False):
    """(source id, target id): a permit-only record with no name and a named passport holder."""
    if source_first:
        s = await seed_subject(conn, "person", None, [("permit", permit(n))])
        t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(n))])
    else:
        t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(n))])
        s = await seed_subject(conn, "person", None, [("permit", permit(n))])
    return s, t


def merged(s, t, dry_run=False):
    return {"outcome": "merged", "source_id": s, "target_id": t, "dry_run": dry_run}


def already(s, t, dry_run=False):
    return {"outcome": "already_merged", "source_id": s, "target_id": t, "dry_run": dry_run}


def refused(reason, s, t):
    return {"outcome": "not_mergeable", "reason": reason, "source_id": s, "target_id": t}


def not_registered(side, reason):
    return {"outcome": "subject_not_registered", "side": side, "reason": reason}


@pytest.fixture
def merge_writes(writes):
    """The two writes a merge makes, on top of the audit INSERT."""
    writes.update({MERGES_INSERT, SUBJECTS_UPDATE})
    return writes


@pytest_asyncio.fixture
async def client(pool, enabled, auth_on, audit_logger, monkeypatch, merge_writes):
    as_identity(monkeypatch, None)
    async with make_client(pool) as c:
        yield c


async def post(c, data, *, headers=HEADERS, url=URL):
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return await c.post(url, content=raw, headers=headers)


async def ledger(c):
    """Every write but the audit INSERT, in order: (table, op, row id, changed columns)."""
    rows = await c.fetch(
        "SELECT tbl, op, rid, changed FROM write_log WHERE tbl <> 'api_audit_trail' ORDER BY id"
    )
    return [(r["tbl"], r["op"], r["rid"], r["changed"]) for r in rows]


async def history(c):
    return await c.fetch(
        "SELECT id, source_id, target_id, basis, evidence, jsonb_typeof(evidence) AS kind, "
        "created_by, created_at FROM related_subject_merges ORDER BY id"
    )


async def pointers(c):
    return {
        r["id"]: r["merged_into"]
        for r in await c.fetch("SELECT id, merged_into FROM related_subjects ORDER BY id")
        if r["merged_into"] is not None
    }


async def no_chain_or_cycle(c) -> int:
    return await c.fetchval(
        "SELECT count(*) FROM related_subjects a JOIN related_subjects b ON b.id = a.merged_into "
        "WHERE b.merged_into IS NOT NULL"
    )


class _Spy:
    """The record of one connection (or of the pool's connections): every call as (sql, args), in
    order, with a hook BEFORE and a hook AFTER a named statement. ``replace`` substitutes the answer
    of a statement that DID run (the transaction still holds its effect until it ends)."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.before: dict = {}
        self.after: dict = {}
        self.replace: dict = {}

    def sqls(self) -> list[str]:
        return [sql for sql, _ in self.calls]

    def indexes(self, sql: str) -> list[int]:
        return [i for i, (s, _) in enumerate(self.calls) if s == sql]

    def writing(self) -> list[str]:
        out = []
        for sql, _ in self.calls:
            head = sql.lstrip().upper()
            if head.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE")):
                out.append(sql)
        return out

    def locks(self) -> list[int]:
        return [args[0] for sql, args in self.calls if sql == merges_module._LOCK_SQL]


class _SpyConn:
    def __init__(self, conn, spy):
        self._conn, self._spy = conn, spy

    def __getattr__(self, attr):
        target = getattr(self._conn, attr)
        if attr in {"fetch", "fetchrow", "fetchval", "execute"}:

            async def recorded(sql, *args, **kwargs):
                spy = self._spy
                spy.calls.append((sql, args))
                if sql in spy.before:
                    await spy.before[sql](args)
                out = await target(sql, *args, **kwargs)
                if sql in spy.replace:
                    out = spy.replace[sql]
                if sql in spy.after:
                    await spy.after[sql](args)
                return out

            return recorded
        return target


class _SpyPool:
    """The pool the app holds for one test: counts the acquisitions, wraps each connection."""

    def __init__(self, pool, spy=None):
        self._pool, self.acquired = pool, 0
        self.spy = spy or _Spy()

    @property
    def calls(self):
        return self.spy.calls

    @asynccontextmanager
    async def _connection(self):
        async with self._pool.acquire() as c:
            yield _SpyConn(c, self.spy)

    def acquire(self):
        self.acquired += 1
        return self._connection()


def spy_client(pool, monkeypatch, spy=None):
    as_identity(monkeypatch, None)
    spy_pool = _SpyPool(pool, spy)
    return spy_pool, make_client(spy_pool)


# ── M2: the production-shaped pool and the stored evidence ────────────────────


@pytest.mark.asyncio
async def test_m2_guard_the_module_pool_carries_the_production_jsonb_codec(conn):
    # a native container round-trips as itself; a pre-serialised string comes back as a STRING,
    # which is the double-encoding signature of the 2026-11-18 incident
    assert await conn.fetchval("SELECT $1::jsonb", {"a": 1}) == {"a": 1}
    assert await conn.fetchval("SELECT $1::jsonb", '{"a": 1}') == '{"a": 1}'
    assert await conn.fetchval("SELECT jsonb_typeof($1::jsonb)", '{"a": 1}') == "string"


@pytest.mark.asyncio
async def test_m2_a_routed_merge_stores_a_jsonb_object_with_the_evidence_sent(client, conn):
    s, t = await seed_pair(conn)
    resp = await post(client, req(src(), tgt(), files=7))
    assert resp.status_code == 200 and resp.json() == merged(s, t)
    (row,) = await history(conn)
    assert (row["source_id"], row["target_id"], row["basis"], row["kind"]) == (
        s,
        t,
        "permit_stamp",
        "object",
    )
    assert row["created_by"] == REGISTRY_ACTOR
    assert row["evidence"] == {"plan_sha256": PLAN, "files": 7}
    stored = await conn.fetchrow(
        "SELECT evidence->>'plan_sha256' AS plan, (evidence->>'files')::int AS files, "
        "evidence->>'basis' AS basis FROM related_subject_merges"
    )
    assert (stored["plan"], stored["files"], stored["basis"]) == (PLAN, 7, None)


# ── M3: dry run and apply write what they say ─────────────────────────────────

COUNTED = (
    "related_subject_identifiers",
    "related_subject_documents",
    "related_subject_names",
    "related_subject_links",
)


async def snapshot(conn, *ids):
    counts = {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in COUNTED}
    counts["related_subjects"] = await conn.fetchval("SELECT count(*) FROM related_subjects")
    rows = {
        i: await conn.fetchval("SELECT to_jsonb(r) - 'merged_into' FROM related_subjects r WHERE id = $1", i)
        for i in ids
    }  # fmt: skip
    return counts, rows


async def decorate(conn, s, t):
    """Documents, names and links on both records, which a merge must leave exactly where they are."""
    await seed_document(conn, s, 11)
    await seed_document(conn, t, 12)
    await seed_name(conn, t, "ZzRecordedTarget")
    await seed_link(conn, s, LIVE1, "identity")
    await seed_link(conn, t, LIVE2, "identity")


@pytest.mark.asyncio
async def test_m3_a_dry_run_writes_nothing_and_says_merged_with_dry_run_true(client, conn, clients):
    s, t = await seed_pair(conn)
    before = await snapshot(conn, s, t)
    resp = await post(client, req(src(), tgt(), dry_run=True))
    assert resp.status_code == 200 and resp.json() == merged(s, t, dry_run=True)
    assert await ledger(conn) == []
    assert await history(conn) == [] and await pointers(conn) == {}
    assert await snapshot(conn, s, t) == before
    omitted = await post(client, {k: v for k, v in req(src(), tgt()).items() if k != "dry_run"})
    assert omitted.json() == merged(s, t, dry_run=True)  # dry_run defaults to true
    assert await ledger(conn) == []


@pytest.mark.asyncio
async def test_m3_apply_writes_exactly_the_ledger_and_moves_nothing(client, conn, clients):
    s, t = await seed_pair(conn)
    await decorate(conn, s, t)
    before = await snapshot(conn, s, t)
    resp = await post(client, req(src(), tgt()))
    assert resp.status_code == 200 and resp.json() == merged(s, t)
    (row,) = await history(conn)
    assert await ledger(conn) == [
        ("related_subject_merges", "INSERT", str(row["id"]), None),
        ("related_subjects", "UPDATE", str(s), ["merged_into"]),
    ]
    assert await pointers(conn) == {s: t}
    assert await snapshot(conn, s, t) == before  # every count and every column but the pointer


@pytest.mark.asyncio
async def test_m3_two_applies_leave_one_history_row_and_the_second_writes_nothing(
    client, conn, clients
):
    s, t = await seed_pair(conn)
    first = await post(client, req(src(), tgt()))
    after_first = await ledger(conn)
    second = await post(client, req(src(), tgt()))
    assert first.json() == merged(s, t) and second.json() == already(s, t)
    assert await ledger(conn) == after_first and len(after_first) == 2
    assert len(await history(conn)) == 1


@pytest.mark.asyncio
async def test_m3_a_repeat_with_other_evidence_is_already_merged_and_changes_nothing(
    client, conn, clients
):
    s, t = await seed_pair(conn)
    await post(client, req(src(), tgt(), files=3))
    stored = await history(conn)
    again = await post(
        client,
        req(src(), tgt(), files=9, evidence={"plan_sha256": "ef56" * 16, "files": 9}),
    )
    assert again.json() == already(s, t)
    assert await history(conn) == stored


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_m3_a_pointer_seeded_without_history_is_already_merged(client, conn, dry_run):
    t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(4242001))])
    s = await seed_subject(conn, "person", None, [("permit", permit(4242001))], merged=t)
    resp = await post(client, req(src(), tgt(), dry_run=dry_run))
    assert resp.json() == already(s, t, dry_run=dry_run)
    assert await history(conn) == [] and await ledger(conn) == []


@pytest.mark.asyncio
async def test_m3_two_sources_into_one_survivor_both_merge(client, conn, clients):
    t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(4242001))])
    s1 = await seed_subject(conn, "person", None, [("permit", permit(4242001))])
    s2 = await seed_subject(conn, "person", None, [("permit", permit(4242002))])
    one = await post(client, req(src(4242001), tgt()))
    two = await post(client, req(src(4242002), tgt()))
    assert one.json() == merged(s1, t) and two.json() == merged(s2, t)
    assert await pointers(conn) == {s1: t, s2: t}
    assert [(r["source_id"], r["target_id"]) for r in await history(conn)] == [(s1, t), (s2, t)]


@pytest.mark.asyncio
async def test_m3_the_survivor_detail_after_a_routed_merge_shows_the_sources_identifiers(
    client, conn, clients
):
    s, t = await seed_pair(conn)
    await post(client, req(src(), tgt()))
    detail = await detail_of(conn, t)
    assert detail["identifiers"] == [
        {"type": "passport", "value": passport(4242001), "from_subject_id": None},
        {"type": "permit", "value": permit(4242001), "from_subject_id": s},
    ]
    assert [(m["subject_id"], m["basis"]) for m in detail["merged_from"]] == [(s, "permit_stamp")]
    assert detail["merged_from"][0]["merged_at"] is not None


# ── M4: statement and lock order ──────────────────────────────────────────────

RESOLUTION = {
    merges_module._OWNERS_SQL,
    merges_module._OWNER_KEYS_SQL,
    merges_module._MERGE_ROW_SQL,
}


async def run_spied(pool, monkeypatch, data):
    spy_pool, spy_app = spy_client(pool, monkeypatch)
    async with spy_app as c:
        resp = await post(c, data)
    return resp, spy_pool.spy


@pytest.mark.asyncio
@pytest.mark.parametrize("source_first", [True, False], ids=["source_lower", "target_lower"])
async def test_m4_apply_resolves_then_locks_in_ascending_order_then_decides_then_writes(
    pool, enabled, auth_on, audit_logger, merge_writes, monkeypatch, conn, source_first
):
    s, t = await seed_pair(conn, source_first=source_first)
    assert (s < t) == source_first
    resp, spy = await run_spied(pool, monkeypatch, req(src(), tgt()))
    assert resp.json() == merged(s, t)
    sqls = spy.sqls()
    lock_at = spy.indexes(merges_module._LOCK_SQL)
    assert len(lock_at) == 2 and spy.locks() == sorted([s, t])
    assert "FOR UPDATE" in merges_module._LOCK_SQL
    resolution_at = [i for i, q in enumerate(sqls) if q in RESOLUTION]
    assert len(resolution_at) == 6 and max(resolution_at) < lock_at[0]
    (survivor_at,) = spy.indexes(merges_module._SURVIVOR_SQL)
    (types_at,) = spy.indexes(merges_module._TYPES_SQL)
    (insert_at,) = spy.indexes(merges_module._INSERT_MERGE_SQL)
    (update_at,) = spy.indexes(merges_module._UPDATE_POINTER_SQL)
    assert lock_at[1] < survivor_at < types_at < insert_at < update_at
    assert len(sqls) == 12 and update_at == 11
    assert spy.writing() == [
        merges_module._INSERT_MERGE_SQL,
        merges_module._UPDATE_POINTER_SQL,
    ]


@pytest.mark.asyncio
async def test_m4_a_dry_run_takes_no_lock_and_writes_nothing_in_at_most_eight_statements(
    pool, enabled, auth_on, audit_logger, merge_writes, monkeypatch, conn
):
    s, t = await seed_pair(conn)
    resp, spy = await run_spied(pool, monkeypatch, req(src(), tgt(), dry_run=True))
    assert resp.json() == merged(s, t, dry_run=True)
    assert spy.writing() == [] and spy.indexes(merges_module._LOCK_SQL) == []
    assert not any("FOR UPDATE" in q for q in spy.sqls())
    assert len(spy.calls) == 8


async def _refusal_states(conn):
    """One seeded state per outcome that must write nothing: name -> (request builder, outcome)."""
    t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(4242001))])
    s = await seed_subject(conn, "person", None, [("permit", permit(4242001))])
    other = await seed_subject(conn, "person", "ZzOther", [("passport", passport(4242002))])
    co = await seed_subject(conn, "company", None, [("npwp", npwp(4242003))])
    mg = await seed_subject(conn, "person", None, [("permit", permit(4242004))], merged=other)
    sur = await seed_subject(conn, "person", None, [("permit", permit(4242005))])
    await seed_subject(conn, "person", None, [("permit", permit(4242006))], merged=sur)
    npk = await seed_subject(
        conn, "person", None, [("permit", permit(4242007)), ("nik", nik(4242007))]
    )
    al = await seed_subject(conn, "person", None, [("permit", permit(4242008))], merged=t)
    return {
        "same_subject": (req(src(), src()), refused("same_subject", s, s)),
        "kind_differs": (
            req(src(), (("npwp", npwp(4242003)),), kind_t="company"),
            refused("kind_differs", s, co),
        ),
        "target_is_merged": (
            req(src(), (("permit", permit(4242004)),)),
            refused("target_is_merged", s, mg),
        ),
        "source_merged_elsewhere": (
            req((("permit", permit(4242004)),), tgt()),
            refused("source_merged_elsewhere", mg, t),
        ),
        "source_is_survivor": (
            req((("permit", permit(4242005)),), tgt()),
            refused("source_is_survivor", sur, t),
        ),
        "basis_precondition": (
            req((("permit", permit(4242007)), ("nik", nik(4242007))), tgt()),
            refused("basis_precondition", npk, t),
        ),
        "already_merged": (req((("permit", permit(4242008)),), tgt()), None),
        "none_owned": (
            req((("permit", permit(4242999)),), tgt()),
            not_registered("source", "none_owned"),
        ),
    }, {"al": al, "t": t}


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_m4_every_refusal_and_already_merged_attempts_no_insert_or_update(
    pool, enabled, auth_on, audit_logger, monkeypatch, conn, dry_run
):
    states, ids = await _refusal_states(conn)
    for name, (data, expected) in states.items():
        data = {**data, "dry_run": dry_run}
        resp, spy = await run_spied(pool, monkeypatch, data)
        body = resp.json()
        if name == "already_merged":
            expected = already(ids["al"], ids["t"], dry_run=dry_run)
        assert body == expected, (name, body)
        assert spy.writing() == [], name
        if dry_run:
            assert spy.indexes(merges_module._LOCK_SQL) == [], name
            assert len(spy.calls) <= 8, name
        if name == "same_subject":
            assert spy.indexes(merges_module._LOCK_SQL) == [] and len(spy.calls) <= 6
        if name == "already_merged":
            assert len(spy.calls) <= 8
    assert await history(conn) == [] and await ledger(conn) == []


# ── M5: not_mergeable, each reason, and the order of decisions ────────────────


async def decision_states(conn):
    """Every decision of 3.2.2 that refuses or says already_merged, as (request, expected answer as
    a function of dry_run, (source id, target id)), on records seeded fresh. Operator-made states
    (a chain, a pointer between two kinds) are seeded directly: the database allows them."""
    numbers = iter(range(4243001, 4243999))
    states: dict = {}

    async def person(ids, name=None, merged=None):
        return await seed_subject(conn, "person", name, ids, merged=merged)

    def put(name, source, target, s, t, expected, **over):
        states[name] = (req(source, target, **over), expected, (s, t))

    a = next(numbers)
    s = await person([("permit", permit(a))])
    put("same_subject", src(a), src(a), s, s, lambda d: refused("same_subject", s, s))
    put(
        "same_subject_spelling",
        src(a),
        (("permit", permit(a).lower()),),
        s,
        s,
        lambda d: refused("same_subject", s, s),
    )

    a = next(numbers)
    s = await person([("permit", permit(a))])
    c = await seed_subject(conn, "company", None, [("npwp", npwp(a))])
    put(
        "kind_differs", src(a), (("npwp", npwp(a)),), s, c,
        lambda d: refused("kind_differs", s, c), kind_t="company",
    )  # fmt: skip

    a, b, x = next(numbers), next(numbers), next(numbers)
    other = await person([("passport", passport(x))], "ZzOther")
    s = await person([("permit", permit(a))])
    t = await person([("passport", passport(b))], "ZzTarget", merged=other)
    put("target_is_merged", src(a), tgt(b), s, t, lambda d: refused("target_is_merged", s, t))

    a, b, x = next(numbers), next(numbers), next(numbers)
    other = await person([("passport", passport(x))], "ZzOther")
    s = await person([("permit", permit(a))], merged=other)
    t = await person([("passport", passport(b))], "ZzTarget")
    put(
        "source_merged_elsewhere", src(a), tgt(b), s, t,
        lambda d: refused("source_merged_elsewhere", s, t),
    )  # fmt: skip

    a, b, z = next(numbers), next(numbers), next(numbers)
    s = await person([("permit", permit(a))])
    await person([("permit", permit(z))], merged=s)
    t = await person([("passport", passport(b))], "ZzTarget")
    put("source_is_survivor", src(a), tgt(b), s, t, lambda d: refused("source_is_survivor", s, t))

    a, b = next(numbers), next(numbers)
    s = await person([("permit", permit(a)), ("nik", nik(a))])
    t = await person([("passport", passport(b))], "ZzTarget")
    put(
        "basis_nik_besides_permit", (("permit", permit(a)), ("nik", nik(a))), tgt(b), s, t,
        lambda d: refused("basis_precondition", s, t),
    )  # fmt: skip

    a, b = next(numbers), next(numbers)
    s = await person([("permit", permit(a))])
    t = await person([("nik", nik(b))], "ZzTarget")
    put(
        "basis_target_without_passport", src(a), (("nik", nik(b)),), s, t,
        lambda d: refused("basis_precondition", s, t),
    )  # fmt: skip

    a, b = next(numbers), next(numbers)
    t = await person([("passport", passport(b))], "ZzTarget")
    s = await person([("permit", permit(a))], merged=t)
    put("already_merged", src(a), tgt(b), s, t, lambda d: already(s, t, d))

    # precedence, on operator-made states that fail an earlier AND a later decision
    a, b = next(numbers), next(numbers)
    c = await seed_subject(conn, "company", None, [("npwp", npwp(b))])
    s = await person([("permit", permit(a))], merged=c)  # a pointer across two kinds
    put(
        "already_merged_before_kind_differs", src(a), (("npwp", npwp(b)),), s, c,
        lambda d: already(s, c, d), kind_t="company",
    )  # fmt: skip

    a, b, x = next(numbers), next(numbers), next(numbers)
    cx = await seed_subject(conn, "company", None, [("npwp", npwp(x))])
    s = await person([("permit", permit(a))])
    c = await seed_subject(conn, "company", None, [("npwp", npwp(b))], merged=cx)
    put(
        "kind_differs_before_target_is_merged", src(a), (("npwp", npwp(b)),), s, c,
        lambda d: refused("kind_differs", s, c), kind_t="company",
    )  # fmt: skip

    a, b, x, y = (next(numbers) for _ in range(4))
    px = await person([("passport", passport(x))], "ZzX")
    py = await person([("passport", passport(y))], "ZzY")
    s = await person([("permit", permit(a))], merged=px)
    t = await person([("passport", passport(b))], "ZzTarget", merged=py)
    put(
        "target_is_merged_before_source_merged_elsewhere", src(a), tgt(b), s, t,
        lambda d: refused("target_is_merged", s, t),
    )  # fmt: skip

    a, b, x, z = (next(numbers) for _ in range(4))
    px = await person([("passport", passport(x))], "ZzX")
    s = await person([("permit", permit(a))], merged=px)
    await person([("permit", permit(z))], merged=s)  # a chain: s is merged AND a survivor
    t = await person([("passport", passport(b))], "ZzTarget")
    put(
        "source_merged_elsewhere_before_source_is_survivor", src(a), tgt(b), s, t,
        lambda d: refused("source_merged_elsewhere", s, t),
    )  # fmt: skip

    a, b, z = next(numbers), next(numbers), next(numbers)
    s = await person([("permit", permit(a)), ("nik", nik(a))])
    await person([("permit", permit(z))], merged=s)
    t = await person([("nik", nik(b))], "ZzTarget")
    put(
        "source_is_survivor_before_basis_precondition",
        (("permit", permit(a)), ("nik", nik(a))), (("nik", nik(b)),), s, t,
        lambda d: refused("source_is_survivor", s, t),
    )  # fmt: skip
    return states


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True], ids=["empty_history", "stale_history"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_m5_every_refusal_has_its_exact_body_in_the_order_of_the_decisions(
    client, conn, clients, dry_run, stale
):
    states = await decision_states(conn)
    if stale:
        for _, _, (s, t) in states.values():
            if s != t:
                await seed_history(conn, s, t)
                await seed_history(conn, t, s)
    rows_before = len(await history(conn))
    pointers_before = await pointers(conn)
    for name, (data, expected, _) in states.items():
        resp = await post(client, {**data, "dry_run": dry_run})
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json() == expected(dry_run), name
    assert len(await history(conn)) == rows_before
    assert await ledger(conn) == [] and await pointers(conn) == pointers_before


@pytest.mark.asyncio
async def test_m5_a_survivor_as_the_target_of_a_second_source_is_mergeable(client, conn, clients):
    t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(4242001))])
    first = await seed_subject(conn, "person", None, [("permit", permit(4242001))], merged=t)
    second = await seed_subject(conn, "person", None, [("permit", permit(4242002))])
    resp = await post(client, req(src(4242002), tgt()))
    assert resp.json() == merged(second, t)
    assert await pointers(conn) == {first: t, second: t}


@pytest.mark.asyncio
@pytest.mark.parametrize("basis", ["same_identity", "manual", "guessed"])
async def test_m5_a_basis_other_than_permit_stamp_is_refused_before_any_statement(
    pool, enabled, auth_on, audit_logger, monkeypatch, conn, basis
):
    await seed_pair(conn)
    spy_pool, spy_app = spy_client(pool, monkeypatch)
    async with spy_app as c:
        resp = await post(c, req(src(), tgt(), basis=basis))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [{"loc": ["basis"], "code": "literal_error"}]}
    assert spy_pool.acquired == 0 and spy_pool.calls == []


# ── M6: a side that cannot be used ────────────────────────────────────────────

SIDE_REASONS = (
    "none_owned",
    "two_owners",
    "subset",
    "superset",
    "partial_overlap",
    "kind_differs",
)


async def side_world(conn):
    """A usable pair and, per reason, the keys that give it. Every key set is complete or not on purpose."""
    w: dict = {}
    w["t"] = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(4244001))])
    w["s"] = await seed_subject(conn, "person", None, [("permit", permit(4244001))])
    w["a"] = await seed_subject(
        conn, "person", None, [("permit", permit(4244002)), ("nik", nik(4244002))]
    )
    w["b"] = await seed_subject(conn, "person", None, [("permit", permit(4244003))])
    w["co"] = await seed_subject(conn, "company", None, [("passport", passport(4244004))])
    bad = {
        "none_owned": (("permit", permit(4244999)),),
        "two_owners": (("permit", permit(4244002)), ("permit", permit(4244003))),
        "subset": (("permit", permit(4244002)),),
        "superset": (("permit", permit(4244003)), ("permit", permit(4244998))),
        "partial_overlap": (("permit", permit(4244002)), ("permit", permit(4244997))),
        "kind_differs": (("passport", passport(4244004)),),
    }
    reasons = {
        "none_owned": "none_owned",
        "two_owners": "two_owners",
        "subset": "set_differs",
        "superset": "set_differs",
        "partial_overlap": "partial_overlap",
        "kind_differs": "kind_differs",
    }
    return w, bad, reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_m6_five_reasons_on_each_side_with_side_and_reason(client, conn, clients, dry_run):
    w, bad, reasons = await side_world(conn)
    good_source, good_target = src(4244001), tgt(4244001)
    for name in SIDE_REASONS:
        resp = await post(client, req(bad[name], good_target, dry_run=dry_run))
        assert resp.json() == not_registered("source", reasons[name]), name
        resp = await post(client, req(good_source, bad[name], dry_run=dry_run))
        assert resp.json() == not_registered("target", reasons[name]), name
    # the source is judged before the target
    both = await post(client, req(bad["none_owned"], bad["two_owners"], dry_run=dry_run))
    assert both.json() == not_registered("source", "none_owned")
    assert await ledger(conn) == [] and await history(conn) == []


@pytest.mark.asyncio
async def test_m6_a_merged_away_owner_resolves_on_either_side(client, conn, clients):
    t = await seed_subject(conn, "person", "ZzTarget", [("passport", passport(4245001))])
    s = await seed_subject(conn, "person", None, [("permit", permit(4245001))], merged=t)
    as_source = await post(client, req(src(4245001), tgt(4245001), dry_run=True))
    assert as_source.json() == already(s, t, True)
    as_target = await post(client, req(tgt(4245001), src(4245001), dry_run=True))
    assert as_target.json() == refused("target_is_merged", t, s)


@pytest.mark.asyncio
@pytest.mark.parametrize("gone", ["source", "target"])
async def test_m6_a_row_deleted_after_the_resolution_answers_none_owned_for_its_side(
    pool, conn, clients, gone
):
    s, t = await seed_pair(conn)
    victim = s if gone == "source" else t
    spy = _Spy()
    fired = []

    async def delete_row(args):
        if not fired:
            fired.append(True)
            async with pool.acquire() as other:
                async with unmeasured(other):
                    await other.execute("DELETE FROM related_subjects WHERE id = $1", victim)

    spy.before[merges_module._LOCK_SQL] = delete_row
    async with pool.acquire() as c:
        out = await record_merge(_SpyConn(c, spy), merge_input(req(src(), tgt())), dry_run=False)
    assert out == not_registered(gone, "none_owned")
    assert spy.writing() == []
    assert await history(conn) == [] and await pointers(conn) == {}
    assert await ledger(conn) == []


PARITY_STATES = (
    "none_owned",
    "two_owners",
    "subset",
    "superset",
    "partial_overlap",
    "kind_differs",
)


@pytest.mark.asyncio
async def test_m6_the_owner_resolvers_agree_on_every_reason_and_the_new_one_lacks_only_merged(
    conn,
):
    w, bad, reasons = await side_world(conn)
    for name in PARITY_STATES:
        kind = "person"
        prepared = prepare_identifiers(kind, [(t, v, {}) for t, v in bad[name]])
        old = await _resolve_subject(conn, NamesInput(kind, prepared, ()))
        new = await merges_module._resolve_owner(conn, merges_module.MergeSide(kind, prepared))
        assert old == {"reason": reasons[name]} and new == old, name
    usable = prepare_identifiers("person", [("permit", permit(4244001), {})])
    old = await _resolve_subject(conn, NamesInput("person", usable, ()))
    new = await merges_module._resolve_owner(conn, merges_module.MergeSide("person", usable))
    assert old["subject_id"] == new["subject_id"] == w["s"]
    assert new["merged_into"] is None and new["kind"] == "person"
    # a merged-away owner: the old resolver refuses it, the new one returns it with its pointer
    m = await seed_subject(conn, "person", None, [("permit", permit(4244010))], merged=w["t"])
    gone = prepare_identifiers("person", [("permit", permit(4244010), {})])
    assert await _resolve_subject(conn, NamesInput("person", gone, ())) == {"reason": "merged"}
    resolved = await merges_module._resolve_owner(conn, merges_module.MergeSide("person", gone))
    assert resolved == {"subject_id": m, "kind": "person", "merged_into": w["t"]}


# ── M7: atomicity ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after", ["_INSERT_MERGE_SQL", "_UPDATE_POINTER_SQL"], ids=["after_insert", "after_update"]
)
async def test_m7_a_failure_after_a_write_leaves_no_history_row_and_no_pointer(pool, conn, after):
    s, t = await seed_pair(conn)
    spy = _Spy()

    async def boom(args):
        raise RuntimeError("injected")

    spy.after[getattr(merges_module, after)] = boom
    async with pool.acquire() as c:
        with pytest.raises(RuntimeError):
            await record_merge(_SpyConn(c, spy), merge_input(req(src(), tgt())), dry_run=False)
    assert getattr(merges_module, after) in spy.sqls()
    assert await history(conn) == [] and await pointers(conn) == {}
    assert await ledger(conn) == []
    ok = await record_merge(conn, merge_input(req(src(), tgt())), dry_run=False)
    assert ok == merged(s, t) and await pointers(conn) == {s: t}


@pytest.mark.asyncio
async def test_m7_an_update_that_returns_no_row_is_the_fixed_500_and_the_insert_is_rolled_back(
    pool, enabled, auth_on, audit_logger, merge_writes, monkeypatch, conn
):
    s, t = await seed_pair(conn)
    spy = _Spy()
    spy.replace[merges_module._UPDATE_POINTER_SQL] = None
    spy_pool, spy_app = spy_client(pool, monkeypatch, spy)
    async with spy_app as c:
        resp = await post(c, req(src(), tgt()))
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    assert spy.indexes(merges_module._INSERT_MERGE_SQL) and spy.indexes(
        merges_module._UPDATE_POINTER_SQL
    )
    assert await history(conn) == [] and await pointers(conn) == {}
    assert await ledger(conn) == []
    assert issubclass(RegistryInconsistent, RuntimeError)


# ── M8: concurrency, driven by a controller ───────────────────────────────────

CAP = 10  # seconds a step of the controller may take


class _Hold:
    """The spy hooks of one transaction of an M8 schedule: it is held BEFORE its first lock
    statement until released, and, as the winner, AFTER the pointer UPDATE until told to commit."""

    def __init__(self):
        self.spy = _Spy()
        self.at_lock, self.release = asyncio.Event(), asyncio.Event()
        self.at_update, self.commit = asyncio.Event(), asyncio.Event()
        self._held = False
        self.spy.before[merges_module._LOCK_SQL] = self._before_lock
        self.spy.after[merges_module._UPDATE_POINTER_SQL] = self._after_update

    async def _before_lock(self, args):
        if not self._held:
            self._held = True
            self.at_lock.set()
            await asyncio.wait_for(self.release.wait(), CAP)

    async def _after_update(self, args):
        self.at_update.set()
        await asyncio.wait_for(self.commit.wait(), CAP)


async def _wait(event: asyncio.Event, what: str, *tasks):
    waiter = asyncio.ensure_future(event.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, *tasks}, timeout=CAP, return_when=asyncio.FIRST_COMPLETED
        )
        if waiter not in done:
            for task in tasks:
                if task in done and not task.cancelled() and task.exception():
                    raise task.exception()
            ended = any(t in done for t in tasks)
            raise AssertionError(
                f"the controller did not reach: {what}"
                + (" (a transaction ended first)" if ended else "")
            )
    finally:
        waiter.cancel()


async def _blocked(observer, pid: int, *tasks):
    """The third connection sees the backend `pid` waiting on a lock it was not granted."""
    deadline = asyncio.get_running_loop().time() + CAP
    while asyncio.get_running_loop().time() < deadline:
        if await observer.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.pid = $1 "
            "AND a.wait_event_type = 'Lock' AND EXISTS "
            "(SELECT 1 FROM pg_locks l WHERE l.pid = a.pid AND NOT l.granted))",
            pid,
        ):
            return
        if any(t.done() for t in tasks):
            break
        await asyncio.sleep(0.05)
    raise AssertionError("the loser was never seen waiting on a lock")


async def run_schedule(pool, winner: dict, loser: dict):
    """Both transactions start and stop BEFORE their first lock; the winner is released and runs to
    its pointer UPDATE holding both locks; the loser is released and must be seen blocked from a
    third connection; only then the winner commits. Returns (winner's answer, loser's answer)."""
    win, lose = _Hold(), _Hold()
    lose.commit.set()
    async with pool.acquire() as cw, pool.acquire() as cl, pool.acquire() as observer:
        loser_pid = cl.get_server_pid()
        tw = asyncio.ensure_future(
            record_merge(_SpyConn(cw, win.spy), merge_input(winner), dry_run=False)
        )
        tl = asyncio.ensure_future(
            record_merge(_SpyConn(cl, lose.spy), merge_input(loser), dry_run=False)
        )
        try:
            await _wait(win.at_lock, "the winner before its first lock", tw, tl)
            await _wait(lose.at_lock, "the loser before its first lock", tw, tl)
            win.release.set()
            await _wait(win.at_update, "the winner at its pointer UPDATE", tw, tl)
            lose.release.set()
            await _blocked(observer, loser_pid, tw, tl)
            win.commit.set()
            return await asyncio.wait_for(tw, CAP), await asyncio.wait_for(tl, CAP)
        finally:
            for event in (win.release, lose.release, win.commit):
                event.set()
            await asyncio.gather(tw, tl, return_exceptions=True)


async def _after_schedule(conn, expected_pointers: dict, history_rows: int):
    assert await pointers(conn) == expected_pointers
    assert len(await history(conn)) == history_rows
    assert await no_chain_or_cycle(conn) == 0


@pytest.mark.asyncio
async def test_m8_s1_the_lower_source_wins_and_the_opposite_merge_is_target_is_merged(
    pool, conn, merge_writes
):
    a = await seed_subject(conn, "person", None, [("permit", permit(4246001))])
    b = await seed_subject(conn, "person", "ZzB", [("passport", passport(4246001))])
    won, lost = await run_schedule(
        pool, req(src(4246001), tgt(4246001)), req(tgt(4246001), src(4246001))
    )
    assert won == merged(a, b) and lost == refused("target_is_merged", b, a)
    await _after_schedule(conn, {a: b}, 1)


@pytest.mark.asyncio
async def test_m8_s2_the_higher_source_wins_and_the_opposite_merge_is_target_is_merged(
    pool, conn, merge_writes
):
    a = await seed_subject(conn, "person", "ZzA", [("passport", passport(4246002))])
    b = await seed_subject(conn, "person", None, [("permit", permit(4246002))])
    won, lost = await run_schedule(
        pool, req(src(4246002), tgt(4246002)), req(tgt(4246002), src(4246002))
    )
    assert won == merged(b, a) and lost == refused("target_is_merged", a, b)
    await _after_schedule(conn, {b: a}, 1)


@pytest.mark.asyncio
async def test_m8_s3_the_same_merge_twice_the_second_is_already_merged_and_one_row(
    pool, conn, merge_writes
):
    s, t = await seed_pair(conn, 4246003)
    won, lost = await run_schedule(
        pool, req(src(4246003), tgt(4246003)), req(src(4246003), tgt(4246003))
    )
    assert won == merged(s, t) and lost == already(s, t)
    await _after_schedule(conn, {s: t}, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["first", "second"])
@pytest.mark.parametrize("target_first", [True, False], ids=["target_lower", "target_higher"])
async def test_m8_s4_two_sources_into_one_target_either_wins_and_both_merge(
    pool, conn, merge_writes, winner, target_first
):
    if target_first:
        t = await seed_subject(conn, "person", "ZzT", [("passport", passport(4246004))])
    s1 = await seed_subject(conn, "person", None, [("permit", permit(4246004))])
    s2 = await seed_subject(conn, "person", None, [("permit", permit(4246005))])
    if not target_first:
        t = await seed_subject(conn, "person", "ZzT", [("passport", passport(4246004))])
    first, second = req(src(4246004), tgt(4246004)), req(src(4246005), tgt(4246004))
    ids = {"first": s1, "second": s2}
    order = (first, second) if winner == "first" else (second, first)
    won, lost = await run_schedule(pool, *order)
    other = s2 if winner == "first" else s1
    assert won == merged(ids[winner], t) and lost == merged(other, t)
    await _after_schedule(conn, {s1: t, s2: t}, 2)


@pytest.mark.asyncio
async def test_m8_s5_a_chain_race_the_merge_into_a_will_make_a_a_survivor_and_a_to_b_is_refused(
    pool, conn, merge_writes
):
    y = await seed_subject(conn, "person", None, [("permit", permit(4246006))])
    a = await seed_subject(
        conn, "person", "ZzA", [("passport", passport(4246006)), ("permit", permit(4246007))]
    )
    b = await seed_subject(conn, "person", "ZzB", [("passport", passport(4246007))])
    a_keys = (("passport", passport(4246006)), ("permit", permit(4246007)))
    won, lost = await run_schedule(pool, req(src(4246006), a_keys), req(a_keys, tgt(4246007)))
    assert won == merged(y, a) and lost == refused("source_is_survivor", a, b)
    await _after_schedule(conn, {y: a}, 1)


@pytest.mark.asyncio
async def test_m8_s6_a_chain_race_a_to_b_wins_and_the_merge_into_a_is_target_is_merged(
    pool, conn, merge_writes
):
    y = await seed_subject(conn, "person", None, [("permit", permit(4246008))])
    a = await seed_subject(conn, "person", None, [("permit", permit(4246009))])
    b = await seed_subject(conn, "person", "ZzB", [("passport", passport(4246009))])
    won, lost = await run_schedule(
        pool, req(src(4246009), tgt(4246009)), req(src(4246008), src(4246009))
    )
    assert won == merged(a, b) and lost == refused("target_is_merged", y, a)
    await _after_schedule(conn, {a: b}, 1)


# ── M9: the one sanctioned UPDATE ─────────────────────────────────────────────

EXPECTED_SQL_NAMES = {
    "_OWNERS_SQL",
    "_OWNER_KEYS_SQL",
    "_MERGE_ROW_SQL",
    "_LOCK_SQL",
    "_SURVIVOR_SQL",
    "_TYPES_SQL",
    "_INSERT_MERGE_SQL",
    "_UPDATE_POINTER_SQL",
}
UPDATE_POINTER = (
    "UPDATE related_subjects SET merged_into = $2 WHERE id = $1 AND merged_into IS NULL "
    "RETURNING id"
)


def sql_texts() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(merges_module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


def without_lock_clause(sql: str) -> str:
    """`FOR UPDATE` is the locking clause of a SELECT, not an UPDATE statement."""
    return re.sub(r"\bFOR UPDATE\b", "", sql)


def test_m9_the_sql_constants_hold_one_update_one_insert_and_nothing_that_deletes():
    texts = sql_texts()
    assert set(texts) == EXPECTED_SQL_NAMES
    updates = [n for n, t in texts.items() if re.search(r"\bUPDATE\b", without_lock_clause(t))]
    assert updates == ["_UPDATE_POINTER_SQL"]
    assert texts["_UPDATE_POINTER_SQL"] == UPDATE_POINTER
    assert not any(re.search(r"\b(DELETE|TRUNCATE)\b", t, re.I) for t in texts.values())
    inserts = [n for n, t in texts.items() if re.search(r"\bINSERT\b", t, re.I)]
    assert inserts == ["_INSERT_MERGE_SQL"]
    assert texts["_INSERT_MERGE_SQL"].startswith("INSERT INTO related_subject_merges ")
    assert texts["_LOCK_SQL"] == (
        "SELECT id, kind, merged_into FROM related_subjects WHERE id = $1 FOR UPDATE"
    )


@pytest.mark.asyncio
async def test_m9_the_update_affects_one_row_and_run_again_none(conn):
    s, t = await seed_pair(conn)
    async with unmeasured(conn):
        first = await conn.fetch(merges_module._UPDATE_POINTER_SQL, s, t)
        again = await conn.fetch(merges_module._UPDATE_POINTER_SQL, s, t)
    assert [r["id"] for r in first] == [s] and again == []
    assert await pointers(conn) == {s: t}


@pytest.mark.asyncio
async def test_m9_the_plain_undo_statement_lists_the_record_again_and_a_second_merge_succeeds(
    client, conn, clients
):
    s, t = await seed_pair(conn)
    assert (await post(client, req(src(), tgt()))).json() == merged(s, t)
    page = await related_registry_read.list_subjects(conn, q=None, kind=None, limit=50, offset=0)
    assert [r["id"] for r in page["items"]] == [t]
    async with unmeasured(conn):
        await conn.execute("UPDATE related_subjects SET merged_into = NULL WHERE id = $1", s)
    page = await related_registry_read.list_subjects(conn, q=None, kind=None, limit=50, offset=0)
    assert sorted(r["id"] for r in page["items"]) == sorted([s, t])
    assert len(await history(conn)) == 1  # the history row stays
    assert (await post(client, req(src(), tgt()))).json() == merged(s, t)
    rows = await history(conn)
    assert len(rows) == 2 and rows[0]["id"] < rows[1]["id"]
    (member,) = (await detail_of(conn, t))["merged_from"]
    assert member["subject_id"] == s and member["merged_at"] == rows[1]["created_at"]


# ── M10: validation, exact 422 bodies ─────────────────────────────────────────

# The probe world of M10 and M18: every identifier carries at least six digits.
PSV, QSV = passport(7373701), permit(7373701)  # the survivor: a passport and a permit
QSRC = permit(7373702)  # the permit-only source
PBY = passport(7373703)  # the bystander, in no request
NCO = npwp(7373704)  # a company
QSRC2 = permit(7373705)  # a second permit-only record (two owners)
QMG = permit(7373706)  # merged into the survivor
QSUR = permit(7373707)  # a survivor of QMEM
QMEM = permit(7373708)
QNP, NNP = permit(7373709), nik(7373709)  # a permit and a national id on one record
QAL = permit(7373710)  # merged into the survivor, no history row
QNONE = permit(7373711)  # owned by nobody
SURVIVOR_KEYS = (("passport", PSV), ("permit", QSV))

BASE = req((("permit", QSRC),), SURVIVOR_KEYS)


def _mut(fn):
    def apply(base):
        data = copy.deepcopy(base)
        fn(data)
        return data

    return apply


def _four(first_three, fourth):
    return [*keys(first_three), fourth]


EXTRA3 = (
    ("passport", passport(7373721)),
    ("passport", passport(7373722)),
    ("permit", permit(7373723)),
)
ZZ_HOSTILE = "ZzHostile<script>"

M10_CASES = {
    "source_kind_unknown": (
        _mut(lambda d: d["source"].update(kind="robot")),
        [(["source", "kind"], "literal_error")],
    ),
    "target_kind_unknown": (
        _mut(lambda d: d["target"].update(kind="robot")),
        [(["target", "kind"], "literal_error")],
    ),
    "source_kind_not_a_string": (
        _mut(lambda d: d["source"].update(kind=7)),
        [(["source", "kind"], "literal_error")],
    ),
    "target_kind_missing": (
        _mut(lambda d: d["target"].pop("kind")),
        [(["target", "kind"], "missing")],
    ),
    "source_kind_type_mismatch": (
        _mut(lambda d: d["source"].update(kind="company")),
        [(["source"], "kind_type_mismatch")],
    ),
    "target_kind_type_mismatch": (
        _mut(lambda d: d["target"].update(kind="company")),
        [(["target"], "kind_type_mismatch")],
    ),
    "source_value_not_a_string_at_index_3": (
        _mut(
            lambda d: d["source"].update(identifiers=_four(EXTRA3, {"type": "nik", "value": 123}))
        ),
        [(["source", "identifiers", 3, "value"], "string_type")],
    ),
    "target_type_unknown_at_index_3": (
        _mut(
            lambda d: d["target"].update(
                identifiers=_four(EXTRA3, {"type": "zzbogus", "value": "x" * 8})
            )
        ),
        [(["target", "identifiers", 3, "type"], "literal_error")],
    ),
    "source_identifier_invalid": (
        _mut(lambda d: d["source"]["identifiers"][0].update(value="!")),
        [(["source", "identifiers", 0], "invalid_identifier")],
    ),
    "target_identifier_invalid": (
        _mut(lambda d: d["target"]["identifiers"][1].update(value="!")),
        [(["target", "identifiers", 1], "invalid_identifier")],
    ),
    "source_identifiers_empty": (
        _mut(lambda d: d["source"].update(identifiers=[])),
        [(["source", "identifiers"], "too_short")],
    ),
    "target_twenty_one_identifiers": (
        _mut(
            lambda d: d["target"].update(
                identifiers=keys([("passport", passport(7373800 + i)) for i in range(21)])
            )
        ),
        [(["target", "identifiers"], "too_long")],
    ),
    "source_duplicate_identifier": (
        _mut(lambda d: d["source"]["identifiers"].append(d["source"]["identifiers"][0])),
        [(["source"], "duplicate_identifier")],
    ),
    "missing_source": (_mut(lambda d: d.pop("source")), [(["source"], "missing")]),
    "missing_target": (_mut(lambda d: d.pop("target")), [(["target"], "missing")]),
    "missing_basis": (_mut(lambda d: d.pop("basis")), [(["basis"], "missing")]),
    "missing_evidence": (_mut(lambda d: d.pop("evidence")), [(["evidence"], "missing")]),
    "same_identity_is_refused": (
        _mut(lambda d: d.update(basis="same_identity")),
        [(["basis"], "literal_error")],
    ),
    "manual_is_refused": (
        _mut(lambda d: d.update(basis="manual")),
        [(["basis"], "literal_error")],
    ),
    "unknown_basis": (
        _mut(lambda d: d.update(basis="guessed")),
        [(["basis"], "literal_error")],
    ),
    "basis_not_a_string": (_mut(lambda d: d.update(basis=1)), [(["basis"], "literal_error")]),
    "unknown_field": (_mut(lambda d: d.update(zzextra=1)), [(["_"], "extra_forbidden")]),
    "source_id_is_not_a_field": (
        _mut(lambda d: d.update(source_id=424242)),
        [(["_"], "extra_forbidden")],
    ),
    "target_id_is_not_a_field": (
        _mut(lambda d: d.update(target_id=424243)),
        [(["_"], "extra_forbidden")],
    ),
    "merged_into_is_not_a_field": (
        _mut(lambda d: d.update(merged_into=424244)),
        [(["_"], "extra_forbidden")],
    ),
    "created_by_is_not_a_field": (
        _mut(lambda d: d.update(created_by="zz")),
        [(["_"], "extra_forbidden")],
    ),
    "unknown_field_in_source": (
        _mut(lambda d: d["source"].update(zzextra="zz")),
        [(["source", "_"], "extra_forbidden")],
    ),
    "unknown_field_in_target": (
        _mut(lambda d: d["target"].update(zzextra="zz")),
        [(["target", "_"], "extra_forbidden")],
    ),
    "hostile_field_name": (
        _mut(lambda d: d["evidence"].update({ZZ_HOSTILE: 1})),
        [(["evidence", "_"], "extra_forbidden")],
    ),
    "plan_hash_missing": (
        _mut(lambda d: d["evidence"].pop("plan_sha256")),
        [(["evidence", "plan_sha256"], "missing")],
    ),
    "plan_hash_short": (
        _mut(lambda d: d["evidence"].update(plan_sha256="ab12" * 15)),
        [(["evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "plan_hash_not_hex": (
        _mut(lambda d: d["evidence"].update(plan_sha256="g" * 64)),
        [(["evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "plan_hash_upper_case": (
        _mut(lambda d: d["evidence"].update(plan_sha256="A" * 64)),
        [(["evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "plan_hash_not_a_string": (
        _mut(lambda d: d["evidence"].update(plan_sha256=12345)),
        [(["evidence", "plan_sha256"], "string_type")],
    ),
    "files_missing": (
        _mut(lambda d: d["evidence"].pop("files")),
        [(["evidence", "files"], "missing")],
    ),
    "files_zero": (
        _mut(lambda d: d["evidence"].update(files=0)),
        [(["evidence", "files"], "greater_than_equal")],
    ),
    "files_above_limit": (
        _mut(lambda d: d["evidence"].update(files=10001)),
        [(["evidence", "files"], "less_than_equal")],
    ),
    "files_is_a_float": (
        _mut(lambda d: d["evidence"].update(files=1.5)),
        [(["evidence", "files"], "int_type")],
    ),
    "files_is_a_boolean": (
        _mut(lambda d: d["evidence"].update(files=True)),
        [(["evidence", "files"], "int_type")],
    ),
    "files_is_a_string": (
        _mut(lambda d: d["evidence"].update(files="3")),
        [(["evidence", "files"], "int_type")],
    ),
    "evidence_is_not_an_object": (
        _mut(lambda d: d.update(evidence="zz")),
        [(["evidence"], "model_type")],
    ),
    "dry_run_is_a_string": (
        _mut(lambda d: d.update(dry_run="true")),
        [(["dry_run"], "bool_type")],
    ),
    "dry_run_is_a_number": (_mut(lambda d: d.update(dry_run=1)), [(["dry_run"], "bool_type")]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(M10_CASES))
async def test_m10_exact_422_bodies(client, conn, clients, case):
    await seed_pair(conn)
    mutate, expected = M10_CASES[case]
    resp = await post(client, mutate(BASE))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [{"loc": loc, "code": code} for loc, code in expected]}
    assert await history(conn) == [] and await pointers(conn) == {}


@pytest.mark.asyncio
async def test_m10_the_only_basis_and_the_bounds_are_accepted(client, conn, clients):
    s, t = await seed_pair(conn)
    for files in (1, 10000):
        resp = await post(client, req(src(), tgt(), dry_run=True, files=files))
        assert resp.status_code == 200 and resp.json() == merged(s, t, True), files
    resp = await post(client, req(src(), tgt(), dry_run=True, basis="permit_stamp"))
    assert resp.json() == merged(s, t, True)


@pytest.mark.asyncio
async def test_m10_malformed_json_and_oversize_body(client, conn, clients):
    await seed_pair(conn)
    for raw in (b"{not json", b"", b"[1, 2]", b"null"):
        resp = await post(client, raw)
        assert resp.status_code == 422, raw
        body = resp.json()["detail"]
        assert (
            len(body) == 1
            and body[0]["loc"] == []
            and body[0]["code"] in {"json_invalid", "model_type"}
        )
    big = await post(client, b" " * (route_module.MAX_BODY_BYTES + 1))
    assert big.status_code == 422
    assert big.json() == {"detail": [{"loc": ["body"], "code": "too_long"}]}
    exact = await post(client, b" " * route_module.MAX_BODY_BYTES)
    assert exact.json()["detail"][0]["loc"] == []  # at the limit the body is parsed, not refused


def _sanitised(errors):
    resp = route_module._invalid_merges(errors)
    assert resp.status_code == 422
    return json.loads(resp.body)["detail"]


def test_m10_sanitiser_keeps_only_closed_names_indexes_and_codes():
    out = _sanitised(
        [
            {"loc": ("source", "identifiers", 19, "type"), "type": "literal_error"},
            {"loc": ("source", "identifiers", 20, "type"), "type": "literal_error"},
            {"loc": ("target", "identifiers", True, "value"), "type": "string_type"},
            {"loc": ("target", "identifiers", -1), "type": "int_type"},
            {"loc": ("source", 3, "kind"), "type": "literal_error"},
            {"loc": ("body", 0), "type": "too_long"},
            {"loc": ("evidence", "ZzHostile"), "type": "extra_forbidden"},
            {"loc": ("source_id",), "type": "extra_forbidden"},
            {"loc": ("evidence", "files"), "type": "weird_code"},
            {"loc": (), "type": "duplicate_identifier"},
            {"loc": ("source",), "type": "value_error"},
            {"loc": ("basis",), "type": "duplicate_name"},
            {"loc": ("basis",), "type": "blobs_above_files"},
        ]
    )
    assert out == [
        {"loc": ["source", "identifiers", 19, "type"], "code": "literal_error"},
        {"loc": ["source", "identifiers", "_", "type"], "code": "literal_error"},
        {"loc": ["target", "identifiers", "_", "value"], "code": "string_type"},
        {"loc": ["target", "identifiers", "_"], "code": "int_type"},
        {"loc": ["source", "_", "kind"], "code": "literal_error"},
        {"loc": ["body", "_"], "code": "too_long"},
        {"loc": ["evidence", "_"], "code": "extra_forbidden"},
        {"loc": ["_"], "code": "extra_forbidden"},
        {"loc": ["evidence", "files"], "code": "invalid"},
        {"loc": [], "code": "duplicate_identifier"},
        {"loc": ["source"], "code": "invalid"},
        {"loc": ["basis"], "code": "invalid"},
        {"loc": ["basis"], "code": "invalid"},
    ]


def test_m10_the_two_closed_lists_are_the_specified_ones():
    assert route_module._MERGES_LOC == {
        "body", "source", "target", "kind", "identifiers", "type", "value", "basis",
        "evidence", "plan_sha256", "files", "dry_run",
    }  # fmt: skip
    assert len(route_module._MERGES_CODE) == 20
    assert route_module._MERGES_CODE == route_module._NAMES_CODE - {
        "invalid_name",
        "blobs_above_files",
        "evidence_source_mismatch",
        "duplicate_name",
    }


def test_m10_the_sanitiser_caps_the_list_at_fifty_errors():
    assert len(_sanitised([{"loc": ("evidence", "files"), "type": "int_type"}] * 60)) == 50


# ── M11: precedence ───────────────────────────────────────────────────────────

BAD_BODIES = [b"{not json", json.dumps(req(src(), tgt(), files=0)).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_m11_the_flag_is_checked_before_the_body(pool, auth_on, monkeypatch, raw):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    spy_pool, spy_app = spy_client(pool, monkeypatch)
    async with spy_app as c:
        resp = await post(c, raw)
        no_key = await post(c, raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}
    assert no_key.status_code == 503 and no_key.json() == {"detail": "registry_unavailable"}
    assert spy_pool.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
@pytest.mark.parametrize("key", [None, "wrong-key"])
async def test_m11_the_key_is_checked_before_the_body(
    pool, enabled, auth_on, monkeypatch, raw, key
):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-CRM-Write-Key"] = key
    spy_pool, spy_app = spy_client(pool, monkeypatch)
    async with spy_app as c:
        resp = await post(c, raw, headers=headers)
    assert resp.status_code == 401 and resp.json() == {"detail": "unauthorized"}
    assert spy_pool.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_m11_no_pool_is_the_existing_503_before_the_body(enabled, auth_on, monkeypatch, raw):
    as_identity(monkeypatch, None)
    async with make_client(None) as c:
        resp = await post(c, raw)
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_m11_with_every_gate_open_an_invalid_body_is_422_and_runs_no_statement(
    pool, enabled, auth_on, audit_logger, monkeypatch, raw
):
    spy_pool, spy_app = spy_client(pool, monkeypatch)
    async with spy_app as c:
        resp = await post(c, raw)
    assert resp.status_code == 422
    assert spy_pool.acquired == 0 and spy_pool.calls == []


# ── M16: registration, public entry, the four older routes ────────────────────

FIVE_PATHS = {
    ("/api/crm/related-subjects/register", ("POST",)),
    ("/api/crm/related-subjects/documents/register", ("POST",)),
    ("/api/crm/related-subjects/names/register", ("POST",)),
    ("/api/crm/related-subjects/links/register", ("POST",)),
    ("/api/crm/related-subjects/merges/register", ("POST",)),
}


def test_m16_the_route_is_registered_in_the_sealed_router():
    assert len([e for e in ROUTER_MANIFEST if e.name == "crm_related_subjects"]) == 1
    paths = {(r.path, tuple(sorted(r.methods))) for r in route_module.router.routes}
    assert paths == FIVE_PATHS
    assert route_module.router.route_class is route_module._SealedRoute


def test_m16_public_entry_is_exact_path_and_post_only():
    entries = [e for e in PUBLIC_ENDPOINTS if e.prefix == URL]
    assert len(entries) == 1
    assert entries[0].match == "exact" and tuple(entries[0].methods) == ("POST",)
    assert any(e.matches(URL, "POST") for e in PUBLIC_ENDPOINTS)
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert not any(e.matches(URL, method) for e in PUBLIC_ENDPOINTS), method
    for path in (
        URL + "/",
        URL + "/x",
        "/api/crm/related-subjects/merges",
        "/api/crm/related-subjects",
    ):
        assert not any(e.matches(path, "POST") for e in PUBLIC_ENDPOINTS), path
    for path in (READ, f"{READ}/7"):
        assert not any(e.matches(path, "GET") for e in PUBLIC_ENDPOINTS), path
    # the four older entries are unchanged
    older = {
        URL_SUBJECT: None,
        URL_DOCUMENTS: ("POST",),
        URL_NAMES: ("POST",),
        URL_LINKS: ("POST",),
    }
    for path, methods in older.items():
        matching = [e for e in PUBLIC_ENDPOINTS if e.prefix == path]
        assert len(matching) == 1 and matching[0].match == "exact", path
        assert (None if matching[0].methods is None else tuple(matching[0].methods)) == methods


@pytest.mark.asyncio
async def test_m16_get_on_the_path_and_the_read_routes_stay_protected(client, conn):
    sid = await seed_subject(conn, "person", "ZzM16 Person", [("passport", passport(4247001))])
    for url in (URL, READ, f"{READ}/{sid}"):
        resp = await client.get(url)
        assert resp.status_code == 401, url
        assert "ZzM16" not in resp.text


@pytest.mark.asyncio
async def test_m16_the_four_older_write_routes_answer_as_before(client, conn):
    expected = {
        URL_SUBJECT: [("kind", "missing"), ("identifiers", "missing"), ("evidence", "missing")],
        URL_DOCUMENTS: [("subject", "missing"), ("documents", "missing"), ("evidence", "missing")],
        URL_NAMES: [("subject", "missing"), ("names", "missing")],
        URL_LINKS: [("subject", "missing"), ("links", "missing")],
    }
    for url, pairs in expected.items():
        resp = await post(client, {}, url=url)
        assert resp.status_code == 422, url
        assert resp.json() == {"detail": [{"loc": [loc], "code": code} for loc, code in pairs]}
    # a field name of this slice is still "_" on the older routes
    older = await post(client, {"basis": "permit_stamp", "kind": "person"}, url=URL_SUBJECT)
    assert {"loc": ["_"], "code": "extra_forbidden"} in older.json()["detail"]
    # and the merge route answers its own list for the same empty body
    empty = await post(client, {})
    assert empty.json() == {
        "detail": [
            {"loc": ["source"], "code": "missing"},
            {"loc": ["target"], "code": "missing"},
            {"loc": ["basis"], "code": "missing"},
            {"loc": ["evidence"], "code": "missing"},
        ]
    }


@pytest.mark.asyncio
async def test_m16_after_a_merge_the_four_other_routes_answer_as_section_3_4_says(
    client, conn, clients, writes, monkeypatch
):
    writes.update({("related_documents", "INSERT"), ("related_subject_documents", "INSERT")})
    t = await seed_subject(conn, "person", "ZzAfter Survivor", [("passport", passport(4247002))])
    m = await seed_subject(conn, "person", None, [("permit", permit(4247002))], merged=t)
    keys_m = {"kind": "person", "identifiers": keys(src(4247002))}
    evidence = {"plan_sha256": PLAN, "subject_ref": "REF-4247"}

    # POST /register: the owner is found by its keys; nothing is created
    body = {**keys_m, "evidence": {**evidence, "tier": "t1"}, "dry_run": False}
    first = await post(client, body, url=URL_SUBJECT)
    assert first.json() == {
        "outcome": "already_registered",
        "subject_id": m,
        "match_basis": "identifiers_only",
        "dry_run": False,
    }
    # POST /documents/register: resolves with no merged check; the document links to the merged-away record
    document = {
        "sha256": sha_of(4247),
        "byte_size": 4096,
        "mime_type": "application/pdf",
        "doc_type": "permit",
        "first_seen_on": "2026-01-05",
        "first_channel": "whatsapp",
        "drive": [],
    }
    second = await post(
        client,
        {"subject": keys_m, "documents": [document], "evidence": evidence, "dry_run": False},
        url=URL_DOCUMENTS,
    )
    assert second.status_code == 200, second.text
    owner = await conn.fetchval("SELECT subject_id FROM related_subject_documents")
    assert owner == m
    # POST /names/register and /links/register: refused, reason merged
    name = {
        "name": "ZZ AFTER NAME",
        "source": "ocr_single",
        "evidence": {"plan_sha256": PLAN, "files": 1, "blobs": 1, "mrz_valid": False},
    }
    third = await post(
        client, {"subject": keys_m, "names": [name], "dry_run": False}, url=URL_NAMES
    )
    assert third.json() == {"outcome": "subject_not_registered", "reason": "merged"}
    link = {"client_id": LIVE1, "basis": "identity", "evidence": {"plan_sha256": PLAN, "files": 1}}
    fourth = await post(
        client, {"subject": keys_m, "links": [link], "dry_run": False}, url=URL_LINKS
    )
    assert fourth.json() == {"outcome": "subject_not_registered", "reason": "merged"}


# ── M17: nothing is written outside the two tables ────────────────────────────

TABLE = "related_subject_merges"
FORBIDDEN_TABLES = (
    "clients",
    "client_family_members",
    "related_subject_names",
    "related_subject_links",
    "related_subject_documents",
    "related_documents",
    "related_document_sightings",
)
ALLOWED_NAMING_THE_TABLE = {
    "services/crm/related_merges.py",
    "app/routers/crm_related_subjects.py",
    "services/crm/related_registry_read.py",
    "app/routers/crm_related_registry_read.py",
}


def test_m17_the_writing_statements_are_the_insert_and_the_one_update_and_nothing_else():
    texts = sql_texts()
    writing = {
        name
        for name, sql in texts.items()
        if re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", without_lock_clause(sql), re.I)
    }
    assert writing == {"_INSERT_MERGE_SQL", "_UPDATE_POINTER_SQL"}
    assert re.match(r"INSERT INTO related_subject_merges \(", texts["_INSERT_MERGE_SQL"])
    assert re.match(r"UPDATE related_subjects SET ", texts["_UPDATE_POINTER_SQL"])
    for name, sql in texts.items():
        for table in FORBIDDEN_TABLES:
            assert not re.search(rf"\b{table}\b", sql), (name, table)
    identifiers = {n for n, s in texts.items() if "related_subject_identifiers" in s}
    assert identifiers == {"_OWNERS_SQL", "_OWNER_KEYS_SQL", "_TYPES_SQL"}
    registry = {n for n, s in texts.items() if re.search(r"\bFROM related_subjects\b", s)}
    assert registry == {"_MERGE_ROW_SQL", "_LOCK_SQL", "_SURVIVOR_SQL"}
    source = Path(merges_module.__file__).read_text()
    for table in ("clients", "client_family_members"):
        assert not re.search(rf"\b{table}\b", source), table
    handler = inspect.getsource(route_module.register_related_subject_merge)
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", handler)
    assert TABLE not in handler


def test_m17_only_the_named_modules_name_the_merges_table_and_no_portal_router_does():
    found = set()
    for root in (BACKEND / "app", BACKEND / "services"):
        for source in root.rglob("*.py"):
            if TABLE in source.read_text():
                found.add(str(source.relative_to(BACKEND)))
    assert found <= ALLOWED_NAMING_THE_TABLE, sorted(found - ALLOWED_NAMING_THE_TABLE)
    assert "services/crm/related_merges.py" in found
    for source in (BACKEND / "app" / "routers").glob("portal*.py"):
        assert TABLE not in source.read_text(), source.name
    # The raw table is readable through the admin data explorer, as the other registry tables are
    # (accepted, stated apart: it holds ids, a basis and counts).


@pytest.mark.asyncio
async def test_m17_the_write_measurement_sees_only_the_two_writes(client, conn, clients):
    s, t = await seed_pair(conn)
    await decorate(conn, s, t)
    await post(client, req(src(), tgt()))
    log = {(r["tbl"], r["op"]) for r in await conn.fetch("SELECT tbl, op FROM write_log")}
    assert log == {MERGES_INSERT, SUBJECTS_UPDATE, AUDIT_INSERT}
    assert await conn.fetchval("SELECT count(*) FROM client_family_members") == 0


# ── M18: privacy matrix ───────────────────────────────────────────────────────
#
# The helpers are the shared module's (backend/tests/fixtures/registry_privacy.py); this module owns
# the vocabulary of THIS route, the probe world, the cases and the runner.

PCO = passport(7373712)  # a company record that carries a passport (a person-kind refusal)
NCO2 = npwp(7373714)  # a company with a tax number (the target of a kind difference)
PNONE = passport(7373713)  # owned by nobody
NAME_SURVIVOR, NAME_BYSTANDER = "ZzProbeSurvivorName", "ZzProbeBystander"
STORED_PROBES = (
    NAME_SURVIVOR, NAME_BYSTANDER, PSV, QSV, QSRC, PBY, PCO, NCO2, QSRC2, QMG, QSUR, QMEM,
    QNP, NNP, QAL,
)  # fmt: skip
SENTINEL = "m18-sentinel"

PROTOCOL_ORIGIN = {
    **dict.fromkeys(
        (
            "source", "target", "kind", "identifiers", "type", "value", "basis", "evidence",
            "plan_sha256", "files", "dry_run",
        ),
        "request key",
    ),
    **dict.fromkeys(
        ("person", "company", "passport", "permit", "nik", "npwp", "nib", "ahu", "permit_stamp"),
        "closed value the model accepts",
    ),
    **dict.fromkeys(("same_identity", "manual"), "refused value M10 sends"),
    **dict.fromkeys(("true", "false", "null"), "JSON literal"),
    **dict.fromkeys(("source_id", "target_id"), "fixed output"),
}  # fmt: skip

SERVICE_LOG_WORDS = (
    "related",
    "merges",
    "registry",
    "outcome",
    "source_id",
    "target_id",
    "dry_run",
)
ROUTE_FAILURE_WORDS = ("related", "subject", "registry", "failure", "code")
PATH_SEGMENTS = ("api", "crm", "related-subjects", "merges", "register")
ANSWER_WORDS = frozenset(
    {
        *route_module._MERGES_LOC, *route_module._MERGES_CODE, "invalid", "_",
        "detail", "loc", "code", "outcome", "reason", "side", "source_id", "target_id", "dry_run",
        "merged", "already_merged", "subject_not_registered", "not_mergeable",
        "none_owned", "two_owners", "set_differs", "partial_overlap", "kind_differs",
        "same_subject", "target_is_merged", "source_merged_elsewhere", "source_is_survivor",
        "basis_precondition", "registry_unavailable", "registry_busy", "unauthorized",
        "database_error", "internal_error",
        *SERVICE_LOG_WORDS, *ROUTE_FAILURE_WORDS, URL, *PATH_SEGMENTS,
    }
)  # fmt: skip
VOCAB = privacy.Vocabulary(
    protocol_words=frozenset(PROTOCOL_ORIGIN),
    answer_words=ANSWER_WORDS,
    plan=PLAN,
    unconditional=frozenset(STORED_PROBES),
)
FIXED_WORDS = {w.casefold() for w in ANSWER_WORDS} | {
    w.casefold() for w in re.split(r"[^A-Za-z0-9_]+", URL) if w
}


@pytest_asyncio.fixture
async def probe_world(conn):
    w: dict[str, int] = {}
    w["sv"] = await seed_subject(
        conn, "person", NAME_SURVIVOR, [("passport", PSV), ("permit", QSV)]
    )
    w["src"] = await seed_subject(conn, "person", None, [("permit", QSRC)])
    w["by"] = await seed_subject(conn, "person", NAME_BYSTANDER, [("passport", PBY)])
    w["co"] = await seed_subject(conn, "company", None, [("passport", PCO)])
    w["co2"] = await seed_subject(conn, "company", None, [("npwp", NCO2)])
    w["src2"] = await seed_subject(conn, "person", None, [("permit", QSRC2)])
    w["mg"] = await seed_subject(conn, "person", None, [("permit", QMG)], merged=w["sv"])
    w["sur"] = await seed_subject(conn, "person", None, [("permit", QSUR)])
    w["mem"] = await seed_subject(conn, "person", None, [("permit", QMEM)], merged=w["sur"])
    w["np"] = await seed_subject(conn, "person", None, [("permit", QNP), ("nik", NNP)])
    w["al"] = await seed_subject(conn, "person", None, [("permit", QAL)], merged=w["sv"])
    return w


def _case(build, status, ids, *, service=False, route=False, quiet=False, patch=None):
    """build(world) -> list of (request, exact expected JSON body); `ids` the identifier probes the
    case's requests carry, declared by hand (R1)."""
    return {
        "build": build, "status": status, "ids": tuple(ids), "service": service,
        "route": route, "quiet": quiet, "patch": patch,
    }  # fmt: skip


def _a(build, ids, **kw):
    return _case(build, 200, ids, service=True, **kw)


def _validation(loc_codes_):
    return {"detail": [{"loc": loc, "code": code} for loc, code in loc_codes_]}


def _patch_parser(mp):
    def boom(cls, *a, **k):
        raise RuntimeError(f"boom {QSRC} {PSV}")

    mp.setattr(route_module.RelatedMergesRegisterRequest, "model_validate_json", classmethod(boom))


def _patch_database(mp):
    async def boom(*a, **k):
        raise asyncpg.PostgresError(f"boom {QSRC} {PSV}")

    mp.setattr(route_module, "record_merge", boom)


def _patch_inconsistent(mp):
    async def boom(*a, **k):
        raise RegistryInconsistent(f"boom {QSRC} {PSV}")

    mp.setattr(route_module, "record_merge", boom)


SRC_KEYS = (("permit", QSRC),)
MAIN_IDS = (QSRC, PSV, QSV)

# Family A: cases that reach the service (15 cases, 16 calls)
FAMILY_A = {
    "a_dry_run": _a(
        lambda w: [(req(SRC_KEYS, SURVIVOR_KEYS, dry_run=True), merged(w["src"], w["sv"], True))],
        MAIN_IDS,
    ),
    "a_apply_then_again": _a(
        lambda w: [
            (req(SRC_KEYS, SURVIVOR_KEYS), merged(w["src"], w["sv"])),
            (req(SRC_KEYS, SURVIVOR_KEYS), already(w["src"], w["sv"])),
        ],
        MAIN_IDS,
    ),
    "a_already_merged_pointer_seeded_without_history": _a(
        lambda w: [(req((("permit", QAL),), SURVIVOR_KEYS), already(w["al"], w["sv"]))],
        (QAL, PSV, QSV),
    ),
    "a_source_none_owned": _a(
        lambda w: [
            (req((("permit", QNONE),), SURVIVOR_KEYS), not_registered("source", "none_owned"))
        ],
        (QNONE, PSV, QSV),
    ),
    "a_source_two_owners": _a(
        lambda w: [
            (
                req((("permit", QSRC), ("permit", QSRC2)), SURVIVOR_KEYS),
                not_registered("source", "two_owners"),
            )
        ],
        (QSRC, QSRC2, PSV, QSV),
    ),
    "a_source_set_differs": _a(
        lambda w: [
            (req((("permit", QNP),), SURVIVOR_KEYS), not_registered("source", "set_differs"))
        ],
        (QNP, PSV, QSV),
    ),
    "a_source_partial_overlap": _a(
        lambda w: [
            (
                req((("permit", QNP), ("permit", QNONE)), SURVIVOR_KEYS),
                not_registered("source", "partial_overlap"),
            )
        ],
        (QNP, QNONE, PSV, QSV),
    ),
    "a_source_kind_differs": _a(
        lambda w: [
            (req((("passport", PCO),), SURVIVOR_KEYS), not_registered("source", "kind_differs"))
        ],
        (PCO, PSV, QSV),
    ),
    "a_target_none_owned": _a(
        lambda w: [(req(SRC_KEYS, (("passport", PNONE),)), not_registered("target", "none_owned"))],
        (QSRC, PNONE),
    ),
    "n_same_subject": _a(
        lambda w: [(req(SRC_KEYS, SRC_KEYS), refused("same_subject", w["src"], w["src"]))],
        (QSRC,),
    ),
    "n_kind_differs": _a(
        lambda w: [
            (
                req(SRC_KEYS, (("npwp", NCO2),), kind_t="company"),
                refused("kind_differs", w["src"], w["co2"]),
            )
        ],
        (QSRC, NCO2),
    ),
    "n_target_is_merged": _a(
        lambda w: [
            (req(SRC_KEYS, (("permit", QMG),)), refused("target_is_merged", w["src"], w["mg"]))
        ],
        (QSRC, QMG),
    ),
    "n_source_merged_elsewhere": _a(
        lambda w: [
            (
                req((("permit", QMG),), (("passport", PBY),)),
                refused("source_merged_elsewhere", w["mg"], w["by"]),
            )
        ],
        (QMG, PBY),
    ),
    "n_source_is_survivor": _a(
        lambda w: [
            (
                req((("permit", QSUR),), SURVIVOR_KEYS),
                refused("source_is_survivor", w["sur"], w["sv"]),
            )
        ],
        (QSUR, PSV, QSV),
    ),
    "n_basis_precondition": _a(
        lambda w: [
            (
                req((("permit", QNP), ("nik", NNP)), SURVIVOR_KEYS),
                refused("basis_precondition", w["np"], w["sv"]),
            )
        ],
        (QNP, NNP, PSV, QSV),
    ),
}

# Family B: every entry of M10_CASES on the probe body, plus malformed JSON and an oversize body.
# A mutation that removes or replaces a key set changes which probes the body still carries.
_EXTRA_IDS = tuple(v for _, v in EXTRA3)
B_IDS = {
    "source_value_not_a_string_at_index_3": (PSV, QSV, *_EXTRA_IDS),
    "target_type_unknown_at_index_3": (QSRC, *_EXTRA_IDS),
    "source_identifier_invalid": (PSV, QSV),
    "target_identifier_invalid": (QSRC, PSV),
    "source_identifiers_empty": (PSV, QSV),
    "target_twenty_one_identifiers": (QSRC, *(passport(7373800 + i) for i in range(21))),
    "missing_source": (PSV, QSV),
    "missing_target": (QSRC,),
}
FAMILY_B = {}
for _key, (_mutator, _expected) in M10_CASES.items():
    FAMILY_B["b_" + _key] = _case(
        lambda w, _m=_mutator, _e=_expected: [(_m(BASE), _validation(_e))],
        422,
        B_IDS.get(_key, MAIN_IDS),
        quiet=True,
    )
FAMILY_B["b_malformed_json"] = _case(
    lambda w: [
        (
            b'{"source": {"kind": "person", "identifiers": [{"type": "permit", "value": "'
            + QSRC.encode()
            + b'"',
            _validation([([], "json_invalid")]),
        )
    ],
    422,
    (QSRC,),
    quiet=True,
)
FAMILY_B["b_oversize_body"] = _case(
    lambda w: [
        (
            b" " * (route_module.MAX_BODY_BYTES + 1) + QSRC.encode(),
            _validation([(["body"], "too_long")]),
        )
    ],
    422,
    (QSRC,),
    quiet=True,
)

# Family C: injected failures (3)
FAMILY_C = {
    "c_parser_failure": _case(
        lambda w: [(req(SRC_KEYS, SURVIVOR_KEYS), {"detail": "internal_error"})],
        500, MAIN_IDS, route=True, patch=_patch_parser,
    ),
    "c_database_failure": _case(
        lambda w: [(req(SRC_KEYS, SURVIVOR_KEYS), {"detail": "database_error"})],
        503, MAIN_IDS, route=True, patch=_patch_database,
    ),
    "c_registry_inconsistent": _case(
        lambda w: [(req(SRC_KEYS, SURVIVOR_KEYS), {"detail": "internal_error"})],
        500, MAIN_IDS, route=True, patch=_patch_inconsistent,
    ),
}  # fmt: skip

M18_CASES = {**FAMILY_A, **FAMILY_B, **FAMILY_C}
FAKE_WORLD = {
    k: i + 1
    for i, k in enumerate(("sv", "src", "by", "co", "co2", "src2", "mg", "sur", "mem", "np", "al"))
}


def _steps(case, world):
    return M18_CASES[case]["build"](world)


def test_m18_the_matrix_has_every_case_of_its_specification():
    assert len(FAMILY_A) == 15 and len(FAMILY_C) == 3
    assert len(FAMILY_B) == len(M10_CASES) + 2
    assert len(M18_CASES) == len(M10_CASES) + 20
    calls = sum(len(_steps(case, FAKE_WORLD)) for case in M18_CASES)
    assert calls == len(M10_CASES) + 21
    assert privacy.unclassified_protocol_words(VOCAB, PROTOCOL_ORIGIN) == []
    assert set(PROTOCOL_ORIGIN) >= {"source_id", "target_id"}
    assert "merged_into" not in VOCAB.protocol_words and "created_by" not in VOCAB.protocol_words
    # no word of the fixed answers, the fixed log lines or the endpoint is a stored probe
    assert privacy.probe_forms(STORED_PROBES) & FIXED_WORDS == set()


@pytest.mark.parametrize("case", sorted(M18_CASES))
def test_m18_first_tests_probes_declared_present_and_collision_free_and_nothing_suppressed(case):
    spec = M18_CASES[case]
    steps = _steps(case, FAKE_WORLD)
    requests = [request for request, _ in steps]
    declared = spec["ids"]
    # R1: every declared probe is in the raw text of a request; a raw body holds nothing undeclared
    assert privacy.missing_declared(requests, declared) == [], case
    for request in requests:
        if isinstance(request, bytes):
            assert privacy.undeclared_runs(VOCAB, request, declared) == [], case
    log_set, answer_set = privacy.case_sets(VOCAB, requests, declared, ())
    # every declared probe is forbidden, in every non-empty form, on every surface
    assert privacy.probe_forms(declared) <= log_set and privacy.probe_forms(declared) <= answer_set
    assert privacy.probe_forms(STORED_PROBES) <= log_set
    # R4: no form of a probe can be, or be inside, a legitimate value of the case
    legitimate: set[str] = {str(spec["status"]), *VOCAB.protocol_words, *FIXED_WORDS}
    for _, expected in steps:
        strings: set[str] = set()
        ints: set[int] = set()
        privacy.leaves(expected, strings, ints)
        legitimate |= {s.casefold() for s in strings} | {str(i) for i in ints}
        legitimate.add(json.dumps(expected).casefold())
    legitimate |= set(FIXED_WORDS)
    probes = privacy.probe_forms([*declared, *STORED_PROBES])
    assert privacy.collisions(probes, legitimate) == [], case
    # R4: no word of the fixed answers, log lines or endpoint is forbidden
    assert (log_set | answer_set) & FIXED_WORDS == set(), case
    # R8: the vocabulary filter must not have removed a request string from either set
    for request in requests:
        assert privacy.suppressed(VOCAB, request, log_set, answer_set) == [], case


def test_m18_a_form_collision_check_fires_on_a_short_numeric_form():
    assert privacy.collisions({"12"}, {"412"}) == [("12", "412")]
    assert privacy.collisions({"7373701"}, {"200", "422", "1", "2"}) == []


def _spoke(records, module):
    return [r for r in records if r.name == module.__name__ and SENTINEL not in r.getMessage()]


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord("zz.teeth", logging.INFO, __file__, 1, "teeth %s", ("plain",), None)
    record.__dict__.update(extra)
    return record


AUDIT_TYPES = {
    "id": "integer",
    "error_message": "text",
    "response_body": "jsonb",
    "request_body": "jsonb",
    "zz_extra": "text",
    "created_at": "timestamp without time zone",
}
TEETH_PROBE = QSRC
TEETH_REQUESTS = {
    "dict": req((("permit", TEETH_PROBE),), SURVIVOR_KEYS),
    "raw": b'{"source": "' + TEETH_PROBE.encode() + b'"',
}


def _teeth_answer_set(kind):
    _, answer = privacy.case_sets(VOCAB, [TEETH_REQUESTS[kind]], [TEETH_PROBE], ())
    return answer


@pytest.mark.parametrize("kind", ["dict", "raw"])
def test_m18_teeth_an_identifier_alone_in_an_audit_error_message_is_caught(kind):
    row = {"id": 1, "error_message": f"boom {TEETH_PROBE}", "created_at": datetime.now()}
    assert privacy.leaks(_teeth_answer_set(kind), privacy.audit_texts([row], AUDIT_TYPES))
    clean = {"id": 1, "error_message": "boom", "created_at": datetime.now()}
    assert privacy.leaks(_teeth_answer_set(kind), privacy.audit_texts([clean], AUDIT_TYPES)) == []


def test_m18_teeth_an_identifier_in_a_jsonb_container_beside_a_non_ascii_value_is_caught():
    row = {"id": 1, "response_body": {"nota": "café 中", "k": f"x{TEETH_PROBE}y"}}
    assert privacy.leaks(_teeth_answer_set("dict"), privacy.audit_texts([row], AUDIT_TYPES))


def test_m18_teeth_an_identifier_in_a_double_encoded_jsonb_string_is_caught():
    # the probe is spelled with unicode escapes: only a DECODING scan reads it
    spelled = TEETH_PROBE.replace("Z99", "\\u005a99", 1)
    inner = '{"k": "' + spelled + '"}'
    assert TEETH_PROBE not in inner
    for value in (inner, json.dumps(inner)):
        row = {"id": 1, "request_body": value}
        assert privacy.leaks(_teeth_answer_set("dict"), privacy.audit_texts([row], AUDIT_TYPES)), (
            value
        )


def test_m18_teeth_a_probe_in_a_column_outside_the_usual_five_is_caught_by_type_not_by_name():
    row = {"id": 1, "zz_extra": TEETH_PROBE, "created_at": TEETH_PROBE}
    assert privacy.leaks(_teeth_answer_set("dict"), privacy.audit_texts([row], AUDIT_TYPES))
    only_timestamp = {"id": 1, "created_at": TEETH_PROBE}
    assert (
        privacy.leaks(_teeth_answer_set("dict"), privacy.audit_texts([only_timestamp], AUDIT_TYPES))
        == []
    )


def test_m18_teeth_an_undeclared_run_in_a_raw_body_is_caught():
    raw = TEETH_REQUESTS["raw"] + b" ZZ9191919"
    assert privacy.undeclared_runs(VOCAB, raw, [TEETH_PROBE]) == ["ZZ9191919"]
    assert privacy.undeclared_runs(VOCAB, TEETH_REQUESTS["raw"], [TEETH_PROBE]) == []


def test_m18_teeth_a_raw_body_probe_is_in_both_forbidden_sets_and_a_leak_of_it_is_caught():
    log_set, answer_set = privacy.case_sets(VOCAB, [TEETH_REQUESTS["raw"]], [TEETH_PROBE], ())
    for form in privacy.probe_forms([TEETH_PROBE]):
        assert form in log_set and form in answer_set, form
    assert privacy.leaks(answer_set, f"detail {TEETH_PROBE}".casefold())


def test_m18_teeth_a_probe_that_is_only_a_dictionary_key_in_a_container_is_caught():
    answer = _teeth_answer_set("dict")
    row = {"id": 1, "response_body": {TEETH_PROBE: 1}}
    assert privacy.leaks(answer, privacy.audit_texts([row], AUDIT_TYPES))
    record = _record(extra={TEETH_PROBE: 1})
    assert privacy.leaks(answer, privacy.log_surface([record], SENTINEL))
    assert privacy.leaks(
        answer, privacy.log_surface([_record(extra=[{"deep": TEETH_PROBE}])], SENTINEL)
    )
    assert privacy.leaks(answer, privacy.log_surface([_record()], SENTINEL)) == []


def test_m18_teeth_a_created_at_outside_the_clock_bracket_is_caught():
    low, high = datetime(2026, 1, 1, 12), datetime(2026, 1, 1, 13)
    inside = {"created_at": datetime(2026, 1, 1, 12, 30)}
    outside = {"created_at": datetime(2026, 1, 1, 14)}
    assert privacy.created_at_outside([inside], low, high) == []
    assert privacy.created_at_outside([inside, outside], low, high) == [outside["created_at"]]


def test_m18_teeth_the_provenance_check_fails_on_a_word_with_no_class():
    grown = privacy.Vocabulary(VOCAB.protocol_words | {"newword"}, ANSWER_WORDS, PLAN, frozenset())
    assert privacy.unclassified_protocol_words(grown, PROTOCOL_ORIGIN) == ["newword"]


def test_m18_teeth_the_suppression_guard_fails_when_the_filter_removes_a_request_string():
    leaf = "zzmarkerleaf"
    swallowing = privacy.Vocabulary(
        VOCAB.protocol_words, ANSWER_WORDS | {"xx" + leaf + "xx"}, PLAN, frozenset()
    )
    request = {"zzfield": leaf}
    log_set, answer_set = privacy.case_sets(swallowing, [request], [], ())
    assert privacy.suppressed(swallowing, request, log_set, answer_set) == [
        (leaf, leaf, "answer"),
    ]
    log_set, answer_set = privacy.case_sets(VOCAB, [request], [], ())
    assert privacy.suppressed(VOCAB, request, log_set, answer_set) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(M18_CASES))
async def test_m18_route_logs_and_answers_carry_no_identifier_name_or_body(
    case, client, conn, probe_world, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    spec = M18_CASES[case]
    steps = spec["build"](probe_world)
    requests = [request for request, _ in steps]

    # the capture is live for both modules, at any level
    logging.getLogger(route_module.__name__).debug(SENTINEL)
    logging.getLogger(merges_module.__name__).debug(SENTINEL)
    live = {r.name for r in caplog.records if SENTINEL in r.getMessage()}
    assert live == {route_module.__name__, merges_module.__name__}, case

    if spec["patch"]:
        spec["patch"](monkeypatch)
    low = await conn.fetchval("SELECT clock_timestamp()::timestamp")
    responses = [await post(client, request) for request in requests]
    high = await conn.fetchval("SELECT clock_timestamp()::timestamp")

    for resp, (_, expected) in zip(responses, steps, strict=True):
        assert resp.status_code == spec["status"], (case, resp.status_code, resp.text[:120])
        assert resp.json() == expected, case

    # the logger that must have spoken did; a validation case speaks through neither
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    if spec["service"]:
        assert _spoke(records, merges_module), (case, "service logger silent")
    if spec["route"]:
        assert _spoke(records, route_module), (case, "route logger silent")
    if spec["quiet"]:
        assert not _spoke(records, merges_module) and not _spoke(records, route_module), case

    log_set, answer_set = privacy.case_sets(VOCAB, requests, spec["ids"], ())
    for resp in responses:
        assert privacy.leaks(answer_set, resp.text.casefold()) == [], case
    assert privacy.leaks(log_set, privacy.log_surface(records, SENTINEL)) == [], case

    # one audit row per call, no request body, no token in any scanned column
    rows, types = await privacy.audit_snapshot(conn)
    excluded = {c for c, t in types.items() if t in privacy.UNSCANNED_AUDIT_TYPES}
    assert excluded == {"created_at"}, excluded
    await privacy.audit_created_at_is_server_generated(
        conn, inspect.getsource(activity_logger.log_api_call)
    )
    assert privacy.created_at_outside(rows, low, high) == [], case
    assert len(rows) == len(requests), case
    assert all(r["request_body"] is None for r in rows), case
    assert privacy.leaks(answer_set, privacy.audit_texts(rows, types)) == [], case
