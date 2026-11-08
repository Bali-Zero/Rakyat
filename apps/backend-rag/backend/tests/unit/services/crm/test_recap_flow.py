"""RECAP_SPEC.md test obligations 4-6: query shape, end to end from DB-shaped rows, PII sentinels."""

import json
import logging
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.services.crm import client_recap as r
from backend.services.crm import client_recap_text as rt

SENTINEL = "QQSENTINEL4417"
USER = {"id": "u", "role": "admin", "email": "a@example.com"}
NOW = datetime(2026, 11, 7, 3, 0, tzinfo=timezone.utc)


def squash(sql: str) -> str:
    return " ".join(sql.split())


class FakeConn:
    """A connection that answers by the SQL it is asked, and remembers every statement."""

    def __init__(self, **tables):
        self.t = tables
        self.calls: list[tuple[str, str, tuple]] = []
        self.cache_row = None
        self.upserts: list[tuple] = []

    def _log(self, kind, sql, args):
        self.calls.append((kind, squash(sql), args))

    def sql_of(self, needle):
        return [c for c in self.calls if needle in c[1]]

    async def fetchrow(self, sql, *args):
        self._log("fetchrow", sql, args)
        if "client_ai_recaps" in sql:
            return self.cache_row
        if "FROM clients" in sql:
            return self.t.get("client")
        if "FROM companies" in sql:
            return self.t.get("company")
        raise AssertionError(squash(sql))

    async def fetchval(self, sql, *args):
        self._log("fetchval", sql, args)
        if "INSERT INTO client_ai_recaps" in sql:
            self.upserts.append(args)
            return NOW
        if "client_company_links" in sql:
            return self.t.get("resolved", 55)
        raise AssertionError(squash(sql))

    async def fetch(self, sql, *args):
        self._log("fetch", sql, args)
        if "catalogue_size" in sql:
            verdicts = self.t.get("verdicts", {})
            return [{"catalogue_size": 1559, "canonical_rows": verdicts.get(args[0], 1)}]
        if "FROM documents" in sql:
            return self.t.get("documents", [])
        if "client_family_members" in sql:
            return self.t.get("family", [])
        if "FROM practices" in sql:
            return self.t.get("practices", [])
        if "visa_types" in sql:
            if isinstance(self.t.get("visa_types"), Exception):
                raise self.t["visa_types"]
            return self.t.get("visa_types", [])
        if "FROM lkpm_reports" in sql:
            if isinstance(self.t.get("lkpm"), Exception):
                raise self.t["lkpm"]
            return self.t.get("lkpm", [])
        if "FROM client_company_links" in sql:
            return self.t.get("links", [])
        raise AssertionError(squash(sql))


class Pool:
    def __init__(self, conn):
        self.conn = conn
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False

        self.acquire = lambda: _Ctx()


async def call_endpoint(conn, scope, company_id=None):
    from backend.app.routers import crm_enhanced as ce

    with (
        patch.object(ce, "verify_client_access", AsyncMock()),
        patch.object(r, "configured_provider", return_value=None),
        patch.object(rt, "_today", return_value=date(2026, 11, 7)),
    ):
        return await ce.get_client_recap(
            41, scope=scope, company_id=company_id, pool=Pool(conn), current_user=USER
        )


# ── query shape ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_practice_query_keeps_every_status_and_every_category():
    conn = FakeConn(client={"passport_expiry": None})
    await r.fetch_immigration_inputs(conn, 41)
    (sql,) = [c[1] for c in conn.sql_of("FROM practices")]
    assert "LEFT JOIN practice_types" in sql and "JOIN practice_types" in sql
    assert sql.endswith("WHERE p.client_id = $1")
    for filtered in (
        "p.status",
        "status NOT IN",
        "status IN",
        "pt.category =",
        "category IN",
        "pt.name IS",
    ):
        assert filtered not in sql.split("WHERE")[1]
    assert "pt.category AS category" in sql and "p.status" in sql.split("FROM")[0]


@pytest.mark.asyncio
async def test_a_legacy_practice_is_resolved_by_its_code_with_no_runtime_probe():
    conn = FakeConn(client={"passport_expiry": None})
    await r.fetch_immigration_inputs(conn, 41)
    (sql,) = [c[1] for c in conn.sql_of("FROM practices")]
    assert (
        "ON pt.id = p.practice_type_id OR (p.practice_type_id IS NULL AND pt.code = p.practice_type_code) WHERE p.client_id = $1"
        in sql
    )
    assert conn.sql_of("information_schema") == []


@pytest.mark.asyncio
async def test_every_query_ends_exactly_where_the_spec_says():
    conn = FakeConn(client={"passport_expiry": None}, company={"company_type": "PT"})
    await r.fetch_immigration_inputs(conn, 41)
    await r.fetch_company_inputs(conn, 55)
    ends = {
        "FROM clients": "FROM clients WHERE id = $1",
        "client_family_members": "FROM client_family_members WHERE client_id = $1",
        "FROM visa_types": "FROM visa_types",
        "FROM companies": "FROM companies WHERE id = $1",
        "shares_count FROM client_company_links": "FROM client_company_links WHERE company_id = $1",
        "FROM lkpm_reports": "WHERE company_id = $1))",
    }
    for needle, tail in ends.items():
        (call,) = conn.sql_of(needle)
        assert call[1].endswith(tail), (needle, call[1][-60:])


@pytest.mark.asyncio
async def test_the_document_query_returns_every_row_so_rule_l_decides_in_the_builder():
    voided = [
        {"deleted_at": NOW, "is_archived": False, "status": "active"},
        {"deleted_at": None, "is_archived": True, "status": "active"},
        {"deleted_at": None, "is_archived": False, "status": "deleted"},
    ]
    conn = FakeConn(client={"passport_expiry": None}, documents=voided)
    out = await r.fetch_immigration_inputs(conn, 41)
    (sql,) = [c[1] for c in conn.sql_of("FROM documents")]
    assert sql.split("WHERE")[1].strip() == "d.client_id = $1"
    for column in (
        "d.deleted_at",
        "d.is_archived",
        "d.status",
        "d.family_member_id",
        "d.ocr_extracted_data",
    ):
        assert column in sql.split("FROM")[0]
    assert len(out["documents"]) == 3


@pytest.mark.asyncio
async def test_the_lkpm_query_returns_the_client_level_rows_of_every_linked_client():
    conn = FakeConn(company={"company_type": "PT"}, lkpm=[{"client_id": 2, "company_id": None}])
    out = await r.fetch_company_inputs(conn, 55)
    (call,) = conn.sql_of("FROM lkpm_reports")
    sql, args = call[1], call[2]
    assert args == (55,)  # no client id: whichever linked client opens the recap, the same rows
    assert "company_id = $1" in sql
    assert (
        "company_id IS NULL AND client_id IN (SELECT client_id FROM client_company_links WHERE company_id = $1)"
        in sql
    )
    assert out["lkpm_rows"] == [{"client_id": 2, "company_id": None}]


@pytest.mark.asyncio
async def test_the_catalogue_is_asked_once_per_distinct_code_and_never_past_the_bound():
    company = {
        "kbli_code": "62010",
        "custom_fields": json.dumps({"kbli_codes": ["62010", "70209"]}),
    }
    conn = FakeConn(company=company)
    out = await r.fetch_company_inputs(conn, 55)
    asked = [c[2][0] for c in conn.sql_of("catalogue_size")]
    assert asked == ["62010", "70209"] and out["kbli_verdicts"] == {
        "62010": "CANONICAL",
        "70209": "CANONICAL",
    }

    many = json.dumps({"kbli_codes": [f"{10000 + i:05d}" for i in range(51)]})
    for bad in (many, json.dumps({"kbli_codes": ["6201"]}), "{broken"):
        conn = FakeConn(company={"kbli_code": "62010", "custom_fields": bad})
        out = await r.fetch_company_inputs(conn, 55)
        assert conn.sql_of("catalogue_size") == [] and out["kbli_verdicts"] == {}


@pytest.mark.asyncio
async def test_a_missing_company_row_asks_nothing_more_and_says_nothing():
    conn = FakeConn(company=None)
    out = await r.fetch_company_inputs(conn, 55)
    assert out["company"] == {} and out["kbli_verdicts"] == {}
    assert r.recap_payload("company", r.build_company_facts(**out)) == {}


# ── end to end ──────────────────────────────────────────────────────────────

VISA_TYPES = [
    {"code": "E23", "name": "E23 - Working KITAS (Standard)"},
    {"code": "E31", "name": "E31 - Dependent KITAS"},
]


def _doc(dtype, expiry, ocr=None, **kw):
    return {
        "document_type": dtype, "status": "active", "expiry_date": expiry, "deleted_at": None,
        "is_archived": False, "family_member_id": None, "ocr_extracted_data": ocr, **kw,
    }  # fmt: skip


@pytest.mark.asyncio
async def test_end_to_end_immigration_from_db_shaped_rows_to_the_stored_text():
    conn = FakeConn(
        client={"passport_expiry": date(2027, 5, 1)},
        documents=[
            _doc(
                "visa",
                date(2027, 3, 14),
                json.dumps(
                    {"raw_response": {"visa_type": "E23 KITAS", "issue_date": "2026-03-15"}}
                ),
            ),
            _doc("kitas", date(2026, 3, 1), json.dumps({"raw_response": {"visa_type": "E23"}})),
            _doc("itk", date(2027, 1, 15)),
            _doc("kitas", date(2031, 1, 1), None, family_member_id=3),
            _doc("kitas", date(2030, 1, 1), None, status="rejected"),
        ],
        family=[
            {"current_visa_type": "E31", "visa_expiry": date(2027, 1, 1)},
            {"current_visa_type": None, "visa_expiry": None},
        ],
        practices=[
            {
                "id": 1,
                "status": "on_process",
                "category": "kitas",
                "practice_type_name": "KITAS Extension",
            },
            {
                "id": 2,
                "status": "waiting_documents",
                "category": "visa",
                "practice_type_name": "Visa Extension",
            },
            {"id": 3, "status": "completed", "category": "kitas", "practice_type_name": "Old"},
            {
                "id": 4,
                "status": "on_process",
                "category": "tax",
                "practice_type_name": "Annual Tax",
            },
        ],
        visa_types=VISA_TYPES,
    )
    out = await call_endpoint(conn, "immigration")
    assert out["source"] == "template" and out["model"] == "template"
    assert out["text"] == (
        "The client's current permit is Working KITAS (E23), issued on 15 March 2026 and valid until 14 March 2027. "
        "Two immigration practices are open: KITAS Extension and Visa Extension and the ITK stamp runs until 15 January 2027. "
        "The passport expires on 1 May 2027 and two family members are on file, "
        "one of them with a permit that ends before the main permit."
    )
    assert out["based_on"] == ["current permit", "ITK", "passport", "family", "practices"]
    assert conn.upserts and conn.upserts[0][4] == out["text"]
    inputs = await r.fetch_immigration_inputs(conn, 41)
    payload = r.recap_payload(
        "immigration", r.build_immigration_facts(**inputs, today=date(2026, 11, 7))
    )
    assert rt.validate_recap(out["text"], payload, "immigration", date(2026, 11, 7)) == (True, [])


@pytest.mark.asyncio
async def test_end_to_end_immigration_family_clause():
    conn = FakeConn(
        client={"passport_expiry": None},
        documents=[_doc("kitas", date(2027, 3, 14))],
        family=[
            {"current_visa_type": "E31", "visa_expiry": date(2027, 1, 1)},
            {"current_visa_type": "", "visa_expiry": None},
        ],
        visa_types=VISA_TYPES,
    )
    out = await call_endpoint(conn, "immigration")
    assert out["text"] == (
        "The client's current permit is KITAS, valid until 14 March 2027. "
        "Two family members are on file, one of them with a permit that ends before the main permit."
    )


@pytest.mark.asyncio
async def test_end_to_end_company_from_db_shaped_rows_to_the_stored_text():
    company = {
        "company_type": " PT PMA ",
        "kbli_code": "62010",
        "akta_pendirian_date": date(2024, 2, 1),
        "akta_perubahan_date": date(2025, 6, 1),
        "custom_fields": json.dumps(
            {
                "authorized_capital": "10.000.000.000",
                "paid_up_capital": 1_049_000_000,
                "kbli_codes": '["70209"]',
            }
        ),
    }
    conn = FakeConn(
        company=company,
        links=[
            {"client_id": 41, "role": "Shareholder", "shares_count": 10},
            {"client_id": 42, "role": "director", "shares_count": None},
            {"client_id": 43, "role": "komisaris", "shares_count": 0},
        ],
        lkpm=[
            {
                "client_id": 41,
                "company_id": 55,
                "year": 2026,
                "quarter": "Q1",
                "status": "approved",
            },
            {
                "client_id": 42,
                "company_id": 55,
                "year": 2026,
                "quarter": "Q2",
                "status": "submitted",
            },
            {
                "client_id": 42,
                "company_id": 55,
                "year": 2026,
                "quarter": "Q2",
                "status": "submitted",
            },
        ],
    )
    out = await call_endpoint(conn, "company")
    assert out["text"] == (
        "The company is a PT PMA, established on 1 February 2024. "
        "Authorized capital is IDR 10.0 billion, of which IDR 1,049,000,000 is paid up "
        "and it has one shareholder and one director linked in the CRM. "
        "It is registered under two KBLI lines, the latest LKPM on record is Q2 2026 (submitted) "
        "and the last amendment dates from 1 June 2025."
    )
    assert out["based_on"] == [
        "legal form",
        "establishment",
        "capital",
        "ownership",
        "KBLI",
        "LKPM",
        "amendments",
    ]


@pytest.mark.asyncio
async def test_end_to_end_company_with_an_absent_code_states_c6_and_no_count():
    conn = FakeConn(company={"kbli_code": "74100"}, verdicts={"74100": 0}, links=[], lkpm=[])
    out = await call_endpoint(conn, "company")
    assert out["text"] == "At least one KBLI code on file is not in the KBLI 2025 catalogue."
    assert "74100" not in json.dumps(out)


@pytest.mark.asyncio
async def test_a_client_level_lkpm_row_of_another_linked_client_omits_c7_for_every_opener():
    base = {
        "company": {"company_type": "PT"},
        "links": [
            {"client_id": 41, "role": "shareholder", "shares_count": 1},
            {"client_id": 42, "role": "director", "shares_count": None},
        ],
        "lkpm": [
            {
                "client_id": 41,
                "company_id": 55,
                "year": 2026,
                "quarter": "Q2",
                "status": "approved",
            },
            {"client_id": 42, "company_id": None, "year": 2025, "quarter": "Q4", "status": "draft"},
        ],
    }
    texts = {
        opener: (await call_endpoint(FakeConn(**base), "company"))["text"] for opener in (41, 42)
    }
    assert texts[41] == texts[42] and "LKPM" not in texts[41]
    base["lkpm"] = base["lkpm"][:1]
    assert "Q2 2026 (approved)" in (await call_endpoint(FakeConn(**base), "company"))["text"]


# ── PII sentinels ───────────────────────────────────────────────────────────


def _walk(v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield str(k)
            yield from _walk(x)
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _walk(x)
    else:
        yield str(v)


@pytest.mark.asyncio
async def test_sentinels_in_every_input_reach_no_payload_prompt_cache_row_response_or_log(caplog):
    s = SENTINEL
    imm = FakeConn(
        client={"passport_expiry": date(2027, 5, 1), "full_name": s},
        documents=[
            _doc(
                "visa",
                date(2027, 3, 14),
                json.dumps(
                    {
                        "raw_response": {
                            "visa_type": "E23",
                            "visa_number": s,
                            "sponsor": s,
                            "full_name": s,
                            "issue_date": "2026-03-15",
                            "address": s,
                        }
                    }
                ),
                file_name=s,
                notes=s,
            ),
            _doc("kitas", None, json.dumps({"raw_response": {"visa_type": f"E23 or {s}"}})),
        ],
        family=[
            {
                "current_visa_type": s,
                "visa_expiry": date(2027, 1, 1),
                "full_name": s,
                "passport_number": s,
            }
        ],
        practices=[
            {
                "id": 1,
                "status": s,
                "category": "kitas",
                "practice_type_name": "KITAS Extension",
                "title": s,
            },
            {"id": 2, "status": "on_process", "category": s, "practice_type_name": s},
        ],
        visa_types=VISA_TYPES,
    )
    company = {
        "company_type": s,
        "kbli_code": s,
        "kbli_description": s,
        "company_name": s,
        "nib": s,
        "npwp_company": s,
        "registered_address": s,
        "company_email": s,
        "akta_pendirian_no": s,
        "akta_pendirian_date": s,
        "akta_perubahan_date": s,
        "custom_fields": json.dumps(
            {
                "authorized_capital": s,
                "paid_up_capital": s,
                "total_shares": s,
                "akta_date": s,
                "shareholders": json.dumps([{"name": s}]),
                "kbli_codes": [s],
            }
        ),
    }
    co = FakeConn(
        company=company,
        links=[
            {"client_id": 41, "role": s, "shares_count": s},
            {"client_id": 42, "role": "director", "shares_count": None},
        ],
        lkpm=[{"client_id": 41, "company_id": 55, "year": s, "quarter": s, "status": s}],
    )
    seen: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for conn, scope in ((imm, "immigration"), (co, "company")):
            out = await call_endpoint(conn, scope)
            seen += list(_walk(out)) + [str(u) for u in conn.upserts]
            inputs = (
                await r.fetch_immigration_inputs(conn, 41)
                if scope == "immigration"
                else await r.fetch_company_inputs(conn, 55)
            )
            facts = (
                r.build_immigration_facts(**inputs)
                if scope == "immigration"
                else r.build_company_facts(**inputs)
            )
            payload = r.recap_payload(scope, facts)
            template = rt.template_recap(scope, payload)
            seen += list(_walk(payload)) + [template, rt.rewrite_prompt(scope, template, payload)]
    assert s not in " ".join(seen)
    assert s not in caplog.text
    for rec in caplog.records:
        assert s not in repr(vars(rec))


@pytest.mark.asyncio
async def test_a_failing_lookup_with_a_sentinel_in_its_message_logs_only_the_type(caplog):
    conn = FakeConn(
        company={"company_type": "PT"}, lkpm=RuntimeError(f"value {SENTINEL}"), links=[]
    )
    with caplog.at_level(logging.DEBUG):
        out = await call_endpoint(conn, "company")
    assert out["text"] == "The company is a PT."
    assert SENTINEL not in caplog.text and "RuntimeError" in caplog.text
