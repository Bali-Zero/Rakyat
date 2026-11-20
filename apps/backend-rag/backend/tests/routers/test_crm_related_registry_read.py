"""Related-subjects registry, slice 3: the read-only admin routes on a real PostgreSQL.

Each test builds a throwaway schema in the TEST database (migrations 361, 362 and 365 forward parts
plus the audit table), seeds invented rows (``Zz`` / ``ZZ`` conventions) and calls the real router
behind the real middleware stack. Nothing here is a real person, company or document.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import backend.app.routers.crm_related_registry_read as route_module
import backend.services.crm.related_registry_read as service_module
from backend.app.auth.public_endpoints import PUBLIC_ENDPOINTS
from backend.app.core.config import settings
from backend.app.setup.router_manifest import ROUTER_MANIFEST
from backend.db.migration_base import split_migration_sql
from backend.middleware.activity_logging import ActivityLoggingMiddleware
from backend.middleware.error_monitoring import ErrorMonitoringMiddleware
from backend.middleware.hybrid_auth import ROLE_BOUNDARY_DETAIL, HybridAuthMiddleware
from backend.middleware.request_tracing import RequestTracingMiddleware
from backend.services.crm.related_identifiers import canonical_form
from backend.services.monitoring.activity_logger import activity_logger
from backend.tests.fixtures.prod_shaped_pool import init_prod_shaped_connection

MIGRATIONS = Path(route_module.__file__).resolve().parents[2] / "db" / "migrations_v2"
BASE = "/api/crm/related-registry"
REGISTRY_TABLES = (
    "related_subjects",
    "related_subject_identifiers",
    "related_documents",
    "related_document_sightings",
    "related_subject_documents",
    "related_subject_links",
    "related_subject_merges",
    "related_subject_detachments",
)

ADMIN = {"email": "zz-admin@example.test", "user_id": 1, "role": "admin"}
STAFF = {"email": "zz-staff@example.test", "user_id": 2, "role": "team_member"}
CLIENT = {"email": "zz-client@example.test", "user_id": 3, "role": "client"}
PARTNER = {"email": "zz-partner@example.test", "user_id": 4, "role": "partner"}

CLIENTS_SCHEMA = """
CREATE TABLE clients (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name text NOT NULL,
    passport_number varchar(100),
    date_of_birth date,
    deleted_at timestamptz
);
"""

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
# accepts nothing but audit-trail INSERTs, so a read route that wrote a registry row fails
# whichever test called it. Seeding happens inside `unmeasured`.
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
    schema = "related_registry_read_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    created = await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=6,
        server_settings={"search_path": schema},
        init=init_prod_shaped_connection,
        statement_cache_size=0,
        max_inactive_connection_lifetime=30.0,
    )
    try:
        async with created.acquire() as c:
            for migration in (
                "361_related_subjects_registry.sql",
                "362_related_documents.sql",
                "365_related_subject_names.sql",
                "366_related_evidence_objects.sql",
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
        assert {(r["tbl"], r["op"]) for r in rows} <= {("api_audit_trail", "INSERT")}, (
            "a read route wrote: " + str(sorted((r["tbl"], r["op"]) for r in rows))
        )
    finally:
        await created.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest_asyncio.fixture
async def conn(pool):
    async with pool.acquire() as c:
        yield c


@pytest.fixture
def audit_logger(pool):
    previous = (activity_logger.pool, activity_logger._initialized)
    activity_logger.pool, activity_logger._initialized = pool, True
    yield activity_logger
    activity_logger.pool, activity_logger._initialized = previous


async def _seed_subject(c, kind, name, ids, *, dob=None, nationality=None, sex=None, merged=None):
    sid = await c.fetchval(
        "INSERT INTO related_subjects (kind, display_name, date_of_birth, nationality, sex, "
        "merged_into, evidence, created_by) VALUES ($1, $2, $3, $4, $5, $6, '{}'::jsonb, 'zz-seed') "
        "RETURNING id",
        kind, name, dob, nationality, sex, merged,
    )  # fmt: skip
    for id_type, raw in ids:
        await c.execute(
            "INSERT INTO related_subject_identifiers (id_type, id_norm, id_raw, norm_version, "
            "subject_id) VALUES ($1, $2, $3, 1, $4)",
            id_type, canonical_form(id_type, raw), raw, sid,
        )  # fmt: skip
    return sid


async def _seed_document(c, sid, sha, doc_type, refs=(), *, mime="application/pdf", size=1234):
    await c.execute(
        "INSERT INTO related_documents (sha256, byte_size, mime_type, first_seen_on, "
        "first_channel, evidence, created_by) VALUES ($1, $2, $3, DATE '2026-01-05', 'drive', "
        "'{}'::jsonb, 'zz-seed') ON CONFLICT DO NOTHING",
        sha, size, mime,
    )  # fmt: skip
    for ref in refs:
        await c.execute(
            "INSERT INTO related_document_sightings (kind, ref, sha256, seen_on, created_by) "
            "VALUES ('drive', $1, $2, DATE '2026-01-06', 'zz-seed')",
            ref, sha,
        )  # fmt: skip
    await c.execute(
        "INSERT INTO related_subject_documents (subject_id, sha256, doc_type, evidence, "
        "created_by) VALUES ($1, $2, $3, '{}'::jsonb, 'zz-seed')",
        sid, sha, doc_type,
    )  # fmt: skip


SHA1, SHA2, SHA3 = "1" * 64, "2" * 64, "3" * 64
REF1, REF2, REF3 = "ZzDriveFileAaaaaaaa01", "ZzDriveFileAaaaaaaa02", "ZzDriveFileBbbbbbbb03"
ZZ_PASSPORT_RAW = "zz-12345a1"
ZZ_PERMIT_RAW = "ZZ77ZZ0000000001Z"
ZZ_NIK_RAW = "3201 0000 0000 0001"
ZZ_AHU_RAW = "AHU-0001234.AH.01.01.TAHUN 2099"
ZZ_NPWP_15 = "012345678901234"


@pytest_asyncio.fixture
async def seeded(pool):
    """Nine visible subjects and one merged one; ids by role. Rows seeded outside the measurement."""
    ids: dict[str, int] = {}
    async with pool.acquire() as c:
        mark = await c.fetchval("SELECT coalesce(max(id), 0) FROM write_log")
        ids["aaa"] = await _seed_subject(
            c, "person", "ZzAaa Person",
            [("passport", ZZ_PASSPORT_RAW), ("permit", ZZ_PERMIT_RAW)],
            dob=date(1990, 1, 2), nationality="ZZ-land", sex="F",
        )  # fmt: skip
        ids["bbb1"] = await _seed_subject(
            c, "person", "ZzBbb Person", [("nik", ZZ_NIK_RAW)], nationality="ZZ-land"
        )
        ids["bbb2"] = await _seed_subject(c, "person", "ZzBbb Person", [("passport", "ZZ98765B2")])
        ids["ccc"] = await _seed_subject(
            c, "company", "ZzCcc Company Ltd", [("npwp", ZZ_NPWP_15), ("nib", "9990000000001")]
        )
        ids["control"] = await _seed_subject(
            c, "person", "ZzControlName", [("passport", "ZZ11111C1")]
        )
        ids["dbl"] = await _seed_subject(c, "person", "ZzDbl%%Name", [("passport", "ZZ22222D2")])
        ids["pct"] = await _seed_subject(c, "person", "ZzPct%_Name", [("passport", "ZZ33333P3")])
        ids["unnamed_company"] = await _seed_subject(c, "company", None, [("ahu", ZZ_AHU_RAW)])
        ids["unnamed_person"] = await _seed_subject(c, "person", None, [("passport", "ZZ55555U5")])
        ids["merged"] = await _seed_subject(
            c, "person", "ZzMerged Person", [("passport", "ZZ77777M7")], merged=ids["aaa"]
        )
        await _seed_document(c, ids["aaa"], SHA2, "passport")
        await _seed_document(c, ids["aaa"], SHA1, "passport", [REF2, REF1])
        await _seed_document(c, ids["aaa"], SHA3, "visa", [REF3], mime="image/jpeg", size=99)
        await c.execute("DELETE FROM write_log WHERE id > $1", mark)
    return ids


ORDER = [
    "aaa", "bbb1", "bbb2", "ccc", "control", "dbl", "pct", "unnamed_company", "unnamed_person",
]  # fmt: skip


# ── a real-stack app: the real middleware, FastAPI's own exception handlers ──


class _Alerts:
    async def send_http_error_alert(self, **kwargs):
        return None

    async def send_latency_alert(self, **kwargs):
        return None


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "api_auth_enabled", True, raising=False)


def as_identity(monkeypatch, identity):
    """The middleware decides from `authenticate_request`; replacing it is what a test identity needs."""

    async def fake(self, request):
        return dict(identity) if identity else None

    monkeypatch.setattr(HybridAuthMiddleware, "authenticate_request", fake)


def make_client(pool) -> httpx.AsyncClient:
    app = FastAPI()
    app.state.db_pool = pool
    app.include_router(route_module.router)
    app.add_middleware(HybridAuthMiddleware)
    app.add_middleware(RequestTracingMiddleware)
    app.add_middleware(ErrorMonitoringMiddleware, alert_service=_Alerts())
    app.add_middleware(ActivityLoggingMiddleware)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
        base_url="http://test",
    )


@pytest_asyncio.fixture
async def admin(pool, auth_on, audit_logger, monkeypatch):
    as_identity(monkeypatch, ADMIN)
    async with make_client(pool) as client:
        yield client


def names(resp) -> list[str | None]:
    return [row["display_name"] for row in resp.json()["items"]]


def ids_of(resp) -> list[int]:
    return [row["id"] for row in resp.json()["items"]]


# ── T1 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t1_anonymous_gets_401_on_both_routes(pool, seeded, auth_on, audit_logger):
    async with make_client(pool) as client:
        for url in (BASE, f"{BASE}/{seeded['aaa']}"):
            resp = await client.get(url)
            assert resp.status_code == 401
            assert "ZzAaa" not in resp.text and "items" not in resp.text


# ── T2 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t2_non_admin_staff_gets_403_and_portal_roles_get_the_boundary_403(
    pool, seeded, auth_on, audit_logger, monkeypatch
):
    for identity, detail in (
        (STAFF, "CRM admin required"),
        (CLIENT, ROLE_BOUNDARY_DETAIL),
        (PARTNER, ROLE_BOUNDARY_DETAIL),
    ):
        as_identity(monkeypatch, identity)
        async with make_client(pool) as client:
            for url in (BASE, f"{BASE}/{seeded['aaa']}"):
                resp = await client.get(url)
                assert resp.status_code == 403, (identity["role"], url, resp.text)
                assert resp.json() == {"detail": detail}
                assert "ZzAaa" not in resp.text and "items" not in resp.text


# ── T3 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t3_list_without_q_orders_pages_and_counts(admin, seeded):
    resp = await admin.get(BASE, params={"limit": 100})
    assert resp.status_code == 200
    body = resp.json()
    assert ids_of(resp) == [seeded[k] for k in ORDER]
    assert body["total"] == 9 and body["limit"] == 100 and body["offset"] == 0
    assert names(resp)[-2:] == [None, None]
    row = body["items"][0]
    assert row["kind"] == "person" and row["nationality"] == "ZZ-land"
    assert row["date_of_birth"] == "1990-01-02" and row["document_count"] == 3
    assert row["identifiers"] == [
        {"type": "passport", "value": ZZ_PASSPORT_RAW, "from_subject_id": None},
        {"type": "permit", "value": ZZ_PERMIT_RAW, "from_subject_id": None},
        {"type": "passport", "value": "ZZ77777M7", "from_subject_id": seeded["merged"]},
    ]
    assert set(row) == {
        "id", "kind", "display_name", "name_source", "date_of_birth", "nationality", "identifiers",
        "document_count", "links_count",
    }  # fmt: skip
    assert row["links_count"] == 0
    assert row["name_source"] == "registration"

    page = await admin.get(BASE, params={"limit": 3, "offset": 2})
    assert ids_of(page) == [seeded[k] for k in ORDER[2:5]]
    assert page.json()["total"] == 9 and page.json()["limit"] == 3 and page.json()["offset"] == 2
    beyond = await admin.get(BASE, params={"offset": 50})
    assert beyond.json()["items"] == [] and beyond.json()["total"] == 9

    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert (await admin.get(BASE, params=params)).status_code == 422


# ── T4 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t4_name_search_is_case_insensitive_substring_with_literal_wildcards(admin, seeded):
    resp = await admin.get(BASE, params={"q": "%_"})
    assert ids_of(resp) == [seeded["pct"]] and resp.json()["total"] == 1
    resp = await admin.get(BASE, params={"q": "%%"})
    assert ids_of(resp) == [seeded["dbl"]] and resp.json()["total"] == 1
    assert seeded["control"] not in ids_of(resp)

    resp = await admin.get(BASE, params={"q": "bBb PERS"})
    assert ids_of(resp) == [seeded["bbb1"], seeded["bbb2"]]
    resp = await admin.get(BASE, params={"q": "  controlname  "})
    assert ids_of(resp) == [seeded["control"]]
    # blank text means no search
    resp = await admin.get(BASE, params={"q": "   "})
    assert resp.status_code == 200 and resp.json()["total"] == 9


# ── T5 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t5_identifier_search_uses_canonical_prefixes(admin, seeded):
    for typed in ("zz 12-345", "ZZ-12345A", "zz12345a1"):
        resp = await admin.get(BASE, params={"q": typed})
        assert ids_of(resp) == [seeded["aaa"]], typed
    resp = await admin.get(BASE, params={"q": "3201"})
    assert ids_of(resp) == [seeded["bbb1"]]
    resp = await admin.get(BASE, params={"q": ZZ_NPWP_15})
    assert ids_of(resp) == [seeded["ccc"]]
    resp = await admin.get(BASE, params={"q": ZZ_AHU_RAW})
    assert ids_of(resp) == [seeded["unnamed_company"]]
    assert names(resp) == [None]
    # fewer than three canonical characters never searches identifiers: only the named subjects
    short = await admin.get(BASE, params={"q": "ZZ", "limit": 100})
    assert seeded["unnamed_person"] not in ids_of(short) and short.json()["total"] == 7
    assert (await admin.get(BASE, params={"q": "q" * 1})).status_code == 422
    assert (await admin.get(BASE, params={"q": "q" * 65})).status_code == 422
    assert (await admin.get(BASE, params={"q": "q" * 64})).status_code == 200


# ── T6 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t6_kind_filter_alone_and_with_q(admin, seeded):
    resp = await admin.get(BASE, params={"kind": "company", "limit": 100})
    assert ids_of(resp) == [seeded["ccc"], seeded["unnamed_company"]]
    assert resp.json()["total"] == 2
    resp = await admin.get(BASE, params={"kind": "person", "limit": 100})
    assert resp.json()["total"] == 7 and seeded["ccc"] not in ids_of(resp)
    resp = await admin.get(BASE, params={"kind": "company", "q": "Zz"})
    assert ids_of(resp) == [seeded["ccc"]]
    resp = await admin.get(BASE, params={"kind": "person", "q": "Zz", "limit": 100})
    assert seeded["ccc"] not in ids_of(resp) and resp.json()["total"] == 6
    assert (await admin.get(BASE, params={"kind": "robot"})).status_code == 422


# ── T7 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t7_detail_identifiers_documents_and_drive_ids(admin, seeded):
    resp = await admin.get(f"{BASE}/{seeded['aaa']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "ZzAaa Person" and body["sex"] == "F"
    assert body["nationality"] == "ZZ-land" and body["date_of_birth"] == "1990-01-02"
    assert "created_at" in body and "evidence" not in body and "created_by" not in body
    assert [i["type"] for i in body["identifiers"]] == ["passport", "permit", "passport"]
    assert [d["sha256"] for d in body["documents"]] == [SHA1, SHA2, SHA3]
    first, second, third = body["documents"]
    assert first["drive_file_ids"] == [REF1, REF2]
    assert second["drive_file_ids"] == []
    assert third["drive_file_ids"] == [REF3]
    assert (third["doc_type"], third["mime_type"], third["byte_size"]) == ("visa", "image/jpeg", 99)
    assert first["first_seen_on"] == "2026-01-05"
    assert set(first) == {
        "sha256", "doc_type", "mime_type", "byte_size", "first_seen_on", "drive_file_ids",
        "from_subject_id",
    }  # fmt: skip
    assert body["merged_from"] == [
        {"subject_id": seeded["merged"], "basis": None, "merged_at": None}
    ]
    assert body["merged_into"] is None

    missing = await admin.get(f"{BASE}/999999")
    assert missing.status_code == 404 and missing.json() == {"detail": "Subject not found"}
    assert (await admin.get(f"{BASE}/0")).status_code == 422
    merged = await admin.get(f"{BASE}/{seeded['merged']}")
    assert merged.status_code == 200 and merged.json()["merged_into"] == seeded["aaa"]


# ── T8 ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_t8_merged_subject_is_absent_from_list_and_total(admin, seeded):
    resp = await admin.get(BASE, params={"limit": 100})
    assert seeded["merged"] not in ids_of(resp) and resp.json()["total"] == 9
    # the merged-away record is never listed itself, but its survivor is now found through the
    # record's name and identifier (slice 4C); its pointer has no history row (LEFT JOIN)
    by_name = await admin.get(BASE, params={"q": "ZzMerged"})
    assert ids_of(by_name) == [seeded["aaa"]] and by_name.json()["total"] == 1
    by_id = await admin.get(BASE, params={"q": "ZZ77777"})
    assert ids_of(by_id) == [seeded["aaa"]] and by_id.json()["total"] == 1
    kind = await admin.get(BASE, params={"kind": "person", "limit": 100})
    assert seeded["merged"] not in ids_of(kind) and kind.json()["total"] == 7
    # opened by id, a merged subject is returned like any other
    assert (await admin.get(f"{BASE}/{seeded['merged']}")).status_code == 200


# ── T9 ────────────────────────────────────────────────────────────────────────


def _record_text(record: logging.LogRecord) -> str:
    rendered = logging.Formatter().format(record)
    return rendered + " " + " ".join(str(v) for v in record.__dict__.values())


@pytest.mark.asyncio
async def test_t9_no_protected_value_in_logs_and_q_is_masked_in_the_audit_row(
    admin, seeded, conn, caplog
):
    caplog.set_level(logging.DEBUG)
    search = await admin.get(BASE, params={"q": ZZ_PASSPORT_RAW, "limit": 20})
    detail = await admin.get(f"{BASE}/{seeded['aaa']}")
    assert search.status_code == 200 and ids_of(search) == [seeded["aaa"]]
    assert detail.status_code == 200 and len(detail.json()["documents"]) == 3

    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    mine = [r for r in records if r.name == route_module.__name__]
    assert len(mine) >= 2, "the capture saw no record of the new router"
    text = " ".join(_record_text(r) for r in records).casefold()
    protected = [
        "ZzAaa", "ZzBbb", "ZzControl", "ZzCcc", ZZ_PASSPORT_RAW, "ZZ12345A1", ZZ_PERMIT_RAW,
        "3201000000000001", ZZ_AHU_RAW, "1234.01.01.2099", ZZ_NPWP_15, "0012345678901234",
        SHA1, SHA2, SHA3, REF1, REF2, REF3, "ZZ-land",
    ]  # fmt: skip
    leaked = [p for p in protected if p.casefold() in text]
    assert leaked == [], f"protected values reached a log record: {len(leaked)}"

    rows = await conn.fetch("SELECT endpoint, query_params FROM api_audit_trail ORDER BY id")
    assert len(rows) == 2
    params = json.loads(rows[0]["query_params"])
    assert params["q"] == "***" and params["limit"] == "20"
    audit = " ".join(str(dict(r)) for r in rows).casefold()
    assert ZZ_PASSPORT_RAW.casefold() not in audit and "zzaaa" not in audit
    assert rows[1]["endpoint"] == f"{BASE}/{seeded['aaa']}"


# ── T10 ───────────────────────────────────────────────────────────────────────

# SQL constants that are clauses spliced into the statements, not statements of their own
FRAGMENTS = {"_MATCH_SQL", "_EFFECTIVE_SQL"}
FORBIDDEN_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|MERGE|ALTER|DROP|CREATE)\b", re.I)


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


def test_t10_every_sql_text_of_the_service_is_a_plain_select():
    constants = _sql_constants(service_module)
    assert len(constants) >= 10
    for sql in constants:
        assert not FORBIDDEN_SQL.search(sql), sql[:60]
        assert not re.search(r"\bWITH\b", sql, re.I), sql[:60]
    statements = [
        value
        for attr, value in vars(service_module).items()
        if attr.endswith("_SQL") and attr not in FRAGMENTS
    ]
    # slice 4A added the recorded-names statement of the detail page, slice 4B the links
    # statement, slice 4C the members statement of the detail page, the file-count slice the
    # detachments statement: 11 complete statements
    assert len(statements) == 11
    for sql in statements:
        assert sql.lstrip().upper().startswith("SELECT"), sql[:40]
    # the executed statements are exactly the module's constants (no inline SQL in a call)
    source = Path(service_module.__file__).read_text()
    assert not re.search(r"\.(execute|executemany|fetch|fetchrow|fetchval)\(\s*f?[\"']", source)


@pytest.mark.asyncio
async def test_t10_registry_row_counts_are_unchanged_by_every_read(admin, seeded, conn):
    async def counts() -> dict[str, int]:
        return {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in REGISTRY_TABLES}

    before = await counts()
    assert before["related_subjects"] == 10 and before["related_subject_documents"] == 3
    calls = [
        (BASE, {"limit": 100}),
        (BASE, {"limit": 3, "offset": 2}),
        (BASE, {"q": "%_"}),
        (BASE, {"q": "zz 12-345"}),
        (BASE, {"kind": "company"}),
        (f"{BASE}/{seeded['aaa']}", {}),
        (f"{BASE}/999999", {}),
    ]
    for url, params in calls:
        await admin.get(url, params=params)
    assert await counts() == before


# ── T11 ───────────────────────────────────────────────────────────────────────


def test_t11_router_is_in_the_manifest_and_its_paths_are_not_public():
    entries = [e for e in ROUTER_MANIFEST if e.name == "crm_related_registry_read"]
    assert len(entries) == 1
    assert "crm-related-registry" in entries[0].tags
    assert route_module.router.prefix == "/api/crm/related-registry"
    assert route_module.router.prefix != "/api/crm/related-subjects"
    for path in (BASE, f"{BASE}/7"):
        assert not any(e.matches(path, "GET") for e in PUBLIC_ENDPOINTS), path
    assert {route.methods.__iter__().__next__() for route in route_module.router.routes} == {"GET"}


# ── T12 ───────────────────────────────────────────────────────────────────────

BOOM_VALUE = "ZZ99999X9"


class _BoomPool:
    """The pool the app holds for this test only: acquiring raises a database error that
    carries a protected value in its message. The `pool` fixture's own pool is untouched."""

    def acquire(self):
        raise asyncpg.PostgresError(f"zz boom {BOOM_VALUE}")


@pytest.mark.asyncio
async def test_t12_database_error_is_a_fixed_503_and_nothing_leaks(
    pool, seeded, auth_on, audit_logger, monkeypatch, caplog
):
    as_identity(monkeypatch, ADMIN)
    caplog.set_level(logging.DEBUG)
    async with make_client(_BoomPool()) as client:
        for url in (BASE, f"{BASE}/{seeded['aaa']}"):
            resp = await client.get(url, params={"q": BOOM_VALUE} if url == BASE else None)
            assert resp.status_code == 503, url
            assert resp.json() == {"detail": "registry_unavailable"}
            assert BOOM_VALUE not in resp.text
    records = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore"))]
    assert any(r.name == route_module.__name__ for r in records), "capture saw no router record"
    text = " ".join(_record_text(r) for r in records).casefold()
    assert BOOM_VALUE.casefold() not in text and "zz boom" not in text


# ── the production jsonb codec ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guard_the_module_pool_carries_the_production_jsonb_codec(conn):
    # a native container round-trips as itself; a pre-serialised string comes back as a STRING,
    # which is the double-encoding signature of the 2026-08-27 incident class
    assert await conn.fetchval("SELECT $1::jsonb", {"a": 1}) == {"a": 1}
    assert await conn.fetchval("SELECT $1::jsonb", '{"a": 1}') == '{"a": 1}'
    assert await conn.fetchval("SELECT jsonb_typeof($1::jsonb)", '{"a": 1}') == "string"


# ── R5: file count per client link, detachments on the subject ───────────────

OWNER_DIGEST = "ab12" * 16
MOVED_HASH_1, MOVED_HASH_2 = "e5" * 32, "f6" * 32
PLAN_DIGEST = "cd34" * 16
DETACH_CREATED_BY = "zz-detach-seed-actor"
DETACHED_AT = datetime(2026, 4, 5, 9, 0, tzinfo=UTC)


@asynccontextmanager
async def _unmeasured(pool):
    """Seed outside the write-log measurement, as the `seeded` fixture does."""
    async with pool.acquire() as c:
        mark = await c.fetchval("SELECT coalesce(max(id), 0) FROM write_log")
        yield c
        await c.execute("DELETE FROM write_log WHERE id > $1", mark)


async def _seed_link(c, sid, client_id, evidence, *, basis="drive_folder", day=1):
    await c.execute(
        "INSERT INTO related_subject_links (subject_id, client_id, basis, evidence, created_by, "
        "created_at) VALUES ($1, $2, $3, $4::jsonb, 'zz-seed', $5)",
        sid, client_id, basis, evidence, datetime(2026, 3, day, 10, 0, tzinfo=UTC),
    )  # fmt: skip


async def _seed_detachment(
    c, from_id, to_id, *, moved=(MOVED_HASH_1, MOVED_HASH_2), at=DETACHED_AT
):
    await c.execute(
        "INSERT INTO related_subject_detachments (from_id, to_id, id_type, owner_keys_sha256, "
        "moved_documents, basis, evidence, created_by, created_at) "
        "VALUES ($1, $2, 'nik', $3, $4, 'nik_birth_date', $5::jsonb, $6, $7)",
        from_id, to_id, OWNER_DIGEST, list(moved), {"plan_sha256": PLAN_DIGEST, "n": len(moved)},
        DETACH_CREATED_BY, at,
    )  # fmt: skip


def _link_files(resp) -> dict[int, int | None]:
    return {link["client_id"]: link["files"] for link in resp.json()["links"]}


@pytest.mark.asyncio
async def test_r5_t1_t2_link_files_is_the_evidence_number_or_none(pool, admin, seeded):
    cases = {
        501: {"files": 3, "plan_sha256": PLAN_DIGEST},
        502: {"files": 1},
        503: {},
        504: {"files": "3"},
        505: {"files": 1.5},
        506: {"files": -2},
        507: {"files": None},
        508: {"files": 99999999999},
    }
    async with _unmeasured(pool) as c:
        for client_id, evidence in cases.items():
            await _seed_link(c, seeded["aaa"], client_id, evidence)
    resp = await admin.get(f"{BASE}/{seeded['aaa']}")
    assert resp.status_code == 200
    assert _link_files(resp) == {
        501: 3, 502: 1, 503: None, 504: None, 505: None, 506: None, 507: None, 508: None,
    }  # fmt: skip


@pytest.mark.asyncio
async def test_r5_t3_survivor_shows_the_files_of_the_earliest_row_of_the_pair(pool, admin, seeded):
    async with _unmeasured(pool) as c:
        await _seed_link(c, seeded["merged"], 511, {"files": 5}, day=1)
        await _seed_link(c, seeded["aaa"], 511, {"files": 2}, day=2)
    body = (await admin.get(f"{BASE}/{seeded['aaa']}")).json()
    assert body["links_total"] == 1
    assert body["links"][0]["files"] == 5
    assert body["links"][0]["from_subject_id"] == seeded["merged"]


@pytest.mark.asyncio
async def test_r5_t4_detached_from_and_detached_into_carry_counts_dates_and_ids(
    pool, admin, seeded
):
    async with _unmeasured(pool) as c:
        src = await _seed_subject(c, "person", "ZzSplit Source", [])
        tgt = await _seed_subject(c, "person", "ZzSplit Target", [("nik", "3201 0000 0000 0002")])
        await _seed_detachment(c, src, tgt)
    expected = {
        "id_type": "nik",
        "basis": "nik_birth_date",
        "documents_moved": 2,
        "from_subject_id": None,
    }
    on_target = (await admin.get(f"{BASE}/{tgt}")).json()
    assert on_target["detached_into"] == []
    (row,) = on_target["detached_from"]
    assert row.pop("detached_at").startswith("2026-04-05T09:00:00")
    assert row == {"subject_id": src, **expected}
    on_source = (await admin.get(f"{BASE}/{src}")).json()
    assert on_source["detached_from"] == []
    (row,) = on_source["detached_into"]
    assert row.pop("detached_at").startswith("2026-04-05T09:00:00")
    assert row == {"subject_id": tgt, **expected}


@pytest.mark.asyncio
async def test_r5_t5_a_members_detachment_shows_on_the_survivor_with_the_member_id(
    pool, admin, seeded
):
    async with _unmeasured(pool) as c:
        out = await _seed_subject(c, "person", "ZzSplit Out", [])
        other = await _seed_subject(c, "person", "ZzSplit Other", [])
        await _seed_detachment(c, seeded["merged"], out, moved=(MOVED_HASH_1,))
        await _seed_detachment(c, other, seeded["aaa"], at=DETACHED_AT.replace(day=6))
    body = (await admin.get(f"{BASE}/{seeded['aaa']}")).json()
    assert [
        (r["subject_id"], r["from_subject_id"], r["documents_moved"]) for r in body["detached_into"]
    ] == [(out, seeded["merged"], 1)]
    assert [(r["subject_id"], r["from_subject_id"]) for r in body["detached_from"]] == [
        (other, None)
    ]


@pytest.mark.asyncio
async def test_r5_t6_a_subject_in_no_detachment_gets_two_empty_lists(admin, seeded):
    body = (await admin.get(f"{BASE}/{seeded['bbb1']}")).json()
    assert body["detached_into"] == [] and body["detached_from"] == []


@pytest.mark.asyncio
async def test_r5_t7_no_digest_hash_evidence_or_actor_in_the_response(pool, conn, admin, seeded):
    async with _unmeasured(pool) as c:
        src = await _seed_subject(c, "person", "ZzSplit Source", [])
        tgt = await _seed_subject(c, "person", "ZzSplit Target", [])
        await _seed_detachment(c, src, tgt)
        await _seed_link(c, src, 521, {"files": 4, "plan_sha256": PLAN_DIGEST})
    for sid in (src, tgt):
        text = (await admin.get(f"{BASE}/{sid}")).text
        for secret in (OWNER_DIGEST, MOVED_HASH_1, MOVED_HASH_2, PLAN_DIGEST, DETACH_CREATED_BY):
            assert secret not in text, secret
        assert "plan_sha256" not in text and "owner_keys" not in text and "created_by" not in text
    # the model would drop an extra key, so the service answer itself is read too
    for sid in (src, tgt):
        raw = repr(await service_module.get_subject(conn, sid))
        for secret in (OWNER_DIGEST, MOVED_HASH_1, MOVED_HASH_2, PLAN_DIGEST, DETACH_CREATED_BY):
            assert secret not in raw, secret


@pytest.mark.asyncio
async def test_r5_t8_the_response_validates_against_the_models_with_exact_keys(pool, admin, seeded):
    async with _unmeasured(pool) as c:
        src = await _seed_subject(c, "person", "ZzSplit Source", [])
        tgt = await _seed_subject(c, "person", "ZzSplit Target", [])
        await _seed_detachment(c, src, tgt)
        await _seed_link(c, src, 531, {"files": 2})
    body = (await admin.get(f"{BASE}/{src}")).json()
    parsed = route_module.RelatedRegistrySubjectDetail.model_validate(body)
    assert parsed.links[0].files == 2
    assert parsed.detached_into[0].documents_moved == 2
    assert set(body["detached_into"][0]) == set(route_module.RelatedRegistryDetachment.model_fields)
    assert "files" in body["links"][0]


@pytest.mark.asyncio
async def test_r5_t9_the_detachment_rows_are_bounded(pool, admin, seeded, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_DETAIL_LINKS", 2)
    async with _unmeasured(pool) as c:
        src = await _seed_subject(c, "person", "ZzSplit Source", [])
        for day in (1, 2, 3):
            tgt = await _seed_subject(c, "person", f"ZzSplit Target {day}", [])
            await _seed_detachment(c, src, tgt, at=DETACHED_AT.replace(day=day))
    body = (await admin.get(f"{BASE}/{src}")).json()
    assert len(body["detached_into"]) == 2
    assert [r["detached_at"][:10] for r in body["detached_into"]] == ["2026-04-01", "2026-04-02"]
