"""An LKPM report reads the configuration of its own company (real PostgreSQL).

One client can report for several companies. The configuration lookup went by client only, so
the ready pack and the validation of one company's report could carry another company's name,
tax number, licence number and investment plan; a report whose company had no configuration was
in no team listing, and a company-less report of a client with two company-less configurations
was listed twice.

Every expected value below is a literal written from the constants of this file. None is
computed by the lookup, the validator or another helper of the service.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException

from backend.app.models.lkpm import InvestmentRealization, LKPMClientSubmission, LKPMDraft
from backend.app.routers import lkpm as lkpm_router
from backend.services.compliance.lkpm_service import LKPMService, LKPMSubmissionConflict

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

_MONEY = (
    "equipment_domestic equipment_import building_domestic building_import "
    "vehicle_domestic vehicle_import land working_capital other"
).split()
_TABLES = f"""
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    company_name TEXT NOT NULL,
    npwp_company TEXT,
    nib TEXT,
    kbli_code TEXT
);
CREATE TABLE client_company_links (
    client_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    is_primary BOOLEAN
);
CREATE TABLE lkpm_client_config (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL,
    company_id INTEGER,
    company_name TEXT NOT NULL,
    npwp TEXT,
    nib TEXT,
    kbli_codes TEXT[] NOT NULL DEFAULT '{{}}',
    {" ".join(f"planned_{name} BIGINT NOT NULL DEFAULT 0," for name in _MONEY)}
    planned_tki INTEGER NOT NULL DEFAULT 0,
    planned_tka INTEGER NOT NULL DEFAULT 0,
    jurnal_api_key TEXT,
    jurnal_company_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_lkpm_client UNIQUE (client_id, company_id)
);
CREATE TABLE lkpm_reports (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL,
    company_id INTEGER,
    quarter TEXT NOT NULL,
    year INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    {" ".join(f"realized_{name} BIGINT NOT NULL DEFAULT 0," for name in _MONEY)}
    {" ".join(f"cumulative_{name} BIGINT NOT NULL DEFAULT 0," for name in _MONEY)}
    current_tki INTEGER NOT NULL DEFAULT 0,
    current_tka INTEGER NOT NULL DEFAULT 0,
    quarterly_revenue BIGINT NOT NULL DEFAULT 0,
    annual_revenue BIGINT NOT NULL DEFAULT 0,
    narrative_obstacles TEXT,
    narrative_plans TEXT,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    validation_alerts JSONB NOT NULL DEFAULT '[]',
    validated_at TIMESTAMPTZ,
    validated_by TEXT,
    client_approved BOOLEAN NOT NULL DEFAULT FALSE,
    client_approved_at TIMESTAMPTZ,
    oss_submitted BOOLEAN NOT NULL DEFAULT FALSE,
    oss_submitted_at TIMESTAMPTZ,
    oss_submitted_by TEXT,
    oss_receipt_number TEXT,
    oss_receipt_file_url TEXT,
    data_source TEXT NOT NULL DEFAULT 'manual',
    has_ai_categorized_items BOOLEAN NOT NULL DEFAULT FALSE,
    ai_categorized_count INTEGER NOT NULL DEFAULT 0,
    lkpm_assigned_to TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_lkpm_report UNIQUE (client_id, company_id, quarter, year)
);
CREATE TABLE kg_nodes (entity_id TEXT PRIMARY KEY);
CREATE TABLE practices (
    id SERIAL PRIMARY KEY,
    client_id INTEGER,
    practice_type TEXT,
    status TEXT
);
"""

CLIENT = 900001
QUARTER, YEAR = "Q2", 2026
JURNAL_ACCESS = "zz-jurnal"  # what every configuration of this file holds for Jurnal

# Two companies of one client. Nothing of one equals anything of the other.
ALPHA = {
    "id": 900101,
    "name": "PT Zz Alpha",
    "npwp": "NPWP-ALPHA",
    "nib": "NIB-ALPHA",
    "code": "62010",
    "plan": 1_000_000_000,  # planned working capital, the only planned category
    "cumulative": 500_000_000,  # of its report: 50.0 per cent of its own plan
}
BETA = {
    "id": 900102,
    "name": "PT Zz Beta",
    "npwp": "NPWP-BETA",
    "nib": "NIB-BETA",
    "code": "70209",
    "plan": 8_000_000_000,
    "cumulative": 2_000_000_000,  # 25.0 per cent of its own plan
}
# On the other company's plan the two would read 6.25 and 200.0.


@pytest_asyncio.fixture()
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"lkpm_company_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            db_pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
            )
            try:
                async with db_pool.acquire() as conn:
                    await conn.execute(_TABLES)
                    for company in (ALPHA, BETA):
                        await conn.execute(
                            "INSERT INTO companies VALUES ($1, $2, $3, $4, $5)",
                            company["id"],
                            company["name"],
                            company["npwp"],
                            company["nib"],
                            company["code"],
                        )
                        await conn.execute(
                            "INSERT INTO client_company_links VALUES ($1, $2, 'active', $3)",
                            CLIENT,
                            company["id"],
                            company is ALPHA,
                        )
                        await conn.execute(
                            "INSERT INTO kg_nodes VALUES ($1)", f"kbli:{company['code']}"
                        )
                yield db_pool
            finally:
                await db_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


async def _config(
    pool: asyncpg.Pool, identity: dict[str, Any], *, company: int | None, name: str | None = None
) -> int:
    """A configuration row holding ``identity``'s data, filed under ``company`` (None: unnamed)."""
    return await pool.fetchval(
        "INSERT INTO lkpm_client_config (client_id, company_id, company_name, npwp, nib, "
        "kbli_codes, planned_working_capital, jurnal_api_key) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
        CLIENT,
        company,
        name or identity["name"],
        identity["npwp"],
        identity["nib"],
        [identity["code"]],
        identity["plan"],
        JURNAL_ACCESS,
    )


async def _report(
    pool: asyncpg.Pool,
    company: int | None,
    *,
    cumulative: int = 1,
    validation_status: str = "pending",
    status: str = "draft",
    alerts: str = "[]",
    quarter: str = QUARTER,
) -> int:
    return await pool.fetchval(
        "INSERT INTO lkpm_reports (client_id, company_id, quarter, year, status, "
        "realized_working_capital, cumulative_working_capital, validation_status, "
        "validation_alerts) VALUES ($1, $2, $3, $4, $5, $6, $6, $7, $8::jsonb) RETURNING id",
        CLIENT,
        company,
        quarter,
        YEAR,
        status,
        cumulative,
        validation_status,
        alerts,
    )


async def test_submission_happens_once_and_only_after_approval(pool: asyncpg.Pool) -> None:
    approved_report = await _report(pool, ALPHA["id"], status="approved")
    draft_report = await _report(pool, BETA["id"], status="draft")
    service = LKPMService(pool)

    result = await service.mark_submitted(approved_report, "staff@example.test")
    assert result["status"] == "submitted"
    first_submission = await pool.fetchrow(
        "SELECT status, oss_submitted, oss_submitted_at, oss_submitted_by "
        "FROM lkpm_reports WHERE id = $1",
        approved_report,
    )
    assert first_submission["status"] == "submitted"
    assert first_submission["oss_submitted"] is True

    with pytest.raises(LKPMSubmissionConflict, match="already submitted"):
        await service.mark_submitted(approved_report, "second@example.test")
    second_submission = await pool.fetchrow(
        "SELECT oss_submitted_at, oss_submitted_by FROM lkpm_reports WHERE id = $1",
        approved_report,
    )
    assert tuple(second_submission.values()) == (
        first_submission["oss_submitted_at"],
        first_submission["oss_submitted_by"],
    )

    with pytest.raises(LKPMSubmissionConflict, match="not approved"):
        await service.mark_submitted(draft_report, "staff@example.test")
    draft_submission = await pool.fetchrow(
        "SELECT status, oss_submitted, oss_submitted_at, oss_submitted_by "
        "FROM lkpm_reports WHERE id = $1",
        draft_report,
    )
    assert tuple(draft_submission.values()) == ("draft", False, None, None)


async def _both_configured(pool: asyncpg.Pool) -> tuple[int, int]:
    """Each company with its own configuration and its own report; the two report ids."""
    await _config(pool, ALPHA, company=ALPHA["id"])
    await _config(pool, BETA, company=BETA["id"])
    return (
        await _report(pool, ALPHA["id"], cumulative=ALPHA["cumulative"]),
        await _report(pool, BETA["id"], cumulative=BETA["cumulative"]),
    )


async def _written(pool: asyncpg.Pool, report_id: int) -> tuple[Any, ...]:
    """What a validation writes on a report, and how many configurations exist."""
    row = await pool.fetchrow(
        "SELECT validation_status, status, validated_at, updated_at, validation_alerts "
        "FROM lkpm_reports WHERE id = $1",
        report_id,
    )
    return (*row.values(), await pool.fetchval("SELECT count(*) FROM lkpm_client_config"))


def _staff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The routes' authorization is not what these tests are about."""
    monkeypatch.setattr(lkpm_router, "_require_lkpm_staff", lambda user, **_: {})


async def _refused_everywhere(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, report_id: int
) -> None:
    """Pack and validation raise, both routes answer their 404, and nothing was written."""
    service = LKPMService(pool)
    before = await _written(pool, report_id)
    with pytest.raises(ValueError):
        await service.get_ready_pack(report_id)
    with pytest.raises(ValueError):
        await service.validate_draft(report_id)
    _staff(monkeypatch)
    for route in (lkpm_router.get_ready_pack, lkpm_router.validate_draft):
        with pytest.raises(HTTPException) as refused:
            await route(report_id, current_user={}, db_pool=pool)
        assert (refused.value.status_code, refused.value.detail) == (404, "LKPM draft not found")
    assert await _written(pool, report_id) == before


# ── 1. the ready pack ─────────────────────────────────────────────────────────


async def test_the_ready_pack_of_each_report_carries_its_own_company(pool: asyncpg.Pool) -> None:
    alpha_report, beta_report = await _both_configured(pool)
    service = LKPMService(pool)

    alpha = await service.get_ready_pack(alpha_report)
    beta = await service.get_ready_pack(beta_report)

    assert (alpha.company_name, alpha.npwp, alpha.nib, alpha.kbli_codes) == (
        "PT Zz Alpha", "NPWP-ALPHA", "NIB-ALPHA", ["62010"],
    )  # fmt: skip
    assert (alpha.plan.working_capital, alpha.realization_percentage) == (1_000_000_000, 50.0)
    assert (beta.company_name, beta.npwp, beta.nib, beta.kbli_codes) == (
        "PT Zz Beta", "NPWP-BETA", "NIB-BETA", ["70209"],
    )  # fmt: skip
    assert (beta.plan.working_capital, beta.realization_percentage) == (8_000_000_000, 25.0)


# ── 2. the validation, read back from the table ───────────────────────────────


def _alerts(code: str, percentage: str) -> list[dict[str, Any]]:
    return [
        {"field": f"kbli_{code}", "severity": "green", "message": f"KBLI {code} verified",
         "details": None},
        {"field": "tka_count", "severity": "green", "message": "TKA count matches CRM (0)",
         "details": None},
        {"field": "working_capital", "severity": "green",
         "message": f"working_capital: {percentage}% of plan realized", "details": None},
    ]  # fmt: skip


async def test_each_report_is_validated_against_its_own_plan_and_stored_so(
    pool: asyncpg.Pool,
) -> None:
    alpha_report, beta_report = await _both_configured(pool)
    service = LKPMService(pool)

    await service.validate_draft(alpha_report)
    await service.validate_draft(beta_report)

    for report_id, expected in ((alpha_report, _alerts("62010", "50.0")),
                                (beta_report, _alerts("70209", "25.0"))):  # fmt: skip
        row = await pool.fetchrow(
            "SELECT validation_status, status, validated_at, validation_alerts "
            "FROM lkpm_reports WHERE id = $1",
            report_id,
        )
        assert json.loads(row["validation_alerts"]) == expected
        assert (row["validation_status"], row["status"]) == ("valid", "validated")
        assert row["validated_at"] is not None
    stored = await pool.fetchval(
        "SELECT validation_alerts::text FROM lkpm_reports WHERE id = $1", beta_report
    )
    assert "exceeds plan" not in stored


# ── 3. one configuration that names no company and holds one company's identity ─


async def test_a_configuration_with_no_company_never_answers_for_a_named_company(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client is linked to both companies; its one configuration is Alpha's, unnamed."""
    await _config(pool, ALPHA, company=None)
    alpha_report = await _report(pool, ALPHA["id"])
    beta_report = await _report(pool, BETA["id"])
    no_company_report = await _report(pool, None, quarter="Q1", cumulative=250_000_000)

    await _refused_everywhere(pool, monkeypatch, beta_report)
    await _refused_everywhere(pool, monkeypatch, alpha_report)  # an exact row is required

    pack = await LKPMService(pool).get_ready_pack(no_company_report)
    assert (pack.company_name, pack.realization_percentage) == ("PT Zz Alpha", 25.0)


# ── 4. a report whose company has no configuration, beside another company's ──


async def test_a_report_never_reads_the_configuration_of_another_company(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"])
    beta_report = await _report(pool, BETA["id"])

    await _refused_everywhere(pool, monkeypatch, beta_report)


# ── 5. an unnamed configuration beside a named one ────────────────────────────


async def test_named_and_unnamed_configurations_each_answer_their_own_report(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _config(pool, BETA, company=None, name="PT Zz Unnamed")
    await _config(pool, ALPHA, company=ALPHA["id"])
    alpha_report = await _report(pool, ALPHA["id"], cumulative=ALPHA["cumulative"])
    beta_report = await _report(pool, BETA["id"])
    no_company_report = await _report(pool, None, quarter="Q1", cumulative=BETA["cumulative"])
    service = LKPMService(pool)

    alpha = await service.get_ready_pack(alpha_report)
    unnamed = await service.get_ready_pack(no_company_report)

    assert (alpha.company_name, alpha.realization_percentage) == ("PT Zz Alpha", 50.0)
    assert (unnamed.company_name, unnamed.realization_percentage) == ("PT Zz Unnamed", 25.0)
    await _refused_everywhere(pool, monkeypatch, beta_report)


# ── 6. a report with no company and no single configuration to pair it with ───


@pytest.mark.parametrize(
    "configurations",
    [
        pytest.param([(ALPHA, ALPHA["id"]), (BETA, BETA["id"])], id="two named"),
        pytest.param([(ALPHA, None), (BETA, None)], id="two unnamed"),
        pytest.param([], id="none"),
    ],
)
async def test_a_report_with_no_company_is_refused_when_no_single_row_is_its_own(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, configurations: list[tuple[dict, Any]]
) -> None:
    for identity, company in configurations:
        await _config(pool, identity, company=company)
    report = await _report(pool, None)

    await _refused_everywhere(pool, monkeypatch, report)


async def test_the_lookup_names_the_client_when_it_refuses_to_pick(pool: asyncpg.Pool) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"])
    await _config(pool, BETA, company=BETA["id"])

    with pytest.raises(ValueError, match="900001.*more than one company"):
        await LKPMService(pool).get_client_config(CLIENT)


# ── 7. the Jurnal synchronisation ─────────────────────────────────────────────


def _collector(service: LKPMService) -> AsyncMock:
    collected = LKPMDraft(
        client_id=CLIENT,
        quarter=QUARTER,
        year=YEAR,
        realized=InvestmentRealization(working_capital=7),
    )
    service.data_collector.collect_from_jurnal = AsyncMock(return_value=collected)
    return service.data_collector.collect_from_jurnal


async def test_the_synchronisation_refuses_a_client_with_several_configurations(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"])
    await _config(pool, BETA, company=BETA["id"])
    service = LKPMService(pool)
    collect = _collector(service)

    with pytest.raises(ValueError, match="more than one company"):
        await service.sync_jurnal(CLIENT, QUARTER, YEAR)

    collect.assert_not_awaited()
    assert await pool.fetchval("SELECT count(*) FROM lkpm_reports") == 0
    _staff(monkeypatch)
    with pytest.raises(HTTPException) as refused:
        await lkpm_router.sync_jurnal(
            CLIENT, quarter=QUARTER, year=YEAR, body=None, current_user={}, db_pool=pool
        )
    assert (refused.value.status_code, refused.value.detail) == (
        400, "Invalid LKPM synchronization request",
    )  # fmt: skip


async def test_the_synchronisation_of_a_client_with_one_configuration_is_as_before(
    pool: asyncpg.Pool,
) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"])
    service = LKPMService(pool)
    collect = _collector(service)

    draft = await service.sync_jurnal(CLIENT, QUARTER, YEAR)

    assert JURNAL_ACCESS in collect.await_args.kwargs.values()
    assert await pool.fetchval(
        "SELECT realized_working_capital FROM lkpm_reports WHERE id = $1", draft.id
    ) == 7  # fmt: skip


# ── 8. the draft generator ────────────────────────────────────────────────────


async def test_a_new_draft_is_refused_for_a_client_with_several_configurations(
    pool: asyncpg.Pool,
) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"])
    await _config(pool, BETA, company=BETA["id"])

    with pytest.raises(ValueError, match="more than one company"):
        await LKPMService(pool).generate_draft(CLIENT, QUARTER, YEAR)

    assert await pool.fetchval("SELECT count(*) FROM lkpm_reports") == 0


async def test_an_existing_report_is_still_returned_before_any_configuration_is_read(
    pool: asyncpg.Pool,
) -> None:
    """A limit that stays: the generator answers with an existing report first."""
    await _config(pool, ALPHA, company=ALPHA["id"])
    await _config(pool, BETA, company=BETA["id"])
    existing = await _report(pool, None)

    assert (await LKPMService(pool).generate_draft(CLIENT, QUARTER, YEAR)).id == existing


async def test_a_new_draft_for_a_client_with_one_configuration_is_as_before(
    pool: asyncpg.Pool,
) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"])

    draft = await LKPMService(pool).generate_draft(CLIENT, QUARTER, YEAR)

    assert await pool.fetchval("SELECT client_id FROM lkpm_reports WHERE id = $1", draft.id) == (
        CLIENT
    )


# ── 9. the configuration a submission creates ─────────────────────────────────


async def _configurations(pool: asyncpg.Pool) -> list[tuple[Any, ...]]:
    rows = await pool.fetch(
        "SELECT company_id, company_name, npwp, nib, kbli_codes FROM lkpm_client_config "
        "WHERE client_id = $1 ORDER BY company_id",
        CLIENT,
    )
    return [tuple(row.values()) for row in rows]


async def _submit(service: LKPMService, company: int) -> None:
    submission = LKPMClientSubmission(
        client_id=CLIENT, company_id=company, quarter=QUARTER, year=YEAR, working_capital=5
    )
    await service._submit_form_data_for_validated_target(submission, target_client_id=CLIENT)


ALPHA_ROW = (900101, "PT Zz Alpha", "NPWP-ALPHA", "NIB-ALPHA", ["62010"])
BETA_ROW = (900102, "PT Zz Beta", "NPWP-BETA", "NIB-BETA", ["70209"])


async def test_each_company_gets_its_own_configuration_on_its_first_submission(
    pool: asyncpg.Pool,
) -> None:
    service = LKPMService(pool)

    await _submit(service, ALPHA["id"])
    assert await _configurations(pool) == [ALPHA_ROW]

    await _submit(service, BETA["id"])
    assert await _configurations(pool) == [ALPHA_ROW, BETA_ROW]

    await _submit(service, ALPHA["id"])  # a second submission for a company creates nothing
    assert await _configurations(pool) == [ALPHA_ROW, BETA_ROW]


async def test_no_configuration_is_created_for_a_company_the_client_is_not_linked_to(
    pool: asyncpg.Pool,
) -> None:
    await pool.execute(
        "UPDATE client_company_links SET status = 'ended' WHERE company_id = $1", BETA["id"]
    )

    await LKPMService(pool)._ensure_client_config(CLIENT, BETA["id"])

    assert await _configurations(pool) == []


async def test_called_with_no_company_the_helper_behaves_as_before(pool: asyncpg.Pool) -> None:
    service = LKPMService(pool)

    await service._ensure_client_config(CLIENT)  # no configuration yet: the primary company
    assert await _configurations(pool) == [ALPHA_ROW]

    await service._ensure_client_config(CLIENT)  # one exists: nothing more
    assert await _configurations(pool) == [ALPHA_ROW]


# ── 10 and 11. the team listings: one row per report ──────────────────────────


async def test_the_batch_lists_every_report_of_the_quarter_exactly_once(
    pool: asyncpg.Pool,
) -> None:
    await _config(pool, ALPHA, company=ALPHA["id"], name="PT Zz Alpha (configured)")
    await _config(pool, ALPHA, company=None, name="PT Zz Unnamed One")
    await _config(pool, BETA, company=None, name="PT Zz Unnamed Two")
    configured = await _report(pool, ALPHA["id"])
    not_configured = await _report(pool, BETA["id"])
    no_company = await _report(pool, None)
    await _report(pool, ALPHA["id"], quarter="Q1")  # another quarter: not listed

    items = await LKPMService(pool).get_batch(QUARTER, YEAR)

    assert [(item.id, item.company_name) for item in items] == [
        (no_company, ""),  # two unnamed configurations: neither is picked
        (configured, "PT Zz Alpha (configured)"),
        (not_configured, "PT Zz Beta"),  # no configuration: the company's own name
    ]
    assert len(items) == 3


INVALID = json.dumps([{"field": "x", "severity": "red", "message": "m", "details": None}])


async def test_the_alerts_list_an_invalid_open_report_once_whatever_its_configuration(
    pool: asyncpg.Pool,
) -> None:
    await _config(pool, ALPHA, company=None, name="PT Zz Unnamed One")
    await _config(pool, BETA, company=None, name="PT Zz Unnamed Two")
    invalid = await _report(pool, BETA["id"], validation_status="invalid", alerts=INVALID)
    invalid_no_company = await _report(
        pool, None, validation_status="invalid", alerts=INVALID, quarter="Q1"
    )
    pending = await _report(pool, ALPHA["id"])  # no configuration either, and not invalid
    await _report(
        pool, BETA["id"], validation_status="invalid", alerts=INVALID, status="submitted",
        quarter="Q3",
    )  # fmt: skip

    service = LKPMService(pool)
    alerts = await service.get_alerts()

    assert [
        (alert["draft_id"], alert["company_name"], alert["red_alerts"]) for alert in alerts
    ] == [
        (invalid, "PT Zz Beta", 1),
        (invalid_no_company, "", 1),
    ]
    assert pending in [item.id for item in await service.get_batch(QUARTER, YEAR)]
