"""The executor consumes the existing audit policy without widening its pins."""

import asyncio
import hashlib
import uuid
from pathlib import Path

import asyncpg
import pytest

from backend.db import migrate
from backend.db.migration_base import BaseMigration
from backend.db.migration_manager import MigrationManager
from backend.db.schema_audit import LEGACY_CHECKSUM_BASELINE
from backend.tests.db.test_universal_migration_cli_pg import (
    _cleanup,
    _ledger_rows,
    _table_exists,
    _write_migration,
    disposable_dsn,  # noqa: F401 -- proven disposable owner/database/system ID
    schema_dir,  # noqa: F401 -- isolate discovery and BaseMigration SQL paths
)

SQL = Path(__file__).resolve().parents[2] / "db/migrations_v2"
KNOWN = [(key, pair) for key, pair in LEGACY_CHECKSUM_BASELINE.items() if pair[1] is not None]
FIRST = next((key, pair) for key, pair in KNOWN if key[0] == 127)


@pytest.fixture(autouse=True)
def _reporting_cleanup():
    yield
    migrate.remove_cli_reporting_boundary()


@pytest.mark.parametrize(("key", "pair"), KNOWN, ids=[str(key[0]) for key, _ in KNOWN])
def test_all_exact_existing_pairs_are_legacy_warnings(key, pair, monkeypatch):
    monkeypatch.delenv("SCHEMA_AUDIT_CHECKSUM_ENFORCE", raising=False)
    number, name = key
    (path,) = SQL.glob(f"{number:03d}_*.sql")
    assert hashlib.sha256(path.read_text().encode()).hexdigest() == pair[1]
    result = MigrationManager._applied_checksum_findings(
        [{"number": number, "path": path}],
        [{"migration_number": number, "migration_name": name, "checksum": pair[0]}],
    )
    assert result == ([], [number])


@pytest.mark.parametrize(
    "mutation", ["name", "stored", "recomputed", "number", "cross-pair", "uppercase"]
)
def test_new_anomalies_are_never_legacy_warnings(mutation, tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEMA_AUDIT_CHECKSUM_ENFORCE", "off")
    (number, name), (stored, _) = FIRST
    (original,) = SQL.glob(f"{number:03d}_*.sql")
    path = tmp_path / original.name
    path.write_bytes(original.read_bytes())
    if mutation == "name":
        name += "_unapproved"
    elif mutation == "stored":
        stored = "0" * 64
    elif mutation == "recomputed":
        path.write_text(path.read_text() + "\n-- synthetic unapproved edit\n")
    elif mutation == "number":
        number = 999
    elif mutation == "cross-pair":
        stored = next(pair[0] for key, pair in KNOWN if key[0] == 157)
    else:
        stored = stored.upper()
    assert MigrationManager._applied_checksum_findings(
        [{"number": number, "path": path}],
        [{"migration_number": number, "migration_name": name, "checksum": stored}],
    ) == ([number], [])


def test_bad_enforcement_flag_refuses_even_without_a_mismatch(monkeypatch):
    monkeypatch.setenv("SCHEMA_AUDIT_CHECKSUM_ENFORCE", "treu")
    with pytest.raises(ValueError):
        MigrationManager._applied_checksum_findings([], [])


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.database
@pytest.mark.parametrize("single", [False, True], ids=["all", "single"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
@pytest.mark.parametrize(
    "policy", ["off", "on", "typo", "wrong-name", "wrong-stored", "wrong-recomputed", "cross-pair"]
)
async def test_real_policy_admission_preserves_ledgers_and_refusals_are_atomic(
    disposable_dsn, schema_dir, single, dry_run, policy, monkeypatch, capsys, caplog
):
    (number, name), (stored, _) = FIRST
    (source,) = SQL.glob(f"{number:03d}_*.sql")
    path = schema_dir / source.name
    path.write_bytes(source.read_bytes())
    table = "udb_policy_" + uuid.uuid4().hex[:12]
    pending = 926
    _write_migration(
        schema_dir,
        pending,
        "policy_pending",
        f"CREATE TABLE public.{table}(id INTEGER)",
        f"DROP TABLE public.{table}",
    )
    if policy == "wrong-name":
        name += "_unapproved"
    elif policy == "wrong-stored":
        stored = "0" * 64
    elif policy == "wrong-recomputed":
        path.write_text(path.read_text() + "\n-- synthetic unapproved edit\n")
    elif policy == "cross-pair":
        stored = next(pair[0] for key, pair in KNOWN if key[0] == 157)
    flag = "synthetic-sensitive-typo" if policy == "typo" else "on" if policy == "on" else "off"
    monkeypatch.setenv("SCHEMA_AUDIT_CHECKSUM_ENFORCE", flag)
    conn = await asyncpg.connect(disposable_dsn)
    migration = BaseMigration(
        number, source.name, "synthetic applied history", rollback_sql="SELECT 1;"
    )
    try:
        await migration._ensure_migration_log(conn)
        for ledger in ("schema_migrations", "_schema_versions"):
            await conn.execute(
                f"INSERT INTO {ledger}(migration_name,migration_number,checksum) VALUES($1,$2,$3)",
                name,
                number,
                stored,
            )
        before = await _ledger_rows(conn, number)
        # Exercise the real CLI reporting boundary and a fresh manager/loop.
        monkeypatch.setattr(
            migrate, "MigrationManager", lambda: MigrationManager(database_url=disposable_dsn)
        )
        args = ["apply", str(pending)] if single else ["apply-all"]
        if dry_run:
            args.append("--dry-run")
        code = await asyncio.to_thread(migrate.main, args)
        admitted = policy == "off"
        assert code == (0 if admitted else 1)
        output = capsys.readouterr()
        all_output = output.out + output.err + caplog.text
        assert name not in all_output and stored not in all_output
        if admitted:
            assert "WARNING: legacy checksum baseline (not verified): 127" in output.out
        else:
            assert "WARNING: legacy checksum baseline" not in output.out
        if policy == "typo":
            assert flag not in all_output
        assert await _ledger_rows(conn, number) == before
        assert await _table_exists(conn, table) is (admitted and not dry_run)
        pending_rows = await _ledger_rows(conn, pending)
        assert all(
            len(rows) == (1 if admitted and not dry_run else 0) for rows in pending_rows.values()
        )
    finally:
        await _cleanup(conn, [number, pending], [table])
        await conn.close()
