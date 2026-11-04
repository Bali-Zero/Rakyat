"""Family A: real throwaway PostgreSQL proof; never uses an existing database."""

import json
from collections.abc import Iterator
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import pytest

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import rows_as_of
from backend.tests.services.visa_engine.test_family_e30_postgres import Cluster
from backend.tests.services.visa_engine.test_family_e30_postgres import pg as isolated_pg

FAMILY = "A"
MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
MIGRATION = MIGRATIONS / "339_visa_types_law_family_a.sql"


@pytest.fixture(scope="module")
def pg() -> Iterator[Cluster]:
    """Keep the established isolation and report host shared-memory exhaustion explicitly."""
    cluster = isolated_pg.__wrapped__()
    try:
        ready = next(cluster)
    except AssertionError as error:
        if "could not create shared memory segment: No space left on device" in str(error):
            pytest.skip("Throwaway PostgreSQL unavailable: host shared-memory capacity exhausted")
        raise
    try:
        yield ready
    finally:
        with suppress(StopIteration):
            next(cluster)


def test_forward_twice_rollback_reapply_and_legacy_preservation(pg: Cluster) -> None:
    # Reuse only the established isolated-cluster fixture, never its test or database state.
    schema = (
        (MIGRATIONS / "330_visa_types_law_projection.sql")
        .read_text()
        .split("-- === ROLLBACK ===", 1)[0]
    )
    options = (
        (MIGRATIONS / "332_visa_types_stay_options.sql")
        .read_text()
        .split("-- === ROLLBACK ===", 1)[0]
    )
    forward, rollback = MIGRATION.read_text().split("-- === ROLLBACK ===", 1)
    ddl = schema.split("ALTER TABLE visa_types", 1)[0]
    pg.sql(ddl)
    assert (
        pg.sql("SELECT count(*) FROM information_schema.columns WHERE table_name='visa_types';")
        == "24"
    )
    expected_rows = rows_as_of(FAMILY, 339)
    # Distinct synthetic legacy values in all 23 non-code columns, including arrays/JSON/timestamps.
    for row in expected_rows:
        code = row["code"]
        pg.sql(
            "INSERT INTO visa_types (code, name, category, duration, extensions, total_stay, renewable, "
            "processing_time_normal, processing_time_express, processing_timeline, cost_visa, cost_extension, "
            "cost_details, requirements, restrictions, allowed_activities, benefits, process_steps, tips, "
            "foreign_eligible, metadata, last_updated, created_at) "
            f"VALUES ('{code}', 'legacy {code}', 'legacy category', 'original duration', 'original extensions', "
            "'original total', true, 'normal', 'express', '{\"normal\":5}', 'original cost', 'extension cost', "
            "'{\"currency\":\"IDR\"}', ARRAY['document'], ARRAY['restriction'], ARRAY['activity'], "
            "ARRAY['benefit'], ARRAY['step'], ARRAY['tip'], false, '{\"synthetic\":true}', "
            "'2026-01-01T00:00:00Z', '2025-01-01T00:00:00Z');"
        )
    columns = pg.sql(
        "SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name='visa_types';"
    )
    legacy_query = (
        f"SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM (SELECT {columns} FROM visa_types) v;"
    )
    before = pg.sql(legacy_query)
    pg.sql(schema)
    pg.sql(options)
    for _ in range(2):
        pg.sql(forward)
        assert pg.sql(legacy_query) == before
    projection_query = "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"
    projection = pg.sql(projection_query)
    actual_rows = {row["code"]: row for row in json.loads(projection)}
    assert len(actual_rows) == len(expected_rows)
    for expected in expected_rows:
        actual = actual_rows[expected["code"]]
        for column in (*importer.LAW_COLUMNS, "stay_options"):
            if column == "retrieved_at":
                assert datetime.fromisoformat(actual[column]) == datetime.fromisoformat(
                    expected[column]
                )
            else:
                assert actual[column] == expected[column], (expected["code"], column)
    # Unrelated family is untouched by both the forward projection and rollback.
    pg.sql(
        "INSERT INTO visa_types (code, name, category, family, metadata) VALUES ('Z_TEST', 'unrelated', 'synthetic', 'SERVICE', '{\"untouched\":true}');"
    )
    unrelated = pg.sql("SELECT to_jsonb(v) FROM visa_types v WHERE code='Z_TEST';")
    pg.sql(forward)
    pg.sql(rollback)
    assert pg.sql("SELECT to_jsonb(v) FROM visa_types v WHERE code='Z_TEST';") == unrelated
    for actual in json.loads(
        pg.sql("SELECT jsonb_agg(to_jsonb(v)) FROM visa_types v WHERE code <> 'Z_TEST';")
    ):
        for column in (*importer.LAW_COLUMNS, "stay_options"):
            assert actual[column] == (
                []
                if column == "price_keys"
                else "TO_VERIFY"
                if column == "verification_status"
                else None
            )
    pg.sql("DELETE FROM visa_types WHERE code='Z_TEST';")
    assert pg.sql(legacy_query) == before
    pg.sql(forward)
    assert pg.sql(projection_query) == projection
    # Case guard must run before any upsert mutates rows.
    code = expected_rows[0]["code"]
    pg.sql(f"UPDATE visa_types SET code=lower(code) WHERE code='{code}';")
    collision = pg.sql(projection_query)
    assert "non-uppercase duplicate" in pg.sql(forward, succeeds=False)
    assert pg.sql(projection_query) == collision
    pg.sql(f"UPDATE visa_types SET code=upper(code) WHERE code='{code.lower()}';")
    # Check fresh inserts as well as legacy-conflict updates.
    pg.sql("DELETE FROM visa_types;")
    pg.sql(forward)
    assert pg.sql(
        f"SELECT count(*) FROM visa_types WHERE family='{FAMILY}' AND category='Visit Visa';"
    ) == str(len(expected_rows))
