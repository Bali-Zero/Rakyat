"""Bonus reform C, PR3c: migration 400 closes as paid the bonuses paid outside the system (real Postgres).

Same schema and pools as test_bonus_run_payment_pg (real 068 triggers, migrations 383-399). Synthetic ids
and amounts only: 603 rows of 186000 (and three of 270000), the production measurement of 2026-11-28.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from backend.app.services.hr import hr_service
from backend.db import migration_base as mb
from backend.db.migration_base import (
    BaseMigration,
    MigrationError,
    extract_dependencies,
    split_migration_sql,
)
from backend.db.migration_manager import MigrationManager
from backend.tests.migrations.test_hr_bonus_payroll_locks import (  # noqa: F401
    SCHEMA,
    Env,
    _dsn,
    env,
)
from backend.tests.migrations.test_migration_399_bonus_run_payment_pg import (
    BOUND,
    _no_stray_advisory_locks,
    _refused_by_a_lock_that_does_not_wait,
    _schema_dsn,
    _seed_393_ledger,
)

pytestmark = pytest.mark.integration

MIG_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations_v2"
FILE_400 = MIG_DIR / "400_close_pre_november_bonuses_paid.sql"
MARKER = (
    "Closed as paid by migration 400: paid outside the system before November 2026 "
    "(owner 2026-11-28)"
)
COUNT, TOTAL = 603, 112_410_000
# the boundary of the cut in Bali time: 23:30 on Oct 31 is closed, 00:30 on Nov 1 is not (UTC would invert)
LAST_OCT = datetime(2026, 10, 31, 15, 30, tzinfo=timezone.utc)
FIRST_NOV = datetime(2026, 10, 31, 16, 30, tzinfo=timezone.utc)
SEP = datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)

O3_PREDICATE = (
    "status='approved' AND payroll_period_id IS NULL AND bonus_run_id IS NULL AND deferred_from IS NULL "
    "AND (awarded_at AT TIME ZONE 'Asia/Makassar') < TIMESTAMP '2026-11-01'"
)


def _parts() -> tuple[str, str]:
    forward, rollback = split_migration_sql(FILE_400.read_text(encoding="utf-8"))
    assert rollback
    return forward, rollback


async def _seed(env: Env, n: int = COUNT) -> list[int]:
    """n approved, unreserved, undeferred rows awarded Jan-Oct 2026 (the first one on the Oct 31 boundary)."""
    emp_b = await env.pool.fetchval("SELECT id FROM hr_employees WHERE team_member_id='tm-b'")
    rows = await env.pool.fetch(
        """INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at,
                                        source, reason)
           SELECT CASE WHEN g % 2 = 0 THEN $1::int ELSE $2::int END, 'manual',
                  CASE WHEN g <= 3 THEN 270000 ELSE 186000 END, 'approved',
                  CASE WHEN g = 1 THEN $3::timestamptz
                       ELSE make_timestamptz(2026, 1 + (g % 10), 10, 4, 0, 0, 'UTC') END,
                  'manual', 'fixture'
           FROM generate_series(1, $4::int) g ORDER BY g RETURNING id""",
        env.emp_id,
        emp_b,
        LAST_OCT,
        n,
    )
    return [r["id"] for r in rows]


async def _decoy(env: Env, status: str, awarded: datetime, **extra) -> int:
    cols = {"payroll_period_id": None, "bonus_run_id": None, "deferred_from": None, **extra}
    return await env.pool.fetchval(
        "INSERT INTO hr_bonus_ledger (employee_id, practice_type_code, amount_idr, status, awarded_at, "
        "source, reason, payroll_period_id, bonus_run_id, deferred_from) "
        "VALUES ($1,'manual',100000,$2,$3,'manual','why',$4,$5,$6) RETURNING id",
        env.emp_id,
        status,
        awarded,
        cols["payroll_period_id"],
        cols["bonus_run_id"],
        cols["deferred_from"],
    )


async def _decoys(env: Env) -> dict[str, int]:
    """Every shape that is NOT in the set, one row each."""
    period = (await env.svc.calculate_payroll(9, 2026, env.admin))["period_id"]
    await env.pool.execute(
        "INSERT INTO hr_bonus_runs(bonus_month, status) VALUES ('2026-11-01','approved')"
    )
    return {
        "pending": await _decoy(env, "pending", SEP),
        "on_period": await _decoy(env, "approved", SEP, payroll_period_id=period),
        "deferred": await _decoy(env, "approved", SEP, deferred_from=date(2026, 9, 1)),
        "on_run": await _decoy(env, "approved", OCT, bonus_run_id=date(2026, 11, 1)),
        "november": await _decoy(env, "approved", FIRST_NOV),
        "rejected": await _decoy(env, "rejected", SEP),
        "paid_outside": await _decoy(env, "paid", SEP),  # paid by hand, no marker, no period
    }


async def _snap(env: Env, ids, *, drop=()) -> list[dict]:
    """Rows as jsonb, minus the columns named in `drop`: byte-equality of everything else."""
    rows = await env.pool.fetch(
        "SELECT to_jsonb(b) AS r FROM hr_bonus_ledger b WHERE id = ANY($1::bigint[]) ORDER BY id",
        list(ids),
    )
    return [{k: v for k, v in json.loads(r["r"]).items() if k not in drop} for r in rows]


async def _forward(env: Env, sql: str | None = None) -> list[str]:
    """Run 400's forward on one connection and return its NOTICE messages."""
    notices: list[str] = []
    async with env.pool.acquire() as conn:
        conn.add_log_listener(lambda _c, msg: notices.append(msg.message))
        try:
            await conn.execute(sql or _parts()[0])
        finally:
            conn.remove_log_listener(notices.append)
    return notices


async def _marked(env: Env) -> tuple[int, int]:
    row = await env.pool.fetchrow(
        "SELECT count(*) AS n, COALESCE(sum(amount_idr),0)::bigint AS s FROM hr_bonus_ledger "
        "WHERE notes=$1 AND status='paid'",
        MARKER,
    )
    return row["n"], row["s"]


async def _o3(env: Env) -> int:
    return await env.pool.fetchval(f"SELECT count(*) FROM hr_bonus_ledger WHERE {O3_PREDICATE}")


@contextlib.asynccontextmanager
async def _hold(pool: asyncpg.Pool, key: str, *, exclusive: bool = True):
    """An open transaction holding an advisory key, as an in-flight HR writer would (T2, T3).

    Every await is bounded, and the connection goes back to the pool even when taking the key fails."""
    conn = await asyncio.wait_for(pool.acquire(), BOUND)
    tx = conn.transaction()
    try:
        await asyncio.wait_for(tx.start(), BOUND)
        fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
        await asyncio.wait_for(conn.execute(f"SELECT {fn}(hashtext($1))", key), BOUND)
        yield
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tx.rollback(), BOUND)
        await asyncio.wait_for(pool.release(conn), BOUND)


# ─── 1: exact ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_1_exactly_the_603_rows_are_closed_and_every_decoy_is_byte_equal(env: Env) -> None:
    ids = await _seed(env)
    decoys = await _decoys(env)
    assert (await _o3(env)) == COUNT
    assert (
        await env.pool.fetchval("SELECT sum(amount_idr) FROM hr_bonus_ledger WHERE " + O3_PREDICATE)
        == TOTAL
    )
    before_decoys = await _snap(env, decoys.values())
    before_rows = await _snap(env, ids, drop=("status", "notes", "updated_at"))
    notices = await _forward(env)
    assert any("603 rows closed as paid, 112410000 IDR" in n for n in notices), notices
    assert await _marked(env) == (COUNT, TOTAL)
    assert await _o3(env) == 0
    assert (
        await env.pool.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE status='paid'")
        == COUNT + 1
    )  # + the decoy that was paid by hand
    # nothing but status, notes and updated_at moved on the closed rows
    assert await _snap(env, ids, drop=("status", "notes", "updated_at")) == before_rows
    assert await _snap(env, decoys.values()) == before_decoys
    # the Oct 31 23:30 WITA row is in, the Nov 1 00:30 WITA row is not
    assert (
        await env.pool.fetchval("SELECT status FROM hr_bonus_ledger WHERE id=$1", ids[0])
    ) == "paid"
    assert (
        await env.pool.fetchval(
            "SELECT status FROM hr_bonus_ledger WHERE id=$1", decoys["november"]
        )
    ) == "approved"


# ─── 2: a differing fact gives a NOTICE and changes nothing ────────────────────────────────────


async def _nothing_closed(env: Env, notices: list[str]) -> None:
    assert any("nothing closed" in n for n in notices), notices
    assert await _marked(env) == (0, 0)
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE status='paid' OR notes IS NOT NULL"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_2a_a_604th_candidate_is_a_notice_and_changes_nothing(env: Env) -> None:
    await _seed(env, COUNT + 1)
    await _nothing_closed(env, await _forward(env))


@pytest.mark.asyncio
async def test_2a2_a_604th_candidate_of_amount_zero_changes_the_count_alone(env: Env) -> None:
    await _seed(env)
    extra = await _decoy(env, "approved", SEP)
    await env.pool.execute("UPDATE hr_bonus_ledger SET amount_idr=0 WHERE id=$1", extra)
    assert (
        await env.pool.fetchval("SELECT sum(amount_idr) FROM hr_bonus_ledger WHERE " + O3_PREDICATE)
        == TOTAL
    )  # the sum matches: only the count differs
    await _nothing_closed(env, await _forward(env))


@pytest.mark.asyncio
async def test_2b_a_sum_off_by_one_is_a_notice_and_changes_nothing(env: Env) -> None:
    ids = await _seed(env)
    await env.pool.execute(
        "UPDATE hr_bonus_ledger SET amount_idr = amount_idr + 1 WHERE id=$1", ids[10]
    )
    await _nothing_closed(env, await _forward(env))


@pytest.mark.asyncio
async def test_2c_a_603rd_row_missing_is_a_notice_and_changes_nothing(env: Env) -> None:
    await _seed(env, COUNT - 1)
    await _nothing_closed(env, await _forward(env))


@pytest.mark.asyncio
async def test_2c2_one_row_fewer_with_the_same_sum_changes_the_count_alone(env: Env) -> None:
    ids = await _seed(env)
    await env.pool.execute("DELETE FROM hr_bonus_ledger WHERE id=$1", ids[20])
    await env.pool.execute(
        "UPDATE hr_bonus_ledger SET amount_idr=amount_idr+186000 WHERE id=$1", ids[21]
    )
    assert (
        await env.pool.fetchval("SELECT sum(amount_idr) FROM hr_bonus_ledger WHERE " + O3_PREDICATE)
        == TOTAL
    )  # the sum matches: only the count differs
    await _nothing_closed(env, await _forward(env))


@pytest.mark.asyncio
async def test_2d_an_id_above_644_is_a_notice_and_changes_nothing(env: Env) -> None:
    ids = await _seed(env)
    await env.pool.execute("UPDATE hr_bonus_ledger SET id = 700 WHERE id=$1", ids[-1])
    assert await _o3(env) == COUNT  # count and sum still match: only the id differs
    await _nothing_closed(env, await _forward(env))


@pytest.mark.asyncio
async def test_2e_a_candidate_with_notes_is_a_notice_and_changes_nothing(env: Env) -> None:
    ids = await _seed(env)
    await env.pool.execute("UPDATE hr_bonus_ledger SET notes='hand note' WHERE id=$1", ids[5])
    notices = await _forward(env)
    assert any("nothing closed" in n for n in notices), notices
    assert await _marked(env) == (0, 0)
    assert await env.pool.fetchval("SELECT count(*) FROM hr_bonus_ledger WHERE status='paid'") == 0


# ─── 3: idempotent ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_3_a_second_forward_is_a_notice_and_changes_nothing(env: Env) -> None:
    ids = await _seed(env)
    await _decoys(env)
    await _forward(env)
    after_first = await _snap(env, ids)
    notices = await _forward(env)
    assert any("already closed as paid" in n for n in notices), notices
    assert await _marked(env) == (COUNT, TOTAL)
    assert await _snap(env, ids) == after_first  # updated_at included


# ─── 4: rollback ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_4a_the_rollback_restores_only_the_marked_rows_that_are_still_paid_unreserved(
    env: Env,
) -> None:
    ids = await _seed(env)
    decoys = await _decoys(env)
    original = await _snap(env, ids, drop=("updated_at",))
    await _forward(env)
    # one marked row was paid by a salary period since; a salary period for it is never undone here
    period = await env.pool.fetchval("SELECT id FROM hr_payroll_periods LIMIT 1")
    await env.pool.execute(
        "UPDATE hr_bonus_ledger SET payroll_period_id=$2 WHERE id=$1", ids[7], period
    )
    # one marked row was paid by a run since, and one is no longer paid: the rollback leaves both alone
    await env.pool.execute(
        "UPDATE hr_bonus_ledger SET bonus_run_id='2026-11-01' WHERE id=$1", ids[8]
    )
    await env.pool.execute("UPDATE hr_bonus_ledger SET status='approved' WHERE id=$1", ids[9])
    moved = [ids[7], ids[8], ids[9]]
    before_moved = await _snap(env, moved)
    before_decoys = await _snap(env, decoys.values())
    await env.pool.execute(_parts()[1])
    restored = [i for i in ids if i not in moved]
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE id = ANY($1::bigint[]) AND status='approved' "
            "AND notes IS NULL",
            restored,
        )
        == COUNT - 3
    )
    assert await _snap(env, restored, drop=("updated_at",)) == [
        r for r in original if r["id"] not in moved
    ]
    assert await _snap(env, moved) == before_moved
    assert [(r["status"], r["notes"]) for r in before_moved] == [
        ("paid", MARKER),
        ("paid", MARKER),
        ("approved", MARKER),
    ]
    # the row that was paid by hand (no marker) stays paid; every other decoy is byte-equal
    assert await _snap(env, decoys.values()) == before_decoys
    assert (
        await env.pool.fetchval(
            "SELECT status FROM hr_bonus_ledger WHERE id=$1", decoys["paid_outside"]
        )
    ) == "paid"


@pytest.mark.asyncio
async def test_4b_rollback_then_forward_closes_the_same_603_again(env: Env) -> None:
    ids = await _seed(env)
    original = await _snap(env, ids, drop=("updated_at",))
    await _forward(env)
    await env.pool.execute(_parts()[1])
    assert await _snap(env, ids, drop=("updated_at",)) == original
    assert await _o3(env) == COUNT
    await _forward(env)
    assert await _marked(env) == (COUNT, TOTAL)


# ─── 5: trigger safety ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_5_a_candidate_whose_practice_is_not_completed_closes_without_the_triggers(
    env: Env,
) -> None:
    ids = await _seed(env)
    practice = await env.pool.fetchval(
        "INSERT INTO practices(status) VALUES ('completed') RETURNING id"
    )
    await env.pool.execute(
        "UPDATE hr_bonus_ledger SET source='practice', practice_id=$2 WHERE id=$1", ids[9], practice
    )
    await env.pool.execute("UPDATE practices SET status='on_process' WHERE id=$1", practice)
    # the validate trigger would now refuse any UPDATE OF practice_id on that row
    with pytest.raises(asyncpg.exceptions.RaiseError, match="requires completion"):
        await env.pool.execute(
            "UPDATE hr_bonus_ledger SET practice_id = practice_id WHERE id=$1", ids[9]
        )
    assert await _o3(env) == COUNT
    notices = await _forward(env)
    assert any("603 rows closed as paid" in n for n in notices), notices
    assert await _marked(env) == (COUNT, TOTAL)
    assert (
        await env.pool.fetchval("SELECT practice_id FROM hr_bonus_ledger WHERE id=$1", ids[9])
    ) == practice


# ─── 6: never waits (T1: no clock, a 60 s lock_timeout and a 15 s bound) ───────────────────────

ALLOCATION_BUSY = "400: bonus payout allocation is busy"


async def _refused(sql: str, *, raised: str | None = None, relation: str | None = None) -> None:
    """The statement refuses with 55P03 at once: a try-lock's own RAISE text or a NOWAIT's relation name.

    lock_timeout and statement_timeout are 60 s on this connection and the call is bounded by 15 s, so a body
    that waits for the lock fails on TimeoutError, and a lock_timeout message could never arrive in time.
    """
    conn = await asyncio.wait_for(
        asyncpg.connect(_dsn(), server_settings={"search_path": SCHEMA}), BOUND
    )
    try:
        await asyncio.wait_for(
            conn.execute("SET lock_timeout = '60s'; SET statement_timeout = '60s'"), BOUND
        )
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError) as busy:
            await asyncio.wait_for(conn.execute(sql), BOUND)
        assert busy.value.sqlstate == "55P03"
        message = str(busy.value)
        if raised is not None:
            assert raised in message, message
        if relation is not None:
            assert f'"{relation}"' in message, message
    finally:
        conn.terminate()


@pytest.mark.asyncio
async def test_6a_the_allocation_lock_held_elsewhere_gives_55p03_and_changes_nothing(
    env: Env,
) -> None:
    await _seed(env)
    for exclusive in (False, True):  # an approve_bonus (shared) or a mark paid (exclusive)
        async with _hold(env.pool, "hr_bonus_payout_allocation", exclusive=exclusive):
            await _refused(_parts()[0], raised=ALLOCATION_BUSY)
        assert await _marked(env) == (0, 0)
    assert await _o3(env) == COUNT
    await _forward(env)  # free again
    assert await _marked(env) == (COUNT, TOTAL)
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
async def test_6b_a_candidate_row_locked_elsewhere_gives_55p03_and_changes_nothing(
    env: Env,
) -> None:
    ids = await _seed(env)
    holder = await env.pool.acquire()
    tx = holder.transaction()
    try:
        await tx.start()
        await asyncio.wait_for(
            holder.fetchval("SELECT id FROM hr_bonus_ledger WHERE id=$1 FOR UPDATE", ids[100]),
            BOUND,
        )
        await _refused(_parts()[0], relation="hr_bonus_ledger")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tx.rollback(), BOUND)
        await env.pool.release(holder)
    assert await _o3(env) == COUNT
    assert await _marked(env) == (0, 0)
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
@pytest.mark.parametrize("month", ["2026-01", "2026-05", "2026-10"])
async def test_6c_a_payroll_month_held_elsewhere_gives_55p03_and_changes_nothing(
    env: Env, month: str
) -> None:
    await _seed(env)
    async with _hold(env.pool, f"hr_payroll_month:{month}", exclusive=False):
        await _refused(_parts()[0], raised=f"400: the {month} payroll month is busy")
    assert await _o3(env) == COUNT
    async with _hold(env.pool, "hr_payroll_month:2026-11", exclusive=True):
        await asyncio.wait_for(_forward(env), BOUND)  # November is not one of its months
    assert await _marked(env) == (COUNT, TOTAL)
    await _no_stray_advisory_locks(env)


@pytest.mark.asyncio
async def test_6d_the_rollback_never_waits_either(env: Env) -> None:
    await _seed(env)
    await _forward(env)
    async with _hold(env.pool, "hr_bonus_payout_allocation", exclusive=True):
        await _refused(_parts()[1], raised=ALLOCATION_BUSY)
    async with _hold(env.pool, "hr_payroll_month:2026-04", exclusive=False):
        await _refused(_parts()[1], raised="400: the 2026-04 payroll month is busy")
    ids = [
        r["id"]
        for r in await env.pool.fetch("SELECT id FROM hr_bonus_ledger WHERE notes=$1", MARKER)
    ]
    holder = await env.pool.acquire()  # a marked row locked by a writer
    tx = holder.transaction()
    try:
        await tx.start()
        await asyncio.wait_for(
            holder.fetchval("SELECT id FROM hr_bonus_ledger WHERE id=$1 FOR UPDATE", ids[50]),
            BOUND,
        )
        await _refused(_parts()[1], relation="hr_bonus_ledger")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tx.rollback(), BOUND)
        await env.pool.release(holder)
    assert await _marked(env) == (COUNT, TOTAL)
    await _no_stray_advisory_locks(env)


# ─── 7: empty schema ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_7_a_schema_with_no_ledger_is_a_notice_not_an_error() -> None:
    conn = await asyncpg.connect(_dsn())
    notices: list[str] = []
    try:
        await conn.execute("DROP SCHEMA IF EXISTS pr3c_empty CASCADE; CREATE SCHEMA pr3c_empty")
        await conn.execute("SET search_path TO pr3c_empty")
        conn.add_log_listener(lambda _c, msg: notices.append(msg.message))
        forward, rollback = _parts()
        await conn.execute(forward)
        await conn.execute(rollback)  # nothing to restore, nothing to fail
        assert any("no bonus ledger here" in n for n in notices), notices
    finally:
        await conn.execute("RESET search_path; DROP SCHEMA IF EXISTS pr3c_empty CASCADE")
        await conn.close()


@pytest.mark.asyncio
async def test_7b_an_empty_ledger_is_a_notice_with_the_measured_counts(env: Env) -> None:
    notices = await _forward(env)
    assert any("found 0 rows, 0 IDR" in n and "nothing closed" in n for n in notices), notices


# ─── 8: the runner ─────────────────────────────────────────────────────────────────────────────


def test_8_the_marker_stands_alone_and_the_forward_holds_none_of_the_rollback() -> None:
    text = FILE_400.read_text(encoding="utf-8")
    forward, rollback = split_migration_sql(text)
    assert rollback, "the rollback marker must be alone on its line (migration_base.py:524-527)"
    assert extract_dependencies(text) == [399], "the depends header must be alone on its line"
    assert [ln for ln in text.splitlines() if ln.strip() == "-- === ROLLBACK ==="] == [
        "-- === ROLLBACK ==="
    ]
    assert "SET status = 'approved'" in rollback and "SET status = 'approved'" not in forward
    assert "SET status = 'paid'" in forward and "SET status = 'paid'" not in rollback
    assert forward.count(MARKER) == 1 and rollback.count(MARKER) == 1


def _migration() -> BaseMigration:
    return BaseMigration(
        400, FILE_400.name, "pr3c", dependencies=[399], rollback_sql=_parts()[1], _sql_dir=MIG_DIR
    )


async def _seed_ledgers(env: Env) -> None:
    """The runner's ledgers in the fixture schema with a 393 and a 399 row (dependency check)."""
    await _seed_393_ledger(env)
    await env.pool.execute(
        "INSERT INTO schema_migrations(migration_name, migration_number, checksum) "
        "VALUES ('399_bonus_run_payment', 399, 'x')"
    )


@pytest.mark.asyncio
async def test_8_the_real_runner_applies_400_finds_its_rollback_and_rolls_it_back(env: Env) -> None:
    ids = await _seed(env)
    original = await _snap(env, ids, drop=("updated_at",))
    await _seed_ledgers(env)
    assert await _migration().apply(database_url=_schema_dsn(), dedicated=False) is True
    assert await _marked(env) == (COUNT, TOTAL)
    stored = await env.pool.fetchval(
        "SELECT rollback_sql FROM _schema_versions WHERE migration_name='400_close_pre_november_bonuses_paid'"
    )
    assert stored == _parts()[1]
    manager = MigrationManager(_schema_dsn(), dedicated=False)
    try:
        assert (
            await asyncio.wait_for(
                manager.rollback_migration("400_close_pre_november_bonuses_paid"), BOUND * 2
            )
            is True
        )
    finally:
        await manager.close()
    assert await _snap(env, ids, drop=("updated_at",)) == original
    for table in ("_schema_versions", "schema_migrations"):
        assert not await env.pool.fetchval(
            f"SELECT count(*) FROM {table} WHERE migration_number=400"
        )


@pytest.mark.asyncio
async def test_8_without_399_the_runner_refuses_400(env: Env) -> None:
    await _seed_393_ledger(env)  # a 393 row only: 399 is missing from schema_migrations
    with pytest.raises(MigrationError):
        await _migration().apply(database_url=_schema_dsn(), dedicated=False)


@pytest.mark.asyncio
async def test_8_a_busy_writer_gives_a_55p03_migration_error_and_nothing_closed(
    env: Env, monkeypatch
) -> None:
    monkeypatch.setattr(mb, "LOCK_RETRY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(
        mb, "LOCK_RETRY_LOCK_TIMEOUT", "60s"
    )  # far above BOUND: only a try-lock refuses
    await _seed(env)
    await _seed_ledgers(env)
    async with _hold(env.pool, "hr_bonus_payout_allocation", exclusive=False):
        await _refused_by_a_lock_that_does_not_wait(
            _migration().apply(database_url=_schema_dsn(), dedicated=False), raised=ALLOCATION_BUSY
        )
    assert await _marked(env) == (0, 0)
    assert not await env.pool.fetchval(
        "SELECT count(*) FROM _schema_versions WHERE migration_number=400"
    )
    assert (
        await asyncio.wait_for(
            _migration().apply(database_url=_schema_dsn(), dedicated=False), BOUND * 2
        )
        is True
    )
    assert await _marked(env) == (COUNT, TOTAL)
    await _no_stray_advisory_locks(env)


# ─── 9: the readers of `paid` ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_9_closing_the_rows_pays_nothing_again_and_moves_no_total(
    env: Env, monkeypatch
) -> None:
    """A retroactive salary, a bonus run and every total: before and after, for every month Jan-Oct."""
    ids = await _seed(env)
    monkeypatch.setattr(hr_service, "business_today", lambda: date(2026, 12, 20))

    async def inputs(month: int) -> set[int]:
        rows = await env.pool.fetch(
            "SELECT bonus_id FROM hr_payroll_bonus_inputs($1, NULL, '2026-12-20')",
            date(2026, month, 1),
        )
        return {r["bonus_id"] for r in rows}

    async def summaries() -> list[dict]:
        return [await env.svc.get_bonus_summary(env.emp_id, m, 2026) for m in range(1, 11)]

    before_inputs = {m: await inputs(m) for m in range(1, 11)}
    assert all(before_inputs[m] for m in range(1, 11)), "D4: a retroactive salary pays these rows"
    totals_before = await summaries()

    await _forward(env)

    for month in range(1, 11):
        assert await inputs(month) == set(), f"month {month} still feeds a salary"
    # the run inputs of November (and December) never held them, and still do not
    for run in ("2026-11-01", "2026-12-01"):
        assert (
            await env.pool.fetchval(
                "SELECT count(*) FROM hr_bonus_run_inputs($1::date, '2026-12-20') WHERE bonus_id = ANY($2::bigint[])",
                date.fromisoformat(run),
                ids,
            )
            == 0
        )
    # totals: approved and paid count alike
    assert await summaries() == totals_before
    # a retroactive October salary carries none of them and reserves none
    result = await env.svc.calculate_payroll(10, 2026, env.admin)
    slips = await env.pool.fetch(
        "SELECT bonus_total_idr FROM hr_payslips WHERE payroll_period_id=$1", result["period_id"]
    )
    assert slips and all(s["bonus_total_idr"] == 0 for s in slips)
    await env.svc.approve_payroll(result["period_id"], env.admin)
    await env.svc.mark_payroll_paid(result["period_id"])
    assert (
        await env.pool.fetchval(
            "SELECT count(*) FROM hr_bonus_ledger WHERE id = ANY($1::bigint[]) AND status='paid' "
            "AND payroll_period_id IS NULL AND bonus_run_id IS NULL AND notes=$2",
            ids,
            MARKER,
        )
        == COUNT
    )
    # "my bonuses" lists them with the same amounts, only the status word moves
    mine = await env.pool.fetch(hr_service.HRService._MY_BONUSES_SQL, env.emp_id, 2026)
    assert {r["status"] for r in mine} == {"paid"}


# ─── 10: the boundary of 399's paid-without-payout CHECK, in Bali time ───────────────────────────


@pytest.mark.asyncio
async def test_10_399s_check_admits_a_paid_row_with_no_payout_only_before_november_in_bali_time(
    env: Env,
) -> None:
    """400 relies on ck_hr_bonus_paid_has_payout: 23:59:59.999999 WITA on Oct 31 may be paid with no period and
    no run, 00:00 WITA on Nov 1 may not (a UTC cut, a December cut or no cut would each flip one of the two)."""
    last_oct = await _decoy(
        env, "approved", datetime(2026, 10, 31, 15, 59, 59, 999999, tzinfo=timezone.utc)
    )
    first_nov = await _decoy(env, "approved", datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc))
    await env.pool.execute("UPDATE hr_bonus_ledger SET status='paid' WHERE id=$1", last_oct)
    with pytest.raises(asyncpg.exceptions.CheckViolationError, match="ck_hr_bonus_paid_has_payout"):
        await env.pool.execute("UPDATE hr_bonus_ledger SET status='paid' WHERE id=$1", first_nov)
    rows = await env.pool.fetch(
        "SELECT id, status FROM hr_bonus_ledger WHERE id = ANY($1::bigint[]) ORDER BY id",
        [last_oct, first_nov],
    )
    assert [(r["id"], r["status"]) for r in rows] == [(last_oct, "paid"), (first_nov, "approved")]
