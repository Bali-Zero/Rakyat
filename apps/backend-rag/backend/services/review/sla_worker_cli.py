"""Cron entrypoint for SLAWorker — every 30 min on Pro.

Usage:
    PYTHONPATH=. python -m backend.services.review.sla_worker_cli

Env:
    DATABASE_URL             (required)
    Telegram is retired. Expiry runs without an alert adapter or chat id.

Exit codes:
    0  sweep completed (any combination of soft/repeat/expire counts)
    1  configuration error or unrecoverable pool failure
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import asyncpg

from backend.services.review.sla_worker import SLAWorker
from backend.services.war_room.repository import WarRoomRepository

logger = logging.getLogger("review.sla.cli")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


async def run() -> int:
    _configure_logging()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL not set")
        return 1

    try:
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=2,
            command_timeout=60,
        )
    except Exception as exc:
        logger.error("pool init failed: %s", exc, exc_info=True)
        return 1

    try:
        repo = WarRoomRepository(db_pool=pool)
        worker = SLAWorker(repo=repo)
        result = await worker.sweep_once()
        sys.stdout.write(
            json.dumps(
                {
                    "swept_count": result.swept_count,
                    "soft_alerts_sent": result.soft_alerts_sent,
                    "repeat_alerts_sent": result.repeat_alerts_sent,
                    "expired_count": result.expired_count,
                    "errors_count": len(result.errors),
                },
                default=str,
            )
            + "\n"
        )
        return 0
    finally:
        await pool.close()


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
