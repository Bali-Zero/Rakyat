"""A timesheet export with an open shift still downloads, as a well-formed CSV."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.analytics.team_timesheet_service import TeamTimesheetService

_DAY = date(2026, 3, 2)


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "user_id": "u1",
        "email": "member@example.invalid",
        "work_date": _DAY,
        "clock_in_bali": datetime(2026, 3, 2, 9, 0),
        "clock_out_bali": datetime(2026, 3, 2, 17, 0),
        "hours_worked": Decimal("8.00"),
    }
    row.update(over)
    return row


async def _export(rows: list[dict[str, Any]]) -> str:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return await TeamTimesheetService(db_pool=pool).export_timesheet_csv(
        datetime(2026, 3, 1), datetime(2026, 3, 31)
    )


def _cells(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


@pytest.mark.asyncio
async def test_a_member_still_clocked_in_exports_with_an_empty_clock_out() -> None:
    text = await _export(
        [_row(), _row(email="open@example.invalid", clock_out_bali=None, hours_worked=None)]
    )

    assert _cells(text)[2] == ["open@example.invalid", "2026-03-02", "09:00", "", ""]


@pytest.mark.asyncio
async def test_a_row_missing_every_nullable_column_still_exports() -> None:
    text = await _export(
        [_row(work_date=None, clock_in_bali=None, clock_out_bali=None, hours_worked=None)]
    )

    assert _cells(text)[1] == ["member@example.invalid", "", "", "", ""]


@pytest.mark.asyncio
async def test_a_comma_or_quote_in_the_email_keeps_five_columns() -> None:
    text = await _export([_row(email='a,b"c@example.invalid')])

    assert _cells(text)[1][0] == 'a,b"c@example.invalid'
    assert all(len(r) == 5 for r in _cells(text))


@pytest.mark.asyncio
@pytest.mark.parametrize("lead", ["=", "+", "-", "@"])
async def test_an_email_starting_with_a_formula_character_is_neutralised(lead: str) -> None:
    text = await _export([_row(email=f"{lead}cmd@example.invalid")])

    assert _cells(text)[1][0] == f"'{lead}cmd@example.invalid"


@pytest.mark.asyncio
async def test_an_ordinary_export_is_byte_identical_to_before() -> None:
    text = await _export([_row(), _row(email="second@example.invalid", hours_worked=7.5)])

    assert text == (
        "Email,Date,Clock In,Clock Out,Hours Worked\n"
        "member@example.invalid,2026-03-02,09:00,17:00,8.00\n"
        "second@example.invalid,2026-03-02,09:00,17:00,7.5"
    )
