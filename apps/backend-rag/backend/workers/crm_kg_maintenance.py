"""CRM knowledge-graph maintenance jobs run by the Fly ``drive`` worker.

Replaces the retired admin-API-key cron: the drive worker calls
``run_kg_maintenance`` as a background task after each poll cycle, and each job
runs when its own interval has elapsed. An atomic lease on the job's
``system_settings`` last-run row keeps the two drive machines from running the
same job at once and carries the last-run state so both machines see it.
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

# Deadline for every housekeeping step (claim, result write, acquire, release, pool
# creation, pool close). A blocked database step must never hang maintenance forever.
_STEP_TIMEOUT_SECONDS = 30

# Atomic lease claim on the last-run row. Postgres row locking serialises concurrent
# claims, and under READ COMMITTED the loser re-evaluates the WHERE on the latest row
# version, so exactly one machine gets a row back. It needs no session affinity, so it
# works through PgBouncer transaction pooling, and there is nothing to release. The
# per-job timeout is far below the minimum interval, so runs never overlap. A missing
# or unparseable value counts as due.
_CLAIM_SQL = r"""
INSERT INTO system_settings (key, value, updated_at)
VALUES ($1, $2, NOW())
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value, updated_at = NOW()
WHERE CASE
    WHEN system_settings.value ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+00:00$'
        THEN system_settings.value::timestamptz <= $3::timestamptz
    ELSE TRUE
END
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
            maximum=3600,
        ),
    )


def _total_budget_seconds(config: KgMaintenanceConfig) -> float:
    # Both jobs at full timeout, plus the bounded housekeeping steps around each.
    return 2 * config.timeout_seconds + 12 * _STEP_TIMEOUT_SECONDS


def _build_jobs(config: KgMaintenanceConfig) -> list[_Job]:
    from backend.services.knowledge_graph.garbage_collector import garbage_collect
    from backend.services.knowledge_graph.mediated_edges_builder import build_mediated_edges

    return [
        _Job(BUILD_MEDIATED_JOB, config.build_mediated_interval_seconds, build_mediated_edges),
        _Job(GARBAGE_COLLECT_JOB, config.garbage_collect_interval_seconds, garbage_collect),
    ]


async def _claim_run(pool: asyncpg.Pool, job: _Job, now: datetime) -> bool:
    """Atomically claim this run; False when it ran recently or another machine claimed it."""
    conn = await pool.acquire(timeout=_STEP_TIMEOUT_SECONDS)
    try:
        claimed = await conn.fetchval(
            _CLAIM_SQL,
            f"{job.name}_last_run_at",
            now.isoformat(),
            now - timedelta(seconds=job.interval_seconds),
            timeout=_STEP_TIMEOUT_SECONDS,
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


async def run_due_jobs(
    pool: asyncpg.Pool,
    config: KgMaintenanceConfig,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Run each KG maintenance job whose interval has elapsed."""
    current = now or datetime.now(UTC)
    results: dict[str, dict[str, Any]] = {}
    for job in _build_jobs(config):
        try:
            if not await _claim_run(pool, job, current):
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


async def _close_pool(pool: asyncpg.Pool) -> None:
    try:
        await asyncio.wait_for(pool.close(), timeout=_STEP_TIMEOUT_SECONDS)
    except BaseException as exc:
        # terminate() closes the server sessions, so Postgres frees anything a stuck
        # connection still holds.
        try:
            pool.terminate()
        except Exception:
            logger.warning("CRM KG maintenance: pool terminate failed", exc_info=True)
        if not isinstance(exc, Exception):
            raise
        logger.warning("CRM KG maintenance: pool close failed (%r); terminated", exc)


async def run_kg_maintenance(database_url: str, config: KgMaintenanceConfig) -> dict[str, Any]:
    """Run due jobs on a short-lived pool. Never raises into the caller.

    The drive worker runs this as a background task, so it never blocks polling.
    """
    if not config.enabled:
        return {"status": "disabled"}
    try:
        # Two connections: the job acquires its own while housekeeping may hold one.
        try:
            pool = await asyncio.wait_for(
                create_database_pool(
                    dsn=database_url,
                    normalize_dsn=False,
                    policy=DatabasePoolPolicy(
                        min_size=1,
                        max_size=2,
                        command_timeout=30,
                        max_inactive_connection_lifetime=300,
                        statement_timeout_seconds=30,
                    ),
                ),
                timeout=_STEP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error("CRM KG maintenance: pool creation exceeded %ss", _STEP_TIMEOUT_SECONDS)
            return {"status": "timeout", "timeout_seconds": _STEP_TIMEOUT_SECONDS}
        budget = _total_budget_seconds(config)
        try:
            try:
                jobs = await asyncio.wait_for(run_due_jobs(pool, config), timeout=budget)
            except TimeoutError:
                logger.error("CRM KG maintenance exceeded its %ss budget", budget)
                return {"status": "timeout", "timeout_seconds": budget}
        finally:
            await _close_pool(pool)
        logger.info("CRM KG maintenance cycle finished: %s", jobs)
        return {"status": "ran", "jobs": jobs}
    except Exception as exc:
        logger.exception("CRM KG maintenance failed")
        return {"status": "error", "error": str(exc)}
