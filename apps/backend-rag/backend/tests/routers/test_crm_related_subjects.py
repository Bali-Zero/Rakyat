"""Related-subject registry, slice 1: every acceptance criterion on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database, applies migration 361's forward part
and reduced CRM tables, and runs the registry service or the real route (with the real
middleware stack) against it. Synthetic placeholders only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import traceback
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
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
from backend.services.crm.related_documents import register_documents
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
MIGRATION2 = MIGRATIONS / "362_related_documents.sql"
URL = "/api/crm/related-subjects/register"
URL2 = "/api/crm/related-subjects/documents/register"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
SHA = "5f3c" * 16
REF = "REF-7319"
TIER = "TIER-Q9"
NEW_TABLES = {"related_subjects", "related_subject_identifiers"}
DOC_TABLES = {"related_documents", "related_document_sightings", "related_subject_documents"}
ALL_REGISTRY_TABLES = frozenset(NEW_TABLES | DOC_TABLES)
REGISTRY_TABLES = ALL_REGISTRY_TABLES
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
# updates and insert-then-delete included, and every TRUNCATE (statement-level; a CASCADE fires
# the trigger of each table it reaches). Row-level, so a statement that touches no row
# records nothing. The whole test is measured: see the `pool` fixture and `writes(...)`.
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


async def log_mark(c) -> int:
    return await c.fetchval("SELECT coalesce(max(id), 0) FROM write_log")


async def written(c, since: int = 0) -> set[str]:
    return {
        r["tbl"] for r in await c.fetch("SELECT DISTINCT tbl FROM write_log WHERE id > $1", since)
    }


async def written_ops(c, since: int = 0) -> Counter:
    """(table, operation) -> number of row writes (a TRUNCATE counts once per table)."""
    rows = await c.fetch(
        "SELECT tbl, op, count(*) AS n FROM write_log WHERE id > $1 GROUP BY 1, 2", since
    )
    return Counter({(r["tbl"], r["op"]): r["n"] for r in rows})


@asynccontextmanager
async def unmeasured(c):
    """The ONE way a test seeds or cleans up mid-test: rows written inside are taken out of the
    measurement. The guard is never switched off; only what happens in this block is excused."""
    mark = await log_mark(c)
    yield
    await c.execute("DELETE FROM write_log WHERE id > $1", mark)


# The operation each table may legitimately receive, by default. The registry is write-once
# (INSERT only); the audit trail is append-only; the activity logger only UPDATEs a session row
# that already exists. Anything else a test does must be declared with `writes.ops(...)`.
DEFAULT_OPS = {
    "related_subjects": {"INSERT"},
    "related_subject_identifiers": {"INSERT"},
    "related_documents": {"INSERT"},
    "related_document_sightings": {"INSERT"},
    "related_subject_documents": {"INSERT"},
    "api_audit_trail": {"INSERT"},
    "session_tracking": {"UPDATE"},
}


class Declared(set):
    """What a test declares it writes. Empty unless the test says otherwise.

    ``writes(*tables)``: the tables written. ``writes.ops(table, *ops)``: operations beyond the
    default ones for that table. ``writes.counts(**inserts)``: exact INSERT row counts.
    """

    def __init__(self):
        super().__init__()
        self.extra_ops: dict[str, set[str]] = {}
        self.expected_counts: dict[str, int] = {}

    def __call__(self, *tables: str) -> None:
        self.update(tables)

    def ops(self, table: str, *ops: str) -> None:
        self.extra_ops.setdefault(table, set()).update(ops)

    def counts(self, **inserts: int) -> None:
        self.expected_counts.update(inserts)


def check_declared_writes(actual: set[str], declared: set[str]) -> None:
    """The teardown assertion of every PostgreSQL test: written tables == declared tables."""
    assert actual == declared, (
        f"write measurement: wrote {sorted(actual)}, declared {sorted(declared)}"
    )


def check_operations(ops: Counter, declared: Declared) -> None:
    """The operation-level half of the teardown assertion: every (table, operation) written is
    an allowed one (default INSERT-only registry), declared INSERT counts are exact, and every
    a test that declares registry writes MUST declare the exact INSERT counts of both registry
    tables (the omission fails the teardown). A same-value UPDATE, a DELETE or a TRUNCATE of a
    registry table is refused unless the test declared it."""
    for (table, op), n in sorted(ops.items()):
        allowed = DEFAULT_OPS.get(table, set()) | declared.extra_ops.get(table, set())
        assert op in allowed, (
            f"write measurement: {op} on {table} ({n} row writes) is not allowed "
            f"(allowed: {sorted(allowed)})"
        )
    for table in sorted(REGISTRY_TABLES & set(declared)):
        assert table in declared.expected_counts, (
            f"write measurement: {table} declared without its exact INSERT count "
            "(writes.counts(related_subjects=..., related_subject_identifiers=...))"
        )
    for table, expected in sorted(declared.expected_counts.items()):
        assert ops[(table, "INSERT")] == expected, (
            f"write measurement: {ops[(table, 'INSERT')]} INSERTs on {table}, declared {expected}"
        )


async def check_no_orphan_subjects(c) -> None:
    """Every subject owns at least one identifier (per subject, not an aggregate balance)."""
    if await c.fetchval("SELECT to_regclass('related_subjects')") is None:
        return  # the rollback test dropped the registry tables: nothing left to own anything
    orphans = await c.fetchval(
        "SELECT count(*) FROM related_subjects s WHERE NOT EXISTS "
        "(SELECT 1 FROM related_subject_identifiers i WHERE i.subject_id = s.id)"
    )
    assert orphans == 0, f"write measurement: {orphans} subject(s) without an identifier"


@pytest.fixture
def writes():
    return Declared()


# ── database fixtures ─────────────────────────────────────────────────────────


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
    schema = "related_subjects_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn, min_size=1, max_size=6, server_settings={"search_path": schema}
    )
    try:
        forward, _ = split_migration_sql(MIGRATION.read_text())
        forward2, _ = split_migration_sql(MIGRATION2.read_text())
        async with created.acquire() as conn:
            await conn.execute(forward)
            await conn.execute(forward2)  # before the measurement triggers: measured from row one
            await conn.execute(CRM_SCHEMA)
            await conn.execute(WRITE_LOG_SQL)
        yield created
        # Default-on write measurement: every test that uses this schema ends here, and the
        # tables it wrote (trigger-fed log, every row write incl. same-value updates and
        # insert-then-delete) must be exactly the ones it declared through `writes(...)`.
        async with created.acquire() as c:
            check_declared_writes(await written(c), set(writes))
            check_operations(await written_ops(c), writes)
            await check_no_orphan_subjects(c)
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


@asynccontextmanager
async def writes_only(c, tables=frozenset(), *, exact_content=False):
    """A narrower window INSIDE a test (the whole test is measured anyway, by the schema fixture).

    The tables that received any row write during the window are exactly ``tables``, and the
    content snapshots show no change outside them (``exact_content``: exactly those changed).
    """
    tables = set(tables)
    before = await snapshot(c)
    mark = await log_mark(c)
    yield
    after = await snapshot(c)
    assert await written(c, mark) == tables
    assert changed(before, after) <= tables
    if exact_content:
        assert changed(before, after) == tables
    if not tables:
        assert after == before


def writes_nothing(c):
    return writes_only(c, set())


# ── builders ──────────────────────────────────────────────────────────────────


def subj(kind="person", ids=(("passport", "ZZ0000001"),), dob=None, name=None, **kw):
    return SubjectInput(
        kind=kind,
        identifiers=prepare_identifiers(kind, [(t, v, {}) for t, v in ids]),
        evidence={"plan_sha256": SHA, "subject_ref": REF, "tier": TIER},
        display_name=name,
        date_of_birth=dob,
        **kw,
    )


def body(kind="person", ids=(("passport", "ZZ0000001"),), **over):
    data = {
        "kind": kind,
        "identifiers": [{"type": t, "value": v} for t, v in ids],
        "evidence": {"plan_sha256": SHA, "subject_ref": REF, "tier": TIER},
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
        self.has_middleware = middleware
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


# ── privacy by derivation: forbidden tokens come from the request itself ─────

# Protocol words that legitimately appear in answers and logs. Everything else a request carries
# (every leaf string and number, every canonical identifier form) is forbidden in every observable.
PROTOCOL_WORDS = frozenset(
    {
        "person", "company", "passport", "permit", "nik", "npwp", "nib", "ahu", "true", "false",
        "null", "kind", "display_name", "date_of_birth", "nationality", "sex", "identifiers",
        "type", "value", "evidence", "plan_sha256", "subject_ref", "tier", "files",
        "independent_files", "strengths", "dry_run",
    }
)  # fmt: skip
MIN_TOKEN = 4  # shorter strings collide with ordinary words and numbers; test values are longer
CLOSED_LOC = frozenset(
    {
        "kind", "display_name", "date_of_birth", "nationality", "sex", "identifiers", "type",
        "value", "evidence", "plan_sha256", "subject_ref", "tier", "files", "independent_files",
        "strengths", "dry_run", "body", "_",
    }
)  # fmt: skip
CLOSED_CODES = frozenset(
    {
        "missing", "extra_forbidden", "string_type", "string_too_short", "string_too_long",
        "string_pattern_mismatch", "bool_type", "int_type", "greater_than_equal",
        "less_than_equal", "date_type", "date_from_datetime_inexact", "date_parsing",
        "literal_error", "list_type", "too_short", "too_long", "model_type", "dict_type",
        "json_invalid", "invalid_text", "invalid_identifier", "kind_type_mismatch",
        "duplicate_identifier", "invalid",
    }
)  # fmt: skip
CLOSED_WORDS = {
    401: {"unauthorized"},
    500: {"internal_error"},
    503: {"registry_unavailable", "registry_busy", "database_error"},
}
CLOSED_OUTCOMES = {
    "created", "would_create", "already_registered", "conflict", "existing_client",
    "existing_family_member", "existing_company",
}  # fmt: skip
CLOSED_REASONS = {"set_differs", "partial_overlap", "two_owners", "contradiction"}
CLOSED_BASES = {"identifiers_only", "identifiers_and_dob"}
CLOSED_200_KEYS = {"outcome", "dry_run", "subject_id", "match_basis", "reason"}


def _leaves(node, out):
    if isinstance(node, dict):
        for value in node.values():
            _leaves(value, out)
        for key in node:  # a caller-chosen KEY is a value too
            out.add(str(key))
    elif isinstance(node, list):
        for item in node:
            _leaves(item, out)
    elif isinstance(node, str):
        out.add(node)
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        out.add(str(node))


def _identifier_forms(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("type"), str) and isinstance(node.get("value"), str):
            canonical = (
                canonical_form(node["type"], node["value"])
                if node["type"] in ("passport", "permit", "nik", "npwp", "nib", "ahu")
                else ""
            )
            out.add(canonical)  # fmt: skip
        for value in node.values():
            _identifier_forms(value, out)
    elif isinstance(node, list):
        for item in node:
            _identifier_forms(item, out)


def forbidden_tokens(payload, protocol=PROTOCOL_WORDS) -> set[str]:
    """Every string/number the request carries, plus canonical identifier forms, minus protocol words."""
    found: set[str] = set()
    parsed = payload
    if isinstance(payload, (bytes, str)):
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
            # not JSON: whole quoted strings (closed or cut off), identifier-like words that
            # carry a digit, and the text itself. Plain words would collide with ordinary text.
            found.update(re.findall(r'"([^"]*)"', text))
            found.update(re.findall(r'"([^"]*)$', text))
            found.update(
                w
                for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]{3,}", text)
                if re.search(r"[0-9]", w)
            )
            found.add(text.strip())
    if parsed is not None:
        _leaves(parsed, found)
        _identifier_forms(parsed, found)
    found.add(KEY)
    return {t.lower() for t in found if len(t) >= MIN_TOKEN and t.lower() not in protocol}


def subject_tokens(subject) -> set[str]:
    found = {subject.display_name, subject.nationality, subject.sex, str(subject.date_of_birth)}
    for ident in subject.identifiers:
        found.update({ident.id_raw, ident.id_norm})
        _leaves(ident.evidence, found)
    _leaves(subject.evidence, found)
    return {
        t.lower()
        for t in found
        if t and t != "None" and len(t) >= MIN_TOKEN and t.lower() not in PROTOCOL_WORDS
    }


@contextmanager
def capture_logs():
    """Capture every record of every logger (propagating or not) at DEBUG."""
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _H(level=logging.DEBUG)
    targets = [logging.getLogger()] + [
        lg
        for lg in logging.Logger.manager.loggerDict.values()
        if isinstance(lg, logging.Logger) and not lg.propagate
    ]
    saved = [(lg, lg.level) for lg in targets]
    for lg in targets:
        lg.addHandler(handler)
    logging.getLogger().setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        for lg, level in saved:
            lg.removeHandler(handler)
            lg.setLevel(level)


def record_text(record: logging.LogRecord) -> str:
    parts = [repr(sorted(vars(record).items(), key=lambda kv: kv[0]))]
    try:
        parts.append(record.getMessage())
    except Exception:
        pass
    if record.exc_info and record.exc_info[0] is not None:
        parts.append("".join(traceback.format_exception(*record.exc_info)))
    return " ".join(parts)


def assert_no_tokens(tokens, label, text):
    hit = sorted(t for t in tokens if t in text.lower())
    assert not hit, (
        f"{label} carries {len(hit)} request-derived token(s): {[h[:3] + '..' for h in hit]}"
    )


async def observe(stack, tokens, response_text, records, trace_before):
    """No request-derived token in ANY observable; nothing reached the middleware."""
    assert_no_tokens(tokens, "response", response_text)
    assert_no_tokens(tokens, "log records", " ".join(record_text(r) for r in records))
    traces = {k: v for k, v in request_tracing._trace_storage.items() if k not in trace_before}
    assert_no_tokens(tokens, "request traces", str(traces))
    assert_no_tokens(tokens, "alert arguments", str(stack.alerts.calls))
    if stack.has_middleware and activity_logger._initialized and activity_logger.pool:
        async with activity_logger.pool.acquire() as c:
            audit = str(await c.fetch("SELECT * FROM api_audit_trail")) + str(
                await c.fetch("SELECT * FROM session_tracking")
            )
        assert_no_tokens(tokens, "audit rows", audit)
    assert stack.sink["raised"] == [] and stack.sink["handlers"] == []


def assert_answer_shape(resp):
    """Closed lists for every answer: 200 keys/values, 422 loc/code, fixed words for 401/500/503."""
    data = resp.json()
    if resp.status_code == 200:
        assert set(data) <= CLOSED_200_KEYS and data["outcome"] in CLOSED_OUTCOMES
        assert data.get("reason") in (None, *CLOSED_REASONS)
        assert data.get("match_basis") in (None, *CLOSED_BASES)
        assert isinstance(data["dry_run"], bool)
        assert isinstance(data.get("subject_id", 0), int)
    elif resp.status_code == 422:
        assert set(data) == {"detail"} and isinstance(data["detail"], list) and data["detail"]
        for item in data["detail"]:
            assert set(item) == {"loc", "code"}
            assert isinstance(item["loc"], list)
            assert all(
                (isinstance(p, int) and not isinstance(p, bool)) or p in CLOSED_LOC
                for p in item["loc"]
            ), item["loc"]
            assert item["code"] in CLOSED_CODES, item["code"]
    else:
        assert resp.status_code in CLOSED_WORDS, resp.status_code
        assert set(data) == {"detail"} and data["detail"] in CLOSED_WORDS[resp.status_code]


async def post(stack, payload, headers=HEADERS, *, url=URL, protocol=PROTOCOL_WORDS, shape=None):
    """Every HTTP call of this file: derives the forbidden tokens from the payload, observes
    every channel, and checks the answer shape against closed lists (per route: slice 2 passes
    its own url, protocol words and shape check)."""
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    tokens = forbidden_tokens(payload, protocol)
    trace_before = set(request_tracing._trace_storage)
    with capture_logs() as records:
        async with stack.client() as http:
            resp = await http.post(url, content=raw, headers=headers)
    await observe(stack, tokens, resp.text + " " + str(resp.headers), records, trace_before)
    (shape or assert_answer_shape)(resp)
    return resp


async def reg(conn, subject, *, dry_run=True):
    """register_subject with the service log checked against the subject's own values."""
    tokens = subject_tokens(subject)
    with capture_logs() as records:
        try:
            return await register_subject(conn, subject, dry_run=dry_run)
        finally:
            assert_no_tokens(
                tokens, "service log records", " ".join(record_text(r) for r in records)
            )


# ── guard on the guard ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_an_undeclared_write_fails_the_teardown_assertion(writes, conn):
    """An INSERT then DELETE on `clients` leaves no row and an equal snapshot, yet the write
    log names the table, and `check_declared_writes` (the very function the schema fixture
    runs at the end of every test) refuses it unless the test declared it.

    Done by calling the checking function directly on the real log of this test: the same
    function, the same log, without a nested pytest session.
    """
    writes("clients")  # declared here only so that THIS test's own teardown passes
    writes.ops("clients", "INSERT", "DELETE")
    before = await snapshot(conn)
    await conn.execute("INSERT INTO clients (full_name) VALUES ('Zz Probe')")
    await conn.execute("DELETE FROM clients WHERE full_name = 'Zz Probe'")
    assert await snapshot(conn) == before
    actual = await written(conn)
    assert actual == {"clients"}
    with pytest.raises(AssertionError, match="wrote \\['clients'\\], declared \\[\\]"):
        check_declared_writes(actual, set())  # the empty (default) declaration
    with pytest.raises(AssertionError):
        check_declared_writes(set(), {"clients"})  # a declared write that did not happen
    check_declared_writes(actual, set(writes))


@pytest.mark.asyncio
async def test_guard_a_truncate_is_a_write_and_a_cascade_names_every_table_it_reaches(writes, conn):
    await conn.execute("TRUNCATE clients")
    plain = await written(conn)
    assert plain == {"clients"}
    with pytest.raises(AssertionError, match="wrote \\['clients'\\], declared \\[\\]"):
        check_declared_writes(plain, set())
    mark = await log_mark(conn)
    await conn.execute(
        "ALTER TABLE client_family_members ADD FOREIGN KEY (client_id) REFERENCES clients(id)"
    )
    await conn.execute("TRUNCATE clients CASCADE")
    cascaded = await written(conn, mark)
    assert cascaded == {"clients", "client_family_members"}
    with pytest.raises(AssertionError):
        check_declared_writes(cascaded, {"clients"})
    writes("clients", "client_family_members")
    writes.ops("clients", "TRUNCATE")
    writes.ops("client_family_members", "TRUNCATE")


def test_guard_registry_declared_without_counts_fails_the_teardown_check():
    """The omission is impossible: declaring registry writes WITHOUT their exact INSERT counts
    fails `check_operations`, the function the schema fixture runs at the end of every test."""
    for tables in (("related_subjects",), ("related_subject_identifiers",), tuple(NEW_TABLES)):
        bare = Declared()
        bare(*tables)
        with pytest.raises(AssertionError, match="without its exact INSERT count"):
            check_operations(Counter(), bare)
    partial = Declared()
    partial(*NEW_TABLES)
    partial.counts(related_subjects=1)
    with pytest.raises(AssertionError, match="related_subject_identifiers declared without"):
        check_operations(Counter(), partial)
    full = Declared()
    full(*NEW_TABLES)
    full.counts(related_subjects=0, related_subject_identifiers=0)
    check_operations(Counter(), full)


@pytest.mark.asyncio
async def test_guard_a_subject_without_an_identifier_is_refused_even_when_the_totals_balance(
    writes, conn
):
    """Two subjects, two identifiers: the totals balance, yet one subject owns both identifiers
    and the other owns none. The per-subject check refuses it; an aggregate comparison would not.
    Built with the test's own SQL, then the per-subject function is called directly."""
    writes(*NEW_TABLES)
    writes.counts(related_subjects=2, related_subject_identifiers=2)
    writes.ops("related_subjects", "DELETE")
    writes.ops("related_subject_identifiers", "DELETE")
    key = json.dumps({"plan_sha256": SHA, "subject_ref": REF, "tier": TIER})
    owner = await conn.fetchval(
        "INSERT INTO related_subjects (kind, evidence, created_by) VALUES ('person', $1::jsonb, 'x') RETURNING id",
        key,
    )
    await conn.fetchval(
        "INSERT INTO related_subjects (kind, evidence, created_by) VALUES ('person', $1::jsonb, 'x') RETURNING id",
        key,
    )
    for norm in ("ZZ0000071", "ZZ0000072"):
        await conn.execute(
            "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, subject_id) "
            "VALUES ('passport', $1, $1, 1, $2)",
            norm,
            owner,
        )
    ops = await written_ops(conn)
    counted = Declared()
    counted.counts(related_subjects=2, related_subject_identifiers=2)
    check_operations(ops, counted)  # the aggregate view sees nothing wrong
    with pytest.raises(AssertionError, match="1 subject\\(s\\) without an identifier"):
        await check_no_orphan_subjects(conn)
    await conn.execute("DELETE FROM related_subjects")  # leave a clean state for the teardown


def test_guard_operations_and_counts_are_checked_not_only_tables():
    """`check_operations` is the operation-level half of the teardown assertion."""
    nothing = Declared()
    check_operations(Counter(), nothing)
    inserts = Counter(
        {("related_subjects", "INSERT"): 1, ("related_subject_identifiers", "INSERT"): 2}
    )
    check_operations(inserts, nothing)  # a creation: INSERTs only
    for table in ("related_subjects", "related_subject_identifiers"):
        for op in ("UPDATE", "DELETE", "TRUNCATE"):
            with pytest.raises(AssertionError, match=f"{op} on {table}"):
                check_operations(inserts + Counter({(table, op): 1}), nothing)
    with pytest.raises(AssertionError, match="UPDATE on clients"):
        check_operations(Counter({("clients", "UPDATE"): 1}), nothing)
    allowed = Declared()
    allowed.ops("related_subjects", "UPDATE")
    check_operations(inserts + Counter({("related_subjects", "UPDATE"): 1}), allowed)
    counted = Declared()
    counted.counts(related_subjects=1, related_subject_identifiers=2)
    check_operations(inserts, counted)
    with pytest.raises(AssertionError, match="declared 1"):
        check_operations(inserts + Counter({("related_subjects", "INSERT"): 1}), counted)


@pytest.mark.asyncio
async def test_guard_seeding_through_unmeasured_is_excused_but_a_later_write_is_not(writes, conn):
    async with unmeasured(conn):
        await conn.execute("INSERT INTO clients (full_name) VALUES ('Zz Seed')")
    assert await written(conn) == set()
    await conn.execute("UPDATE clients SET full_name = full_name")  # same-value update
    assert await written(conn) == {"clients"}
    writes("clients")
    writes.ops("clients", "UPDATE")


# ── criterion 1: service level, one connection, counters on the two tables only ──


@pytest.mark.asyncio
async def test_c1_batch_counters_cover_the_two_new_tables_only(writes, conn):
    tr = conn.transaction()
    await tr.start()
    try:
        start = await log_mark(conn)
        for n in range(3):
            out = await reg(
                conn, subj(ids=(("passport", f"ZZ000000{n}"),), name=f"Zz Probe {n}"), dry_run=False
            )
            assert out["outcome"] == "created"
        # The replay has its own no-write window, read BEFORE the rollback removes the log.
        replay_mark = await log_mark(conn)
        again = await reg(conn, subj(ids=(("passport", "ZZ0000000"),)), dry_run=False)
        assert again["outcome"] == "already_registered"
        replay_ops = await written_ops(conn, replay_mark)
        batch_ops = await written_ops(conn, start)
        rows = await conn.fetch(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables "
            "WHERE schemaname = current_schema()"
        )
        await check_no_orphan_subjects(conn)
    finally:
        await tr.rollback()
    assert replay_ops == Counter()
    # Exactly what three creations write: INSERTs only, nothing else anywhere.
    assert batch_ops == Counter(
        {("related_subjects", "INSERT"): 3, ("related_subject_identifiers", "INSERT"): 3}
    )
    # write_log is the measuring instrument (trigger target), not a table the registry writes.
    stats = {r["relname"]: (r["n_tup_ins"], r["n_tup_upd"], r["n_tup_del"]) for r in rows}
    assert {t for t, v in stats.items() if t != "write_log" and any(v)} == NEW_TABLES
    assert stats["related_subjects"] == (3, 0, 0)
    assert stats["related_subject_identifiers"] == (3, 0, 0)
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 0


# ── criterion 2: HTTP level, what a call writes ──────────────────────────────


@pytest.mark.asyncio
async def test_c2_http_call_writes_two_tables_and_the_audit_row_only(
    writes, pool, conn, enabled, audit_logger
):
    writes("related_subjects", "related_subject_identifiers", "api_audit_trail")
    writes.counts(related_subjects=1, related_subject_identifiers=1)
    stack = Stack(pool)
    async with writes_only(conn, NEW_TABLES | {"api_audit_trail"}, exact_content=True):
        resp = await post(stack, body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID),)))
    assert resp.status_code == 200 and resp.json()["outcome"] == "created"
    audit = await conn.fetch("SELECT * FROM api_audit_trail")
    assert len(audit) == 1 and audit[0]["request_body"] is None
    assert SENTINEL_NAME not in str(dict(audit[0])) and SENTINEL_ID not in str(dict(audit[0]))
    assert stack.sink["request_body_set"] is False


@pytest.mark.asyncio
async def test_c2_session_header_and_user_email_also_update_session_tracking(
    writes, pool, conn, enabled, audit_logger
):
    writes("related_subjects", "related_subject_identifiers", "api_audit_trail", "session_tracking")
    writes.counts(related_subjects=1, related_subject_identifiers=1)
    stack = Stack(pool)
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO session_tracking (session_id, user_email) "
            "VALUES ('sess-probe-1', 'staff@example.test')"
        )
    headers = {**HEADERS, "X-Session-ID": "sess-probe-1", "X-User-Email": "staff@example.test"}
    async with writes_only(conn, NEW_TABLES | AUDIT_TABLES, exact_content=True):
        resp = await post(
            stack, body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID),)), headers
        )
    assert resp.status_code == 200
    dump = str(await conn.fetch("SELECT * FROM session_tracking")) + str(
        await conn.fetch("SELECT * FROM api_audit_trail")
    )
    assert SENTINEL_NAME not in dump and SENTINEL_ID not in dump
    assert all(r["request_body"] is None for r in await conn.fetch("SELECT * FROM api_audit_trail"))


# ── criterion 3: replay ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_c3_replay_is_already_registered_and_changes_no_row(writes, pool, conn, enabled):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=2)
    stack = Stack(pool, middleware=False)
    payload = body(
        kind="person", ids=(("passport", "ZZ0000001"), ("nik", "3201234567890123")),
        date_of_birth="1990-01-02", display_name="Zz Probe Subject",
    )  # fmt: skip
    first = await post(stack, payload)
    assert first.json()["outcome"] == "created"
    async with writes_nothing(conn):
        again = await post(stack, payload)
    assert again.json()["outcome"] == "already_registered"
    assert again.json()["match_basis"] == "identifiers_and_dob"
    assert again.json()["subject_id"] == first.json()["subject_id"]


# ── criterion 4: the key-set rule ────────────────────────────────────────────


async def _seed(conn, ids=(("nik", "3201234567890123"), ("passport", "ZZ0000001")), dob=None):
    """Seed one registry subject with the fixture's OWN SQL: plain INSERTs, no service call.

    The block is excused from the measurement (it is the test's setup); nothing the code under
    test does can hide in it, because the code under test does not run here.
    """
    prepared = prepare_identifiers("person", [(t, v, {}) for t, v in ids])
    async with unmeasured(conn):
        sid = await conn.fetchval(
            "INSERT INTO related_subjects (kind, display_name, date_of_birth, evidence, created_by) "
            "VALUES ('person', 'Zz Probe Subject', $1, $2::jsonb, $3) RETURNING id",
            dob,
            json.dumps({"plan_sha256": SHA, "subject_ref": REF, "tier": TIER}),
            REGISTRY_ACTOR,
        )
        for ident in prepared:
            await conn.execute(
                "INSERT INTO related_subject_identifiers "
                "(id_type, id_norm, id_raw, norm_version, subject_id) VALUES ($1, $2, $3, $4, $5)",
                ident.id_type,
                ident.id_norm,
                ident.id_raw,
                NORM_VERSION,
                sid,
            )
    return sid


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
async def test_c4_key_set_differs_is_conflict_and_writes_nothing(writes, conn, ids, reason):
    owner = await _seed(conn)
    async with writes_nothing(conn):
        out = await reg(conn, subj(ids=ids), dry_run=False)
    assert out["outcome"] == "conflict" and out["reason"] == reason
    assert out["subject_id"] == owner


@pytest.mark.asyncio
async def test_c4_keys_of_two_owners_conflict(writes, conn):
    await _seed(conn)
    await _seed(conn, ids=(("passport", "ZZ0000005"),))
    async with writes_nothing(conn):
        out = await reg(
            conn, subj(ids=(("passport", "ZZ0000001"), ("passport", "ZZ0000005"))), dry_run=False
        )
    assert (out["outcome"], out["reason"]) == ("conflict", "two_owners")


@pytest.mark.asyncio
async def test_c4_equal_set_with_a_different_birth_date_is_a_contradiction(writes, conn):
    await _seed(conn, dob=date(1990, 1, 2))
    async with writes_nothing(conn):
        out = await reg(conn, subj(ids=(("nik", "3201234567890123"), ("passport", "ZZ0000001")), dob=date(1991, 1, 2)), dry_run=False)  # fmt: skip
    assert (out["outcome"], out["reason"]) == ("conflict", "contradiction")


@pytest.mark.asyncio
async def test_c4_equal_set_match_basis_follows_the_birth_dates(writes, conn):
    ids = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    ids2 = (("passport", "ZZ0000009"),)
    await _seed(conn, ids=ids, dob=date(1990, 1, 2))
    await _seed(conn, ids=ids2)
    async with writes_nothing(conn):
        both = await reg(conn, subj(ids=ids, dob=date(1990, 1, 2)), dry_run=False)
        request_without = await reg(conn, subj(ids=ids), dry_run=False)
        owner_without = await reg(conn, subj(ids=ids2, dob=date(1990, 1, 2)), dry_run=False)
    assert (both["outcome"], both["match_basis"]) == ("already_registered", "identifiers_and_dob")
    assert (request_without["outcome"], request_without["match_basis"]) == (
        "already_registered",
        "identifiers_only",
    )
    assert (owner_without["outcome"], owner_without["match_basis"]) == (
        "already_registered",
        "identifiers_only",
    )


@pytest.mark.asyncio
async def test_c4_kind_change_keeping_the_identifiers_is_422_and_writes_nothing(
    writes, pool, conn, enabled
):
    stack = Stack(pool, middleware=False)
    await _seed(conn)
    async with writes_nothing(conn):
        resp = await post(stack, body(kind="company", ids=(("passport", "ZZ0000001"),)))
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["code"] == "kind_type_mismatch"


@pytest.mark.asyncio
async def test_c4_creation_stores_actor_canonical_keys_and_norm_version(writes, conn):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=2)
    out = await reg(
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
    assert json.loads(row["evidence"]) == {"plan_sha256": SHA, "subject_ref": REF, "tier": TIER}
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
            return await reg(_Conn(c, barrier_hook(barrier)), s, dry_run=False)

    return await asyncio.gather(*(one(s) for s in subjects))


async def _assert_no_subject_without_identifiers(conn):
    await check_no_orphan_subjects(conn)


@pytest.mark.asyncio
@pytest.mark.parametrize("callers", [2, 3])
async def test_c5_identical_requests_make_exactly_one_subject(writes, pool, conn, callers):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=2)
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
    writes, pool, conn, callers
):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=2)
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
async def test_c5_identifiers_are_inserted_in_sorted_key_order(writes, conn):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=3)
    seen = []

    class _Spy(_Conn):
        async def fetchval(self, sql, *args):
            if sql.startswith("INSERT INTO related_subject_identifiers"):
                seen.append((args[0], args[1]))
            return await super().fetchval(sql, *args)

    ids = (("passport", "ZZ0000009"), ("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    await reg(_Spy(conn), subj(ids=ids), dry_run=False)
    assert seen == [
        ("nik", "3201234567890123"),
        ("passport", "ZZ0000001"),
        ("passport", "ZZ0000009"),
    ]


def failing_hook(times, factory):
    state = {"left": times}

    async def hook(sql):
        if sql.startswith("INSERT INTO related_subject_identifiers") and state["left"] > 0:
            state["left"] -= 1
            raise factory()

    return hook


def deadlock_hook(times):
    return failing_hook(times, lambda: asyncpg.DeadlockDetectedError("deadlock detected"))


def serialization_hook(times):
    return failing_hook(times, lambda: asyncpg.SerializationError("could not serialize access"))


class _FailingCommit:
    """Transaction whose COMMIT fails: the work is rolled back, then the injected error raises."""

    def __init__(self, real, state, factory):
        self._real, self._state, self._factory = real, state, factory

    async def __aenter__(self):
        return await self._real.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None and self._state["left"] > 0:
            self._state["left"] -= 1
            await self._real.__aexit__(Exception, Exception("forced rollback"), None)
            raise self._factory()
        return await self._real.__aexit__(exc_type, exc, tb)


class _CommitConn(_Conn):
    def __init__(self, real, times, factory):
        super().__init__(real)
        self._commit = {"left": times}
        self._factory = factory

    def transaction(self, *a, **kw):
        return _FailingCommit(self._real.transaction(*a, **kw), self._commit, self._factory)


class _PoolOf:
    """Pool proxy: connections are wrapped by ``wrap``; ``fail_enter``/``fail_exit`` inject."""

    def __init__(self, pool, wrap=lambda c: c, fail_enter=None, fail_exit=None):
        self.pool, self.wrap = pool, wrap
        self.fail_enter, self.fail_exit = fail_enter, fail_exit

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(_s):
                if outer.fail_enter:
                    raise outer.fail_enter
                _s.c = await outer.pool.acquire()
                return outer.wrap(_s.c)

            async def __aexit__(_s, *exc):
                await outer.pool.release(_s.c)
                if outer.fail_exit:
                    raise outer.fail_exit
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_c5_one_forced_deadlock_is_retried_to_the_right_outcome(writes, conn):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=2)
    ids = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    out = await reg(_Conn(conn, deadlock_hook(1)), subj(ids=ids), dry_run=False)
    assert out["outcome"] == "created"
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1
    assert await conn.fetchval("SELECT count(*) FROM related_subject_identifiers") == 2


@pytest.mark.asyncio
async def test_c5_a_second_forced_deadlock_is_the_fixed_503_and_leaves_nothing(
    writes, pool, conn, enabled
):
    async with writes_nothing(conn):
        with pytest.raises(RegistryUnavailable) as caught:
            await reg(_Conn(conn, deadlock_hook(2)), subj(), dry_run=False)
    assert caught.value.code == "registry_busy"

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

    async with writes_nothing(conn):
        resp = await post(Stack(_Pool(), middleware=False), body())
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_busy"})


@pytest.mark.asyncio
async def test_c5_one_forced_serialization_failure_is_retried_to_the_right_outcome(writes, conn):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=2)
    ids = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
    out = await reg(_Conn(conn, serialization_hook(1)), subj(ids=ids), dry_run=False)
    assert out["outcome"] == "created"
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1
    assert await conn.fetchval("SELECT count(*) FROM related_subject_identifiers") == 2


@pytest.mark.asyncio
async def test_c5_a_second_forced_serialization_failure_is_the_fixed_503_and_writes_nothing(
    writes, pool, conn, enabled
):
    async with writes_nothing(conn):
        with pytest.raises(RegistryUnavailable) as caught:
            await reg(_Conn(conn, serialization_hook(2)), subj(), dry_run=False)
    assert caught.value.code == "registry_busy"
    wrapped = _PoolOf(pool, lambda c: _Conn(c, serialization_hook(2)))
    async with writes_nothing(conn):
        resp = await post(Stack(wrapped, middleware=False), body())
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_busy"})


@pytest.mark.asyncio
async def test_c5_a_failed_commit_with_a_serialization_error_is_retried_once(
    writes, conn, pool, enabled
):
    writes("related_subjects", "related_subject_identifiers")
    writes.counts(related_subjects=1, related_subject_identifiers=1)
    one = _CommitConn(conn, 1, lambda: asyncpg.SerializationError("x"))
    assert (await reg(one, subj(), dry_run=False))["outcome"] == "created"
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1
    async with unmeasured(conn):
        await conn.execute("DELETE FROM related_subjects")
    two = _CommitConn(conn, 2, lambda: asyncpg.SerializationError("x"))
    async with writes_nothing(conn):
        with pytest.raises(RegistryUnavailable):
            await reg(two, subj(), dry_run=False)


@pytest.mark.asyncio
async def test_c5_a_lost_race_that_loses_again_is_the_fixed_503(writes, conn):
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
        await reg(_Conn(conn, hook), subj(), dry_run=False)
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 0


# ── criterion 6: CRM matches ─────────────────────────────────────────────────


async def _crm_fixtures(conn):
    async with unmeasured(conn):
        await _insert_crm_fixtures(conn)


async def _insert_crm_fixtures(conn):
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
async def test_c6_crm_matches_in_every_stored_spelling(writes, conn, kind, ids, outcome):
    await _crm_fixtures(conn)
    await _seed(conn, ids=(("passport", "ZZ0000500"),))
    if outcome == "created":
        writes("related_subjects", "related_subject_identifiers")
        writes.counts(related_subjects=1, related_subject_identifiers=1)
        out = await reg(conn, subj(kind=kind, ids=ids), dry_run=False)
    else:
        async with writes_nothing(conn):
            out = await reg(conn, subj(kind=kind, ids=ids), dry_run=False)
    assert out["outcome"] == outcome


@pytest.mark.asyncio
async def test_c6_python_and_sql_canonical_forms_agree_on_the_vectors(writes, conn):
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
    "person_npwp": body(kind="person", ids=(("npwp", "6054321098765432"), ("passport", SENTINEL_ID)), display_name=SENTINEL_NAME),
    "failing_validator": body(ids=(("passport", "ZQ447"),), display_name=SENTINEL_NAME),
    "yearless_ahu": body(kind="company", ids=(("ahu", "AHU-7731.AH.04.09"),), display_name=SENTINEL_NAME),
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
async def test_c7_invalid_bodies_are_422_write_nothing_and_echo_nothing(
    writes, pool, conn, enabled, case
):
    stack = Stack(pool, middleware=False)
    async with writes_nothing(conn):
        resp = await post(stack, INVALID_BODIES[case])
    assert resp.status_code == 422, resp.text
    assert SENTINEL_NAME not in resp.text and SENTINEL_ID not in resp.text
    assert "zzsent" not in resp.text.lower() and "Sentinel" not in resp.text
    detail = resp.json()["detail"]
    assert detail and all(set(item) == {"loc", "code"} for item in detail)


@pytest.mark.asyncio
async def test_c7_codes_name_the_failure(writes, pool, enabled):
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
async def test_c8_flag_off_is_503_even_with_a_valid_key(writes, pool, monkeypatch):
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
async def test_c8_wrong_or_missing_key_is_401(writes, pool, enabled, headers):
    resp = await post(Stack(pool, middleware=False), body(), headers)
    assert (resp.status_code, resp.json()) == (401, {"detail": "unauthorized"})


@pytest.mark.asyncio
async def test_c8_key_comparison_is_constant_time(writes, pool, enabled, monkeypatch):
    import hmac

    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    resp = await post(Stack(pool, middleware=False), body(dry_run=True))
    assert resp.status_code == 200 and calls == [1]


@pytest.mark.asyncio
async def test_c8_unconfigured_key_is_401_not_open(writes, pool, monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", None, raising=False)
    resp = await post(Stack(pool, middleware=False), body(), {"X-CRM-Write-Key": ""})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_c8_dry_run_absent_and_true_write_nothing_on_every_outcome(
    writes, pool, conn, enabled
):
    stack = Stack(pool, middleware=False)
    await _crm_fixtures(conn)
    owner = ("nik", "3201234567890123"), ("passport", "ZZ0000001")

    def variants(payload):
        absent = {k: v for k, v in payload.items() if k != "dry_run"}
        return [absent, {**payload, "dry_run": True}]

    async with writes_nothing(conn):
        for payload in variants(body(ids=owner)):  # empty registry
            resp = await post(stack, payload)
            assert resp.json() == {"outcome": "would_create", "dry_run": True}

    await _seed(conn)
    cases = {
        "already_registered": body(ids=owner),
        "conflict": body(ids=(("passport", "ZZ0000001"),)),
        "existing_client": body(ids=(("passport", "AB123456"),)),
        "existing_family_member": body(ids=(("passport", "EF777888"),)),
        "existing_company": body(kind="company", ids=(("npwp", "0123456789012345"),)),
        "would_create": body(ids=(("passport", "ZZ0000077"),)),
    }
    async with writes_nothing(conn):
        for outcome, payload in cases.items():
            for variant in variants(payload):
                resp = await post(stack, variant)
                assert resp.json()["outcome"] == outcome and resp.json()["dry_run"] is True


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


async def _asgi_post(app, receive, path=URL):
    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "client": ("127.0.0.1", 1), "server": ("test", 80), "state": {},
        "headers": [
            (b"x-crm-write-key", KEY.encode()),
            (b"content-type", b"application/json"),
            (b"user-agent", b"probe"),
        ],
    }  # fmt: skip
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    text = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], text.decode(), str(start["headers"])


async def _privacy_run(pool, conn, caplog, setup, payload, *, expect_status):
    stack = Stack(pool)
    setup(stack)
    resp = await post(stack, payload)
    assert resp.status_code == expect_status, resp.text
    return resp, stack


SECRET_BODY = body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID),))


@pytest.mark.asyncio
async def test_c9_privacy_on_success_and_conflict(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("related_subjects", "related_subject_identifiers", "api_audit_trail")
    writes.counts(related_subjects=1, related_subject_identifiers=1)
    async with writes_only(
        conn, {"related_subjects", "related_subject_identifiers", "api_audit_trail"}
    ):
        await _privacy_run(pool, conn, caplog, lambda s: None, SECRET_BODY, expect_status=200)
    # The conflict call has its OWN window: the HTTP path may only append its audit row.
    async with writes_only(conn, {"api_audit_trail"}):
        resp, _ = await _privacy_run(
            pool, conn, caplog, lambda s: None,
            body(display_name=SENTINEL_NAME, ids=(("passport", SENTINEL_ID), ("passport", "ZZ0000002"))),
            expect_status=200,
        )  # fmt: skip
    assert resp.json()["outcome"] == "conflict"


@pytest.mark.asyncio
async def test_c9_privacy_on_422(writes, pool, conn, enabled, audit_logger, caplog):
    writes("api_audit_trail")
    for case in sorted(INVALID_BODIES):
        await _privacy_run(
            pool, conn, caplog, lambda s: None, INVALID_BODIES[case], expect_status=422
        )


@pytest.mark.asyncio
async def test_c9_privacy_on_a_forced_database_error(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("api_audit_trail")
    leaky = asyncpg.UniqueViolationError(
        f"duplicate key value violates unique constraint: Key (id_norm)=({SENTINEL_ID}) {SENTINEL_NAME}"
    )

    def setup(stack):
        stack.app.state.db_pool = _Boom(leaky)

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=503)
    assert resp.json() == {"detail": "database_error"}


@pytest.mark.asyncio
async def test_c9_privacy_on_an_unexpected_service_failure(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("api_audit_trail")
    resp, stack = await _privacy_run(
        pool, conn, caplog,
        lambda s: setattr(s.app.state, "db_pool", _Boom(RuntimeError(SENTINEL_NAME + SENTINEL_ID))),
        SECRET_BODY, expect_status=500,
    )  # fmt: skip
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_c9_privacy_on_injected_body_parsing_failure(
    writes, pool, conn, enabled, audit_logger, caplog, monkeypatch
):
    writes("api_audit_trail")

    def boom(*a, **kw):
        raise RuntimeError(f"{SENTINEL_NAME} {SENTINEL_ID}")

    monkeypatch.setattr(route_module.RelatedSubjectRegisterRequest, "model_validate_json", boom)
    resp, _ = await _privacy_run(pool, conn, caplog, lambda s: None, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_c9_privacy_on_injected_auth_dependency_failure(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("api_audit_trail")

    async def broken():
        raise RuntimeError(f"{SENTINEL_NAME} {SENTINEL_ID}")

    def setup(stack):
        stack.app.dependency_overrides[route_module.verify_related_subject_write] = broken

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http_503_with_detail", "runtime_error"])
async def test_c9_privacy_on_injected_pool_dependency_failure(
    writes, pool, conn, enabled, audit_logger, caplog, kind
):
    writes("api_audit_trail")

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
async def test_c9_oversized_body_is_refused_with_a_fixed_code(writes, pool, enabled):
    resp = await post(Stack(pool, middleware=False), "x" * (route_module.MAX_BODY_BYTES + 1))
    assert resp.status_code == 422 and resp.json()["detail"][0]["code"] == "too_long"


LEAKY = f"{SENTINEL_NAME} {SENTINEL_ID}"


class _Hostile(Exception):
    def __str__(self):
        raise ValueError(LEAKY)

    def __repr__(self):
        raise ValueError(LEAKY)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [499, 600, 0, 402, 404, 418])
async def test_c9_privacy_on_a_dependency_http_exception_with_any_status(
    writes, pool, conn, enabled, audit_logger, caplog, status
):
    writes("api_audit_trail")

    async def broken():
        raise HTTPException(status_code=status, detail=LEAKY, headers={"X-Leak": SENTINEL_ID})

    def setup(stack):
        stack.app.dependency_overrides[route_module.verify_related_subject_write] = broken

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"} and "x-leak" not in resp.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), [(401, "unauthorized"), (503, "registry_unavailable")])
async def test_c9_a_dependency_http_exception_keeps_only_its_fixed_code(
    writes, pool, conn, enabled, audit_logger, caplog, status, code
):
    writes("api_audit_trail")

    async def broken():
        raise HTTPException(status_code=status, detail=LEAKY)

    def setup(stack):
        stack.app.dependency_overrides[route_module.verify_related_subject_write] = broken

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=status)
    assert resp.json() == {"detail": code}


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["auth", "pool"])
async def test_c9_privacy_on_an_exception_whose_text_cannot_be_rendered(
    writes, pool, conn, enabled, audit_logger, caplog, where
):
    writes("api_audit_trail")

    async def broken_auth():
        raise _Hostile()

    def broken_pool():
        raise _Hostile()

    def setup(stack):
        if where == "auth":
            stack.app.dependency_overrides[route_module.verify_related_subject_write] = broken_auth
        else:
            stack.app.dependency_overrides[get_database_pool] = broken_pool

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_c9_privacy_when_reading_the_body_fails(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("api_audit_trail")
    stack = Stack(pool)
    tokens = forbidden_tokens(SECRET_BODY) | {t.lower() for t in (LEAKY,)}
    trace_before = set(request_tracing._trace_storage)

    async def receive():
        raise RuntimeError(LEAKY)

    with capture_logs() as records:
        status, text, headers = await _asgi_post(stack.app, receive)
    assert status == 500 and json.loads(text) == {"detail": "internal_error"}
    await observe(stack, tokens, text + headers, records, trace_before)


@pytest.mark.asyncio
async def test_c9_privacy_when_pool_acquisition_fails(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("api_audit_trail")

    def setup(stack):
        stack.app.state.db_pool = _PoolOf(pool, fail_enter=RuntimeError(LEAKY))

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}

    def setup_db(stack):
        stack.app.state.db_pool = _PoolOf(pool, fail_enter=asyncpg.PostgresError(LEAKY))

    resp, _ = await _privacy_run(pool, conn, caplog, setup_db, SECRET_BODY, expect_status=503)
    assert resp.json() == {"detail": "database_error"}


@pytest.mark.asyncio
async def test_c9_privacy_when_releasing_the_connection_fails(
    writes, pool, conn, enabled, audit_logger, caplog
):
    writes("related_subjects", "related_subject_identifiers", "api_audit_trail")
    writes.counts(related_subjects=1, related_subject_identifiers=1)

    def setup(stack):
        stack.app.state.db_pool = _PoolOf(pool, fail_exit=RuntimeError(LEAKY))

    resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}
    # The work had committed before the release failed: a replay finds it, nothing is doubled.
    assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "status", "code"),
    [
        (lambda: RuntimeError(LEAKY), 500, "internal_error"),
        (lambda: asyncpg.QueryCanceledError(LEAKY), 503, "database_error"),
        (lambda: asyncpg.SerializationError(LEAKY), 503, "registry_busy"),
    ],
    ids=["runtime", "database", "serialization-twice"],
)
async def test_c9_privacy_when_the_commit_fails(
    writes, pool, conn, enabled, audit_logger, caplog, factory, status, code
):
    writes("api_audit_trail")

    def setup(stack):
        stack.app.state.db_pool = _PoolOf(pool, lambda c: _CommitConn(c, 2, factory))

    async with writes_only(conn, {"api_audit_trail"}):
        resp, _ = await _privacy_run(pool, conn, caplog, setup, SECRET_BODY, expect_status=status)
    assert resp.json() == {"detail": code}


@pytest.mark.asyncio
async def test_c9_privacy_when_building_the_success_response_fails(
    writes, pool, conn, enabled, audit_logger, caplog, monkeypatch
):
    writes("related_subjects", "related_subject_identifiers", "api_audit_trail")
    writes.counts(related_subjects=1, related_subject_identifiers=1)

    def boom(*a, **kw):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(route_module, "JSONResponse", boom)
    resp, _ = await _privacy_run(pool, conn, caplog, lambda s: None, SECRET_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


class _Stop(BaseException):
    pass


@pytest.mark.asyncio
async def test_c9_a_base_exception_that_is_not_an_exception_still_propagates(writes, pool, enabled):
    async def stop():
        raise _Stop()

    stack = Stack(pool, middleware=False)
    stack.app.dependency_overrides[route_module.verify_related_subject_write] = stop
    with pytest.raises(_Stop):
        await post(stack, SECRET_BODY)


@pytest.mark.asyncio
async def test_c9_the_failure_answer_cannot_raise(monkeypatch):
    monkeypatch.setattr(route_module.logger, "error", lambda *a, **k: 1 / 0)
    assert route_module._failure_response(_Hostile()).status_code == 500

    class _Liar(Exception):
        @property
        def __class__(self):
            raise ValueError(LEAKY)

    assert route_module._failure_response(_Liar()).body == b'{"detail": "internal_error"}'


# ── criterion 10: portal ─────────────────────────────────────────────────────


def test_c10_router_is_not_a_portal_route_and_portal_roles_are_refused():
    paths = {r.path for r in route_module.router.routes}
    assert paths == {URL, URL2}  # slice 2 adds the documents route to the same router
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
async def test_c11_database_refuses_short_duplicate_and_self_merged_rows(writes, conn):
    writes("related_subjects", "related_subject_identifiers")
    writes.ops("related_subjects", "DELETE")
    writes.ops("related_subject_identifiers", "DELETE")  # the cascade of the subject delete
    writes.counts(related_subjects=1, related_subject_identifiers=1)
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
async def test_c11_rollback_section_drops_both_tables(writes, pool):
    _, rollback = split_migration_sql(MIGRATION.read_text())
    _, rollback2 = split_migration_sql(MIGRATION2.read_text())
    async with pool.acquire() as c:
        await c.execute(rollback2)  # slice 2 first: its link table references related_subjects
        await c.execute(rollback)
        left = await c.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
            sorted(NEW_TABLES),
        )
    assert left == 0


# ══ slice 2: documents by reference ═══════════════════════════════════════════
# Same module, same harness: the write measurement, the real middleware stack, the privacy
# derivation. Values are synthetic and generated at run time (a hash of a label; Drive-like file
# ids built from a short repeated word): no literal hash and no literal file id in this file.

DOC_PROTOCOL = PROTOCOL_WORDS | frozenset(
    {
        "subject", "documents", "sha256", "byte_size", "mime_type", "doc_type", "first_seen_on",
        "first_channel", "drive", "file_id", "seen_on", "whatsapp",
    }
)  # fmt: skip
CLOSED_LOC_DOCS = CLOSED_LOC | {
    "subject", "documents", "sha256", "byte_size", "mime_type", "doc_type", "first_seen_on",
    "first_channel", "drive", "file_id", "seen_on",
}  # fmt: skip
CLOSED_CODES_DOCS = CLOSED_CODES | {"duplicate_document", "duplicate_file_id"}
CLOSED_OUTCOMES_DOCS = {
    "subject_not_registered", "conflict", "would_link", "linked", "already_linked",
}  # fmt: skip
CLOSED_REASONS_DOCS = {
    "none_owned", "set_differs", "partial_overlap", "two_owners", "kind_differs", "document_differs",
}  # fmt: skip
CLOSED_COUNT_KEYS = {"documents", "sightings", "links", "descriptive_differences"}
CLOSED_200_KEYS_DOCS = {"outcome", "reason", "subject_id", "counts", "dry_run"}
INDEX_LIMITS = {"identifiers": 20, "documents": 40, "drive": 8}

PLAN2 = hashlib.sha256(b"plan-slice-2").hexdigest()
DT, DT2 = "zzdoctype", "zzdoctwo"
MIME, MIME2 = "application/zzprobe", "application/zzother"
DAY1, DAY2, DAY3, DAY4 = "2019-01-02", "2019-02-03", "2019-03-04", "2019-04-05"
OWNER = (("nik", "3201234567890123"), ("passport", "ZZ0000001"))
OTHER = (("passport", "ZZ0000009"),)


def dh(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def fid(word: str) -> str:
    return (word * 40)[:33]


def doc(label, *, size=1234, files=(), seen=DAY3, **over):
    data = {
        "sha256": dh(label), "byte_size": size, "mime_type": MIME, "doc_type": DT,
        "first_seen_on": DAY1, "first_channel": "drive" if files else "whatsapp",
        "drive": [{"file_id": fid(w), "seen_on": seen} for w in files],
    }  # fmt: skip
    data.update(over)
    return data


def dbody(docs, ids=OWNER, kind="person", **over):
    data = {
        "subject": {"kind": kind, "identifiers": [{"type": t, "value": v} for t, v in ids]},
        "documents": list(docs),
        "evidence": {"plan_sha256": PLAN2, "subject_ref": REF},
        "dry_run": False,
    }
    data.update(over)
    return data


def dreq(payload):
    model = route_module.RelatedDocumentsRegisterRequest
    return model.model_validate_json(json.dumps(payload)).to_input()


def assert_answer_shape_docs(resp):
    """Closed lists of the documents route: 200 keys/values/counts, 422 loc/code, fixed words."""
    data = resp.json()
    if resp.status_code == 200:
        assert set(data) <= CLOSED_200_KEYS_DOCS and data["outcome"] in CLOSED_OUTCOMES_DOCS
        assert data.get("reason") in (None, *CLOSED_REASONS_DOCS)
        assert isinstance(data["dry_run"], bool)
        assert isinstance(data.get("subject_id", 0), int)
        if "counts" in data:
            assert set(data["counts"]) == CLOSED_COUNT_KEYS
            assert all(isinstance(n, int) and 0 <= n <= 1000 for n in data["counts"].values())
        if data["outcome"] in ("linked", "would_link", "already_linked"):
            assert "subject_id" in data and "counts" in data
        else:
            assert "subject_id" not in data and "counts" not in data
    elif resp.status_code == 422:
        assert set(data) == {"detail"} and isinstance(data["detail"], list) and data["detail"]
        for item in data["detail"]:
            assert set(item) == {"loc", "code"} and isinstance(item["loc"], list)
            for n, part in enumerate(item["loc"]):
                if isinstance(part, int) and not isinstance(part, bool):
                    assert n > 0 and 0 <= part < INDEX_LIMITS[item["loc"][n - 1]], item["loc"]
                else:
                    assert part in CLOSED_LOC_DOCS, item["loc"]
            assert item["code"] in CLOSED_CODES_DOCS, item["code"]
    else:
        assert resp.status_code in CLOSED_WORDS, resp.status_code
        assert set(data) == {"detail"} and data["detail"] in CLOSED_WORDS[resp.status_code]


async def postd(stack, payload, headers=HEADERS):
    return await post(
        stack, payload, headers, url=URL2, protocol=DOC_PROTOCOL, shape=assert_answer_shape_docs
    )


async def regd(conn, payload, *, dry_run=None):
    """register_documents with the service log checked against the request's own values."""
    tokens = forbidden_tokens(payload, DOC_PROTOCOL)
    live = payload.get("dry_run", True) if dry_run is None else dry_run
    with capture_logs() as records:
        try:
            return await register_documents(conn, dreq(payload), dry_run=live)
        finally:
            assert_no_tokens(
                tokens, "service log records", " ".join(record_text(r) for r in records)
            )


def declare_docs(writes, *, documents=0, sightings=0, links=0, extra=()):
    """Declare the document tables a test writes, with the exact INSERT counts (0 = not written)."""
    counts = {
        "related_documents": documents,
        "related_document_sightings": sightings,
        "related_subject_documents": links,
    }
    writes(*[t for t, n in counts.items() if n], *extra)
    writes.counts(**{t: n for t, n in counts.items() if n})


# fixture-owned SQL: the only way a slice-2 test writes a registry table directly
async def _seed_doc(conn, label, *, size=1234, mime=MIME, day=DAY1, channel="whatsapp"):
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
            "first_channel, evidence, created_by) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
            dh(label), size, mime, date.fromisoformat(day), channel,
            json.dumps({"plan_sha256": PLAN2}), REGISTRY_ACTOR,
        )  # fmt: skip


async def _seed_sighting(conn, word, label, day=DAY3):
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO related_document_sightings (kind, ref, sha256, seen_on, created_by) "
            "VALUES ('drive', $1, $2, $3, $4)",
            fid(word), dh(label), date.fromisoformat(day), REGISTRY_ACTOR,
        )  # fmt: skip


async def _seed_link(conn, sid, label, doc_type=DT):
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
            "created_by) VALUES ($1, $2, $3, $4::jsonb, $5)",
            sid, dh(label), doc_type,
            json.dumps({"plan_sha256": PLAN2, "subject_ref": REF}), REGISTRY_ACTOR,
        )  # fmt: skip


_KEY_SQL = {
    "related_documents": "SELECT t::text AS txt, sha256 AS k FROM related_documents t",
    "related_document_sightings": (
        "SELECT t::text AS txt, ref || '/' || sha256 AS k FROM related_document_sightings t"
    ),
    "related_subject_documents": (
        "SELECT t::text AS txt, subject_id || '/' || sha256 AS k FROM related_subject_documents t"
    ),
}


async def registry_rows(c) -> dict[str, dict[str, str]]:
    """Every registry row of the five tables as {table: {row text: row key}} (slice-1 tables: id)."""
    out = {}
    for table, sql in _KEY_SQL.items():
        out[table] = {r["txt"]: r["k"] for r in await c.fetch(sql)}
    for table in sorted(NEW_TABLES):
        out[table] = {r["t"]: r["t"] for r in await c.fetch(f"SELECT t::text AS t FROM {table} t")}
    return out


def requested_keys(payload, sid):
    docs = {d["sha256"] for d in payload["documents"]}
    sightings = {f"{x['file_id']}/{d['sha256']}" for d in payload["documents"] for x in d["drive"]}
    return {
        "related_documents": docs,
        "related_document_sightings": sightings,
        "related_subject_documents": {f"{sid}/{h}" for h in docs},
    }


async def assert_exact_rows(conn, before, payload, sid):
    """Item 3: new rows are exactly the missing requested keys; old rows are byte-for-byte kept."""
    after = await registry_rows(conn)
    wanted = requested_keys(payload, sid)
    for table in sorted(ALL_REGISTRY_TABLES):
        assert set(before[table]) <= set(after[table]), f"{table}: a row changed or went away"
    for table, keys in wanted.items():
        before_keys = set(before[table].values())
        inserted = {k for txt, k in after[table].items() if txt not in before[table]}
        assert inserted == keys - before_keys, table
        assert keys <= set(after[table].values()), table
    for table in NEW_TABLES:
        assert after[table] == before[table]
    by_sha = {d["sha256"]: d for d in payload["documents"]}
    for row in await conn.fetch("SELECT * FROM related_documents"):
        if row["sha256"] in wanted["related_documents"] and not any(
            row["sha256"] == v for v in before["related_documents"].values()
        ):
            sent = by_sha[row["sha256"]]
            assert (row["byte_size"], row["mime_type"], row["first_channel"]) == (
                sent["byte_size"], sent["mime_type"], sent["first_channel"],
            )  # fmt: skip
            assert row["first_seen_on"].isoformat() == sent["first_seen_on"]
            assert row["created_by"] == REGISTRY_ACTOR
            assert json.loads(row["evidence"]) == {"plan_sha256": PLAN2}
    new_sighting_keys = {
        k for txt, k in after["related_document_sightings"].items()
        if txt not in before["related_document_sightings"]
    }  # fmt: skip
    for row in await conn.fetch("SELECT * FROM related_document_sightings"):
        key = f"{row['ref']}/{row['sha256']}"
        if key in new_sighting_keys:
            sent = next(
                x for d in by_sha.values() for x in d["drive"]
                if d["sha256"] == row["sha256"] and x["file_id"] == row["ref"]
            )  # fmt: skip
            assert row["seen_on"].isoformat() == sent["seen_on"] and row["kind"] == "drive"
            assert row["created_by"] == REGISTRY_ACTOR and row["sha256"] in by_sha
    new_link_keys = {
        k for txt, k in after["related_subject_documents"].items()
        if txt not in before["related_subject_documents"]
    }  # fmt: skip
    for row in await conn.fetch("SELECT * FROM related_subject_documents"):
        key = f"{row['subject_id']}/{row['sha256']}"
        if key in new_link_keys:
            assert row["subject_id"] == sid and row["sha256"] in by_sha
            assert row["doc_type"] == by_sha[row["sha256"]]["doc_type"]
            assert row["created_by"] == REGISTRY_ACTOR
            assert json.loads(row["evidence"]) == {"plan_sha256": PLAN2, "subject_ref": REF}


async def _count(conn, table):
    return await conn.fetchval(f"SELECT count(*) FROM {table}")


async def totals(conn):
    """(documents, sightings, links) row counts."""
    return (
        await _count(conn, "related_documents"),
        await _count(conn, "related_document_sightings"),
        await _count(conn, "related_subject_documents"),
    )


# ── guards of the extended harness ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_s2_guard_the_document_tables_are_measured_from_the_first_row(writes, conn):
    await conn.execute(
        "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
        "first_channel, evidence, created_by) VALUES ($1, 1, $2, now(), 'drive', '{}', 'x')",
        dh("guard"), MIME,
    )  # fmt: skip
    assert await written(conn) == {"related_documents"}
    with pytest.raises(AssertionError, match="wrote \\['related_documents'\\], declared \\[\\]"):
        check_declared_writes(await written(conn), set())
    ops = await written_ops(conn)
    with pytest.raises(AssertionError, match="without its exact INSERT count"):
        bare = Declared()
        bare("related_documents")
        check_operations(ops, bare)
    declared = Declared()
    declared("related_documents")
    declared.counts(related_documents=1)
    check_operations(ops, declared)
    writes("related_documents")
    writes.counts(related_documents=1)


def test_s2_guard_operations_on_the_three_tables_are_insert_only_by_default():
    assert ALL_REGISTRY_TABLES == NEW_TABLES | DOC_TABLES and REGISTRY_TABLES == ALL_REGISTRY_TABLES
    assert all(DEFAULT_OPS[t] == {"INSERT"} for t in ALL_REGISTRY_TABLES)
    for table in sorted(DOC_TABLES):
        for op in ("UPDATE", "DELETE", "TRUNCATE"):
            with pytest.raises(AssertionError, match=f"{op} on {table}"):
                check_operations(Counter({(table, op): 1}), Declared())
        bare = Declared()
        bare(table)
        with pytest.raises(AssertionError, match=f"{table} declared without"):
            check_operations(Counter(), bare)
    assert Declared() == set() and Declared().extra_ops == {} and Declared().expected_counts == {}


@pytest.mark.asyncio
async def test_s2_guard_a_same_value_update_of_a_document_row_is_a_recorded_write(writes, conn):
    await _seed_doc(conn, "guard-update")
    assert await written(conn) == set()
    await conn.execute("UPDATE related_documents SET mime_type = mime_type")
    ops = await written_ops(conn)
    assert ops == Counter({("related_documents", "UPDATE"): 1})
    with pytest.raises(AssertionError, match="UPDATE on related_documents"):
        check_operations(ops, Declared())
    writes("related_documents")
    writes.ops("related_documents", "UPDATE")
    writes.counts(related_documents=0)


# ── item 1: service level, one connection, counters on the three new tables only ──


@pytest.mark.asyncio
async def test_s2_c1_batch_counters_cover_the_three_document_tables_only(writes, conn):
    await _seed(conn)
    tr = conn.transaction()
    await tr.start()
    try:
        start = await log_mark(conn)
        for n in range(3):
            out = await regd(conn, dbody([doc(f"c1-{n}", files=(f"wordc1n{n}",))]))
            assert out["outcome"] == "linked"
        replay_mark = await log_mark(conn)
        again = await regd(conn, dbody([doc("c1-0", files=("wordc1n0",))]))
        assert again["outcome"] == "already_linked"
        replay_ops = await written_ops(conn, replay_mark)
        batch_ops = await written_ops(conn, start)
        rows = await conn.fetch(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables "
            "WHERE schemaname = current_schema()"
        )
    finally:
        await tr.rollback()
    assert replay_ops == Counter()
    assert batch_ops == Counter({(t, "INSERT"): 3 for t in DOC_TABLES})
    stats = {r["relname"]: (r["n_tup_ins"], r["n_tup_upd"], r["n_tup_del"]) for r in rows}
    assert {t for t, v in stats.items() if t != "write_log" and any(v)} == DOC_TABLES
    assert all(stats[t] == (3, 0, 0) for t in DOC_TABLES)
    assert stats["related_subjects"] == (0, 0, 0)
    assert stats["related_subject_identifiers"] == (0, 0, 0)
    assert await totals(conn) == (0, 0, 0)


# ── item 2: HTTP level ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s2_c2_http_call_writes_the_three_tables_and_the_audit_row_only(
    writes, pool, conn, enabled, audit_logger
):
    declare_docs(writes, documents=1, sightings=2, links=1, extra=("api_audit_trail",))
    await _seed(conn)
    stack = Stack(pool)
    payload = dbody([doc("c2", files=("wordc2one", "wordc2two"))])
    async with writes_only(conn, DOC_TABLES | {"api_audit_trail"}, exact_content=True):
        resp = await postd(stack, payload)
    assert resp.status_code == 200 and resp.json()["outcome"] == "linked"
    audit = await conn.fetch("SELECT * FROM api_audit_trail")
    assert len(audit) == 1 and audit[0]["request_body"] is None
    dump = str(dict(audit[0]))
    for token in (dh("c2"), fid("wordc2one"), fid("wordc2two"), "ZZ0000001", "3201234567890123"):
        assert token not in dump
    assert stack.sink["request_body_set"] is False


@pytest.mark.asyncio
async def test_s2_c2_session_header_and_user_email_also_update_session_tracking(
    writes, pool, conn, enabled, audit_logger
):
    declare_docs(writes, documents=1, links=1, extra=("api_audit_trail", "session_tracking"))
    await _seed(conn)
    stack = Stack(pool)
    async with unmeasured(conn):
        await conn.execute(
            "INSERT INTO session_tracking (session_id, user_email) "
            "VALUES ('sess-probe-2', 'staff@example.test')"
        )
    headers = {**HEADERS, "X-Session-ID": "sess-probe-2", "X-User-Email": "staff@example.test"}
    payload = dbody([doc("c2b")])
    async with writes_only(conn, DOC_TABLES - {"related_document_sightings"} | AUDIT_TABLES):
        resp = await postd(stack, payload, headers)
    assert resp.status_code == 200
    dump = str(await conn.fetch("SELECT * FROM session_tracking")) + str(
        await conn.fetch("SELECT * FROM api_audit_trail")
    )
    assert dh("c2b") not in dump and "ZZ0000001" not in dump


# ── item 3: exact rows, new rows told from rows that were already there ──────


@pytest.mark.asyncio
async def test_s2_c3_linked_inserts_exactly_the_missing_requested_keys(writes, conn):
    declare_docs(writes, documents=2, sightings=3, links=3)
    s1 = await _seed(conn)
    s2 = await _seed(conn, ids=OTHER)
    # E1: known, linked to the OTHER subject only; E2: known, linked to s1, sighting known
    await _seed_doc(conn, "e1", mime=MIME2, day=DAY2, channel="drive")
    await _seed_sighting(conn, "wordeone", "e1", DAY2)
    await _seed_link(conn, s2, "e1", DT2)
    await _seed_doc(conn, "e2")
    await _seed_sighting(conn, "wordethr", "e2")
    await _seed_link(conn, s1, "e2")
    # U: outside the request altogether
    await _seed_doc(conn, "u")
    await _seed_sighting(conn, "worduone", "u")
    await _seed_link(conn, s2, "u")
    payload = dbody(
        [
            doc("e1", files=("wordeone", "wordetwo")),
            doc("e2", files=("wordethr",), mime_type=MIME2),
            doc("n1", files=("wordnone", "wordntwo")),
            doc("n2"),
        ]
    )
    before = await registry_rows(conn)
    out = await regd(conn, payload)
    assert out == {
        "outcome": "linked", "subject_id": s1, "dry_run": False,
        "counts": {"documents": 2, "sightings": 3, "links": 3, "descriptive_differences": 3},
    }  # fmt: skip
    await assert_exact_rows(conn, before, payload, s1)


# ── item 4: replay ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s2_c4_replay_is_already_linked_with_zero_counts_and_an_equal_snapshot(
    writes, pool, conn, enabled
):
    declare_docs(writes, documents=2, sightings=2, links=2)
    await _seed(conn)
    stack = Stack(pool, middleware=False)
    payload = dbody([doc("r1", files=("wordr1one",)), doc("r2", files=("wordr2one",))])
    first = await postd(stack, payload)
    assert first.json()["outcome"] == "linked"
    before = await registry_rows(conn)
    async with writes_nothing(conn):
        again = await postd(stack, payload)
    assert again.json() == {
        "outcome": "already_linked", "subject_id": first.json()["subject_id"], "dry_run": False,
        "counts": {"documents": 0, "sightings": 0, "links": 0, "descriptive_differences": 0},
    }  # fmt: skip
    assert await registry_rows(conn) == before
    again2 = await regd(conn, payload)
    assert again2["counts"] == again.json()["counts"] and await registry_rows(conn) == before


# ── item 5: subject resolution ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ids", "reason"),
    [
        ((("passport", "ZZ0000001"),), "set_differs"),
        (OWNER + (("passport", "ZZ0000002"),), "set_differs"),
        ((("passport", "ZZ0000001"), ("passport", "ZZ0000003")), "partial_overlap"),
        ((("passport", "ZZ0000001"), ("passport", "ZZ0000005")), "two_owners"),
        ((("passport", "ZZ0000077"),), "none_owned"),
    ],
    ids=["subset", "superset", "partial-overlap", "two-owners", "none-owned"],
)
async def test_s2_c5_a_key_set_that_is_not_the_subject_writes_nothing(writes, conn, ids, reason):
    await _seed(conn)
    await _seed(conn, ids=(("passport", "ZZ0000005"),))
    async with writes_nothing(conn):
        for dry in (False, True):
            out = await regd(conn, dbody([doc("s5", files=("words5one",))], ids=ids), dry_run=dry)
            assert out == {"outcome": "subject_not_registered", "reason": reason, "dry_run": dry}
    assert await totals(conn) == (0, 0, 0)


@pytest.mark.asyncio
async def test_s2_c5_equal_keys_under_another_kind_is_kind_differs(writes, conn):
    async with unmeasured(conn):
        sid = await conn.fetchval(
            "INSERT INTO related_subjects (kind, evidence, created_by) "
            "VALUES ('company', '{}'::jsonb, 'x') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, "
            "subject_id) VALUES ('passport', 'ZZ0000001', 'ZZ0000001', 1, $1)",
            sid,
        )
    async with writes_nothing(conn):
        out = await regd(conn, dbody([doc("kd")], ids=(("passport", "ZZ0000001"),)))
    assert out == {"outcome": "subject_not_registered", "reason": "kind_differs", "dry_run": False}


@pytest.mark.asyncio
async def test_s2_c5_two_subjects_a_request_never_writes_to_the_other(writes, conn):
    declare_docs(writes, documents=2, sightings=2, links=2)
    s1 = await _seed(conn)
    s2 = await _seed(conn, ids=OTHER)
    await regd(conn, dbody([doc("two-a", files=("wordtwoa",))]))

    async def links():
        rows = await conn.fetch("SELECT subject_id, sha256 FROM related_subject_documents")
        return {(r["subject_id"], r["sha256"]) for r in rows}

    assert await links() == {(s1, dh("two-a"))}
    await regd(conn, dbody([doc("two-b", files=("wordtwob",))], ids=OTHER))
    assert await links() == {(s1, dh("two-a")), (s2, dh("two-b"))}


# ── item 6: documents, counts equal rows, dry run, schema ────────────────────


async def _case_new_doc_two_sightings(conn, s1, s2):
    return [doc("k1", files=("wordk1one", "wordk1two"))], (1, 2, 1)


async def _case_second_subject_links_known_doc(conn, s1, s2):
    await _seed_doc(conn, "k2", channel="drive")
    await _seed_sighting(conn, "wordk2one", "k2")
    await _seed_link(conn, s2, "k2")
    return [doc("k2", files=("wordk2one",), first_channel="drive")], (0, 0, 1)


async def _case_known_doc_one_new_sighting(conn, s1, s2):
    await _seed_doc(conn, "k3", channel="drive")
    await _seed_sighting(conn, "wordk3one", "k3")
    await _seed_link(conn, s1, "k3")
    return [doc("k3", files=("wordk3one", "wordk3two"), first_channel="drive")], (0, 1, 0)


async def _case_same_file_id_under_two_hashes(conn, s1, s2):
    return [doc("k4a", files=("wordk4",)), doc("k4b", files=("wordk4",))], (2, 2, 2)


async def _case_doc_with_no_sighting(conn, s1, s2):
    return [doc("k5")], (1, 0, 1)


CASES = {
    "new-doc-two-sightings": _case_new_doc_two_sightings,
    "second-subject-known-doc": _case_second_subject_links_known_doc,
    "known-doc-one-new-sighting": _case_known_doc_one_new_sighting,
    "same-file-id-two-hashes": _case_same_file_id_under_two_hashes,
    "doc-with-no-sighting": _case_doc_with_no_sighting,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_s2_c6_counts_equal_the_rows_inserted_per_table(writes, conn, case):
    s1 = await _seed(conn)
    s2 = await _seed(conn, ids=OTHER)
    docs, (d, s, link) = await CASES[case](conn, s1, s2)
    declare_docs(writes, documents=d, sightings=s, links=link)
    base = await totals(conn)
    out = await regd(conn, dbody(docs))
    assert out["outcome"] == "linked" and out["subject_id"] == s1
    assert (out["counts"]["documents"], out["counts"]["sightings"], out["counts"]["links"]) == (
        d,
        s,
        link,
    )
    after = await totals(conn)
    assert tuple(a - b for a, b in zip(after, base, strict=True)) == (d, s, link)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_s2_c6_dry_run_is_would_link_with_the_same_counts_and_writes_nothing(
    writes, pool, conn, enabled, case
):
    s1 = await _seed(conn)
    s2 = await _seed(conn, ids=OTHER)
    docs, (d, s, link) = await CASES[case](conn, s1, s2)
    stack = Stack(pool, middleware=False)
    async with writes_nothing(conn):
        resp = await postd(stack, dbody(docs, dry_run=True))
        absent = await postd(stack, {k: v for k, v in dbody(docs).items() if k != "dry_run"})
    for answer in (resp.json(), absent.json()):
        assert answer["outcome"] == "would_link" and answer["dry_run"] is True
        assert (
            answer["counts"]["documents"],
            answer["counts"]["sightings"],
            answer["counts"]["links"],
        ) == (d, s, link)
        assert answer["counts"]["descriptive_differences"] == 0


async def _mixed(conn, s1):
    """A: known, with a link of s1 and a sighting, all three differing from what is submitted.
    B: known, no link of s1. C: new."""
    await _seed_doc(conn, "ma", mime=MIME, day=DAY1, channel="drive")
    await _seed_sighting(conn, "wordmaone", "ma", DAY3)
    await _seed_link(conn, s1, "ma", DT)
    await _seed_doc(conn, "mb", channel="drive")
    return [
        doc("ma", files=("wordmaone",), seen=DAY4, mime_type=MIME2, doc_type=DT2, first_channel="drive"),
        doc("mb", files=("wordmbone",)),
        doc("mc", files=("wordmcone",)),
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_s2_c6_mixed_request_dry_run_and_real_run_have_the_same_four_counts(
    writes, pool, conn, enabled
):
    s1 = await _seed(conn)
    docs = await _mixed(conn, s1)
    expected = {"documents": 1, "sightings": 2, "links": 2, "descriptive_differences": 3}
    stack = Stack(pool, middleware=False)
    async with writes_nothing(conn):
        dry = await postd(stack, dbody(docs, dry_run=True))
    assert dry.json()["outcome"] == "would_link" and dry.json()["counts"] == expected
    declare_docs(writes, documents=1, sightings=2, links=2)
    real = await postd(stack, dbody(docs))
    assert real.json()["outcome"] == "linked" and real.json()["counts"] == expected


@pytest.mark.asyncio
async def test_s2_c6_schema_refuses_orphans_and_cascades_as_specified(writes, conn):
    s1 = await _seed(conn)
    sighting = (
        "INSERT INTO related_document_sightings (kind, ref, sha256, seen_on, created_by) "
        "VALUES ('drive', $1, $2, now(), 'x')"
    )
    link = (
        "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, created_by) "
        "VALUES ($1, $2, 'zzdoctype', '{}', 'x')"
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(sighting, fid("wordorphan"), dh("no-doc"))
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(link, s1, dh("no-doc"))
    await _seed_doc(conn, "schema-doc")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute(link, 987654321, dh("schema-doc"))
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(sighting, "short", dh("schema-doc"))
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
            "first_channel, evidence, created_by) VALUES ($1, 0, $2, now(), 'drive', '{}', 'x')",
            dh("zero-size"), MIME,
        )  # fmt: skip
    assert await totals(conn) == (1, 0, 0)  # the seeded document only


@pytest.mark.asyncio
async def test_s2_c6_deleting_a_subject_deletes_its_links_and_leaves_the_documents(writes, conn):
    writes("related_subjects", "related_subject_identifiers", "related_subject_documents")
    writes.ops("related_subjects", "DELETE")
    writes.ops("related_subject_identifiers", "DELETE")
    writes.ops("related_subject_documents", "DELETE")
    writes.counts(related_subjects=0, related_subject_identifiers=0, related_subject_documents=0)
    s1 = await _seed(conn)
    await _seed_doc(conn, "del-a")
    await _seed_link(conn, s1, "del-a")
    await conn.execute("DELETE FROM related_subjects WHERE id = $1", s1)
    assert await _count(conn, "related_subject_documents") == 0
    assert await _count(conn, "related_documents") == 1


@pytest.mark.asyncio
async def test_s2_c6_a_linked_document_cannot_be_deleted_an_unlinked_one_takes_its_sightings(
    writes, conn
):
    writes("related_documents", "related_document_sightings")
    writes.ops("related_documents", "DELETE")
    writes.ops("related_document_sightings", "DELETE")
    writes.counts(related_documents=0, related_document_sightings=0)
    s1 = await _seed(conn)
    await _seed_doc(conn, "del-b")
    await _seed_sighting(conn, "worddelb", "del-b")
    await _seed_link(conn, s1, "del-b")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await conn.execute("DELETE FROM related_documents WHERE sha256 = $1", dh("del-b"))
    await _seed_doc(conn, "del-c")
    await _seed_sighting(conn, "worddelc", "del-c")
    await conn.execute("DELETE FROM related_documents WHERE sha256 = $1", dh("del-c"))
    assert await totals(conn) == (1, 1, 1)  # b keeps its document, its sighting and its link


# ── item 7: first write wins, descriptive_differences ────────────────────────


async def _existing_everything(conn, s1):
    await _seed_doc(conn, "fw", mime=MIME, day=DAY1, channel="whatsapp")
    await _seed_sighting(conn, "wordfwone", "fw", DAY3)
    await _seed_link(conn, s1, "fw", DT)


FW_BASE = {"files": ("wordfwone",), "first_channel": "whatsapp", "seen": DAY3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"mime_type": MIME2},
        {"first_seen_on": DAY2},
        {"first_channel": "drive"},
        {"seen": DAY4},
        {"doc_type": DT2},
    ],
    ids=["mime", "first_seen_on", "first_channel", "seen_on", "doc_type"],
)
async def test_s2_c7_a_replay_with_another_descriptive_value_writes_nothing(
    writes, pool, conn, enabled, change
):
    s1 = await _seed(conn)
    await _existing_everything(conn, s1)
    before = await registry_rows(conn)
    stack = Stack(pool, middleware=False)
    async with writes_nothing(conn):
        resp = await postd(stack, dbody([doc("fw", **{**FW_BASE, **change})]))
    assert resp.json()["outcome"] == "already_linked"
    assert resp.json()["counts"] == {
        "documents": 0, "sightings": 0, "links": 0, "descriptive_differences": 1,
    }  # fmt: skip
    assert await registry_rows(conn) == before


@pytest.mark.asyncio
async def test_s2_c7_descriptive_differences_follows_its_definition(writes, conn):
    s1 = await _seed(conn)
    s2 = await _seed(conn, ids=OTHER)
    await _existing_everything(conn, s1)
    async with writes_nothing(conn):
        # one row differing in two attributes counts 1
        two = await regd(
            conn, dbody([doc("fw", **{**FW_BASE, "mime_type": MIME2, "first_channel": "drive"})])
        )
        assert two["counts"]["descriptive_differences"] == 1
        # a document, one of its sightings and its link all differing count 3
        three = await regd(
            conn,
            dbody(
                [
                    doc(
                        "fw",
                        files=("wordfwone",),
                        seen=DAY4,
                        mime_type=MIME2,
                        doc_type=DT2,
                        first_channel="whatsapp",
                    )
                ]
            ),
        )
        assert three["counts"]["descriptive_differences"] == 3
        # nothing differing counts 0
        same = await regd(conn, dbody([doc("fw", **FW_BASE)]))
        assert same["counts"]["descriptive_differences"] == 0
    # a row of another request that is not among the submitted keys counts 0
    await _seed_link(conn, s2, "fw", DT2)  # another subject's link on the same document
    await _seed_sighting(conn, "wordfwtwo", "fw", DAY4)  # another file id, not submitted
    async with writes_nothing(conn):
        other = await regd(conn, dbody([doc("fw", **FW_BASE)]))
    assert other["counts"] == {
        "documents": 0,
        "sightings": 0,
        "links": 0,
        "descriptive_differences": 0,
    }


@pytest.mark.asyncio
async def test_s2_c7_a_known_hash_with_another_size_is_a_conflict_and_nothing_is_written(
    writes, pool, conn, enabled
):
    await _seed(conn)
    await _seed_doc(conn, "known", size=100)
    stack = Stack(pool, middleware=False)
    payload = dbody([doc("fresh", files=("wordfresh",)), doc("known", size=200)])
    async with writes_nothing(conn):
        resp = await postd(stack, payload)
        dry = await postd(stack, {**payload, "dry_run": True})
    for answer in (resp.json(), dry.json()):
        assert answer["outcome"] == "conflict" and answer["reason"] == "document_differs"
    assert await totals(conn) == (1, 0, 0)  # the seeded document only: the fresh one is absent


# ── item 8: order ────────────────────────────────────────────────────────────


class _ArgsConn(_Conn):
    """_Conn whose hook sees (sql, args) and which counts the transactions (attempts) it opens."""

    def __init__(self, real, hook=None):
        super().__init__(real, hook)
        self.attempts = 0

    def transaction(self, *a, **kw):
        self.attempts += 1
        return self._real.transaction(*a, **kw)

    async def _run(self, method, sql, args):
        if self._hook is not None:
            await self._hook(sql, args)
        return await getattr(self._real, method)(sql, *args)


async def _normalized(conn):
    rows = {}
    rows["docs"] = sorted(
        tuple(r)
        for r in await conn.fetch(
            "SELECT sha256, byte_size, mime_type, first_seen_on, first_channel, evidence::text, "
            "created_by FROM related_documents"
        )
    )
    rows["sightings"] = sorted(
        tuple(r)
        for r in await conn.fetch(
            "SELECT kind, ref, sha256, seen_on, created_by FROM related_document_sightings"
        )
    )
    rows["links"] = sorted(
        tuple(r)
        for r in await conn.fetch(
            "SELECT subject_id, sha256, doc_type, evidence::text, created_by FROM related_subject_documents"
        )
    )
    return rows


@pytest.mark.asyncio
async def test_s2_c8_the_service_sorts_and_inserts_documents_then_sightings_then_links(
    writes, conn
):
    declare_docs(writes, documents=3, sightings=4, links=3)
    await _seed(conn)
    docs = [
        doc("o1", files=("wordo1b", "wordo1a")), doc("o2", files=("wordo2a",)),
        doc("o3", files=("wordo3a", "wordo3b")),
    ]  # fmt: skip
    reverse = [{**d, "drive": list(reversed(d["drive"]))} for d in reversed(docs)]
    seen = []

    async def spy(sql, args):
        if sql.startswith("INSERT INTO related_"):
            seen.append((sql.split()[2], args[:2]))

    tr = conn.transaction()
    await tr.start()
    try:
        await regd(_ArgsConn(conn, spy), dbody(docs))
        forward_rows, forward_seen = await _normalized(conn), list(seen)
    finally:
        await tr.rollback()
    seen.clear()
    await regd(_ArgsConn(conn, spy), dbody(reverse))
    assert await _normalized(conn) == forward_rows
    assert seen == forward_seen
    tables = [t for t, _ in seen]
    assert (
        tables
        == ["related_documents"] * 3
        + ["related_document_sightings"] * 4
        + ["related_subject_documents"] * 3
    )
    documents = [a[0] for t, a in seen if t == "related_documents"]
    sightings = [a for t, a in seen if t == "related_document_sightings"]
    links = [a[1] for t, a in seen if t == "related_subject_documents"]
    assert documents == sorted(documents) and sightings == sorted(sightings)
    assert links == sorted(links)


# ── item 9: concurrency with a barrier, and the budget of two attempts ───────


def any_insert_barrier(barrier):
    state = {"waited": False}

    async def hook(sql, args):
        if sql.startswith("INSERT INTO related_") and not state["waited"]:
            state["waited"] = True
            await asyncio.wait_for(barrier.wait(), timeout=20)

    return hook


async def _race_docs(pool, payloads, *, raise_errors=True):
    barrier = asyncio.Barrier(len(payloads))

    async def one(payload):
        async with pool.acquire() as c:
            return await regd(_ArgsConn(c, any_insert_barrier(barrier)), payload)

    results = await asyncio.gather(*(one(p) for p in payloads), return_exceptions=True)
    if raise_errors:
        for result in results:
            if isinstance(result, BaseException):
                raise result
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize("callers", [2, 3])
async def test_s2_c9_identical_requests_make_exactly_one_row_per_key(writes, pool, conn, callers):
    declare_docs(writes, documents=1, sightings=2, links=1)
    await _seed(conn)
    payload = dbody([doc("race-a", files=("wordrace1", "wordrace2"))])
    results = await _race_docs(pool, [payload] * callers)
    assert sorted(r["outcome"] for r in results) == ["already_linked"] * (callers - 1) + ["linked"]
    assert await totals(conn) == (1, 2, 1)


@pytest.mark.asyncio
async def test_s2_c9_two_subjects_linking_the_same_new_document_make_one_document_two_links(
    writes, pool, conn
):
    declare_docs(writes, documents=1, sightings=1, links=2)
    await _seed(conn)
    await _seed(conn, ids=OTHER)
    results = await _race_docs(
        pool,
        [
            dbody([doc("race-b", files=("wordraceb",))]),
            dbody([doc("race-b", files=("wordraceb",))], ids=OTHER),
        ],
    )
    assert [r["outcome"] for r in results] == ["linked", "linked"]
    assert sorted(
        (r["counts"]["documents"], r["counts"]["sightings"], r["counts"]["links"]) for r in results
    ) == [
        (0, 0, 1),
        (1, 1, 1),
    ]  # the loser's counts are those of the attempt that committed
    assert await _count(conn, "related_documents") == 1
    assert await _count(conn, "related_subject_documents") == 2


@pytest.mark.asyncio
async def test_s2_c9_the_same_new_hash_with_two_sizes_leaves_one_row_and_a_conflict(
    writes, pool, conn
):
    declare_docs(writes, documents=1, sightings=1, links=1)
    await _seed(conn)
    await _seed(conn, ids=OTHER)
    results = await _race_docs(
        pool,
        [
            dbody([doc("race-c", size=100, files=("wordracec",))]),
            dbody([doc("race-c", size=200, files=("wordracec",))], ids=OTHER),
        ],
        raise_errors=False,
    )
    outcomes = sorted(r["outcome"] for r in results if isinstance(r, dict))
    assert outcomes == ["conflict", "linked"], results
    assert await _count(conn, "related_documents") == 1
    assert await _count(conn, "related_subject_documents") == 1


async def _winner_doc(side, label, *, size=1234):
    """A request that won the race first: written through fixture SQL on another connection."""
    await _seed_doc(side, label, size=size)


def _sorted_labels(*labels):
    return sorted(labels, key=dh)


@pytest.mark.asyncio
async def test_s2_c9_a_request_that_loses_its_race_twice_is_busy_and_stores_nothing(
    writes, pool, conn, enabled
):
    x, y, z = _sorted_labels("busy-x", "busy-y", "busy-z")
    declare_docs(writes, documents=2, sightings=2, links=2)
    await _seed(conn)
    await _seed(conn, ids=OTHER)
    await _seed(conn, ids=(("passport", "ZZ0000004"),))
    state = {"y": False, "z": False}

    async def hook(sql, args):
        if not sql.startswith("INSERT INTO related_documents "):
            return
        if args[0] == dh(y) and not state["y"]:
            state["y"] = True
            async with pool.acquire() as c:
                await regd(c, dbody([doc(y, files=("wordwiny",))], ids=OTHER))
        if args[0] == dh(z) and not state["z"]:
            state["z"] = True
            async with pool.acquire() as c:
                await regd(
                    c, dbody([doc(z, files=("wordwinz",))], ids=(("passport", "ZZ0000004"),))
                )

    loser = dbody(
        [doc(x, files=("wordlosex",)), doc(y, files=("wordlosey",)), doc(z, files=("wordlosez",))]
    )
    wrapped = _PoolOf(pool, lambda c: _ArgsConn(c, hook))
    resp = await postd(Stack(wrapped, middleware=False), loser)
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_busy"})
    assert state == {"y": True, "z": True}
    docs = {r["sha256"] for r in await conn.fetch("SELECT sha256 FROM related_documents")}
    assert docs == {dh(y), dh(z)}  # the winners' rows; the loser's own x is absent
    assert {r["ref"] for r in await conn.fetch("SELECT ref FROM related_document_sightings")} == {
        fid("wordwiny"),
        fid("wordwinz"),
    }
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM related_subject_documents WHERE subject_id = (SELECT subject_id FROM related_subject_identifiers WHERE id_norm = 'ZZ0000001')"
        )
        == 0
    )


LOST = {
    "document": ("INSERT INTO related_documents ", "doc"),
    "sighting": ("INSERT INTO related_document_sightings", "sighting"),
    "link": ("INSERT INTO related_subject_documents", "link"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("lost", sorted(LOST))
async def test_s2_c9_a_lost_insert_abandons_the_attempt_and_the_second_one_answers(
    writes, conn, pool, lost
):
    s1 = await _seed(conn)
    if lost == "document":
        declare_docs(writes, sightings=1, links=1)
        expected = (0, 1, 1)
    elif lost == "sighting":
        await _seed_doc(conn, "lost-d", channel="drive")
        declare_docs(writes, links=1)
        expected = (0, 0, 1)
    else:
        await _seed_doc(conn, "lost-d", channel="drive")
        await _seed_sighting(conn, "wordlost", "lost-d")
        expected = None
    state = {"done": False}

    async def hook(sql, args):
        if sql.startswith(LOST[lost][0]) and not state["done"]:
            state["done"] = True
            async with pool.acquire() as side:
                if lost == "document":
                    await _seed_doc(side, "lost-d", channel="drive")
                elif lost == "sighting":
                    await _seed_sighting(side, "wordlost", "lost-d")
                else:
                    await _seed_link(side, s1, "lost-d")

    async with pool.acquire() as lc:
        wrapped = _ArgsConn(lc, hook)
        out = await regd(
            wrapped, dbody([doc("lost-d", files=("wordlost",), first_channel="drive")])
        )
    assert state["done"] and wrapped.attempts == 2
    if expected is None:
        assert out["outcome"] == "already_linked"
        expected = (0, 0, 0)
    else:
        assert out["outcome"] == "linked"
    assert (
        out["counts"]["documents"],
        out["counts"]["sightings"],
        out["counts"]["links"],
    ) == expected
    assert await totals(conn) == (1, 1, 1)


CAUSES = ("race", "deadlock", "serialization")


def _cause_hook(pool, causes, winners):
    """The first INSERT into related_documents of attempt n suffers causes[n-1]."""
    done: set[int] = set()
    holder: dict = {}

    async def hook(sql, args):
        if not sql.startswith("INSERT INTO related_documents "):
            return
        attempt = holder["conn"].attempts
        if attempt in done or attempt > len(causes):
            return
        done.add(attempt)
        cause = causes[attempt - 1]
        if cause == "race":
            async with pool.acquire() as side:
                async with unmeasured(side):
                    await side.execute(
                        "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
                        "first_channel, evidence, created_by) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
                        args[0], args[1], args[2], args[3], args[4], args[5], args[6],
                    )  # fmt: skip
            winners.append(args[0])
        elif cause == "deadlock":
            raise asyncpg.DeadlockDetectedError("deadlock detected")
        else:
            raise asyncpg.SerializationError("could not serialize access")

    return hook, holder


@pytest.mark.asyncio
@pytest.mark.parametrize("second", CAUSES)
@pytest.mark.parametrize("first", CAUSES)
async def test_s2_c9_one_budget_of_two_attempts_for_every_ordered_pair_of_causes(
    writes, pool, conn, first, second
):
    await _seed(conn)
    winners: list[str] = []
    hook, holder = _cause_hook(pool, (first, second), winners)
    payload = dbody([doc("bud-a", files=("wordbuda",)), doc("bud-b", files=("wordbudb",))])
    async with pool.acquire() as lc:
        wrapped = holder["conn"] = _ArgsConn(lc, hook)
        with pytest.raises(RegistryUnavailable) as caught:
            await regd(wrapped, payload)
    assert caught.value.code == "registry_busy"
    assert wrapped.attempts == 2, "there is no third attempt"
    docs = {r["sha256"] for r in await conn.fetch("SELECT sha256 FROM related_documents")}
    assert docs == set(winners) and len(winners) == (first, second).count("race")
    assert await _count(conn, "related_document_sightings") == 0
    assert await _count(conn, "related_subject_documents") == 0


@pytest.mark.asyncio
async def test_s2_c9_a_second_failure_over_http_is_the_fixed_503_and_leaves_no_row(
    writes, pool, conn, enabled
):
    await _seed(conn)
    hook, holder = _cause_hook(pool, ("deadlock", "race"), [])
    wrapped = _PoolOf(pool, lambda c: holder.setdefault("conn", _ArgsConn(c, hook)))
    payload = dbody([doc("bud-h", files=("wordbudh",)), doc("bud-i")])
    resp = await postd(Stack(wrapped, middleware=False), payload)
    assert (resp.status_code, resp.json()) == (503, {"detail": "registry_busy"})
    assert await _count(conn, "related_document_sightings") == 0
    assert await _count(conn, "related_subject_documents") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["deadlock", "serialization"])
async def test_s2_c9_a_forced_failure_then_success_counts_the_second_attempt_alone(
    writes, pool, conn, cause
):
    await _seed(conn)
    first, second = _sorted_labels("two-att-a", "two-att-b")
    state = {"done": False}
    holder = {}

    async def hook(sql, args):
        if (
            sql.startswith("INSERT INTO related_documents ")
            and args[0] == dh(second)
            and not state["done"]
        ):
            state["done"] = True
            await _seed_doc(conn, second)  # another request committed the second document
            raise (
                asyncpg.DeadlockDetectedError("deadlock detected")
                if cause == "deadlock"
                else asyncpg.SerializationError("could not serialize access")
            )

    declare_docs(writes, documents=1, sightings=2, links=2)
    payload = dbody([doc(first, files=("wordatta",)), doc(second, files=("wordattb",))])
    async with pool.acquire() as lc:
        wrapped = holder["conn"] = _ArgsConn(lc, hook)
        out = await regd(wrapped, payload)
    assert wrapped.attempts == 2 and out["outcome"] == "linked"
    assert out["counts"] == {
        "documents": 1,
        "sightings": 2,
        "links": 2,
        "descriptive_differences": 0,
    }
    assert await totals(conn) == (2, 2, 2)


@pytest.mark.asyncio
async def test_s2_c9_a_failed_commit_follows_the_same_budget(writes, conn, pool, enabled):
    declare_docs(writes, documents=1, links=1)
    await _seed(conn)
    one = _CommitConn(conn, 1, lambda: asyncpg.SerializationError("x"))
    assert (await regd(one, dbody([doc("cm-a")])))["outcome"] == "linked"
    two = _CommitConn(conn, 2, lambda: asyncpg.SerializationError("x"))
    async with writes_nothing(conn):
        with pytest.raises(RegistryUnavailable):
            await regd(two, dbody([doc("cm-b")]))


# ── item 10: rollback at each stage ──────────────────────────────────────────


class _Stage(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after-documents", "after-sightings", "before-commit"])
async def test_s2_c10_a_failure_at_each_stage_leaves_none_of_the_rows(writes, conn, stage):
    await _seed(conn)
    payload = dbody([doc("rb-a", files=("wordrba",)), doc("rb-b", files=("wordrbb",))])

    async def hook(sql, args):
        if stage == "after-documents" and sql.startswith("INSERT INTO related_document_sightings"):
            raise _Stage
        if stage == "after-sightings" and sql.startswith("INSERT INTO related_subject_documents"):
            raise _Stage

    target = _CommitConn(conn, 1, _Stage) if stage == "before-commit" else _ArgsConn(conn, hook)
    async with writes_nothing(conn):
        with pytest.raises(_Stage):
            await regd(target, payload)
    assert await totals(conn) == (0, 0, 0)


# ── item 11: validation ──────────────────────────────────────────────────────

SENT_HASH, SENT_FILE, SENT_TYPE = "ZzSentinelHashValue", "ZzSentinelFileValue", "Zz Sentinel Type"


def _bad(mutate):
    payload = dbody([doc("val", files=("wordval1", "wordval2"))])
    mutate(payload)
    return payload


def _set(path, value):
    def apply(payload):
        node = payload
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value

    return apply


INVALID_DOC_BODIES = {
    "malformed_hash": _bad(_set(("documents", 0, "sha256"), SENT_HASH)),
    "uppercase_hash": _bad(_set(("documents", 0, "sha256"), dh("val").upper())),
    "size_zero": _bad(_set(("documents", 0, "byte_size"), 0)),
    "size_above_bigint": _bad(_set(("documents", 0, "byte_size"), 9223372036854775808)),
    "size_as_string": _bad(_set(("documents", 0, "byte_size"), "123456789")),
    "size_float": _bad(_set(("documents", 0, "byte_size"), 1.5)),
    "malformed_file_id": _bad(_set(("documents", 0, "drive", 0, "file_id"), SENT_FILE + " !")),
    "short_file_id": _bad(_set(("documents", 0, "drive", 0, "file_id"), "short")),
    "malformed_doc_type": _bad(_set(("documents", 0, "doc_type"), SENT_TYPE)),
    "malformed_mime": _bad(_set(("documents", 0, "mime_type"), "NoSlash" + SENT_TYPE)),
    "unknown_channel": _bad(_set(("documents", 0, "first_channel"), "carrier-pigeon-sentinel")),
    "bad_first_seen_date": _bad(_set(("documents", 0, "first_seen_on"), SENT_TYPE)),
    "bad_seen_date": _bad(_set(("documents", 0, "drive", 0, "seen_on"), "2019-13-45")),
    "datetime_as_date": _bad(_set(("documents", 0, "first_seen_on"), "2019-01-02T03:04:05")),
    "dry_run_as_string": _bad(_set(("dry_run",), "no" + SENT_TYPE)),
    "duplicate_hash": _bad(lambda p: p["documents"].append(dict(p["documents"][0]))),
    "duplicate_file_id": _bad(
        lambda p: p["documents"][0]["drive"].append(dict(p["documents"][0]["drive"][0]))
    ),
    "forty_one_documents": dbody([doc(f"many-{n}") for n in range(41)]),
    "nine_file_ids": dbody([doc("nine", files=tuple(f"wordnine{n}" for n in range(9)))]),
    "no_documents": dbody([]),
    "unknown_top_field": {**dbody([doc("val")]), SENT_TYPE: SENT_FILE},
    "unknown_document_field": _bad(_set(("documents", 0, SENT_TYPE), SENT_FILE)),
    "unknown_subject_field": _bad(_set(("subject", SENT_TYPE), SENT_FILE)),
    "identifier_evidence_is_not_accepted": _bad(
        lambda p: p["subject"]["identifiers"][0].update({"evidence": {"files": 1}})
    ),
    "missing_evidence": {k: v for k, v in dbody([doc("val")]).items() if k != "evidence"},
    "bad_subject_ref": _bad(_set(("evidence", "subject_ref"), SENT_TYPE)),
    "bad_plan_sha": _bad(_set(("evidence", "plan_sha256"), SENT_HASH)),
    "kind_type_mismatch": dbody([doc("val")], kind="company", ids=(("passport", SENTINEL_ID),)),
    "failing_identifier": dbody([doc("val")], ids=(("passport", "ZQ447"),)),
    "duplicate_identifier": dbody([doc("val")], ids=(("passport", SENTINEL_ID), ("passport", "zzsent-0001"))),
    "not_an_object": [SENT_TYPE, SENT_FILE],
    "malformed_json": '{"subject": {"kind": "person"}, "documents": [{"sha256": "' + SENT_HASH + '", "drive": [',
}  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(INVALID_DOC_BODIES))
async def test_s2_c11_invalid_bodies_are_422_write_nothing_and_echo_nothing(
    writes, pool, conn, enabled, case
):
    await _seed(conn)
    stack = Stack(pool, middleware=False)
    async with writes_nothing(conn):
        resp = await postd(stack, INVALID_DOC_BODIES[case])
    assert resp.status_code == 422, resp.text
    text = resp.text.lower()
    assert not any(
        w.lower() in text for w in (SENT_HASH, SENT_FILE, SENT_TYPE, "sentinel", "pigeon")
    )
    assert all(set(item) == {"loc", "code"} for item in resp.json()["detail"])


@pytest.mark.asyncio
async def test_s2_c11_the_new_codes_and_the_location_rules_are_produced_by_the_route(
    writes, pool, enabled
):
    stack = Stack(pool, middleware=False)
    codes = {}
    for case in (
        "duplicate_hash",
        "duplicate_file_id",
        "malformed_hash",
        "size_zero",
        "unknown_channel",
        "forty_one_documents",
        "nine_file_ids",
        "bad_first_seen_date",
        "size_as_string",
        "malformed_json",
    ):
        resp = await postd(stack, INVALID_DOC_BODIES[case])
        codes[case] = (resp.json()["detail"][0]["code"], resp.json()["detail"][0]["loc"])
    assert codes["duplicate_hash"] == ("duplicate_document", [])
    assert codes["duplicate_file_id"] == ("duplicate_file_id", [])
    assert codes["malformed_hash"] == ("string_pattern_mismatch", ["documents", 0, "sha256"])
    assert codes["size_zero"] == ("greater_than_equal", ["documents", 0, "byte_size"])
    assert codes["unknown_channel"] == ("literal_error", ["documents", 0, "first_channel"])
    assert codes["forty_one_documents"] == ("too_long", ["documents"])
    assert codes["nine_file_ids"] == ("too_long", ["documents", 0, "drive"])
    assert codes["size_as_string"][0] == "int_type"
    assert codes["bad_first_seen_date"][0] in ("date_parsing", "date_type")
    assert codes["malformed_json"][0] == "json_invalid"
    resp = await postd(stack, INVALID_DOC_BODIES["unknown_document_field"])
    assert resp.json()["detail"][0]["loc"] == ["documents", 0, "_"]
    assert resp.json()["detail"][0]["code"] == "extra_forbidden"


def test_s2_c11_a_location_part_outside_the_closed_lists_is_replaced():
    items = route_module._invalid_documents(
        [
            {"loc": ("documents", 39, "drive", 7, "file_id"), "type": "missing"},
            {"loc": ("documents", 40, "sha256"), "type": "missing"},
            {"loc": ("documents", 0, "drive", 8), "type": "missing"},
            {"loc": ("identifiers", 20), "type": "missing"},
            {"loc": (True, "documents", SENT_TYPE), "type": SENT_TYPE},
            {"loc": ("subject", "identifiers", -1), "type": "too_short"},
        ]
    )
    detail = json.loads(items.body)["detail"]
    assert detail[0]["loc"] == ["documents", 39, "drive", 7, "file_id"]
    assert detail[1]["loc"] == ["documents", "_", "sha256"]
    assert detail[2]["loc"] == ["documents", 0, "drive", "_"]
    assert detail[3]["loc"] == ["identifiers", "_"]
    assert detail[4] == {"loc": ["_", "documents", "_"], "code": "invalid"}
    assert detail[5]["loc"] == ["subject", "identifiers", "_"]


def test_s2_c11_the_slice_one_lists_are_not_widened():
    assert not {"documents", "sha256", "drive", "file_id", "subject"} & route_module._REPORTABLE_LOC
    assert not {"duplicate_document", "duplicate_file_id"} & route_module._REPORTABLE_CODE


@pytest.mark.asyncio
async def test_s2_c11_an_oversized_body_is_refused_with_a_fixed_code(writes, pool, enabled):
    resp = await postd(Stack(pool, middleware=False), "x" * (route_module.MAX_BODY_BYTES + 1))
    assert resp.status_code == 422 and resp.json()["detail"][0]["code"] == "too_long"


def test_s2_c11_the_largest_valid_request_fits_the_cap_as_compact_utf8():
    files = tuple(f"wordmax{n}" for n in range(8))
    ids = tuple(("passport", f"ZZ{n:07d}") for n in range(20))
    payload = dbody([doc(f"max-{n}", files=files) for n in range(40)], ids=ids)
    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    assert len(compact) <= route_module.MAX_BODY_BYTES
    assert dreq(payload).documents and len(dreq(payload).documents) == 40


# ── item 12: auth, dry run, method scoping ───────────────────────────────────


@pytest.mark.asyncio
async def test_s2_c12_flag_off_is_503_even_with_a_valid_key(writes, pool, monkeypatch):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    resp = await postd(Stack(pool, middleware=False), dbody([doc("auth")]))
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
async def test_s2_c12_wrong_or_missing_key_is_401(writes, pool, enabled, headers):
    resp = await postd(Stack(pool, middleware=False), dbody([doc("auth")]), headers)
    assert (resp.status_code, resp.json()) == (401, {"detail": "unauthorized"})


@pytest.mark.asyncio
async def test_s2_c12_dry_run_absent_and_true_write_nothing_on_every_outcome(
    writes, pool, conn, enabled
):
    s1 = await _seed(conn)
    await _seed_doc(conn, "dr-known", size=100)
    await _seed_doc(conn, "dr-linked", channel="drive")
    await _seed_link(conn, s1, "dr-linked")
    stack = Stack(pool, middleware=False)
    cases = {
        "subject_not_registered": dbody([doc("dr-a")], ids=(("passport", "ZZ0000077"),)),
        "conflict": dbody([doc("dr-known", size=200)]),
        "would_link": dbody([doc("dr-new", files=("worddrnew",))]),
        "already_linked": dbody([doc("dr-linked", first_channel="drive")]),
    }
    async with writes_nothing(conn):
        for outcome, payload in cases.items():
            absent = {k: v for k, v in payload.items() if k != "dry_run"}
            for variant in (absent, {**payload, "dry_run": True}):
                resp = await postd(stack, variant)
                expected = "would_link" if outcome == "would_link" else outcome
                assert resp.json()["outcome"] == expected and resp.json()["dry_run"] is True


def test_s2_c12_the_public_entry_of_the_new_path_is_scoped_to_post():
    from backend.app.auth.public_endpoints import find_entry, is_public_path

    assert is_public_path(URL2, "POST") and is_public_path(URL2, "post")
    for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"):
        assert not is_public_path(URL2, method), method
    entry = find_entry(URL2, "POST")
    assert entry is not None and entry.methods == ("POST",) and entry.match == "exact"
    assert find_entry(URL2 + "/", "POST") is None and find_entry(URL2 + "/x", "POST") is None


def test_s2_c12_the_slice_one_entry_behaves_as_before_for_every_method():
    from backend.app.auth.public_endpoints import find_entry

    for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", None):
        entry = find_entry(URL, method)
        assert entry is not None and entry.prefix == URL and entry.methods is None, method


@pytest.mark.asyncio
async def test_s2_c12_post_reaches_the_registry_dependency_and_get_does_not(writes, pool, enabled):
    stack = Stack(pool)  # the real middleware stack
    async with stack.client() as http:
        post_resp = await http.post(
            URL2, content="{}", headers={"Content-Type": "application/json"}
        )
        get_resp = await http.get(URL2, headers=HEADERS)
    assert (post_resp.status_code, post_resp.json()) == (401, {"detail": "unauthorized"})
    assert get_resp.status_code != 200 and get_resp.json() != {"detail": "unauthorized"}


# ── item 13: privacy, inside the stated boundary ─────────────────────────────

SECRET_DOC_BODY = dbody([doc("secret", files=("wordsecret",))], ids=(("passport", SENTINEL_ID),))


async def _privacy_run_docs(pool, setup, payload, *, expect_status):
    stack = Stack(pool)
    setup(stack)
    resp = await postd(stack, payload)
    assert resp.status_code == expect_status, resp.text
    return resp, stack


@pytest.mark.asyncio
async def test_s2_c13_privacy_on_every_outcome(writes, pool, conn, enabled, audit_logger):
    declare_docs(writes, documents=1, sightings=1, links=1, extra=("api_audit_trail",))
    await _seed(conn, ids=(("passport", SENTINEL_ID),))
    await _seed_doc(conn, "secret-known", size=10)
    outcomes = []
    for payload in (
        SECRET_DOC_BODY,  # linked
        SECRET_DOC_BODY,  # already_linked
        {**SECRET_DOC_BODY, "dry_run": True},  # already_linked, dry
        dbody(
            [doc("secret-two", files=("wordsecrettwo",))],
            ids=(("passport", SENTINEL_ID),),
            dry_run=True,
        ),  # would_link
        dbody([doc("secret-known", size=11)], ids=(("passport", SENTINEL_ID),)),  # conflict
        dbody([doc("secret")], ids=(("passport", "ZZ0000077"),)),  # subject_not_registered
    ):
        resp, _ = await _privacy_run_docs(pool, lambda s: None, payload, expect_status=200)
        outcomes.append(resp.json()["outcome"])
    assert outcomes == [
        "linked", "already_linked", "already_linked", "would_link", "conflict", "subject_not_registered",
    ]  # fmt: skip


@pytest.mark.asyncio
async def test_s2_c13_privacy_on_422(writes, pool, conn, enabled, audit_logger):
    writes("api_audit_trail")
    for case in sorted(INVALID_DOC_BODIES):
        await _privacy_run_docs(pool, lambda s: None, INVALID_DOC_BODIES[case], expect_status=422)


async def _broken_auth():
    raise RuntimeError(LEAKY)


def _broken_pool():
    raise RuntimeError(LEAKY)


def _inject_auth(stack):
    stack.app.dependency_overrides[route_module.verify_related_subject_write] = _broken_auth


def _inject_pool_dependency(stack):
    stack.app.dependency_overrides[get_database_pool] = _broken_pool


def _inject_pool_error(stack):
    stack.app.state.db_pool = _Boom(
        asyncpg.UniqueViolationError(f"Key (sha256)=({dh('secret')}) {LEAKY}")
    )


def _inject_unexpected(stack):
    stack.app.state.db_pool = _Boom(RuntimeError(LEAKY))


def _inject_acquire_runtime(stack):
    stack.app.state.db_pool = _PoolOf(stack.app.state.db_pool, fail_enter=RuntimeError(LEAKY))


def _inject_acquire_database(stack):
    stack.app.state.db_pool = _PoolOf(
        stack.app.state.db_pool, fail_enter=asyncpg.PostgresError(LEAKY)
    )


def _inject_commit_twice(factory):
    def inject(stack):
        stack.app.state.db_pool = _PoolOf(
            stack.app.state.db_pool, lambda c: _CommitConn(c, 2, factory)
        )

    return inject


INJECTIONS = {
    "auth-dependency": (_inject_auth, 500, "internal_error"),
    "pool-dependency": (_inject_pool_dependency, 500, "internal_error"),
    "database-error-echoing-values": (_inject_pool_error, 503, "database_error"),
    "unexpected-service-failure": (_inject_unexpected, 500, "internal_error"),
    "pool-acquisition-runtime": (_inject_acquire_runtime, 500, "internal_error"),
    "pool-acquisition-database": (_inject_acquire_database, 503, "database_error"),
    "commit-runtime": (_inject_commit_twice(lambda: RuntimeError(LEAKY)), 500, "internal_error"),
    "commit-database": (_inject_commit_twice(lambda: asyncpg.QueryCanceledError(LEAKY)), 503, "database_error"),
    "commit-serialization-twice": (_inject_commit_twice(lambda: asyncpg.SerializationError(LEAKY)), 503, "registry_busy"),
}  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(INJECTIONS))
async def test_s2_c13_privacy_on_injected_failures(writes, pool, conn, enabled, audit_logger, name):
    writes("api_audit_trail")
    await _seed(conn, ids=(("passport", SENTINEL_ID),))
    setup, status, code = INJECTIONS[name]
    async with writes_only(conn, {"api_audit_trail"}):
        resp, _ = await _privacy_run_docs(pool, setup, SECRET_DOC_BODY, expect_status=status)
    assert resp.json() == {"detail": code}


@pytest.mark.asyncio
async def test_s2_c13_privacy_when_parsing_or_building_the_answer_fails(
    writes, pool, conn, enabled, audit_logger, monkeypatch
):
    writes("api_audit_trail")

    def boom(*a, **kw):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(route_module.RelatedDocumentsRegisterRequest, "model_validate_json", boom)
    resp, _ = await _privacy_run_docs(pool, lambda s: None, SECRET_DOC_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_s2_c13_privacy_when_building_the_success_response_fails(
    writes, pool, conn, enabled, audit_logger, monkeypatch
):
    declare_docs(writes, documents=1, sightings=1, links=1, extra=("api_audit_trail",))
    await _seed(conn, ids=(("passport", SENTINEL_ID),))

    def boom(*a, **kw):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(route_module, "JSONResponse", boom)
    resp, _ = await _privacy_run_docs(pool, lambda s: None, SECRET_DOC_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}


@pytest.mark.asyncio
async def test_s2_c13_privacy_when_releasing_the_connection_fails(
    writes, pool, conn, enabled, audit_logger
):
    declare_docs(writes, documents=1, sightings=1, links=1, extra=("api_audit_trail",))
    await _seed(conn, ids=(("passport", SENTINEL_ID),))

    def setup(stack):
        stack.app.state.db_pool = _PoolOf(pool, fail_exit=RuntimeError(LEAKY))

    resp, _ = await _privacy_run_docs(pool, setup, SECRET_DOC_BODY, expect_status=500)
    assert resp.json() == {"detail": "internal_error"}
    assert await _count(conn, "related_documents") == 1  # committed before the release failed


@pytest.mark.asyncio
async def test_s2_c13_privacy_when_reading_the_body_fails(
    writes, pool, conn, enabled, audit_logger
):
    writes("api_audit_trail")
    stack = Stack(pool)
    tokens = forbidden_tokens(SECRET_DOC_BODY, DOC_PROTOCOL) | {LEAKY.lower()}
    trace_before = set(request_tracing._trace_storage)

    async def receive():
        raise RuntimeError(LEAKY)

    with capture_logs() as records:
        status, text, headers = await _asgi_post(stack.app, receive, path=URL2)
    assert status == 500 and json.loads(text) == {"detail": "internal_error"}
    await observe(stack, tokens, text + headers, records, trace_before)


# ── item 15: migration ───────────────────────────────────────────────────────


def test_s2_c15_migration_is_362_alone_and_the_rollback_drops_leaves_first():
    numbered = sorted(p.name for p in MIGRATIONS.glob("362_*.sql"))
    assert numbered == ["362_related_documents.sql"]
    assert sorted(p.name for p in MIGRATIONS.glob("361_*.sql")) == [
        "361_related_subjects_registry.sql"
    ]
    forward, rollback = split_migration_sql(MIGRATION2.read_text())
    assert rollback is not None and "related_subjects" not in rollback.replace(
        "related_subject_documents", ""
    )
    order = [m.group(1) for m in re.finditer(r"DROP TABLE IF EXISTS (\w+)", rollback)]
    assert order == ["related_subject_documents", "related_document_sightings", "related_documents"]
    assert not re.search(
        r"REFERENCES (clients|documents|companies|client_family_members)\b", forward
    )
    assert "TRIGGER" not in forward.upper() and "CREATE VIEW" not in forward.upper()


@pytest.mark.asyncio
async def test_s2_c15_the_slice_two_rollback_alone_leaves_slice_one_working(writes, pool, conn):
    _, rollback2 = split_migration_sql(MIGRATION2.read_text())
    async with pool.acquire() as c:
        await c.execute(rollback2)
    left = await conn.fetchval(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = current_schema() AND table_name = ANY($1::text[])",
        sorted(DOC_TABLES),
    )
    assert left == 0
    writes(*NEW_TABLES)
    writes.counts(related_subjects=1, related_subject_identifiers=1)
    assert (await reg(conn, subj(), dry_run=False))["outcome"] == "created"
