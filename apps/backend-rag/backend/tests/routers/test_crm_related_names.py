"""Related-subjects registry, slice 4A: recorded names, on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362 and 364 forward
parts plus the audit table), seeds invented rows (``Zz`` / ``ZZ`` conventions) and calls the real
routers behind the real middleware stack. Nothing here is a real person, company or document.

The ids N1 to N14 are those of the specification. Write measurement: during the execution of a
route the only writes are INSERTs into ``related_subject_names`` (the names route) and the audit
table; during the read routes there is no write at all. Fixtures, the migration, its rollback and
the deliberate cascade delete of N1 run inside ``unmeasured``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
from backend.services.crm.related_identifiers import canonical_form
from backend.services.crm.related_names import (
    InvalidName,
    NameEntry,
    NamesInput,
    normalise_name,
    record_names,
)
from backend.services.crm.related_subjects import REGISTRY_ACTOR, prepare_identifiers
from backend.services.monitoring.activity_logger import activity_logger

MIGRATIONS = Path(route_module.__file__).resolve().parents[2] / "db" / "migrations_v2"
MIGRATION_NAMES = MIGRATIONS / "364_related_subject_names.sql"
URL = "/api/crm/related-subjects/names/register"
URL_SUBJECT = "/api/crm/related-subjects/register"
URL_DOCUMENTS = "/api/crm/related-subjects/documents/register"
READ = "/api/crm/related-registry"
KEY = "test-crm-write-key-0000"
HEADERS = {"X-CRM-Write-Key": KEY, "Content-Type": "application/json"}
PLAN = "ab12" * 16

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}

NAMES_INSERT = ("related_subject_names", "INSERT")
AUDIT_INSERT = ("api_audit_trail", "INSERT")

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
    schema = "related_names_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn, min_size=1, max_size=6, server_settings={"search_path": schema}
    )
    try:
        async with created.acquire() as c:
            for migration in (
                "361_related_subjects_registry.sql",
                "362_related_documents.sql",
                "364_related_subject_names.sql",
            ):
                forward, _ = split_migration_sql((MIGRATIONS / migration).read_text())
                await c.execute(forward)
            await c.execute(AUDIT_SCHEMA)
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


async def seed_name(c, sid, name, source, *, at=None):
    async with unmeasured(c):
        return await c.fetchval(
            "INSERT INTO related_subject_names (subject_id, name, source, evidence, created_by, "
            "created_at) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed', coalesce($4, now())) "
            "RETURNING id",
            sid, name, source, at,
        )  # fmt: skip


async def name_rows(c, sid=None):
    where = "" if sid is None else " WHERE subject_id = " + str(int(sid))
    return await c.fetch(
        "SELECT id, subject_id, name, source, evidence, created_by FROM related_subject_names"
        + where
        + " ORDER BY id"
    )


# ── request builders ──────────────────────────────────────────────────────────

P1, P2, P3, P4 = "ZZ0000001", "ZZ0000002", "ZZ0000003", "ZZ0000004"
NIK1 = "3201 0000 0000 0001"
UNOWNED, UNOWNED2 = "ZZ9999991", "ZZ9999992"
_DEFAULT_COUNTS = {"ocr_single": (1, 1), "ocr_corroborated": (2, 2), "mrz_corrected": (1, 1)}


def entry(name, source="ocr_single", files=None, blobs=None, mrz=False):
    default_files, default_blobs = _DEFAULT_COUNTS.get(source, (1, 1))
    return {
        "name": name,
        "source": source,
        "evidence": {
            "plan_sha256": PLAN,
            "files": default_files if files is None else files,
            "blobs": default_blobs if blobs is None else blobs,
            "mrz_valid": mrz,
        },
    }


def payload(names, ids=(("passport", P1),), kind="person", **over):
    data = {
        "subject": {"kind": kind, "identifiers": [{"type": t, "value": v} for t, v in ids]},
        "names": list(names),
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


async def post(client, data, *, headers=HEADERS, url=URL):
    raw = data if isinstance(data, (bytes, str)) else json.dumps(data)
    return await client.post(url, content=raw, headers=headers)


def errors_of(resp):
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def loc_codes(resp):
    return [(e["loc"], e["code"]) for e in errors_of(resp)]


# ── N1 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n1_migration_table_constraints_cascade_and_rollback(pool, conn):
    sid = await seed_subject(conn, "person", "ZzN1 Person", [("passport", P1)])
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'related_subject_names'"
        )
    }
    assert cols == {
        "id": ("bigint", "NO"),
        "subject_id": ("bigint", "NO"),
        "name": ("text", "NO"),
        "source": ("text", "NO"),
        "evidence": ("jsonb", "NO"),
        "created_by": ("text", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
    }
    identity = await conn.fetchrow(
        "SELECT is_identity, identity_generation FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'related_subject_names' "
        "AND column_name = 'id'"
    )
    assert (identity["is_identity"], identity["identity_generation"]) == ("YES", "ALWAYS")
    indexes = [
        r["indexdef"]
        for r in await conn.fetch(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND tablename = 'related_subject_names'"
        )
    ]
    assert any(d.endswith("(subject_id)") for d in indexes), indexes

    insert = (
        "INSERT INTO related_subject_names (subject_id, name, source, evidence, created_by) "
        "VALUES ($1, $2, $3, $4::jsonb, 'zz')"
    )
    async with unmeasured(conn):
        await conn.execute(insert, sid, "ZZ ALPHA", "ocr_single", "{}")
        # the database accepts what a later slice will write
        await conn.execute(insert, sid, "ZZ ALPHA", "manual", "{}")
        refused = [
            (asyncpg.CheckViolationError, (sid, "ZZ BETA", "guessed", "{}")),
            (asyncpg.UniqueViolationError, (sid, "ZZ ALPHA", "ocr_single", "{}")),
            (asyncpg.CheckViolationError, (sid, "", "ocr_single", "{}")),
            (asyncpg.CheckViolationError, (sid, " ZZ BETA", "ocr_single", "{}")),
            (asyncpg.CheckViolationError, (sid, "ZZ BETA ", "ocr_single", "{}")),
            (asyncpg.CheckViolationError, (sid, "Z" * 201, "ocr_single", "{}")),
            (asyncpg.CheckViolationError, (sid, "ZZ BETA", "ocr_single", "[]")),
            (asyncpg.ForeignKeyViolationError, (sid + 999, "ZZ BETA", "ocr_single", "{}")),
        ]
        for error, args in refused:
            with pytest.raises(error):
                await conn.execute(insert, *args)
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "INSERT INTO related_subject_names (id, subject_id, name, source, evidence, "
                "created_by) VALUES (7, $1, 'ZZ GAMMA', 'ocr_single', '{}'::jsonb, 'zz')",
                sid,
            )
        assert await conn.fetchval("SELECT count(*) FROM related_subject_names") == 2

        # cascade: deleting the subject deletes its names, nothing else
        other = await conn.fetchval(
            "INSERT INTO related_subjects (kind, evidence, created_by) "
            "VALUES ('person', '{}'::jsonb, 'zz') RETURNING id"
        )
        await conn.execute(insert, other, "ZZ KEEP", "ocr_single", "{}")
        await conn.execute("DELETE FROM related_subjects WHERE id = $1", sid)
        assert [r["name"] for r in await name_rows(conn)] == ["ZZ KEEP"]

        # rollback drops the new table and nothing else
        _, rollback = split_migration_sql(MIGRATION_NAMES.read_text())
        assert rollback and "related_subject_names" in rollback
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
        }
        assert await conn.fetchval("SELECT count(*) FROM related_subjects") == 1


# ── N2 ────────────────────────────────────────────────────────────────────────


def _model_errors(name: str) -> list[tuple[tuple, str]]:
    data = json.dumps(payload([entry(name)]))
    try:
        route_module.RelatedNamesRegisterRequest.model_validate_json(data)
    except ValidationError as exc:
        return [(e["loc"], e["type"]) for e in exc.errors(include_input=False)]
    return []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("zz alpha", "ZZ ALPHA"),
        ("  zz   alpha\tbeta\ngamma  ", "ZZ ALPHA BETA GAMMA"),
        ("zz alpha", "ZZ ALPHA"),
        ("zé", "ZÉ"),
        ("zz", "ZZ"),
        ("Zz", "ZZ"),
        ("zz o'neil", "ZZ O'NEIL"),
        ("zz-yy", "ZZ-YY"),
        ("zz. yy", "ZZ. YY"),
        ("zz 4u", "ZZ 4U"),
        ("жж яя", "ЖЖ ЯЯ"),
        ("ززز", "ززز"),
        ("漢漢", "漢漢"),
        ("ß" * 100, "SS" * 100),
        ("a" * 200, "A" * 200),
        ("a" + " " * 399, "A"),
    ],
)
def test_n2_accepted_forms(raw, expected):
    assert normalise_name(raw) == expected
    assert _model_errors(raw) == []


def test_n2_final_nfc_after_upper_casing():
    # str.upper() of U+0390 leaves a decomposed sequence; the stored form is composed again
    upper = "ΐ".upper()
    assert upper != unicodedata.normalize("NFC", upper)
    assert normalise_name("ΐ") == unicodedata.normalize("NFC", upper) == "Ϊ́"


@pytest.mark.parametrize(
    "raw",
    [
        "a" * 201,
        "ß" * 200,
        "1234",
        "---",
        "   ",
        "zz\u0000yy",
        "zz​yy",
        "zz‮yy",
    ],
)
def test_n2_refused_forms(raw):
    with pytest.raises(InvalidName):
        normalise_name(raw)
    assert (("names", 0, "name"), "invalid_name") in _model_errors(raw)


def test_n2_raw_bound_is_checked_before_normalisation():
    assert _model_errors("a" + " " * 399) == []
    assert _model_errors("a" + " " * 400) == [(("names", 0, "name"), "string_too_long")]
    assert _model_errors("") == [(("names", 0, "name"), "string_too_short")]


# ── N3 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n3_apply_writes_normalised_names_with_the_server_actor(client, conn, writes):
    writes.add(NAMES_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client,
        payload([entry("zz  Alpha beta"), entry("ZZ GAMMA", "ocr_corroborated", 3, 2)]),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "outcome": "recorded",
        "subject_id": sid,
        "recorded": 2,
        "already_recorded": 0,
        "dry_run": False,
    }
    rows = await name_rows(conn, sid)
    assert [(r["name"], r["source"]) for r in rows] == [
        ("ZZ GAMMA", "ocr_corroborated"),
        ("ZZ ALPHA BETA", "ocr_single"),
    ]
    assert {r["created_by"] for r in rows} == {REGISTRY_ACTOR}
    assert json.loads(rows[0]["evidence"]) == {
        "plan_sha256": PLAN,
        "files": 3,
        "blobs": 2,
        "mrz_valid": False,
    }
    assert isinstance(json.loads(rows[1]["evidence"]), dict)


@pytest.mark.asyncio
async def test_n3_dry_run_writes_nothing_and_counts_as_the_apply_does(client, conn, writes):
    writes.add(NAMES_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    names = [entry("zz alpha"), entry("zz beta", "ocr_corroborated")]
    dry = await post(client, payload(names, dry_run=True))
    assert dry.json() == {
        "outcome": "recorded",
        "subject_id": sid,
        "recorded": 2,
        "already_recorded": 0,
        "dry_run": True,
    }
    assert await name_rows(conn) == []
    omitted = await post(client, {k: v for k, v in payload(names).items() if k != "dry_run"})
    assert omitted.json()["dry_run"] is True and omitted.json()["recorded"] == 2
    assert await name_rows(conn) == []
    applied = await post(client, payload(names))
    assert applied.json() == {**dry.json(), "dry_run": False}
    assert len(await name_rows(conn)) == 2


@pytest.mark.asyncio
async def test_n3_mixed_batch_counts_one_and_one_in_both_modes(client, conn, writes):
    writes.add(NAMES_INSERT)
    await seed_subject(conn, "person", None, [("passport", P1)])
    assert (await post(client, payload([entry("zz alpha")]))).json()["recorded"] == 1
    batch = [entry("zz alpha"), entry("zz beta")]
    dry = (await post(client, payload(batch, dry_run=True))).json()
    assert (dry["recorded"], dry["already_recorded"], dry["dry_run"]) == (1, 1, True)
    assert len(await name_rows(conn)) == 1
    applied = (await post(client, payload(batch))).json()
    assert (applied["recorded"], applied["already_recorded"], applied["dry_run"]) == (1, 1, False)
    assert len(await name_rows(conn)) == 2


@pytest.mark.asyncio
async def test_n3_a_second_identical_call_records_zero(client, conn, writes):
    writes.add(NAMES_INSERT)
    await seed_subject(conn, "person", None, [("passport", P1)])
    names = [entry("zz alpha"), entry("zz beta", "ocr_corroborated")]
    assert (await post(client, payload(names))).json()["recorded"] == 2
    again = (await post(client, payload(names))).json()
    assert (again["recorded"], again["already_recorded"]) == (0, 2)
    assert len(await name_rows(conn)) == 2


# ── N4 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n4_same_name_under_two_sources_is_two_rows(client, conn, writes):
    writes.add(NAMES_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(client, payload([entry("zz alpha"), entry("zz alpha", "ocr_corroborated")]))
    assert resp.json()["recorded"] == 2
    assert [(r["name"], r["source"]) for r in await name_rows(conn, sid)] == [
        ("ZZ ALPHA", "ocr_corroborated"),
        ("ZZ ALPHA", "ocr_single"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["ZZ ALPHA", "zz alpha", "  Zz   AlPhA\t"])
async def test_n4_two_entries_with_the_same_source_and_name_are_duplicate_name(
    client, conn, second
):
    await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(client, payload([entry("zz alpha"), entry(second)]))
    assert loc_codes(resp) == [([], "duplicate_name")]
    assert await name_rows(conn) == []


@pytest.mark.asyncio
async def test_n4_a_repeat_with_other_evidence_numbers_leaves_the_stored_evidence(
    client, conn, writes
):
    writes.add(NAMES_INSERT)
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    first = entry("zz alpha", "ocr_corroborated", files=2, blobs=2)
    await post(client, payload([first]))
    stored = (await name_rows(conn, sid))[0]
    again = await post(
        client, payload([entry("zz alpha", "ocr_corroborated", files=9, blobs=7, mrz=True)])
    )
    assert again.json()["recorded"] == 0 and again.json()["already_recorded"] == 1
    after = (await name_rows(conn, sid))[0]
    assert json.loads(after["evidence"]) == json.loads(stored["evidence"])
    assert json.loads(after["evidence"])["files"] == 2
    assert after["id"] == stored["id"]


# ── N5 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n5_one_invalid_entry_among_valid_ones_writes_nothing(client, conn):
    await seed_subject(conn, "person", None, [("passport", P1)])
    resp = await post(
        client, payload([entry("zz alpha"), entry("zz beta"), entry("12345"), entry("zz gamma")])
    )
    assert loc_codes(resp) == [(["names", 2, "name"], "invalid_name")]
    assert await name_rows(conn) == []


class _FailOnInsert:
    """Delegates to a real connection; the n-th INSERT of a recorded name raises."""

    def __init__(self, conn, fail_at):
        self._conn, self._fail_at, self.inserts = conn, fail_at, 0

    def __getattr__(self, attr):
        return getattr(self._conn, attr)

    async def fetchval(self, sql, *args):
        if sql == names_module._INSERT_NAME_SQL:
            self.inserts += 1
            if self.inserts == self._fail_at:
                raise RuntimeError("zz injected failure")
        return await self._conn.fetchval(sql, *args)


def _request(names, *, kind="person", ids=(("passport", P1),)):
    return NamesInput(
        kind=kind,
        identifiers=prepare_identifiers(kind, [(t, v, {}) for t, v in ids]),
        names=tuple(NameEntry(normalise_name(n), s, {"plan_sha256": PLAN}) for n, s in names),
    )


@pytest.mark.asyncio
async def test_n5_a_failure_after_the_first_insert_leaves_no_row(conn, writes):
    writes.add(NAMES_INSERT)
    await seed_subject(conn, "person", None, [("passport", P1)])
    flaky = _FailOnInsert(conn, fail_at=2)
    request = _request([("zz alpha", "ocr_single"), ("zz beta", "ocr_single")])
    with pytest.raises(RuntimeError):
        await record_names(flaky, request, dry_run=False)
    assert flaky.inserts == 2
    assert await name_rows(conn) == []
    ok = await record_names(conn, request, dry_run=False)
    assert ok["recorded"] == 2 and len(await name_rows(conn)) == 2


# ── N6 ────────────────────────────────────────────────────────────────────────


class _Gate:
    """Delegates to a real connection; the first INSERT waits until both callers reached it."""

    def __init__(self, conn, barrier):
        self._conn, self._barrier, self._waited = conn, barrier, False

    def __getattr__(self, attr):
        return getattr(self._conn, attr)

    async def fetchval(self, sql, *args):
        if sql == names_module._INSERT_NAME_SQL and not self._waited:
            self._waited = True
            await asyncio.wait_for(self._barrier.wait(), 10)
        return await self._conn.fetchval(sql, *args)


@pytest.mark.asyncio
async def test_n6_concurrent_identical_requests_leave_one_row_per_triple(pool, writes):
    writes.add(NAMES_INSERT)
    async with pool.acquire() as seed:
        sid = await seed_subject(seed, "person", None, [("passport", P1)])
    request = _request(
        [("zz alpha", "ocr_single"), ("zz alpha", "ocr_corroborated"), ("zz beta", "ocr_single")]
    )
    barrier = asyncio.Barrier(2)
    async with pool.acquire() as c1, pool.acquire() as c2:
        out = await asyncio.gather(
            record_names(_Gate(c1, barrier), request, dry_run=False),
            record_names(_Gate(c2, barrier), request, dry_run=False),
        )
        rows = await name_rows(c1, sid)
    assert sum(o["recorded"] for o in out) == 3
    assert sum(o["already_recorded"] for o in out) == 3
    assert len(rows) == 3 and len({(r["name"], r["source"]) for r in rows}) == 3


# ── N7 ────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def owners(conn):
    ids = {}
    ids["a"] = await seed_subject(conn, "person", None, [("passport", P1), ("nik", NIK1)])
    ids["b"] = await seed_subject(conn, "person", None, [("passport", P2)])
    ids["company"] = await seed_subject(conn, "company", None, [("passport", P3)])
    ids["merged"] = await seed_subject(conn, "person", None, [("passport", P4)], merged=ids["a"])
    return ids


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "ids,kind,reason",
    [
        ((("passport", UNOWNED),), "person", "none_owned"),
        ((("passport", P1), ("passport", P2)), "person", "two_owners"),
        ((("passport", P1),), "person", "set_differs"),
        ((("passport", P1), ("nik", NIK1), ("passport", UNOWNED)), "person", "set_differs"),
        ((("passport", P1), ("passport", UNOWNED)), "person", "partial_overlap"),
        ((("passport", P3),), "person", "kind_differs"),
        ((("passport", P4),), "person", "merged"),
    ],
)
async def test_n7_subject_refusals_each_with_its_reason(
    client, conn, owners, ids, kind, reason, dry_run
):
    resp = await post(client, payload([entry("zz alpha")], ids=ids, kind=kind, dry_run=dry_run))
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "subject_not_registered", "reason": reason}
    assert await name_rows(conn) == []


@pytest.mark.asyncio
async def test_n7_the_owner_with_its_complete_key_set_is_accepted(client, conn, owners, writes):
    writes.add(NAMES_INSERT)
    resp = await post(client, payload([entry("zz alpha")], ids=(("nik", NIK1), ("passport", P1))))
    assert resp.json()["outcome"] == "recorded"
    assert resp.json()["subject_id"] == owners["a"]


# ── N8 ────────────────────────────────────────────────────────────────────────


async def _mrz(client, ids, name, **over):
    return await post(client, payload([entry(name, "mrz_corrected")], ids=ids, **over))


@pytest.mark.asyncio
async def test_n8_a_provable_correction_is_recorded(client, conn, writes):
    writes.add(NAMES_INSERT)
    sid = await seed_subject(conn, "person", "zz sm1th 8", [("passport", P1)])
    resp = await _mrz(client, (("passport", P1),), "zz smith b")
    assert resp.json() == {
        "outcome": "recorded",
        "subject_id": sid,
        "recorded": 1,
        "already_recorded": 0,
        "dry_run": False,
    }
    assert [(r["name"], r["source"]) for r in await name_rows(conn, sid)] == [
        ("ZZ SMITH B", "mrz_corrected")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize(
    "kind,registered,submitted",
    [
        ("company", "ZZ C0RP", "ZZ CORP"),
        ("person", None, "ZZ SMITH"),
        ("person", "ZZ SMITH", "ZZ SMYTH"),
        ("person", "ZZ AB2", "ZZ ABZ"),
        ("person", "ZZ SM1TH", "ZZ SM1TH"),
        ("person", "ZZ SM1TH", "zz  sm1th"),
        ("person", "ZZ SM1TH", "ZZ JONES"),
        ("person", "ZZ 8O8", "ZZ BO8"),
        ("person", "ZZ SM1TH", "ZZ SMITHE"),
    ],
)
async def test_n8_a_correction_that_is_not_provable_is_refused(
    client, conn, kind, registered, submitted, dry_run
):
    ids = (("passport", P1),) if kind == "person" else (("npwp", "012345678901234"),)
    sid = await seed_subject(conn, kind, registered, list(ids))
    resp = await post(
        client, payload([entry(submitted, "mrz_corrected")], ids=ids, kind=kind, dry_run=dry_run)
    )
    assert resp.status_code == 200
    assert resp.json() == {"outcome": "correction_refused", "subject_id": sid}
    assert await name_rows(conn) == []


@pytest.mark.asyncio
async def test_n8_one_unprovable_correction_refuses_the_whole_request(client, conn):
    await seed_subject(conn, "person", "ZZ SM1TH", [("passport", P1)])
    resp = await post(
        client,
        payload([entry("zz ocr only"), entry("ZZ JONES", "mrz_corrected")]),
    )
    assert resp.json()["outcome"] == "correction_refused"
    assert await name_rows(conn) == []


# ── N9 ────────────────────────────────────────────────────────────────────────


def _mut(fn):
    def apply(data):
        data = copy.deepcopy(data)
        fn(data)
        return data

    return apply


def _set_evidence(**kw):
    return _mut(lambda d: d["names"][0]["evidence"].update(kw))


def _set_entry(**kw):
    return _mut(lambda d: d["names"][0].update(kw))


BASE_BODY = payload([entry("zz alpha")])

N9_CASES = {
    "invalid_name": (
        _set_entry(name="12345"),
        [(["names", 0, "name"], "invalid_name")],
    ),
    "blobs_above_files": (
        _set_evidence(files=1, blobs=2),
        [(["names", 0, "evidence"], "blobs_above_files")],
    ),
    "ocr_corroborated_needs_two_blobs_or_mrz": (
        _set_entry(source="ocr_corroborated"),
        [(["names", 0], "evidence_source_mismatch")],
    ),
    "ocr_single_refuses_two_blobs": (
        _set_evidence(files=2, blobs=2),
        [(["names", 0], "evidence_source_mismatch")],
    ),
    "ocr_single_refuses_mrz_valid": (
        _set_evidence(mrz_valid=True),
        [(["names", 0], "evidence_source_mismatch")],
    ),
    "name_is_a_string": (_set_entry(name=5), [(["names", 0, "name"], "string_type")]),
    "dry_run_is_a_boolean": (
        _mut(lambda d: d.update(dry_run="true")),
        [(["dry_run"], "bool_type")],
    ),
    "mrz_valid_is_a_boolean": (
        _set_evidence(mrz_valid="yes"),
        [(["names", 0, "evidence", "mrz_valid"], "bool_type")],
    ),
    "files_is_an_integer": (
        _set_evidence(files=1.5),
        [(["names", 0, "evidence", "files"], "int_type")],
    ),
    "blobs_is_not_a_boolean": (
        _set_evidence(blobs=True),
        [(["names", 0, "evidence", "blobs"], "int_type")],
    ),
    "files_zero": (
        _set_evidence(files=0),
        [(["names", 0, "evidence", "files"], "greater_than_equal")],
    ),
    "files_above_limit": (
        _set_evidence(files=10001),
        [(["names", 0, "evidence", "files"], "less_than_equal")],
    ),
    "plan_hash_shape": (
        _set_evidence(plan_sha256="A" * 64),
        [(["names", 0, "evidence", "plan_sha256"], "string_pattern_mismatch")],
    ),
    "no_names": (_mut(lambda d: d.update(names=[])), [(["names"], "too_short")]),
    "six_names": (
        _mut(lambda d: d.update(names=[entry(f"zz name {i}") for i in range(6)])),
        [(["names"], "too_long")],
    ),
    "manual_is_refused": (
        _set_entry(source="manual"),
        [(["names", 0, "source"], "literal_error")],
    ),
    "unknown_source": (
        _set_entry(source="guessed"),
        [(["names", 0, "source"], "literal_error")],
    ),
    "unknown_field": (_mut(lambda d: d.update(extra=1)), [(["_"], "extra_forbidden")]),
    "created_by_is_not_a_field": (
        _mut(lambda d: d.update(created_by="zz")),
        [(["_"], "extra_forbidden")],
    ),
    "created_by_in_an_entry": (
        _set_entry(created_by="zz"),
        [(["names", 0, "_"], "extra_forbidden")],
    ),
    "hostile_field_name": (
        _set_evidence(**{"ZzHostile<script>": 1}),
        [(["names", 0, "evidence", "_"], "extra_forbidden")],
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
    "names_is_a_list": (_mut(lambda d: d.update(names="zz")), [(["names"], "list_type")]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(N9_CASES))
async def test_n9_exact_422_bodies(client, conn, case):
    await seed_subject(conn, "person", None, [("passport", P1)])
    mutate, expected = N9_CASES[case]
    resp = await post(client, mutate(BASE_BODY))
    assert resp.status_code == 422
    assert resp.json() == {"detail": [{"loc": loc, "code": code} for loc, code in expected]}
    assert await name_rows(conn) == []


@pytest.mark.asyncio
async def test_n9_accepted_evidence_boundaries(client, conn, writes):
    writes.add(NAMES_INSERT)
    await seed_subject(conn, "person", None, [("passport", P1)])
    names = [
        entry("zz one", "ocr_corroborated", files=1, blobs=1, mrz=True),
        entry("zz two", "ocr_corroborated", files=10000, blobs=10000),
        entry("zz three", "ocr_single", files=10000, blobs=1),
    ]
    resp = await post(client, payload(names))
    assert resp.status_code == 200 and resp.json()["recorded"] == 3


@pytest.mark.asyncio
async def test_n9_malformed_json_and_oversize_body(client, conn):
    await seed_subject(conn, "person", None, [("passport", P1)])
    for raw in (b"{not json", b"", b"[1, 2]", b"null"):
        resp = await post(client, raw)
        assert resp.status_code == 422, raw
        body = resp.json()["detail"]
        assert (
            len(body) == 1
            and body[0]["loc"] == []
            and body[0]["code"]
            in {
                "json_invalid",
                "model_type",
            }
        )
    pad = "x" * (route_module.MAX_BODY_BYTES + 1)
    big = await post(client, pad.encode())
    assert big.status_code == 422
    assert big.json() == {"detail": [{"loc": ["body"], "code": "too_long"}]}
    exact = await post(client, b" " * route_module.MAX_BODY_BYTES)
    assert exact.json()["detail"][0]["loc"] == []  # at the limit the body is parsed, not refused


def _sanitised(errors):
    resp = route_module._invalid_names(errors)
    assert resp.status_code == 422
    return json.loads(resp.body)["detail"]


def test_n9_sanitiser_keeps_only_closed_names_and_indexes():
    out = _sanitised(
        [
            {"loc": ("names", 9, "name"), "type": "invalid_name"},
            {"loc": ("names", 4, "name"), "type": "invalid_name"},
            {"loc": ("subject", "identifiers", 19, "type"), "type": "literal_error"},
            {"loc": ("subject", "identifiers", 20, "type"), "type": "literal_error"},
            {"loc": ("names", True, "name"), "type": "invalid_name"},
            {"loc": ("names", -1), "type": "invalid_name"},
            {"loc": ("body", 0), "type": "too_long"},
            {"loc": ("names", 0, "ZzHostile"), "type": "extra_forbidden"},
            {"loc": ("evidence", 1), "type": "weird_code"},
            {"loc": (), "type": "duplicate_name"},
            {"loc": ("names", 0), "type": "value_error"},
        ]
    )
    assert out == [
        {"loc": ["names", "_", "name"], "code": "invalid_name"},
        {"loc": ["names", 4, "name"], "code": "invalid_name"},
        {"loc": ["subject", "identifiers", 19, "type"], "code": "literal_error"},
        {"loc": ["subject", "identifiers", "_", "type"], "code": "literal_error"},
        {"loc": ["names", "_", "name"], "code": "invalid_name"},
        {"loc": ["names", "_"], "code": "invalid_name"},
        {"loc": ["body", "_"], "code": "too_long"},
        {"loc": ["names", 0, "_"], "code": "extra_forbidden"},
        {"loc": ["evidence", "_"], "code": "invalid"},
        {"loc": [], "code": "duplicate_name"},
        {"loc": ["names", 0], "code": "invalid"},
    ]


def test_n9_sanitiser_caps_the_list_at_fifty_errors():
    out = _sanitised([{"loc": ("names", 0, "name"), "type": "invalid_name"}] * 60)
    assert len(out) == 50


# ── N10 ───────────────────────────────────────────────────────────────────────


class _SpyPool:
    """The pool the app holds for one test: counts the acquisitions, delegates to the real one."""

    def __init__(self, pool):
        self._pool, self.acquired = pool, 0

    def acquire(self):
        self.acquired += 1
        return self._pool.acquire()


BAD_BODIES = [b"{not json", json.dumps(payload([entry("12345")])).encode()]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_n10_the_flag_is_checked_before_the_body(pool, auth_on, monkeypatch, raw):
    monkeypatch.setattr(settings, "related_subject_registry_enabled", False, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    as_identity(monkeypatch, None)
    spy = _SpyPool(pool)
    async with make_client(spy) as client:
        resp = await post(client, raw)
        no_key = await post(client, raw, headers={"Content-Type": "application/json"})
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}
    assert no_key.status_code == 503 and no_key.json() == {"detail": "registry_unavailable"}
    assert spy.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
@pytest.mark.parametrize("key", [None, "wrong-key"])
async def test_n10_the_key_is_checked_before_the_body(
    pool, enabled, auth_on, monkeypatch, raw, key
):
    as_identity(monkeypatch, None)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-CRM-Write-Key"] = key
    spy = _SpyPool(pool)
    async with make_client(spy) as client:
        resp = await post(client, raw, headers=headers)
    assert resp.status_code == 401 and resp.json() == {"detail": "unauthorized"}
    assert spy.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_n10_no_pool_is_the_existing_503_before_the_body(enabled, auth_on, monkeypatch, raw):
    as_identity(monkeypatch, None)
    async with make_client(None) as client:
        resp = await post(client, raw)
    assert resp.status_code == 503 and resp.json() == {"detail": "registry_unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", BAD_BODIES)
async def test_n10_with_every_gate_open_an_invalid_body_is_422_and_runs_no_statement(
    pool, enabled, auth_on, audit_logger, monkeypatch, raw
):
    as_identity(monkeypatch, None)
    spy = _SpyPool(pool)
    async with make_client(spy) as client:
        resp = await post(client, raw)
    assert resp.status_code == 422
    assert spy.acquired == 0


# ── N13 ───────────────────────────────────────────────────────────────────────

SECRET_NAME = "ZzSecretAlphaName"
SECRET_ID = "ZZ4242424"
SECRET_KEY_ID = "ZZ4242425"
PROTOCOL_WORDS = frozenset(
    {
        "person", "company", "passport", "nik", "ocr_single", "ocr_corroborated", "mrz_corrected",
        "true", "false", "null", "kind", "subject", "identifiers", "type", "value", "names",
        "name", "source", "evidence", "plan_sha256", "files", "blobs", "mrz_valid", "dry_run",
    }
)  # fmt: skip


def _leaves(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            _leaves(k, out)
            _leaves(v, out)
    elif isinstance(node, list):
        for v in node:
            _leaves(v, out)
    elif isinstance(node, str):
        out.add(node)


def _forbidden(data) -> set[str]:
    leaves: set[str] = set()
    _leaves(data, leaves)
    tokens = set()
    for leaf in leaves:
        if len(leaf) >= 4 and leaf not in PROTOCOL_WORDS and leaf != PLAN:
            tokens.add(leaf.casefold())
            tokens.add(" ".join(leaf.upper().split()).casefold())
            tokens.add(canonical_form("passport", leaf).casefold())
    tokens.discard("")
    return tokens


def _record_text(record: logging.LogRecord) -> str:
    rendered = logging.Formatter().format(record)
    return rendered + " " + " ".join(str(v) for v in record.__dict__.values())


@pytest.mark.asyncio
async def test_n13_no_name_identifier_or_body_in_any_log_response_or_audit_row(
    client, conn, caplog, monkeypatch, writes
):
    writes.add(NAMES_INSERT)
    caplog.set_level(logging.DEBUG)
    await seed_subject(conn, "person", "ZZ SM1TH", [("passport", SECRET_ID)])
    await seed_subject(conn, "person", None, [("passport", SECRET_KEY_ID)])
    await seed_subject(conn, "company", None, [("passport", "ZZ4242426")], merged=None)
    ids = (("passport", SECRET_ID),)
    sent: list[dict] = []
    responses: list[httpx.Response] = []

    async def call(data, **kw):
        sent.append(data if isinstance(data, dict) else {"raw": data.decode()})
        resp = await post(client, data, **kw)
        responses.append(resp)
        return resp

    names = [entry(SECRET_NAME), entry(SECRET_NAME + " Two", "ocr_corroborated")]
    assert (await call(payload(names, ids=ids, dry_run=True))).status_code == 200
    assert (await call(payload(names, ids=ids))).status_code == 200
    assert (await call(payload(names, ids=ids))).status_code == 200
    # refusals
    for refused in (
        payload(names, ids=(("passport", "ZZ4242427"),)),
        payload(names, ids=(("passport", SECRET_ID), ("passport", SECRET_KEY_ID))),
        payload(names, ids=(("passport", "ZZ4242426"),)),
        payload([entry(SECRET_NAME + " Zebra", "mrz_corrected")], ids=ids),
    ):
        assert (await call(refused)).status_code == 200
    # validation errors
    for invalid in (
        payload([entry("\u0000" + SECRET_NAME)], ids=ids),
        payload([entry(SECRET_NAME), entry(SECRET_NAME.lower())], ids=ids),
        payload(names, ids=ids, **{SECRET_NAME: 1}),
        b'{"names": "' + SECRET_NAME.encode() + b'"',
        b"x" * (route_module.MAX_BODY_BYTES + 1) + SECRET_NAME.encode(),
    ):
        assert (await call(invalid)).status_code == 422

    # injected parser failure and injected database failure
    def parser_boom(cls, *a, **k):
        raise RuntimeError(f"boom {SECRET_NAME} {SECRET_ID}")

    monkeypatch.setattr(
        route_module.RelatedNamesRegisterRequest, "model_validate_json", classmethod(parser_boom)
    )
    boom = await call(payload(names, ids=ids))
    assert boom.status_code == 500 and boom.json() == {"detail": "internal_error"}
    monkeypatch.undo()
    monkeypatch.setattr(settings, "related_subject_registry_enabled", True, raising=False)
    monkeypatch.setattr(settings, "wa_mirror_crm_write_key", KEY, raising=False)
    as_identity(monkeypatch, None)

    async def db_boom(*a, **k):
        raise asyncpg.PostgresError(f"boom {SECRET_NAME} {SECRET_ID}")

    monkeypatch.setattr(route_module, "record_names", db_boom)
    down = await call(payload(names, ids=ids))
    assert down.status_code == 503 and down.json() == {"detail": "database_error"}

    tokens = _forbidden(sent)
    assert any(SECRET_NAME.casefold() in t for t in tokens) and SECRET_ID.casefold() in tokens
    for resp in responses:
        text = resp.text.casefold()
        assert [t for t in tokens if t in text] == [], resp.status_code
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert any(r.name == names_module.__name__ for r in records), "capture saw no service record"
    assert any(r.name == route_module.__name__ for r in records), "capture saw no route record"
    log_text = " ".join(_record_text(r) for r in records).casefold()
    assert [t for t in tokens if t in log_text] == []
    rows = await conn.fetch(
        "SELECT endpoint, request_body, query_params, response_body, error_message "
        "FROM api_audit_trail ORDER BY id"
    )
    assert len(rows) == len(sent)
    assert all(r["request_body"] is None for r in rows)
    audit_text = " ".join(str(dict(r)) for r in rows).casefold()
    assert [t for t in tokens if t in audit_text] == []


# ── N14 ───────────────────────────────────────────────────────────────────────


def test_n14_the_route_is_registered_where_the_two_existing_ones_are():
    entries = [e for e in ROUTER_MANIFEST if e.name == "crm_related_subjects"]
    assert len(entries) == 1
    paths = {(r.path, tuple(sorted(r.methods))) for r in route_module.router.routes}
    assert paths == {
        ("/api/crm/related-subjects/register", ("POST",)),
        ("/api/crm/related-subjects/documents/register", ("POST",)),
        ("/api/crm/related-subjects/names/register", ("POST",)),
    }
    assert route_module.router.route_class is route_module._SealedRoute


def test_n14_public_entry_is_exact_path_and_post_only():
    entries = [e for e in PUBLIC_ENDPOINTS if e.prefix == URL]
    assert len(entries) == 1
    assert entries[0].match == "exact" and tuple(entries[0].methods) == ("POST",)
    assert any(e.matches(URL, "POST") for e in PUBLIC_ENDPOINTS)
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        assert not any(e.matches(URL, method) for e in PUBLIC_ENDPOINTS), method
    for path in (
        URL + "/",
        URL + "/x",
        "/api/crm/related-subjects/names",
        "/api/crm/related-subjects",
    ):
        assert not any(e.matches(path, "POST") for e in PUBLIC_ENDPOINTS), path
    for path in (READ, f"{READ}/7"):
        assert not any(e.matches(path, "GET") for e in PUBLIC_ENDPOINTS), path


@pytest.mark.asyncio
async def test_n14_get_on_the_path_and_the_read_routes_stay_protected(client, conn):
    sid = await seed_subject(conn, "person", "ZzN14 Person", [("passport", P1)])
    for url in (URL, READ, f"{READ}/{sid}"):
        resp = await client.get(url)
        assert resp.status_code == 401, url
        assert "ZzN14" not in resp.text


@pytest.mark.asyncio
async def test_n14_the_two_existing_write_routes_answer_as_before(client, conn):
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
    # a slice-4A field name is still "_" on the older routes
    third = await post(client, {"names": 1, "kind": "person"}, url=URL_SUBJECT)
    assert {"loc": ["_"], "code": "extra_forbidden"} in third.json()["detail"]


# ── N11: the effective name ───────────────────────────────────────────────────

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAY = timedelta(days=1)


@pytest_asyncio.fixture
async def admin(pool, auth_on, audit_logger, monkeypatch):
    as_identity(monkeypatch, ADMIN)
    async with make_client(pool) as c:
        yield c


async def _effective(conn, sid):
    """The effective name through the detail query and through the list query, which must agree."""
    detail = await related_registry_read.get_subject(conn, sid)
    listed = await related_registry_read.list_subjects(conn, q=None, kind=None, limit=100, offset=0)
    row = next(r for r in listed["items"] if r["id"] == sid)
    assert (row["display_name"], row["name_source"]) == (
        detail["display_name"],
        detail["name_source"],
    )
    return detail["display_name"], detail["name_source"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "higher,lower",
    [
        ("manual", "mrz_corrected"),
        ("mrz_corrected", "registration"),
        ("registration", "ocr_corroborated"),
        ("ocr_corroborated", "ocr_single"),
    ],
)
async def test_n11_each_rank_beats_the_next_even_when_the_next_is_newer(conn, higher, lower):
    registration = (
        "ZZ HIGH" if higher == "registration" else "ZZ LOW" if lower == "registration" else None
    )
    sid = await seed_subject(conn, "person", registration, [("passport", P1)])
    if higher != "registration":
        await seed_name(conn, sid, "ZZ HIGH", higher, at=T0)
    if lower != "registration":
        await seed_name(conn, sid, "ZZ LOW", lower, at=T0 + DAY)
    assert await _effective(conn, sid) == ("ZZ HIGH", higher)


@pytest.mark.asyncio
async def test_n11_the_newest_row_wins_inside_a_rank(conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_name(conn, sid, "ZZ NEWER", "ocr_single", at=T0 + DAY)
    await seed_name(conn, sid, "ZZ OLDER", "ocr_single", at=T0)
    assert await _effective(conn, sid) == ("ZZ NEWER", "ocr_single")


@pytest.mark.asyncio
async def test_n11_the_highest_id_wins_on_equal_time(conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_name(conn, sid, "ZZ FIRST", "ocr_single", at=T0)
    await seed_name(conn, sid, "ZZ SECOND", "ocr_single", at=T0)
    assert await _effective(conn, sid) == ("ZZ SECOND", "ocr_single")


@pytest.mark.asyncio
async def test_n11_a_null_registration_name_takes_its_best_ocr_row(conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_name(conn, sid, "ZZ SINGLE", "ocr_single", at=T0 + DAY)
    await seed_name(conn, sid, "ZZ CORROBORATED", "ocr_corroborated", at=T0)
    assert await _effective(conn, sid) == ("ZZ CORROBORATED", "ocr_corroborated")


@pytest.mark.asyncio
async def test_n11_no_candidate_is_a_null_name_and_a_null_source(conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    assert await _effective(conn, sid) == (None, None)


@pytest.mark.asyncio
async def test_n11_a_company_takes_its_ocr_rows(conn):
    sid = await seed_subject(conn, "company", None, [("npwp", "012345678901234")])
    await seed_name(conn, sid, "ZZ CORP ONE", "ocr_single", at=T0)
    await seed_name(conn, sid, "ZZ CORP TWO", "ocr_corroborated", at=T0)
    assert await _effective(conn, sid) == ("ZZ CORP TWO", "ocr_corroborated")


# ── N12: the read routes ──────────────────────────────────────────────────────


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


def _ids(resp):
    return [r["id"] for r in resp.json()["items"]]


@pytest.mark.asyncio
async def test_n12_list_and_detail_carry_the_effective_name_and_its_source(admin, conn):
    sid = await seed_subject(conn, "person", "ZzReg Name", [("passport", P1)])
    await seed_name(conn, sid, "ZZ CORRECTED", "mrz_corrected", at=T0)
    listed = (await admin.get(READ)).json()["items"][0]
    assert listed["display_name"] == "ZZ CORRECTED" and listed["name_source"] == "mrz_corrected"
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert detail["display_name"] == "ZZ CORRECTED" and detail["name_source"] == "mrz_corrected"
    other = await seed_subject(conn, "person", None, [("passport", P2)])
    unnamed = (await admin.get(f"{READ}/{other}")).json()
    assert unnamed["display_name"] is None and unnamed["name_source"] is None
    assert unnamed["names"] == [] and unnamed["names_total"] == 0


@pytest.mark.asyncio
async def test_n12_every_names_item_holds_exactly_three_fields(admin, conn):
    sid = await seed_subject(conn, "person", "ZzReg Name", [("passport", P1)])
    await seed_name(conn, sid, "ZZ ROW", "ocr_corroborated", at=T0)
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert [n["source"] for n in detail["names"]] == ["registration", "ocr_corroborated"]
    for item in detail["names"]:
        assert set(item) == {"name", "source", "created_at"}
        assert item["source"] is not None
        assert datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")).tzinfo
    assert detail["names"][0]["name"] == "ZzReg Name"
    assert detail["names"][0]["created_at"] == detail["created_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("registration", ["ZZ REGISTERED", None])
async def test_n12_detail_shows_one_plus_nineteen_or_twenty_and_counts_every_candidate(
    admin, conn, registration
):
    sid = await seed_subject(conn, "person", registration, [("passport", P1)])
    for i in range(25):
        await seed_name(conn, sid, f"ZZ ROW {i:02d}", "ocr_single", at=T0 + i * DAY)
    detail = (await admin.get(f"{READ}/{sid}")).json()
    shown = [n["name"] for n in detail["names"]]
    if registration:
        assert shown[0] == registration and len(shown) == 20
        assert shown[1:] == [f"ZZ ROW {i:02d}" for i in range(24, 5, -1)]
        assert detail["names_total"] == 26
    else:
        assert len(shown) == 20 and shown[0] == "ZZ ROW 24"
        assert shown == [f"ZZ ROW {i:02d}" for i in range(24, 4, -1)]
        assert detail["names_total"] == 25


@pytest.mark.asyncio
async def test_n12_equal_registration_and_recorded_strings_are_counted_twice(admin, conn):
    sid = await seed_subject(conn, "person", "ZZ SAME", [("passport", P1)])
    await seed_name(conn, sid, "ZZ SAME", "ocr_single", at=T0)
    detail = (await admin.get(f"{READ}/{sid}")).json()
    assert [(n["name"], n["source"]) for n in detail["names"]] == [
        ("ZZ SAME", "registration"),
        ("ZZ SAME", "ocr_single"),
    ]
    assert detail["names_total"] == 2 and detail["name_source"] == "registration"


@pytest.mark.asyncio
async def test_n12_search_finds_a_subject_by_an_outranked_name_and_by_its_registration_name(
    admin, conn
):
    one = await seed_subject(conn, "person", "ZzRegAlpha Person", [("passport", P1)])
    await seed_name(conn, one, "ZZ OTHERNAME", "ocr_single", at=T0)
    two = await seed_subject(conn, "person", "ZzRegBeta Person", [("passport", P2)])
    await seed_name(conn, two, "ZZ FIXEDBETA", "mrz_corrected", at=T0)
    multi = await seed_subject(conn, "person", None, [("passport", P3)])
    for source in ("ocr_single", "ocr_corroborated", "mrz_corrected"):
        await seed_name(conn, multi, "ZZ MULTIWORD " + source[:4], source, at=T0)

    by_outranked = await admin.get(READ, params={"q": "othername"})
    assert _ids(by_outranked) == [one] and by_outranked.json()["total"] == 1
    assert by_outranked.json()["items"][0]["display_name"] == "ZzRegAlpha Person"
    by_registration = await admin.get(READ, params={"q": "regbeta"})
    assert _ids(by_registration) == [two]
    assert by_registration.json()["items"][0]["name_source"] == "mrz_corrected"
    by_recorded = await admin.get(READ, params={"q": "fixedbeta"})
    assert _ids(by_recorded) == [two]
    again = await admin.get(READ, params={"q": "multiword"})
    assert _ids(again) == [multi] and again.json()["total"] == 1


@pytest.mark.asyncio
async def test_n12_a_literal_percent_and_underscore_in_q_match_themselves_only(admin, conn):
    pct = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_name(conn, pct, "ZZ 50%_OFF", "ocr_single", at=T0)
    plain = await seed_subject(conn, "person", None, [("passport", P2)])
    await seed_name(conn, plain, "ZZ 50XYOFF", "ocr_single", at=T0)
    assert _ids(await admin.get(READ, params={"q": "%_"})) == [pct]
    assert (await admin.get(READ, params={"q": "%%"})).json()["total"] == 0
    assert _ids(await admin.get(READ, params={"q": "zz 50"})) == [pct, plain]
    assert (await admin.get(READ, params={"q": "5__"})).json()["total"] == 0


@pytest.mark.asyncio
async def test_n12_the_identifier_prefix_search_answers_as_before(admin, conn):
    sid = await seed_subject(conn, "person", None, [("passport", P1)])
    await seed_name(conn, sid, "ZZ HAS NAME", "ocr_single", at=T0)
    other = await seed_subject(conn, "person", "ZzOther", [("passport", P2)])
    for typed in (P1, "zz-0000001", "zz 000000 1"):
        assert _ids(await admin.get(READ, params={"q": typed})) == [sid], typed
    assert sorted(_ids(await admin.get(READ, params={"q": "ZZ000000"}))) == sorted([sid, other])


@pytest.mark.asyncio
async def test_n12_order_and_paging_follow_the_effective_name_nulls_last(admin, conn):
    c = await seed_subject(conn, "person", "ZZ C REGISTERED", [("passport", P1)])
    a = await seed_subject(conn, "person", None, [("passport", P2)])
    await seed_name(conn, a, "ZZ A RECORDED", "ocr_single", at=T0)
    b = await seed_subject(conn, "person", "ZZ B OUTRANKED", [("passport", P3)])
    await seed_name(conn, b, "ZZ AA NEVER SHOWN", "ocr_single", at=T0)
    none1 = await seed_subject(conn, "person", None, [("passport", P4)])
    none2 = await seed_subject(conn, "company", None, [("npwp", "012345678901234")])
    full = await admin.get(READ, params={"limit": 100})
    assert _ids(full) == [a, b, c, none1, none2]
    assert [r["display_name"] for r in full.json()["items"]] == [
        "ZZ A RECORDED",
        "ZZ B OUTRANKED",
        "ZZ C REGISTERED",
        None,
        None,
    ]
    pages = [
        _ids(await admin.get(READ, params={"limit": 2, "offset": off})) for off in (0, 2, 4, 6)
    ]
    assert pages == [[a, b], [c, none1], [none2], []]
    assert (await admin.get(READ, params={"limit": 2})).json()["total"] == 5


@pytest.mark.asyncio
async def test_n12_a_populated_page_runs_at_most_four_selects(conn):
    for i, raw in enumerate((P1, P2, P3)):
        sid = await seed_subject(conn, "person", f"ZZ REG {i}", [("passport", raw)])
        await seed_name(conn, sid, f"ZZ ROW {i}", "ocr_single", at=T0)
    counting = _CountingConn(conn)
    result = await related_registry_read.list_subjects(
        counting, q=None, kind=None, limit=50, offset=0
    )
    assert len(result["items"]) == 3
    assert counting.statements == 4
    searching = _CountingConn(conn)
    await related_registry_read.list_subjects(searching, q="zz row", kind=None, limit=50, offset=0)
    assert searching.statements == 4
    empty = _CountingConn(conn)
    await related_registry_read.list_subjects(empty, q="nomatch", kind=None, limit=50, offset=0)
    assert empty.statements == 2


@pytest.mark.asyncio
async def test_n13_the_read_routes_log_no_name(admin, conn, caplog):
    caplog.set_level(logging.DEBUG)
    sid = await seed_subject(conn, "person", "ZzReadSecretReg", [("passport", SECRET_ID)])
    await seed_name(conn, sid, "ZzReadSecretRow", "ocr_single", at=T0)
    assert (await admin.get(READ, params={"q": "zzreadsecretrow"})).status_code == 200
    assert (await admin.get(f"{READ}/{sid}")).status_code == 200
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert any(r.name == read_module.__name__ for r in records)
    text = " ".join(_record_text(r) for r in records).casefold()
    assert "zzreadsecret" not in text and SECRET_ID.casefold() not in text
    audit = await conn.fetch("SELECT query_params, request_body FROM api_audit_trail")
    assert "zzreadsecret" not in " ".join(str(dict(r)) for r in audit).casefold()
