"""CRM knowledge-graph maintenance jobs run by the Fly ``drive`` worker.

Replaces the retired admin-API-key cron. The drive worker starts
``run_maintenance_loop`` once as its own task; the loop checks every few minutes
and runs each job when its interval has elapsed. A compare-and-swap on the job's
``system_settings`` last-run row decides which drive machine runs a job, and
carries the last-run state so both machines see it.

Both jobs are idempotent (the garbage collector is safe on every tick, the
mediated-edge builder upserts with ON CONFLICT DO UPDATE), so a job that outlives
its interval and overlaps a run on the other machine costs duplicate work, not
wrong data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from backend.app.core.database import DatabasePoolPolicy, create_database_pool

logger = logging.getLogger(__name__)

BUILD_MEDIATED_JOB = "crm_kg_build_mediated"
GARBAGE_COLLECT_JOB = "crm_kg_garbage_collect"

# Deadline passed to every housekeeping acquire, release and statement.
_STEP_TIMEOUT_SECONDS = 30

_READ_SQL = "SELECT value FROM system_settings WHERE key = $1"

# Claim = compare-and-swap on the exact stored text. Both statements are atomic under
# Postgres row locking (READ COMMITTED re-checks the WHERE on the latest row version),
# so of several machines racing for the same stamp exactly one gets a row back. They
# need no session affinity, so they work through PgBouncer transaction pooling, and
# there is nothing to release afterwards.
_CLAIM_INSERT_SQL = """
INSERT INTO system_settings (key, value, updated_at)
VALUES ($1, $2, NOW())
ON CONFLICT (key) DO NOTHING
RETURNING key
"""

_CLAIM_UPDATE_SQL = """
UPDATE system_settings SET value = $2, updated_at = NOW()
WHERE key = $1 AND value = $3
RETURNING key
"""

_UPSERT_SQL = """
INSERT INTO system_settings (key, value, updated_at)
VALUES ($1, $2, NOW())
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value, updated_at = NOW()
"""


@dataclass(frozen=True)
class KgMaintenanceConfig:
    enabled: bool
    build_mediated_interval_seconds: int
    garbage_collect_interval_seconds: int
    timeout_seconds: int
    check_seconds: int = 300


@dataclass(frozen=True)
class _Job:
    name: str
    interval_seconds: int
    run: Callable[[asyncpg.Pool], Awaitable[dict[str, Any]]]


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw_value, default)
        return default
    return max(minimum, min(value, maximum))


def load_kg_maintenance_config() -> KgMaintenanceConfig:
    # On by default: these jobs ran on a schedule until the cron lost its key.
    # CRM_KG_MAINTENANCE_ENABLED=false is the kill switch.
    # Intervals are at least 3600 s and the job timeout at most 1800 s, so an interval
    # is always at least twice the timeout.
    return KgMaintenanceConfig(
        enabled=_env_bool("CRM_KG_MAINTENANCE_ENABLED", default=True),
        build_mediated_interval_seconds=_env_int(
            "CRM_KG_BUILD_MEDIATED_INTERVAL_SECONDS",
            default=21_600,
            minimum=3600,
            maximum=604_800,
        ),
        garbage_collect_interval_seconds=_env_int(
            "CRM_KG_GARBAGE_COLLECT_INTERVAL_SECONDS",
            default=86_400,
            minimum=3600,
            maximum=604_800,
        ),
        timeout_seconds=_env_int(
            "CRM_KG_MAINTENANCE_TIMEOUT_SECONDS",
            default=900,
            minimum=60,
            maximum=1800,
        ),
        check_seconds=_env_int(
            "CRM_KG_MAINTENANCE_CHECK_SECONDS",
            default=300,
            minimum=60,
            maximum=3600,
        ),
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _build_jobs(config: KgMaintenanceConfig) -> list[_Job]:
    from backend.services.knowledge_graph.garbage_collector import garbage_collect
    from backend.services.knowledge_graph.mediated_edges_builder import build_mediated_edges

    return [
        _Job(BUILD_MEDIATED_JOB, config.build_mediated_interval_seconds, build_mediated_edges),
        _Job(GARBAGE_COLLECT_JOB, config.garbage_collect_interval_seconds, garbage_collect),
    ]


def _is_due(raw_last_run: str | None, interval_seconds: int, now: datetime) -> bool:
    """Due when never run, unparseable or naive (treated as unparseable), or old enough."""
    if raw_last_run is None:
        return True
    try:
        last_run = datetime.fromisoformat(raw_last_run)
    except ValueError:
        return True
    if last_run.tzinfo is None:
        return True
    return now - last_run >= timedelta(seconds=interval_seconds)


async def _claim_run(pool: asyncpg.Pool, job: _Job) -> bool:
    """Claim this run; False when it ran recently or another machine got it first."""
    key = f"{job.name}_last_run_at"
    conn = await pool.acquire(timeout=_STEP_TIMEOUT_SECONDS)
    try:
        # Fresh clock at claim time: the previous job may have run for a long time.
        now = _utcnow()
        raw = await conn.fetchval(_READ_SQL, key, timeout=_STEP_TIMEOUT_SECONDS)
        if not _is_due(raw, job.interval_seconds, now):
            return False
        if raw is None:
            claimed = await conn.fetchval(
                _CLAIM_INSERT_SQL, key, now.isoformat(), timeout=_STEP_TIMEOUT_SECONDS
            )
        else:
            claimed = await conn.fetchval(
                _CLAIM_UPDATE_SQL, key, now.isoformat(), raw, timeout=_STEP_TIMEOUT_SECONDS
            )
        return claimed is not None
    finally:
        try:
            await pool.release(conn, timeout=_STEP_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("CRM KG job %s: failed to release connection", job.name, exc_info=True)


async def _record_result(pool: asyncpg.Pool, job: _Job, result: dict[str, Any]) -> None:
    try:
        conn = await pool.acquire(timeout=_STEP_TIMEOUT_SECONDS)
        try:
            await conn.execute(
                _UPSERT_SQL,
                f"{job.name}_last_result",
                json.dumps(result, sort_keys=True, default=str)[:1000],
                timeout=_STEP_TIMEOUT_SECONDS,
            )
        finally:
            try:
                await pool.release(conn, timeout=_STEP_TIMEOUT_SECONDS)
            except Exception:
                logger.warning(
                    "CRM KG job %s: failed to release connection", job.name, exc_info=True
                )
    except Exception:
        # The job already ran; a lost result row must not turn it into an error.
        logger.warning("CRM KG job %s: failed to record result", job.name, exc_info=True)


async def _run_job(pool: asyncpg.Pool, job: _Job, config: KgMaintenanceConfig) -> dict[str, Any]:
    try:
        job_result = await asyncio.wait_for(job.run(pool), timeout=config.timeout_seconds)
        return {
            "status": "ok" if job_result.get("ok", True) else "error",
            "result": job_result,
        }
    except TimeoutError:
        logger.error("CRM KG job %s exceeded %ss", job.name, config.timeout_seconds)
        return {"status": "timeout", "timeout_seconds": config.timeout_seconds}
    except Exception as exc:
        logger.exception("CRM KG job %s failed", job.name)
        return {"status": "error", "error": str(exc) or type(exc).__name__}


async def run_due_jobs(pool: asyncpg.Pool, config: KgMaintenanceConfig) -> dict[str, Any]:
    """Run each KG maintenance job whose interval has elapsed and that this machine claims."""
    results: dict[str, Any] = {}
    for job in _build_jobs(config):
        try:
            if not await _claim_run(pool, job):
                results[job.name] = {"status": "not_due"}
                continue
        except Exception as exc:
            logger.exception("CRM KG job %s: claim failed", job.name)
            results[job.name] = {"status": "error", "error": str(exc) or type(exc).__name__}
            continue
        result = await _run_job(pool, job, config)
        await _record_result(pool, job, result)
        results[job.name] = result
    return results


async def _wait_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; True when a stop was requested."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def _close_pool(pool: asyncpg.Pool) -> None:
    try:
        await pool.close()
    except BaseException as exc:
        try:
            pool.terminate()
        except Exception:
            logger.warning("CRM KG maintenance: pool terminate failed", exc_info=True)
        if not isinstance(exc, Exception):
            raise
        logger.warning("CRM KG maintenance: pool close failed (%r); terminated", exc)


async def run_maintenance_loop(
    database_url: str, config: KgMaintenanceConfig, stop_event: asyncio.Event
) -> None:
    """Long-lived maintenance loop with one pool. Runs in its own task, so a stuck
    maintenance call stalls maintenance only, until the worker restarts."""
    if not config.enabled:
        return
    pool: asyncpg.Pool | None = None
    while pool is None and not stop_event.is_set():
        try:
            # Claim, job and result write use the pool one after another; the second
            # connection is a spare while a cancelled job's connection is cleaned up.
            pool = await create_database_pool(
                dsn=database_url,
                normalize_dsn=False,
                policy=DatabasePoolPolicy(
                    min_size=1,
                    max_size=2,
                    command_timeout=30,
                    max_inactive_connection_lifetime=300,
                    statement_timeout_seconds=30,
                ),
            )
        except Exception:
            logger.exception("CRM KG maintenance: pool creation failed; retrying")
            if await _wait_or_stop(stop_event, config.check_seconds):
                return
    if pool is None:
        return
    try:
        while not stop_event.is_set():
            try:
                results = await run_due_jobs(pool, config)
                logger.info("CRM KG maintenance check finished: %s", results)
            except Exception:
                logger.exception("CRM KG maintenance check failed")
            if await _wait_or_stop(stop_event, config.check_seconds):
                break
    finally:
        await _close_pool(pool)
