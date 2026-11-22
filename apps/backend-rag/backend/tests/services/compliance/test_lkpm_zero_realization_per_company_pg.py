"""The zero-realization streak of an LKPM report counts its own company's quarters (real PostgreSQL).

The validator read the latest report rows of the CLIENT and called each row a quarter. A client
with two companies has one row per company per quarter, so two quarters at zero read as four
and the red alert kept the report from being validated; another company's later report could
also hide a real streak, and rows later than the report being validated were counted.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.app.models.lkpm import InvestmentRealization, LKPMClientConfig, LKPMDraft
from backend.services.compliance.lkpm_validator import LKPMValidator

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
# The realized amounts may be NULL, as on the real table since the legacy import.
_TABLE = f"""
CREATE TABLE lkpm_reports (
    id SERIAL PRIMARY KEY,
    client_id INTEGER NOT NULL,
    company_id INTEGER,
    quarter TEXT NOT NULL,
    year INTEGER NOT NULL,
    {" ".join(f"realized_{name} BIGINT DEFAULT 0," for name in _MONEY)}
    CONSTRAINT uq_lkpm_report UNIQUE (client_id, company_id, quarter, year)
);
"""

CLIENT = 900001
ALPHA, BETA = 900101, 900102
YEAR = 2026


@pytest_asyncio.fixture()
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"lkpm_streak_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            db_pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
            )
            try:
                await db_pool.execute(_TABLE)
                yield db_pool
            finally:
                await db_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


async def _report(
    pool: asyncpg.Pool, company: int | None, quarter: str, realized: int | None = 0
) -> None:
    await pool.execute(
        "INSERT INTO lkpm_reports (client_id, company_id, quarter, year, "
        "realized_working_capital) VALUES ($1, $2, $3, $4, $5)",
        CLIENT,
        company,
        quarter,
        YEAR,
        realized,
    )


async def _streak(pool: asyncpg.Pool, company: int | None, quarter: str) -> list[tuple[str, str]]:
    alerts = await LKPMValidator(pool).detect_zero_realization(
        CLIENT, company, year=YEAR, quarter=quarter
    )
    return [(alert.severity.value, alert.message) for alert in alerts]


TWO = [("yellow", "2 consecutive quarters with zero realization")]
FOUR = [("red", "4 consecutive quarters with zero realization")]


async def test_two_companies_at_zero_for_two_quarters_are_two_quarters_each(
    pool: asyncpg.Pool,
) -> None:
    for company in (ALPHA, BETA):
        for quarter in ("Q1", "Q2"):
            await _report(pool, company, quarter)

    assert await _streak(pool, ALPHA, "Q2") == TWO
    assert await _streak(pool, BETA, "Q2") == TWO


async def test_one_company_at_zero_for_two_quarters_is_two_quarters(pool: asyncpg.Pool) -> None:
    for quarter in ("Q1", "Q2"):
        await _report(pool, ALPHA, quarter)

    assert await _streak(pool, ALPHA, "Q2") == TWO


async def test_four_quarters_at_zero_of_one_company_are_still_a_red_alert(
    pool: asyncpg.Pool,
) -> None:
    for quarter in ("Q1", "Q2", "Q3", "Q4"):
        await _report(pool, ALPHA, quarter)

    assert await _streak(pool, ALPHA, "Q4") == FOUR


async def test_another_companys_later_report_does_not_hide_the_streak(pool: asyncpg.Pool) -> None:
    for quarter in ("Q1", "Q2", "Q3", "Q4"):
        await _report(pool, ALPHA, quarter)
    await _report(pool, BETA, "Q4", realized=5_000_000)

    assert await _streak(pool, ALPHA, "Q4") == FOUR
    assert await _streak(pool, BETA, "Q4") == []


async def test_quarters_after_the_report_being_validated_are_not_counted(
    pool: asyncpg.Pool,
) -> None:
    await _report(pool, ALPHA, "Q2", realized=5_000_000)
    await _report(pool, ALPHA, "Q3")
    await _report(pool, ALPHA, "Q4")

    assert await _streak(pool, ALPHA, "Q2") == []
    assert await _streak(pool, ALPHA, "Q4") == TWO


async def test_an_amount_never_filled_in_counts_as_zero(pool: asyncpg.Pool) -> None:
    await _report(pool, ALPHA, "Q1", realized=None)
    await _report(pool, ALPHA, "Q2")

    assert await _streak(pool, ALPHA, "Q2") == TWO


async def test_a_report_with_no_company_reads_the_rows_with_no_company(pool: asyncpg.Pool) -> None:
    for quarter in ("Q1", "Q2"):
        await _report(pool, None, quarter)
        await _report(pool, ALPHA, quarter)

    assert await _streak(pool, None, "Q2") == TWO


async def test_the_validation_of_a_two_company_report_is_not_blocked_by_the_other_company(
    pool: asyncpg.Pool,
) -> None:
    """Through ``validate``: a red alert is what keeps a report from being validated."""
    for company in (ALPHA, BETA):
        for quarter in ("Q1", "Q2"):
            await _report(pool, company, quarter)
    draft = LKPMDraft(client_id=CLIENT, company_id=ALPHA, quarter="Q2", year=YEAR)
    config = LKPMClientConfig(
        client_id=CLIENT,
        company_id=ALPHA,
        company_name="PT Zz Alpha",
        kbli_codes=["62010"],
        planned=InvestmentRealization(working_capital=1_000_000_000),
    )

    result = await LKPMValidator(pool).validate(draft, config)

    assert result.red_count == 0
    assert result.is_valid is True
    assert ("yellow", "2 consecutive quarters with zero realization") in [
        (alert.severity.value, alert.message) for alert in result.alerts
    ]
