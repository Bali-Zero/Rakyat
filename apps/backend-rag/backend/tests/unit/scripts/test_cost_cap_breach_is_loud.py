"""Regression coverage for the daily cost-cap scheduler outcome."""

import argparse
import logging
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.core import database
from backend.scripts import cost_advisor_cli


def _pool_reading_today_cost(cost: Decimal) -> MagicMock:
    connection = MagicMock()
    connection.fetchval = AsyncMock(return_value=cost)
    acquire_context = MagicMock()
    acquire_context.__aenter__ = AsyncMock(return_value=connection)
    acquire_context.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value = acquire_context
    return pool


@pytest.mark.asyncio
async def test_daily_cap_command_exits_loudly_when_cost_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _pool_reading_today_cost(Decimal("20.01"))
    monkeypatch.setattr(database, "get_db_pool", AsyncMock(return_value=pool))
    caplog.set_level(logging.ERROR, logger=cost_advisor_cli.__name__)

    exit_code = 0
    try:
        await cost_advisor_cli._main(argparse.Namespace(check_daily_cap=True))
    except SystemExit as error:
        exit_code = error.code

    assert exit_code == cost_advisor_cli.EXIT_DAILY_CAP_EXCEEDED == 3
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert date.today().isoformat() in record.message
    assert "20.01" in record.message
    assert "20.00" in record.message


@pytest.mark.asyncio
async def test_daily_cap_command_is_quiet_under_cap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _pool_reading_today_cost(Decimal("19.99"))
    monkeypatch.setattr(database, "get_db_pool", AsyncMock(return_value=pool))
    caplog.set_level(logging.ERROR, logger=cost_advisor_cli.__name__)

    await cost_advisor_cli._main(argparse.Namespace(check_daily_cap=True))

    assert not caplog.records
