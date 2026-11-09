"""P1/P2 - the ``recovery-v1`` pipeline tag is held back from both default consumers.

The client-document-recovery producer enqueues ``source='drive'`` rows tagged
``pipeline_version='recovery-v1'``. Two guarantees keep those rows under the
producer's control. Per the cicatrix #3 antidote each ships a GUILT test (the tag
is held back) and an INNOCENCE test (every other tag behaves exactly as before):

* P1 - the routing chokepoint never evaluates an auto-attach gate for the tag,
  even with every kill-switch armed (the live worker runs armed).
* P2 - the unfiltered default worker never claims the tag; a worker whose
  ``INTAKE_PIPELINE_VERSION_FILTER`` names it still does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import cast
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from backend.services.intake import auto_attach, routing
from backend.services.intake import worker as worker_module
from backend.services.intake.enqueue import enqueue
from backend.services.intake.worker import (
    FILTER_ONLY_PIPELINE_VERSIONS,
    IntakeWorker,
    WorkerConfig,
)

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://nuzantara@localhost:5432/nuzantara_test",
)


def _proposal(pipeline_version: str) -> dict:
    return {
        "pipeline_version": pipeline_version,
        "routing": {"client_id": None},
        "entity_resolution": {
            "decision": routing.DECISION_AUTO_ATTACH,
            "candidates": [],
        },
    }


# --------------------------------------------------------------------------- #
# P1 - auto-attach suppression
# --------------------------------------------------------------------------- #
def test_recovery_tag_joins_the_suppressed_set_without_dropping_the_old_one():
    assert "recovery-v1" in routing.AUTO_ATTACH_SUPPRESSED_PIPELINE_VERSIONS
    assert "v2.3-drive-autocreate" in routing.AUTO_ATTACH_SUPPRESSED_PIPELINE_VERSIONS


@pytest.mark.parametrize(
    ("tag", "suppressed"),
    [
        # guilt: the exact tag and the batch-qualified form
        ("recovery-v1", True),
        ("recovery-v1:cdr-batch-20261103", True),
        # unchanged: the tag that was already suppressed
        ("v2.3-drive-autocreate", True),
        ("v2.3-drive-autocreate:w1-x", True),
        # innocence (guard family #3): a similar tag without the ':' separator,
        # a neighbouring version, a longer prefix, a different case - all pass
        ("recovery-v10", False),
        ("recovery-v1-other", False),
        ("recovery-v2", False),
        ("recovery", False),
        ("xrecovery-v1", False),
        ("RECOVERY-V1", False),
        ("intake-v1", False),
        ("v2.2-m227-folder", False),
        ("", False),
    ],
)
def test_suppression_matrix(tag: str, suppressed: bool):
    assert routing._pipeline_version_suppressed(tag) is suppressed


@pytest.mark.parametrize("tag", ["recovery-v1", "recovery-v1:cdr-batch-20261103"])
async def test_recovery_tag_never_evaluates_an_attach_gate_even_when_armed(monkeypatch, tag):
    async def _must_not_run(*a, **kw):  # pragma: no cover - failure path
        raise AssertionError("attach gate evaluated despite suppression")

    monkeypatch.setattr(auto_attach, "try_auto_attach", _must_not_run)
    monkeypatch.setattr(auto_attach, "try_direct_phone_auto_attach", _must_not_run)
    monkeypatch.setattr(auto_attach, "try_nameid_auto_attach", _must_not_run)
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")
    monkeypatch.setenv("INTAKE_AUTO_ATTACH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_DIRECT_PHONE_AUTO_ATTACH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_NAMEID_AUTO_ATTACH_ENABLED", "true")

    verdict = await routing._try_auto_attach_after_route(
        proposal_id=901,
        proposal=_proposal(tag),
        pool=None,
        sender_phone=None,
        source_context=None,
        effective_status="review_pending",
    )

    assert verdict == {"skipped": "suppressed_pipeline_version", "pipeline_version": tag}


@pytest.mark.parametrize("tag", ["intake-v1", "recovery-v1-other", "recovery-v10"])
async def test_neighbouring_tags_still_reach_the_attach_gate(monkeypatch, tag):
    sentinel = {"committed": False, "skipped": "kill_switch_off"}
    calls: list[str] = []

    async def _gate(*a, **kw):
        calls.append("try_auto_attach")
        return sentinel

    monkeypatch.setattr(auto_attach, "try_auto_attach", _gate)

    verdict = await routing._try_auto_attach_after_route(
        proposal_id=902,
        proposal=_proposal(tag),
        pool=None,
        sender_phone=None,
        source_context=None,
        effective_status="review_pending",
    )

    assert calls == ["try_auto_attach"]
    assert verdict is sentinel


# --------------------------------------------------------------------------- #
# P2 - worker isolation (DB-free: what the claim is told)
# --------------------------------------------------------------------------- #
class _RecordingConn:
    sql: str | None = None
    args: tuple | None = None

    async def fetchrow(self, sql, *args):  # noqa: ANN001, ANN202
        self.sql = sql
        self.args = args
        return None


def test_recovery_tag_is_filter_only():
    assert "recovery-v1" in FILTER_ONLY_PIPELINE_VERSIONS


async def test_default_worker_claim_carries_the_filter_only_tags():
    conn = _RecordingConn()
    worker = IntakeWorker(cast(asyncpg.Pool, object()), config=WorkerConfig())

    await worker._claim_with_inbound(conn)

    assert conn.sql is not None and conn.args is not None
    assert conn.args[4] is None, "default worker has no include-filter"
    assert conn.args[5] == sorted(FILTER_ONLY_PIPELINE_VERSIONS)
    assert "split_part(pipeline_version, ':', 1) = ANY($6::text[])" in conn.sql
    assert "$5::text IS NOT NULL" in conn.sql


async def test_filtered_worker_claim_keeps_its_explicit_filter():
    conn = _RecordingConn()
    config = WorkerConfig(pipeline_version_filter="recovery-v1")
    worker = IntakeWorker(cast(asyncpg.Pool, object()), config=config)

    await worker._claim_with_inbound(conn)

    assert conn.args is not None
    assert conn.args[4] == "recovery-v1"


def test_worker_config_reads_the_recovery_filter_from_env(monkeypatch):
    monkeypatch.setenv("INTAKE_PIPELINE_VERSION_FILTER", "recovery-v1")
    assert WorkerConfig.from_env().pipeline_version_filter == "recovery-v1"

    monkeypatch.setenv("INTAKE_PIPELINE_VERSION_FILTER", "   ")
    assert WorkerConfig.from_env().pipeline_version_filter is None

    monkeypatch.delenv("INTAKE_PIPELINE_VERSION_FILTER")
    assert WorkerConfig.from_env().pipeline_version_filter is None


async def test_worker_start_log_names_the_filter_and_the_filter_only_tags(monkeypatch, caplog):
    """The live observation (Bites): a restarted worker says what it will not claim."""
    monkeypatch.setattr(worker_module, "remap_legacy_statuses", AsyncMock())
    monkeypatch.setattr(worker_module, "reap_expired_review_claims", AsyncMock())
    worker = IntakeWorker(
        cast(asyncpg.Pool, object()),
        config=WorkerConfig(poll_interval_seconds=0.001),
        worker_id="start-log-test",
    )

    async def run_once() -> bool:
        worker.stop()
        return False

    monkeypatch.setattr(worker, "run_once", run_once)
    with caplog.at_level(logging.INFO, logger="zantara.intake.worker"):
        await asyncio.wait_for(worker.run_forever(), timeout=5.0)

    assert (
        "intake worker start-log-test started "
        "(pipeline_version_filter=-, filter-only tags=recovery-v1)"
    ) in caplog.text


# --------------------------------------------------------------------------- #
# P2 - worker isolation (real SQL against the local nuzantara_test book)
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(_DB_URL, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.close()


async def _cleanup(pool: asyncpg.Pool, qids: list[int]) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM intake_stage_metrics WHERE queue_id = ANY($1)", qids)
        rows = await conn.fetch("SELECT id, instance_id FROM intake_queue WHERE id = ANY($1)", qids)
        await conn.execute("DELETE FROM intake_queue WHERE id = ANY($1)", qids)
        await conn.execute(
            """DELETE FROM document_instances di WHERE di.id = ANY($1)
               AND NOT EXISTS (SELECT 1 FROM intake_queue q WHERE q.instance_id = di.id)""",
            list({r["instance_id"] for r in rows}),
        )


async def test_default_worker_skips_recovery_rows_and_a_filtered_worker_claims_them(pool, tmp_path):
    suffix = uuid.uuid4().hex[:8]
    tags = {
        "recovery": "recovery-v1",
        "recovery_batch": f"recovery-v1:b{suffix}",
        "near_miss": f"recovery-v1-other-{suffix}",
        "control": f"pytest-recovery-iso-{suffix}",
    }
    # The isolated rows are enqueued FIRST (lowest ids) and every row is forced
    # to the head of the claim order, so a leaking predicate would hand the
    # default worker a recovery row before the near-miss and control rows.
    ids: dict[str, int] = {}
    for name, tag in tags.items():
        blob = tmp_path / f"{name}.bin"
        blob.write_bytes(f"{name}-{suffix}-{uuid.uuid4()}".encode())
        res = await enqueue(
            pool,
            source="drive",
            source_ref=f"test/recovery-iso/{name}/{uuid.uuid4()}",
            blob_path=str(blob),
            pipeline_version=tag,
        )
        ids[name] = res.queue_id
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE intake_queue
                   SET status = 'validated', next_visible_at = '1970-01-01'::timestamptz
                 WHERE id = ANY($1)
                """,
                list(ids.values()),
            )

        default_worker = IntakeWorker(
            pool, config=WorkerConfig(lease_ttl_seconds=30), worker_id="iso-default"
        )
        async with pool.acquire() as conn:
            first = await default_worker._claim_with_inbound(conn)
            second = await default_worker._claim_with_inbound(conn)
            third = await default_worker._claim_with_inbound(conn)
        assert first is not None and first["id"] == ids["near_miss"]
        assert second is not None and second["id"] == ids["control"]
        isolated = {ids["recovery"], ids["recovery_batch"]}
        assert third is None or third["id"] not in isolated

        dedicated = IntakeWorker(
            pool,
            config=WorkerConfig(lease_ttl_seconds=30, pipeline_version_filter="recovery-v1"),
            worker_id="iso-dedicated",
        )
        batch_dedicated = IntakeWorker(
            pool,
            config=WorkerConfig(
                lease_ttl_seconds=30, pipeline_version_filter=tags["recovery_batch"]
            ),
            worker_id="iso-batch",
        )
        async with pool.acquire() as conn:
            exact = await dedicated._claim_with_inbound(conn)
            batch = await batch_dedicated._claim_with_inbound(conn)
        assert exact is not None and exact["id"] == ids["recovery"]
        assert batch is not None and batch["id"] == ids["recovery_batch"]
    finally:
        await _cleanup(pool, list(ids.values()))
