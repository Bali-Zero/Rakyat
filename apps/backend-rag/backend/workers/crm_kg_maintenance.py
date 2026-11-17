"""CRM knowledge-graph maintenance jobs run by the Fly ``drive`` worker.

Replaces the retired admin-API-key cron: the drive worker calls
``run_kg_maintenance`` after each poll cycle, and each job runs when its own
interval has elapsed. A Postgres advisory lock keeps the two drive machines
from running the same job at once, and ``system_settings`` carries the last-run
state so both machines see it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

BUILD_MEDIATED_JOB = "crm_kg_build_mediated"
GARBAGE_COLLECT_JOB = "crm_kg_garbage_collect"
BUILD_MEDIATED_LOCK_ID = 614_238_904_712
GARBAGE_COLLECT_LOCK_ID = 614_238_904_713


@dataclass(frozen=True)
class KgMaintenanceConfig:
    enabled: bool
    build_mediated_interval_seconds: int
    garbage_collect_interval_seconds: int
    timeout_seconds: int


@dataclass(frozen=True)
class _Job:
    name: str
    lock_id: int
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


async def _upsert_setting(conn: asyncpg.Connection, key: str, value: str) -> None:
    await conn.execute(
        """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (key) DO UPDATE
        SET value = EXCLUDED.value, updated_at = NOW()
        """,
        key,
        value,
    )


def _build_jobs(config: KgMaintenanceConfig) -> list[_Job]:
    from backend.services.knowledge_graph.garbage_collector import garbage_collect
    from backend.services.knowledge_graph.mediated_edges_builder import build_mediated_edges

    return [
        _Job(
            BUILD_MEDIATED_JOB,
            BUILD_MEDIATED_LOCK_ID,
            config.build_mediated_interval_seconds,
            build_mediated_edges,
        ),
        _Job(
            GARBAGE_COLLECT_JOB,
            GARBAGE_COLLECT_LOCK_ID,
            config.garbage_collect_interval_seconds,
            garbage_collect,
        ),
    ]


def _is_due(raw_last_run: Any, interval_seconds: int, now: datetime) -> bool:
    if not raw_last_run:
        return True
    try:
        last_run = datetime.fromisoformat(str(raw_last_run))
    except ValueError:
        return True
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=UTC)
    return (now - last_run).total_seconds() >= interval_seconds


async def _run_job_locked(
    pool: asyncpg.Pool,
    conn: asyncpg.Connection,
    job: _Job,
    config: KgMaintenanceConfig,
    now: datetime,
) -> dict[str, Any]:
    last_run_key = f"{job.name}_last_run_at"
    raw_last_run = await conn.fetchval(
        "SELECT value FROM system_settings WHERE key = $1", last_run_key
    )
    if not _is_due(raw_last_run, job.interval_seconds, now):
        return {"status": "not_due", "last_run_at": raw_last_run}

    # Stamp before running so a crash or timeout does not retry every poll cycle.
    await _upsert_setting(conn, last_run_key, now.isoformat())
    try:
        job_result = await asyncio.wait_for(job.run(pool), timeout=config.timeout_seconds)
        result: dict[str, Any] = {
            "status": "ok" if job_result.get("ok", True) else "error",
            "result": job_result,
        }
    except TimeoutError:
        logger.error("CRM KG job %s exceeded %ss", job.name, config.timeout_seconds)
        result = {"status": "timeout", "timeout_seconds": config.timeout_seconds}
    except Exception as exc:
        logger.exception("CRM KG job %s failed", job.name)
        result = {"status": "error", "error": str(exc)}

    await _upsert_setting(
        conn,
        f"{job.name}_last_result",
        json.dumps(result, sort_keys=True, default=str)[:1000],
    )
    return result


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
            lock_conn = await pool.acquire()
        except Exception as exc:
            logger.exception("CRM KG job %s: cannot acquire connection", job.name)
            results[job.name] = {"status": "error", "error": str(exc)}
            continue
        lock_acquired = False
        try:
            lock_acquired = bool(
                await lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", job.lock_id)
            )
            if not lock_acquired:
                results[job.name] = {"status": "locked"}
                continue
            results[job.name] = await _run_job_locked(pool, lock_conn, job, config, current)
        except Exception as exc:
            logger.exception("CRM KG job %s: maintenance step failed", job.name)
            results[job.name] = {"status": "error", "error": str(exc)}
        finally:
            if lock_acquired:
                try:
                    await lock_conn.execute("SELECT pg_advisory_unlock($1)", job.lock_id)
                except Exception:
                    logger.warning(
                        "CRM KG job %s: failed to release advisory lock", job.name, exc_info=True
                    )
            try:
                await pool.release(lock_conn)
            except Exception:
                logger.warning(
                    "CRM KG job %s: failed to release connection", job.name, exc_info=True
                )
    return results


async def run_kg_maintenance(database_url: str, config: KgMaintenanceConfig) -> dict[str, Any]:
    """Run due jobs on a short-lived pool. Never raises into the poll loop."""
    if not config.enabled:
        return {"status": "disabled"}
    try:
        # Two connections: the lock connection stays held while the job acquires its own.
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            jobs = await run_due_jobs(pool, config)
        finally:
            await pool.close()
        logger.info("CRM KG maintenance cycle finished: %s", jobs)
        return {"status": "ran", "jobs": jobs}
    except Exception as exc:
        logger.exception("CRM KG maintenance failed")
        return {"status": "error", "error": str(exc)}
