"""Completes the practices whose first proof an older machine accepted without completing them (P10).

Keyset scan, one transaction per page under a transaction-scoped advisory lock (safe behind
PgBouncer), one savepoint per candidate; a locked or refused practice is busy until the next cycle.
"""

import asyncio
import random
from typing import Any

import asyncpg
from fastapi import BackgroundTasks, HTTPException

from backend.app.utils.logging_utils import get_logger
from backend.services.crm import practice_delivery as delivery
from backend.services.crm.practice_state_machine import LEGACY_STATE_MAP

logger = get_logger(__name__)

SWEEP_ACTOR = "system:first-proof-sweep"
_LOCK_KEY = 7_203_011_028  # advisory key of the sweep
_PAGE = 200
_OPEN_STATES = ["on_process", *(k for k, v in LEGACY_STATE_MAP.items() if v == "on_process")]
_cursor = 0


async def _lock_practice(conn: Any, practice_id: int) -> dict | None:
    row = await conn.fetchrow(
        """SELECT p.id, p.client_id, p.family_member_id, p.assigned_to, p.client_visible,
                  p.status, p.payment_status, pt.code, pt.name, pt.category
           FROM practices p JOIN practice_types pt ON pt.id = p.practice_type_id
           WHERE p.id = $1 AND (p.family_member_id IS NULL OR EXISTS (
               SELECT 1 FROM client_family_members f WHERE f.id=p.family_member_id AND f.client_id=p.client_id))
           FOR UPDATE OF p""",
        practice_id,
    )
    return dict(row) if row else None


async def _complete_one(conn: Any, practice_id: int) -> dict | None:
    """The practice completed by the sweep, or None when it is no longer a candidate."""
    p = await _lock_practice(conn, practice_id)
    if p is None:
        return None
    delivery.require_open(p)
    await delivery.lock_slot_documents(conn, practice_id)
    view = await delivery.state(conn, p)
    if delivery.proof_kind(view) is None:
        return None
    await delivery.finish(conn, p, view, SWEEP_ACTOR)
    return p


async def sweep_open_proofs(pool: Any, pages: int = 50) -> dict:
    """One tick: up to `pages` pages from the process-local cursor; a short page ends a cycle."""
    global _cursor
    examined, completed, busy = 0, [], []
    done = 0
    while done < pages:
        done += 1
        notices = BackgroundTasks()
        async with pool.acquire() as conn, conn.transaction():
            if not await conn.fetchval("SELECT pg_try_advisory_xact_lock($1)", _LOCK_KEY):
                break  # another machine is sweeping
            await conn.execute("SET LOCAL lock_timeout = '5s'")
            ids = [
                r["id"]
                for r in await conn.fetch(
                    """SELECT p.id FROM practices p WHERE p.id > $1 AND p.payment_status='paid'
                       AND p.status = ANY($2::text[]) AND EXISTS (
                         SELECT 1 FROM practice_delivery_slots s WHERE s.practice_id=p.id
                         AND (s.document_id IS NOT NULL OR s.waiver_reason IS NOT NULL))
                       ORDER BY p.id LIMIT $3""",
                    _cursor,
                    _OPEN_STATES,
                    _PAGE,
                )
            ]
            for practice_id in ids:
                examined += 1
                try:
                    async with conn.transaction():  # a savepoint per candidate
                        p = await _complete_one(conn, practice_id)
                except (HTTPException, asyncpg.PostgresError):
                    busy.append(practice_id)
                    continue
                if p is not None:
                    completed.append(practice_id)
                    delivery.queue_completed(pool, p, SWEEP_ACTOR, notices)
        _cursor = ids[-1] if len(ids) == _PAGE else 0
        for task in notices.tasks:  # after the page commits; a failure never undoes a completion
            try:
                await task()
            except Exception:
                logger.warning("first-proof sweep notice failed", exc_info=True)
        if _cursor == 0:
            break
    logger.info(
        "first_proof_sweep pages=%s examined=%s completed=%s busy=%s",
        done,
        examined,
        completed,
        busy,
    )
    return {"pages": done, "examined": examined, "completed": completed, "busy": busy}


async def run_first_proof_sweep_scheduler(pool: Any) -> None:
    """First run 60-120 s after boot, then every 600 s +- 60 s; never raises."""
    await asyncio.sleep(random.uniform(60, 120))
    while True:
        try:
            await sweep_open_proofs(pool)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("first-proof sweep tick failed: %s", type(exc).__name__)
        await asyncio.sleep(600 + random.uniform(-60, 60))
