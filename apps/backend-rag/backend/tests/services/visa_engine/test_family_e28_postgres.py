"""E28 projection on a disposable PG16+ cluster; no existing database connections."""

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

MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
FILES = (
    "330_visa_types_law_projection.sql",
    "331_visa_types_law_family_c.sql",
    "332_visa_types_stay_options.sql",
    "333_visa_types_law_family_e30.sql",
    "335_visa_types_law_family_e28.sql",
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
                "55485",
                "-U",
                "e28_test",
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
                "e28_test",
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
                f"-F -k {cluster.base} -h '' -p 55485",
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


def parts() -> list[list[str]]:
    return [(MIGRATIONS / name).read_text().split("-- === ROLLBACK ===", 1) for name in FILES]


def test_forward_twice_rollback_reapply_preserve_every_legacy_column(pg: Cluster) -> None:
    migrations = parts()
    pg.sql(migrations[0][0].split("ALTER TABLE visa_types", 1)[0])
    assert (
        pg.sql("SELECT count(*) FROM information_schema.columns WHERE table_name='visa_types';")
        == "24"
    )
    codes = sorted(r["code"] for family in ("C", "E30", "E28") for r in importer.build_rows(family))
    pg.sql(
        "\n".join(
            "INSERT INTO visa_types (code, name, category, duration, extensions, total_stay, renewable, "
            "processing_time_normal, processing_time_express, processing_timeline, cost_visa, cost_extension, "
            "cost_details, requirements, restrictions, allowed_activities, benefits, process_steps, tips, "
            "foreign_eligible, metadata, last_updated, created_at) "
            f"VALUES ('{code}', 'legacy {code}', 'legacy category', 'original duration', 'original extensions', "
            "'original total', true, 'normal', 'express', '{\"normal\": 5}', 'original cost', 'extension cost', "
            "'{\"currency\": \"IDR\"}', ARRAY['document'], ARRAY['restriction'], ARRAY['activity'], "
            "ARRAY['benefit'], ARRAY['step'], ARRAY['tip'], false, '{\"synthetic\": true}', "
            "'2026-01-01T00:00:00Z', '2025-01-01T00:00:00Z');"
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
        for forward, _rollback in migrations:
            pg.sql(forward)
        assert pg.sql(legacy_query) == before
    projected = pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;")
    rows = json.loads(projected)
    assert len(rows) == 26
    for family in ("C", "E30", "E28"):
        for expected in importer.build_rows(family):
            actual = next(row for row in rows if row["code"] == expected["code"])
            for column in (*importer.LAW_COLUMNS, "stay_options"):
                if column == "retrieved_at":
                    assert datetime.fromisoformat(actual[column]) == datetime.fromisoformat(
                        expected[column]
                    )
                else:
                    assert actual[column] == expected.get(column), (expected["code"], column)
    other_query = (
        "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE code NOT LIKE 'E28%';"
    )
    other_families = pg.sql(other_query)
    pg.sql(migrations[-1][1])
    cleared = json.loads(
        pg.sql(
            "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE code LIKE 'E28%';"
        )
    )
    assert len(cleared) == 8
    for row in cleared:
        for column in (*importer.LAW_COLUMNS, "stay_options"):
            assert row[column] == (
                []
                if column == "price_keys"
                else "TO_VERIFY"
                if column == "verification_status"
                else None
            )
    assert pg.sql(legacy_query) == before
    assert pg.sql(other_query) == other_families
    pg.sql(migrations[-1][0])
    assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == projected
    assert pg.sql(legacy_query) == before


@pytest.mark.parametrize("code", ["E28", "E28A", "E28B", "E28C", "E28D", "E28E", "E28F", "E28G"])
def test_case_collision_fails_before_any_upsert(pg: Cluster, code: str) -> None:
    for forward, _rollback in parts():
        pg.sql(forward)
    pg.sql(f"UPDATE visa_types SET code=lower(code) WHERE code='{code}';")
    before = pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;")
    try:
        assert "non-uppercase duplicate" in pg.sql(parts()[-1][0], succeeds=False)
        assert pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;") == before
    finally:
        pg.sql(f"UPDATE visa_types SET code=upper(code) WHERE code='{code.lower()}';")


def test_fresh_e28_rows_get_law_projection_without_losing_other_families(pg: Cluster) -> None:
    for forward, _rollback in parts():
        pg.sql(forward)
    other_query = (
        "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE code NOT LIKE 'E28%';"
    )
    other_families = pg.sql(other_query)
    pg.sql("DELETE FROM visa_types WHERE code LIKE 'E28%';")
    pg.sql(parts()[-1][0])
    rows = json.loads(
        pg.sql("SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE family='E28';")
    )
    assert len(rows) == 8
    for row in rows:
        assert row["category"] == "KITAS/Limited Stay"
        assert row["name"] == row["official_name_id"]
        if row["code"] in {"E28", "E28E"}:
            assert row["verification_status"] == "TO_VERIFY" and row["permit_class"] is None
        else:
            assert row["verification_status"] == "VERIFIED" and row["permit_class"] == "ITAS"
    assert pg.sql(other_query) == other_families
