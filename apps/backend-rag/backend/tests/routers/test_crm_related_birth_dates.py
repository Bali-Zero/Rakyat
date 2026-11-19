"""Registry of related subjects, slice 4E: a person's birth date recorded from an identity document
through one sealed route, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362, 365, 367, 369, 370 and
371 forward parts, minimal ``clients`` and ``client_family_members`` tables, the audit table and a write
log) and seeds invented rows (``Zz`` / ``ZZ`` conventions, passports ``ZZ`` plus digits, content hashes
of invented byte strings, dates that belong to nobody). Nothing here is a real person or document.

Kind variant (SPEC 3.1 / C4): ``subject`` is ``_PersonKeys``, a subclass of ``_SubjectKeys`` whose
``kind`` is ``Literal["person"]``; a company is the pydantic literal error at ``["subject", "kind"]``.
``_SubjectKeys`` itself is not narrowed. The write log, the spy and the controller are this module's own
(the same shape as the detach module's): hooks are keyed by (statement, occurrence) and receive the RAW
connection and the arguments; ``spy.transactions`` keeps the keywords of each ``transaction(...)``.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import dataclasses
import hashlib
import inspect
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError

import backend.app.routers.crm_related_registry_read as read_module
import backend.app.routers.crm_related_subjects as route_module
import backend.services.crm.related_birth_dates as birth_module
import backend.services.crm.related_detach as detach_module
import backend.services.crm.related_merges as merges_module
from backend.app.auth.public_endpoints import PUBLIC_ENDPOINTS
from backend.app.core.config import settings
from backend.app.setup.router_manifest import ROUTER_MANIFEST
from backend.db.migration_base import split_migration_sql
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm.related_birth_dates import (
    BirthDateInconsistent,
    record_birth_date,
)
from backend.services.crm.related_detach import detach_identifier
from backend.services.crm.related_identifiers import canonical_form
from backend.services.crm.related_merges import record_merge
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures import registry_privacy as privacy
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

BACKEND = Path(route_module.__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "371_related_subject_birth_dates.sql"
READ = "/api/crm/related-registry"
URL = "/api/crm/related-subjects/birth-date/register"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
PLAN = "ef56" * 16
TABLE = "related_subject_birth_dates"
UNIQUE_NAME = "related_subject_birth_dates_subject_id_key"

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}
AUDIT_INSERT = ("api_audit_trail", "INSERT")
BIRTH_WRITES = {("related_subjects", "UPDATE"), ("related_subject_birth_dates", "INSERT")}
DETACH_WRITES = {
    ("related_subjects", "INSERT"),
    ("related_subject_identifiers", "UPDATE"),
    ("related_subject_documents", "UPDATE"),
    ("related_subject_detachments", "INSERT"),
}
MERGE_WRITES = {("related_subject_merges", "INSERT"), ("related_subjects", "UPDATE")}

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
B = "1977-03-12"  # the birth date of the requests: a synthetic date
B_DATE = date(1977, 3, 12)
B2 = "1981-11-05"
OTHER_DOB = date(1990, 1, 2)  # a date the nik of the detach worlds does not encode
TODAY = date(2026, 11, 19)

O_SQL = birth_module._OWNERS_SQL
KF_SQL = birth_module._OWNER_KEYS_SQL
RW_SQL = birth_module._ROW_SQL
L_SQL = birth_module._LOCK_SQL
BH_SQL = birth_module._HISTORY_SQL
DC_SQL = birth_module._OWNED_DOC_SQL
SD_SQL = birth_module._SET_DATE_SQL
IB_SQL = birth_module._INSERT_BIRTH_DATE_SQL
STATEMENT_NAMES = {
    O_SQL: "O",
    KF_SQL: "KF",
    RW_SQL: "RW",
    L_SQL: "L",
    BH_SQL: "BH",
    DC_SQL: "DC",
    SD_SQL: "SD",
    IB_SQL: "IB",
}

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
    schema = "related_birth_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=8,
        server_settings={"search_path": schema},
        init=init_prod_shaped_connection,
        statement_cache_size=0,
        max_inactive_connection_lifetime=30.0,
        command_timeout=60,
    )
    try:
        async with created.acquire() as c:
            # C5: the FORWARD parts of exactly these seven files, never the whole file.
            for migration in (
                "361_related_subjects_registry.sql",
                "362_related_documents.sql",
                "365_related_subject_names.sql",
                "367_related_subject_links.sql",
                "369_related_subject_merges.sql",
                "370_related_subject_detachments.sql",
                "371_related_subject_birth_dates.sql",
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


def doc(tag: int) -> str:
    """A content hash: the sha256 of an invented byte string, never of a document."""
    return hashlib.sha256(f"zz-birth-doc-{tag}".encode()).hexdigest()


async def seed_document(c, sid, tag, *, doc_type="passport", at=T0):
    sha = doc(tag)
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
            "first_channel, evidence, created_by) VALUES ($1, 1234, 'application/pdf', "
            "DATE '2026-01-05', 'drive', '{}'::jsonb, 'zz-seed') ON CONFLICT DO NOTHING",
            sha,
        )
        if sid is not None:
            await c.execute(
                "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
                "created_by, created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
                sid, sha, doc_type, at,
            )  # fmt: skip
    return sha


async def seed_history(c, sid, *, birth=B_DATE, document=None, kind="passport_data_page"):
    """A history row, the evidence bound as a NATIVE dict (the production codec encodes it)."""
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_birth_dates (subject_id, birth_date, document_sha256, "
            "document_kind, basis, evidence, created_by) "
            "VALUES ($1, $2, $3, $4, 'identity_document', $5, 'zz-seed')",
            sid, birth, document or doc(1), kind, {"plan_sha256": PLAN, "files": 1},
        )  # fmt: skip


async def seed_client(c, client_id, name):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO clients (id, full_name) OVERRIDING SYSTEM VALUE VALUES ($1, $2)",
            client_id, name,
        )  # fmt: skip


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


@pytest.fixture
def today(monkeypatch):
    """A fixed UTC date for the upper bound of the birth date."""
    monkeypatch.setattr(route_module, "_utc_today", lambda: TODAY)
    return TODAY


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


@pytest.fixture
def birth_writes(writes):
    """The two writes a recording makes, on top of the audit INSERT."""
    writes.update(BIRTH_WRITES)
    return writes


@pytest_asyncio.fixture
async def client(pool, enabled, auth_on, audit_logger, monkeypatch, birth_writes):
    as_identity(monkeypatch, None)
    async with make_client(pool) as c:
        yield c


async def post(c, data, *, headers=HEADERS, url=URL):
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return await c.post(url, content=raw, headers=headers)


# ── requests and answers ──────────────────────────────────────────────────────


def passport(n: int) -> str:
    return f"ZZ{n:07d}"


def permit(n: int) -> str:
    return f"Z99ZZ{n:011d}"


def nik(ddmmyy: str, serial: int = 4242) -> str:
    """A national number of region 990042 (none exists) with the DDMMYY digits given."""
    return f"990042{ddmmyy}{serial:04d}"


def keys(ids) -> list[dict]:
    return [{"type": t, "value": v} for t, v in ids]


def ids_of(n: int):
    return (("passport", passport(n)),)


def doc_of(n: int, tag: int = 1) -> str:
    return doc(n * 100 + tag)


def req(ids, document, *, birth=B, document_kind="passport_data_page", dry_run=False, **over):
    data = {
        "subject": {"kind": "person", "identifiers": keys(ids)},
        "birth_date": birth,
        "document": document,
        "document_kind": document_kind,
        "basis": "identity_document",
        "evidence": {"plan_sha256": PLAN, "files": 1},
        "dry_run": dry_run,
    }
    data.update(over)
    return data


def birth_input(data: dict):
    """The service input of a request dictionary, through the route's own strict model."""
    return route_module.RelatedBirthDateRequest.model_validate_json(json.dumps(data)).to_input()


def detach_input(data: dict):
    return route_module.RelatedDetachRequest.model_validate_json(json.dumps(data)).to_input()


def detach_req(owner, detach, documents=()):
    return {
        "owner": {"kind": "person", "identifiers": keys(owner)},
        "detach": {"type": detach[0], "value": detach[1]},
        "documents": list(documents),
        "basis": "nik_birth_date",
        "evidence": {"plan_sha256": PLAN, "files": 1},
        "dry_run": False,
    }


def merge_input(source, target):
    data = {
        "source": {"kind": "person", "identifiers": keys(source)},
        "target": {"kind": "person", "identifiers": keys(target)},
        "basis": "permit_stamp",
        "evidence": {"plan_sha256": PLAN, "files": 1},
        "dry_run": False,
    }
    return route_module.RelatedMergesRegisterRequest.model_validate_json(
        json.dumps(data)
    ).to_input()


def recorded(s, dry_run=False):
    return {"outcome": "recorded", "subject_id": s, "dry_run": dry_run}


def already(s, dry_run=False):
    return {"outcome": "already_recorded", "subject_id": s, "dry_run": dry_run}


def refused(reason, s):
    return {"outcome": "not_recordable", "reason": reason, "subject_id": s}


def not_registered(reason):
    return {"outcome": "subject_not_registered", "reason": reason}


async def seed_person(c, n, *, dob=None, tags=(1,), kind="person", merged=None, extra=()):
    """A record holding one passport (plus ``extra`` keys) and one linked document per tag."""
    sid = await seed_subject(c, kind, f"ZzPerson {n}", (*ids_of(n), *extra), dob=dob, merged=merged)
    for tag in tags:
        await seed_document(c, sid, n * 100 + tag)
    return sid


async def ledger(c):
    """Every write but the audit INSERT, in order: (table, op, old key, new key, changed)."""
    rows = await c.fetch(
        "SELECT tbl, op, old_key, new_key, changed FROM write_log "
        "WHERE tbl <> 'api_audit_trail' ORDER BY id"
    )
    return [(r["tbl"], r["op"], r["old_key"], r["new_key"], r["changed"]) for r in rows]


async def history(c):
    return await c.fetch(
        "SELECT id, subject_id, birth_date, document_sha256, document_kind, basis, evidence, "
        "jsonb_typeof(evidence) AS kind, created_by, created_at "
        "FROM related_subject_birth_dates ORDER BY id"
    )


async def dob_of(c, sid):
    return await c.fetchval("SELECT date_of_birth FROM related_subjects WHERE id = $1", sid)


async def keys_of(c, sid):
    rows = await c.fetch(
        "SELECT id_type, id_norm FROM related_subject_identifiers WHERE subject_id = $1 "
        "ORDER BY id_type, id_norm",
        sid,
    )
    return [(r["id_type"], r["id_norm"]) for r in rows]


NINE_TABLES = (
    "related_subjects",
    "related_subject_identifiers",
    "related_subject_documents",
    "related_subject_names",
    "related_subject_links",
    "related_subject_merges",
    "related_subject_detachments",
    "related_documents",
    "related_document_sightings",
)
REGISTRY_TABLES = (*NINE_TABLES, TABLE)
CRM_CONTROLS = ("clients", "client_family_members")
ALL_TABLES = (*REGISTRY_TABLES, *CRM_CONTROLS)


async def digest(c, table):
    """md5 of the table's rows as ordered to_jsonb texts."""
    return await c.fetchval(
        f"SELECT md5(coalesce(string_agg(to_jsonb(r)::text, ',' ORDER BY to_jsonb(r)::text), '')) "
        f"FROM {table} r"
    )


async def digests(c, tables=ALL_TABLES):
    return {t: await digest(c, t) for t in tables}


async def rows_of(c, table):
    """Every row of the table as its jsonb, by id."""
    out = {}
    for r in await c.fetch(f"SELECT to_jsonb(r) AS j FROM {table} r"):
        j = json.loads(r["j"]) if isinstance(r["j"], str) else r["j"]
        out[j["id"]] = j
    return out


# ── the spy ───────────────────────────────────────────────────────────────────


class _Spy:
    """The record of one connection: every call as (sql, args), in order; hooks BEFORE and AFTER a
    statement keyed by (sql, occurrence), called with the RAW connection and the arguments;
    ``replace`` substitutes the answer of the (sql, occurrence) that DID run; ``transactions`` keeps
    the keywords of each ``transaction(...)``."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.transactions: list[dict] = []
        self.before: dict = {}
        self.after: dict = {}
        self.replace: dict = {}
        self.seen: dict[str, int] = {}

    def sqls(self) -> list[str]:
        return [sql for sql, _ in self.calls]

    def names(self) -> list[str]:
        return [STATEMENT_NAMES.get(sql, sql) for sql, _ in self.calls]

    def count(self, sql: str) -> int:
        return sum(1 for s, _ in self.calls if s == sql)

    def writing(self) -> list[str]:
        return [
            sql
            for sql, _ in self.calls
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
        ]


class _SpyConn:
    def __init__(self, conn, spy):
        self._conn, self._spy = conn, spy

    def transaction(self, **kwargs):
        self._spy.transactions.append(dict(kwargs))
        return self._conn.transaction(**kwargs)

    def __getattr__(self, attr):
        target = getattr(self._conn, attr)
        if attr in {"fetch", "fetchrow", "fetchval", "execute"}:

            async def recorded_call(sql, *args, **kwargs):
                spy = self._spy
                spy.calls.append((sql, args))
                n = spy.seen[sql] = spy.seen.get(sql, 0) + 1
                hook = spy.before.get((sql, n))
                if hook is not None:
                    await hook(self._conn, args)
                out = await target(sql, *args, **kwargs)
                if (sql, n) in spy.replace:
                    out = spy.replace[(sql, n)]
                hook = spy.after.get((sql, n))
                if hook is not None:
                    await hook(self._conn, args)
                return out

            return recorded_call
        return target


class _SpyPool:
    """The pool the app holds for one test: counts the acquisitions, wraps each connection."""

    def __init__(self, pool, spy=None):
        self._pool, self.acquired = pool, 0
        self.spy = spy or _Spy()

    @asynccontextmanager
    async def _connection(self):
        async with self._pool.acquire() as c:
            yield _SpyConn(c, self.spy)

    def acquire(self):
        self.acquired += 1
        return self._connection()


async def run_spied(pool, monkeypatch, data, spy=None):
    as_identity(monkeypatch, None)
    spy_pool = _SpyPool(pool, spy)
    async with make_client(spy_pool) as c:
        resp = await post(c, data)
    return resp, spy_pool.spy


# ── the controller ────────────────────────────────────────────────────────────

CAP = 10  # seconds a step of the controller may take


class _Point:
    """A hold point of one transaction at (statement, occurrence), before or after it runs."""

    def __init__(self, spy, sql, n=1, *, after=False):
        self.reached, self.release = asyncio.Event(), asyncio.Event()
        (spy.after if after else spy.before)[(sql, n)] = self._hold

    async def _hold(self, raw, args):
        self.reached.set()
        await asyncio.wait_for(self.release.wait(), CAP)


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
    """The observer sees the backend `pid` waiting on a lock it was not granted."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CAP
    while loop.time() < deadline:
        try:
            seen = await asyncio.wait_for(
                observer.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity a WHERE a.pid = $1 "
                    "AND a.wait_event_type = 'Lock' AND EXISTS "
                    "(SELECT 1 FROM pg_locks l WHERE l.pid = a.pid AND NOT l.granted))",
                    pid,
                ),
                timeout=max(deadline - loop.time(), 0.001),
            )
        except asyncio.TimeoutError:
            break
        if seen:
            return
        if any(t.done() for t in tasks):
            break
        await asyncio.sleep(0.05)
    raise AssertionError("the second transaction was never seen waiting on a lock")


async def _settle(tasks, points):
    """Every hold released, every task awaited (or cancelled past the cap)."""
    for point in points:
        point.release.set()
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), CAP)
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise AssertionError("a T4 transaction did not finish within the cap") from None


def birth_task(c, spy, data):
    return asyncio.ensure_future(
        record_birth_date(_SpyConn(c, spy), birth_input(data), dry_run=data["dry_run"])
    )


def detach_task(c, spy, data):
    return asyncio.ensure_future(
        detach_identifier(_SpyConn(c, spy), detach_input(data), dry_run=False)
    )


# ── M: migration 371 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m1_columns_types_nullability_identity_and_unique(pool, conn):
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = $1",
            TABLE,
        )
    }
    assert cols == {
        "id": ("bigint", "NO"),
        "subject_id": ("bigint", "NO"),
        "birth_date": ("date", "NO"),
        "document_sha256": ("text", "NO"),
        "document_kind": ("text", "NO"),
        "basis": ("text", "NO"),
        "evidence": ("jsonb", "NO"),
        "created_by": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    }
    assert (
        await conn.fetchval(
            "SELECT is_identity || '/' || identity_generation FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = 'id'",
            TABLE,
        )
        == "YES/ALWAYS"
    )
    unique = await conn.fetch(
        "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS def FROM pg_constraint c "
        "WHERE c.conrelid = $1::regclass AND c.contype IN ('u', 'f') ORDER BY c.conname",
        TABLE,
    )
    assert [(r["conname"], r["contype"]) for r in unique] == [
        (UNIQUE_NAME, "u"),
        ("related_subject_birth_dates_subject_id_fkey", "f"),
    ]
    assert "UNIQUE (subject_id)" in unique[0]["def"]
    assert "ON DELETE CASCADE" in unique[1]["def"] and "related_subjects" in unique[1]["def"]


INSERT_HISTORY = (
    "INSERT INTO related_subject_birth_dates (subject_id, birth_date, document_sha256, "
    "document_kind, basis, evidence, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7)"
)


def good_row(s, **over):
    row = {
        "s": s,
        "birth": B_DATE,
        "sha": doc(9),
        "kind": "passport_data_page",
        "basis": "identity_document",
        "evidence": {"plan_sha256": PLAN, "files": 1},
        "by": "zz",
    }
    row.update(over)
    return [row[k] for k in ("s", "birth", "sha", "kind", "basis", "evidence", "by")]


@pytest.mark.asyncio
async def test_m2_the_checks_refuse_each_bad_value_and_the_unique_refuses_a_second_row(pool, conn):
    s = await seed_subject(conn, "person", "ZzM2", ids_of(4300001))
    bad = {
        "before 1900": {"birth": date(1899, 12, 31)},
        "short hash": {"sha": "a" * 63},
        "upper hash": {"sha": "A" * 64},
        "kind": {"kind": "letter"},
        "basis": {"basis": "manual"},
        "array evidence": {"evidence": []},
    }
    async with unmeasured(conn):
        for over in bad.values():
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(INSERT_HISTORY, *good_row(s, **over))
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(INSERT_HISTORY, *good_row(s, by=None))
        await conn.execute(INSERT_HISTORY, *good_row(s, birth=date(1900, 1, 1)))
        for kind in ("passport_data_page", "itas", "evisa"):
            other = await seed_subject(conn, "person", "ZzM2b", ids_of(4300010 + len(kind)))
            await conn.execute(INSERT_HISTORY, *good_row(other, kind=kind))
        with pytest.raises(asyncpg.UniqueViolationError) as exc:
            await conn.execute(INSERT_HISTORY, *good_row(s))
    assert exc.value.constraint_name == UNIQUE_NAME
    assert len(await history(conn)) == 4


@pytest.mark.asyncio
async def test_m3_deleting_the_subject_removes_its_history_row(pool, conn):
    s = await seed_subject(conn, "person", "ZzM3", ids_of(4300021))
    await seed_history(conn, s)
    async with unmeasured(conn):
        await conn.execute("DELETE FROM related_subject_identifiers WHERE subject_id = $1", s)
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", s)
    assert await history(conn) == []


@pytest.mark.asyncio
async def test_m4_the_rollback_is_the_owners_drop_line_and_drops_only_the_table(pool, conn):
    # RED in the builder's runs (spec 1a): the builder leaves the rollback empty and the owner
    # appends the DROP line by hand.
    forward, rollback = split_migration_sql(MIGRATION.read_text())
    assert not re.search(r"\bDROP\b", forward, re.I)
    assert rollback.strip() == "DROP TABLE IF EXISTS related_subject_birth_dates;"
    assert re.findall(r"DROP\s+(\w+)\s+IF\s+EXISTS\s+(\w+)", rollback) == [("TABLE", TABLE)]
    s = await seed_subject(conn, "person", "ZzM4", ids_of(4300031))
    async with unmeasured(conn):
        await conn.execute(rollback)
        present = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name LIKE 'related\\_%'"
            )
        }
    assert TABLE not in present
    assert set(NINE_TABLES) <= present
    assert await keys_of(conn, s) == [("passport", passport(4300031))]


def test_m5_the_file_is_the_recipe_of_370_and_writes_no_drop_before_the_marker():
    text = MIGRATION.read_text()
    forward, _ = split_migration_sql(text)
    assert "SET lock_timeout = '5s';" in forward and "SET statement_timeout = '60s';" in forward
    assert "CREATE TABLE IF NOT EXISTS related_subject_birth_dates" in forward
    assert text.count("-- === ROLLBACK ===") == 1


# ── B: the request model and the 422 answers (no database) ────────────────────

BASE = req(ids_of(4301001), doc_of(4301001))
H = doc_of(4301001)


def _mut(**over):
    out = json.loads(json.dumps(BASE))
    out.update(over)
    return out


def _without(*names):
    out = json.loads(json.dumps(BASE))
    for name in names:
        out.pop(name)
    return out


def _errors(raw) -> list[dict]:
    try:
        route_module.RelatedBirthDateRequest.model_validate_json(raw)
    except ValidationError as exc:
        return exc.errors(include_input=False, include_context=False, include_url=False)
    return []


def _answer_items(raw) -> list[dict]:
    resp = route_module._invalid_birth_date(_errors(raw))
    assert resp.status_code == 422
    return json.loads(resp.body)["detail"]


def item(loc, code):
    return {"loc": loc, "code": code}


B_CASES = {
    # B1: the kind, a pydantic literal error at subject.kind
    "b1_company": (
        _mut(subject={"kind": "company", "identifiers": keys([("npwp", "012345678901234")])}),
        [item(["subject", "kind"], "literal_error")],
    ),
    "b1_missing_kind": (
        _mut(subject={"identifiers": keys(ids_of(4301001))}),
        [item(["subject", "kind"], "missing")],
    ),
    "b1_garbage_kind": (
        _mut(subject={"kind": "ZzHostile<script>", "identifiers": keys(ids_of(4301001))}),
        [item(["subject", "kind"], "literal_error")],
    ),
    # B2: not a calendar date
    "b2_feb_30": (_mut(birth_date="1977-02-30"), [item(["birth_date"], "birth_date_invalid")]),
    "b2_month_13": (_mut(birth_date="1977-13-01"), [item(["birth_date"], "birth_date_invalid")]),
    "b2_year_0": (_mut(birth_date="0000-01-01"), [item(["birth_date"], "birth_date_invalid")]),
    "b2_feb_29_non_leap": (
        _mut(birth_date="1977-02-29"),
        [item(["birth_date"], "birth_date_invalid")],
    ),
    "b2_before_1900_and_invalid": (
        _mut(birth_date="1899-02-30"),
        [item(["birth_date"], "birth_date_invalid")],
    ),
    # B3: out of range
    "b3_before_1900": (
        _mut(birth_date="1899-12-31"),
        [item(["birth_date"], "birth_date_out_of_range")],
    ),
    "b3_tomorrow": (
        _mut(birth_date="2026-11-20"),
        [item(["birth_date"], "birth_date_out_of_range")],
    ),
    "b3_far_future": (
        _mut(birth_date="9999-12-31"),
        [item(["birth_date"], "birth_date_out_of_range")],
    ),
    # the format and the types
    "format_short_month": (
        _mut(birth_date="1977-3-12"),
        [item(["birth_date"], "string_pattern_mismatch")],
    ),
    "format_compact": (
        _mut(birth_date="19770312"),
        [item(["birth_date"], "string_pattern_mismatch")],
    ),
    "format_padded": (
        _mut(birth_date=" 1977-03-12"),
        [item(["birth_date"], "string_pattern_mismatch")],
    ),
    "format_trailing_newline": (
        _mut(birth_date="1977-03-12\n"),
        [item(["birth_date"], "string_pattern_mismatch")],
    ),
    "format_timestamp": (
        _mut(birth_date="1977-03-12T00:00:00"),
        [item(["birth_date"], "string_pattern_mismatch")],
    ),
    "format_number": (_mut(birth_date=19770312), [item(["birth_date"], "string_type")]),
    "format_null": (_mut(birth_date=None), [item(["birth_date"], "string_type")]),
    "missing_birth_date": (_without("birth_date"), [item(["birth_date"], "missing")]),
    # B4: unknown fields (the names are not in the closed list: reported as "_")
    "b4_date_of_birth": (_mut(date_of_birth=B), [item(["_"], "extra_forbidden")]),
    "b4_subject_id": (_mut(subject_id=7), [item(["_"], "extra_forbidden")]),
    "b4_id": (_mut(id=7), [item(["_"], "extra_forbidden")]),
    "b4_unknown_in_subject": (
        _mut(subject={"kind": "person", "identifiers": keys(ids_of(4301001)), "id": 7}),
        [item(["subject", "_"], "extra_forbidden")],
    ),
    # B5, B6
    "b5_basis": (_mut(basis="nik_birth_date"), [item(["basis"], "literal_error")]),
    "b5_basis_letter": (_mut(basis="letter"), [item(["basis"], "literal_error")]),
    "b5_basis_missing": (_without("basis"), [item(["basis"], "missing")]),
    "b6_document_kind": (_mut(document_kind="letter"), [item(["document_kind"], "literal_error")]),
    "b6_document_kind_upper": (
        _mut(document_kind="ITAS"),
        [item(["document_kind"], "literal_error")],
    ),
    "b6_document_kind_missing": (_without("document_kind"), [item(["document_kind"], "missing")]),
    # the document hash
    "document_upper": (_mut(document=H.upper()), [item(["document"], "string_pattern_mismatch")]),
    "document_short": (_mut(document=H[:63]), [item(["document"], "string_pattern_mismatch")]),
    "document_missing": (_without("document"), [item(["document"], "missing")]),
    "document_number": (_mut(document=7), [item(["document"], "string_type")]),
    # the evidence (_MergeEvidence's fields and bounds)
    "evidence_missing": (_without("evidence"), [item(["evidence"], "missing")]),
    "evidence_files_zero": (
        _mut(evidence={"plan_sha256": PLAN, "files": 0}),
        [item(["evidence", "files"], "greater_than_equal")],
    ),
    "evidence_files_over": (
        _mut(evidence={"plan_sha256": PLAN, "files": 10001}),
        [item(["evidence", "files"], "less_than_equal")],
    ),
    "evidence_files_bool": (
        _mut(evidence={"plan_sha256": PLAN, "files": True}),
        [item(["evidence", "files"], "int_type")],
    ),
    "evidence_plan_bad": (
        _mut(evidence={"plan_sha256": "xyz", "files": 1}),
        [item(["evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "evidence_extra": (
        _mut(evidence={"plan_sha256": PLAN, "files": 1, "name": "ZzHostile"}),
        [item(["evidence", "_"], "extra_forbidden")],
    ),
    "evidence_not_object": (_mut(evidence=[]), [item(["evidence"], "model_type")]),
    "dry_run_string": (_mut(dry_run="false"), [item(["dry_run"], "bool_type")]),
    "dry_run_number": (_mut(dry_run=0), [item(["dry_run"], "bool_type")]),
    # the key set (_SubjectKeys, complete set)
    "identifiers_empty": (
        _mut(subject={"kind": "person", "identifiers": []}),
        [item(["subject", "identifiers"], "too_short")],
    ),
    "identifier_invalid": (
        _mut(subject={"kind": "person", "identifiers": keys([("passport", "!!")])}),
        [item(["subject", "identifiers", 0], "invalid_identifier")],
    ),
    "identifier_type_unknown": (
        _mut(subject={"kind": "person", "identifiers": keys([("ssn", "ZZ1234567")])}),
        [item(["subject", "identifiers", 0, "type"], "literal_error")],
    ),
    "person_with_a_company_key": (
        _mut(subject={"kind": "person", "identifiers": keys([("npwp", "012345678901234")])}),
        [item(["subject"], "kind_type_mismatch")],
    ),
    "duplicate_key": (
        _mut(subject={"kind": "person", "identifiers": keys([ids_of(4301001)[0]] * 2)}),
        [item(["subject"], "duplicate_identifier")],
    ),
    "twenty_one_keys": (
        _mut(
            subject={
                "kind": "person",
                "identifiers": keys([("passport", passport(4301100 + i)) for i in range(21)]),
            }
        ),
        [item(["subject", "identifiers"], "too_long")],
    ),
    "hostile_key_name": (
        _mut(subject={"kind": "person", "identifiers": [{"type": "passport", "ZzHostile": 1}]}),
        [
            item(["subject", "identifiers", 0, "_"], "extra_forbidden"),
            item(["subject", "identifiers", 0, "value"], "missing"),
        ],
    ),
    "body_not_an_object": ([], [item([], "model_type")]),
    # the order of several errors: the fields' declaration order (kind, date, document, ...)
    "precedence_kind_then_date_then_document": (
        _mut(
            subject={"kind": "company", "identifiers": keys([("npwp", "012345678901234")])},
            birth_date="1977-02-30",
            document="zz",
        ),
        [
            item(["subject", "kind"], "literal_error"),
            item(["birth_date"], "birth_date_invalid"),
            item(["document"], "string_pattern_mismatch"),
        ],
    ),
    "precedence_date_then_basis": (
        _mut(birth_date="1899-12-31", basis="x"),
        [item(["birth_date"], "birth_date_out_of_range"), item(["basis"], "literal_error")],
    ),
}


@pytest.mark.parametrize("case", sorted(B_CASES))
def test_b_exact_422_items(case, today):
    data, expected = B_CASES[case]
    assert _answer_items(json.dumps(data)) == expected


def test_b_the_bounds_and_the_accepted_forms(today):
    for ok in ("1900-01-01", "1977-02-28", "1976-02-29", "2000-02-29", "2026-11-19", "1999-12-31"):
        assert _errors(json.dumps(_mut(birth_date=ok))) == [], ok
    assert _errors(json.dumps(_mut(birth_date="2026-11-20"))) != []
    assert _errors(json.dumps(_mut(birth_date="1899-12-31"))) != []


def test_b_the_current_utc_date_is_the_servers_not_the_callers(monkeypatch):
    monkeypatch.setattr(route_module, "_utc_today", lambda: date(2030, 1, 1))
    assert _errors(json.dumps(_mut(birth_date="2030-01-01"))) == []
    assert _errors(json.dumps(_mut(birth_date="2030-01-02"))) != []
    monkeypatch.undo()
    real = datetime.now(timezone.utc).date()
    assert abs((route_module._utc_today() - real).days) <= 1


def test_b_a_valid_request_builds_the_service_input_with_canonical_keys(today):
    data = _mut(
        subject={
            "kind": "person",
            "identifiers": keys([("passport", "zz4301001"), ("permit", permit(4301001))]),
        }
    )
    built = route_module.RelatedBirthDateRequest.model_validate_json(json.dumps(data)).to_input()
    assert built.kind == "person" and built.birth_date == B_DATE and built.document == H
    assert built.basis == "identity_document" and built.document_kind == "passport_data_page"
    assert built.evidence == {"plan_sha256": PLAN, "files": 1}
    assert [i.key for i in built.identifiers] == sorted(i.key for i in built.identifiers)
    assert ("passport", canonical_form("passport", "zz4301001")) in built.keys


def test_b_the_kind_variant_narrows_the_birth_date_model_only():
    company = {"kind": "company", "identifiers": keys([("npwp", "012345678901234")])}
    assert route_module._SubjectKeys.model_validate(company).kind == "company"
    assert route_module._PersonKeys.model_fields["kind"].annotation.__args__ == ("person",)
    assert issubclass(route_module._PersonKeys, route_module._SubjectKeys)


def test_b_the_closed_lists_are_the_specified_ones():
    assert route_module._BIRTH_DATE_LOC == frozenset(
        {
            "body",
            "subject",
            "kind",
            "identifiers",
            "type",
            "value",
            "birth_date",
            "document",
            "document_kind",
            "basis",
            "evidence",
            "plan_sha256",
            "files",
            "dry_run",
        }  # fmt: skip
    )
    assert route_module._BIRTH_DATE_CODE == frozenset(
        {
            "missing",
            "extra_forbidden",
            "string_type",
            "string_too_short",
            "string_too_long",
            "string_pattern_mismatch",
            "bool_type",
            "int_type",
            "greater_than_equal",
            "less_than_equal",
            "literal_error",
            "list_type",
            "too_short",
            "too_long",
            "model_type",
            "dict_type",
            "json_invalid",
            "invalid_identifier",
            "kind_type_mismatch",
            "duplicate_identifier",
            "birth_date_invalid",
            "birth_date_out_of_range",
        }  # fmt: skip
    )
    assert route_module._BIRTH_DATE_INDEX_LIMIT == {"identifiers": route_module.MAX_KEYS}
    assert "date_of_birth" not in route_module._BIRTH_DATE_LOC
    assert "kind_not_person" not in route_module._BIRTH_DATE_CODE


def test_b_the_sanitiser_keeps_closed_names_indexes_below_their_limits_and_codes():
    out = json.loads(
        route_module._invalid_birth_date(
            [
                {"loc": ("subject", "identifiers", 19, "type"), "type": "literal_error"},
                {"loc": ("subject", "identifiers", 20, "ZzHostile"), "type": "ZzMessage"},
                {"loc": ("subject", "identifiers", True), "type": "missing"},
                {"loc": ("ZzField",), "type": "value_error"},
                {"loc": ("birth_date",), "type": "birth_date_out_of_range"},
            ]
        ).body
    )["detail"]
    assert out == [
        item(["subject", "identifiers", 19, "type"], "literal_error"),
        item(["subject", "identifiers", "_", "_"], "invalid"),
        item(["subject", "identifiers", "_"], "missing"),
        item(["_"], "invalid"),
        item(["birth_date"], "birth_date_out_of_range"),
    ]
    many = [{"loc": ("body",), "type": "missing"}] * 80
    assert len(json.loads(route_module._invalid_birth_date(many).body)["detail"]) == 50


def test_b_malformed_raw_bodies_have_their_exact_sanitised_item(today):
    assert _answer_items(b"{not json") == [item([], "json_invalid")]
    assert _answer_items(b"") == [item([], "json_invalid")]
    assert _answer_items(b"null") == [item([], "model_type")]
    assert _answer_items(b'"ZzHostile"') == [item([], "model_type")]
    assert _answer_items(b"{}") == [
        item(["subject"], "missing"),
        item(["birth_date"], "missing"),
        item(["document"], "missing"),
        item(["document_kind"], "missing"),
        item(["basis"], "missing"),
        item(["evidence"], "missing"),
    ]


def test_b_the_body_is_read_raw_not_declared_as_a_fastapi_body_parameter():
    handler = route_module.register_related_subject_birth_date
    assert list(inspect.signature(handler).parameters) == ["request", "_actor", "db_pool"]
    assert "request.body()" in inspect.getsource(handler)
    assert "request.state" not in inspect.getsource(handler)


@pytest.mark.asyncio
async def test_b7_a_body_above_65536_bytes_is_422_too_long_and_runs_no_statement(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch
):
    resp, spy = await run_spied(pool, monkeypatch, b" " * (route_module.MAX_BODY_BYTES + 1))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [{"loc": ["body"], "code": "too_long"}]}
    assert spy.calls == []
    resp, spy = await run_spied(pool, monkeypatch, b" " * route_module.MAX_BODY_BYTES)
    assert resp.status_code == 422 and resp.json()["detail"] == [item([], "json_invalid")]


# ── T1 rows 1-5: the gates ────────────────────────────────────────────────────

BAD_BODIES = [b"{not json", json.dumps(_mut(basis="manual")).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_t1_row_1_the_flag_off_is_503_before_the_body(
    pool, auth_on, audit_logger, birth_writes, monkeypatch, raw
):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    resp, spy = await run_spied(pool, monkeypatch, raw)
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
@pytest.mark.parametrize("key", [None, "wrong-key"])
async def test_t1_row_2_the_key_is_checked_before_the_body(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, raw, key
):
    as_identity(monkeypatch, None)
    headers = {k: v for k, v in HEADERS.items() if k != "X-CRM-Write-Key"}
    if key:
        headers["X-CRM-Write-Key"] = key
    async with make_client(pool) as c:
        resp = await post(c, raw, headers=headers)
    assert resp.status_code == 401 and resp.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_t1_row_1_no_pool_is_the_existing_503(enabled, auth_on, monkeypatch):
    as_identity(monkeypatch, None)
    app = FastAPI()
    app.include_router(route_module.router)
    app.add_middleware(HybridAuthMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True), base_url="http://test"
    ) as c:
        resp = await post(c, BASE)
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}


@pytest.mark.asyncio
async def test_t1_row_5_an_invalid_body_through_the_route_is_422_and_runs_no_statement(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, today
):
    resp, spy = await run_spied(pool, monkeypatch, _mut(birth_date="1977-02-30"))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [item(["birth_date"], "birth_date_invalid")]}
    assert spy.calls == []


@pytest.mark.asyncio
async def test_t1_get_and_the_read_routes_stay_protected(client, conn):
    sid = await seed_subject(conn, "person", "ZzGate Person", ids_of(4302001))
    for url in (URL, READ, f"{READ}/{sid}"):
        resp = await client.get(url)
        assert resp.status_code == 401, url
        assert "ZzGate" not in resp.text


# ── T2: every refusal, in both modes; nothing written ─────────────────────────


async def refusal_world(conn):
    """Eleven records, one per resolution or refusal answer."""
    w = {}
    w["plain"] = await seed_person(conn, 4303001)
    w["recorded_same"] = await seed_person(conn, 4303002, dob=B_DATE)
    w["recorded_other"] = await seed_person(conn, 4303003, dob=date(1960, 5, 5))
    w["merged_target"] = await seed_person(conn, 4303004)
    w["merged"] = await seed_person(conn, 4303005, merged=w["merged_target"])
    w["company"] = await seed_person(conn, 4303006, kind="company")
    w["no_doc"] = await seed_person(conn, 4303007, tags=())
    w["history"] = await seed_person(conn, 4303008)
    await seed_history(conn, w["history"], document=doc_of(4303008))
    w["two_a"] = await seed_person(conn, 4303009)
    w["two_b"] = await seed_person(conn, 4303010)
    w["wide"] = await seed_person(conn, 4303011, extra=(("permit", permit(4303011)),))
    w["other_doc"] = await seed_person(conn, 4303012, tags=(1,))
    return w


def refusal_cases(w):
    p = ids_of
    return {
        "r1_none_owned": (req(p(4303999), doc_of(4303001)), not_registered("none_owned")),
        "r2_two_owners": (
            req((*p(4303009), *p(4303010)), doc_of(4303009)),
            not_registered("two_owners"),
        ),
        "r3_subset": (req(p(4303011), doc_of(4303011)), not_registered("set_differs")),
        "r3_superset": (
            req((*p(4303001), ("permit", permit(4303001))), doc_of(4303001)),
            not_registered("set_differs"),
        ),
        "r4_partial_overlap": (
            req((*p(4303011), ("permit", permit(4303999))), doc_of(4303011)),
            not_registered("partial_overlap"),
        ),
        "r5_company": (req(p(4303006), doc_of(4303006)), not_registered("kind_differs")),
        "n1_merged": (req(p(4303005), doc_of(4303005)), refused("subject_is_merged", w["merged"])),
        "n2_differs": (
            req(p(4303003), doc_of(4303003)),
            refused("birth_date_differs", w["recorded_other"]),
        ),
        "n3_history": (req(p(4303008), doc_of(4303008)), refused("history_exists", w["history"])),
        "n4_not_on_subject": (
            req(p(4303001), doc_of(4303012)),
            refused("document_not_on_subject", w["plain"]),
        ),
        "n4_no_document_at_all": (
            req(p(4303007), doc_of(4303001)),
            refused("document_not_on_subject", w["no_doc"]),
        ),
        "w_already": (req(p(4303002), doc_of(4303002)), already(w["recorded_same"])),
        "w_already_without_a_document": (
            req(p(4303002), doc_of(4303999)),
            already(w["recorded_same"]),
        ),
    }


REFUSAL_NAMES = sorted(refusal_cases(collections.defaultdict(int)))


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
@pytest.mark.parametrize("name", REFUSAL_NAMES)
async def test_t2_each_answer_has_its_exact_body_and_writes_nothing(client, conn, dry_run, name):
    w = await refusal_world(conn)
    data, expected = refusal_cases(w)[name]
    before = await digests(conn)
    resp = await post(client, {**data, "dry_run": dry_run})
    assert resp.status_code == 200
    body = resp.json()
    if body.get("outcome") == "already_recorded":
        expected = {**expected, "dry_run": dry_run}
    assert body == expected
    assert await digests(conn) == before
    assert await ledger(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_t2_order_r3_before_r5_n1_before_w_and_w_before_n2_n3_n4(client, conn, dry_run):
    # R3 before R5: a company record asked with a SUBSET of its keys
    c = await seed_person(conn, 4304001, kind="company", extra=(("permit", permit(4304001)),))
    data = req(ids_of(4304001), doc_of(4304001), dry_run=dry_run)
    assert (await post(client, data)).json() == not_registered("set_differs")
    # N1 before W: a merged record already holding the requested date
    target = await seed_person(conn, 4304002)
    m = await seed_person(conn, 4304003, dob=B_DATE, merged=target)
    assert (await post(client, req(ids_of(4304003), doc_of(4304003), dry_run=dry_run))).json() == (
        refused("subject_is_merged", m)
    )
    # W before N3 and N4: the date is the request's, a history row exists, no document is linked
    w = await seed_person(conn, 4304004, dob=B_DATE, tags=())
    await seed_history(conn, w)
    assert (await post(client, req(ids_of(4304004), doc_of(4304999), dry_run=dry_run))).json() == (
        already(w, dry_run)
    )
    # N2 before N3: a different date and a history row
    n2 = await seed_person(conn, 4304005, dob=date(1960, 5, 5))
    await seed_history(conn, n2, document=doc_of(4304005))
    assert (await post(client, req(ids_of(4304005), doc_of(4304005), dry_run=dry_run))).json() == (
        refused("birth_date_differs", n2)
    )
    # N3 before N4: a history row and no linked document
    n3 = await seed_person(conn, 4304006, tags=())
    await seed_history(conn, n3, document=doc_of(4304006))
    assert (await post(client, req(ids_of(4304006), doc_of(4304006), dry_run=dry_run))).json() == (
        refused("history_exists", n3)
    )
    assert c and await ledger(conn) == []


# ── C1: resolution without redirection ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_c1_a_survivor_with_its_own_document_is_accepted_a_members_is_not(
    client, conn, dry_run
):
    survivor = await seed_person(conn, 4305001)
    member_keys = (("permit", permit(4305001)),)
    member = await seed_subject(conn, "person", "ZzMember", member_keys, merged=survivor)
    await seed_document(conn, member, 4305001 * 100 + 5)
    before = await digests(conn)
    # the survivor's own document: its own row gets the date (apply) or would (dry run)
    ok = await post(client, req(ids_of(4305001), doc_of(4305001), dry_run=dry_run))
    assert ok.json() == recorded(survivor, dry_run)
    # a document only on the merged-in member does not count (N4)
    only_member = await post(
        client, req(ids_of(4305001), doc_of(4305001, 5), dry_run=dry_run, birth=B2)
    )
    if dry_run:
        assert only_member.json() == refused("document_not_on_subject", survivor)
        assert await digests(conn) == before
    else:
        assert only_member.json() == refused("birth_date_differs", survivor)
        assert await dob_of(conn, survivor) == B_DATE


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_c1_the_members_keys_are_merged_and_the_union_of_both_is_two_owners(
    client, conn, dry_run
):
    survivor = await seed_person(conn, 4305011)
    member_keys = (("permit", permit(4305011)),)
    member = await seed_subject(conn, "person", "ZzMember", member_keys, merged=survivor)
    await seed_document(conn, member, 4305011 * 100 + 5)
    before = await digests(conn)
    docs = doc_of(4305011, 5)
    r = await post(client, req(member_keys, docs, dry_run=dry_run))
    assert r.json() == refused("subject_is_merged", member)
    union = (*ids_of(4305011), *member_keys)
    r = await post(client, req(union, doc_of(4305011), dry_run=dry_run))
    assert r.json() == not_registered("two_owners")
    # the document only on the member, asked on the survivor's keys: N4, never a redirection
    r = await post(client, req(ids_of(4305011), docs, dry_run=dry_run))
    assert r.json() == refused("document_not_on_subject", survivor)
    assert await digests(conn) == before and await dob_of(conn, survivor) is None


# ── recorded: the apply, the dry run, the second apply ────────────────────────


@pytest.mark.asyncio
async def test_t1_rows_9_10_a_dry_run_writes_nothing_and_the_apply_writes_two_statements(
    client, conn
):
    s = await seed_person(conn, 4306001)
    other = await seed_person(conn, 4306002, dob=date(1960, 5, 5))
    before = await digests(conn)
    rows_before = {t: await rows_of(conn, t) for t in ("related_subjects",)}
    dry = await post(client, req(ids_of(4306001), doc_of(4306001), dry_run=True))
    assert dry.status_code == 200 and dry.json() == recorded(s, True)
    assert await digests(conn) == before and await ledger(conn) == []
    applied = await post(client, req(ids_of(4306001), doc_of(4306001), document_kind="itas"))
    assert applied.status_code == 200 and applied.json() == recorded(s)
    led = await ledger(conn)
    assert [(t, op, ch) for t, op, _, _, ch in led] == [
        ("related_subjects", "UPDATE", ["date_of_birth"]),
        ("related_subject_birth_dates", "INSERT", None),
    ]
    # exactly one column of one subject row changed: every other row and column is as it was
    after = await rows_of(conn, "related_subjects")
    changed = {k for k in after if after[k] != rows_before["related_subjects"][k]}
    assert changed == {s}
    assert {**after[s], "date_of_birth": None} == rows_before["related_subjects"][s]
    assert after[s]["date_of_birth"] == B
    assert after[other] == rows_before["related_subjects"][other]
    after_digests = await digests(conn)
    for table in ALL_TABLES:
        if table not in ("related_subjects", TABLE):
            assert after_digests[table] == before[table], table
    # one history row equal to the request
    (h,) = await history(conn)
    assert (h["subject_id"], h["birth_date"], h["document_sha256"], h["document_kind"]) == (
        s, B_DATE, doc_of(4306001), "itas",
    )  # fmt: skip
    assert (
        h["basis"] == "identity_document" and h["created_by"] == "system:related-subject-registry"
    )
    assert h["kind"] == "object" and h["evidence"] == {"plan_sha256": PLAN, "files": 1}
    assert h["created_at"] is not None


@pytest.mark.asyncio
async def test_t1_rows_8_a_second_apply_and_a_dry_run_after_it_are_already_recorded(client, conn):
    s = await seed_person(conn, 4306011)
    data = req(ids_of(4306011), doc_of(4306011))
    assert (await post(client, data)).json() == recorded(s)
    after_first = await digests(conn)
    mark = len(await ledger(conn))
    assert (await post(client, data)).json() == already(s)
    assert (await post(client, {**data, "dry_run": True})).json() == already(s, True)
    assert await digests(conn) == after_first and len(await ledger(conn)) == mark
    assert len(await history(conn)) == 1


@pytest.mark.asyncio
async def test_t1_a_date_written_by_any_other_means_is_already_recorded_and_writes_nothing(
    client, conn
):
    s = await seed_person(conn, 4306021, dob=B_DATE)
    before = await digests(conn)
    assert (await post(client, req(ids_of(4306021), doc_of(4306021)))).json() == already(s)
    assert await digests(conn) == before and await history(conn) == []


@pytest.mark.asyncio
async def test_sd_guard_a_row_whose_date_is_set_returns_no_row_and_changes_nothing(conn):
    s = await seed_person(conn, 4306031, dob=date(1960, 5, 5))
    before = await digests(conn)
    async with unmeasured(conn):
        assert await conn.fetchval(SD_SQL, s, B_DATE) is None
    assert await digests(conn) == before and await dob_of(conn, s) == date(1960, 5, 5)
    t = await seed_person(conn, 4306032)
    async with unmeasured(conn):
        assert await conn.fetchval(SD_SQL, t, B_DATE) == t
    assert await dob_of(conn, t) == B_DATE


# ── T3: statements, budgets, lock ─────────────────────────────────────────────

T3_APPLY = {
    "r1_none_owned": ["O"],
    "r2_two_owners": ["O"],
    "r3_subset": ["O", "L", "O", "KF"],
    "r4_partial_overlap": ["O", "L", "O", "KF"],
    "r5_company": ["O", "L", "O", "KF"],
    "n1_merged": ["O", "L", "O", "KF"],
    "n2_differs": ["O", "L", "O", "KF"],
    "w_already": ["O", "L", "O", "KF"],
    "n3_history": ["O", "L", "O", "KF", "BH"],
    "n4_not_on_subject": ["O", "L", "O", "KF", "BH", "DC"],
}
T3_DRY = {
    "r1_none_owned": ["O"],
    "r2_two_owners": ["O"],
    "r3_subset": ["O", "KF"],
    "r4_partial_overlap": ["O", "KF"],
    "r5_company": ["O", "KF", "RW"],
    "n1_merged": ["O", "KF", "RW"],
    "n2_differs": ["O", "KF", "RW"],
    "w_already": ["O", "KF", "RW"],
    "n3_history": ["O", "KF", "RW", "BH"],
    "n4_not_on_subject": ["O", "KF", "RW", "BH", "DC"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
@pytest.mark.parametrize("name", sorted(T3_APPLY))
async def test_t3_each_row_runs_its_statements_in_order(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, conn, dry_run, name
):
    w = await refusal_world(conn)
    data, _ = refusal_cases(w)[name]
    resp, spy = await run_spied(pool, monkeypatch, {**data, "dry_run": dry_run})
    assert resp.status_code == 200
    assert spy.names() == (T3_DRY if dry_run else T3_APPLY)[name]
    assert spy.writing() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_t3_recorded_statements_transactions_budgets_and_the_lock_text(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, conn, dry_run
):
    s = await seed_person(conn, 4307001)
    resp, spy = await run_spied(
        pool, monkeypatch, req(ids_of(4307001), doc_of(4307001), dry_run=dry_run)
    )
    assert resp.json() == recorded(s, dry_run)
    names = spy.names()
    if dry_run:
        assert names == ["O", "KF", "RW", "BH", "DC"] and len(names) <= 5
        assert spy.transactions == [{"isolation": "repeatable_read", "readonly": True}]
        assert not any("FOR UPDATE" in sql for sql in spy.sqls()) and spy.writing() == []
    else:
        assert names == ["O", "L", "O", "KF", "BH", "DC", "SD", "IB"] and len(names) <= 8
        assert spy.transactions == [{}]
        assert spy.writing() == [SD_SQL, IB_SQL]
        locked = [sql for sql in spy.sqls() if "FOR UPDATE" in sql]
        assert locked == [L_SQL]
        index = names.index("L")
        assert not {"KF", "RW", "BH", "DC"} & set(names[:index])
    # the exact text of L: a stronger lock than FOR NO KEY UPDATE, which E3 cannot tell apart
    assert L_SQL == RW_SQL + " FOR UPDATE" and "NO KEY" not in L_SQL
    assert L_SQL.endswith(" FOR UPDATE") and L_SQL.count("FOR") == 1


# ── T1 rows 3 and 11 and the rollbacks ────────────────────────────────────────


@pytest.mark.asyncio
async def test_t1_row_3_a_database_error_is_503_after_one_attempt_and_rolled_back(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, conn
):
    s = await seed_person(conn, 4308001)
    before = await digests(conn)
    spy = _Spy()

    async def deadlock(raw, args):
        raise asyncpg.DeadlockDetectedError("injected deadlock")

    spy.after[(SD_SQL, 1)] = deadlock
    resp, spy = await run_spied(pool, monkeypatch, req(ids_of(4308001), doc_of(4308001)), spy)
    assert resp.status_code == 503 and resp.json() == {"detail": "database_error"}
    assert spy.transactions == [{}] and spy.count(SD_SQL) == 1 and spy.count(O_SQL) == 2
    assert await digests(conn) == before and await ledger(conn) == []
    assert await dob_of(conn, s) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["dry_rw_no_row", "sd_no_row", "ib_unique"])
async def test_t1_row_11_a_defence_is_the_fixed_500_and_everything_is_rolled_back(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, conn, fault
):
    s = await seed_person(conn, 4308011)
    data = req(ids_of(4308011), doc_of(4308011), dry_run=fault == "dry_rw_no_row")
    spy = _Spy()
    if fault == "dry_rw_no_row":
        spy.replace[(RW_SQL, 1)] = None
    elif fault == "sd_no_row":
        spy.replace[(SD_SQL, 1)] = None  # the UPDATE DID run: the rollback must undo it
    else:
        # BH answers "no history" while one exists: IB violates UNIQUE(subject_id)
        await seed_history(conn, s, document=doc_of(4308011), birth=date(1960, 5, 5))
        spy.replace[(BH_SQL, 1)] = None
    before = await digests(conn)
    resp, spy = await run_spied(pool, monkeypatch, data, spy)
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    assert spy.names()[-1] == {"dry_rw_no_row": "RW", "sd_no_row": "SD", "ib_unique": "IB"}[fault]
    assert await digests(conn) == before and await ledger(conn) == []
    assert await dob_of(conn, s) is None
    assert issubclass(BirthDateInconsistent, RuntimeError)


@pytest.mark.asyncio
async def test_t1_row_11_the_unique_violation_is_converted_by_the_constraints_name_only(
    pool, conn, birth_writes
):
    s = await seed_person(conn, 4308021)
    spy = _Spy()
    spy.replace[(BH_SQL, 1)] = None
    await seed_history(conn, s, document=doc_of(4308021), birth=date(1960, 5, 5))
    async with pool.acquire() as c:
        with pytest.raises(BirthDateInconsistent):
            await record_birth_date(
                _SpyConn(c, spy), birth_input(req(ids_of(4308021), doc_of(4308021))), dry_run=False
            )
    assert await dob_of(conn, s) is None
    # another violation of the same insert is NOT converted: it stays the database's own error
    other = await seed_person(conn, 4308022)
    built = birth_input(req(ids_of(4308022), doc_of(4308022)))
    early = dataclasses.replace(built, birth_date=date(1899, 12, 31))
    async with pool.acquire() as c:
        with pytest.raises(asyncpg.CheckViolationError):
            await record_birth_date(c, early, dry_run=False)
    assert await dob_of(conn, other) is None and await history(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["after_sd", "after_ib"])
async def test_t1_a_failure_after_each_write_leaves_the_before_state(
    pool, conn, birth_writes, which
):
    s = await seed_person(conn, 4308031)
    other = await seed_person(conn, 4308032, dob=date(1960, 5, 5))
    before = await digests(conn)
    spy = _Spy()

    async def boom(raw, args):
        raise RuntimeError("injected")

    spy.after[(SD_SQL if which == "after_sd" else IB_SQL, 1)] = boom
    async with pool.acquire() as c:
        with pytest.raises(RuntimeError):
            await record_birth_date(
                _SpyConn(c, spy), birth_input(req(ids_of(4308031), doc_of(4308031))), dry_run=False
            )
    assert spy.names()[-1] == ("SD" if which == "after_sd" else "IB")
    # an exception right AFTER a successful IB, before the commit, leaves nothing
    assert await dob_of(conn, s) is None and await history(conn) == []
    assert await digests(conn) == before and await ledger(conn) == []
    assert await dob_of(conn, other) == date(1960, 5, 5)
    ok = await record_birth_date(
        conn, birth_input(req(ids_of(4308031), doc_of(4308031))), dry_run=False
    )
    assert ok == recorded(s)


@pytest.mark.asyncio
async def test_t1_the_owner_row_deleted_before_the_lock_is_none_owned(pool, conn, birth_writes):
    s = await seed_person(conn, 4308041)
    spy = _Spy()

    async def gone(raw, args):
        async with pool.acquire() as other, unmeasured(other):
            await other.execute("DELETE FROM related_subjects WHERE id = $1", s)

    spy.after[(O_SQL, 1)] = gone
    async with pool.acquire() as c:
        out = await record_birth_date(
            _SpyConn(c, spy), birth_input(req(ids_of(4308041), doc_of(4308041))), dry_run=False
        )
    assert out == not_registered("none_owned") and spy.names() == ["O", "L"]
    assert await history(conn) == []


@pytest.mark.asyncio
async def test_t2_c4_under_the_lock_the_owners_are_read_again_and_answer_as_written(
    pool, conn, birth_writes
):
    s = await seed_person(conn, 4308051)
    x = await seed_person(conn, 4308052)
    data = req(ids_of(4308051), doc_of(4308051))

    async def run(replace):
        spy = _Spy()
        spy.replace[(O_SQL, 2)] = replace
        async with pool.acquire() as c:
            out = await record_birth_date(_SpyConn(c, spy), birth_input(data), dry_run=False)
        return out, spy.names()

    key = ("passport", canonical_form("passport", passport(4308051)))

    def row(sid):
        return {"subject_id": sid, "id_type": key[0], "id_norm": key[1]}

    assert await run([]) == (not_registered("none_owned"), ["O", "L", "O"])
    assert await run([row(s), row(x)]) == (not_registered("two_owners"), ["O", "L", "O"])
    # one owner X != S: R3 at once, no KF(X), no chase of X
    assert await run([row(x)]) == (not_registered("set_differs"), ["O", "L", "O"])
    assert await history(conn) == [] and await dob_of(conn, x) is None


# ── log lines ─────────────────────────────────────────────────────────────────

SERVICE_FORMAT = "related birth date registry: outcome=%s subject_id=%s dry_run=%s"
ROUTE_FORMAT = "related subject registry failure: code=%s"


class _AfterTransaction(logging.Handler):
    """Records each service line with whether the connection was still inside a transaction."""

    def __init__(self, raw):
        super().__init__(logging.DEBUG)
        self.raw, self.lines = raw, []

    def emit(self, record):
        self.lines.append((record.msg, record.args, self.raw.is_in_transaction()))


@pytest.mark.asyncio
async def test_log_rows_6_to_10_log_one_service_line_after_the_transaction(
    conn, birth_writes, pool
):
    s = await seed_person(conn, 4309001)
    service_logger = logging.getLogger(birth_module.__name__)
    async with pool.acquire() as raw:
        handler = _AfterTransaction(raw)
        previous = service_logger.level
        service_logger.addHandler(handler)
        service_logger.setLevel(logging.DEBUG)
        try:
            calls = [
                (req(ids_of(4309999), doc_of(4309001)), True),
                (req(ids_of(4309001), doc_of(4309999)), True),
                (req(ids_of(4309001), doc_of(4309001)), True),
                (req(ids_of(4309001), doc_of(4309001)), False),
                (req(ids_of(4309001), doc_of(4309001)), False),
            ]
            answers = [
                await record_birth_date(raw, birth_input(d), dry_run=dry) for d, dry in calls
            ]
        finally:
            service_logger.removeHandler(handler)
            service_logger.setLevel(previous)
    assert answers == [
        not_registered("none_owned"),
        refused("document_not_on_subject", s),
        recorded(s, True),
        recorded(s),
        already(s),
    ]
    assert handler.lines == [
        (SERVICE_FORMAT, ("subject_not_registered", None, True), False),
        (SERVICE_FORMAT, ("not_recordable", s, True), False),
        (SERVICE_FORMAT, ("recorded", s, True), False),
        (SERVICE_FORMAT, ("recorded", s, False), False),
        (SERVICE_FORMAT, ("already_recorded", s, False), False),
    ]


def _lines(caplog, module):
    return [(r.msg, r.args) for r in caplog.records if r.name == module.__name__]


@pytest.mark.asyncio
async def test_log_rows_1_2_4_5_log_no_service_line_and_1_2_the_routers_fixed_line(
    pool, auth_on, audit_logger, monkeypatch, conn, caplog
):
    caplog.set_level(logging.DEBUG)
    good = req(ids_of(4309011), doc_of(4309011), dry_run=True)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    as_identity(monkeypatch, None)
    expected = []
    async with make_client(pool) as c:
        monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
        assert (await post(c, good)).status_code == 503
        expected.append((ROUTE_FORMAT, ("registry_unavailable",)))
        monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
        wrong = {**HEADERS, "X-CRM-Write-Key": "wrong-key"}
        assert (await post(c, good, headers=wrong)).status_code == 401
        expected.append((ROUTE_FORMAT, ("unauthorized",)))
        assert (await post(c, b" " * (route_module.MAX_BODY_BYTES + 1))).status_code == 422
        assert (await post(c, {**good, "basis": "manual"})).status_code == 422
    assert _lines(caplog, birth_module) == []
    assert _lines(caplog, route_module) == expected


@pytest.mark.asyncio
async def test_log_rows_3_and_11_log_only_the_routers_fixed_line(
    pool, enabled, auth_on, audit_logger, birth_writes, monkeypatch, conn, caplog
):
    caplog.set_level(logging.DEBUG)
    await seed_person(conn, 4309021)
    data = req(ids_of(4309021), doc_of(4309021))
    deadlock = _Spy()

    async def boom(raw, args):
        raise asyncpg.DeadlockDetectedError("injected deadlock")

    deadlock.after[(SD_SQL, 1)] = boom
    resp, _ = await run_spied(pool, monkeypatch, data, deadlock)
    assert resp.status_code == 503 and resp.json() == {"detail": "database_error"}
    short = _Spy()
    short.replace[(SD_SQL, 1)] = None
    resp, _ = await run_spied(pool, monkeypatch, data, short)
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    assert _lines(caplog, birth_module) == []
    assert _lines(caplog, route_module) == [
        (ROUTE_FORMAT, ("database_error",)),
        (ROUTE_FORMAT, ("internal_error",)),
    ]


# ── T4: concurrency ───────────────────────────────────────────────────────────


async def two_calls(pool, first, second, *, hold_sql, after=True, observe=None):
    """``first`` and ``second`` are (kind, data) pairs: the first holds at ``hold_sql``; the second
    starts, must be seen blocked on a lock; the first is released. Returns both answers and spies."""
    spy1, spy2 = _Spy(), _Spy()
    hold = _Point(spy1, hold_sql, after=after)
    tasks: list = []
    make = {"birth": birth_task, "detach": detach_task}
    async with pool.acquire() as c1, pool.acquire() as c2, pool.acquire() as observer:
        try:
            tasks.append(make[first[0]](c1, spy1, first[1]))
            await _wait(hold.reached, "the first at its hold point", *tasks)
            tasks.append(make[second[0]](c2, spy2, second[1]))
            await _blocked(observer, c2.get_server_pid(), *tasks)
            if observe is not None:
                observe(spy2)
            hold.release.set()
            won = await asyncio.wait_for(tasks[0], CAP)
            lost = await asyncio.wait_for(tasks[1], CAP)
        finally:
            await _settle(tasks, [hold])
    return won, lost, spy1, spy2


def _blocked_at_l(spy):
    assert spy.names() == ["O", "L"]


@pytest.mark.asyncio
async def test_e1_the_same_request_twice_the_second_blocked_at_l_is_already_recorded(
    pool, conn, birth_writes
):
    s = await seed_person(conn, 4310001)
    data = req(ids_of(4310001), doc_of(4310001))
    won, lost, _, second = await two_calls(
        pool,
        ("birth", data),
        ("birth", data),
        hold_sql=IB_SQL,
        observe=_blocked_at_l,
    )
    assert won == recorded(s) and lost == already(s)
    assert second.names() == ["O", "L", "O", "KF"]
    assert len(await history(conn)) == 1 and await dob_of(conn, s) == B_DATE


@pytest.mark.asyncio
async def test_e2_two_dates_for_one_record_the_second_blocked_at_l_is_birth_date_differs(
    pool, conn, birth_writes
):
    s = await seed_person(conn, 4310011)
    first = req(ids_of(4310011), doc_of(4310011), birth=B)
    second = req(ids_of(4310011), doc_of(4310011), birth=B2)
    won, lost, _, spy2 = await two_calls(pool, ("birth", first), ("birth", second), hold_sql=IB_SQL)
    assert won == recorded(s) and lost == refused("birth_date_differs", s)
    assert spy2.names() == ["O", "L", "O", "KF"]  # RW is never read before L
    assert await dob_of(conn, s) == B_DATE and len(await history(conn)) == 1


@pytest.mark.asyncio
async def test_e3_a_detach_blocked_behind_the_birth_date_sees_the_date_it_wrote(
    pool, conn, birth_writes
):
    birth_writes.update(DETACH_WRITES)
    k = (("passport", passport(4310021)), ("nik", nik("150680", 4210)))
    s = await seed_subject(conn, "person", "ZzE3", k)
    await seed_document(conn, s, 4310021 * 100 + 1)
    data = req(k, doc_of(4310021))
    detach = detach_req(k, k[1])
    won, lost, _, spy2 = await two_calls(pool, ("birth", data), ("detach", detach), hold_sql=IB_SQL)
    assert won == recorded(s)
    assert lost["outcome"] == "detached" and lost["from_id"] == s
    assert await dob_of(conn, s) == B_DATE and await keys_of(conn, s) == [k[0]]
    assert await keys_of(conn, lost["to_id"]) == [k[1]]


@pytest.mark.asyncio
async def test_e4_a_birth_date_blocked_behind_a_detach_that_judged_no_date_then_records(
    pool, conn, birth_writes
):
    birth_writes.update(DETACH_WRITES)
    k = (("passport", passport(4310031)), ("nik", nik("150680", 4220)))
    s = await seed_subject(conn, "person", "ZzE4", k)
    await seed_document(conn, s, 4310031 * 100 + 1)
    data = req(k, doc_of(4310031))
    detach = detach_req(k, k[1])
    won, lost, _, spy2 = await two_calls(
        pool, ("detach", detach), ("birth", data), hold_sql=detach_module._LOCK_SQL
    )
    assert won == {
        "outcome": "not_detachable", "reason": "owner_has_no_birth_date", "from_id": s, "to_id": None
    }  # fmt: skip
    assert lost == recorded(s)
    assert spy2.names() == ["O", "L", "O", "KF", "BH", "DC", "SD", "IB"]
    assert await dob_of(conn, s) == B_DATE


@pytest.mark.asyncio
async def test_e5_a_merge_of_the_named_source_wins_and_the_birth_date_is_subject_is_merged(
    pool, conn, birth_writes
):
    birth_writes.update(MERGE_WRITES)
    target_keys = ids_of(4310041)
    target = await seed_subject(conn, "person", "ZzE5 target", target_keys)
    q = ("permit", permit(4310041))
    source = await seed_subject(conn, "person", "ZzE5 source", [q])
    await seed_document(conn, source, 4310041 * 100 + 1)
    data = req([q], doc_of(4310041))
    mspy, bspy = _Spy(), _Spy()
    hold = _Point(mspy, merges_module._UPDATE_POINTER_SQL, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2, pool.acquire() as observer:
        try:
            tasks.append(
                asyncio.ensure_future(
                    record_merge(_SpyConn(c1, mspy), merge_input([q], target_keys), dry_run=False)
                )
            )
            await _wait(hold.reached, "the merge after its pointer UPDATE", *tasks)
            tasks.append(birth_task(c2, bspy, data))
            await _blocked(observer, c2.get_server_pid(), *tasks)
            assert bspy.names() == ["O", "L"]
            hold.release.set()
            won = await asyncio.wait_for(tasks[0], CAP)
            lost = await asyncio.wait_for(tasks[1], CAP)
        finally:
            await _settle(tasks, [hold])
    assert won["outcome"] == "merged" and lost == refused("subject_is_merged", source)
    assert await dob_of(conn, source) is None and await dob_of(conn, target) is None
    assert bspy.names() == ["O", "L", "O", "KF"]


@pytest.mark.asyncio
async def test_e6_a_dry_run_held_after_its_first_read_answers_from_its_snapshot(
    pool, conn, birth_writes
):
    s = await seed_person(conn, 4310051)
    dry = req(ids_of(4310051), doc_of(4310051), dry_run=True)
    spy = _Spy()
    hold = _Point(spy, O_SQL, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(birth_task(c1, spy, dry))
            await _wait(hold.reached, "the dry run after its first read", *tasks)
            applied = await record_birth_date(
                c2, birth_input({**dry, "dry_run": False}), dry_run=False
            )
            hold.release.set()
            answer = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    assert applied == recorded(s)
    assert answer == recorded(s, True)  # from its own snapshot, taken before the apply
    assert await dob_of(conn, s) == B_DATE


@pytest.mark.asyncio
async def test_e7_a_detach_that_moved_the_one_key_of_k_before_l_is_set_differs_nothing_written(
    pool, conn, birth_writes
):
    birth_writes.update(DETACH_WRITES)
    k = (("passport", passport(4310061)), ("nik", nik("150680", 4230)))
    s = await seed_subject(conn, "person", "ZzE7", k, dob=OTHER_DOB)
    await seed_document(conn, s, 4310061 * 100 + 1)
    only_nik = req([k[1]], doc_of(4310061))
    spy = _Spy()
    hold = _Point(spy, O_SQL, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(birth_task(c1, spy, only_nik))
            await _wait(hold.reached, "the birth date after its candidate read", *tasks)
            moved = await detach_identifier(c2, detach_input(detach_req(k, k[1])), dry_run=False)
            hold.release.set()
            answer = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    x = moved["to_id"]
    assert moved["outcome"] == "detached" and await keys_of(conn, x) == [k[1]]
    assert answer == not_registered("set_differs")
    assert spy.names() == ["O", "L", "O"]
    assert await dob_of(conn, x) is None and await dob_of(conn, s) == OTHER_DOB
    assert await history(conn) == []


# ── the undo of 2.2 ───────────────────────────────────────────────────────────

UNDO_BLOCK = """DO $$
DECLARE s bigint := <S>; h related_subject_birth_dates; n bigint;
BEGIN
  PERFORM 1 FROM related_subjects WHERE id = s FOR UPDATE;
  GET DIAGNOSTICS n = ROW_COUNT; IF n <> 1 THEN RAISE EXCEPTION 'undo 1 row'; END IF;
  SELECT * INTO h FROM related_subject_birth_dates WHERE subject_id = s;
  IF NOT FOUND THEN RAISE EXCEPTION 'undo 2 history'; END IF;
  IF (SELECT date_of_birth FROM related_subjects WHERE id = s) IS DISTINCT FROM h.birth_date
     THEN RAISE EXCEPTION 'undo 3 date changed since'; END IF;
  IF EXISTS (SELECT 1 FROM related_subject_detachments WHERE from_id = s)
     THEN RAISE EXCEPTION 'undo 4 a detachment exists on this record: undo it first'; END IF;
  UPDATE related_subjects SET date_of_birth = NULL WHERE id = s AND date_of_birth = h.birth_date;
  GET DIAGNOSTICS n = ROW_COUNT; IF n <> 1 THEN RAISE EXCEPTION 'undo 5'; END IF;
  DELETE FROM related_subject_birth_dates WHERE id = h.id;
  GET DIAGNOSTICS n = ROW_COUNT; IF n <> 1 THEN RAISE EXCEPTION 'undo 6'; END IF;
END $$;"""


def undo_block(s: int) -> str:
    return UNDO_BLOCK.replace("<S>", str(int(s)))


@pytest.mark.asyncio
async def test_undo_restores_the_before_digest_of_every_table(conn, birth_writes):
    s = await seed_person(conn, 4311001)
    before = await digests(conn)
    assert await record_birth_date(
        conn, birth_input(req(ids_of(4311001), doc_of(4311001))), dry_run=False
    ) == recorded(s)
    assert await digests(conn) != before
    async with unmeasured(conn):
        await conn.execute(undo_block(s))
    assert await digests(conn) == before


@pytest.mark.asyncio
async def test_undo_refuses_when_the_date_changed_since_or_there_is_no_history(conn, birth_writes):
    s = await seed_person(conn, 4311011)
    with pytest.raises(asyncpg.PostgresError, match="undo 2 history"):
        await conn.execute(undo_block(s))
    await record_birth_date(conn, birth_input(req(ids_of(4311011), doc_of(4311011))), dry_run=False)
    async with unmeasured(conn):
        await conn.execute(
            "UPDATE related_subjects SET date_of_birth = $2 WHERE id = $1", s, date(1960, 5, 5)
        )
    with pytest.raises(asyncpg.PostgresError, match="undo 3 date changed since"):
        await conn.execute(undo_block(s))
    assert len(await history(conn)) == 1


@pytest.mark.asyncio
async def test_undo_refuses_on_any_detachment_even_one_that_began_before_the_birth_date(
    pool, conn, birth_writes
):
    birth_writes.update(DETACH_WRITES)
    k = (("passport", passport(4311021)), ("nik", nik("150680", 4240)))
    s = await seed_subject(conn, "person", "ZzUndo", k)
    await seed_document(conn, s, 4311021 * 100 + 1)
    data = req(k, doc_of(4311021))
    dspy = _Spy()
    hold = _Point(dspy, detach_module._LOCK_SQL)  # BEFORE its L: its transaction has begun
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(detach_task(c1, dspy, detach_req(k, k[1])))
            await _wait(hold.reached, "the detach before its lock", *tasks)
            assert await record_birth_date(c2, birth_input(data), dry_run=False) == recorded(s)
            hold.release.set()
            detached = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    assert detached["outcome"] == "detached"
    # the detach began before the birth date was committed: its created_at is EARLIER
    detach_at, birth_at = (
        await conn.fetchval("SELECT created_at FROM related_subject_detachments"),
        (await history(conn))[0]["created_at"],
    )
    assert detach_at < birth_at
    before = await digests(conn)
    with pytest.raises(asyncpg.PostgresError, match="undo 4 a detachment exists"):
        await conn.execute(undo_block(s))
    assert await digests(conn) == before and await dob_of(conn, s) == B_DATE
    assert len(await history(conn)) == 1


# ── privacy: no key, date or hash in an answer, a log line or an audit row ────

PROBE_KEY = passport(4312001)
PROBES = (
    PROBE_KEY,
    doc_of(4312001),
    B,
    "19770312",
    "12031977",
    "12-03-1977",
    "12/03/1977",
    B2,
    PLAN,
)


async def privacy_world(conn):
    s = await seed_person(conn, 4312001)
    await seed_person(conn, 4312002, dob=date(1960, 5, 5))
    await seed_person(conn, 4312003, dob=B_DATE)
    await seed_person(conn, 4312004, tags=())
    return s


def _boom_database(mp):
    async def boom(*a, **k):
        raise asyncpg.PostgresError(f"boom {PROBE_KEY} {doc_of(4312001)} {B}")

    mp.setattr(route_module, "record_birth_date", boom)


def _boom_inconsistent(mp):
    async def boom(*a, **k):
        raise BirthDateInconsistent(f"boom {PROBE_KEY} {doc_of(4312001)} {B}")

    mp.setattr(route_module, "record_birth_date", boom)


def _boom_parser(mp):
    def boom(cls, *a, **k):
        raise RuntimeError(f"boom {PROBE_KEY} {doc_of(4312001)} {B}")

    mp.setattr(route_module.RelatedBirthDateRequest, "model_validate_json", classmethod(boom))


P1 = ids_of(4312001)
PRIVACY_CASES = {
    "recorded_dry": (req(P1, doc_of(4312001), dry_run=True), 200, None),
    "recorded": (req(P1, doc_of(4312001)), 200, None),
    "already": (req(P1, doc_of(4312001)), 200, None),
    "differs": (req(ids_of(4312002), doc_of(4312002), birth=B), 200, None),
    "same_without_document": (req(ids_of(4312003), doc_of(4312999)), 200, None),
    "no_document": (req(ids_of(4312004), doc_of(4312001)), 200, None),
    "not_registered": (req(ids_of(4312999), doc_of(4312001)), 200, None),
    "invalid_date": (req(P1, doc_of(4312001), birth="1977-02-30"), 422, None),
    "invalid_company": (
        req(P1, doc_of(4312001), subject={"kind": "company", "identifiers": keys(P1)}),
        422,
        None,
    ),
    "hostile_field": (req(P1, doc_of(4312001), **{B: 1, "document_": PROBE_KEY}), 422, None),
    "raw_garbage": (f'{{"subject": "{PROBE_KEY}", "birth_date": "{B}" '.encode(), 422, None),
    "service_database_error": (req(P1, doc_of(4312001)), 503, _boom_database),
    "service_inconsistent": (req(P1, doc_of(4312001)), 500, _boom_inconsistent),
    "parser_raises": (req(P1, doc_of(4312001)), 500, _boom_parser),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(PRIVACY_CASES))
async def test_privacy_the_answer_the_log_and_the_audit_rows_carry_no_key_date_or_hash(
    case, client, conn, caplog, monkeypatch, today
):
    caplog.set_level(logging.DEBUG)
    s = await privacy_world(conn)
    data, status, patch = PRIVACY_CASES[case]
    if case == "already":
        assert (await post(client, data)).json() == recorded(s)
    caplog.clear()
    if patch:
        patch(monkeypatch)
    low = await conn.fetchval("SELECT clock_timestamp()::timestamp")
    resp = await post(client, data)
    high = await conn.fetchval("SELECT clock_timestamp()::timestamp")
    assert resp.status_code == status, (case, resp.status_code, resp.text[:120])
    forbidden = privacy.probe_forms(PROBES)
    assert privacy.leaks(forbidden, privacy.response_surface(resp.text)) == [], case
    records = [r for r in caplog.records if not privacy.is_http_client_logger(r.name)]
    assert privacy.leaks(forbidden, privacy.log_surface(records)) == [], case
    rows, types = await privacy.audit_snapshot(conn)
    excluded = {c for c, t in types.items() if t in privacy.UNSCANNED_AUDIT_TYPES}
    assert excluded == {"created_at"}, excluded
    await privacy.audit_created_at_is_server_generated(
        conn, inspect.getsource(activity_logger.log_api_call)
    )
    assert privacy.created_at_outside(rows, low, high) == [], case
    assert all(r["request_body"] is None for r in rows), case
    assert privacy.leaks(forbidden, privacy.audit_texts(rows, types)) == [], case


def test_privacy_teeth_a_date_a_hash_and_a_key_are_caught_on_every_surface():
    forbidden = privacy.probe_forms(PROBES)
    for value in (B, "19770312", doc_of(4312001), doc_of(4312001).upper(), PROBE_KEY):
        record = logging.LogRecord(
            "zz.teeth", logging.INFO, __file__, 1, "teeth %s", (value,), None
        )
        assert privacy.leaks(forbidden, privacy.log_surface([record])), value
        assert privacy.leaks(forbidden, privacy.response_surface(json.dumps({"x": value}))), value
    clean = json.dumps(recorded(7))
    assert privacy.leaks(forbidden, privacy.response_surface(clean)) == []
    line = logging.LogRecord(
        "zz.teeth", logging.INFO, __file__, 1, SERVICE_FORMAT, ("recorded", 7, True), None
    )
    assert privacy.leaks(forbidden, privacy.log_surface([line])) == []


# ── the write measurement and the module's own inventories ────────────────────


def sql_texts(module) -> dict[str, str]:
    return {k: v for k, v in vars(module).items() if k.endswith("_SQL") and isinstance(v, str)}


def test_the_service_writes_only_sd_and_ib_and_names_no_other_table():
    texts = sql_texts(birth_module)
    writing = {
        k for k, v in texts.items() if v.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    }
    assert writing == {"_SET_DATE_SQL", "_INSERT_BIRTH_DATE_SQL"}
    assert texts["_SET_DATE_SQL"].startswith("UPDATE related_subjects SET date_of_birth = $2 WHERE")
    assert "date_of_birth IS NULL" in texts["_SET_DATE_SQL"]
    assert texts["_SET_DATE_SQL"].count("SET") == 1
    assert texts["_INSERT_BIRTH_DATE_SQL"].startswith("INSERT INTO related_subject_birth_dates (")
    named = set()
    for sql in texts.values():
        named |= set(re.findall(r"\b(?:FROM|INTO|UPDATE)\s+(related_\w+|clients\w*)", sql))
    assert named <= {
        "related_subjects", "related_subject_birth_dates", "related_subject_documents",
        "related_subject_identifiers",
    }  # fmt: skip
    tree = ast.parse(Path(birth_module.__file__).read_text())
    assert not any(
        isinstance(n, ast.Attribute) and n.attr in {"executemany", "copy_records_to_table"}
        for n in ast.walk(tree)
    )


def test_only_the_service_and_371_name_the_new_table_and_nothing_updates_or_deletes_it():
    app_root = BACKEND.parent  # apps/backend-rag
    names = set()
    for path in (BACKEND).rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        text = path.read_text()
        if TABLE in text:
            names.add(path.name)
            assert not re.search(rf"(UPDATE|DELETE\s+FROM)\s+{TABLE}", text), path
    assert names == {"related_birth_dates.py"}
    assert TABLE in MIGRATION.read_text() and app_root.name == "backend-rag"


def test_the_route_is_the_seventh_of_the_sealed_router():
    assert len([e for e in ROUTER_MANIFEST if e.name == "crm_related_subjects"]) == 1
    paths = {(r.path, tuple(sorted(r.methods))) for r in route_module.router.routes}
    assert paths == {
        ("/api/crm/related-subjects/register", ("POST",)),
        ("/api/crm/related-subjects/documents/register", ("POST",)),
        ("/api/crm/related-subjects/names/register", ("POST",)),
        ("/api/crm/related-subjects/links/register", ("POST",)),
        ("/api/crm/related-subjects/merges/register", ("POST",)),
        ("/api/crm/related-subjects/identifiers/detach", ("POST",)),
        (URL, ("POST",)),
    }
    assert route_module.router.route_class is route_module._SealedRoute


def test_the_public_entry_is_exact_path_and_post_only():
    entries = [e for e in PUBLIC_ENDPOINTS if e.prefix == URL]
    assert len(entries) == 1
    assert entries[0].match == "exact" and tuple(entries[0].methods) == ("POST",)
    assert any(e.matches(URL, "POST") for e in PUBLIC_ENDPOINTS)
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert not any(e.matches(URL, method) for e in PUBLIC_ENDPOINTS), method
    for path in (
        URL + "/",
        URL + "/x",
        "/api/crm/related-subjects/birth-date",
        "/api/crm/related-subjects",
    ):
        assert not any(e.matches(path, "POST") for e in PUBLIC_ENDPOINTS), path
    for path in (READ, f"{READ}/7"):
        assert not any(e.matches(path, "GET") for e in PUBLIC_ENDPOINTS), path


def test_openapi_the_route_is_in_the_full_document_and_not_in_the_consumed_subset():
    from scripts.openapi_consumed import derive_consumed

    app = FastAPI()
    app.include_router(route_module.router)
    document = app.openapi()
    assert set(document["paths"][URL]) == {"post"}
    mouth_src = BACKEND.parents[1] / "mouth" / "src"
    assert mouth_src.is_dir()
    consumed = derive_consumed(mouth_src)
    assert URL not in set().union(*consumed.values())


@pytest.mark.asyncio
async def test_the_older_write_routes_still_answer_their_empty_body_422_as_before(client, conn):
    expected = {
        "/api/crm/related-subjects/register": [
            item(["kind"], "missing"), item(["identifiers"], "missing"), item(["evidence"], "missing"),
        ],
    }  # fmt: skip
    for url, items in expected.items():
        resp = await post(client, b"{}", url=url)
        assert resp.status_code == 422 and resp.json() == {"detail": items}
