"""Real, isolated PostgreSQL proof; never connects to an existing database."""

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
    "336_visa_types_law_family_e33.sql",
)


class Cluster:
    """psql is pinned to the throwaway socket, port, user and database."""

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
                "55486",
                "-U",
                "e33_test",
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
            env={key: value for key, value in os.environ.items() if not key.startswith("PG")},
        )
        if succeeds:
            assert result.returncode == 0, result.stderr
        else:
            assert result.returncode != 0, "Invalid data unexpectedly accepted"
        return result.stdout.strip() if succeeds else result.stderr


@pytest.fixture(scope="module")
def pg() -> Iterator[Cluster]:
    for binary in ("initdb", "pg_ctl", "psql"):
        if not shutil.which(binary):
            pytest.skip(f"Throwaway PostgreSQL unavailable: {binary} missing")
    # Short socket path, independent of pytest's potentially long temporary paths.
    with tempfile.TemporaryDirectory(prefix="pg", dir="/tmp") as directory:
        cluster = Cluster(Path(directory))
        initialized = subprocess.run(
            [
                "initdb",
                "-D",
                str(cluster.data),
                "-A",
                "trust",
                "-U",
                "e33_test",
                "--no-locale",
                "--encoding=UTF8",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if initialized.returncode and "Operation not permitted" in initialized.stderr:
            pytest.skip(f"Throwaway PostgreSQL blocked by sandbox: {initialized.stderr.strip()}")
        assert initialized.returncode == 0, initialized.stderr
        start = subprocess.run(
            [
                "pg_ctl",
                "-D",
                str(cluster.data),
                "-l",
                str(cluster.base / "server.log"),
                "-o",
                f"-F -k {cluster.base} -h '' -p 55486",
                "-w",
                "start",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert start.returncode == 0, start.stderr + (cluster.base / "server.log").read_text()
        try:
            version = cluster.sql("SHOW server_version_num;")
            assert int(version) >= 160000
            yield cluster
        finally:
            subprocess.run(
                ["pg_ctl", "-D", str(cluster.data), "-m", "immediate", "-w", "stop"],
                capture_output=True,
                check=True,
                timeout=30,
            )


def test_forward_twice_rollback_reapply_case_guard_and_fresh_rows(pg: Cluster) -> None:
    parts = [(MIGRATIONS / name).read_text().split("-- === ROLLBACK ===", 1) for name in FILES]
    pg.sql(parts[0][0].split("ALTER TABLE visa_types", 1)[0])
    assert (
        pg.sql("SELECT count(*) FROM information_schema.columns WHERE table_name='visa_types';")
        == "24"
    )
    codes = sorted(r["code"] for family in ("C", "E30", "E33") for r in rows_as_of(family, 336))
    pg.sql(
        "\n".join(
            "INSERT INTO visa_types (code, name, category, duration, extensions, total_stay, renewable, "
            "processing_time_normal, processing_time_express, processing_timeline, cost_visa, cost_extension, "
            "cost_details, requirements, restrictions, allowed_activities, benefits, process_steps, tips, "
            "foreign_eligible, metadata, last_updated, created_at) "
            f"VALUES ('{code}', 'legacy {code}', 'legacy category', 'duration', 'extensions', 'total', "
            "true, 'normal', 'express', '{\"normal\": 5}', 'cost', 'extension cost', '{\"currency\": \"IDR\"}', "
            "ARRAY['document'], ARRAY['restriction'], ARRAY['activity'], ARRAY['benefit'], ARRAY['step'], "
            "ARRAY['tip'], false, '{\"synthetic\": true}', '2026-01-01T00:00:00Z', '2025-01-01T00:00:00Z');"
            for code in codes
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
    rows = json.loads(projected)
    assert len(rows) == 26
    for expected in rows_as_of("E33", 336):
        actual = next(r for r in rows if r["code"] == expected["code"])
        for column in (*importer.LAW_COLUMNS, "stay_options"):
            if column == "retrieved_at":
                assert datetime.fromisoformat(actual[column]) == datetime.fromisoformat(
                    expected[column]
                )
            else:
                assert actual[column] == expected.get(column), (expected["code"], column)
    others_query = "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE family IN ('C', 'E30');"
    others_before = pg.sql(others_query)
    pg.sql(parts[4][1])
    assert (
        pg.sql(
            "SELECT count(*) FROM visa_types WHERE code LIKE 'E33%' AND family IS NULL "
            "AND permit_class IS NULL AND sponsor IS NULL AND itap IS NULL "
            "AND stay_options IS NULL AND verification_status='TO_VERIFY';"
        )
        == "8"
    )
    assert pg.sql(legacy_query) == before
    assert pg.sql(others_query) == others_before
    pg.sql(parts[4][0])
    assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == projected
    # Every casing collision must halt BEFORE any upsert can write another row.
    for code in sorted(r["code"] for r in rows_as_of("E33", 336)):
        pg.sql(f"UPDATE visa_types SET code=lower(code) WHERE code='{code}';")
        snapshot = pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;")
        assert "non-uppercase duplicate" in pg.sql(parts[4][0], succeeds=False)
        assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == snapshot
        pg.sql(f"UPDATE visa_types SET code=upper(code) WHERE code='{code.lower()}';")
    pg.sql("DELETE FROM visa_types WHERE code LIKE 'E33%';")
    pg.sql(parts[4][0])
    assert (
        pg.sql(
            "SELECT count(*) FROM visa_types WHERE family='E33' AND category='KITAS/Limited Stay';"
        )
        == "8"
    )
    assert pg.sql("SELECT count(*) FROM visa_types WHERE family='E33' AND itap IS NOT NULL;") == "0"
    assert (
        pg.sql(
            "SELECT count(*) FROM visa_types WHERE family='E33' AND verification_status='VERIFIED';"
        )
        == "7"
    )
    assert (
        pg.sql(
            "SELECT permit_class IS NULL AND verification_status='TO_VERIFY' FROM visa_types WHERE code='E33D';"
        )
        == "t"
    )
    assert pg.sql(others_query) == others_before
