"""Faithfulness and real-Postgres coverage for adoption migrations 345-347.

The static checks run everywhere. Behavioural checks require an explicitly
proven disposable PostgreSQL database; they never fall back to the shared test
database or any application setting.

Target proof:
* ``NUZ_ADOPTION_PG_DSN`` must name database ``udb_adoption_test``;
* ``NUZ_ADOPTION_PG_SYSTEM_ID`` must match ``pg_control_system()``.

The fixture owns that disposable database and resets its public schema around
each behavioural test. All rows are synthetic.
"""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from backend.db.migration_base import extract_dependencies, split_migration_sql  # noqa: E402
from backend.migrations.migration_119_partners import apply as apply_legacy_119  # noqa: E402
from backend.migrations.migration_120_partner_email_outbox import (  # noqa: E402
    apply as apply_legacy_120,
)
from backend.migrations.migration_121_practices_family_member import (  # noqa: E402
    apply as apply_legacy_121,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]

BACKEND = Path(__file__).resolve().parents[2]
REPO = Path(__file__).resolve().parents[5]
MIGRATIONS = {
    345: BACKEND / "db" / "migrations_v2" / "345_partners_module_adoption.sql",
    346: BACKEND / "db" / "migrations_v2" / "346_partner_email_outbox_adoption.sql",
    347: BACKEND / "db" / "migrations_v2" / "347_practices_family_member_adoption.sql",
}
LEGACY_SOURCES = {
    345: BACKEND / "migrations" / "migration_119_partners.py",
    346: BACKEND / "migrations" / "migration_120_partner_email_outbox.py",
    347: BACKEND / "migrations" / "migration_121_practices_family_member.py",
}
EXPECTED_DEPENDENCIES = {345: [344], 346: [345], 347: [346]}
WORKFLOW = REPO / ".github" / "workflows" / "fly-deploy.yml"

_DSN_ENV = "NUZ_ADOPTION_PG_DSN"
_SYSID_ENV = "NUZ_ADOPTION_PG_SYSTEM_ID"
_EXPECTED_DATABASE = "udb_adoption_test"
_EXPECTED_USER = "universal_db_test_owner"


def _legacy_forward_sql(source: Path) -> str:
    """Reproduce the mechanical extraction used to build the adoption SQL."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    apply_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "apply"
    )
    statements: list[tuple[int, str]] = []
    for node in ast.walk(apply_function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            continue
        try:
            statement = ast.literal_eval(node.args[0])
        except (ValueError, TypeError):
            continue
        if isinstance(statement, str):
            statements.append((node.lineno, statement.strip()))
    return "\n\n".join(statement for _, statement in sorted(statements)) + "\n"


def test_adoption_files_preserve_legacy_forward_ddl_and_order() -> None:
    for number, path in MIGRATIONS.items():
        raw = path.read_text(encoding="utf-8")
        forward, rollback = split_migration_sql(raw)
        extracted = _legacy_forward_sql(LEGACY_SOURCES[number])
        digest = hashlib.sha256(extracted.encode("utf-8")).hexdigest()

        assert f"extracted forward SHA-256: {digest}" in raw
        # Squawk's FK diagnostic is anchored on REFERENCES rather than ALTER.
        # Ignore only its approved trailing annotation, retaining literal DDL identity.
        ddl = forward.replace(" -- squawk-ignore adding-foreign-key-constraint", "")
        assert extracted.rstrip() in ddl
        assert extract_dependencies(raw) == EXPECTED_DEPENDENCIES[number]
        assert rollback is not None
        assert "RAISE EXCEPTION" in rollback
        assert "cannot be rolled back destructively" in rollback


def test_deploy_workflow_has_one_migration_executor() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    canonical_command = (
        "--command \"/bin/sh -c 'cd /app && python -m backend.db.migrate apply-all'\""
    )

    # Pre-deploy and fresh-image passes use the same idempotent CLI. There is
    # no second migration implementation or direct historical module caller.
    assert workflow.count(canonical_command) == 2
    assert "run-python-migrations:" not in workflow
    for historical in (119, 120, 121):
        assert f"python -m backend.migrations.apply_migration_{historical}" not in workflow


async def _reset_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")


async def _create_prerequisites(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE team_members (id VARCHAR PRIMARY KEY);
        CREATE TABLE practices (id INTEGER PRIMARY KEY);
        CREATE TABLE client_family_members (id BIGINT PRIMARY KEY);
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )


@pytest_asyncio.fixture
async def adoption_conn() -> asyncpg.Connection:
    dsn = os.environ.get(_DSN_ENV, "").strip()
    expected_sysid = os.environ.get(_SYSID_ENV, "").strip()
    if not dsn:
        pytest.skip(f"{_DSN_ENV} is unset: no explicit disposable PostgreSQL target")
    if not expected_sysid:
        pytest.skip(f"{_SYSID_ENV} is unset: target cluster cannot be proven disposable")

    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:  # The DSN itself must never be printed.
        pytest.fail(f"{_DSN_ENV} target is unreachable ({type(exc).__name__})")

    target_verified = False
    try:
        identity = await conn.fetchrow(
            "SELECT current_database() AS db, current_user AS usr, "
            "(SELECT system_identifier FROM pg_control_system())::text AS sysid"
        )
        problems: list[str] = []
        if identity["usr"] != _EXPECTED_USER:
            problems.append("current_user differs from the disposable owner")
        if identity["db"] != _EXPECTED_DATABASE:
            problems.append(f"current_database={identity['db']!r}, expected {_EXPECTED_DATABASE!r}")
        if identity["sysid"] != expected_sysid:
            problems.append("pg_control_system().system_identifier does not match proof")
        if problems:
            pytest.fail("refusing to write: disposable-target mismatch: " + "; ".join(problems))

        target_verified = True
        await _reset_schema(conn)
        await _create_prerequisites(conn)
        yield conn
    finally:
        try:
            if target_verified:
                await _reset_schema(conn)
        finally:
            await conn.close()


async def _apply(conn: asyncpg.Connection, number: int) -> None:
    forward, _ = split_migration_sql(MIGRATIONS[number].read_text(encoding="utf-8"))
    await conn.execute(forward)


async def _apply_all(conn: asyncpg.Connection) -> None:
    for number in MIGRATIONS:
        await _apply(conn, number)


async def test_fresh_schema_gets_every_historical_effect(adoption_conn) -> None:
    await _apply_all(adoption_conn)

    tables = await adoption_conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename = ANY($1::text[]) ORDER BY tablename",
        [
            "partners",
            "partner_referrals",
            "partner_commissions",
            "partner_audit_log",
            "partner_email_outbox",
        ],
    )
    assert [row["tablename"] for row in tables] == [
        "partner_audit_log",
        "partner_commissions",
        "partner_email_outbox",
        "partner_referrals",
        "partners",
    ]
    assert (
        await adoption_conn.fetchval(
            "SELECT count(*) FROM system_settings WHERE key LIKE 'partner_%'"
        )
        == 5
    )
    assert (
        await adoption_conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='practices' "
            "AND column_name='family_member_id'"
        )
        == "bigint"
    )


async def test_repeat_is_idempotent_and_preserves_existing_setting(adoption_conn) -> None:
    await _apply_all(adoption_conn)
    relation_oids = await adoption_conn.fetch(
        "SELECT relname, oid FROM pg_class "
        "WHERE relnamespace='public'::regnamespace AND relname LIKE 'partner%' "
        "ORDER BY relname"
    )
    await adoption_conn.execute(
        "UPDATE system_settings SET value='77' WHERE key='partner_accrual_cooling_off_days'"
    )

    await _apply_all(adoption_conn)

    assert (
        await adoption_conn.fetch(
            "SELECT relname, oid FROM pg_class "
            "WHERE relnamespace='public'::regnamespace AND relname LIKE 'partner%' "
            "ORDER BY relname"
        )
        == relation_oids
    )
    assert (
        await adoption_conn.fetchval(
            "SELECT value FROM system_settings WHERE key='partner_accrual_cooling_off_days'"
        )
        == "77"
    )


async def test_partial_legacy_119_state_completes_without_replacing_it(adoption_conn) -> None:
    await apply_legacy_119(adoption_conn)
    await adoption_conn.execute("ALTER TABLE partners ADD COLUMN local_extension TEXT")
    partner_oid = await adoption_conn.fetchval("SELECT 'public.partners'::regclass::oid")

    await _apply_all(adoption_conn)

    assert await adoption_conn.fetchval("SELECT 'public.partners'::regclass::oid") == partner_oid
    assert await adoption_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='partners' "
        "AND column_name='local_extension')"
    )
    assert await adoption_conn.fetchval(
        "SELECT to_regclass('public.partner_email_outbox') IS NOT NULL"
    )
    assert await adoption_conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='practices' "
        "AND column_name='family_member_id')"
    )


async def test_complete_legacy_119_121_state_survives_repeated_adoption(adoption_conn) -> None:
    for apply_legacy in (apply_legacy_119, apply_legacy_120, apply_legacy_121):
        await apply_legacy(adoption_conn)
    await adoption_conn.execute(
        "UPDATE system_settings SET value='83' WHERE key='partner_accrual_cooling_off_days'"
    )
    catalog_sql = (
        "SELECT relname, oid FROM pg_class "
        "WHERE relnamespace='public'::regnamespace AND relkind='r' ORDER BY relname"
    )
    before_relations = await adoption_conn.fetch(catalog_sql)
    column_sql = (
        "SELECT attnum, atttypid FROM pg_attribute "
        "WHERE attrelid='public.practices'::regclass AND attname='family_member_id'"
    )
    before_column = await adoption_conn.fetchrow(column_sql)
    assert before_column is not None

    await _apply_all(adoption_conn)
    await _apply_all(adoption_conn)

    assert await adoption_conn.fetch(catalog_sql) == before_relations
    assert await adoption_conn.fetchrow(column_sql) == before_column
    assert (
        await adoption_conn.fetchval(
            "SELECT value FROM system_settings WHERE key='partner_accrual_cooling_off_days'"
        )
        == "83"
    )


async def test_incompatible_prior_shape_is_refused_by_catalog_guard(adoption_conn) -> None:
    await apply_legacy_119(adoption_conn)
    await adoption_conn.execute("ALTER TABLE partners ALTER COLUMN email DROP NOT NULL")

    with pytest.raises(
        asyncpg.CheckViolationError,
        match="Migration 345 refused: column catalog drift",
    ):
        await _apply(adoption_conn, 345)

    assert not await adoption_conn.fetchval(
        "SELECT is_nullable = 'NO' FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='partners' AND column_name='email'"
    )


async def test_adoption_through_canonical_executor_preserves_dual_ledgers(
    adoption_conn, tmp_path, monkeypatch, capsys
) -> None:
    """Exercise exact adoption bytes through discovery, dependencies and CLI."""
    from backend.db import migrate, migration_base, migration_manager

    directory = tmp_path / "migrations_v2"
    directory.mkdir()
    (directory / "344_synthetic_prerequisite.sql").write_text(
        "SELECT 1;\n-- === ROLLBACK ===\nSELECT 1;\n", encoding="utf-8"
    )
    for path in MIGRATIONS.values():
        (directory / path.name).write_bytes(path.read_bytes())
    monkeypatch.setattr(migration_manager, "__file__", str(tmp_path / "migration_manager.py"))
    monkeypatch.setattr(migration_base.BaseMigration, "MIGRATIONS_DIR", directory)
    monkeypatch.setattr(migration_base.settings, "migration_database_url", None)
    manager = migration_manager.MigrationManager(database_url=os.environ[_DSN_ENV])
    try:
        async with manager:
            assert await migrate.cmd_apply(manager) is True
            output = capsys.readouterr().out
            assert "Summary: applied=4 skipped=0 failed=0 refused=0" in output
            before = {}
            for ledger in ("schema_migrations", "_schema_versions"):
                rows = await adoption_conn.fetch(
                    f"SELECT migration_number, checksum FROM {ledger} ORDER BY migration_number"
                )
                assert [r["migration_number"] for r in rows] == [344, 345, 346, 347]
                for row in rows:
                    source = next(directory.glob(f"{row['migration_number']}_*.sql"))
                    assert (
                        row["checksum"]
                        == hashlib.sha256(
                            source.read_text(encoding="utf-8").encode("utf-8")
                        ).hexdigest()
                    )
                before[ledger] = rows
            assert await adoption_conn.fetchval(
                "SELECT to_regclass('public.partner_email_outbox') IS NOT NULL"
            )
            assert (
                await adoption_conn.fetchval(
                    "SELECT count(*) FROM system_settings WHERE key LIKE 'partner_%'"
                )
                == 5
            )
            assert await migrate.cmd_apply(manager) is True
            assert "Summary: applied=0 skipped=0 failed=0 refused=0" in capsys.readouterr().out
            for ledger, rows in before.items():
                assert (
                    await adoption_conn.fetch(
                        f"SELECT migration_number, checksum FROM {ledger} ORDER BY migration_number"
                    )
                    == rows
                )
    finally:
        migrate.remove_cli_reporting_boundary()
