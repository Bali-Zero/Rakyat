"""CI bootstrap fidelity and the real bootstrap -> SQL executor consumer.

Uses the adoption suite's explicitly proven disposable owner/database/system ID.
This tests CI schema preparation, not an empty-database executor contract.
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit

import pytest

from backend.tests.db.test_migration_345_347_partner_adoption_pg import (
    MIGRATIONS,
    adoption_conn,  # noqa: F401 -- shared proven-disposable fixture
    apply_legacy_121,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]
ROOT = Path(__file__).resolve().parents[3]
LEGACY = ROOT / "scripts/apply_migration_033.py"
BOOTSTRAP = ROOT / "scripts/ci_bootstrap_schema.py"


def _family_ddl(path: Path) -> list[str]:
    """Extract literal CREATEs without importing/executing the legacy runner."""
    statements = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value.strip()
        if value.startswith("CREATE TABLE IF NOT EXISTS client_family_members (") or (
            value.startswith("CREATE INDEX IF NOT EXISTS idx_family_members_")
        ):
            statements.append((node.lineno, value))
    return [value for _, value in sorted(statements)]


def test_ci_family_ddl_matches_actual_legacy_033() -> None:
    historical = _family_ddl(LEGACY)
    assert len(historical) == 5, "full 033 table plus all four historical indexes"
    assert [" ".join(sql.split()) for sql in _family_ddl(BOOTSTRAP)] == [
        " ".join(sql.split()) for sql in historical
    ]


async def _family_catalog(conn) -> dict:
    return {
        "columns": [
            dict(row)
            for row in await conn.fetch(
                "SELECT a.attname, format_type(a.atttypid,a.atttypmod) AS type, "
                "a.attnotnull, pg_get_expr(d.adbin,d.adrelid) AS default_expr "
                "FROM pg_attribute a LEFT JOIN pg_attrdef d "
                "ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                "WHERE a.attrelid=to_regclass('public.client_family_members') "
                "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum"
            )
        ],
        "constraints": [
            dict(row)
            for row in await conn.fetch(
                "SELECT conname,pg_get_constraintdef(oid,true) AS definition "
                "FROM pg_constraint WHERE conrelid=to_regclass('public.client_family_members') "
                "ORDER BY conname"
            )
        ],
        "indexes": [
            dict(row)
            for row in await conn.fetch(
                "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
                "AND tablename='client_family_members' ORDER BY indexname"
            )
        ],
    }


async def _consumer(*args: str) -> None:
    # Never inherit application credentials/DSNs from the runner environment.
    target = urlsplit(os.environ["NUZ_ADOPTION_PG_DSN"])
    socket = unquote(target.hostname or "")
    assert socket.startswith("/"), "the CI reproduction requires an owned Unix socket"
    # psycopg2 does not accept asyncpg's percent-encoded Unix-host authority.
    # Both drivers accept the query-parameter spelling; never fall back to TCP.
    dsn = f"postgresql://{target.username}@{target.path}?" + urlencode(
        {"host": socket, "port": target.port}
    )
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": ".:../crm-cell",
        "DATABASE_URL": dsn,
        "JWT_SECRET_KEY": "test_jwt_secret_key_for_testing_only_min_32_chars_long",
        "API_KEYS": "test_api_key_1,test_api_key_2",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *args,
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError:
        process.kill()
        await process.communicate()
        pytest.fail("CI consumer exceeded its 120-second bound")
    assert process.returncode == 0, output.decode("utf-8", errors="replace")


@pytest.mark.asyncio
async def test_ci_bootstrap_then_all_sql_preserves_033_and_is_repeatable(adoption_conn) -> None:
    conn = adoption_conn
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY)")
    for statement in _family_ddl(LEGACY):
        await conn.execute(statement)
    historical = await _family_catalog(conn)
    assert len(historical["columns"]) == 16
    assert len(historical["constraints"]) == 2
    assert len(historical["indexes"]) == 5

    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await _consumer("scripts/ci_bootstrap_schema.py")
    assert await _family_catalog(conn) == historical

    await _consumer("scripts/ci_bootstrap_schema.py")
    assert await _family_catalog(conn) == historical

    await _consumer("-m", "backend.db.migrate", "apply-all")
    sql_count = len(list((ROOT / "backend/db/migrations_v2").glob("*.sql")))
    assert sql_count > 0, "the migrations directory was not found"
    ledgers = await conn.fetchval(
        "SELECT jsonb_build_object('primary', "
        "(SELECT count(*) FROM schema_migrations), 'compatibility', "
        "(SELECT count(*) FROM _schema_versions))"
    )
    assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == sql_count
    assert await conn.fetchval("SELECT count(*) FROM _schema_versions") == sql_count
    assert (
        await conn.fetchval(
            "SELECT convalidated FROM pg_constraint WHERE "
            "conrelid='practices'::regclass AND conname='practices_family_member_id_fkey'"
        )
        is True
    )
    await _consumer("-m", "backend.db.migrate", "apply-all")
    assert (
        await conn.fetchval(
            "SELECT jsonb_build_object('primary', "
            "(SELECT count(*) FROM schema_migrations), 'compatibility', "
            "(SELECT count(*) FROM _schema_versions))"
        )
        == ledgers
    )
    assert await _family_catalog(conn) == historical


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("already_adopted", "failure_mode"),
    [(False, "post-ledger"), (True, "post-ledger"), (False, "timeout")],
    ids=["new-column-rollback", "legacy-121-rollback", "real-60s-timeout"],
)
async def test_347_real_locks_and_post_ledger_failure_roll_back(
    adoption_conn, already_adopted, failure_mode, tmp_path, monkeypatch
) -> None:
    """Observe locks in the real executor before forcing its transaction to fail."""
    from backend.db import migration_base, migration_manager

    conn = adoption_conn
    if already_adopted:
        await apply_legacy_121(conn)

    async def signature():
        return [
            dict(row)
            for row in await conn.fetch(
                "SELECT a.attname,format_type(a.atttypid,a.atttypmod) AS type "
                "FROM pg_attribute a WHERE a.attrelid='practices'::regclass "
                "AND a.attname='family_member_id' AND NOT a.attisdropped"
            )
        ]

    before = await signature()
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
    original_log = migration_base.BaseMigration._log_migration
    observed = []
    timeout_states = []
    attempts = []

    async def fail_after_ledgers(self, connection, *args, **kwargs):
        if self.migration_number == 347:
            attempts.append(self.migration_number)
            observed.extend(
                dict(row)
                for row in await connection.fetch(
                    "SELECT c.relname,l.mode FROM pg_locks l JOIN pg_class c ON c.oid=l.relation "
                    "WHERE l.pid=pg_backend_pid() AND l.granted "
                    "AND c.relname IN ('practices','client_family_members') "
                    "ORDER BY c.relname,l.mode"
                )
            )
            assert await connection.fetchval("SHOW statement_timeout") == "1min"
            assert await connection.fetchval("SHOW lock_timeout") == "2s"
            if failure_mode == "timeout":
                started = time.monotonic()
                try:
                    await connection.execute("SELECT pg_sleep(65)")
                except Exception as exc:
                    timeout_states.append(getattr(exc, "sqlstate", None))
                    assert 59 <= time.monotonic() - started < 90
                    raise
                pytest.fail("the actual 60-second statement timeout did not cancel")
            await original_log(self, connection, *args, **kwargs)
            raise RuntimeError("synthetic 347 failure after both ledger writes")
        await original_log(self, connection, *args, **kwargs)

    monkeypatch.setattr(migration_base.BaseMigration, "_log_migration", fail_after_ledgers)
    manager = migration_manager.MigrationManager(database_url=os.environ["NUZ_ADOPTION_PG_DSN"])
    async with manager:
        result = await manager.apply_all_pending()
    assert len(result["failed"]) == 1
    assert attempts == [347], "post-ledger errors and 57014 are terminal, not retried"
    if failure_mode == "timeout":
        assert timeout_states == ["57014"]
    assert {"relname": "practices", "mode": "AccessExclusiveLock"} in observed
    if not already_adopted:
        assert {"relname": "client_family_members", "mode": "ShareRowExclusiveLock"} in observed
    # Emit only synthetic catalog metadata for the retained native test log.
    print({"path": "legacy-121" if already_adopted else "new-column", "locks": observed})
    assert await signature() == before
    for ledger in ("schema_migrations", "_schema_versions"):
        assert await conn.fetchval(f"SELECT count(*) FROM {ledger} WHERE migration_number=347") == 0


@pytest.mark.parametrize(
    "table", ["hr_bonus_rates", "hr_payroll_periods", "hr_leave_types", "hr_leave_balances"]
)
def test_delivery_payroll_prerequisites_match_legacy_068(table: str) -> None:
    """Keep the rate window, closed-month states and foreign keys in CI faithful to payroll."""
    import re

    pattern = rf"CREATE TABLE IF NOT EXISTS {table} \([\s\S]*?\n\s*\)"
    # An outer ')' terminates the SQL literal, while inner CHECKs also have
    # closing parens. Match the literal's end in each of its source formats.
    legacy = (ROOT / "backend/migrations/migration_068_hr_payroll.py").read_text()
    expected = re.search(pattern + r";", legacy)
    actual = re.search(pattern + r'\s*"""', BOOTSTRAP.read_text())
    assert expected and actual
    assert expected.group().rstrip(";").split() == actual.group().rstrip('"').split()


def test_leave_triggers_and_index_in_ci_match_legacy_068() -> None:
    """CI keeps 068's updated_at function, its triggers on the leave tables and the balance index."""
    import re

    legacy = (ROOT / "backend/migrations/migration_068_hr_payroll.py").read_text()
    boot = BOOTSTRAP.read_text()

    function = r"CREATE OR REPLACE FUNCTION hr_set_updated_at\(\)[\s\S]*?\$\$ LANGUAGE plpgsql"
    expected_function = re.search(function, legacy)
    actual_function = re.search(function, boot)
    assert expected_function and actual_function
    assert expected_function.group().split() == actual_function.group().split()

    # 068 attaches the function to every leave table by name with one template.
    legacy_tables = re.search(r"unnest\(ARRAY\[([\s\S]*?)\]\)", legacy).group(1)
    for table in ("hr_leave_types", "hr_leave_balances"):
        assert f"'{table}'" in legacy_tables
    assert "'DROP TRIGGER IF EXISTS trg_%s_updated_at ON %I; '" in legacy
    assert "'CREATE TRIGGER trg_%s_updated_at BEFORE UPDATE ON %I '" in legacy
    assert "'FOR EACH ROW EXECUTE FUNCTION hr_set_updated_at();'" in legacy
    assert 'for table in ("hr_leave_types", "hr_leave_balances"):' in boot
    assert 'f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}"' in boot
    assert 'f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "' in boot
    assert '"FOR EACH ROW EXECUTE FUNCTION hr_set_updated_at()"' in boot

    index = "CREATE INDEX IF NOT EXISTS idx_hr_leave_bal_emp ON hr_leave_balances(employee_id, balance_year)"
    assert index + ";" in legacy
    pieces = re.search(
        r'"(CREATE INDEX IF NOT EXISTS idx_hr_leave_bal_emp )"\s*"(ON hr_leave_balances\([^"]*\))"',
        boot,
    )
    assert pieces and "".join(pieces.groups()) == index
