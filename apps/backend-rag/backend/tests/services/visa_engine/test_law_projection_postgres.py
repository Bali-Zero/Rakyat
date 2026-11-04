"""Every visa_types Law migration (330+) on a throwaway PostgreSQL equals ``law_projection``.

Never connects to an existing database: the cluster is the disposable one of
``test_family_e30_postgres``.
"""

import json
from pathlib import Path

from backend.services.visa_engine import law_projection
from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.test_family_e30_postgres import Cluster, pg  # noqa: F401

MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"


def _law_migrations() -> list[Path]:
    files = [
        p for p in MIGRATIONS.glob("*_visa_types_*.sql") if int(p.name.split("_", 1)[0]) >= 330
    ]
    return sorted(files, key=lambda p: int(p.name.split("_", 1)[0]))


def test_applied_migrations_equal_the_projection(pg: Cluster) -> None:  # noqa: F811
    files = _law_migrations()
    assert files[0].name == "330_visa_types_law_projection.sql"
    forwards = [p.read_text().split("-- === ROLLBACK ===", 1)[0] for p in files]
    pg.sql("DROP TABLE IF EXISTS visa_types;")
    pg.sql(forwards[0].split("ALTER TABLE visa_types", 1)[0])
    for forward in forwards:
        pg.sql(forward)
    projection = law_projection.law_projection()
    codes = ", ".join(f"'{code}'" for code in sorted(projection))
    rows = json.loads(
        pg.sql(
            f"SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE code IN ({codes});"
        )
    )
    assert [row["code"] for row in rows] == sorted(projection)
    for row in rows:
        assert law_projection.entry_from_row(row) == projection[row["code"]], row["code"]
    assert {e.family for e in projection.values()} == set(law_projection.shipped_families())
