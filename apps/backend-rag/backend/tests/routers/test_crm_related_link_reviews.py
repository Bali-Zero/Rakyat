"""Registry of related subjects, slice 4F: a CRM admin confirms or rejects a proposed client link,
on a real PostgreSQL, through the real middleware stack.

Each test builds a throwaway schema in the TEST database (the forward parts of migrations 361, 362,
365, 367, 369, 370, 371 and 372, minimal ``clients`` and ``client_family_members`` tables, the audit
table and a write log) and seeds invented rows: ``Zz`` names, passports ``ZZ`` plus digits, national
numbers of the invented region 990042, client ids from 424242 up. Nothing here is a real person,
document or client.

T1 migration, T2 auth, T3 validation, T4 outcomes, T5 merges, T6 concurrency, T7 rollback, T7b write
accounting, T8 the detail read (reviews and suggestions), T9 privacy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import backend.app.routers.crm_related_link_reviews as route_module
import backend.app.routers.crm_related_registry_read as read_module
import backend.app.routers.crm_related_subjects as sealed_module
import backend.services.crm.related_link_reviews as service_module
import backend.services.crm.related_merges as merges_module
from backend.app.auth.public_endpoints import PUBLIC_ENDPOINTS
from backend.app.core.config import settings
from backend.app.setup.router_manifest import ROUTER_MANIFEST
from backend.db.migration_base import split_migration_sql
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm.related_identifiers import canonical_form
from backend.services.crm.related_merges import record_merge
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures import registry_privacy as privacy
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

BACKEND = Path(route_module.__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "372_related_subject_link_reviews.sql"
READ = "/api/crm/related-registry"
URL = f"{READ}/link-reviews"
TABLE = "related_subject_link_reviews"

ADMIN = {"email": "Zz-Admin@Example.Test", "user_id": 1, "role": "admin"}
ACTOR = "zz-admin@example.test"
OTHER_ADMIN = {"email": "zz-second@example.test", "user_id": 6, "role": "admin"}
STAFF = {"email": "zz-staff@example.test", "user_id": 2, "role": "team_member"}
NO_EMAIL = {"user_id": 5, "role": "admin"}
SHORT_EMAIL = {"email": " ab ", "user_id": 7, "role": "admin"}

AUDIT_INSERT = ("api_audit_trail", "INSERT")
REVIEW_INSERT = (TABLE, "INSERT")
MERGE_WRITES = {("related_subject_merges", "INSERT"), ("related_subjects", "UPDATE")}

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)
C1, C2, C3 = 424242, 424243, 424244
PASSPORT = "ZZ1234567"
BIRTH = "1988-04-05"

FP_TABLES = (
    "related_subjects",
    "related_subject_identifiers",
    "related_documents",
    "related_document_sightings",
    "related_subject_documents",
    "related_subject_names",
    "related_subject_links",
    "related_subject_merges",
    "related_subject_detachments",
    "related_subject_birth_dates",
    TABLE,
    "clients",
    "client_family_members",
)

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
    passport_number varchar(100),
    date_of_birth date,
    deleted_at timestamptz
);
CREATE TABLE client_family_members (
    id serial PRIMARY KEY,
    client_id integer NOT NULL,
    full_name text NOT NULL,
    passport_number varchar(100),
    relationship text NOT NULL DEFAULT 'zz'
);
"""

# The trigger receives the key columns of its table as arguments and records the OLD and the NEW
# key, so a moved key or document link is named by both of its keys; for an UPDATE it records the
# sorted names of the columns whose value changed.
WRITE_LOG_SQL = """
CREATE TABLE write_log (
    id bigserial PRIMARY KEY, tbl text NOT NULL, op text NOT NULL,
    old_key jsonb, new_key jsonb, changed text[]
);
CREATE FUNCTION log_write() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE
    cols text[] := TG_ARGV;
    o jsonb;
    n jsonb;
    ch text[];
BEGIN
    IF TG_OP <> 'INSERT' THEN
        SELECT jsonb_object_agg(c, to_jsonb(OLD) -> c) INTO o FROM unnest(cols) AS c;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        SELECT jsonb_object_agg(c, to_jsonb(NEW) -> c) INTO n FROM unnest(cols) AS c;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        SELECT array_agg(k.key ORDER BY k.key) INTO ch
        FROM jsonb_object_keys(to_jsonb(NEW)) AS k(key)
        WHERE to_jsonb(NEW) -> k.key IS DISTINCT FROM to_jsonb(OLD) -> k.key;
    END IF;
    INSERT INTO write_log (tbl, op, old_key, new_key, changed)
    VALUES (TG_TABLE_NAME, TG_OP, o, n, ch);
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
DECLARE
    t text;
    cols text;
BEGIN
    FOR t IN SELECT table_name FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name <> 'write_log'
               AND table_type = 'BASE TABLE'
    LOOP
        IF t = 'related_subject_identifiers' THEN
            cols := quote_literal('id_type') || ', ' || quote_literal('id_norm') || ', '
                    || quote_literal('subject_id');
        ELSIF t = 'related_subject_documents' THEN
            cols := quote_literal('subject_id') || ', ' || quote_literal('sha256');
        ELSE
            SELECT string_agg(quote_literal(a.attname), ', ' ORDER BY k.ord) INTO cols
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = c.relnamespace
            CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
            WHERE i.indisprimary AND ns.nspname = current_schema() AND c.relname = t;
        END IF;
        EXECUTE format(
            'CREATE TRIGGER log_write AFTER INSERT OR UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION log_write(%s)', t, coalesce(cols, ''));
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
    schema = "related_review_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=10,
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
                "370_related_subject_detachments.sql",
                "371_related_subject_birth_dates.sql",
                "372_related_subject_link_reviews.sql",
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


async def seed_subject(c, kind, name, ids, *, merged=None, dob=None, at=T0):
    async with unmeasured(c):
        sid = await c.fetchval(
            "INSERT INTO related_subjects (kind, display_name, date_of_birth, merged_into, "
            "evidence, created_by, created_at) VALUES ($1, $2, $3, $4, '{}'::jsonb, 'zz-seed', $5) "
            "RETURNING id",
            kind, name, dob, merged, at,
        )  # fmt: skip
        for id_type, raw in ids:
            await c.execute(
                "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, "
                "norm_version, subject_id) VALUES ($1, $2, $3, 1, $4)",
                id_type, canonical_form(id_type, raw), raw, sid,
            )  # fmt: skip
    return sid


async def seed_name(c, sid, name, source="manual", *, at=T0):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_names (subject_id, name, source, evidence, created_by, "
            "created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
            sid, name, source, at,
        )  # fmt: skip


async def seed_link(c, sid, client_id, basis="identity", *, at=T0):
    async with unmeasured(c):
        return await c.fetchval(
            "INSERT INTO related_subject_links (subject_id, client_id, basis, evidence, created_by, "
            "created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4) RETURNING id",
            sid, client_id, basis, at,
        )  # fmt: skip


async def seed_client(c, client_id, name, *, passport=None, dob=None, deleted=False):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO clients (id, full_name, passport_number, date_of_birth, deleted_at) "
            "OVERRIDING SYSTEM VALUE VALUES ($1, $2, $3, $4, $5)",
            client_id, name, passport, dob, T0 if deleted else None,
        )  # fmt: skip


async def seed_review(c, sid, client_id, decision, relation, *, at=T0, by="zz-seed@example.test"):
    async with unmeasured(c):
        return await c.fetchval(
            f"INSERT INTO {TABLE} (subject_id, client_id, decision, relation, decided_by, "
            "created_at) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            sid, client_id, decision, relation, by, at,
        )  # fmt: skip


async def reviews(c):
    return [
        dict(r)
        for r in await c.fetch(
            f"SELECT id, subject_id, client_id, decision, relation, decided_by FROM {TABLE} "
            "ORDER BY id"
        )
    ]


async def fingerprint(c, tables=FP_TABLES):
    return {
        t: await c.fetchval(
            f"SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {t} x"
        )
        for t in tables
    }


async def non_audit_writes(c):
    rows = await c.fetch("SELECT tbl, op FROM write_log ORDER BY id")
    return [(r["tbl"], r["op"]) for r in rows if (r["tbl"], r["op"]) != AUDIT_INSERT]


async def world(c, *, name="ZzReview Person", clients=(C1, C2), subject_dob=None):
    """One unmerged person subject with a link to each client; returns the subject id."""
    sid = await seed_subject(c, "person", name, [("passport", PASSPORT)], dob=subject_dob)
    for n, cid in enumerate(clients):
        await seed_client(c, cid, f"ZzClient {n}")
        await seed_link(c, sid, cid, at=T0 + n * DAY)
    return sid


# ── a real-stack app ──────────────────────────────────────────────────────────


class _Alerts:
    async def send_http_error_alert(self, **kwargs):
        return None

    async def send_latency_alert(self, **kwargs):
        return None


class _BoomPool:
    """A pool whose acquire raises a database error carrying a protected value in its message."""

    def acquire(self):
        raise asyncpg.PostgresError("zz boom ZZBOOM99X")


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True, raising=False)


@pytest.fixture
def ident(monkeypatch):
    """Sets the identity the real middleware authenticates for the next requests."""

    def set_identity(identity):
        async def fake(self, request):
            return dict(identity) if identity else None

        monkeypatch.setattr(HybridAuthMiddleware, "authenticate_request", fake)

    return set_identity


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
async def client(pool, auth_on, audit_logger, writes, ident):
    """An admin session on the real stack; a test that records adds the review INSERT itself."""
    ident(ADMIN)
    async with make_client(pool) as c:
        yield c


@pytest.fixture
def recording(writes):
    writes.add(REVIEW_INSERT)


def body(sid, client_id=C1, decision="confirmed", relation="self"):
    out = {"subject_id": sid, "client_id": client_id, "decision": decision}
    if relation is not None or decision == "confirmed":
        out["relation"] = relation
    return out


async def post(c, data, *, headers=None):
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return await c.post(
        URL, content=raw, headers={"Content-Type": "application/json", **(headers or {})}
    )


async def detail(c, sid):
    resp = await c.get(f"{READ}/{sid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def link_of(data, client_id, basis=None):
    return next(
        link
        for link in data["links"]
        if link["client_id"] == client_id and (basis is None or link["basis"] == basis)
    )


# ── T1 migration ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t1_the_table_constraints_refuse_what_the_application_never_writes(pool, conn):
    sid = await seed_subject(conn, "person", "ZzT1 Person", [("passport", PASSPORT)])
    ok = [sid, C1, "confirmed", "self", "zz-a@example.test"]
    sql = f"INSERT INTO {TABLE} (subject_id, client_id, decision, relation, decided_by) VALUES ($1, $2, $3, $4, $5)"
    refused = [
        [sid, C1, "confirmed", None, "zz-a@example.test"],
        [sid, C1, "rejected", "self", "zz-a@example.test"],
        [sid, C1, "confirmed", "parent", "zz-a@example.test"],
        [sid, 0, "confirmed", "self", "zz-a@example.test"],
        [sid, C1, "confirmed", "self", "ab"],
        [sid, C1, "maybe", None, "zz-a@example.test"],
    ]
    async with unmeasured(conn):
        for args in refused:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(sql, *args)
        await conn.execute(sql, *ok)
        await conn.execute(sql, sid, C1, "rejected", None, "zz-a@example.test")
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(sql, 999999, C1, "confirmed", "self", "zz-a@example.test")
        assert await conn.fetchval(f"SELECT count(*) FROM {TABLE}") == 2


@pytest.mark.asyncio
async def test_t1_the_review_rows_follow_their_subject_on_delete_and_the_index_exists(pool, conn):
    sid = await seed_subject(conn, "person", "ZzT1 Cascade", [("passport", PASSPORT)])
    await seed_review(conn, sid, C1, "confirmed", "self")
    index = await conn.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = $1 "
        "AND indexname LIKE '%effective'",
        TABLE,
    )
    assert "(subject_id, client_id, created_at DESC, id DESC)" in index
    async with unmeasured(conn):
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", sid)
        assert await conn.fetchval(f"SELECT count(*) FROM {TABLE}") == 0


@pytest.mark.asyncio
async def test_t1_the_rollback_section_drops_the_table_and_nothing_else(pool, conn):
    forward, rollback = split_migration_sql(MIGRATION.read_text())
    assert "CREATE TABLE IF NOT EXISTS related_subject_link_reviews" in forward
    assert rollback == "DROP TABLE IF EXISTS related_subject_link_reviews;"
    async with unmeasured(conn):
        await conn.execute(rollback)
        present = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = "
            "current_schema() AND table_name LIKE 'related\\_%'"
        )
    names = {r["table_name"] for r in present}
    assert TABLE not in names and "related_subjects" in names and "related_subject_links" in names


# ── T2 auth ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t2_a_non_admin_gets_403_and_no_row_and_the_body_is_not_read_first(
    client, conn, ident
):
    sid = await world(conn)
    ident(STAFF)
    resp = await post(client, body(sid))
    assert (resp.status_code, resp.json()) == (403, {"detail": "CRM admin required"})
    invalid = await post(client, b"{ not json")
    assert (invalid.status_code, invalid.json()) == (403, {"detail": "CRM admin required"})
    huge = await post(client, b"x" * 70000)
    assert huge.status_code == 403
    assert await reviews(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [NO_EMAIL, SHORT_EMAIL, {"email": "", "role": "admin"}])
async def test_t2_an_admin_without_a_usable_email_gets_403_and_no_row(
    client, conn, ident, identity
):
    sid = await world(conn)
    ident(identity)
    resp = await post(client, body(sid))
    assert (resp.status_code, resp.json()) == (403, {"detail": "CRM admin required"})
    assert await reviews(conn) == []


@pytest.mark.asyncio
async def test_t2_an_unauthenticated_request_is_401_as_on_the_read_routes(client, conn, ident):
    sid = await world(conn)
    ident(None)
    assert (await post(client, body(sid))).status_code == 401
    assert (await client.get(f"{READ}/{sid}")).status_code == 401
    keyed = await post(client, body(sid), headers={"X-CRM-Write-Key": "zz-machine-key-0000"})
    assert keyed.status_code == 401
    assert await reviews(conn) == []


@pytest.mark.asyncio
async def test_t2_the_route_is_authenticated_admin_gated_and_never_public(
    client, conn, ident, monkeypatch, recording
):
    assert not any(e.matches(URL, "POST") for e in PUBLIC_ENDPOINTS)
    assert not any(e.matches(URL, method) for e in PUBLIC_ENDPOINTS for method in ("GET", "PUT"))
    entries = [e for e in ROUTER_MANIFEST if e.name == "crm_related_link_reviews"]
    assert len(entries) == 1 and "crm-related-registry" in entries[0].tags
    assert route_module.router.prefix == read_module.router.prefix
    assert {tuple(sorted(r.methods)) for r in route_module.router.routes} == {("POST",)}
    assert [r.path for r in route_module.router.routes] == [URL]
    # the route is not behind the registry flag
    sid = await world(conn)
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    resp = await post(client, body(sid))
    assert resp.status_code == 200 and resp.json()["status"] == "recorded"


# ── T3 validation ─────────────────────────────────────────────────────────────

GOOD = {"subject_id": 1, "client_id": C1, "decision": "confirmed", "relation": "self"}
EMAIL_SHAPED = "zz-probe-value@example.test"


def mk(**over):
    out = dict(GOOD)
    for key, value in over.items():
        if value is KeyError:
            out.pop(key)
        else:
            out[key] = value
    return out


def err(loc, code):
    return {"loc": loc, "code": code}


# (label, raw request, the exact detail list, strings of the request that must not come back)
INVALID = [
    ("unknown_decision", mk(decision="maybe"), [err(["decision"], "literal_error")], ["maybe"]),
    (
        "email_decision",
        mk(decision=EMAIL_SHAPED),
        [err(["decision"], "literal_error")],
        [EMAIL_SHAPED],
    ),
    ("confirmed_null", mk(relation=None), [err([], "relation_required")], []),
    ("confirmed_absent", mk(relation=KeyError), [err([], "relation_required")], []),
    (
        "rejected_with_relation",
        mk(decision="rejected"),
        [err([], "relation_not_allowed")],
        [],
    ),
    ("relation_parent", mk(relation="parent"), [err(["relation"], "literal_error")], ["parent"]),
    (
        "extra_email_field",
        {**GOOD, EMAIL_SHAPED: 1},
        [err(["_"], "extra_forbidden")],
        [EMAIL_SHAPED],
    ),
    ("client_zero", mk(client_id=0), [err(["client_id"], "greater_than_equal")], []),
    ("client_negative", mk(client_id=-5), [err(["client_id"], "greater_than_equal")], []),
    ("client_string", mk(client_id="424242"), [err(["client_id"], "int_type")], []),
    (
        "client_text",
        mk(client_id="ZzFreeTextProbe"),
        [err(["client_id"], "int_type")],
        ["ZzFreeTextProbe"],
    ),
    ("client_float", mk(client_id=424242.5), [err(["client_id"], "int_type")], []),
    ("client_bool", mk(client_id=True), [err(["client_id"], "int_type")], []),
    ("subject_zero", mk(subject_id=0), [err(["subject_id"], "greater_than_equal")], []),
    ("subject_string", mk(subject_id="7"), [err(["subject_id"], "int_type")], []),
    (
        "everything_missing",
        {},
        [
            err(["subject_id"], "missing"),
            err(["client_id"], "missing"),
            err(["decision"], "missing"),
        ],
        [],
    ),
    ("malformed_json", b'{"subject_id": 1, "client_id": 424242, ', [err([], "json_invalid")], []),
    ("json_array", [1, 2, 3], [err([], "model_type")], []),
    ("too_long", b'{"pad": "' + b"x" * 70000 + b'"}', [err(["body"], "too_long")], []),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,raw,expected,_probes", INVALID, ids=[c[0] for c in INVALID])
async def test_t3_every_invalid_body_is_a_closed_422_and_writes_nothing(
    client, conn, label, raw, expected, _probes
):
    await world(conn)
    resp = await post(client, raw)
    assert resp.status_code == 422, (label, resp.text)
    assert resp.json() == {"detail": expected}
    assert await reviews(conn) == []
    assert await non_audit_writes(conn) == []


@pytest.mark.asyncio
async def test_t3_the_model_validator_is_the_one_that_refuses_a_relation_on_a_rejection(
    client, conn
):
    sid = await world(conn)
    resp = await post(client, body(sid, decision="rejected", relation="self"))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [err([], "relation_not_allowed")]}


@pytest.mark.asyncio
async def test_t3_the_route_answers_its_own_errors_never_the_generic_422_shape(client, conn):
    sid = await world(conn)
    resp = await post(client, body(sid, client_id=0))
    assert set(resp.json()) == {"detail"} and isinstance(resp.json()["detail"], list)
    for item in resp.json()["detail"]:
        assert set(item) == {"loc", "code"}


# ── T4 outcomes ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t4_an_unknown_subject_is_404_subject_not_found(client, conn):
    await world(conn)
    resp = await post(client, body(999999))
    assert (resp.status_code, resp.json()) == (404, {"detail": "subject_not_found"})
    assert await reviews(conn) == []


@pytest.mark.asyncio
async def test_t4_a_merged_subject_is_409_subject_is_merged(client, conn):
    survivor = await world(conn)
    member = await seed_subject(
        conn, "person", "ZzMerged Person", [("passport", "ZZ7654321")], merged=survivor
    )
    await seed_link(conn, member, C3)
    resp = await post(client, body(member, C3))
    assert (resp.status_code, resp.json()) == (409, {"detail": "subject_is_merged"})
    assert await reviews(conn) == []


@pytest.mark.asyncio
async def test_t4_a_client_the_subject_has_no_link_to_is_404_link_not_found(client, conn):
    sid = await world(conn)
    resp = await post(client, body(sid, C3))
    assert (resp.status_code, resp.json()) == (404, {"detail": "link_not_found"})
    assert await reviews(conn) == []


@pytest.mark.asyncio
async def test_t4_a_decision_is_recorded_with_the_lower_cased_actor_on_the_opened_subject(
    client, conn, recording
):
    sid = await world(conn)
    resp = await post(client, body(sid, C1, "confirmed", "spouse"))
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"status", "review"} and data["status"] == "recorded"
    assert data["review"]["decision"] == "confirmed" and data["review"]["relation"] == "spouse"
    assert data["review"]["decided_by"] == ACTOR
    assert datetime.fromisoformat(data["review"]["decided_at"]).tzinfo is not None
    rows = await reviews(conn)
    assert [
        (r["subject_id"], r["client_id"], r["decision"], r["relation"], r["decided_by"])
        for r in rows
    ] == [(sid, C1, "confirmed", "spouse", ACTOR)]
    rejected = await post(client, body(sid, C2, "rejected", None))
    assert rejected.json()["review"]["relation"] is None
    assert [r["decision"] for r in await reviews(conn)] == ["confirmed", "rejected"]


@pytest.mark.asyncio
async def test_t4_the_same_request_twice_is_unchanged_and_the_second_writes_nothing(
    client, conn, recording
):
    sid = await world(conn)
    first = await post(client, body(sid))
    before = await fingerprint(conn)
    second = await post(client, body(sid))
    assert second.status_code == 200 and second.json()["status"] == "unchanged"
    assert second.json()["review"] == first.json()["review"]
    assert await fingerprint(conn) == before
    assert len(await reviews(conn)) == 1


@pytest.mark.asyncio
async def test_t4_a_decision_is_changed_by_a_later_row_and_the_latest_stays_effective(
    client, conn, recording
):
    sid = await world(conn)

    async def stamp(rows_expected, at):
        rows = await reviews(conn)
        assert len(rows) == rows_expected
        async with unmeasured(conn):
            await conn.execute(
                f"UPDATE {TABLE} SET created_at = $2 WHERE id = $1", rows[-1]["id"], at
            )

    assert (await post(client, body(sid, C1, "confirmed", "self"))).json()["status"] == "recorded"
    await stamp(1, T0)
    assert (await post(client, body(sid, C1, "rejected", None))).json()["status"] == "recorded"
    await stamp(2, T0 + DAY)
    assert (await post(client, body(sid, C1, "confirmed", "spouse"))).json()["status"] == "recorded"
    await stamp(3, T0 + 2 * DAY)
    review = link_of(await detail(client, sid), C1)["review"]
    assert (review["decision"], review["relation"]) == ("confirmed", "spouse")
    again = await post(client, body(sid, C1, "confirmed", "spouse"))
    assert again.json()["status"] == "unchanged"
    assert len(await reviews(conn)) == 3


@pytest.mark.asyncio
async def test_t4_with_equal_timestamps_the_greater_id_is_the_effective_review(
    client, conn, recording
):
    sid = await world(conn)
    first = await seed_review(conn, sid, C1, "confirmed", "self", at=T0)
    second = await seed_review(conn, sid, C1, "rejected", None, at=T0)
    assert second > first
    review = link_of(await detail(client, sid), C1)["review"]
    assert review["decision"] == "rejected"
    resp = await post(client, body(sid, C1, "rejected", None))
    assert resp.json()["status"] == "unchanged"
    assert len(await reviews(conn)) == 2


@pytest.mark.asyncio
async def test_t4_two_admins_keep_both_rows_and_the_second_wins(client, conn, ident, recording):
    sid = await world(conn)
    await post(client, body(sid, C1, "confirmed", "self"))
    ident(OTHER_ADMIN)
    await post(client, body(sid, C1, "confirmed", "other"))
    rows = await reviews(conn)
    assert [r["decided_by"] for r in rows] == [ACTOR, "zz-second@example.test"]
    review = link_of(await detail(client, sid), C1)["review"]
    assert (review["relation"], review["decided_by"]) == ("other", "zz-second@example.test")


# ── T5 merges ─────────────────────────────────────────────────────────────────


async def merged_world(c):
    survivor = await seed_subject(c, "person", "ZzSurvivor Person", [("passport", PASSPORT)])
    member = await seed_subject(
        c, "person", "ZzMember Person", [("permit", "Z99ZZ00000000001")], merged=survivor
    )
    await seed_client(c, C1, "ZzClient Zero")
    await seed_link(c, member, C1)
    return survivor, member


@pytest.mark.asyncio
async def test_t5_a_link_on_a_merged_in_record_is_reviewable_on_the_survivor(
    client, conn, recording
):
    survivor, member = await merged_world(conn)
    resp = await post(client, body(survivor))
    assert resp.status_code == 200 and resp.json()["status"] == "recorded"
    assert [r["subject_id"] for r in await reviews(conn)] == [survivor]


@pytest.mark.asyncio
async def test_t5_a_review_written_on_the_member_counts_on_the_survivor(client, conn, recording):
    survivor, member = await merged_world(conn)
    await seed_review(conn, member, C1, "confirmed", "employee", at=T0)
    review = link_of(await detail(client, survivor), C1)["review"]
    assert review["relation"] == "employee"
    same = await post(client, body(survivor, C1, "confirmed", "employee"))
    assert same.json()["status"] == "unchanged"
    assert await reviews(conn) == [
        {"id": 1, "subject_id": member, "client_id": C1, "decision": "confirmed",
         "relation": "employee", "decided_by": "zz-seed@example.test"}
    ]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("survivor_later", [True, False])
async def test_t5_between_a_review_on_the_survivor_and_one_on_the_member_the_latest_wins(
    client, conn, recording, survivor_later
):
    survivor, member = await merged_world(conn)
    on_survivor, on_member = (T0 + DAY, T0) if survivor_later else (T0, T0 + DAY)
    await seed_review(conn, survivor, C1, "confirmed", "self", at=on_survivor)
    await seed_review(conn, member, C1, "confirmed", "child", at=on_member)
    review = link_of(await detail(client, survivor), C1)["review"]
    assert review["relation"] == ("self" if survivor_later else "child")
    again = await post(client, body(survivor, C1, "confirmed", review["relation"]))
    assert again.json()["status"] == "unchanged"


# ── T6 concurrency: the wait is observed, with the real merge service ─────────


async def wait_blocked(conn, needle, timeout=8.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        n = await conn.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query LIKE $1",
            f"%{needle}%",
        )
        if n:
            return True
        await asyncio.sleep(0.02)
    return False


def merge_request(source_keys, target_keys):
    data = {
        "source": {
            "kind": "person",
            "identifiers": [{"type": t, "value": v} for t, v in source_keys],
        },
        "target": {
            "kind": "person",
            "identifiers": [{"type": t, "value": v} for t, v in target_keys],
        },
        "basis": "permit_stamp",
        "evidence": {"plan_sha256": "ef56" * 16, "files": 1},
        "dry_run": False,
    }
    return sealed_module.RelatedMergesRegisterRequest.model_validate_json(
        json.dumps(data)
    ).to_input()


PERMIT = "Z99ZZ00000000007"


async def merge_pair(c):
    """S: a permit-only record with a link to a client; T: a record with a passport."""
    source = await seed_subject(c, "person", "ZzSource Person", [("permit", PERMIT)])
    target = await seed_subject(c, "person", "ZzTarget Person", [("passport", "ZZ7777777")])
    await seed_client(c, C1, "ZzClient Zero")
    await seed_link(c, source, C1)
    return source, target


@pytest.mark.asyncio
async def test_t6a_a_review_waits_for_a_merge_holding_the_subject_then_answers_subject_is_merged(
    client, pool, conn, monkeypatch, writes
):
    writes |= MERGE_WRITES
    source, target = await merge_pair(conn)
    entered, release = asyncio.Event(), asyncio.Event()
    original = merges_module._decide_on_data

    async def paused(c, s, t):
        entered.set()
        await release.wait()
        return await original(c, s, t)

    monkeypatch.setattr(merges_module, "_decide_on_data", paused)

    async def merge():
        async with pool.acquire() as c:
            return await record_merge(
                c, merge_request([("permit", PERMIT)], [("passport", "ZZ7777777")]), dry_run=False
            )

    merge_task = asyncio.create_task(merge())
    await asyncio.wait_for(entered.wait(), 8)
    review_task = asyncio.create_task(post(client, body(source)))
    assert await wait_blocked(conn, "FOR SHARE"), "the review was not observed waiting"
    assert not review_task.done()
    release.set()
    assert (await merge_task)["outcome"] == "merged"
    resp = await asyncio.wait_for(review_task, 8)
    assert (resp.status_code, resp.json()) == (409, {"detail": "subject_is_merged"})
    assert await reviews(conn) == []


@pytest.mark.asyncio
async def test_t6b_a_merge_waits_for_a_review_holding_the_subject_and_the_review_follows(
    client, pool, conn, writes
):
    writes |= MERGE_WRITES | {REVIEW_INSERT}
    source, target = await merge_pair(conn)
    async with unmeasured(conn):
        await conn.execute(
            """
            CREATE FUNCTION zz_hold() RETURNS trigger LANGUAGE plpgsql AS $f$
            BEGIN PERFORM pg_advisory_xact_lock(424242); RETURN NEW; END $f$;
            CREATE TRIGGER zz_hold BEFORE INSERT ON related_subject_link_reviews
            FOR EACH ROW EXECUTE FUNCTION zz_hold();
            """
        )
        await conn.execute("SELECT pg_advisory_lock(424242)")
    review_task = asyncio.create_task(post(client, body(source)))
    assert await wait_blocked(conn, "INSERT INTO related_subject_link_reviews"), "review not held"

    async def merge():
        async with pool.acquire() as c:
            return await record_merge(
                c, merge_request([("permit", PERMIT)], [("passport", "ZZ7777777")]), dry_run=False
            )

    merge_task = asyncio.create_task(merge())
    assert await wait_blocked(conn, "FOR UPDATE"), "the merge was not observed waiting"
    assert not merge_task.done() and not review_task.done()
    await conn.execute("SELECT pg_advisory_unlock(424242)")
    resp = await asyncio.wait_for(review_task, 8)
    assert resp.status_code == 200 and resp.json()["status"] == "recorded"
    assert (await asyncio.wait_for(merge_task, 8))["outcome"] == "merged"
    assert [r["subject_id"] for r in await reviews(conn)] == [source]
    review = link_of(await detail(client, target), C1)["review"]
    assert (review["decision"], review["relation"]) == ("confirmed", "self")


# ── T7 rollback, T7b write accounting, one log line ───────────────────────────


def route_lines(caplog):
    return [r for r in caplog.records if r.name == route_module.__name__]


@pytest.mark.asyncio
async def test_t7_a_failure_after_the_insert_and_before_the_commit_rolls_it_back(
    client, conn, monkeypatch, caplog, recording
):
    caplog.set_level(logging.DEBUG)
    sid = await world(conn)

    def boom(row):
        raise asyncpg.PostgresError("zz after insert ZZBOOM77")

    monkeypatch.setattr(service_module, "review_dict", boom)
    resp = await post(client, body(sid))
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_unavailable"})
    assert await reviews(conn) == []
    lines = route_lines(caplog)
    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert lines[0].getMessage() == (
        f"related link review: subject={sid} outcome=unavailable error=PostgresError"
    )


@pytest.mark.asyncio
async def test_t7_an_insert_that_itself_fails_is_a_separate_503_with_no_row(
    client, conn, monkeypatch, caplog, recording
):
    caplog.set_level(logging.DEBUG)
    sid = await world(conn)
    monkeypatch.setattr(
        service_module,
        "_INSERT_SQL",
        service_module._INSERT_SQL.replace("decided_by", "zz_no_column", 1),
    )
    resp = await post(client, body(sid))
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_unavailable"})
    assert await reviews(conn) == []
    assert len(route_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_t7_an_unreachable_pool_is_a_fixed_503_with_one_log_line(
    pool, auth_on, audit_logger, ident, caplog
):
    caplog.set_level(logging.DEBUG)
    ident(ADMIN)
    async with make_client(_BoomPool()) as c:
        resp = await post(c, body(5))
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_unavailable"})
    assert "ZZBOOM99X" not in resp.text
    assert [r.getMessage() for r in route_lines(caplog)] == [
        "related link review: subject=5 outcome=unavailable error=PostgresError"
    ]


@pytest.mark.asyncio
async def test_t7_exactly_one_application_line_per_outcome_and_none_carries_a_value(
    client, conn, ident, caplog, recording
):
    caplog.set_level(logging.DEBUG)
    sid = await world(conn)
    member = await seed_subject(
        conn, "person", "ZzMerged Line", [("passport", "ZZ7654321")], merged=sid
    )
    expected = []

    async def step(identity, data, outcome, subject):
        ident(identity)
        caplog.clear()
        await post(client, data)
        lines = [r.getMessage() for r in route_lines(caplog)]
        assert lines == [f"related link review: subject={subject} outcome={outcome}"], lines
        expected.append(outcome)

    await step(STAFF, body(sid), "forbidden", "-")
    await step(ADMIN, mk(decision="maybe"), "invalid", "-")
    await step(ADMIN, body(999999), "subject_not_found", 999999)
    await step(ADMIN, body(member, C1), "subject_is_merged", member)
    await step(ADMIN, body(sid, C3), "link_not_found", sid)
    await step(ADMIN, body(sid), "recorded", sid)
    await step(ADMIN, body(sid), "unchanged", sid)
    assert len(set(expected)) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["recorded", "change", "unchanged", "error"])
async def test_t7b_the_only_writes_are_the_review_insert_and_the_audit_row(
    client, conn, monkeypatch, recording, path
):
    sid = await world(conn)
    await seed_subject(conn, "person", "ZzBystander", [("passport", "ZZ1111111")])
    if path in ("change", "unchanged"):
        await seed_review(conn, sid, C1, "confirmed", "self", at=T0)
    before = await fingerprint(conn, [t for t in FP_TABLES if t != TABLE])
    if path == "error":

        def boom(row):
            raise asyncpg.PostgresError("zz")

        monkeypatch.setattr(service_module, "review_dict", boom)
    data = body(sid, C1, "confirmed", "self" if path == "unchanged" else "spouse")
    resp = await post(client, data)
    assert resp.status_code == (503 if path == "error" else 200)
    assert await fingerprint(conn, [t for t in FP_TABLES if t != TABLE]) == before
    expected = [REVIEW_INSERT] if path in ("recorded", "change") else []
    assert await non_audit_writes(conn) == expected
    rows = await reviews(conn)
    assert len(rows) == {"recorded": 1, "change": 2, "unchanged": 1, "error": 0}[path]


@pytest.mark.asyncio
async def test_t7b_a_refused_request_writes_nothing_but_the_audit_row(
    client, conn, ident, recording
):
    sid = await world(conn)
    before = await fingerprint(conn)
    ident(STAFF)
    await post(client, body(sid))
    ident(ADMIN)
    for data in (mk(decision="maybe"), body(999999), body(sid, C3)):
        await post(client, data)
    assert await fingerprint(conn) == before
    assert await non_audit_writes(conn) == []


# ── T8 the detail read ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t8_a_link_carries_no_review_a_confirmation_or_a_rejection(client, conn):
    sid = await world(conn, clients=(C1, C2, C3))
    await seed_review(conn, sid, C2, "confirmed", "child", at=T0, by="zz-reader@example.test")
    await seed_review(conn, sid, C3, "rejected", None, at=T0)
    data = await detail(client, sid)
    assert link_of(data, C1)["review"] is None
    confirmed = link_of(data, C2)["review"]
    assert {k: confirmed[k] for k in ("decision", "relation", "decided_by")} == {
        "decision": "confirmed", "relation": "child", "decided_by": "zz-reader@example.test",
    }  # fmt: skip
    assert datetime.fromisoformat(confirmed["decided_at"].replace("Z", "+00:00")) == T0
    rejected = link_of(data, C3)["review"]
    assert (rejected["decision"], rejected["relation"]) == ("rejected", None)


@pytest.mark.asyncio
async def test_t8_two_bases_of_one_client_carry_the_same_review(client, conn):
    sid = await world(conn, clients=(C1,))
    await seed_link(conn, sid, C1, "drive_folder", at=T0 + DAY)
    await seed_review(conn, sid, C1, "confirmed", "other", at=T0)
    data = await detail(client, sid)
    reviews_seen = [link["review"] for link in data["links"] if link["client_id"] == C1]
    assert len(reviews_seen) == 2 and reviews_seen[0] == reviews_seen[1] is not None


@pytest.mark.asyncio
async def test_t8_a_review_of_another_client_or_subject_is_not_shown(client, conn):
    sid = await world(conn, clients=(C1,))
    other = await seed_subject(conn, "person", "ZzOther Subject", [("passport", "ZZ2222222")])
    await seed_link(conn, other, C1)
    await seed_review(conn, other, C1, "confirmed", "self")
    assert link_of(await detail(client, sid), C1)["review"] is None


def nik(day, month, year):
    return f"990042{day:02d}{month:02d}{year:02d}4242"


async def suggestion(client, conn, *, subject_ids=(), subject_name="ZzMatch Person", subject_dob=None,
                     client_name="ZzMatch Person", passport=None, dob=None, deleted=False,
                     setup=None):  # fmt: skip
    sid = await seed_subject(conn, "person", subject_name, list(subject_ids), dob=subject_dob)
    await seed_client(conn, C1, client_name, passport=passport, dob=dob, deleted=deleted)
    await seed_link(conn, sid, C1)
    if setup:
        await setup(sid)
    link = link_of(await detail(client, sid), C1)
    return link["suggested_relation"], link["suggestion_basis"]


B_DATE = date(1988, 4, 5)

SUGGESTION_CASES = [
    # id, kwargs, (suggested_relation, basis)
    (
        "passport_equal",
        {"subject_ids": [("passport", PASSPORT)], "passport": "zz 123-4567"},
        ("self", ["passport_equal"]),
    ),
    (
        "one_char_apart",
        {"subject_ids": [("passport", PASSPORT)], "passport": "ZZ1234568"},
        ("self", ["passport_one_char_apart"]),
    ),
    ("two_apart", {"subject_ids": [("passport", PASSPORT)], "passport": "ZZ1234578"}, (None, [])),
    ("short_one_apart", {"subject_ids": [("passport", "ZZ123")], "passport": "ZZ124"}, (None, [])),
    (
        "different_length",
        {"subject_ids": [("passport", PASSPORT)], "passport": "ZZ12345678"},
        (None, []),
    ),
    (
        "nik_man",
        {"subject_ids": [("nik", nik(5, 4, 88))], "dob": B_DATE},
        ("self", ["nik_encodes_client_birth_date"]),
    ),
    (
        "nik_woman",
        {"subject_ids": [("nik", nik(45, 4, 88))], "dob": B_DATE},
        ("self", ["nik_encodes_client_birth_date"]),
    ),
    ("nik_other_day", {"subject_ids": [("nik", nik(6, 4, 88))], "dob": B_DATE}, (None, [])),
    ("nik_other_month", {"subject_ids": [("nik", nik(5, 5, 88))], "dob": B_DATE}, (None, [])),
    ("nik_other_year", {"subject_ids": [("nik", nik(5, 4, 89))], "dob": B_DATE}, (None, [])),
    (
        "nik_day_plus_forty_only_for_the_day",
        {"subject_ids": [("nik", nik(46, 4, 88))], "dob": B_DATE},
        (None, []),
    ),
    ("birth_date_equal", {"subject_dob": B_DATE, "dob": B_DATE}, ("self", ["birth_date_equal"])),
    ("birth_date_differs", {"subject_dob": date(1988, 4, 6), "dob": B_DATE}, (None, [])),
    (
        "every_basis_in_order",
        {
            "subject_ids": [("passport", PASSPORT), ("nik", nik(5, 4, 88))],
            "passport": PASSPORT,
            "dob": B_DATE,
            "subject_dob": B_DATE,
        },
        ("self", ["passport_equal", "nik_encodes_client_birth_date", "birth_date_equal"]),
    ),
    (
        "name_normalised",
        {
            "subject_ids": [("passport", PASSPORT)],
            "client_name": "  zzmatch    PERSON  ",
            "passport": PASSPORT,
        },
        ("self", ["passport_equal"]),
    ),
    (
        "name_differs_every_basis_true",
        {
            "subject_ids": [("passport", PASSPORT), ("nik", nik(5, 4, 88))],
            "client_name": "ZzOther Name",
            "passport": PASSPORT,
            "dob": B_DATE,
            "subject_dob": B_DATE,
        },
        (None, []),
    ),
    (
        "soft_deleted_client",
        {"subject_ids": [("passport", PASSPORT)], "passport": PASSPORT, "deleted": True},
        (None, []),
    ),
    (
        "client_without_passport_and_birth_date",
        {"subject_ids": [("passport", PASSPORT)]},
        (None, []),
    ),
    ("name_only", {}, (None, [])),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,kwargs,expected", SUGGESTION_CASES, ids=[c[0] for c in SUGGESTION_CASES]
)
async def test_t8_the_suggestion_matrix(client, conn, label, kwargs, expected):
    assert await suggestion(client, conn, **kwargs) == expected


@pytest.mark.asyncio
async def test_t8_the_name_and_the_keys_of_a_merged_in_member_count_but_its_birth_date_does_not(
    client, conn
):
    async def setup(sid):
        member = await seed_subject(
            conn,
            "person",
            "ZzDifferent Registration",
            [("passport", PASSPORT)],
            merged=sid,
            dob=B_DATE,
        )
        await seed_name(conn, member, "ZzMatch Person", "ocr_single")

    result = await suggestion(
        client, conn, subject_name="ZzSomething Else", passport=PASSPORT, dob=B_DATE, setup=setup
    )
    assert result == ("self", ["passport_equal"])


@pytest.mark.asyncio
async def test_t8_a_member_s_registration_name_matches_too(client, conn):
    async def setup(sid):
        await seed_subject(conn, "person", "ZzMatch Person", [("passport", PASSPORT)], merged=sid)

    result = await suggestion(
        client, conn, subject_name="ZzNothing Alike", passport=PASSPORT, setup=setup
    )
    assert result == ("self", ["passport_equal"])


@pytest.mark.asyncio
async def test_t8_the_list_is_unchanged_and_the_detail_adds_exactly_three_fields_per_link(
    client, conn
):
    sid = await world(conn)
    listing = (await client.get(READ)).json()
    assert set(listing["items"][0]) == {
        "id", "kind", "display_name", "name_source", "date_of_birth", "nationality",
        "identifiers", "document_count", "links_count",
    }  # fmt: skip
    data = await detail(client, sid)
    for link in data["links"]:
        assert set(link) == {
            "client_id", "client_name", "client_live", "basis", "relation", "created_at",
            "files", "from_subject_id", "review", "suggested_relation", "suggestion_basis",
        }  # fmt: skip


class _CountingConn:
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


@pytest.mark.asyncio
async def test_t8_the_detail_grows_by_at_most_two_selects(conn):
    from backend.services.crm import related_registry_read

    sid = await world(conn)
    counting = _CountingConn(conn)
    await related_registry_read.get_subject(counting, sid)
    with_links = counting.statements
    assert (
        with_links == 8
    )  # subject, identifiers, documents none, names, links, reviews, suggestions, detachments
    bare = await seed_subject(conn, "person", None, [("passport", "ZZ3333333")])
    counting = _CountingConn(conn)
    await related_registry_read.get_subject(counting, bare)
    assert counting.statements == 6


# ── T9 privacy ────────────────────────────────────────────────────────────────

CLIENT_NAME = "ZzPrivClient Probe"
CLIENT_PASSPORT = "ZZPRIVP77"
CLIENT_BIRTH = "1966-02-03"
PROTOCOL = {"subject_id", "client_id", "decision", "relation"}


def body_tokens(raw, extra=()):
    strings, ints = set(), set()
    if isinstance(raw, (bytes, bytearray)):
        strings.update(privacy.RUN.findall(raw.decode("latin-1")))
    else:
        privacy.leaves(raw, strings, ints)
    out = {s for s in strings if 4 <= len(s) <= 200 and s not in PROTOCOL} | {
        str(i) for i in ints if len(str(i)) >= 4
    }
    return out | set(extra)


def surfaces_leaks(tokens, resp, records, rows, types, *, status):
    """The leaks of every surface. A 200 answer is scanned without the four fields of `review`
    (decision, relation, deciding admin, timestamp); the audit rows without `user_email`."""
    forbidden = privacy.probe_forms(tokens)
    text = resp.text
    if status == 200:
        data = resp.json()
        assert set(data) == {"status", "review"}
        assert set(data["review"]) == {"decision", "relation", "decided_by", "decided_at"}
        text = json.dumps({"status": data["status"]})
    found = {"answer": privacy.leaks(forbidden, privacy.response_surface(text))}
    found["log"] = privacy.leaks(forbidden, privacy.log_surface(records))
    scrubbed = [{**r, "user_email": None} for r in rows]
    found["audit"] = privacy.leaks(forbidden, privacy.audit_texts(scrubbed, types))
    return {k: v for k, v in found.items() if v}


async def privacy_world(c):
    sid = await seed_subject(c, "person", "ZzPriv Subject", [("passport", "ZZSUBJ4321")])
    await seed_client(
        c, C1, CLIENT_NAME, passport=CLIENT_PASSPORT, dob=date.fromisoformat(CLIENT_BIRTH)
    )
    await seed_link(c, sid, C1)
    member = await seed_subject(
        c, "person", "ZzPriv Merged", [("passport", "ZZMEMB8765")], merged=sid
    )
    return sid, member


def post_cases(sid, member):
    probe = (CLIENT_NAME, CLIENT_PASSPORT, CLIENT_BIRTH, "19660203", str(C1), ACTOR)
    cases = {
        "forbidden": (STAFF, body(sid, C1, "confirmed", "spouse"), 403),
        "no_email": (NO_EMAIL, body(sid, C1, "confirmed", "spouse"), 403),
        "subject_not_found": (ADMIN, body(999999, C1, "confirmed", "spouse"), 404),
        "link_not_found": (ADMIN, body(sid, C3, "confirmed", "spouse"), 404),
        "subject_is_merged": (ADMIN, body(member, C1, "confirmed", "spouse"), 409),
        "recorded_confirmed": (ADMIN, body(sid, C1, "confirmed", "employee"), 200),
        "recorded_rejected": (ADMIN, body(sid, C1, "rejected", None), 200),
        "unchanged": (ADMIN, body(sid, C1, "confirmed", "employee"), 200),
    }
    for label, raw, _expected, _probes in INVALID:
        cases[f"invalid_{label}"] = (ADMIN, raw, 422)
    return cases, probe


CASE_NAMES = [
    "forbidden", "no_email", "subject_not_found", "link_not_found", "subject_is_merged",
    "recorded_confirmed", "recorded_rejected", "unchanged", "unavailable",
] + [f"invalid_{c[0]}" for c in INVALID]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASE_NAMES)
async def test_t9_post_answer_log_and_audit_row_carry_nothing_from_the_request(
    case, pool, auth_on, audit_logger, writes, ident, conn, caplog
):
    writes.add(REVIEW_INSERT)
    caplog.set_level(logging.DEBUG)
    sid, member = await privacy_world(conn)
    cases, probe = post_cases(sid, member)
    if case == "unavailable":
        identity, raw, status = ADMIN, body(sid, C1, "confirmed", "spouse"), 503
    else:
        identity, raw, status = cases[case]
    ident(identity)
    async with make_client(_BoomPool() if case == "unavailable" else pool) as c:
        if case == "unchanged":
            assert (await post(c, raw)).json()["status"] == "recorded"
            async with unmeasured(conn):
                await conn.execute("DELETE FROM api_audit_trail")
        caplog.clear()
        resp = await post(c, raw)
    assert resp.status_code == status, (case, resp.status_code, resp.text[:160])
    extra = list(probe)
    if case.startswith("invalid_"):
        extra += next(c[3] for c in INVALID if c[0] == case[len("invalid_") :])
    if status == 503:
        extra.append("ZZBOOM99X")
    tokens = body_tokens(raw, extra)
    rows, types = await privacy.audit_snapshot(conn)
    assert all(r["request_body"] is None for r in rows), case
    records = [r for r in caplog.records if not privacy.is_http_client_logger(r.name)]
    assert surfaces_leaks(tokens, resp, records, rows, types, status=status) == {}, case
    assert all(r["endpoint"] == URL for r in rows if r["method"] == "POST"), case


GET_CASES = {
    "matching": {
        "subject_ids": [("passport", "ZZGETMATCH1")],
        "passport": "ZZGETMATCH1",
        "client_name": "ZzGet Match",
    },
    "non_matching": {
        "subject_ids": [("passport", "ZZGETSUBJ2")],
        "passport": "ZZGETCLIE9",
        "dob": date(1971, 8, 9),
        "client_name": "ZzGet Client Other",
    },
    "dob_only": {
        "subject_dob": date(1971, 8, 9),
        "dob": date(1971, 8, 9),
        "client_name": "ZzGet Match",
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(GET_CASES))
async def test_t9_get_adds_closed_fields_only_and_never_a_copy_of_the_client_card(
    case, client, conn, caplog
):
    caplog.set_level(logging.DEBUG)
    spec = dict(GET_CASES[case])
    spec.setdefault("subject_name", "ZzGet Match")
    sid = await seed_subject(
        conn,
        "person",
        spec["subject_name"],
        spec.get("subject_ids", []),
        dob=spec.get("subject_dob"),
    )
    await seed_client(
        conn, C1, spec["client_name"], passport=spec.get("passport"), dob=spec.get("dob")
    )
    await seed_link(conn, sid, C1)
    await seed_review(conn, sid, C1, "confirmed", "self", at=T0, by="zz-reader@example.test")
    async with unmeasured(conn):
        await conn.execute("DELETE FROM api_audit_trail")
    caplog.clear()
    resp = await client.get(f"{READ}/{sid}")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {
        "id", "kind", "display_name", "name_source", "names", "names_total", "links",
        "links_total", "date_of_birth", "nationality", "sex", "created_at", "identifiers",
        "documents", "merged_from", "merged_into", "detached_into", "detached_from",
    }  # fmt: skip
    link = data["links"][0]
    assert link["suggested_relation"] in (None, "self")
    assert set(link["suggestion_basis"]) <= {
        "passport_equal", "passport_one_char_apart", "nik_encodes_client_birth_date", "birth_date_equal"
    }  # fmt: skip
    assert set(link["review"]) == {"decision", "relation", "decided_by", "decided_at"}
    client_values = {CLIENT_PASSPORT, "ZZGETMATCH1", "ZZGETCLIE9", "1971-08-09", "19710809"}
    scrubbed = {
        k: v
        for k, v in data.items()
        if k not in {"identifiers", "date_of_birth", "names", "display_name"}
    }
    scrubbed["links"] = [
        {k: v for k, v in item.items() if k != "client_name"} for item in data["links"]
    ]
    surface = privacy.response_surface(json.dumps(scrubbed))
    assert privacy.leaks(privacy.probe_forms(client_values), surface) == []
    if case == "non_matching":
        assert (
            privacy.leaks(privacy.probe_forms(client_values), privacy.response_surface(resp.text))
            == []
        )
        assert link["suggested_relation"] is None and link["suggestion_basis"] == []
    records = [r for r in caplog.records if not privacy.is_http_client_logger(r.name)]
    rows, types = await privacy.audit_snapshot(conn)
    forbidden = privacy.probe_forms(client_values | {spec["client_name"]})
    assert privacy.leaks(forbidden, privacy.log_surface(records)) == []
    scrubbed_rows = [{**r, "user_email": None} for r in rows]
    assert privacy.leaks(forbidden, privacy.audit_texts(scrubbed_rows, types)) == []


AUDIT_TYPES = {
    "id": "integer",
    "error_message": "text",
    "created_at": "timestamp without time zone",
}


def _planted(value):
    record = logging.LogRecord("zz.teeth", logging.INFO, __file__, 1, "teeth %s", (value,), None)
    row = {"id": 1, "error_message": f"boom {value}", "created_at": datetime.now()}
    return (
        privacy.log_surface([record]),
        privacy.response_surface(json.dumps({"x": value})),
        privacy.audit_texts([row], AUDIT_TYPES),
    )


@pytest.mark.parametrize(
    "value", [CLIENT_NAME, CLIENT_PASSPORT, CLIENT_BIRTH, str(C1), EMAIL_SHAPED]
)
def test_t9_the_detector_catches_a_planted_probe_on_every_surface_also_upper_cased(value):
    forbidden = privacy.probe_forms([value])
    for planted in (value, value.upper()):
        for surface in _planted(planted):
            assert privacy.leaks(forbidden, surface)
    for surface in _planted("a closed word"):
        assert privacy.leaks(forbidden, surface) == []


def test_t9_the_detector_honours_the_allowed_exceptions_and_nothing_else():
    class Resp:
        def __init__(self, data, status=200):
            self.text, self.status_code, self._data = json.dumps(data), status, data

        def json(self):
            return self._data

    review = {
        "decision": "confirmed",
        "relation": "self",
        "decided_by": ACTOR,
        "decided_at": "2026-11-21T00:00:00+00:00",
    }
    ok = Resp({"status": "recorded", "review": review})
    tokens = {"confirmed", "self", ACTOR, "2026-11-21t00:00:00+00:00"}
    assert (
        surfaces_leaks(
            tokens,
            ok,
            [],
            [{"user_email": ACTOR, "id": 1}],
            {"user_email": "text", "id": "integer"},
            status=200,
        )
        == {}
    )
    # the same words anywhere else are caught: in the status field, in the log, in the audit row
    leaked = Resp({"status": "confirmed", "review": review})
    assert surfaces_leaks(tokens, leaked, [], [], {}, status=200)["answer"]
    record = logging.LogRecord("zz", logging.INFO, __file__, 1, "x %s", (ACTOR,), None)
    assert surfaces_leaks(tokens, ok, [record], [], {}, status=200)["log"]
    row = {"user_email": None, "error_message": "self", "id": 1}
    assert surfaces_leaks(
        tokens,
        ok,
        [],
        [row],
        {"user_email": "text", "error_message": "text", "id": "integer"},
        status=200,
    )["audit"]
