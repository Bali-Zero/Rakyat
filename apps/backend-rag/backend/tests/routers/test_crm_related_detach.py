"""Registry of related subjects, slice 4D: one key and listed documents of a person record moved to a
NEW record through one sealed route, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362, 365, 367, 369 and 370
forward parts, minimal ``clients`` and ``client_family_members`` tables carrying ``passport_number``,
and the audit table), seeds invented rows (``Zz`` / ``ZZ`` conventions, client ids from 424242 up,
national numbers starting ``990042``, which no issuing region uses) and calls the real service and
routers behind the real middleware stack. Nothing here is a real person, company, client or document.

Write measurement: the write log records the table, the operation, the OLD and NEW key of the row
(``related_subject_identifiers``: id_type, id_norm, subject_id; ``related_subject_documents``:
subject_id, sha256; any other table its primary key) and, for an UPDATE, the sorted names of the
columns whose value changed. The teardown accepts only the writes a test declares through
``writes``. Every catalog read is scoped to ``current_schema()``. The spy and the controller are this
module's own: hooks are keyed by (statement, occurrence) and receive the RAW connection and the
arguments; ``spy.transactions`` keeps the keywords of each ``transaction(...)``.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import inspect
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
from pydantic import ValidationError

import backend.app.routers.crm_related_registry_read as read_module
import backend.app.routers.crm_related_subjects as route_module
import backend.services.crm.related_detach as detach_module
import backend.services.crm.related_documents as documents_module
import backend.services.crm.related_links as links_module
import backend.services.crm.related_merges as merges_module
import backend.services.crm.related_names as names_module
from backend.app.auth.public_endpoints import PUBLIC_ENDPOINTS
from backend.app.core.config import settings
from backend.app.setup.router_manifest import ROUTER_MANIFEST
from backend.db.migration_base import split_migration_sql
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import HybridAuthMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm import related_registry_read
from backend.services.crm.related_detach import (
    DetachInconsistent,
    DetachInput,
    detach_identifier,
    keys_sha256,
    nik_date,
)
from backend.services.crm.related_documents import register_documents
from backend.services.crm.related_identifiers import canonical_form
from backend.services.crm.related_links import record_links
from backend.services.crm.related_merges import record_merge
from backend.services.crm.related_names import record_names
from backend.services.crm.related_subjects import (
    REGISTRY_ACTOR,
    IdentifierInput,
    register_subject,
)
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures import registry_privacy as privacy
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

BACKEND = Path(route_module.__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "db" / "migrations_v2"
MIGRATION_DETACH = MIGRATIONS / "370_related_subject_detachments.sql"
READ = "/api/crm/related-registry"
URL = "/api/crm/related-subjects/identifiers/detach"
URL_SUBJECT = "/api/crm/related-subjects/register"
URL_DOCUMENTS = "/api/crm/related-subjects/documents/register"
URL_NAMES = "/api/crm/related-subjects/names/register"
URL_LINKS = "/api/crm/related-subjects/links/register"
URL_MERGES = "/api/crm/related-subjects/merges/register"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
PLAN = "cd34" * 16
TABLE = "related_subject_detachments"

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}
AUDIT_INSERT = ("api_audit_trail", "INSERT")
DETACH_WRITES = {
    ("related_subjects", "INSERT"),
    ("related_subject_identifiers", "UPDATE"),
    ("related_subject_documents", "UPDATE"),
    ("related_subject_detachments", "INSERT"),
}

LIVE1, LIVE2, GONE = 424242, 424243, 424244
NAME_LIVE1 = "ZzClientOne"

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)
DOB = date(1977, 3, 12)

O_SQL = detach_module._OWNERS_SQL
KF_SQL = detach_module._OWNER_KEYS_SQL
RW_SQL = detach_module._ROW_SQL
L_SQL = detach_module._LOCK_SQL
WH_SQL = detach_module._WITNESS_SQL
DC_SQL = detach_module._OWNED_DOCS_SQL
IS_SQL = detach_module._INSERT_SUBJECT_SQL
MK_SQL = detach_module._MOVE_KEY_SQL
MD_SQL = detach_module._MOVE_DOCS_SQL
IH_SQL = detach_module._INSERT_DETACH_SQL
STATEMENT_NAMES = {
    O_SQL: "O",
    KF_SQL: "KF",
    RW_SQL: "RW",
    L_SQL: "L",
    WH_SQL: "WH",
    DC_SQL: "DC",
    IS_SQL: "IS",
    MK_SQL: "MK",
    MD_SQL: "MD",
    IH_SQL: "IH",
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

# `passport_number` is read by POST /register when the key set holds a passport
# (related_subjects.py:34-43); no test key is an npwp or a nib, so no `companies` table.
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
# key, so a moved key or document link is named by both of its keys.
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
    schema = "related_detach_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    # Production-shaped: the init hook every production pool registers (the jsonb codec),
    # `statement_cache_size=0`, `max_inactive_connection_lifetime=30.0` and the API pool's
    # `command_timeout=60`; built here because the factory takes no `server_settings`.
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
            for migration in (
                "361_related_subjects_registry.sql",
                "362_related_documents.sql",
                "365_related_subject_names.sql",
                "367_related_subject_links.sql",
                "369_related_subject_merges.sql",
                "370_related_subject_detachments.sql",
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


async def forget_writes(c):
    """Takes every write made so far out of the measurement (a setup that went through a route)."""
    await c.execute("DELETE FROM write_log")


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


async def seed_raw_key(c, sid, id_type, norm):
    """A key stored with a norm no validator would produce (the service's own defences)."""
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, "
            "subject_id) VALUES ($1, $2, $2, 1, $3)",
            id_type, norm, sid,
        )  # fmt: skip


async def seed_name(c, sid, name, source="ocr_single", *, at=T0):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_names (subject_id, name, source, evidence, created_by, "
            "created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
            sid, name, source, at,
        )  # fmt: skip


def doc(tag: int) -> str:
    """A content hash: the sha256 of an invented byte string, never of a document."""
    return hashlib.sha256(f"zz-doc-{tag}".encode()).hexdigest()


async def seed_document(c, sid, tag, *, at=T0, doc_type="passport", ref=None):
    sha = doc(tag)
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
        if sid is not None:
            await c.execute(
                "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
                "created_by, created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', $4)",
                sid, sha, doc_type, at,
            )  # fmt: skip
    return sha


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


async def seed_merge_history(c, source, target, *, at=T0):
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_merges (source_id, target_id, basis, evidence, "
            "created_by, created_at) VALUES ($1, $2, 'permit_stamp', $3, 'zz-seed', $4)",
            source, target, {"plan_sha256": PLAN, "files": 1}, at,
        )  # fmt: skip


async def seed_detach_history(c, from_id, to_id, fingerprint, moved, *, id_type="nik"):
    """A history row, the evidence bound as a NATIVE dict (the production codec encodes it)."""
    async with unmeasured(c):
        await c.execute(
            "INSERT INTO related_subject_detachments (from_id, to_id, id_type, "
            "owner_keys_sha256, moved_documents, basis, evidence, created_by) "
            "VALUES ($1, $2, $3, $4, $5::text[], 'nik_birth_date', $6, 'zz-seed')",
            from_id, to_id, id_type, fingerprint, sorted(moved), {"plan_sha256": PLAN, "files": 1},
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


@pytest.fixture
def detach_writes(writes):
    """The four writes a detachment makes, on top of the audit INSERT."""
    writes.update(DETACH_WRITES)
    return writes


@pytest_asyncio.fixture
async def client(pool, enabled, auth_on, audit_logger, monkeypatch, detach_writes):
    as_identity(monkeypatch, None)
    async with make_client(pool) as c:
        yield c


@pytest_asyncio.fixture
async def admin(pool, auth_on, audit_logger, monkeypatch, detach_writes):
    """The read routes as an admin, and the write routes behind the same app (key in headers)."""
    as_identity(monkeypatch, ADMIN)
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
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


def req(owner, detach, documents=(), *, dry_run=False, kind="person", files=3, **over):
    data = {
        "owner": {"kind": kind, "identifiers": keys(owner)},
        "detach": {"type": detach[0], "value": detach[1]},
        "documents": list(documents),
        "basis": "nik_birth_date",
        "evidence": {"plan_sha256": PLAN, "files": files},
        "dry_run": dry_run,
    }
    data.update(over)
    return data


def detach_input(data: dict):
    """The service input of a request dictionary, through the route's own strict model."""
    return route_module.RelatedDetachRequest.model_validate_json(json.dumps(data)).to_input()


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


def subject_input(ids, kind="person"):
    data = {
        "kind": kind,
        "identifiers": keys(ids),
        "evidence": {"plan_sha256": PLAN, "subject_ref": "REF-4D", "tier": "t1"},
        "dry_run": False,
    }
    return route_module.RelatedSubjectRegisterRequest.model_validate_json(
        json.dumps(data)
    ).to_subject()


def document_entry(tag: int) -> dict:
    return {
        "sha256": doc(tag),
        "byte_size": 4096,
        "mime_type": "application/pdf",
        "doc_type": "passport",
        "first_seen_on": "2026-01-05",
        "first_channel": "whatsapp",
        "drive": [],
    }


def documents_body(ids, tags, kind="person") -> dict:
    return {
        "subject": {"kind": kind, "identifiers": keys(ids)},
        "documents": [document_entry(t) for t in tags],
        "evidence": {"plan_sha256": PLAN, "subject_ref": "REF-4D"},
        "dry_run": False,
    }


def documents_input(ids, tags):
    return route_module.RelatedDocumentsRegisterRequest.model_validate_json(
        json.dumps(documents_body(ids, tags))
    ).to_input()


NAME_EVIDENCE = {"plan_sha256": PLAN, "files": 1, "blobs": 1, "mrz_valid": False}


def names_body(ids, name="ZZ DETACH RECORDED", kind="person") -> dict:
    return {
        "subject": {"kind": kind, "identifiers": keys(ids)},
        "names": [{"name": name, "source": "ocr_single", "evidence": NAME_EVIDENCE}],
        "dry_run": False,
    }


def names_input(ids, name="ZZ DETACH RECORDED"):
    return route_module.RelatedNamesRegisterRequest.model_validate_json(
        json.dumps(names_body(ids, name))
    ).to_input()


def links_body(ids, client_id=LIVE1, kind="person") -> dict:
    return {
        "subject": {"kind": kind, "identifiers": keys(ids)},
        "links": [
            {
                "client_id": client_id,
                "basis": "identity",
                "evidence": {"plan_sha256": PLAN, "files": 1},
            }
        ],
        "dry_run": False,
    }


def links_input(ids, client_id=LIVE1):
    return route_module.RelatedLinksRegisterRequest.model_validate_json(
        json.dumps(links_body(ids, client_id))
    ).to_input()


def detached(s, t, dry_run=False):
    return {"outcome": "detached", "from_id": s, "to_id": t, "dry_run": dry_run}


def would(s):
    return detached(s, None, True)


def already(s, t, dry_run=False):
    return {"outcome": "already_detached", "from_id": s, "to_id": t, "dry_run": dry_run}


def refused(reason, s, t=None):
    return {"outcome": "not_detachable", "reason": reason, "from_id": s, "to_id": t}


def not_registered(reason):
    return {"outcome": "subject_not_registered", "reason": reason}


def merged(s, t):
    return {"outcome": "merged", "source_id": s, "target_id": t, "dry_run": False}


def fingerprint(*pairs) -> str:
    """The test's own hex of 2.1's byte string: 'type:norm' lines, code-point order, one newline."""
    lines = sorted(f"{t}:{canonical_form(t, v)}" for t, v in pairs)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def owner_keys(n: int = 4242001, ddmmyy: str = "150680"):
    """An owner's complete key set: a passport and the national number of ANOTHER person (the
    date it encodes is not the owner's), and that key."""
    k = (("passport", passport(n)), ("nik", nik(ddmmyy, n % 10000)))
    return k, k[1]


async def seed_owner(
    c, n: int = 4242001, *, ddmmyy="150680", dob=DOB, docs=(1, 2), name="ZzOwner Detach", **kw
):
    k, _ = owner_keys(n, ddmmyy)
    sid = await seed_subject(c, "person", name, k, dob=dob, **kw)
    for tag in docs:
        await seed_document(c, sid, n * 100 + tag)
    return sid


def owner_docs(n: int = 4242001, tags=(1, 2)) -> list[str]:
    return [doc(n * 100 + t) for t in tags]


async def ledger(c):
    """Every write but the audit INSERT, in order: (table, op, old key, new key, changed)."""
    rows = await c.fetch(
        "SELECT tbl, op, old_key, new_key, changed FROM write_log "
        "WHERE tbl <> 'api_audit_trail' ORDER BY id"
    )
    return [(r["tbl"], r["op"], r["old_key"], r["new_key"], r["changed"]) for r in rows]


async def history(c):
    return await c.fetch(
        "SELECT id, from_id, to_id, id_type, owner_keys_sha256, moved_documents, basis, "
        "evidence, jsonb_typeof(evidence) AS kind, created_by, created_at "
        "FROM related_subject_detachments ORDER BY id"
    )


async def keys_of(c, sid):
    rows = await c.fetch(
        "SELECT id_type, id_norm FROM related_subject_identifiers WHERE subject_id = $1 "
        "ORDER BY id_type, id_norm",
        sid,
    )
    return [(r["id_type"], r["id_norm"]) for r in rows]


async def docs_of(c, sid):
    rows = await c.fetch(
        "SELECT sha256 FROM related_subject_documents WHERE subject_id = $1 ORDER BY sha256", sid
    )
    return [r["sha256"] for r in rows]


async def subject_count(c):
    return await c.fetchval("SELECT count(*) FROM related_subjects")


REGISTRY_TABLES = (
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
UNTOUCHED_TABLES = (
    "related_documents",
    "related_document_sightings",
    "related_subject_names",
    "related_subject_links",
    "related_subject_merges",
    "clients",
    "client_family_members",
)


async def digest(c, table):
    """md5 of the table's rows as ordered to_jsonb texts."""
    return await c.fetchval(
        f"SELECT md5(coalesce(string_agg(to_jsonb(r)::text, ',' ORDER BY to_jsonb(r)::text), '')) "
        f"FROM {table} r"
    )


async def digests(c, tables=REGISTRY_TABLES):
    return {t: await digest(c, t) for t in tables}


# ── the spy ───────────────────────────────────────────────────────────────────


class _Spy:
    """The record of one connection (or of the pool's connections): every call as (sql, args), in
    order; hooks BEFORE and AFTER a statement keyed by (sql, occurrence), called with the RAW
    connection and the arguments; ``replace`` substitutes the answer of the (sql, occurrence) that
    DID run; ``transactions`` keeps the keywords of each ``transaction(...)``."""

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

    def args_of(self, sql: str) -> list[tuple]:
        return [args for s, args in self.calls if s == sql]

    def writing(self) -> list[str]:
        out = []
        for sql, _ in self.calls:
            head = sql.lstrip().upper()
            if head.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE")):
                out.append(sql)
        return out


class _SpyConn:
    def __init__(self, conn, spy):
        self._conn, self._spy = conn, spy

    def transaction(self, **kwargs):
        self._spy.transactions.append(dict(kwargs))
        return self._conn.transaction(**kwargs)

    def __getattr__(self, attr):
        target = getattr(self._conn, attr)
        if attr in {"fetch", "fetchrow", "fetchval", "execute"}:

            async def recorded(sql, *args, **kwargs):
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


async def run_spied(pool, monkeypatch, data, spy=None):
    spy_pool, spy_app = spy_client(pool, monkeypatch, spy)
    async with spy_app as c:
        resp = await post(c, data)
    return resp, spy_pool.spy


def row(subject_id, id_type, norm):
    """A row of O as the service reads it (a mapping by column name)."""
    return {"subject_id": subject_id, "id_type": id_type, "id_norm": norm}


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
    """The observer sees the backend `pid` waiting on a lock it was not granted. The cap bounds
    when the wait gives up; no task is left pending."""
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


def apply_task(c, spy, data):
    return asyncio.ensure_future(
        detach_identifier(_SpyConn(c, spy), detach_input(data), dry_run=data["dry_run"])
    )


# ── D1-D4: the migration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_d1_columns_types_nullability_identity_and_indexes(pool, conn):
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
        "from_id": ("bigint", "NO"),
        "to_id": ("bigint", "NO"),
        "id_type": ("text", "NO"),
        "owner_keys_sha256": ("text", "NO"),
        "moved_documents": ("ARRAY", "NO"),
        "basis": ("text", "NO"),
        "evidence": ("jsonb", "NO"),
        "created_by": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    }
    element = await conn.fetchval(
        "SELECT udt_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = $1 AND column_name = 'moved_documents'",
        TABLE,
    )
    assert element == "_text"
    identity = await conn.fetchrow(
        "SELECT is_identity, identity_generation FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = 'id'",
        TABLE,
    )
    assert (identity["is_identity"], identity["identity_generation"]) == ("YES", "ALWAYS")
    default = await conn.fetchval(
        "SELECT column_default FROM information_schema.columns WHERE table_schema = "
        "current_schema() AND table_name = $1 AND column_name = 'created_at'",
        TABLE,
    )
    assert default == "now()"

    indexes = await conn.fetch(
        "SELECT c.relname, i.indisunique, i.indisprimary, pg_get_indexdef(i.indexrelid) AS def "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = current_schema() AND t.relname = $1",
        TABLE,
    )
    by_column = {r["def"].rsplit("(", 1)[1].rstrip(")"): r for r in indexes}
    assert set(by_column) == {"id", "from_id", "to_id"}, [r["def"] for r in indexes]
    assert by_column["id"]["indisprimary"]
    assert by_column["to_id"]["indisunique"] and not by_column["to_id"]["indisprimary"]
    assert not by_column["from_id"]["indisunique"]
    assert by_column["from_id"]["relname"] == "idx_related_subject_detachments_from"

    fks = {
        (r["col"], r["target"]): r["action"]
        for r in await conn.fetch(
            "SELECT a.attname AS col, r.relname AS target, c.confdeltype::text AS action "
            "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_class r ON r.oid = c.confrelid "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.contype = 'f' AND n.nspname = current_schema() AND t.relname = $1",
            TABLE,
        )
    }
    assert fks == {("from_id", "related_subjects"): "c", ("to_id", "related_subjects"): "c"}


INSERT_HISTORY = (
    "INSERT INTO related_subject_detachments (from_id, to_id, id_type, owner_keys_sha256, "
    "moved_documents, basis, evidence, created_by) VALUES ($1, $2, $3, $4, $5::text[], $6, "
    "$7::jsonb, 'zz')"
)


@pytest.mark.asyncio
async def test_d2_the_checks_refuse_each_bad_value_and_two_rows_of_one_from_id_pass(pool, conn):
    s = await seed_subject(conn, "person", "ZzD2 Owner", [("passport", passport(4242201))])
    t1 = await seed_subject(conn, "person", None, [("nik", nik("150680", 2201))])
    t2 = await seed_subject(conn, "person", None, [("nik", nik("150680", 2202))])
    t3 = await seed_subject(conn, "person", None, [("nik", nik("150680", 2203))])
    fp = "ab" * 32
    good = (s, t1, "nik", fp, [doc(1)], "nik_birth_date", {"plan_sha256": PLAN, "files": 1})
    async with unmeasured(conn):
        refused_rows = [
            (asyncpg.CheckViolationError, {6: "a string"}),
            (asyncpg.CheckViolationError, {6: [1, 2]}),
            (asyncpg.CheckViolationError, {5: "permit_stamp"}),
            (asyncpg.CheckViolationError, {2: "zzbogus"}),
            (asyncpg.CheckViolationError, {1: s}),
            (asyncpg.CheckViolationError, {3: "AB" * 32}),
            (asyncpg.CheckViolationError, {3: "a" * 63}),
            (asyncpg.NotNullViolationError, {4: None}),
            (asyncpg.ForeignKeyViolationError, {1: t1 + 999}),
        ]
        for error, change in refused_rows:
            args = list(good)
            for index, value in change.items():
                args[index] = value
            with pytest.raises(error):
                await conn.execute(INSERT_HISTORY, *args)
        assert await conn.fetchval("SELECT count(*) FROM related_subject_detachments") == 0
        # one record may give several keys: two rows of one from_id, an empty moved list
        await conn.execute(INSERT_HISTORY, *good)
        await conn.execute(INSERT_HISTORY, s, t2, "nik", fp, [], "nik_birth_date", {})
        # one detachment per new record
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(INSERT_HISTORY, s, t2, "nik", fp, [], "nik_birth_date", {})
        await conn.execute(INSERT_HISTORY, t1, t3, "nik", fp, [], "nik_birth_date", {})
        assert await conn.fetchval("SELECT count(*) FROM related_subject_detachments") == 3
        typed = await conn.fetchval(
            "SELECT jsonb_typeof(evidence) FROM related_subject_detachments WHERE to_id = $1", t1
        )
        assert typed == "object"


ALLOWED_ID_TYPES = ("passport", "permit", "nik", "npwp", "nib", "ahu")


@pytest.mark.asyncio
async def test_d2_every_allowed_type_is_accepted_and_a_long_or_non_hex_digest_is_refused(
    pool, conn
):
    s = await seed_subject(conn, "person", "ZzD2 Types", [("passport", passport(4242211))])
    targets = [await seed_subject(conn, "person", None, []) for _ in ALLOWED_ID_TYPES]
    fp = "ab" * 32
    async with unmeasured(conn):
        for bad in ("a" * 65, "g" * 64):
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    INSERT_HISTORY, s, targets[0], "nik", bad, [], "nik_birth_date", {}
                )
        for t, id_type in zip(targets, ALLOWED_ID_TYPES, strict=True):
            await conn.execute(INSERT_HISTORY, s, t, id_type, fp, [], "nik_birth_date", {})
        rows = await conn.fetch("SELECT id_type FROM related_subject_detachments ORDER BY id")
    assert [r["id_type"] for r in rows] == list(ALLOWED_ID_TYPES)


@pytest.mark.asyncio
async def test_d3_deleting_the_new_record_or_the_owner_removes_the_history_row(pool, conn):
    s = await seed_subject(conn, "person", "ZzD3 Owner", [("passport", passport(4242301))])
    t1 = await seed_subject(conn, "person", None, [("nik", nik("150680", 2301))])
    t2 = await seed_subject(conn, "person", None, [("nik", nik("150680", 2302))])
    await seed_detach_history(conn, s, t1, "ab" * 32, [])
    await seed_detach_history(conn, s, t2, "cd" * 32, [])
    async with unmeasured(conn):
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", t1)
        left = await conn.fetch("SELECT from_id, to_id FROM related_subject_detachments")
        assert [(r["from_id"], r["to_id"]) for r in left] == [(s, t2)]
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", s)
        assert await conn.fetchval("SELECT count(*) FROM related_subject_detachments") == 0
        assert await conn.fetchval("SELECT count(*) FROM related_subjects WHERE id = $1", t2) == 1


@pytest.mark.asyncio
async def test_d4_the_rollback_is_the_owners_drop_line_and_drops_only_the_table(pool, conn):
    # RED in the builder's runs (spec 1a): the builder leaves the rollback empty and the owner
    # appends the DROP line by hand.
    forward, rollback = split_migration_sql(MIGRATION_DETACH.read_text())
    assert not re.search(r"\bDROP\b", forward, re.I)
    assert rollback.strip() == "DROP TABLE IF EXISTS related_subject_detachments;"
    assert re.findall(r"DROP\s+(\w+)\s+IF\s+EXISTS\s+(\w+)", rollback) == [("TABLE", TABLE)]
    s = await seed_subject(conn, "person", "ZzD4 Owner", [("passport", passport(4242401))])
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
    assert set(REGISTRY_TABLES) - {TABLE} <= present
    assert await keys_of(conn, s) == [("passport", passport(4242401))]


# ── D13-D16, D18, D32: dry run and apply write what they say ─────────────────


@pytest.mark.asyncio
async def test_d13_a_dry_run_writes_nothing_in_one_read_only_repeatable_read_transaction(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    before = await digests(conn)
    seen = {}
    spy = _Spy()

    async def probe(raw, args):
        seen["isolation"] = await raw.fetchval("SHOW transaction_isolation")
        seen["read_only"] = await raw.fetchval("SHOW transaction_read_only")

    spy.after[(O_SQL, 1)] = probe
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, owner_docs(), dry_run=True), spy)
    assert resp.status_code == 200 and resp.json() == would(s)
    assert await ledger(conn) == [] and await history(conn) == []
    assert await digests(conn) == before
    assert spy.names() == ["O", "KF", "RW", "DC"] and len(spy.calls) <= 4
    assert not any("FOR UPDATE" in q for q in spy.sqls()) and spy.writing() == []
    assert spy.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert seen == {"isolation": "repeatable read", "read_only": "on"}
    # dry_run defaults to true
    data = req(k, d, owner_docs())
    data.pop("dry_run")
    async with make_client(pool) as c:
        omitted = await post(c, data)
    assert omitted.json() == would(s) and await ledger(conn) == []


@pytest.mark.asyncio
async def test_d13_an_apply_runs_in_one_read_committed_read_write_transaction(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    seen = {}
    spy = _Spy()

    async def probe(raw, args):
        seen["isolation"] = await raw.fetchval("SHOW transaction_isolation")
        seen["read_only"] = await raw.fetchval("SHOW transaction_read_only")

    spy.after[(O_SQL, 1)] = probe
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, owner_docs()), spy)
    (h,) = await history(conn)
    assert resp.json() == detached(s, h["to_id"])
    assert spy.transactions == [{}]
    assert seen == {"isolation": "read committed", "read_only": "off"}


@pytest.mark.asyncio
async def test_d14_the_apply_ledger_is_t3_detached_exactly(client, conn, clients):
    s = await seed_owner(conn)
    k, d = owner_keys()
    listed = owner_docs()
    resp = await post(client, req(k, d, listed))
    (h,) = await history(conn)
    t = h["to_id"]
    assert resp.status_code == 200 and resp.json() == detached(s, t)
    log = await ledger(conn)
    assert log[0] == ("related_subjects", "INSERT", None, {"id": t}, None)
    assert log[1] == (
        "related_subject_identifiers",
        "UPDATE",
        {"id_type": "nik", "id_norm": d[1], "subject_id": s},
        {"id_type": "nik", "id_norm": d[1], "subject_id": t},
        ["subject_id"],
    )
    moved = sorted(log[2:4], key=lambda e: e[2]["sha256"])
    assert moved == [
        (
            "related_subject_documents",
            "UPDATE",
            {"subject_id": s, "sha256": sha},
            {"subject_id": t, "sha256": sha},
            ["subject_id"],
        )
        for sha in sorted(listed)
    ]
    assert log[4] == ("related_subject_detachments", "INSERT", None, {"id": h["id"]}, None)
    assert len(log) == 5


@pytest.mark.asyncio
async def test_d15_the_new_record_is_a_bare_person_with_the_t3_evidence(client, conn, clients):
    s = await seed_owner(conn, dob=DOB)
    async with unmeasured(conn):
        await conn.execute(
            "UPDATE related_subjects SET nationality = 'ZZ-land', sex = 'F' WHERE id = $1", s
        )
    k, d = owner_keys()
    resp = await post(client, req(k, d, owner_docs()))
    (h,) = await history(conn)
    t = h["to_id"]
    assert resp.json() == detached(s, t)
    new = await conn.fetchrow(
        "SELECT kind, display_name, date_of_birth, nationality, sex, merged_into, evidence, "
        "jsonb_typeof(evidence) AS ev_kind, created_by FROM related_subjects WHERE id = $1",
        t,
    )
    assert dict(new) == {
        "kind": "person",
        "display_name": None,
        "date_of_birth": None,
        "nationality": None,
        "sex": None,
        "merged_into": None,
        "evidence": {"detached_from": s, "basis": "nik_birth_date", "plan_sha256": PLAN},
        "ev_kind": "object",
        "created_by": REGISTRY_ACTOR,
    }
    assert await keys_of(conn, t) == [d]
    assert await keys_of(conn, s) == [k[0]]
    owner = await conn.fetchrow(
        "SELECT display_name, date_of_birth, nationality, sex, merged_into FROM related_subjects "
        "WHERE id = $1",
        s,
    )
    assert tuple(owner) == ("ZzOwner Detach", DOB, "ZZ-land", "F", None)


@pytest.mark.asyncio
async def test_d16_the_history_row_holds_ids_type_sorted_list_basis_evidence_and_fingerprint(
    client, conn, clients
):
    s = await seed_owner(conn, docs=(1, 2, 3))
    k, d = owner_keys()
    listed = owner_docs(tags=(3, 1, 2))
    resp = await post(client, req(k, d, listed, files=7))
    (h,) = await history(conn)
    assert resp.json() == detached(s, h["to_id"])
    assert (h["from_id"], h["id_type"], h["basis"], h["kind"]) == (
        s,
        "nik",
        "nik_birth_date",
        "object",
    )
    assert list(h["moved_documents"]) == sorted(listed)
    assert h["evidence"] == {"plan_sha256": PLAN, "files": 7}
    assert h["created_by"] == REGISTRY_ACTOR
    literal = f"nik:{d[1]}\npassport:{k[0][1]}".encode()
    assert h["owner_keys_sha256"] == hashlib.sha256(literal).hexdigest()
    assert h["owner_keys_sha256"] != hashlib.sha256(f"passport:{k[0][1]}".encode()).hexdigest()


@pytest.mark.asyncio
async def test_d18_the_same_apply_again_then_a_dry_run_are_already_detached_and_write_nothing(
    client, conn, clients
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    data = req(k, d, owner_docs())
    first = await post(client, data)
    (h,) = await history(conn)
    t = h["to_id"]
    assert first.json() == detached(s, t)
    await forget_writes(conn)
    before = await digests(conn)
    again = await post(client, data)
    assert again.json() == already(s, t)
    dry = await post(client, {**data, "dry_run": True})
    assert dry.json() == already(s, t, dry_run=True)
    assert await ledger(conn) == [] and await digests(conn) == before
    assert await subject_count(conn) == 2


@pytest.mark.asyncio
async def test_d18_a_repeat_of_the_same_list_in_another_order_is_already_detached(
    client, conn, clients
):
    # W(d) compares the SORTED list: the order a request sends is not part of the request
    s = await seed_owner(conn)
    k, d = owner_keys()
    listed = owner_docs()
    first = await post(client, req(k, d, sorted(listed)))
    (h,) = await history(conn)
    t = h["to_id"]
    assert first.json() == detached(s, t)
    other_order = sorted(listed, reverse=True)
    assert other_order != list(h["moved_documents"])
    # an apply sent unsorted, repeated sorted
    s2 = await seed_owner(conn, 4242011, docs=(1, 2, 3))
    k2, d2 = owner_keys(4242011)
    unsorted = sorted(owner_docs(4242011, (1, 2, 3)), reverse=True)
    second = await post(client, req(k2, d2, unsorted))
    t2 = second.json()["to_id"]
    assert second.json() == detached(s2, t2)
    await forget_writes(conn)
    for dry_run in (True, False):
        again = await post(client, req(k, d, other_order, dry_run=dry_run))
        assert again.json() == already(s, t, dry_run), dry_run
        again = await post(client, req(k2, d2, sorted(unsorted), dry_run=dry_run))
        assert again.json() == already(s2, t2, dry_run), dry_run
    assert await ledger(conn) == []


@pytest.mark.asyncio
async def test_d19_other_documents_two_owners_and_a_first_request_after_a_second_detachment(
    client, conn, clients
):
    # W': the same key set and key, another list
    s = await seed_owner(conn, docs=(1, 2))
    k, d = owner_keys()
    assert (await post(client, req(k, d, owner_docs(tags=(1,))))).json()["outcome"] == "detached"
    (h,) = await history(conn)
    t = h["to_id"]
    for dry_run in (True, False):
        other = await post(client, req(k, d, owner_docs(tags=(1, 2)), dry_run=dry_run))
        assert other.json() == refused("detached_with_other_documents", s, t)
        empty = await post(client, req(k, d, [], dry_run=dry_run))
        assert empty.json() == refused("detached_with_other_documents", s, t)

    # two owners: d's owner has no history row
    k2, d2 = owner_keys(4242902)
    p2 = await seed_subject(conn, "person", "ZzD19 Two", [k2[0]], dob=DOB)
    await seed_subject(conn, "person", None, [d2])
    for dry_run in (True, False):
        resp = await post(client, req(k2, d2, dry_run=dry_run))
        assert resp.json() == not_registered("two_owners")
    # two owners: a history row whose fingerprint is of another key set (K never sent)
    k3, d3 = owner_keys(4242903)
    p3 = await seed_subject(conn, "person", "ZzD19 Three", [k3[0]], dob=DOB)
    t3 = await seed_subject(conn, "person", None, [d3])
    await seed_detach_history(conn, p3, t3, fingerprint(k3[0], d3, ("permit", permit(4242903))), [])
    for dry_run in (True, False):
        resp = await post(client, req(k3, d3, dry_run=dry_run))
        assert resp.json() == not_registered("two_owners")
    assert p2 != p3

    # two keys of one owner detached in turn, then the FIRST request again
    n1, n2 = nik("150680", 2904), nik("160781", 2904)
    first_keys = (("passport", passport(4242904)), ("nik", n1), ("nik", n2))
    o = await seed_subject(conn, "person", "ZzD19 Four", first_keys, dob=DOB)
    first = req(first_keys, ("nik", n1))
    r1 = await post(client, first)
    t1 = r1.json()["to_id"]
    assert r1.json() == detached(o, t1)
    r2 = await post(client, req((first_keys[0], ("nik", n2)), ("nik", n2)))
    assert r2.json() == detached(o, r2.json()["to_id"])
    await forget_writes(conn)
    for dry_run in (True, False):
        assert (await post(client, {**first, "dry_run": dry_run})).json() == already(o, t1, dry_run)
    assert await ledger(conn) == []


@pytest.mark.asyncio
async def test_d32_an_empty_list_moves_the_key_alone_and_the_undo_restores_the_before_digest(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn
):
    s = await seed_owner(conn, docs=(1, 2))
    k, d = owner_keys()
    before = await digests(conn)
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, []))
    (h,) = await history(conn)
    t = h["to_id"]
    assert resp.json() == detached(s, t)
    assert spy.names() == ["O", "L", "O", "KF", "DC", "IS", "MK", "MD", "IH"]
    assert spy.args_of(MD_SQL) == [(s, t, [])]
    assert list(h["moved_documents"]) == []
    assert [e[:2] for e in await ledger(conn)] == [
        ("related_subjects", "INSERT"),
        ("related_subject_identifiers", "UPDATE"),
        ("related_subject_detachments", "INSERT"),
    ]
    assert await docs_of(conn, s) == sorted(owner_docs()) and await docs_of(conn, t) == []
    await forget_writes(conn)
    assert (await post_plain(pool, req(k, d, []))).json() == already(s, t)
    async with unmeasured(conn):
        await conn.execute(undo_block(s, t))
    assert await digests(conn) == before


async def post_plain(pool, data):
    async with make_client(pool) as c:
        return await post(c, data)


# ── D34: the fingerprint ──────────────────────────────────────────────────────


def test_d34_the_fingerprint_is_the_hex_of_the_literal_byte_string_in_either_order():
    a, b = ("passport", "ZZ4242001"), ("nik", "9900421506804242")
    literal = b"nik:9900421506804242\npassport:ZZ4242001"
    expected = hashlib.sha256(literal).hexdigest()
    assert keys_sha256([a, b]) == keys_sha256([b, a]) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)
    assert (
        keys_sha256([a, b])
        != hashlib.sha256(b"nik:9900421506804242,passport:ZZ4242001").hexdigest()
    )
    assert keys_sha256([a, b]) != hashlib.sha256(literal + b"\n").hexdigest()
    # code-point order: an upper-case letter sorts before a lower-case one
    assert (
        keys_sha256([("passport", "ZZ1"), ("nik", "1")])
        == hashlib.sha256(b"nik:1\npassport:ZZ1").hexdigest()
    )


@pytest.mark.asyncio
async def test_d34_the_sql_form_of_section_2_1_gives_the_same_hex(conn):
    pairs = [("passport", "ZZ4242001"), ("nik", "9900421506804242"), ("permit", "Z99ZZ00004242001")]
    sql = (
        "SELECT encode(sha256(convert_to(string_agg(id_type || ':' || id_norm, E'\\n' "
        "ORDER BY id_type || ':' || id_norm COLLATE \"C\"), 'UTF8')), 'hex') "
        "FROM unnest($1::text[], $2::text[]) AS v(id_type, id_norm)"
    )
    for chosen in (pairs[:2], pairs[1:], pairs, list(reversed(pairs))):
        got = await conn.fetchval(sql, [p[0] for p in chosen], [p[1] for p in chosen])
        assert got == keys_sha256(chosen), chosen


# ── D20: the statements of every T3 row ───────────────────────────────────────


async def t3_world(conn):
    """One seeded state per T3 row reachable without a concurrent change."""
    w: dict = {}
    w["s"] = await seed_owner(conn, 4243001)
    w["k"], w["d"] = owner_keys(4243001)
    # R2 with (a): d on its own record, no history row
    k2, d2 = owner_keys(4243002)
    await seed_subject(conn, "person", "ZzT3 Two", [k2[0]], dob=DOB)
    await seed_subject(conn, "person", None, [d2])
    w["r2_a"] = (k2, d2)
    # R2 without (a): the passport and a second passport on two records, d owned by nobody
    k3, d3 = owner_keys(4243003)
    other = ("passport", passport(4243903))
    await seed_subject(conn, "person", "ZzT3 Three", [k3[0]], dob=DOB)
    await seed_subject(conn, "person", "ZzT3 Three B", [other], dob=DOB)
    w["r2_no_a"] = ((k3[0], other, d3), d3)
    # R3: the owner holds one key more
    k4, d4 = owner_keys(4243004)
    await seed_subject(conn, "person", "ZzT3 Four", [*k4, ("permit", permit(4243004))], dob=DOB)
    w["r3"] = (k4, d4)
    # R5: a company record holding person keys
    k5, d5 = owner_keys(4243005)
    await seed_subject(conn, "company", None, k5)
    w["r5"] = (k5, d5)
    # N8: a listed hash not on the owner
    w["n8_doc"] = doc(4243999)
    # N1, N3-N7: one owner each, refused before DC
    y = await seed_subject(conn, "person", "ZzT3 Survivor", [("passport", passport(4243906))])
    k6, d6 = owner_keys(4243006)
    w["n1"] = (k6, d6, await seed_subject(conn, "person", None, k6, dob=DOB, merged=y))
    k7 = (("permit", permit(4243007)), ("nik", nik("150680", 3007)))
    w["n3"] = (k7, k7[1], await seed_subject(conn, "person", None, k7, dob=DOB))
    k8, _ = owner_keys(4243008)
    w["n4"] = (k8, k8[0], await seed_subject(conn, "person", None, k8, dob=DOB))
    k9, d9 = owner_keys(4243009)
    w["n5"] = (k9, d9, await seed_subject(conn, "person", None, k9, dob=None))
    k10, d10 = owner_keys(4243010, "000377")
    w["n6"] = (k10, d10, await seed_subject(conn, "person", None, k10, dob=DOB))
    k11, d11 = owner_keys(4243011, "120377")
    w["n7"] = (k11, d11, await seed_subject(conn, "person", None, k11, dob=DOB))
    # W' at the candidate read: d on a record a history row names, another list
    k12, d12 = owner_keys(4243012)
    s12 = await seed_subject(conn, "person", "ZzT3 Twelve", [k12[0]], dob=DOB)
    t12 = await seed_subject(conn, "person", None, [d12])
    await seed_document(conn, t12, 4243912)
    await seed_detach_history(conn, s12, t12, fingerprint(*k12), [doc(4243912)])
    w["w_prime"] = (k12, d12, s12, t12)
    return w


def _refusal_row(name, reason):
    """A T3 row refused by the judge before DC: dry O, KF, RW; apply O, L, O, KF."""
    return (
        lambda w: req(w[name][0], w[name][1]),
        lambda w: refused(reason, w[name][2]),
        ["O", "KF", "RW"],
        ["O", "L", "O", "KF"],
    )


T3_ROWS = {
    # name: (request builder, expected builder, dry-run statements, apply statements)
    "r1": (
        lambda w: req(
            (("passport", passport(4243901)), ("nik", nik("150680", 3901))),
            ("nik", nik("150680", 3901)),
        ),
        lambda w: not_registered("none_owned"),
        ["O"],
        ["O"],
    ),
    "r2_with_a": (
        lambda w: req(*w["r2_a"]),
        lambda w: not_registered("two_owners"),
        ["O", "WH"],
        ["O", "WH"],
    ),
    "r2_without_a": (
        lambda w: req(*w["r2_no_a"]),
        lambda w: not_registered("two_owners"),
        ["O"],
        ["O"],
    ),
    "r3": (
        lambda w: req(*w["r3"]),
        lambda w: not_registered("set_differs"),
        ["O", "KF"],
        ["O", "L", "O", "KF"],
    ),
    "r5": (
        lambda w: req(*w["r5"]),
        lambda w: not_registered("kind_differs"),
        ["O", "KF", "RW"],
        ["O", "L", "O", "KF"],
    ),
    "n8": (
        lambda w: req(w["k"], w["d"], [w["n8_doc"]]),
        lambda w: refused("document_not_on_owner", w["s"]),
        ["O", "KF", "RW", "DC"],
        ["O", "L", "O", "KF", "DC"],
    ),
    "n1": _refusal_row("n1", "owner_is_merged"),
    "n3": _refusal_row("n3", "owner_left_permit_only"),
    "n4": _refusal_row("n4", "identifier_not_nik"),
    "n5": _refusal_row("n5", "owner_has_no_birth_date"),
    "n6": _refusal_row("n6", "nik_date_invalid"),
    "n7": _refusal_row("n7", "nik_date_matches"),
    "w_prime_at_the_candidate_read": (
        lambda w: req(w["w_prime"][0], w["w_prime"][1], []),
        lambda w: refused("detached_with_other_documents", w["w_prime"][2], w["w_prime"][3]),
        ["O", "WH"],
        ["O", "WH"],
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
@pytest.mark.parametrize("name", sorted(T3_ROWS))
async def test_d20_each_t3_row_runs_its_statements_in_order(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn, name, dry_run
):
    w = await t3_world(conn)
    build, expected, dry_names, apply_names = T3_ROWS[name]
    resp, spy = await run_spied(pool, monkeypatch, {**build(w), "dry_run": dry_run})
    assert resp.status_code == 200 and resp.json() == expected(w), resp.text
    assert spy.names() == (dry_names if dry_run else apply_names)
    assert spy.writing() == [] and await ledger(conn) == []
    assert not any(q in {"BEGIN", "COMMIT", "ROLLBACK"} for q in spy.sqls())


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d20_detached_and_already_detached_run_their_statements_in_order(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn, dry_run
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, owner_docs(), dry_run=dry_run))
    if dry_run:
        assert resp.json() == would(s)
        assert spy.names() == ["O", "KF", "RW", "DC"]
        return
    (h,) = await history(conn)
    t = h["to_id"]
    assert resp.json() == detached(s, t)
    assert spy.names() == ["O", "L", "O", "KF", "DC", "IS", "MK", "MD", "IH"]
    assert spy.writing() == [IS_SQL, MK_SQL, MD_SQL, IH_SQL]
    assert spy.args_of(L_SQL) == [(s,)] and spy.args_of(KF_SQL) == [(s,)]
    assert spy.args_of(MK_SQL) == [("nik", d[1], s, t)]
    assert spy.args_of(MD_SQL) == [(s, t, sorted(owner_docs()))]
    assert not any(q in {"BEGIN", "COMMIT", "ROLLBACK"} for q in spy.sqls())
    # the same request again: the W path at the candidate read
    again, spy2 = await run_spied(pool, monkeypatch, req(k, d, owner_docs()))
    assert again.json() == already(s, t)
    assert spy2.names() == ["O", "WH"] and spy2.args_of(WH_SQL) == [(t,)]


# ── D5-D7: validation, exact 422 bodies, precedence ───────────────────────────

# The probe world of D5 and D30: every identifier carries at least six digits; every national
# number is of the invented region 990042.
PO, NO = passport(7474001), nik("150680", 7401)  # the owner: a passport, another person's number
H1, H2, H3, H4 = doc(7474001), doc(7474002), doc(7474003), doc(7474004)
K_OWN = (("passport", PO), ("nik", NO))
D_OWN = ("nik", NO)
PB = passport(7474002)  # the bystander: links H1, in no request
PNONE, NNONE = passport(7474003), nik("150680", 7403)  # owned by nobody
P2A, N2B = passport(7474004), nik("150680", 7404)  # R2: on two records
P3, N3, Q3 = passport(7474005), nik("150680", 7405), permit(7474005)  # R3: one key more
P4, N4, Q4 = passport(7474006), nik("150680", 7406), permit(7474006)  # R4 (K adds PNONE)
PCO, NCO = passport(7474007), nik("150680", 7407)  # R5: a company record
PMG, NMG = passport(7474008), nik("150680", 7408)  # N1: merged into the bystander
QPL, NPL = permit(7474009), nik("150680", 7409)  # N3: a permit is all that is left
NNN, PNN = nik("150680", 7410), passport(7474010)  # N4: the passport is the key sent
PND, NND = passport(7474011), nik("150680", 7411)  # N5: no stored birth date
PIV, NIV = passport(7474012), nik("000377", 7412)  # N6: day 0
PMT, NMT = passport(7474013), nik("120377", 7413)  # N7: the owner's own date
PWF, NWT = passport(7474014), nik("150680", 7414)  # W': detached before with H4
NX = nik("150680", 7430)  # a key of no owner, sent as the key to detach in B2
K_W = (("passport", PWF), ("nik", NWT))

BASE = req(K_OWN, D_OWN, [H1, H2])


def _mut(fn):
    def apply(base):
        data = copy.deepcopy(base)
        fn(data)
        return data

    return apply


def _four(first_three, fourth):
    return [*keys(first_three), fourth]


EXTRA3 = (
    ("passport", passport(7474021)),
    ("passport", passport(7474022)),
    ("permit", permit(7474023)),
)
ZZ_HOSTILE = "ZzHostile<script>"
FORTY_ONE = [doc(7474200 + i) for i in range(41)]
UPPER_FORTY = [doc(7474300 + i).upper() for i in range(40)]
FIFTY = [(["owner", "identifiers", i], "invalid_identifier") for i in range(20)] + [
    (["documents", i], "string_pattern_mismatch") for i in range(30)
]

D_CASES = {
    "owner_missing": (_mut(lambda d: d.pop("owner")), [(["owner"], "missing")]),
    "detach_missing": (_mut(lambda d: d.pop("detach")), [(["detach"], "missing")]),
    "documents_missing": (_mut(lambda d: d.pop("documents")), [(["documents"], "missing")]),
    "basis_missing": (_mut(lambda d: d.pop("basis")), [(["basis"], "missing")]),
    "evidence_missing": (_mut(lambda d: d.pop("evidence")), [(["evidence"], "missing")]),
    "owner_kind_unknown": (
        _mut(lambda d: d["owner"].update(kind="robot")),
        [(["owner", "kind"], "literal_error")],
    ),
    "owner_kind_missing": (
        _mut(lambda d: d["owner"].pop("kind")),
        [(["owner", "kind"], "missing")],
    ),
    "owner_kind_type_mismatch": (
        _mut(lambda d: d["owner"].update(kind="company")),
        [(["owner"], "kind_type_mismatch")],
    ),
    "owner_value_not_a_string_at_index_3": (
        _mut(lambda d: d["owner"].update(identifiers=_four(EXTRA3, {"type": "nik", "value": 123}))),
        [(["owner", "identifiers", 3, "value"], "string_type")],
    ),
    "owner_type_unknown_at_index_3": (
        _mut(
            lambda d: d["owner"].update(
                identifiers=_four(EXTRA3, {"type": "zzbogus", "value": "x" * 8})
            )
        ),
        [(["owner", "identifiers", 3, "type"], "literal_error")],
    ),
    "owner_identifier_invalid": (
        _mut(lambda d: d["owner"]["identifiers"][0].update(value="!")),
        [(["owner", "identifiers", 0], "invalid_identifier")],
    ),
    "owner_identifiers_empty": (
        _mut(lambda d: d["owner"].update(identifiers=[])),
        [(["owner", "identifiers"], "too_short")],
    ),
    "owner_twenty_one_identifiers": (
        _mut(
            lambda d: d["owner"].update(
                identifiers=keys([("passport", passport(7474800 + i)) for i in range(21)])
            )
        ),
        [(["owner", "identifiers"], "too_long")],
    ),
    "owner_duplicate_identifier": (
        _mut(lambda d: d["owner"]["identifiers"].append(d["owner"]["identifiers"][0])),
        [(["owner"], "duplicate_identifier")],
    ),
    "owner_unknown_field": (
        _mut(lambda d: d["owner"].update(zzextra="zz")),
        [(["owner", "_"], "extra_forbidden")],
    ),
    "detach_type_unknown": (
        _mut(lambda d: d["detach"].update(type="zzbogus")),
        [(["detach", "type"], "literal_error")],
    ),
    "detach_value_not_a_string": (
        _mut(lambda d: d["detach"].update(value=123)),
        [(["detach", "value"], "string_type")],
    ),
    "detach_value_missing": (
        _mut(lambda d: d["detach"].pop("value")),
        [(["detach", "value"], "missing")],
    ),
    "detach_fifteen_digits": (
        _mut(lambda d: d["detach"].update(value=NO[:15])),
        [(["detach"], "invalid_identifier")],
    ),
    "detach_seventeen_digits": (
        _mut(lambda d: d["detach"].update(value=NO + "1")),
        [(["detach"], "invalid_identifier")],
    ),
    "detach_embedded_newline": (
        _mut(lambda d: d["detach"].update(value=NO[:8] + "\n" + NO[8:])),
        [(["detach"], "invalid_identifier")],
    ),
    "detach_unknown_field": (
        _mut(lambda d: d["detach"].update(zzextra="zz")),
        [(["detach", "_"], "extra_forbidden")],
    ),
    "detach_is_not_an_object": (
        _mut(lambda d: d.update(detach="zz")),
        [(["detach"], "model_type")],
    ),
    "b1_duplicate_document": (
        _mut(lambda d: d.update(documents=[H1, H1])),
        [([], "duplicate_document")],
    ),
    "b2_detach_not_in_owner": (
        _mut(lambda d: d["detach"].update(value=NX)),
        [([], "detach_not_in_owner")],
    ),
    "b3_owner_keeps_no_identifier": (
        _mut(lambda d: d["owner"].update(identifiers=keys([D_OWN]))),
        [([], "owner_keeps_no_identifier")],
    ),
    "b1_before_b2": (
        _mut(lambda d: (d.update(documents=[H1, H1]), d["detach"].update(value=NX))),
        [([], "duplicate_document")],
    ),
    "forty_one_documents": (
        _mut(lambda d: d.update(documents=list(FORTY_ONE))),
        [(["documents"], "too_long")],
    ),
    "document_in_capitals": (
        _mut(lambda d: d.update(documents=[H1, H2.upper()])),
        [(["documents", 1], "string_pattern_mismatch")],
    ),
    "document_short": (
        _mut(lambda d: d.update(documents=[H1[:63], H2])),
        [(["documents", 0], "string_pattern_mismatch")],
    ),
    "document_not_a_string": (
        _mut(lambda d: d.update(documents=[7, H2])),
        [(["documents", 0], "string_type")],
    ),
    "documents_not_a_list": (
        _mut(lambda d: d.update(documents="zz")),
        [(["documents"], "list_type")],
    ),
    "unknown_field": (_mut(lambda d: d.update(zzextra=1)), [(["_"], "extra_forbidden")]),
    "from_id_is_not_a_field": (
        _mut(lambda d: d.update(from_id=424242)),
        [(["_"], "extra_forbidden")],
    ),
    "to_id_is_not_a_field": (
        _mut(lambda d: d.update(to_id=424243)),
        [(["_"], "extra_forbidden")],
    ),
    "subject_id_is_not_a_field": (
        _mut(lambda d: d.update(subject_id=424244)),
        [(["_"], "extra_forbidden")],
    ),
    "hostile_field_name": (
        _mut(lambda d: d["evidence"].update({ZZ_HOSTILE: 1})),
        [(["evidence", "_"], "extra_forbidden")],
    ),
    "basis_manual": (_mut(lambda d: d.update(basis="manual")), [(["basis"], "literal_error")]),
    "basis_permit_stamp": (
        _mut(lambda d: d.update(basis="permit_stamp")),
        [(["basis"], "literal_error")],
    ),
    "basis_unknown": (_mut(lambda d: d.update(basis="guessed")), [(["basis"], "literal_error")]),
    "basis_not_a_string": (_mut(lambda d: d.update(basis=1)), [(["basis"], "literal_error")]),
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
    "fifty_errors_capped": (
        _mut(
            lambda d: d.update(
                owner={
                    "kind": "person",
                    "identifiers": [{"type": "passport", "value": "!"}] * 20,
                },
                documents=list(UPPER_FORTY),
            )
        ),
        FIFTY,
    ),
}


def _validation(loc_codes):
    return {"detail": [{"loc": loc, "code": code} for loc, code in loc_codes]}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(D_CASES))
async def test_d5_exact_422_bodies(client, conn, clients, case):
    await seed_subject(conn, "person", "ZzD5 Owner", K_OWN, dob=DOB)
    mutate, expected = D_CASES[case]
    resp = await post(client, mutate(BASE))
    assert resp.status_code == 422
    assert resp.json() == _validation(expected)
    assert await history(conn) == [] and await ledger(conn) == []


@pytest.mark.asyncio
async def test_d5_the_bounds_and_permitted_raw_forms_are_accepted(client, conn, clients):
    s = await seed_subject(conn, "person", "ZzD5 Owner", K_OWN, dob=DOB)
    for files in (1, 10000):
        resp = await post(client, req(K_OWN, D_OWN, dry_run=True, files=files))
        assert resp.status_code == 200 and resp.json() == would(s), files
    # outer white space and printable separators, a colon among them, canonicalise to the key
    for raw in (f"  {NO}  ", f"NIK:{NO[:4]}-{NO[4:8]}.{NO[8:12]} {NO[12:]}"):
        data = req(K_OWN, ("nik", raw), dry_run=True)
        resp = await post(client, data)
        assert resp.status_code == 200 and resp.json() == would(s), raw
    # the empty list is a list
    assert (await post(client, req(K_OWN, D_OWN, [], dry_run=True))).json() == would(s)


MALFORMED_CODES = {
    b"{not json": "json_invalid",
    b"": "json_invalid",
    b"[1, 2]": "model_type",
    b"null": "model_type",
    b" " * route_module.MAX_BODY_BYTES: "json_invalid",
}


@pytest.mark.parametrize(
    "raw", list(MALFORMED_CODES), ids=["text", "empty", "list", "null", "limit"]
)
def test_d5_each_malformed_raw_body_has_its_exact_sanitised_item(raw):
    assert _model_errors(raw) == [{"loc": [], "code": MALFORMED_CODES[raw]}]


@pytest.mark.asyncio
async def test_d5_malformed_json_and_oversize_body(client, conn, clients):
    for raw in (b"{not json", b"", b"[1, 2]", b"null"):
        resp = await post(client, raw)
        assert resp.status_code == 422, raw
        body = resp.json()["detail"]
        assert (
            len(body) == 1
            and body[0]["loc"] == []
            and body[0]["code"] in {"json_invalid", "model_type"}
        )
        assert resp.json() == _validation([([], MALFORMED_CODES[raw])]), raw
    big = await post(client, b" " * (route_module.MAX_BODY_BYTES + 1))
    assert big.status_code == 422
    assert big.json() == {"detail": [{"loc": ["body"], "code": "too_long"}]}
    exact = await post(client, b" " * route_module.MAX_BODY_BYTES)
    assert exact.json()["detail"][0]["loc"] == []  # at the limit the body is parsed, not refused
    assert exact.json() == _validation([([], "json_invalid")])


def _sanitised(errors):
    resp = route_module._invalid_detach(errors)
    assert resp.status_code == 422
    return json.loads(resp.body)["detail"]


def _model_errors(raw):
    """The sanitised 422 items of a raw body, through the route's own strict model."""
    with pytest.raises(ValidationError) as raised:
        route_module.RelatedDetachRequest.model_validate_json(raw)
    return _sanitised(
        raised.value.errors(include_input=False, include_context=False, include_url=False)
    )


@pytest.mark.parametrize(
    "owner,detach,documents,code",
    [
        (K_OWN, ("nik", NX), [H1, H1], "duplicate_document"),  # B1 before B2
        ((D_OWN,), D_OWN, [H1, H1], "duplicate_document"),  # B1 before B3
        ((D_OWN,), ("nik", NX), [], "detach_not_in_owner"),  # B2 before B3
    ],
    ids=["b1_before_b2", "b1_before_b3", "b2_before_b3"],
)
def test_d5_on_a_body_holding_two_b_errors_the_earlier_one_answers(owner, detach, documents, code):
    raw = json.dumps(req(owner, detach, documents))
    assert _model_errors(raw) == [{"loc": [], "code": code}]


def test_d5_the_sanitiser_keeps_closed_names_indexes_below_their_limits_and_codes():
    # index 40 of `documents` cannot be reached through a request (pydantic reports a list above
    # its maximum as one `too_long`, without item errors), so it is proved on the sanitiser
    out = _sanitised(
        [
            {"loc": ("documents", 39), "type": "string_pattern_mismatch"},
            {"loc": ("documents", 40), "type": "string_pattern_mismatch"},
            {"loc": ("owner", "identifiers", 19, "type"), "type": "literal_error"},
            {"loc": ("owner", "identifiers", 20, "type"), "type": "literal_error"},
            {"loc": ("documents", True), "type": "string_type"},
            {"loc": ("documents", -1), "type": "string_type"},
            {"loc": ("detach", 3), "type": "literal_error"},
            {"loc": ("body", 0), "type": "too_long"},
            {"loc": ("evidence", "ZzHostile"), "type": "extra_forbidden"},
            {"loc": ("from_id",), "type": "extra_forbidden"},
            {"loc": ("evidence", "files"), "type": "weird_code"},
            {"loc": (), "type": "duplicate_document"},
            {"loc": (), "type": "detach_not_in_owner"},
            {"loc": (), "type": "owner_keeps_no_identifier"},
            {"loc": ("owner",), "type": "value_error"},
            {"loc": ("basis",), "type": "duplicate_name"},
            {"loc": ("documents",), "type": "duplicate_file_id"},
        ]
    )
    assert out == [
        {"loc": ["documents", 39], "code": "string_pattern_mismatch"},
        {"loc": ["documents", "_"], "code": "string_pattern_mismatch"},
        {"loc": ["owner", "identifiers", 19, "type"], "code": "literal_error"},
        {"loc": ["owner", "identifiers", "_", "type"], "code": "literal_error"},
        {"loc": ["documents", "_"], "code": "string_type"},
        {"loc": ["documents", "_"], "code": "string_type"},
        {"loc": ["detach", "_"], "code": "literal_error"},
        {"loc": ["body", "_"], "code": "too_long"},
        {"loc": ["evidence", "_"], "code": "extra_forbidden"},
        {"loc": ["_"], "code": "extra_forbidden"},
        {"loc": ["evidence", "files"], "code": "invalid"},
        {"loc": [], "code": "duplicate_document"},
        {"loc": [], "code": "detach_not_in_owner"},
        {"loc": [], "code": "owner_keeps_no_identifier"},
        {"loc": ["owner"], "code": "invalid"},
        {"loc": ["basis"], "code": "invalid"},
        {"loc": ["documents"], "code": "invalid"},
    ]


def test_d5_the_sanitiser_caps_the_list_at_fifty_errors():
    assert len(_sanitised([{"loc": ("evidence", "files"), "type": "int_type"}] * 60)) == 50


def test_d6_the_closed_lists_and_index_limits_are_the_specified_ones():
    assert route_module._DETACH_LOC == {
        "body", "owner", "detach", "kind", "identifiers", "type", "value", "documents",
        "basis", "evidence", "plan_sha256", "files", "dry_run",
    }  # fmt: skip
    assert len(route_module._MERGES_CODE) == 20
    assert route_module._DETACH_CODE == route_module._MERGES_CODE | {
        "duplicate_document",
        "detach_not_in_owner",
        "owner_keeps_no_identifier",
    }
    assert len(route_module._DETACH_CODE) == 23
    assert route_module._DETACH_INDEX_LIMIT == {"identifiers": 20, "documents": 40}
    assert (route_module.MAX_KEYS, route_module.MAX_DOCUMENTS) == (20, 40)
    # the lists of the five older routes are not widened
    for name in ("_MERGES_LOC", "_LINKS_LOC", "_NAMES_LOC", "_DOCUMENTS_LOC", "_REPORTABLE_LOC"):
        assert "owner" not in getattr(route_module, name) and "detach" not in getattr(
            route_module, name
        ), name
    for name in ("_MERGES_CODE", "_LINKS_CODE", "_NAMES_CODE", "_REPORTABLE_CODE"):
        assert "detach_not_in_owner" not in getattr(route_module, name), name


BAD_BODIES = [b"{not json", json.dumps(req(K_OWN, D_OWN, files=0)).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_d7_the_flag_is_checked_before_the_body(pool, auth_on, monkeypatch, raw):
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
async def test_d7_the_key_is_checked_before_the_body(pool, enabled, auth_on, monkeypatch, raw, key):
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
async def test_d7_no_pool_is_the_existing_503_before_the_body(enabled, auth_on, monkeypatch, raw):
    as_identity(monkeypatch, None)
    async with make_client(None) as c:
        resp = await post(c, raw)
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_d7_with_every_gate_open_an_invalid_body_is_422_and_runs_no_statement(
    pool, enabled, auth_on, audit_logger, monkeypatch, raw
):
    spy_pool, spy_app = spy_client(pool, monkeypatch)
    async with spy_app as c:
        resp = await post(c, raw)
    assert resp.status_code == 422
    assert spy_pool.acquired == 0 and spy_pool.calls == []


def test_d7_the_body_is_read_raw_not_declared_as_a_fastapi_body_parameter():
    params = inspect.signature(route_module.detach_related_subject_identifier).parameters
    assert set(params) == {"request", "_actor", "db_pool"}
    handler = inspect.getsource(route_module.detach_related_subject_identifier)
    assert "model_validate_json(raw)" in handler and "request.body()" in handler


# ── D26: registration, public entry, the older routes ─────────────────────────

SEVEN_PATHS = {
    ("/api/crm/related-subjects/register", ("POST",)),
    ("/api/crm/related-subjects/documents/register", ("POST",)),
    ("/api/crm/related-subjects/names/register", ("POST",)),
    ("/api/crm/related-subjects/links/register", ("POST",)),
    ("/api/crm/related-subjects/merges/register", ("POST",)),
    ("/api/crm/related-subjects/identifiers/detach", ("POST",)),
    ("/api/crm/related-subjects/birth-date/register", ("POST",)),
}


def test_d26_the_route_is_the_sixth_of_the_sealed_router():
    assert len([e for e in ROUTER_MANIFEST if e.name == "crm_related_subjects"]) == 1
    paths = {(r.path, tuple(sorted(r.methods))) for r in route_module.router.routes}
    assert paths == SEVEN_PATHS
    assert route_module.router.route_class is route_module._SealedRoute


def test_d26_public_entry_is_exact_path_and_post_only():
    entries = [e for e in PUBLIC_ENDPOINTS if e.prefix == URL]
    assert len(entries) == 1
    assert entries[0].match == "exact" and tuple(entries[0].methods) == ("POST",)
    assert any(e.matches(URL, "POST") for e in PUBLIC_ENDPOINTS)
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert not any(e.matches(URL, method) for e in PUBLIC_ENDPOINTS), method
    for path in (
        URL + "/",
        URL + "/x",
        "/api/crm/related-subjects/identifiers",
        "/api/crm/related-subjects",
    ):
        assert not any(e.matches(path, "POST") for e in PUBLIC_ENDPOINTS), path
    for path in (READ, f"{READ}/7"):
        assert not any(e.matches(path, "GET") for e in PUBLIC_ENDPOINTS), path
    older = {
        URL_SUBJECT: None,
        URL_DOCUMENTS: ("POST",),
        URL_NAMES: ("POST",),
        URL_LINKS: ("POST",),
        URL_MERGES: ("POST",),
    }
    for path, methods in older.items():
        matching = [e for e in PUBLIC_ENDPOINTS if e.prefix == path]
        assert len(matching) == 1 and matching[0].match == "exact", path
        assert (None if matching[0].methods is None else tuple(matching[0].methods)) == methods


@pytest.mark.asyncio
async def test_d26_get_on_the_path_and_the_read_routes_stay_protected(client, conn):
    sid = await seed_subject(conn, "person", "ZzD26 Person", [("passport", passport(4247001))])
    for url in (URL, READ, f"{READ}/{sid}"):
        resp = await client.get(url)
        assert resp.status_code == 401, url
        assert "ZzD26" not in resp.text


@pytest.mark.asyncio
async def test_d26_the_older_write_routes_answer_their_empty_body_422_as_before(client, conn):
    expected = {
        URL_SUBJECT: [("kind", "missing"), ("identifiers", "missing"), ("evidence", "missing")],
        URL_DOCUMENTS: [("subject", "missing"), ("documents", "missing"), ("evidence", "missing")],
        URL_NAMES: [("subject", "missing"), ("names", "missing")],
        URL_LINKS: [("subject", "missing"), ("links", "missing")],
        URL_MERGES: [
            ("source", "missing"),
            ("target", "missing"),
            ("basis", "missing"),
            ("evidence", "missing"),
        ],
        URL: [
            ("owner", "missing"),
            ("detach", "missing"),
            ("documents", "missing"),
            ("basis", "missing"),
            ("evidence", "missing"),
        ],
    }
    for url, pairs in expected.items():
        resp = await post(client, {}, url=url)
        assert resp.status_code == 422, url
        assert resp.json() == {"detail": [{"loc": [loc], "code": code} for loc, code in pairs]}
    # a field name of this slice is still "_" on the older routes
    for url in (URL_SUBJECT, URL_MERGES):
        older = await post(client, {"owner": {}, "detach": {}}, url=url)
        assert {"loc": ["_"], "code": "extra_forbidden"} in older.json()["detail"], url
        assert not any("owner" in item["loc"] for item in older.json()["detail"]), url


# ── D27-D28: nothing is written outside the four statements ───────────────────

EXPECTED_SQL_NAMES = {
    "_OWNERS_SQL",
    "_OWNER_KEYS_SQL",
    "_INSERT_SUBJECT_SQL",
    "_ROW_SQL",
    "_LOCK_SQL",
    "_WITNESS_SQL",
    "_OWNED_DOCS_SQL",
    "_MOVE_KEY_SQL",
    "_MOVE_DOCS_SQL",
    "_INSERT_DETACH_SQL",
}
T3_TEXTS = {
    "_ROW_SQL": "SELECT id, kind, merged_into, date_of_birth FROM related_subjects WHERE id = $1",
    "_LOCK_SQL": (
        "SELECT id, kind, merged_into, date_of_birth FROM related_subjects WHERE id = $1 FOR UPDATE"
    ),
    "_WITNESS_SQL": (
        "SELECT from_id, owner_keys_sha256, moved_documents FROM related_subject_detachments "
        "WHERE to_id = $1"
    ),
    "_OWNED_DOCS_SQL": (
        "SELECT sha256 FROM related_subject_documents WHERE subject_id = $1 "
        "AND sha256 = ANY($2::text[])"
    ),
    "_MOVE_KEY_SQL": (
        "UPDATE related_subject_identifiers SET subject_id = $4 "
        "WHERE id_type = $1 AND id_norm = $2 AND subject_id = $3 RETURNING id_type"
    ),
    "_MOVE_DOCS_SQL": (
        "UPDATE related_subject_documents SET subject_id = $2 "
        "WHERE subject_id = $1 AND sha256 = ANY($3::text[]) RETURNING sha256"
    ),
    "_INSERT_DETACH_SQL": (
        "INSERT INTO related_subject_detachments (from_id, to_id, id_type, owner_keys_sha256, "
        "moved_documents, basis, evidence, created_by) "
        "VALUES ($1, $2, $3, $4, $5::text[], $6, $7::jsonb, $8)"
    ),
}
FORBIDDEN_TABLES = (
    "clients",
    "client_family_members",
    "companies",
    "related_subject_names",
    "related_subject_links",
    "related_subject_merges",
    "related_documents",
    "related_document_sightings",
)
ALLOWED_NAMING_THE_TABLE = {
    "services/crm/related_detach.py",
    "app/routers/crm_related_subjects.py",
    "services/crm/related_registry_read.py",
}


def sql_texts() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(detach_module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


def without_lock_clause(sql: str) -> str:
    return re.sub(r"\bFOR UPDATE\b", "", sql)


def test_d27_the_module_writes_only_is_mk_md_ih_and_names_no_other_table():
    texts = sql_texts()
    assert set(texts) == EXPECTED_SQL_NAMES
    for name, text in T3_TEXTS.items():
        assert texts[name] == text, name
    from backend.services.crm import related_subjects as subjects_module

    for name in ("_OWNERS_SQL", "_OWNER_KEYS_SQL", "_INSERT_SUBJECT_SQL"):
        assert texts[name] is getattr(subjects_module, name), name
    writing = {
        n
        for n, sql in texts.items()
        if re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE)\b", without_lock_clause(sql), re.I)
    }
    assert writing == {
        "_INSERT_SUBJECT_SQL",
        "_MOVE_KEY_SQL",
        "_MOVE_DOCS_SQL",
        "_INSERT_DETACH_SQL",
    }
    for name, sql in texts.items():
        assert not re.search(r"\b(DELETE|TRUNCATE|ON CONFLICT)\b", sql, re.I), name
        for table in FORBIDDEN_TABLES:
            assert not re.search(rf"\b{table}\b", sql), (name, table)
    source = Path(detach_module.__file__).read_text()
    for table in FORBIDDEN_TABLES:
        assert not re.search(rf"\b{table}\b", source), table
    assert not re.search(r"\b(DELETE|TRUNCATE)\b", source)
    # every statement the module runs is one of its named constants
    tree = ast.parse(source)
    ran = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"fetch", "fetchrow", "fetchval", "execute"}
        ):
            assert node.args and isinstance(node.args[0], ast.Name), ast.dump(node)[:80]
            ran.add(node.args[0].id)
    assert ran == EXPECTED_SQL_NAMES
    handler = inspect.getsource(route_module.detach_related_subject_identifier)
    assert not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", handler)
    assert TABLE not in handler


def test_d27_only_the_service_the_router_and_370_name_the_new_table():
    found = set()
    for root in (BACKEND / "app", BACKEND / "services"):
        for source in root.rglob("*.py"):
            if TABLE in source.read_text():
                found.add(str(source.relative_to(BACKEND)))
    assert found <= ALLOWED_NAMING_THE_TABLE, sorted(found - ALLOWED_NAMING_THE_TABLE)
    assert "services/crm/related_detach.py" in found
    migrations = sorted(p.name for p in MIGRATIONS.glob("*.sql") if TABLE in p.read_text())
    assert migrations == ["370_related_subject_detachments.sql"]
    for source in (BACKEND / "app" / "routers").glob("portal*.py"):
        assert TABLE not in source.read_text(), source.name


def test_d28_no_update_or_delete_of_the_new_table_anywhere_and_ih_is_a_plain_insert():
    pattern = re.compile(
        rf"\b(UPDATE\s+(ONLY\s+)?{TABLE}|DELETE\s+FROM\s+(ONLY\s+)?{TABLE}|TRUNCATE\s+(TABLE\s+)?"
        rf"(ONLY\s+)?{TABLE})\b",
        re.I,
    )
    for root in (BACKEND / "app", BACKEND / "services"):
        for source in root.rglob("*.py"):
            assert not pattern.search(source.read_text()), source
    ih = detach_module._INSERT_DETACH_SQL
    assert ih.startswith(f"INSERT INTO {TABLE} (") and "ON CONFLICT" not in ih.upper()
    assert "RETURNING" not in ih.upper()


@pytest.mark.asyncio
async def test_d27_the_write_measurement_sees_only_the_four_writes(client, conn, clients):
    s = await seed_owner(conn)
    await seed_name(conn, s, "ZzRecorded Owner")
    await seed_link(conn, s, LIVE1)
    k, d = owner_keys()
    resp = await post(client, req(k, d, owner_docs()))
    assert resp.json()["outcome"] == "detached"
    log = {(r["tbl"], r["op"]) for r in await conn.fetch("SELECT tbl, op FROM write_log")}
    assert log == DETACH_WRITES | {AUDIT_INSERT}


# ── D29: the log lines ────────────────────────────────────────────────────────

SERVICE_FORMAT = "related detach registry: outcome=%s from_id=%s to_id=%s dry_run=%s"
ROUTE_FORMAT = "related subject registry failure: code=%s"


class _AfterTransaction(logging.Handler):
    """Records each service line with whether the connection was still inside a transaction."""

    def __init__(self, raw):
        super().__init__(logging.DEBUG)
        self.raw, self.lines = raw, []

    def emit(self, record):
        self.lines.append((record.msg, record.args, self.raw.is_in_transaction()))


@pytest.mark.asyncio
async def test_d29_rows_6_to_10_log_one_service_line_after_the_transaction(
    conn, detach_writes, pool
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    service_logger = logging.getLogger(detach_module.__name__)
    async with pool.acquire() as raw:
        handler = _AfterTransaction(raw)
        previous = service_logger.level
        service_logger.addHandler(handler)
        service_logger.setLevel(logging.DEBUG)
        try:
            calls = [
                (req((("passport", PNONE), ("nik", NNONE)), ("nik", NNONE)), True),
                (req(k, d, [H3]), True),
                (req(k, d, owner_docs()), True),
                (req(k, d, owner_docs()), False),
                (req(k, d, owner_docs()), False),
                (req(k, d, owner_docs(tags=(1,))), False),
            ]
            answers = []
            for data, dry_run in calls:
                answers.append(await detach_identifier(raw, detach_input(data), dry_run=dry_run))
        finally:
            service_logger.removeHandler(handler)
            service_logger.setLevel(previous)
    t = answers[3]["to_id"]
    assert answers == [
        not_registered("none_owned"),
        refused("document_not_on_owner", s),
        would(s),
        detached(s, t),
        already(s, t),
        refused("detached_with_other_documents", s, t),
    ]
    assert handler.lines == [
        (SERVICE_FORMAT, ("subject_not_registered", None, None, True), False),
        (SERVICE_FORMAT, ("not_detachable", s, None, True), False),
        (SERVICE_FORMAT, ("detached", s, None, True), False),
        (SERVICE_FORMAT, ("detached", s, t, False), False),
        (SERVICE_FORMAT, ("already_detached", s, t, False), False),
        (SERVICE_FORMAT, ("not_detachable", s, t, False), False),
    ]


def _lines(caplog, module):
    return [(r.msg, r.args) for r in caplog.records if r.name == module.__name__]


@pytest.mark.asyncio
async def test_d29_rows_1_2_4_5_log_no_service_line_and_1_2_the_routers_fixed_line(
    pool, auth_on, audit_logger, monkeypatch, conn, caplog
):
    caplog.set_level(logging.DEBUG)
    await seed_owner(conn)
    k, d = owner_keys()
    good = req(k, d, owner_docs(), dry_run=True)
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
    assert _lines(caplog, detach_module) == []
    assert _lines(caplog, route_module) == expected


@pytest.mark.asyncio
async def test_d29_rows_3_and_11_log_only_the_routers_fixed_line(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn, caplog
):
    caplog.set_level(logging.DEBUG)
    s = await seed_owner(conn)
    k, d = owner_keys()
    deadlock = _Spy()

    async def boom(raw, args):
        raise asyncpg.DeadlockDetectedError("injected deadlock")

    deadlock.after[(MK_SQL, 1)] = boom
    resp, _ = await run_spied(pool, monkeypatch, req(k, d, owner_docs()), deadlock)
    assert resp.status_code == 503 and resp.json() == {"detail": "database_error"}
    short = _Spy()
    short.replace[(MD_SQL, 1)] = []
    resp, _ = await run_spied(pool, monkeypatch, req(k, d, owner_docs()), short)
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    assert _lines(caplog, detach_module) == []
    assert _lines(caplog, route_module) == [
        (ROUTE_FORMAT, ("database_error",)),
        (ROUTE_FORMAT, ("internal_error",)),
    ]
    assert await history(conn) == [] and await keys_of(conn, s) == sorted(k)


# ── D8-D10, D12: resolution and refusals ──────────────────────────────────────


async def decision_world(conn):
    """Every answer of T2 but detached, as name -> (request, expected answer)."""
    out: dict = {}

    async def owner(n, ids, *, kind="person", dob=DOB, merged=None, docs=()):
        sid = await seed_subject(conn, kind, f"ZzDecision {n}", ids, dob=dob, merged=merged)
        for tag in docs:
            await seed_document(conn, sid, n * 100 + tag)
        return sid

    n = 4245000
    out["r1_none_owned"] = (
        req(
            (("passport", passport(n)), ("nik", nik("150680", 5000))), ("nik", nik("150680", 5000))
        ),
        not_registered("none_owned"),
    )
    p, d = ("passport", passport(n + 1)), ("nik", nik("150680", 5001))
    await owner(n + 1, [p])
    await owner(n + 101, [d])
    out["r2_two_owners"] = (req((p, d), d), not_registered("two_owners"))
    p, d, q = ("passport", passport(n + 2)), ("nik", nik("150680", 5002)), ("permit", permit(n + 2))
    await owner(n + 2, [p, d, q])
    out["r3_set_differs_subset"] = (req((p, d), d), not_registered("set_differs"))
    p, d = ("passport", passport(n + 3)), ("nik", nik("150680", 5003))
    await owner(n + 3, [p, d])
    out["r3_set_differs_superset"] = (
        req((p, d, ("permit", permit(n + 903))), d),
        not_registered("set_differs"),
    )
    p, d, q = ("passport", passport(n + 4)), ("nik", nik("150680", 5004)), ("permit", permit(n + 4))
    await owner(n + 4, [p, d, q])
    out["r4_partial_overlap"] = (
        req((p, d, ("passport", passport(n + 904))), d),
        not_registered("partial_overlap"),
    )
    p, d = ("passport", passport(n + 5)), ("nik", nik("150680", 5005))
    await owner(n + 5, [p, d], kind="company")
    out["r5_kind_differs"] = (req((p, d), d), not_registered("kind_differs"))

    y = await owner(n + 6, [("passport", passport(n + 906))])
    p, d = ("passport", passport(n + 6)), ("nik", nik("150680", 5006))
    s = await owner(n + 7, [p, d], merged=y)
    out["n1_owner_is_merged"] = (req((p, d), d), refused("owner_is_merged", s))
    q, d = ("permit", permit(n + 8)), ("nik", nik("150680", 5008))
    s = await owner(n + 8, [q, d])
    out["n3_owner_left_permit_only"] = (req((q, d), d), refused("owner_left_permit_only", s))
    p, d = ("passport", passport(n + 9)), ("nik", nik("150680", 5009))
    s = await owner(n + 9, [p, d])
    out["n4_identifier_not_nik"] = (req((p, d), p), refused("identifier_not_nik", s))
    p, d = ("passport", passport(n + 10)), ("nik", nik("150680", 5010))
    s = await owner(n + 10, [p, d], dob=None)
    out["n5_owner_has_no_birth_date"] = (req((p, d), d), refused("owner_has_no_birth_date", s))
    p, d = ("passport", passport(n + 11)), ("nik", nik("000377", 5011))
    s = await owner(n + 11, [p, d])
    out["n6_nik_date_invalid"] = (req((p, d), d), refused("nik_date_invalid", s))
    p, d = ("passport", passport(n + 12)), ("nik", nik("120377", 5012))
    s = await owner(n + 12, [p, d])
    out["n7_nik_date_matches"] = (req((p, d), d), refused("nik_date_matches", s))
    p, d = ("passport", passport(n + 13)), ("nik", nik("150680", 5013))
    s = await owner(n + 13, [p, d], docs=(1,))
    out["n8_document_not_on_owner"] = (
        req((p, d), d, [doc((n + 13) * 100 + 1), doc(4245999)]),
        refused("document_not_on_owner", s),
    )
    # W': detached before with another list (history seeded, as an apply leaves it)
    p, d = ("passport", passport(n + 14)), ("nik", nik("150680", 5014))
    s = await owner(n + 14, [p])
    t = await owner(n + 914, [d], dob=None)
    await seed_document(conn, t, 4245914)
    await seed_detach_history(conn, s, t, fingerprint(p, d), [doc(4245914)])
    out["w_detached_with_other_documents"] = (
        req((p, d), d, []),
        refused("detached_with_other_documents", s, t),
    )
    out["w_already_detached"] = (req((p, d), d, [doc(4245914)]), ("already", s, t))
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d8_d9_every_reason_has_its_exact_body_in_both_modes(client, conn, clients, dry_run):
    world = await decision_world(conn)
    before = await digests(conn)
    for name, (data, expected) in world.items():
        if isinstance(expected, tuple):
            expected = already(expected[1], expected[2], dry_run)
        resp = await post(client, {**data, "dry_run": dry_run})
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json() == expected, (name, resp.json())
    assert await ledger(conn) == [] and await digests(conn) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d9_n3_an_owner_left_a_passport_and_a_permit_detaches(client, conn, clients, dry_run):
    # N3 refuses only when EVERY key left is a permit: the common production owner holds a
    # passport, a permit and a national number, and keeps the passport and the permit
    p, q, d = (
        ("passport", passport(4245020)),
        ("permit", permit(4245020)),
        ("nik", nik("150680", 5020)),
    )
    s = await seed_subject(conn, "person", "ZzDecision Three Keys", [p, q, d], dob=DOB)
    await seed_document(conn, s, 424502001)
    resp = await post(client, req((p, q, d), d, [doc(424502001)], dry_run=dry_run))
    if dry_run:
        assert resp.json() == would(s)
        assert await ledger(conn) == []
        return
    (h,) = await history(conn)
    t = h["to_id"]
    assert resp.json() == detached(s, t)
    assert await keys_of(conn, t) == [d] and await keys_of(conn, s) == sorted([p, q])
    assert await docs_of(conn, t) == [doc(424502001)] and await docs_of(conn, s) == []


async def precedence_world(conn):
    """Seeded states holding two refusals at once: name -> (request, the earlier reason, owner)."""
    out: dict = {}
    n = 4246000
    y = await seed_subject(conn, "person", "ZzPrecedence Y", [("passport", passport(n))])
    q, d = ("permit", permit(n + 1)), ("nik", nik("150680", 6001))
    s = await seed_subject(conn, "person", None, [q, d], dob=DOB, merged=y)
    out["n1_before_n3"] = (req((q, d), d), "owner_is_merged", s)
    q, p = ("permit", permit(n + 2)), ("passport", passport(n + 2))
    s = await seed_subject(conn, "person", None, [q, p], dob=DOB)
    out["n3_before_n4"] = (req((q, p), p), "owner_left_permit_only", s)
    p, d = ("passport", passport(n + 3)), ("nik", nik("150680", 6003))
    s = await seed_subject(conn, "person", None, [p, d], dob=None)
    out["n4_before_n5"] = (req((p, d), p), "identifier_not_nik", s)
    p, d = ("passport", passport(n + 4)), ("nik", nik("000377", 6004))
    s = await seed_subject(conn, "person", None, [p, d], dob=None)
    out["n5_before_n6"] = (req((p, d), d), "owner_has_no_birth_date", s)
    p, d = ("passport", passport(n + 5)), ("nik", nik("000377", 6005))
    s = await seed_subject(conn, "person", None, [p, d], dob=DOB)
    out["n6_before_n8"] = (req((p, d), d, [doc(4246999)]), "nik_date_invalid", s)
    p, d = ("passport", passport(n + 6)), ("nik", nik("120377", 6006))
    s = await seed_subject(conn, "person", None, [p, d], dob=DOB)
    out["n7_before_n8"] = (req((p, d), d, [doc(4246999)]), "nik_date_matches", s)
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d10_the_earlier_refusal_wins_on_a_state_holding_two(client, conn, clients, dry_run):
    for name, (data, reason, s) in (await precedence_world(conn)).items():
        resp = await post(client, {**data, "dry_run": dry_run})
        assert resp.json() == refused(reason, s), (name, resp.json())
    assert await ledger(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d10_r3_before_r5_and_r5_before_n1(client, conn, clients, dry_run):
    n = 4246010
    y = await seed_subject(conn, "person", "ZzPrecedence Survivor", [("passport", passport(n))])
    # a merged company record holding exactly K: R5 (kind) before N1 (merged)
    p, d = ("passport", passport(n + 1)), ("nik", nik("150680", 6011))
    await seed_subject(conn, "company", None, [p, d], merged=y)
    resp = await post(client, req((p, d), d, dry_run=dry_run))
    assert resp.json() == not_registered("kind_differs"), resp.json()
    # a company record holding K and one key more: R3 (set) before R5 (kind)
    p, d, q = ("passport", passport(n + 2)), ("nik", nik("150680", 6012)), ("permit", permit(n + 2))
    await seed_subject(conn, "company", None, [p, d, q])
    resp = await post(client, req((p, d), d, dry_run=dry_run))
    assert resp.json() == not_registered("set_differs"), resp.json()
    assert await ledger(conn) == []


@pytest.mark.asyncio
async def test_d12_a_survivor_detaches_and_a_hash_only_on_a_merged_in_record_is_n8(
    client, conn, clients
):
    s = await seed_owner(conn, 4246101, docs=(1,))
    k, d = owner_keys(4246101)
    m = await seed_subject(conn, "person", None, [("permit", permit(4246101))], merged=s)
    only_member = await seed_document(conn, m, 4246199)
    for dry_run in (True, False):
        resp = await post(client, req(k, d, [only_member], dry_run=dry_run))
        assert resp.json() == refused("document_not_on_owner", s)
    assert (await post(client, req(k, d, owner_docs(4246101, (1,)), dry_run=True))).json() == would(
        s
    )
    resp = await post(client, req(k, d, owner_docs(4246101, (1,))))
    t = resp.json()["to_id"]
    assert resp.json() == detached(s, t)
    assert await docs_of(conn, m) == [only_member]
    assert await conn.fetchval("SELECT merged_into FROM related_subjects WHERE id = $1", m) == s


# ── D11: the national-number date rule (T6) ───────────────────────────────────

MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
NIK_DATE_ROWS = [
    ("120377", (12, 3, 77)),
    ("520377", (12, 3, 77)),
    ("130377", (13, 3, 77)),
    ("120477", (12, 4, 77)),
    ("120378", (12, 3, 78)),
    ("120301", (12, 3, 1)),
    ("000377", None),
    ("410177", (1, 1, 77)),
    ("710177", (31, 1, 77)),
    ("400377", None),
    ("320177", None),
    ("720177", None),
    ("310477", None),
    ("710477", None),
    ("700477", (30, 4, 77)),
    ("120077", None),
    ("121377", None),
    ("311277", (31, 12, 77)),
    ("290277", (29, 2, 77)),
    ("690277", (29, 2, 77)),
    ("300277", None),
]
for _mm, _last in enumerate(MONTH_DAYS, start=1):
    NIK_DATE_ROWS += [
        (f"{_last:02d}{_mm:02d}77", (_last, _mm, 77)),
        (f"{_last + 1:02d}{_mm:02d}77", None),
        (f"{_last + 40:02d}{_mm:02d}77", (_last, _mm, 77)),
        (f"{_last + 41:02d}{_mm:02d}77", None),
    ]
# rows 7-9 above day 72: no day of any month, whatever the encoding (a rule that takes DD modulo
# 40 agrees with T6 on every DD up to 72 and accepts 81-99)
NIK_DATE_ROWS += [(f"{_dd:02d}0377", None) for _dd in range(73, 100)]


@pytest.mark.parametrize("ddmmyy,expected", NIK_DATE_ROWS)
def test_d11_the_helper_returns_day_month_and_two_digit_year_or_none(ddmmyy, expected):
    assert nik_date(nik(ddmmyy)) == expected


@pytest.mark.parametrize(
    "bad",
    [
        nik("150680")[:15],
        nik("150680") + "1",
        nik("150680")[:15] + "١",
        "١" * 16,
        "99004215068O4242",
        "",
        " " + nik("150680")[:15],
    ],
)
def test_d11_the_helper_raises_on_anything_but_sixteen_ascii_digits(bad):
    with pytest.raises(ValueError):
        nik_date(bad)


def test_d11_the_helper_returns_integers():
    assert all(type(x) is int for x in nik_date(nik("120377")))


# (DDMMYY, owner's birth date, expected): "pass" = the request would detach
T6_ROUTE_ROWS = [
    ("120377", date(1977, 3, 12), "nik_date_matches"),
    ("520377", date(1977, 3, 12), "nik_date_matches"),
    ("130377", date(1977, 3, 12), "pass"),
    ("120477", date(1977, 3, 12), "pass"),
    ("120378", date(1977, 3, 12), "pass"),
    ("120301", date(1901, 3, 12), "nik_date_matches"),
    ("120301", date(2001, 3, 12), "nik_date_matches"),
    ("000377", date(1977, 3, 12), "nik_date_invalid"),
    ("410177", date(1977, 1, 1), "nik_date_matches"),
    ("710177", date(1977, 1, 31), "nik_date_matches"),
    ("400377", date(1977, 3, 12), "nik_date_invalid"),
    ("320177", date(1977, 3, 12), "nik_date_invalid"),
    ("720177", date(1977, 3, 12), "nik_date_invalid"),
    ("310477", date(1977, 3, 12), "nik_date_invalid"),
    ("710477", date(1977, 3, 12), "nik_date_invalid"),
    ("700477", date(1977, 3, 12), "pass"),
    ("120077", date(1977, 3, 12), "nik_date_invalid"),
    ("121377", date(1977, 3, 12), "nik_date_invalid"),
    ("311277", date(1977, 12, 31), "nik_date_matches"),
    ("311277", date(1977, 3, 12), "pass"),
    ("290277", date(1977, 3, 12), "pass"),
    ("690277", date(1977, 3, 12), "pass"),
    ("000377", None, "owner_has_no_birth_date"),
]
for _mm, _last in enumerate(MONTH_DAYS, start=1):
    T6_ROUTE_ROWS += [
        (f"{_last:02d}{_mm:02d}77", date(1977, 3, 12), "pass"),
        (f"{_last + 1:02d}{_mm:02d}77", date(1977, 3, 12), "nik_date_invalid"),
    ]
T6_ROUTE_ROWS += [
    ("810377", date(1977, 3, 12), "nik_date_invalid"),
    ("990377", date(1977, 3, 12), "nik_date_invalid"),
]


@pytest.mark.asyncio
async def test_d11_every_t6_row_through_the_route(client, conn, clients):
    cases = []
    for i, (ddmmyy, dob, expected) in enumerate(T6_ROUTE_ROWS):
        p, d = ("passport", passport(4248000 + i)), ("nik", nik(ddmmyy, 8000 + i))
        s = await seed_subject(conn, "person", None, [p, d], dob=dob)
        cases.append((req((p, d), d, dry_run=True), s, expected, ddmmyy, dob))
    # row 15: the key sent is a passport, the key left a national number
    p, d = ("passport", passport(4248900)), ("nik", nik("150680", 8900))
    s = await seed_subject(conn, "person", None, [p, d], dob=DOB)
    cases.append((req((p, d), p, dry_run=True), s, "identifier_not_nik", "passport", DOB))
    # row 18: separators and letters around the sixteen digits; the rule reads the digits
    p, d = ("passport", passport(4248901)), ("nik", nik("120377", 8901))
    s = await seed_subject(conn, "person", None, [p, d], dob=DOB)
    raw = f"NIK:{d[1][:6]}-{d[1][6:12]}.{d[1][12:]}"
    cases.append((req((p, d), ("nik", raw), dry_run=True), s, "nik_date_matches", raw, DOB))
    for data, s, expected, ddmmyy, dob in cases:
        resp = await post(client, data)
        want = would(s) if expected == "pass" else refused(expected, s)
        assert resp.status_code == 200 and resp.json() == want, (ddmmyy, dob, resp.json())
    # row 17: a value of 15 or 17 digits is refused by the model at ["detach"]
    p, d = cases[0][0]["owner"]["identifiers"]
    for value in (d["value"][:15], d["value"] + "7"):
        resp = await post(
            client, req(((p["type"], p["value"]), (d["type"], d["value"])), ("nik", value))
        )
        assert resp.status_code == 422
        assert resp.json() == _validation([(["detach"], "invalid_identifier")])
    assert await ledger(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d11_a_stored_malformed_key_is_refused_by_n4_before_the_date_rule(
    conn, detach_writes, dry_run
):
    bad_norms = (
        nik("150680", 8950)[:15],
        nik("150680", 8951) + "1",
        nik("150680", 8952)[:15] + "١",
    )
    for i, bad in enumerate(bad_norms):
        p = ("passport", passport(4248950 + i))
        s = await seed_subject(conn, "person", None, [p], dob=DOB)
        await seed_raw_key(conn, s, "nik", bad)
        identifiers = tuple(
            sorted(
                (IdentifierInput("nik", bad, bad), IdentifierInput("passport", p[1], p[1])),
                key=lambda x: x.key,
            )
        )
        request = DetachInput(
            kind="person",
            identifiers=identifiers,
            detach=("nik", bad),
            documents=(),
            basis="nik_birth_date",
            evidence={"plan_sha256": PLAN, "files": 1},
        )
        answer = await detach_identifier(conn, request, dry_run=dry_run)
        assert answer == refused("identifier_not_nik", s), (i, answer)
    assert await ledger(conn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False], ids=["dry_run", "apply"])
async def test_d11_row_15_a_key_of_another_type_is_n4_even_when_it_is_sixteen_digits(
    conn, detach_writes, dry_run
):
    # No valid passport or permit is sixteen ASCII digits (12 characters at most; a permit holds a
    # letter), so through the route row 15 cannot tell "type checked" from "digits checked". A
    # stored passport norm of sixteen digits, sent to the service, encodes a valid date that is not
    # the owner's: only the type check refuses it.
    n = nik("150680", 8960)
    sixteen = nik("150680", 8961)
    s = await seed_subject(conn, "person", None, [("nik", n)], dob=DOB)
    await seed_raw_key(conn, s, "passport", sixteen)
    request = DetachInput(
        kind="person",
        identifiers=(IdentifierInput("nik", n, n), IdentifierInput("passport", sixteen, sixteen)),
        detach=("passport", sixteen),
        documents=(),
        basis="nik_birth_date",
        evidence={"plan_sha256": PLAN, "files": 1},
    )
    assert nik_date(sixteen) == (15, 6, 80)
    answer = await detach_identifier(conn, request, dry_run=dry_run)
    assert answer == refused("identifier_not_nik", s)
    assert await ledger(conn) == []


# ── D21-D22: defences and failures ────────────────────────────────────────────

WRITE_SQL = {"IS": IS_SQL, "MK": MK_SQL, "MD": MD_SQL, "IH": IH_SQL}


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["IS", "MK", "MD", "IH"])
async def test_d21_a_failure_after_each_write_leaves_the_before_state(
    pool, conn, detach_writes, after
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    before = await digests(conn)
    spy = _Spy()

    async def boom(raw, args):
        raise RuntimeError("injected")

    spy.after[(WRITE_SQL[after], 1)] = boom
    async with pool.acquire() as c:
        with pytest.raises(RuntimeError):
            await detach_identifier(
                _SpyConn(c, spy), detach_input(req(k, d, owner_docs())), dry_run=False
            )
    assert spy.names()[-1] == after
    assert await digests(conn) == before and await ledger(conn) == []
    ok = await detach_identifier(conn, detach_input(req(k, d, owner_docs())), dry_run=False)
    assert ok == detached(s, ok["to_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["mk_no_row", "md_none", "md_short"], ids=["mk_no_row", "md_none", "md_short"]
)
async def test_d21_a_defence_is_the_fixed_500_and_everything_is_rolled_back(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn, fault
):
    s = await seed_owner(conn)
    k, d = owner_keys()
    before = await digests(conn)
    spy = _Spy()
    if fault == "mk_no_row":
        spy.replace[(MK_SQL, 1)] = None
    elif fault == "md_none":
        spy.replace[(MD_SQL, 1)] = []
    else:
        spy.replace[(MD_SQL, 1)] = [{"sha256": owner_docs()[0]}]
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, owner_docs()), spy)
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    assert spy.names()[-1] == ("MK" if fault == "mk_no_row" else "MD")
    assert await digests(conn) == before and await ledger(conn) == []
    assert await keys_of(conn, s) == sorted(k)
    assert issubclass(DetachInconsistent, RuntimeError)


@pytest.mark.asyncio
async def test_d21_a_deadlock_is_the_503_database_error_after_one_attempt_and_rolled_back(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn
):
    await seed_owner(conn)
    k, d = owner_keys()
    before = await digests(conn)
    spy = _Spy()

    async def deadlock(raw, args):
        raise asyncpg.DeadlockDetectedError("injected deadlock")

    spy.after[(MK_SQL, 1)] = deadlock
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, owner_docs()), spy)
    assert resp.status_code == 503 and resp.json() == {"detail": "database_error"}
    assert spy.transactions == [{}]
    assert spy.count(O_SQL) == 2 and spy.count(IS_SQL) == 1 and spy.count(MK_SQL) == 1
    assert await digests(conn) == before and await ledger(conn) == []


@pytest.mark.asyncio
async def test_d22_the_owner_row_deleted_before_the_lock_is_none_owned(pool, conn, detach_writes):
    s = await seed_owner(conn)
    k, d = owner_keys()
    spy = _Spy()

    async def delete_owner(raw, args):
        async with pool.acquire() as other, unmeasured(other):
            await other.execute("DELETE FROM related_subjects WHERE id = $1", s)

    spy.after[(O_SQL, 1)] = delete_owner
    async with pool.acquire() as c:
        answer = await detach_identifier(
            _SpyConn(c, spy), detach_input(req(k, d, owner_docs())), dry_run=False
        )
    assert answer == not_registered("none_owned")
    assert spy.names() == ["O", "L"]
    assert await ledger(conn) == [] and await history(conn) == []


async def _with_owner_rows(pool, data, replace, *, kf=None):
    spy = _Spy()
    spy.replace[(O_SQL, 2)] = replace
    if kf is not None:
        spy.replace[(KF_SQL, 1)] = kf
    async with pool.acquire() as c:
        answer = await detach_identifier(_SpyConn(c, spy), detach_input(data), dry_run=False)
    return answer, spy


@pytest.mark.asyncio
async def test_d22_the_owners_read_again_under_the_lock_give_t2s_answers(pool, conn, detach_writes):
    s = await seed_owner(conn)
    k, d = owner_keys()
    data = req(k, d, [])
    # no row under the lock
    answer, spy = await _with_owner_rows(pool, data, [])
    assert answer == not_registered("none_owned") and spy.names() == ["O", "L", "O"]
    # two owners, d's owner without a history row
    x = await seed_subject(conn, "person", None, [("nik", nik("150680", 9101))])
    two = [row(s, "passport", k[0][1]), row(x, "nik", d[1])]
    answer, spy = await _with_owner_rows(pool, data, two)
    assert answer == not_registered("two_owners")
    assert spy.names() == ["O", "L", "O", "WH"] and spy.args_of(WH_SQL) == [(x,)]
    # two owners and a history row of THIS request: the witness reads the LATEST owners
    await seed_detach_history(conn, s, x, keys_sha256([k[0], d]), [])
    answer, spy = await _with_owner_rows(pool, data, two)
    assert answer == already(s, x)
    assert spy.names() == ["O", "L", "O", "WH"] and spy.args_of(WH_SQL) == [(x,)]
    answer, spy = await _with_owner_rows(pool, {**data, "documents": owner_docs()}, two)
    assert answer == refused("detached_with_other_documents", s, x)
    assert spy.names() == ["O", "L", "O", "WH"] and spy.args_of(WH_SQL) == [(x,)]
    # one owner X other than the candidate: KF(X), no lock on X, set_differs
    y = await seed_subject(conn, "person", None, [("nik", nik("150680", 9102))])
    answer, spy = await _with_owner_rows(pool, data, [row(y, "nik", d[1])])
    assert answer == not_registered("set_differs")
    assert spy.names() == ["O", "L", "O", "KF"]
    assert spy.args_of(KF_SQL) == [(y,)] and spy.args_of(L_SQL) == [(s,)]
    # ... and X holding exactly K is a defence
    k_rows = [{"id_type": t, "id_norm": v} for t, v in (k[0], d)]
    with pytest.raises(DetachInconsistent):
        await _with_owner_rows(pool, data, [row(y, "nik", d[1])], kf=k_rows)
    defence = _Spy()
    defence.replace[(O_SQL, 2)] = [row(y, "nik", d[1])]
    defence.replace[(KF_SQL, 1)] = k_rows
    async with pool.acquire() as c:
        with pytest.raises(DetachInconsistent):
            await detach_identifier(_SpyConn(c, defence), detach_input(data), dry_run=False)
    assert defence.names() == ["O", "L", "O", "KF"] and defence.args_of(KF_SQL) == [(y,)]
    assert await ledger(conn) == [] and await keys_of(conn, s) == sorted(k)


@pytest.mark.asyncio
async def test_d22_the_two_defences_through_the_route_are_the_fixed_500(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn
):
    await seed_owner(conn)
    k, d = owner_keys()
    y = await seed_subject(conn, "person", None, [("nik", nik("150680", 9103))])
    spy = _Spy()
    spy.replace[(O_SQL, 2)] = [row(y, "nik", d[1])]
    spy.replace[(KF_SQL, 1)] = [{"id_type": t, "id_norm": v} for t, v in (k[0], d)]
    resp, spy = await run_spied(pool, monkeypatch, req(k, d, []), spy)
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    dry = _Spy()
    dry.replace[(RW_SQL, 1)] = None
    resp, dry = await run_spied(pool, monkeypatch, req(k, d, [], dry_run=True), dry)
    assert resp.status_code == 500 and resp.json() == {"detail": "internal_error"}
    assert dry.names() == ["O", "KF", "RW"]
    assert await ledger(conn) == [] and await history(conn) == []


# ── D17: after every T1 outcome, nothing else changed ─────────────────────────

TOUCHED_PK = {
    "related_subjects": ("id",),
    "related_subject_identifiers": ("id_type", "id_norm"),
    "related_subject_documents": ("subject_id", "sha256"),
    "related_subject_detachments": ("id",),
}


async def world_state(conn):
    untouched = {t: await digest(conn, t) for t in UNTOUCHED_TABLES}
    rows = {}
    for table, pk in TOUCHED_PK.items():
        cols = ", ".join(pk)
        rows[table] = {
            tuple(r[c] for c in pk): r["j"]
            for r in await conn.fetch(f"SELECT {cols}, to_jsonb(x)::text AS j FROM {table} x")
        }
    return untouched, rows


def named_by(log) -> set:
    """(table, primary key) of every row the ledger names, by its OLD or its NEW key."""
    out = set()
    for tbl, _op, old, new, _changed in log:
        assert tbl in TOUCHED_PK, tbl
        for key in (old, new):
            if key:
                out.add((tbl, tuple(key[c] for c in TOUCHED_PK[tbl])))
    return out


async def d17_world(conn):
    n = 4249001
    s = await seed_owner(conn, n, docs=(1, 2, 3))
    by = await seed_subject(conn, "person", "ZzD17 Bystander", [("passport", passport(4249901))])
    await seed_document(conn, by, n * 100 + 1, ref="ZzDriveFileDetach0001")  # a listed hash
    await seed_name(conn, s, "ZzRecorded Owner")
    await seed_link(conn, s, LIVE1)
    await seed_link(conn, by, LIVE2)
    m = await seed_subject(conn, "person", None, [("permit", permit(n))], merged=s)
    await seed_merge_history(conn, m, s)
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO client_family_members (client_id, full_name, passport_number) "
            "VALUES ($1, 'ZzFamily Member', 'ZZ4249902')",
            LIVE1,
        )
    k, d = owner_keys(n)
    return s, k, d, owner_docs(n, (1, 2))


D17_OUTCOMES = (
    "1_flag_off",
    "2_no_key",
    "3_database_error",
    "4_too_long",
    "5_invalid",
    "6_none_owned",
    "7_document_not_on_owner",
    "8_already_detached",
    "9_would_detach",
    "10_detached",
    "11_internal_error",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", D17_OUTCOMES)
async def test_d17_after_every_t1_outcome_only_the_rows_the_ledger_names_changed(
    pool, enabled, auth_on, audit_logger, detach_writes, monkeypatch, conn, clients, outcome
):
    s, k, d, listed = await d17_world(conn)
    data = req(k, d, listed)
    headers = HEADERS
    spy = _Spy()
    if outcome == "8_already_detached":
        await detach_identifier(conn, detach_input(data), dry_run=False)
        await forget_writes(conn)
    before = await world_state(conn)
    if outcome == "1_flag_off":
        monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    elif outcome == "2_no_key":
        headers = {"Content-Type": "application/json"}
    elif outcome == "3_database_error":

        async def deadlock(raw, args):
            raise asyncpg.DeadlockDetectedError("injected deadlock")

        spy.after[(MK_SQL, 1)] = deadlock
    elif outcome == "4_too_long":
        data = b" " * (route_module.MAX_BODY_BYTES + 1)
    elif outcome == "5_invalid":
        data = {**data, "basis": "manual"}
    elif outcome == "6_none_owned":
        data = req(
            (("passport", passport(4249950)), ("nik", nik("150680", 9950))),
            ("nik", nik("150680", 9950)),
        )
    elif outcome == "7_document_not_on_owner":
        data = {**data, "documents": [*listed, doc(4249999)]}
    elif outcome == "9_would_detach":
        data = {**data, "dry_run": True}
    elif outcome == "11_internal_error":
        spy.replace[(MD_SQL, 1)] = []
    spy_pool, spy_app = spy_client(pool, monkeypatch, spy)
    async with spy_app as c:
        resp = await post(c, data, headers=headers)
    expected_status = {
        "1_flag_off": 503,
        "2_no_key": 401,
        "3_database_error": 503,
        "4_too_long": 422,
        "5_invalid": 422,
        "11_internal_error": 500,
    }.get(outcome, 200)
    assert resp.status_code == expected_status, resp.text
    t = None
    if outcome in ("8_already_detached", "10_detached"):
        (h,) = await history(conn)
        t = h["to_id"]
    expected_body = {
        "1_flag_off": {"detail": "registry_unavailable"},
        "2_no_key": {"detail": "unauthorized"},
        "3_database_error": {"detail": "database_error"},
        "4_too_long": _validation([(["body"], "too_long")]),
        "5_invalid": _validation([(["basis"], "literal_error")]),
        "6_none_owned": not_registered("none_owned"),
        "7_document_not_on_owner": refused("document_not_on_owner", s),
        "8_already_detached": already(s, t),
        "9_would_detach": would(s),
        "10_detached": detached(s, t),
        "11_internal_error": {"detail": "internal_error"},
    }[outcome]
    assert resp.json() == expected_body, (outcome, resp.text)
    log = await ledger(conn)
    if outcome == "10_detached":
        assert {(e[0], e[1]) for e in log} == DETACH_WRITES
    else:
        assert log == [], outcome
    after = await world_state(conn)
    assert after[0] == before[0], outcome  # the seven tables no detachment touches
    named = named_by(log)
    for table in TOUCHED_PK:
        rows_before, rows_after = before[1][table], after[1][table]
        for pk in set(rows_before) | set(rows_after):
            if (table, pk) not in named:
                assert rows_before.get(pk) == rows_after.get(pk), (outcome, table, pk)
    # the bystander's link of a listed hash stays where it was
    by_docs = await conn.fetchval(
        "SELECT count(*) FROM related_subject_documents d JOIN related_subjects s "
        "ON s.id = d.subject_id WHERE s.display_name = 'ZzD17 Bystander' AND d.sha256 = $1",
        listed[0],
    )
    assert by_docs == 1


# ── D24-D25: the read path and the other routes after a detachment ────────────


async def read_world(conn):
    n = 4250001
    s = await seed_owner(conn, n, docs=(1, 2, 3), name="ZzOwner Read")
    await seed_subject(conn, "person", "ZzAnother Named", [("passport", passport(4250901))])
    m = await seed_subject(conn, "person", "ZzMember Read", [("permit", permit(n))], merged=s)
    await seed_merge_history(conn, m, s)
    await seed_document(conn, m, n * 100 + 1)  # the first listed hash is also on a member
    k, d = owner_keys(n)
    return s, m, k, d, owner_docs(n, (1, 2))


def _without(row, *names):
    return {key: value for key, value in row.items() if key not in names}


async def read_statement_counts(conn, subject_id):
    """(statements of one unfiltered list, statements of the subject's detail), read service."""
    spy = _Spy()
    reader = _SpyConn(conn, spy)
    await related_registry_read.list_subjects(reader, q=None, kind=None, limit=50, offset=0)
    listed = len(spy.calls)
    await related_registry_read.get_subject(reader, subject_id)
    return listed, len(spy.calls) - listed


@pytest.mark.asyncio
async def test_d24_the_list_the_rows_the_search_and_both_details_after_a_detachment(
    admin, conn, clients
):
    s, m, k, d, listed = await read_world(conn)
    counts_before = await read_statement_counts(conn, s)
    # list: total, page, identifiers, document counts; S's detail: subject, merges, identifiers,
    # documents, sightings, names, links
    assert counts_before == (4, 8)
    before = (await admin.get(READ, params={"limit": 100})).json()
    s_before = next(r for r in before["items"] if r["id"] == s)
    assert {"type": "nik", "value": d[1], "from_subject_id": None} in s_before["identifiers"]
    assert s_before["document_count"] == 3
    resp = await post(admin, req(k, d, listed))
    t = resp.json()["to_id"]
    assert resp.json() == detached(s, t)

    after = (await admin.get(READ, params={"limit": 100})).json()
    assert after["total"] == before["total"] + 1
    ids = [r["id"] for r in after["items"]]
    assert ids[:-1] == [r["id"] for r in before["items"]] and ids[-1] == t  # NULLS LAST
    s_after = next(r for r in after["items"] if r["id"] == s)
    assert _without(s_after, "identifiers", "document_count") == _without(
        s_before, "identifiers", "document_count"
    )
    assert s_after["identifiers"] == [
        i for i in s_before["identifiers"] if (i["type"], i["value"]) != d
    ]
    assert s_after["document_count"] == 3 - len(listed) + 1  # one moved hash is on a member
    t_row = after["items"][-1]
    assert t_row == {
        "id": t,
        "kind": "person",
        "display_name": None,
        "name_source": None,
        "date_of_birth": None,
        "nationality": None,
        "identifiers": [{"type": "nik", "value": d[1], "from_subject_id": None}],
        "document_count": len(listed),
        "links_count": 0,
    }

    found = await admin.get(READ, params={"q": d[1][:10], "limit": 100})
    assert [r["id"] for r in found.json()["items"]] == [t]
    service = await related_registry_read.list_subjects(
        conn, q=d[1][:10], kind=None, limit=50, offset=0
    )
    assert [r["id"] for r in service["items"]] == [t]

    s_detail = (await admin.get(f"{READ}/{s}")).json()
    assert ("nik", d[1]) not in {(i["type"], i["value"]) for i in s_detail["identifiers"]}
    s_docs = {x["sha256"]: x["from_subject_id"] for x in s_detail["documents"]}
    assert s_docs == {listed[0]: m, doc(4250001 * 100 + 3): None}
    t_detail = (await admin.get(f"{READ}/{t}")).json()
    created = t_detail.pop("created_at")
    assert created
    (split,) = t_detail.pop("detached_from")
    assert split.pop("detached_at")
    assert split == {
        "subject_id": s,
        "id_type": "nik",
        "basis": "nik_birth_date",
        "documents_moved": len(listed),
        "from_subject_id": None,
    }
    assert t_detail == {
        "id": t,
        "kind": "person",
        "display_name": None,
        "name_source": None,
        "names": [],
        "names_total": 0,
        "links": [],
        "links_total": 0,
        "date_of_birth": None,
        "nationality": None,
        "sex": None,
        "identifiers": [{"type": "nik", "value": d[1], "from_subject_id": None}],
        "documents": [
            {
                "sha256": sha,
                "doc_type": "passport",
                "mime_type": "application/pdf",
                "byte_size": 1234,
                "first_seen_on": "2026-01-05",
                "drive_file_ids": [],
                "from_subject_id": None,
            }
            for sha in sorted(listed)
        ],
        "merged_from": [],
        "merged_into": None,
        "detached_into": [],
    }
    assert await related_registry_read.get_subject(conn, t) is not None
    # read statement counts unchanged: S as before; T is a record without merges, with documents
    assert await read_statement_counts(conn, s) == counts_before
    assert (await read_statement_counts(conn, t))[1] == 7


@pytest.mark.asyncio
async def test_d25_the_other_routes_after_a_detachment_answer_as_section_4_says(
    admin, conn, clients, writes
):
    writes.update(
        {
            ("related_documents", "INSERT"),
            ("related_subject_documents", "INSERT"),
            ("related_subject_names", "INSERT"),
            ("related_subject_links", "INSERT"),
        }
    )
    s = await seed_owner(conn, 4251001)
    y = await seed_subject(conn, "person", "ZzMerge Target", [("passport", passport(4251901))])
    k, d = owner_keys(4251001)
    t = (await post(admin, req(k, d, owner_docs(4251001)))).json()["to_id"]
    assert t != s
    evidence = {"plan_sha256": PLAN, "subject_ref": "REF-4D", "tier": "t1"}
    register = {"kind": "person", "evidence": evidence, "dry_run": False}
    one = await post(admin, {**register, "identifiers": keys([d])}, url=URL_SUBJECT)
    assert one.json() == {
        "outcome": "already_registered",
        "subject_id": t,
        "match_basis": "identifiers_only",
        "dry_run": False,
    }
    both = await post(admin, {**register, "identifiers": keys(k)}, url=URL_SUBJECT)
    assert both.json() == {"outcome": "conflict", "reason": "two_owners", "dry_run": False}

    documents = await post(admin, documents_body([d], [4251777]), url=URL_DOCUMENTS)
    assert documents.json() == {
        "outcome": "linked",
        "subject_id": t,
        "dry_run": False,
        "counts": {"documents": 1, "sightings": 0, "links": 1, "descriptive_differences": 0},
    }
    documents_k = await post(admin, documents_body(k, [4251778]), url=URL_DOCUMENTS)
    assert documents_k.json() == {
        "outcome": "subject_not_registered",
        "reason": "two_owners",
        "dry_run": False,
    }
    names = await post(admin, names_body([d]), url=URL_NAMES)
    assert names.json() == {
        "outcome": "recorded",
        "subject_id": t,
        "recorded": 1,
        "already_recorded": 0,
        "dry_run": False,
    }
    names_k = await post(admin, names_body(k), url=URL_NAMES)
    assert names_k.json() == {"outcome": "subject_not_registered", "reason": "two_owners"}
    links = await post(admin, links_body([d]), url=URL_LINKS)
    assert links.json() == {
        "outcome": "recorded",
        "subject_id": t,
        "recorded": 1,
        "already_recorded": 0,
        "dry_run": False,
    }
    links_k = await post(admin, links_body(k), url=URL_LINKS)
    assert links_k.json() == {"outcome": "subject_not_registered", "reason": "two_owners"}
    merge = {
        "source": {"kind": "person", "identifiers": keys([d])},
        "target": {"kind": "person", "identifiers": keys([("passport", passport(4251901))])},
        "basis": "permit_stamp",
        "evidence": {"plan_sha256": PLAN, "files": 1},
        "dry_run": False,
    }
    merged_answer = await post(admin, merge, url=URL_MERGES)
    assert merged_answer.json() == {
        "outcome": "not_mergeable",
        "reason": "basis_precondition",
        "source_id": t,
        "target_id": y,
    }


# ── D33: the operator's undo ──────────────────────────────────────────────────

# 2.2's block, verbatim; <S> and <T> are the operator's two ids.
UNDO_BLOCK = """DO $$
DECLARE s bigint := <S>; t bigint := <T>; h related_subject_detachments; n bigint;
BEGIN
  IF s = t THEN RAISE EXCEPTION 'undo 1 S=T'; END IF;
  PERFORM 1 FROM related_subjects WHERE id IN (s, t) ORDER BY id FOR UPDATE;
  GET DIAGNOSTICS n = ROW_COUNT; IF n <> 2 THEN RAISE EXCEPTION 'undo 2 row'; END IF;
  SELECT * INTO h FROM related_subject_detachments WHERE from_id = s AND to_id = t;
  IF NOT FOUND THEN RAISE EXCEPTION 'undo 3 history'; END IF;
  IF (SELECT count(*) FROM related_subject_identifiers WHERE subject_id = t) <> 1
     OR NOT EXISTS (SELECT 1 FROM related_subject_identifiers WHERE subject_id = t AND id_type = h.id_type)
     THEN RAISE EXCEPTION 'undo 4 key'; END IF;
  IF (SELECT coalesce(array_agg(sha256 ORDER BY sha256 COLLATE "C"), '{}') FROM related_subject_documents
      WHERE subject_id = t) <> h.moved_documents THEN RAISE EXCEPTION 'undo 5 documents'; END IF;
  IF EXISTS (SELECT 1 FROM related_subject_names WHERE subject_id = t)
     OR EXISTS (SELECT 1 FROM related_subject_links WHERE subject_id = t)
     OR EXISTS (SELECT 1 FROM related_subjects WHERE merged_into = t OR (id = t AND merged_into IS NOT NULL))
     THEN RAISE EXCEPTION 'undo 6 name, link, pointer'; END IF;
  IF EXISTS (SELECT 1 FROM related_subject_documents WHERE subject_id = s AND sha256 = ANY (h.moved_documents))
     THEN RAISE EXCEPTION 'undo 7 S document'; END IF;
  UPDATE related_subject_identifiers SET subject_id = s WHERE subject_id = t;
  GET DIAGNOSTICS n = ROW_COUNT; IF n <> 1 THEN RAISE EXCEPTION 'undo 8'; END IF;
  UPDATE related_subject_documents SET subject_id = s WHERE subject_id = t;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> cardinality(h.moved_documents) THEN RAISE EXCEPTION 'undo 9'; END IF;
  DELETE FROM related_subjects WHERE id = t;   -- the history row goes by cascade (9.2 P4)
  GET DIAGNOSTICS n = ROW_COUNT; IF n <> 1 THEN RAISE EXCEPTION 'undo 10'; END IF;
END $$;"""


def undo_block(s: int, t: int) -> str:
    return UNDO_BLOCK.replace("<S>", str(s)).replace("<T>", str(t))


async def undo_world(conn):
    """A detachment through the service (S keeps h3, T gets h1 and h2) and a bystander."""
    n = 4252001
    s = await seed_owner(conn, n, docs=(1, 2, 3))
    by = await seed_subject(conn, "person", "ZzUndo Bystander", [("passport", passport(4252901))])
    k, d = owner_keys(n)
    before = await digests(conn)
    answer = await detach_identifier(conn, detach_input(req(k, d, owner_docs(n))), dry_run=False)
    await forget_writes(conn)
    return s, answer["to_id"], by, owner_docs(n), before


@pytest.mark.asyncio
async def test_d33_the_undo_block_restores_every_registry_tables_digest(conn, detach_writes):
    s, t, _by, _listed, before = await undo_world(conn)
    assert await digests(conn) != before
    async with unmeasured(conn):
        await conn.execute(undo_block(s, t))
    assert await digests(conn) == before
    assert await history(conn) == []


async def _seed_fault(conn, check, s, t, by, listed):
    """The state in which check `check` is the FIRST guard that fails; returns (S, T) to send."""
    async with unmeasured(conn):
        if check == 1:
            return t, t
        if check == 2:
            return s, t + 100000
        if check == 3:
            return by, t
        if check == 4:
            await conn.execute(
                "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, "
                "subject_id) VALUES ('passport', 'ZZ4252902', 'ZZ4252902', 1, $1)",
                t,
            )
        elif check == 5:
            # an equal-cardinality replacement: one moved hash leaves T, a new one arrives
            await conn.execute(
                "UPDATE related_subject_documents SET subject_id = $1 "
                "WHERE subject_id = $2 AND sha256 = $3",
                by, t, listed[1],
            )  # fmt: skip
        elif check == 6:
            await conn.execute(
                "INSERT INTO related_subject_names (subject_id, name, source, evidence, "
                "created_by) VALUES ($1, 'ZzUndo Name', 'ocr_single', '{}'::jsonb, 'zz')",
                t,
            )
        elif check == 7:
            await conn.execute(
                "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
                "created_by) VALUES ($1, $2, 'passport', '{}'::jsonb, 'zz')",
                s, listed[0],
            )  # fmt: skip
    if check == 5:
        await seed_document(conn, t, 4252999)
    return s, t


UNDO_MESSAGES = {
    1: "undo 1 S=T",
    2: "undo 2 row",
    3: "undo 3 history",
    4: "undo 4 key",
    5: "undo 5 documents",
    6: "undo 6 name, link, pointer",
    7: "undo 7 S document",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("check", sorted(UNDO_MESSAGES))
async def test_d33_each_guard_alone_aborts_with_its_message_and_changes_nothing(
    conn, detach_writes, check
):
    s, t, by, listed, _before = await undo_world(conn)
    send_s, send_t = await _seed_fault(conn, check, s, t, by, listed)
    faulted = await digests(conn)
    async with unmeasured(conn):
        with pytest.raises(asyncpg.PostgresError) as raised:
            await conn.execute(undo_block(send_s, send_t))
    assert str(raised.value) == UNDO_MESSAGES[check]
    assert await digests(conn) == faulted


# The clauses of checks 4 and 6 that the faults above never make the FIRST failing guard: T's one
# key of another type (4), a link on T, a record merged into T, T merged (6).
SUB_CLAUSE_FAULTS = {
    "4_the_key_of_t_has_another_type": (
        "UPDATE related_subject_identifiers SET id_type = 'passport' WHERE subject_id = $1",
        "undo 4 key",
    ),
    "6_a_link_on_t": (
        "INSERT INTO related_subject_links (subject_id, client_id, basis, evidence, created_by) "
        "VALUES ($1, 424242, 'identity', '{}'::jsonb, 'zz')",
        "undo 6 name, link, pointer",
    ),
    "6_a_record_merged_into_t": (
        "INSERT INTO related_subjects (kind, merged_into, evidence, created_by) "
        "VALUES ('person', $1, '{}'::jsonb, 'zz')",
        "undo 6 name, link, pointer",
    ),
    "6_t_merged_into_the_bystander": (
        "UPDATE related_subjects SET merged_into = $2 WHERE id = $1",
        "undo 6 name, link, pointer",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", sorted(SUB_CLAUSE_FAULTS))
async def test_d33_each_clause_of_checks_4_and_6_alone_aborts_with_its_message(
    conn, detach_writes, fault
):
    s, t, by, _listed, _before = await undo_world(conn)
    sql, message = SUB_CLAUSE_FAULTS[fault]
    async with unmeasured(conn):
        await conn.execute(sql, *((t, by) if "$2" in sql else (t,)))
    faulted = await digests(conn)
    async with unmeasured(conn):
        with pytest.raises(asyncpg.PostgresError) as raised:
            await conn.execute(undo_block(s, t))
    assert str(raised.value) == message
    assert await digests(conn) == faulted


# ── D23: concurrency (T4), driven by the controller ───────────────────────────
#
# S9 (a documents registration re-sending a listed hash resolves before the detachment commits and
# reads its links after) is a declared limit of the specification (P3, 10): not tested.

MERGE_WRITES = {("related_subject_merges", "INSERT"), ("related_subjects", "UPDATE")}


async def _detach_then(pool, first_data, second_data, *, hold_first_at=IH_SQL):
    """The first detachment holds after its history INSERT (the owner locked); the second starts,
    must be seen blocked at L; the first commits. Returns both answers and both spies."""
    first, second = _Spy(), _Spy()
    hold = _Point(first, hold_first_at, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2, pool.acquire() as observer:
        try:
            tasks.append(apply_task(c1, first, first_data))
            await _wait(hold.reached, "the first after its history INSERT", *tasks)
            tasks.append(apply_task(c2, second, second_data))
            await _blocked(observer, c2.get_server_pid(), *tasks)
            assert second.names() == ["O", "L"]
            hold.release.set()
            won = await asyncio.wait_for(tasks[0], CAP)
            lost = await asyncio.wait_for(tasks[1], CAP)
        finally:
            await _settle(tasks, [hold])
    return won, lost, first, second


@pytest.mark.asyncio
async def test_d23_s1_the_same_detachment_twice_the_second_is_already_detached(
    pool, conn, detach_writes
):
    s = await seed_owner(conn, 4253001)
    k, d = owner_keys(4253001)
    data = req(k, d, owner_docs(4253001))
    won, lost, _, second = await _detach_then(pool, data, data)
    t = won["to_id"]
    assert won == detached(s, t) and lost == already(s, t)
    assert second.names() == ["O", "L", "O", "WH"]
    assert len(await history(conn)) == 1 and await subject_count(conn) == 2


@pytest.mark.asyncio
async def test_d23_s2_another_list_second_is_detached_with_other_documents(
    pool, conn, detach_writes
):
    s = await seed_owner(conn, 4253002)
    k, d = owner_keys(4253002)
    first = req(k, d, owner_docs(4253002, (1,)))
    won, lost, _, _ = await _detach_then(pool, first, req(k, d, owner_docs(4253002)))
    t = won["to_id"]
    assert won == detached(s, t) and lost == refused("detached_with_other_documents", s, t)
    assert await docs_of(conn, t) == owner_docs(4253002, (1,))
    assert await docs_of(conn, s) == owner_docs(4253002, (2,))


@pytest.mark.asyncio
async def test_d23_s3_the_detachment_wins_and_a_merge_into_the_owner_then_merges(
    pool, conn, detach_writes
):
    detach_writes.update(MERGE_WRITES)
    s = await seed_owner(conn, 4253003)
    k, d = owner_keys(4253003)
    q = ("permit", permit(4253003))
    x = await seed_subject(conn, "person", None, [q])
    dspy, mspy = _Spy(), _Spy()
    hold = _Point(dspy, IH_SQL, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2, pool.acquire() as observer:
        try:
            tasks.append(apply_task(c1, dspy, req(k, d, owner_docs(4253003))))
            await _wait(hold.reached, "the detachment after its history INSERT", *tasks)
            tasks.append(
                asyncio.ensure_future(
                    record_merge(_SpyConn(c2, mspy), merge_input([q], k), dry_run=False)
                )
            )
            await _blocked(observer, c2.get_server_pid(), *tasks)
            assert mspy.sqls()[-1] == merges_module._LOCK_SQL
            hold.release.set()
            won = await asyncio.wait_for(tasks[0], CAP)
            lost = await asyncio.wait_for(tasks[1], CAP)
        finally:
            await _settle(tasks, [hold])
    t = won["to_id"]
    assert won == detached(s, t) and lost == merged(x, s)
    assert await keys_of(conn, s) == [k[0]] and await keys_of(conn, t) == [d]
    assert await conn.fetchval("SELECT merged_into FROM related_subjects WHERE id = $1", x) == s


@pytest.mark.asyncio
async def test_d23_s4_a_merge_into_the_owner_wins_and_the_survivor_then_detaches(
    pool, conn, detach_writes
):
    detach_writes.update(MERGE_WRITES)
    s = await seed_owner(conn, 4253004)
    k, d = owner_keys(4253004)
    q = ("permit", permit(4253004))
    x = await seed_subject(conn, "person", None, [q])
    dspy, mspy = _Spy(), _Spy()
    hold = _Point(mspy, merges_module._UPDATE_POINTER_SQL, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2, pool.acquire() as observer:
        try:
            tasks.append(
                asyncio.ensure_future(
                    record_merge(_SpyConn(c1, mspy), merge_input([q], k), dry_run=False)
                )
            )
            await _wait(hold.reached, "the merge after its pointer UPDATE", *tasks)
            tasks.append(apply_task(c2, dspy, req(k, d, owner_docs(4253004))))
            await _blocked(observer, c2.get_server_pid(), *tasks)
            assert dspy.names() == ["O", "L"]
            hold.release.set()
            won = await asyncio.wait_for(tasks[0], CAP)
            lost = await asyncio.wait_for(tasks[1], CAP)
        finally:
            await _settle(tasks, [hold])
    t = lost["to_id"]
    assert won == merged(x, s) and lost == detached(s, t)
    assert await conn.fetchval("SELECT merged_into FROM related_subjects WHERE id = $1", s) is None


@pytest.mark.asyncio
async def test_d23_s5_a_merge_of_the_owner_resolves_the_detachment_is_n3_the_merge_is_refused(
    pool, conn, detach_writes
):
    detach_writes.update(MERGE_WRITES)
    y = await seed_subject(conn, "person", "ZzS5 Target", [("passport", passport(4253005))])
    q, dk = ("permit", permit(4253005)), ("nik", nik("150680", 3005))
    s = await seed_subject(conn, "person", None, [q, dk], dob=DOB)
    mspy = _Spy()
    hold = _Point(mspy, merges_module._LOCK_SQL, 1)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(
                asyncio.ensure_future(
                    record_merge(
                        _SpyConn(c1, mspy),
                        merge_input([q, dk], [("passport", passport(4253005))]),
                        dry_run=False,
                    )
                )
            )
            await _wait(hold.reached, "the merge resolved, before its first lock", *tasks)
            refused_answer = await asyncio.wait_for(
                detach_identifier(c2, detach_input(req((q, dk), dk)), dry_run=False), CAP
            )
            hold.release.set()
            lost = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    assert refused_answer == refused("owner_left_permit_only", s)
    assert lost == {
        "outcome": "not_mergeable",
        "reason": "basis_precondition",
        "source_id": s,
        "target_id": y,
    }
    assert await ledger(conn) == [] and await history(conn) == []
    assert await keys_of(conn, s) == sorted([q, dk])


@pytest.mark.asyncio
async def test_d23_s6_registrations_of_the_key_and_of_the_set_before_and_after_the_commit(
    pool, conn, detach_writes
):
    s = await seed_owner(conn, 4253006)
    k, d = owner_keys(4253006)
    dspy = _Spy()
    hold = _Point(dspy, IH_SQL, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c3:
        try:
            tasks.append(apply_task(c1, dspy, req(k, d, owner_docs(4253006))))
            await _wait(hold.reached, "the detachment after its history INSERT", *tasks)
            # not blocked: both answer while the detachment holds the owner
            one_before = await asyncio.wait_for(
                register_subject(c3, subject_input([d]), dry_run=False), CAP
            )
            set_before = await asyncio.wait_for(
                register_subject(c3, subject_input(k), dry_run=False), CAP
            )
            hold.release.set()
            won = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    t = won["to_id"]
    assert won == detached(s, t)
    assert one_before == {
        "outcome": "conflict",
        "reason": "set_differs",
        "subject_id": s,
        "dry_run": False,
    }
    assert set_before == {
        "outcome": "already_registered",
        "subject_id": s,
        "match_basis": "identifiers_only",
        "dry_run": False,
    }
    one_after = await register_subject(conn, subject_input([d]), dry_run=False)
    set_after = await register_subject(conn, subject_input(k), dry_run=False)
    assert one_after == {
        "outcome": "already_registered",
        "subject_id": t,
        "match_basis": "identifiers_only",
        "dry_run": False,
    }
    assert set_after == {"outcome": "conflict", "reason": "two_owners", "dry_run": False}


async def _race_with_registration(
    pool, data, run_registration, *, hold_sql, blocked_sql, detach_first
):
    """A detachment and a registration that inserts a NEW child row of the owner.

    Detachment first: it holds after its history INSERT; the registration resolves completely and
    is held BEFORE `hold_sql`, then released, and must be seen blocked at `blocked_sql` (its foreign
    key check on the locked owner row); the detachment commits.
    Registration first: it holds AFTER `hold_sql`; the detachment must be seen blocked at L; the
    registration commits. Returns (registration's answer, detachment's answer, both spies)."""
    rspy, dspy = _Spy(), _Spy()
    tasks: list = []
    points: list = []
    async with pool.acquire() as cr, pool.acquire() as cd, pool.acquire() as observer:
        try:
            if detach_first:
                dhold = _Point(dspy, IH_SQL, after=True)
                rhold = _Point(rspy, hold_sql, 1)
                points = [dhold, rhold]
                detach_task = apply_task(cd, dspy, data)
                tasks.append(detach_task)
                await _wait(dhold.reached, "the detachment after its history INSERT", *tasks)
                registration_task = asyncio.ensure_future(run_registration(cr, rspy))
                tasks.append(registration_task)
                await _wait(rhold.reached, "the registration resolved, before its INSERT", *tasks)
                rhold.release.set()
                await _blocked(observer, cr.get_server_pid(), *tasks)
                assert rspy.sqls()[-1] == blocked_sql
                dhold.release.set()
            else:
                rhold = _Point(rspy, hold_sql, 1, after=True)
                points = [rhold]
                registration_task = asyncio.ensure_future(run_registration(cr, rspy))
                tasks.append(registration_task)
                await _wait(rhold.reached, "the registration after its INSERT", *tasks)
                detach_task = apply_task(cd, dspy, data)
                tasks.append(detach_task)
                await _blocked(observer, cd.get_server_pid(), *tasks)
                assert dspy.names() == ["O", "L"]
                rhold.release.set()
            registration = await asyncio.wait_for(registration_task, CAP)
            detachment = await asyncio.wait_for(detach_task, CAP)
        finally:
            await _settle(tasks, points)
    return registration, detachment, rspy, dspy


DOCUMENT_WRITES = {("related_documents", "INSERT"), ("related_subject_documents", "INSERT")}


@pytest.mark.asyncio
async def test_d23_s7_a_documents_registration_of_a_new_document_waits_for_the_detachment(
    pool, conn, detach_writes
):
    detach_writes.update(DOCUMENT_WRITES)
    s = await seed_owner(conn, 4253007)
    k, d = owner_keys(4253007)
    registration, detachment, rspy, _ = await _race_with_registration(
        pool,
        req(k, d, owner_docs(4253007)),
        lambda c, spy: register_documents(
            _SpyConn(c, spy), documents_input(k, [4253777]), dry_run=False
        ),
        hold_sql=documents_module._INSERT_DOC_SQL,
        blocked_sql=documents_module._INSERT_LINK_SQL,
        detach_first=True,
    )
    t = detachment["to_id"]
    assert detachment == detached(s, t)
    assert registration == {
        "outcome": "linked",
        "subject_id": s,
        "counts": {"documents": 1, "sightings": 0, "links": 1, "descriptive_differences": 0},
        "dry_run": False,
    }
    assert rspy.count(O_SQL) == 1  # resolved once, before the detachment committed
    assert doc(4253777) in await docs_of(conn, s)
    assert await docs_of(conn, t) == sorted(owner_docs(4253007))


@pytest.mark.asyncio
async def test_d23_s8_a_listed_document_linked_by_a_registration_first_is_moved_after_it(
    pool, conn, detach_writes
):
    detach_writes.update(DOCUMENT_WRITES)
    s = await seed_owner(conn, 4253008, docs=())
    k, d = owner_keys(4253008)
    x = doc(4253888)
    registration, detachment, _, dspy = await _race_with_registration(
        pool,
        req(k, d, [x]),
        lambda c, spy: register_documents(
            _SpyConn(c, spy), documents_input(k, [4253888]), dry_run=False
        ),
        hold_sql=documents_module._INSERT_LINK_SQL,
        blocked_sql=None,
        detach_first=False,
    )
    t = detachment["to_id"]
    assert registration["outcome"] == "linked" and registration["subject_id"] == s
    assert detachment == detached(s, t)
    assert dspy.names() == ["O", "L", "O", "KF", "DC", "IS", "MK", "MD", "IH"]
    assert await docs_of(conn, t) == [x] and await docs_of(conn, s) == []


@pytest.mark.asyncio
async def test_d23_s10_another_key_of_the_owner_detached_first_is_two_owners(
    pool, conn, detach_writes
):
    p, d1, d2 = (
        ("passport", passport(4253010)),
        ("nik", nik("150680", 3010)),
        ("nik", nik("160781", 3010)),
    )
    s = await seed_subject(conn, "person", "ZzS10 Owner", [p, d1, d2], dob=DOB)
    spy2 = _Spy()
    hold = _Point(spy2, O_SQL, 1, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(apply_task(c2, spy2, req((p, d1, d2), d2)))
            await _wait(hold.reached, "the second after its first owners read", *tasks)
            won = await asyncio.wait_for(
                detach_identifier(c1, detach_input(req((p, d1, d2), d1)), dry_run=False), CAP
            )
            hold.release.set()
            lost = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    assert won == detached(s, won["to_id"]) and lost == not_registered("two_owners")
    assert spy2.names() == ["O", "L", "O", "WH"] and spy2.args_of(WH_SQL) == [(s,)]
    assert await keys_of(conn, s) == sorted([p, d2])


@pytest.mark.asyncio
async def test_d23_s11_the_key_moved_away_under_a_set_with_an_unowned_key_is_set_differs(
    pool, conn, detach_writes
):
    p, dk = ("passport", passport(4253011)), ("nik", nik("150680", 3011))
    q = ("passport", passport(4253911))  # owned by nobody
    s = await seed_subject(conn, "person", "ZzS11 Owner", [p, dk], dob=DOB)
    spy2 = _Spy()
    hold = _Point(spy2, O_SQL, 1, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(apply_task(c2, spy2, req((q, dk), dk)))
            await _wait(hold.reached, "the second after its first owners read", *tasks)
            won = await asyncio.wait_for(
                detach_identifier(c1, detach_input(req((p, dk), dk)), dry_run=False), CAP
            )
            hold.release.set()
            lost = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    t = won["to_id"]
    assert won == detached(s, t) and lost == not_registered("set_differs")
    assert spy2.names() == ["O", "L", "O", "KF"] and spy2.args_of(KF_SQL) == [(t,)]


@pytest.mark.asyncio
async def test_d23_s12_a_dry_run_reads_one_snapshot_across_a_committed_detachment(
    pool, conn, detach_writes
):
    p, d1, d2 = (
        ("passport", passport(4253012)),
        ("nik", nik("150680", 3012)),
        ("nik", nik("160781", 3012)),
    )
    s = await seed_subject(conn, "person", "ZzS12 Owner", [p, d1, d2], dob=DOB)
    spy2 = _Spy()
    hold = _Point(spy2, O_SQL, 1, after=True)
    tasks: list = []
    async with pool.acquire() as c1, pool.acquire() as c2:
        try:
            tasks.append(apply_task(c2, spy2, req((p, d1, d2), d2, dry_run=True)))
            await _wait(hold.reached, "the dry run after its first owners read", *tasks)
            won = await asyncio.wait_for(
                detach_identifier(c1, detach_input(req((p, d1, d2), d1)), dry_run=False), CAP
            )
            hold.release.set()
            lost = await asyncio.wait_for(tasks[0], CAP)
        finally:
            await _settle(tasks, [hold])
    assert won == detached(s, won["to_id"]) and lost == would(s)
    assert spy2.names() == ["O", "KF", "RW", "DC"]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["registration_first", "detach_first"])
async def test_d23_s13_a_names_registration_and_the_detachment_in_both_orders(
    pool, conn, detach_writes, order
):
    detach_writes.add(("related_subject_names", "INSERT"))
    s = await seed_owner(conn, 4253013)
    k, d = owner_keys(4253013)
    registration, detachment, _, _ = await _race_with_registration(
        pool,
        req(k, d, owner_docs(4253013)),
        lambda c, spy: record_names(_SpyConn(c, spy), names_input(k), dry_run=False),
        hold_sql=names_module._INSERT_NAME_SQL,
        blocked_sql=names_module._INSERT_NAME_SQL,
        detach_first=order == "detach_first",
    )
    t = detachment["to_id"]
    assert detachment == detached(s, t)
    assert registration == {
        "outcome": "recorded",
        "subject_id": s,
        "recorded": 1,
        "already_recorded": 0,
        "dry_run": False,
    }
    owners = await conn.fetch("SELECT subject_id FROM related_subject_names")
    assert [r["subject_id"] for r in owners] == [s]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["registration_first", "detach_first"])
async def test_d23_s14_a_links_registration_and_the_detachment_in_both_orders(
    pool, conn, clients, detach_writes, order
):
    detach_writes.add(("related_subject_links", "INSERT"))
    s = await seed_owner(conn, 4253014)
    k, d = owner_keys(4253014)
    registration, detachment, _, _ = await _race_with_registration(
        pool,
        req(k, d, owner_docs(4253014)),
        lambda c, spy: record_links(_SpyConn(c, spy), links_input(k), dry_run=False),
        hold_sql=links_module._INSERT_LINK_SQL,
        blocked_sql=links_module._INSERT_LINK_SQL,
        detach_first=order == "detach_first",
    )
    t = detachment["to_id"]
    assert detachment == detached(s, t)
    assert registration == {
        "outcome": "recorded",
        "subject_id": s,
        "recorded": 1,
        "already_recorded": 0,
        "dry_run": False,
    }
    owners = await conn.fetch("SELECT subject_id FROM related_subject_links")
    assert [r["subject_id"] for r in owners] == [s]


# ── D30-D31: privacy matrix ───────────────────────────────────────────────────
#
# The helpers are the shared module's (backend/tests/fixtures/registry_privacy.py); this module owns
# the vocabulary of THIS route, the probe world, the cases and the runner.

QRAW1, QRAW2 = passport(7474031), passport(7474032)  # probes of the two raw bodies, stored nowhere
NAME_OWNER, NAME_BYSTANDER = "ZzProbeOwnerName", "ZzProbeBystander"
STORED_DATE = DOB.isoformat()  # the owner's stored birth date
FP_OWN, FP_W = keys_sha256(K_OWN), keys_sha256(K_W)  # the history rows' key digests
# "never a date" (3.2): the stored date as DDMMYY, the DDMMYY digits each probe national number
# encodes, and each (day, month, two-digit year) the rule compares, as Python and as JSON prints it
_PROBE_NIKS = (NO, N2B, N3, N4, NCO, NMG, NPL, NNN, NND, NIV, NMT, NWT)
_RULE_DATES = {(DOB.day, DOB.month, DOB.year % 100)} | ({nik_date(n) for n in _PROBE_NIKS} - {None})
DATE_PROBES = tuple(
    sorted(
        {DOB.strftime("%d%m%y")}
        | {n[6:12] for n in _PROBE_NIKS}
        | {str(x) for x in _RULE_DATES}
        | {json.dumps(list(x)) for x in _RULE_DATES}
    )
)
STORED_PROBES = (
    NAME_OWNER, NAME_BYSTANDER, STORED_DATE, FP_OWN, FP_W, H1, H2, H3, H4,
    PO, NO, PB, P2A, N2B, P3, N3, Q3, P4, N4, Q4, PCO, NCO, PMG, NMG, QPL, NPL,
    NNN, PNN, PND, NND, PIV, NIV, PMT, NMT, PWF, NWT, *DATE_PROBES,
)  # fmt: skip
SENTINEL = "d30-sentinel"

PROTOCOL_ORIGIN = {
    **dict.fromkeys(
        (
            "owner", "kind", "identifiers", "type", "value", "detach", "documents", "basis",
            "evidence", "plan_sha256", "files", "dry_run",
        ),
        "request key",
    ),
    **dict.fromkeys(
        ("person", "company", "passport", "permit", "nik", "npwp", "nib", "ahu", "nik_birth_date"),
        "closed value the model accepts",
    ),
    **dict.fromkeys(("manual", "permit_stamp"), "refused value M10 sends"),
    **dict.fromkeys(("true", "false", "null"), "JSON literal"),
    **dict.fromkeys(("from_id", "to_id"), "fixed output"),
}  # fmt: skip

SERVICE_LOG_WORDS = ("related", "detach", "registry", "outcome", "from_id", "to_id", "dry_run")
ROUTE_FAILURE_WORDS = ("related", "subject", "registry", "failure", "code")
PATH_SEGMENTS = ("api", "crm", "related-subjects", "identifiers", "detach")
DETACH_OUTCOMES = ("detached", "already_detached", "subject_not_registered", "not_detachable")
DETACH_REASONS = (
    "none_owned", "two_owners", "set_differs", "partial_overlap", "kind_differs",
    "owner_is_merged", "owner_left_permit_only", "identifier_not_nik", "owner_has_no_birth_date",
    "nik_date_invalid", "nik_date_matches", "document_not_on_owner",
    "detached_with_other_documents",
)  # fmt: skip
DETAIL_TEXTS = (
    "registry_unavailable", "registry_busy", "unauthorized", "database_error", "internal_error",
)  # fmt: skip
ANSWER_WORDS = frozenset(
    {
        *route_module._DETACH_LOC, *route_module._DETACH_CODE, "invalid", "_",
        "detail", "loc", "code", "outcome", "reason", "from_id", "to_id", "dry_run",
        *DETACH_OUTCOMES, *DETACH_REASONS, *DETAIL_TEXTS,
    }
)  # fmt: skip
VOCAB = privacy.Vocabulary(
    protocol_words=frozenset(PROTOCOL_ORIGIN),
    answer_words=ANSWER_WORDS,
    plan=PLAN,
    unconditional=frozenset(STORED_PROBES),
)
FIXED_WORDS = {
    w.casefold()
    for w in (*ANSWER_WORDS, *SERVICE_LOG_WORDS, *ROUTE_FAILURE_WORDS, URL, *PATH_SEGMENTS)
} | {w.casefold() for w in re.split(r"[^A-Za-z0-9_]+", URL) if w}


async def next_subject_id(c) -> int:
    """The id the next registry record will get (the sequence read, never advanced)."""
    seq = await c.fetchval("SELECT pg_get_serial_sequence('related_subjects', 'id')")
    state = await c.fetchrow(f"SELECT last_value, is_called FROM {seq}")
    return state["last_value"] + 1 if state["is_called"] else state["last_value"]


@pytest_asyncio.fixture
async def probe_world(conn):
    w: dict[str, int] = {}
    w["by"] = await seed_subject(conn, "person", NAME_BYSTANDER, [("passport", PB)], dob=DOB)
    w["own"] = await seed_subject(conn, "person", NAME_OWNER, list(K_OWN), dob=DOB)
    for sid, tag in ((w["own"], 7474001), (w["own"], 7474002), (w["own"], 7474003)):
        await seed_document(conn, sid, tag)
    await seed_document(conn, w["by"], 7474001)  # the bystander links H1
    for name, ids, extra in (
        ("r2a", [("passport", P2A)], {}),
        ("r2b", [("nik", N2B)], {}),
        ("r3", [("passport", P3), ("nik", N3), ("permit", Q3)], {}),
        ("r4", [("passport", P4), ("nik", N4), ("permit", Q4)], {}),
        ("mg", [("passport", PMG), ("nik", NMG)], {"merged": w["by"]}),
        ("pl", [("permit", QPL), ("nik", NPL)], {}),
        ("nn", [("nik", NNN), ("passport", PNN)], {}),
        ("iv", [("passport", PIV), ("nik", NIV)], {}),
        ("mt", [("passport", PMT), ("nik", NMT)], {}),
        ("wf", [("passport", PWF)], {}),
    ):
        w[name] = await seed_subject(conn, "person", None, ids, dob=DOB, **extra)
    w["co"] = await seed_subject(conn, "company", None, [("passport", PCO), ("nik", NCO)])
    w["nd"] = await seed_subject(conn, "person", None, [("passport", PND), ("nik", NND)])
    w["wt"] = await seed_subject(conn, "person", None, [("nik", NWT)])
    await seed_document(conn, w["wt"], 7474004)
    await seed_detach_history(conn, w["wf"], w["wt"], FP_W, [H4])
    w["next"] = await next_subject_id(conn)
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


def _patch_parser(mp):
    def boom(cls, *a, **k):
        raise RuntimeError(f"boom {PO} {NO} {H1}")

    mp.setattr(route_module.RelatedDetachRequest, "model_validate_json", classmethod(boom))


def _patch_database(mp):
    async def boom(*a, **k):
        raise asyncpg.PostgresError(f"boom {PO} {NO} {H1}")

    mp.setattr(route_module, "detach_identifier", boom)


def _patch_inconsistent(mp):
    async def boom(*a, **k):
        raise DetachInconsistent(f"boom {PO} {NO} {H1}")

    mp.setattr(route_module, "detach_identifier", boom)


def _pair(p, n):
    return (("passport", p), ("nik", n))


MAIN_IDS = (PO, NO, H1, H2)

# Family A: cases that reach the service (15 cases, 16 calls)
FAMILY_A = {
    "a_dry_run": _a(lambda w: [({**BASE, "dry_run": True}, would(w["own"]))], MAIN_IDS),
    "a_apply_then_again": _a(
        lambda w: [
            (BASE, detached(w["own"], w["next"])),
            (BASE, already(w["own"], w["next"])),
        ],
        MAIN_IDS,
    ),
    "r1_none_owned": _a(
        lambda w: [(req(_pair(PNONE, NNONE), ("nik", NNONE)), not_registered("none_owned"))],
        (PNONE, NNONE),
    ),
    "r2_two_owners": _a(
        lambda w: [(req(_pair(P2A, N2B), ("nik", N2B)), not_registered("two_owners"))],
        (P2A, N2B),
    ),
    "r3_set_differs": _a(
        lambda w: [(req(_pair(P3, N3), ("nik", N3)), not_registered("set_differs"))],
        (P3, N3),
    ),
    "r4_partial_overlap": _a(
        lambda w: [
            (
                req((*_pair(P4, N4), ("passport", PNONE)), ("nik", N4)),
                not_registered("partial_overlap"),
            )
        ],
        (P4, N4, PNONE),
    ),
    "r5_kind_differs": _a(
        lambda w: [(req(_pair(PCO, NCO), ("nik", NCO)), not_registered("kind_differs"))],
        (PCO, NCO),
    ),
    "n1_owner_is_merged": _a(
        lambda w: [(req(_pair(PMG, NMG), ("nik", NMG)), refused("owner_is_merged", w["mg"]))],
        (PMG, NMG),
    ),
    "n3_owner_left_permit_only": _a(
        lambda w: [
            (
                req((("permit", QPL), ("nik", NPL)), ("nik", NPL)),
                refused("owner_left_permit_only", w["pl"]),
            )
        ],
        (QPL, NPL),
    ),
    "n4_identifier_not_nik": _a(
        lambda w: [
            (
                req((("nik", NNN), ("passport", PNN)), ("passport", PNN)),
                refused("identifier_not_nik", w["nn"]),
            )
        ],
        (NNN, PNN),
    ),
    "n5_owner_has_no_birth_date": _a(
        lambda w: [
            (req(_pair(PND, NND), ("nik", NND)), refused("owner_has_no_birth_date", w["nd"]))
        ],
        (PND, NND),
    ),
    "n6_nik_date_invalid": _a(
        lambda w: [(req(_pair(PIV, NIV), ("nik", NIV)), refused("nik_date_invalid", w["iv"]))],
        (PIV, NIV),
    ),
    "n7_nik_date_matches": _a(
        lambda w: [(req(_pair(PMT, NMT), ("nik", NMT)), refused("nik_date_matches", w["mt"]))],
        (PMT, NMT),
    ),
    "n8_document_not_on_owner": _a(
        lambda w: [(req(K_OWN, D_OWN, [H1, H4]), refused("document_not_on_owner", w["own"]))],
        (PO, NO, H1, H4),
    ),
    "w_detached_with_other_documents": _a(
        lambda w: [
            (
                req(K_W, ("nik", NWT), [H3]),
                refused("detached_with_other_documents", w["wf"], w["wt"]),
            )
        ],
        (PWF, NWT, H3),
    ),
}

# Family B: every entry of D_CASES on the probe body, plus malformed JSON and an oversize body.
# A mutation that removes or replaces a key set changes which probes the body still carries.
_EXTRA_IDS = tuple(v for _, v in EXTRA3)
B_IDS = {
    "owner_missing": (NO, H1, H2),
    "documents_missing": (PO, NO),
    "owner_value_not_a_string_at_index_3": (NO, H1, H2, *_EXTRA_IDS),
    "owner_type_unknown_at_index_3": (NO, H1, H2, *_EXTRA_IDS),
    "owner_identifier_invalid": (NO, H1, H2),
    "owner_identifiers_empty": (NO, H1, H2),
    "owner_twenty_one_identifiers": (NO, H1, H2, *(passport(7474800 + i) for i in range(21))),
    "b1_duplicate_document": (PO, NO, H1),
    "b1_before_b2": (PO, NO, NX, H1),
    "b2_detach_not_in_owner": (PO, NO, NX, H1, H2),
    "b3_owner_keeps_no_identifier": (NO, H1, H2),
    "forty_one_documents": (PO, NO, *FORTY_ONE),
    "document_short": (PO, NO, H2),
    "document_not_a_string": (PO, NO, H2),
    "documents_not_a_list": (PO, NO),
    "fifty_errors_capped": (NO, *(h.lower() for h in UPPER_FORTY)),
}
FAMILY_B = {}
for _key, (_mutator, _expected) in D_CASES.items():
    FAMILY_B["b_" + _key] = _case(
        lambda w, _m=_mutator, _e=_expected: [(_m(BASE), _validation(_e))],
        422,
        B_IDS.get(_key, MAIN_IDS),
        quiet=True,
    )
FAMILY_B["b_malformed_json"] = _case(
    lambda w: [
        (
            b'{"owner": {"kind": "person", "identifiers": [{"type": "passport", "value": "'
            + QRAW1.encode()
            + b'"',
            _validation([([], "json_invalid")]),
        )
    ],
    422,
    (QRAW1,),
    quiet=True,
)
FAMILY_B["b_oversize_body"] = _case(
    lambda w: [
        (
            b" " * (route_module.MAX_BODY_BYTES + 1) + QRAW2.encode(),
            _validation([(["body"], "too_long")]),
        )
    ],
    422,
    (QRAW2,),
    quiet=True,
)

# Family C: injected failures, each raising with a probe (3)
FAMILY_C = {
    "c_parser_failure": _case(
        lambda w: [(BASE, {"detail": "internal_error"})],
        500, MAIN_IDS, route=True, patch=_patch_parser,
    ),
    "c_database_failure": _case(
        lambda w: [(BASE, {"detail": "database_error"})],
        503, MAIN_IDS, route=True, patch=_patch_database,
    ),
    "c_detach_inconsistent": _case(
        lambda w: [(BASE, {"detail": "internal_error"})],
        500, MAIN_IDS, route=True, patch=_patch_inconsistent,
    ),
}  # fmt: skip

D30_CASES = {**FAMILY_A, **FAMILY_B, **FAMILY_C}
FAKE_WORLD = {
    k: i + 1
    for i, k in enumerate(
        ("by", "own", "r2a", "r2b", "r3", "r4", "mg", "pl", "nn", "iv", "mt", "wf", "co", "nd")
        + ("wt", "next")
    )
}
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def _steps(case, world):
    return D30_CASES[case]["build"](world)


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


def test_d31_the_matrix_has_every_case_of_its_specification():
    assert len(FAMILY_A) == 15 and len(FAMILY_C) == 3
    assert len(FAMILY_B) == len(D_CASES) + 2
    assert len(D30_CASES) == len(D_CASES) + 20
    calls = sum(len(_steps(case, FAKE_WORLD)) for case in D30_CASES)
    assert calls == len(D_CASES) + 21
    assert privacy.unclassified_protocol_words(VOCAB, PROTOCOL_ORIGIN) == []
    assert len([w for w, c in PROTOCOL_ORIGIN.items() if c == "request key"]) == 12
    assert PROTOCOL_ORIGIN["nik_birth_date"] == "closed value the model accepts"
    assert set(PROTOCOL_ORIGIN) >= {"from_id", "to_id"}
    assert "created_by" not in VOCAB.protocol_words and "to_id" in VOCAB.protocol_words
    assert len(DETACH_REASONS) == 13 and len(DETAIL_TEXTS) == 5 and len(DETACH_OUTCOMES) == 4
    # no word of the fixed answers, the fixed log lines or the endpoint is a stored probe
    assert privacy.probe_forms(STORED_PROBES) & FIXED_WORDS == set()


def test_d31_hashes_the_key_digests_and_the_stored_date_are_probes_with_long_digit_forms():
    hashes = (H1, H2, H3, H4, *FORTY_ONE, *(h.lower() for h in UPPER_FORTY))
    for value in (*hashes, FP_OWN, FP_W):
        assert len(_digits(value)) >= 6, value
        assert _digits(value) in privacy.probe_forms([value]), value
    assert {H1, H2, H3, H4, FP_OWN, FP_W, STORED_DATE} <= set(STORED_PROBES)
    assert _digits(STORED_DATE) in privacy.probe_forms([STORED_DATE])
    # the owner's digest is the 2.1 digest of the probe owner's keys
    assert FP_OWN == fingerprint(*K_OWN) and FP_W == fingerprint(*K_W)


@pytest.mark.parametrize("case", sorted(D30_CASES))
def test_d31_first_tests_probes_declared_present_and_collision_free_and_nothing_suppressed(case):
    spec = D30_CASES[case]
    steps = _steps(case, FAKE_WORLD)
    requests = [request for request, _ in steps]
    declared = spec["ids"]
    # R1: every declared probe is in the raw text of a request; a raw body holds nothing undeclared
    assert privacy.missing_declared(requests, declared) == [], case
    for request in requests:
        if isinstance(request, bytes):
            assert privacy.undeclared_runs(VOCAB, request, declared) == [], case
    # each listed hash is declared
    declared_folded = {p.casefold() for p in declared}
    for request in requests:
        listed = request.get("documents") if isinstance(request, dict) else None
        for item in listed if isinstance(listed, list) else ():
            if isinstance(item, str) and HEX64.fullmatch(item):
                assert item.casefold() in declared_folded, (case, item)
    log_set, answer_set = privacy.case_sets(VOCAB, requests, declared, ())
    # every declared probe is forbidden, in every non-empty form, on every surface
    assert privacy.probe_forms(declared) <= log_set and privacy.probe_forms(declared) <= answer_set
    assert privacy.probe_forms(STORED_PROBES) <= log_set
    # R4: no form of a probe (hashes and the stored date included) can be, or be inside, a
    # legitimate value of the case
    legitimate: set[str] = {str(spec["status"]), *VOCAB.protocol_words, *FIXED_WORDS}
    for _, expected in steps:
        strings: set[str] = set()
        ints: set[int] = set()
        privacy.leaves(expected, strings, ints)
        legitimate |= {s.casefold() for s in strings} | {str(i) for i in ints}
        legitimate.add(json.dumps(expected).casefold())
    probes = privacy.probe_forms([*declared, *STORED_PROBES])
    assert privacy.collisions(probes, legitimate) == [], case
    # R4: no word of the fixed answers, log lines or endpoint is forbidden
    assert (log_set | answer_set) & FIXED_WORDS == set(), case
    # R8: the vocabulary filter must not have removed a request string from either set
    for request in requests:
        assert privacy.suppressed(VOCAB, request, log_set, answer_set) == [], case


def _spoke(records, module, sentinels=()):
    return [r for r in records if r.name == module.__name__ and not any(r is s for s in sentinels)]


def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord("zz.teeth", logging.INFO, __file__, 1, "teeth %s", ("plain",), None)
    record.__dict__.update(extra)
    return record


AUDIT_TYPES = {
    "id": "integer",
    "error_message": "text",
    "response_body": "jsonb",
    "request_body": "jsonb",
    "created_at": "timestamp without time zone",
}
TEETH_PROBE = passport(7474041)  # stored nowhere: only the declaration can put it in a set
TEETH_REQUESTS = {
    "dict": req((("passport", TEETH_PROBE), D_OWN), D_OWN),
    "raw": b'{"owner": "' + TEETH_PROBE.encode() + b'"',
}


def _teeth_answer_set(kind):
    _, answer = privacy.case_sets(VOCAB, [TEETH_REQUESTS[kind]], [TEETH_PROBE], ())
    return answer


def test_d31_teeth_a_short_numeric_form_collides_and_the_probe_forms_do_not():
    assert privacy.collisions({"12"}, {"412"}) == [("12", "412")]
    assert privacy.collisions(privacy.probe_forms([PO, H1, STORED_DATE]), {"200", "422", "1"}) == []


@pytest.mark.parametrize("kind", ["dict", "raw"])
def test_d31_teeth_an_identifier_in_an_audit_error_message_is_caught(kind):
    row = {"id": 1, "error_message": f"boom {TEETH_PROBE}", "created_at": datetime.now()}
    assert privacy.leaks(_teeth_answer_set(kind), privacy.audit_texts([row], AUDIT_TYPES))
    clean = {"id": 1, "error_message": "boom", "created_at": datetime.now()}
    assert privacy.leaks(_teeth_answer_set(kind), privacy.audit_texts([clean], AUDIT_TYPES)) == []


def test_d31_teeth_a_hash_its_digit_form_and_a_key_digest_are_caught_in_a_log_record():
    forbidden = privacy.probe_forms([H1, FP_OWN, STORED_DATE])
    for value in (H1, H1.upper(), _digits(H1), FP_OWN, STORED_DATE, _digits(STORED_DATE)):
        assert privacy.leaks(forbidden, privacy.log_surface([_record(extra={"v": value})])), value
    assert privacy.leaks(forbidden, privacy.log_surface([_record()])) == []


def test_d31_teeth_a_date_the_rule_reads_is_caught_in_a_log_line_and_in_an_answer():
    assert set(DATE_PROBES) <= set(STORED_PROBES)
    assert DATE_PROBES == (
        "(12, 3, 77)", "(15, 6, 80)", "000377", "120377", "150680", "[12, 3, 77]", "[15, 6, 80]",
    )  # fmt: skip
    forbidden = privacy.probe_forms(DATE_PROBES)
    owner_date = (DOB.day, DOB.month, DOB.year % 100)
    for value in (nik_date(NO), owner_date):  # a line that formats the compared tuple
        line = logging.LogRecord("zz.teeth", logging.INFO, __file__, 1, "zz %s", (value,), None)
        assert privacy.leaks(forbidden, privacy.log_surface([line])), value
    for value in (DOB.strftime("%d%m%y"), NO[6:12], NIV[6:12]):
        assert privacy.leaks(forbidden, privacy.log_surface([_record(extra={"v": value})])), value
    for answer in ({"encoded": list(nik_date(NO))}, {"dob": DOB.strftime("%d%m%y")}):
        assert privacy.leaks(forbidden, privacy.response_surface(json.dumps(answer))), answer
    assert privacy.leaks(forbidden, privacy.log_surface([_record()])) == []
    clean = json.dumps({"outcome": "detached", "from_id": 2, "to_id": 16, "dry_run": False})
    assert privacy.leaks(forbidden, privacy.response_surface(clean)) == []


def test_d30_teeth_clock_and_random_log_values_are_masked_inside_the_call_window():
    low = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    high = low + timedelta(seconds=2)
    token = "000377"
    request_timestamp = "2030-01-02T03:04:06.000377+00:00"
    request_uuid = "000377aa-0000-4000-8000-000000000000"
    records = [
        _record(extra={"timestamp": request_timestamp}),
        logging.LogRecord(
            "zz.teeth", logging.WARNING, __file__, 1, "[%s] POST", (request_uuid,), None
        ),
    ]

    assert privacy.leaks({token}, privacy.log_surface(records)) == [token]
    assert privacy.leaks({token}, privacy.log_surface(records, clock_window=(low, high))) == []


def test_d30_teeth_nonvolatile_values_stay_scanned_when_clock_values_are_masked():
    low = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    high = low + timedelta(seconds=2)
    token = "000377"
    for value in (
        "prefix-000377-suffix",
        "000377aa-0000-1000-8000-000000000000",
        "2020-01-02T03:04:05.000377+00:00",
    ):
        record = _record(extra={"value": value})
        assert privacy.leaks({token}, privacy.log_surface([record])) == [token], value
        assert privacy.leaks({token}, privacy.log_surface([record], clock_window=(low, high))) == [
            token
        ], value


def test_d31_teeth_an_undeclared_run_in_a_raw_body_is_caught():
    raw = TEETH_REQUESTS["raw"] + b" ZZ9191919"
    assert privacy.undeclared_runs(VOCAB, raw, [TEETH_PROBE]) == ["ZZ9191919"]
    assert privacy.undeclared_runs(VOCAB, TEETH_REQUESTS["raw"], [TEETH_PROBE]) == []


def test_d31_teeth_the_provenance_check_fails_on_an_unclassified_word():
    grown = privacy.Vocabulary(VOCAB.protocol_words | {"newword"}, ANSWER_WORDS, PLAN, frozenset())
    assert privacy.unclassified_protocol_words(grown, PROTOCOL_ORIGIN) == ["newword"]
    origin = {k: v for k, v in PROTOCOL_ORIGIN.items() if k != "nik_birth_date"}
    assert privacy.unclassified_protocol_words(VOCAB, origin) == ["nik_birth_date"]


def test_d31_teeth_the_suppression_guard_fails_when_the_filter_removes_a_request_string():
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
@pytest.mark.parametrize("case", sorted(D30_CASES))
async def test_d30_route_logs_answers_and_audit_carry_no_identifier_hash_name_or_body(
    case, client, conn, probe_world, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)
    spec = D30_CASES[case]
    steps = spec["build"](probe_world)
    requests = [request for request, _ in steps]

    # the capture is live for both modules, at any level
    logging.getLogger(route_module.__name__).debug(SENTINEL)
    logging.getLogger(detach_module.__name__).debug(SENTINEL)
    sentinels = [r for r in caplog.records if r.getMessage() == SENTINEL]
    assert sorted(r.name for r in sentinels) == sorted(
        [route_module.__name__, detach_module.__name__]
    ), case

    if spec["patch"]:
        spec["patch"](monkeypatch)
    low = await conn.fetchval("SELECT clock_timestamp()::timestamp")
    responses = [await post(client, request) for request in requests]
    high = await conn.fetchval("SELECT clock_timestamp()::timestamp")

    for resp, (_, expected) in zip(responses, steps, strict=True):
        assert resp.status_code == spec["status"], (case, resp.status_code, resp.text[:120])
        assert resp.json() == expected, case

    # the logger that must have spoken did; a validation case speaks through neither
    records = [r for r in caplog.records if not privacy.is_http_client_logger(r.name)]
    if spec["service"]:
        assert _spoke(records, detach_module, sentinels), (case, "service logger silent")
    if spec["route"]:
        assert _spoke(records, route_module, sentinels), (case, "route logger silent")
    if spec["quiet"]:
        assert not _spoke(records, detach_module, sentinels), case
        assert not _spoke(records, route_module, sentinels), case

    log_set, answer_set = privacy.case_sets(VOCAB, requests, spec["ids"], ())
    for resp in responses:
        assert privacy.leaks(answer_set, privacy.response_surface(resp.text)) == [], case
    # the clock-window mask never hides a probe: no forbidden token is itself a uuid4
    assert [t for t in log_set if privacy.UUID4.search(t)] == [], case
    assert privacy.leaks(log_set, privacy.log_surface(records, sentinels, (low, high))) == [], case

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
