"""Tests for JurnalService lifecycle ownership in LKPMDataCollector."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.lkpm import EmploymentData
from backend.services.compliance import lkpm_data_collector


def _collector_with_employment(
    collector: lkpm_data_collector.LKPMDataCollector,
) -> None:
    collector.collect_employment = AsyncMock(return_value=EmploymentData())


@pytest.mark.asyncio
async def test_owned_jurnal_service_is_closed_after_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock()
    service.get_all_journal_entries = AsyncMock(return_value=[])
    service.close = AsyncMock()
    monkeypatch.setattr(lkpm_data_collector, "JurnalService", lambda: service)

    collector = lkpm_data_collector.LKPMDataCollector(db_pool=MagicMock())
    _collector_with_employment(collector)

    await collector.collect_from_jurnal(client_id=1, quarter="Q1", year=2026)

    service.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_owned_jurnal_service_is_closed_when_collection_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("Jurnal unavailable")
    service = MagicMock()
    service.get_all_journal_entries = AsyncMock(side_effect=error)
    service.close = AsyncMock()
    monkeypatch.setattr(lkpm_data_collector, "JurnalService", lambda: service)

    collector = lkpm_data_collector.LKPMDataCollector(db_pool=MagicMock())

    with pytest.raises(RuntimeError) as caught:
        await collector.collect_from_jurnal(client_id=1, quarter="Q1", year=2026)

    assert caught.value is error
    service.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_injected_jurnal_service_is_not_closed() -> None:
    service = MagicMock()
    service.get_all_journal_entries = AsyncMock(return_value=[])
    service.close = AsyncMock()
    collector = lkpm_data_collector.LKPMDataCollector(
        db_pool=MagicMock(),
        jurnal_service=service,
    )
    _collector_with_employment(collector)

    await collector.collect_from_jurnal(client_id=1, quarter="Q1", year=2026)

    service.close.assert_not_awaited()
