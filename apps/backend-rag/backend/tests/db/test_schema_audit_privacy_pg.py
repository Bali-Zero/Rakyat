"""Exercise the existing audit CLI against a guarded, disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.db.migration_manager import MigrationManager

BACKEND_ROOT = Path(__file__).resolve().parents[3]
MARKERS = (
    "w4b-client-a@example.invalid",
    "w4b-client-b@example.invalid",
    "W4B_SQL_MESSAGE",
    "W4B_SQL_DETAIL",
    "W4B_SQL_HINT",
    "W4B_SQL_NOTICE",
)


@pytest_asyncio.fixture
async def audit_database() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    system_id = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    if not dsn or not system_id:
        pytest.skip("Requires the explicitly guarded disposable PostgreSQL runner")
    parsed = urlsplit(dsn)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.username == "universal_db_test_owner"
    assert parsed.path == "/universal_db_test"
    admin = await asyncpg.connect(dsn)
    name = "udb_audit_" + uuid4().hex
    created = False
    conn = None
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, current_user AS role, "
            "system_identifier::text AS system_id FROM pg_control_system()"
        )
        assert dict(identity) == {
            "db": "universal_db_test",
            "role": "universal_db_test_owner",
            "system_id": system_id,
        }
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        target = urlunsplit(parsed._replace(path="/" + name))
        conn = await asyncpg.connect(target)
        manager = MigrationManager(database_url=target)
        migrations = await manager.discover_migrations()
        assert migrations
        assert len({migration["number"] for migration in migrations}) == len(migrations)
        await manager._ensure_migration_log(conn)
        await conn.execute("CREATE TABLE schema_migrations (migration_number integer PRIMARY KEY)")
        await conn.executemany(
            "INSERT INTO _schema_versions (migration_name, migration_number, checksum) "
            "VALUES ($1, $2, $3)",
            [
                (
                    migration["path"].stem,
                    migration["number"],
                    hashlib.sha256(migration["path"].read_text().encode()).hexdigest(),
                )
                for migration in migrations
            ],
        )
        await conn.executemany(
            "INSERT INTO schema_migrations VALUES ($1)",
            [(migration["number"],) for migration in migrations],
        )
        await conn.execute("CREATE TABLE clients (email text)")
        yield conn, target
    finally:
        if conn is not None:
            await conn.close()
        if created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def _run_consumer(dsn: str, json_output: bool) -> tuple[int, str, str]:
    path = BACKEND_ROOT / "scripts/backend_stability_gate.py"
    spec = importlib.util.spec_from_file_location("w4b_stability_gate", path)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    command = gate.build_gate_commands()[1]
    assert command == [sys.executable, "-m", "backend.db.schema_audit"]
    if json_output:
        command += ["--json"]
    env = dict(os.environ)
    env.update(
        DATABASE_URL=dsn,
        MIGRATION_DATABASE_URL="",
        SCHEMA_AUDIT_REQUIRED_TABLES="clients",
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=BACKEND_ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("scenario", ["clean", "duplicates", "database_error"])
async def test_audit_consumer_privacy_on_real_postgres(
    audit_database: tuple[asyncpg.Connection, str],
    json_output: bool,
    scenario: str,
) -> None:
    conn, dsn = audit_database
    if scenario == "database_error":
        await conn.execute(
            """
            DROP TABLE clients;
            CREATE FUNCTION w4b_audit_error() RETURNS text LANGUAGE plpgsql AS $$
            BEGIN
                RAISE NOTICE 'W4B_SQL_NOTICE';
                RAISE EXCEPTION USING MESSAGE = 'W4B_SQL_MESSAGE',
                    DETAIL = 'W4B_SQL_DETAIL', HINT = 'W4B_SQL_HINT', ERRCODE = 'P0001';
            END $$;
            CREATE VIEW clients AS SELECT w4b_audit_error() AS email;
            """
        )
    else:
        emails = [MARKERS[0], MARKERS[1], None, "", "   "]
        if scenario == "duplicates":
            emails += [MARKERS[0].upper(), "  " + MARKERS[0] + "  ", MARKERS[1].upper()]
        await conn.executemany("INSERT INTO clients VALUES ($1)", [(email,) for email in emails])

    code, stdout, stderr = await _run_consumer(dsn, json_output)

    assert code == {"clean": 0, "duplicates": 1, "database_error": 2}[scenario]
    output = stdout + stderr
    for marker in MARKERS:
        assert marker.lower() not in output.lower()
        assert hashlib.sha256(marker.encode()).hexdigest() not in output
    assert "Traceback" not in output
    if scenario == "database_error":
        assert stdout == ""
        assert "RaiseError" in stderr
    elif json_output:
        report = json.loads(stdout)
        assert report["ok"] == (scenario == "clean")
        assert len(report["checks_run"]) == 5
        if scenario == "clean":
            assert report["findings"] == []
        else:
            assert {finding["code"] for finding in report["findings"]} == {
                "client_email_duplicates"
            }
            assert report["findings"][0]["details"] == {"duplicates": [{"count": 3}, {"count": 2}]}
    else:
        assert ("Result: OK" if scenario == "clean" else "Result: FAIL") in stdout
        if scenario == "duplicates":
            assert stdout.count("[ERROR]") == 1
            assert '"count": 3' in stdout and '"count": 2' in stdout
