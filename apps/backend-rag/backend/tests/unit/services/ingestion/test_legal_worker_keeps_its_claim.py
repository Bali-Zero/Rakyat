"""The legal ingest worker renews its claim while a job runs (no database needed)."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.ingestion import legal_full_ingestion_worker as worker


class _Acquired:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _pool_with_one_pending_job(renewals: list[tuple[Any, ...]], renewed: asyncio.Event) -> Any:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Acquired(None))
    conn.fetchrow = AsyncMock(
        return_value={
            "id": "job-1",
            "tipo": "UU",
            "nomor": "1",
            "anno": "2099",
            "source_url": "https://example.test/law.pdf",
            "nb_target": "none",
            "titolo": None,
            "status": "pending",
        }
    )

    async def execute(sql: str, *args: Any) -> None:
        if "visibility_at" in sql:
            renewals.append(args)
            renewed.set()

    conn.execute = AsyncMock(side_effect=execute)
    pool = MagicMock()
    pool.acquire.return_value = _Acquired(conn)
    return pool


async def test_the_worker_renews_its_claim_while_the_job_runs_and_stops_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker, "CLAIM_RENEW_INTERVAL", timedelta(milliseconds=10), raising=False)
    renewals: list[tuple[Any, ...]] = []
    renewed = asyncio.Event()
    release = asyncio.Event()

    async def held_download(*args: Any, **kwargs: Any) -> None:
        await release.wait()
        raise RuntimeError("the test ends the job here")

    monkeypatch.setattr(worker, "_download_pdf", held_download)
    running = asyncio.create_task(
        worker._process_one_job(_pool_with_one_pending_job(renewals, renewed), MagicMock())
    )
    try:
        await asyncio.wait_for(renewed.wait(), 2)
    finally:
        release.set()
        await asyncio.wait_for(running, 5)

    assert renewals[0] == ("job-1", worker.VISIBILITY_TIMEOUT)
    seen = len(renewals)
    await asyncio.sleep(0.05)
    assert len(renewals) == seen, "the claim was renewed after the job had ended"


def test_the_claim_is_renewed_well_inside_the_time_it_hides_the_job_for() -> None:
    assert worker.CLAIM_RENEW_INTERVAL * 3 <= worker.VISIBILITY_TIMEOUT
    assert worker.CLAIM_MAX_HOLD > worker.VISIBILITY_TIMEOUT


class _Renewals:
    """Stands in for the renewal: records when each one starts, and takes its time."""

    def __init__(self, takes: float, hangs_first: bool = False) -> None:
        self.takes = takes
        self.hangs_first = hangs_first
        self.started_at: list[float] = []

    async def __call__(self, db_pool: Any, job_id: str) -> None:
        self.started_at.append(asyncio.get_running_loop().time())
        if self.hangs_first and len(self.started_at) == 1:
            await asyncio.Event().wait()
        await asyncio.sleep(self.takes)


@pytest.mark.parametrize(
    "takes", [0.0, 0.03, 0.06], ids=["instant", "slow", "slower than the interval"]
)
async def test_no_renewal_starts_after_the_limit_however_long_each_one_takes(
    monkeypatch: pytest.MonkeyPatch, takes: float
) -> None:
    interval, limit = 0.04, 0.4
    monkeypatch.setattr(worker, "CLAIM_RENEW_INTERVAL", timedelta(seconds=interval), raising=False)
    monkeypatch.setattr(worker, "CLAIM_MAX_HOLD", timedelta(seconds=limit), raising=False)
    renewals = _Renewals(takes)
    monkeypatch.setattr(worker, "_renew_claim", renewals, raising=False)
    loop = asyncio.get_running_loop()
    began = loop.time()

    await asyncio.wait_for(worker._keep_claim(MagicMock(), "job-1"), 5)

    ended = loop.time()
    assert len(renewals.started_at) >= 3, "the claim was hardly renewed inside the limit"
    assert max(renewals.started_at) < began + limit, "a renewal started after the limit"
    assert ended < began + limit + interval, "the keeper went on past the limit"
    seen = len(renewals.started_at)
    await asyncio.sleep(interval * 3)
    assert len(renewals.started_at) == seen


async def test_a_renewal_that_never_answers_does_not_stop_the_next_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interval, limit = 0.04, 0.4
    monkeypatch.setattr(worker, "CLAIM_RENEW_INTERVAL", timedelta(seconds=interval), raising=False)
    monkeypatch.setattr(worker, "CLAIM_MAX_HOLD", timedelta(seconds=limit), raising=False)
    renewals = _Renewals(0.0, hangs_first=True)
    monkeypatch.setattr(worker, "_renew_claim", renewals, raising=False)
    loop = asyncio.get_running_loop()
    began = loop.time()

    await asyncio.wait_for(worker._keep_claim(MagicMock(), "job-1"), 5)

    assert len(renewals.started_at) >= 3, "one renewal that hung stopped all the others"
    assert max(renewals.started_at) < began + limit
    assert loop.time() < began + limit + interval
