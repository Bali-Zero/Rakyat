"""Migration 334 on a throwaway PostgreSQL; never connects to an existing database."""

from pathlib import Path

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.test_family_e30_postgres import Cluster, pg  # noqa: F401

MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
OLD_A, OLD_B = "E30A - Student KITAS (Research)", "E30B - Student KITAS (Training)"
NEW_A = "E30A - Student KITAS (Primary & Secondary Education)"
NEW_B = "E30B - Student KITAS (Higher Education)"


def _names(pg: Cluster) -> str:
    return pg.sql("SELECT string_agg(code || '=' || name, '|' ORDER BY code) FROM visa_types;")


def _reset(pg: Cluster, a: str, b: str) -> None:
    pg.sql(
        "DROP TABLE IF EXISTS visa_types; "
        "CREATE TABLE visa_types (code text PRIMARY KEY, name text NOT NULL, "
        "last_updated timestamptz NOT NULL DEFAULT '2026-01-01');"
        f"INSERT INTO visa_types (code, name) VALUES ('E30A', '{a}'), ('E30B', '{b}'), "
        "('E30', 'E30 untouched');"
    )


def test_forward_twice_rollback_and_manual_edit(pg: Cluster) -> None:  # noqa: F811
    text = (MIGRATIONS / "334_visa_types_e30_official_names.sql").read_text()
    forward, rollback = text.split("-- === ROLLBACK ===", 1)
    _reset(pg, OLD_A, OLD_B)
    for _ in range(2):
        pg.sql(forward)
        assert _names(pg) == f"E30={'E30 untouched'}|E30A={NEW_A}|E30B={NEW_B}"
    assert pg.sql("SELECT count(*) FROM visa_types WHERE last_updated > '2026-06-01';") == "2"
    pg.sql(rollback)
    assert _names(pg) == f"E30=E30 untouched|E30A={OLD_A}|E30B={OLD_B}"
    # A manually edited name is never clobbered, forward or backward.
    _reset(pg, "E30A custom", OLD_B)
    pg.sql(forward)
    assert _names(pg) == f"E30=E30 untouched|E30A=E30A custom|E30B={NEW_B}"
    pg.sql(rollback)
    assert _names(pg) == f"E30=E30 untouched|E30A=E30A custom|E30B={OLD_B}"
