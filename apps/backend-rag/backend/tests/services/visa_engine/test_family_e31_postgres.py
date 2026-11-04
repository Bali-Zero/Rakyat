"""Migration 337 on a disposable PG16+ cluster; never use an existing database."""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import rows_as_of

MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
FILES = (
    "330_visa_types_law_projection.sql",
    "331_visa_types_law_family_c.sql",
    "332_visa_types_stay_options.sql",
    "333_visa_types_law_family_e30.sql",
    "337_visa_types_law_family_e31.sql",
)


def pg_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("PG")}


class Cluster:
    """Every invocation pins the private socket, port, user and database."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.data = base / "data"

    def sql(self, sql: str, *, succeeds: bool = True) -> str:
        result = subprocess.run(
            [
                "psql",
                "-X",
                "-h",
                str(self.base),
                "-p",
                "55487",
                "-U",
                "e31_test",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-At",
            ],
            input=sql,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            env=pg_env(),
        )
        if succeeds:
            assert result.returncode == 0, result.stderr
            return result.stdout.strip()
        assert result.returncode != 0, "Invalid migration unexpectedly accepted"
        return result.stderr


@pytest.fixture(scope="module")
def pg() -> Iterator[Cluster]:
    for binary in ("initdb", "pg_ctl", "psql"):
        if not shutil.which(binary):
            pytest.skip(f"Throwaway PostgreSQL unavailable: {binary} missing")
    with tempfile.TemporaryDirectory(prefix="pg31", dir="/tmp") as directory:
        cluster = Cluster(Path(directory))
        initialized = subprocess.run(
            [
                "initdb",
                "-D",
                str(cluster.data),
                "-A",
                "trust",
                "-U",
                "e31_test",
                "--no-locale",
                "--encoding=UTF8",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=pg_env(),
        )
        if initialized.returncode and "Operation not permitted" in initialized.stderr:
            pytest.skip("Sandbox blocks disposable PostgreSQL initialization")
        assert initialized.returncode == 0, initialized.stderr
        start = subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(cluster.data),
                "-l",
                str(cluster.base / "server.log"),
                "-o",
                f"-F -k {cluster.base} -h '' -p 55487",
                "-w",
                "start",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=pg_env(),
        )
        log = (cluster.base / "server.log").read_text()
        if start.returncode and "Operation not permitted" in start.stderr + log:
            pytest.skip("Sandbox blocks disposable PostgreSQL socket")
        assert start.returncode == 0, start.stderr + log
        try:
            if int(cluster.sql("SHOW server_version_num;")) < 160000:
                pytest.skip("Disposable migration proof requires PostgreSQL 16+")
            yield cluster
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(cluster.data), "-m", "immediate", "-w", "stop"],
                capture_output=True,
                check=True,
                timeout=30,
                env=pg_env(),
            )


def test_forward_twice_rollback_reapply_and_fresh_rows_preserve_other_families(pg: Cluster) -> None:
    parts = [(MIGRATIONS / name).read_text().split("-- === ROLLBACK ===", 1) for name in FILES]
    pg.sql(parts[0][0].split("ALTER TABLE visa_types", 1)[0])
    assert (
        pg.sql("SELECT count(*) FROM information_schema.columns WHERE table_name='visa_types';")
        == "24"
    )
    expected_rows = [r for family in ("C", "E30", "E31") for r in rows_as_of(family, 337)]
    pg.sql(
        "\n".join(
            "INSERT INTO visa_types (code, name, category, duration, extensions, total_stay, renewable, "
            "processing_time_normal, processing_time_express, processing_timeline, cost_visa, cost_extension, "
            "cost_details, requirements, restrictions, allowed_activities, benefits, process_steps, tips, "
            "foreign_eligible, metadata, last_updated, created_at) "
            f"VALUES ('{row['code']}', 'legacy {row['code']}', 'legacy category', 'original duration', 'original extensions', "
            "'original total', true, 'normal', 'express', '{\"normal\": 5}', 'original cost', 'extension cost', "
            "'{\"currency\": \"IDR\"}', ARRAY['document'], ARRAY['restriction'], ARRAY['activity'], "
            "ARRAY['benefit'], ARRAY['step'], ARRAY['tip'], false, '{\"synthetic\": true}', "
            "'2026-01-01T00:00:00Z', '2025-01-01T00:00:00Z');"
            for row in expected_rows
        )
    )
    columns = pg.sql(
        "SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_name='visa_types';"
    )
    legacy_query = (
        f"SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM (SELECT {columns} FROM visa_types) v;"
    )
    before = pg.sql(legacy_query)
    for _ in range(2):
        for forward, _rollback in parts:
            pg.sql(forward)
        assert pg.sql(legacy_query) == before
    projected = pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;")
    rows = {r["code"]: r for r in json.loads(projected)}
    assert len(rows) == 28
    for expected in expected_rows:
        actual = rows[expected["code"]]
        for column in (*importer.LAW_COLUMNS, "stay_options"):
            if column == "retrieved_at":
                assert datetime.fromisoformat(actual[column]) == datetime.fromisoformat(
                    expected[column]
                )
            else:
                assert actual[column] == expected.get(column), (expected["code"], column)
    others_query = "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE family IN ('C', 'E30');"
    others = pg.sql(others_query)
    pg.sql(parts[4][1])
    assert pg.sql(legacy_query) == before
    assert pg.sql(others_query) == others
    cleared = json.loads(
        pg.sql("SELECT jsonb_agg(to_jsonb(v)) FROM visa_types v WHERE code LIKE 'E31%';")
    )
    assert len(cleared) == 10
    for row in cleared:
        for column in (*importer.LAW_COLUMNS, "stay_options"):
            assert row[column] == (
                []
                if column == "price_keys"
                else "TO_VERIFY"
                if column == "verification_status"
                else None
            )
    pg.sql(parts[4][0])
    assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == projected
    assert pg.sql(legacy_query) == before
    # A case collision must be detected before any upsert changes another row.
    pg.sql("UPDATE visa_types SET code='e31j' WHERE code='E31J';")
    collision = pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;")
    assert "non-uppercase duplicate" in pg.sql(parts[4][0], succeeds=False)
    assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == collision
    pg.sql("UPDATE visa_types SET code='E31J' WHERE code='e31j';")
    # Fresh rows get the declared category; existing legacy values above were preserved.
    pg.sql("DELETE FROM visa_types WHERE code LIKE 'E31%';")
    pg.sql(parts[4][0])
    assert (
        pg.sql(
            "SELECT count(*) FROM visa_types WHERE family='E31' AND category='KITAS/Limited Stay';"
        )
        == "10"
    )
    assert (
        pg.sql(
            "SELECT count(*) FROM visa_types WHERE code='E31' AND verification_status='TO_VERIFY' AND permit_class IS NULL;"
        )
        == "1"
    )
    assert (
        pg.sql(
            "SELECT count(*) FROM visa_types WHERE family='E31' AND itap IS NULL AND initial_stay_days IS NULL AND max_total_days IS NULL AND max_total_months IS NULL;"
        )
        == "10"
    )
    assert pg.sql(others_query) == others
