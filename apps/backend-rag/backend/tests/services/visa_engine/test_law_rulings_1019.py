"""Owner rulings of 2026-10-19: bundle migration 344 re-projects the affected families.

Applied migrations 331..342 are immutable (prod ledger checksums). 344 re-upserts whole
families from the current cards; its rollback restores only the changed literals of the
previous applied files. The PostgreSQL test runs on the disposable PG16 fixture only.
"""

import json
from datetime import datetime
from pathlib import Path

from backend.db.migration_base import split_migration_sql
from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.test_family_e31_postgres import Cluster
from backend.tests.services.visa_engine.test_family_e31_postgres import pg as pg

BUNDLE = "344_visa_types_law_rulings_2026_10_19.sql"
MIGRATIONS = importer.MIGRATIONS_DIR
FAMILIES = ("BRIDGING", "E33", "E31", "D", "A", "B")
UNCHANGED = ("C", "E30", "E28", "E23")
# (code, columns the rulings change) - the complete rollback inventory.
CHANGED = {
    "BRIDGING": ["extendable", "law_card", "card_sha256"],
    "E33E": ["law_card", "card_sha256"],
    "E33F": ["law_card", "card_sha256"],
    "E31C": ["is_sold", "price_keys"],
    "E31D": ["is_sold", "price_keys"],
    "E31F": ["is_sold", "price_keys"],
    "E31G": ["is_sold", "price_keys"],
    "D12": ["max_total_days", "law_card", "card_sha256"],
    "A1": ["law_card", "card_sha256"],
    "B1": ["law_card", "card_sha256"],
}


def test_every_family_is_checked_against_its_latest_migration() -> None:
    assert importer.BUNDLES[BUNDLE][0] == FAMILIES
    for family in FAMILIES:
        assert importer.latest_migration(family) == BUNDLE
        assert importer.main(["--family", family, "--check", str(MIGRATIONS / BUNDLE)]) == 0
    for family in UNCHANGED:
        assert importer.latest_migration(family) != BUNDLE
    assert importer.main(["--family", "C", "--check", str(MIGRATIONS / BUNDLE)]) == 1
    assert importer.main(["--check-latest"]) == 0
    assert (MIGRATIONS / BUNDLE).read_text() == importer.emit_bundle_sql(BUNDLE)
    assert not (MIGRATIONS / "343_visa_types_law_rulings_2026_10_19.sql").exists()


def test_family_and_bundle_are_mutually_exclusive(capsys) -> None:
    import pytest

    with pytest.raises(SystemExit) as exc:
        importer.main(["--family", "D", "--bundle", BUNDLE, "--emit-sql"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_bundle_drift_is_detected(tmp_path: Path) -> None:
    drifted = tmp_path / BUNDLE
    drifted.write_text((MIGRATIONS / BUNDLE).read_text().replace("FALSE", "TRUE", 1))
    assert importer.main(["--bundle", BUNDLE, "--check", str(drifted)]) == 1
    assert importer.main(["--family", "D", "--check", str(drifted)]) == 1


def test_bundle_forward_is_each_family_and_rollback_restores_only_changed_literals() -> None:
    text = (MIGRATIONS / BUNDLE).read_text()
    assert text.count("-- === ROLLBACK ===") == 1
    # The runner's own splitter sees the same single forward/rollback boundary.
    runner_forward, runner_rollback = split_migration_sql(text)
    forward, rollback = text.split("-- === ROLLBACK ===", 1)
    assert runner_forward.strip() == forward.strip() and runner_rollback.strip() == rollback.strip()
    for family in FAMILIES:
        body = importer._family_body(family)
        assert f"\n-- ---- family {family} ----\n{body}" in forward
    assert forward.count("INSERT INTO visa_types") == sum(
        len(importer.build_rows(f)) for f in FAMILIES
    )
    assert "DELETE" not in rollback and "DROP" not in rollback and "INSERT" not in rollback
    restored = {
        line.split(": restore ", 1)[0].removeprefix("-- "): line.split(": restore ", 1)[1]
        .rsplit(" from ", 1)[0]
        .split(", ")
        for line in rollback.splitlines()
        if ": restore " in line
    }
    assert restored == CHANGED
    previous = {f: importer.FAMILY_MIGRATIONS[f][-2] for f in FAMILIES}
    assert previous == {
        "BRIDGING": "342_visa_types_law_family_bridging.sql",
        "E33": "336_visa_types_law_family_e33.sql",
        "E31": "337_visa_types_law_family_e31.sql",
        "D": "341_visa_types_law_family_d.sql",
        "A": "339_visa_types_law_family_a.sql",
        "B": "340_visa_types_law_family_b.sql",
    }


def test_ruled_values_in_the_projection() -> None:
    rows = {r["code"]: r for f in FAMILIES for r in importer.build_rows(f)}
    assert rows["BRIDGING"]["extendable"] is False
    assert rows["D12"]["max_total_days"] == 180 and rows["D12"]["max_total_months"] is None
    for code in ("E31C", "E31D", "E31F", "E31G"):
        assert rows[code]["is_sold"] is True
        assert rows[code]["price_keys"] == rows["E31E"]["price_keys"]
        assert len(rows[code]["price_keys"]) == 6
    for code in ("E33E", "E33F"):
        assert "senior_age_threshold_55_vs_60" not in rows[code]["law_card"]["to_verify"]
    assert len(rows["A1"]["law_card"]["eligible_nationalities"]["entries"]) == 19
    assert len(rows["B1"]["law_card"]["eligible_nationalities"]["entries"]) == 97
    # The DB sponsor column keeps the Law value; the agency ruling is Guide behaviour.
    assert rows["D3"]["sponsor"] == "REQUIRED" and rows["D1"]["sponsor"] == "CONDITIONAL"


def _forward(name: str) -> str:
    return (MIGRATIONS / name).read_text().split("-- === ROLLBACK ===", 1)[0]


def test_330_to_344_forward_twice_rollback_restores_342_state_and_reapply(pg: Cluster) -> None:
    # pg is a private initdb cluster on a unique Unix socket (no TCP, no existing database).
    files = sorted(
        (p.name for p in MIGRATIONS.glob("*_visa_types_*.sql")),
        key=lambda n: int(n.split("_", 1)[0]),
    )
    files = [n for n in files if 330 <= int(n.split("_", 1)[0]) <= 344]
    assert files[0] == "330_visa_types_law_projection.sql" and files[-1] == BUNDLE
    pg.sql(_forward(files[0]).split("ALTER TABLE visa_types", 1)[0])
    for name in files[:-1]:
        pg.sql(_forward(name))
    everything = "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"
    before = pg.sql(everything)
    unchanged_codes = ", ".join(f"'{r['code']}'" for f in UNCHANGED for r in importer.build_rows(f))
    unchanged_query = f"SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE code IN ({unchanged_codes});"
    unchanged_before = pg.sql(unchanged_query)
    forward, rollback = (MIGRATIONS / BUNDLE).read_text().split("-- === ROLLBACK ===", 1)
    for _ in range(2):
        pg.sql(forward)
    projected = pg.sql(everything)
    assert pg.sql(unchanged_query) == unchanged_before
    actual = {row["code"]: row for row in json.loads(projected)}
    for family in FAMILIES:
        for expected in importer.build_rows(family):
            row = actual[expected["code"]]
            for column in (*importer.LAW_COLUMNS, "stay_options"):
                if column == "retrieved_at":
                    assert datetime.fromisoformat(row[column]) == datetime.fromisoformat(
                        expected[column]
                    )
                else:
                    assert row[column] == expected[column], (expected["code"], column)
    assert actual["BRIDGING"]["extendable"] is False
    assert actual["D12"]["max_total_days"] == 180
    assert actual["E31C"]["price_keys"] and actual["E31C"]["is_sold"] is True
    pg.sql(rollback)
    assert pg.sql(everything) == before
    pg.sql(forward)
    assert pg.sql(everything) == projected
