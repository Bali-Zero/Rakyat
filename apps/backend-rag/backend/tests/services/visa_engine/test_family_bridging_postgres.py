"""Migration 342 on the existing disposable PG16+ fixture; no existing DB access."""

import json
from datetime import datetime
from pathlib import Path

from backend.services.visa_engine.families import importer
from backend.tests.services.visa_engine.law_history import rows_as_of
from backend.tests.services.visa_engine.test_family_e31_postgres import Cluster
from backend.tests.services.visa_engine.test_family_e31_postgres import pg as pg

MIGRATIONS = Path(importer.__file__).parents[3] / "db" / "migrations_v2"
PREVIOUS = (
    "331_visa_types_law_family_c.sql",
    "333_visa_types_law_family_e30.sql",
    "335_visa_types_law_family_e28.sql",
    "336_visa_types_law_family_e33.sql",
    "337_visa_types_law_family_e31.sql",
)


def test_forward_twice_rollback_reapply_case_guard_and_fresh_insert(pg: Cluster) -> None:
    # pg creates a private initdb cluster with a unique /tmp Unix socket, no TCP,
    # pinned user/database/port and scrubbed PG* environment. PG <16 is skipped.
    projection = (MIGRATIONS / "330_visa_types_law_projection.sql").read_text()
    legacy_ddl = projection.split("ALTER TABLE visa_types", 1)[0]
    pg.sql(legacy_ddl)
    assert (
        pg.sql("SELECT count(*) FROM information_schema.columns WHERE table_name='visa_types';")
        == "24"
    )
    legacy_columns = pg.sql(
        "SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_name='visa_types';"
    )
    pg.sql(projection.split("-- === ROLLBACK ===", 1)[0])
    pg.sql(
        (MIGRATIONS / "332_visa_types_stay_options.sql")
        .read_text()
        .split("-- === ROLLBACK ===", 1)[0]
    )
    for filename in PREVIOUS:
        pg.sql((MIGRATIONS / filename).read_text().split("-- === ROLLBACK ===", 1)[0])
    pg.sql(
        "INSERT INTO visa_types (code, name, category, duration, extensions, total_stay, renewable, "
        "processing_time_normal, processing_time_express, processing_timeline, cost_visa, cost_extension, "
        "cost_details, requirements, restrictions, allowed_activities, benefits, process_steps, tips, "
        "foreign_eligible, metadata, last_updated, created_at) "
        "VALUES ('BRIDGING', 'legacy bridging', 'legacy category', 'original duration', 'original extensions', "
        "'original total', true, 'normal', 'express', '{\"normal\": 5}', 'original cost', 'extension cost', "
        "'{\"currency\": \"IDR\"}', ARRAY['document'], ARRAY['restriction'], ARRAY['activity'], "
        "ARRAY['benefit'], ARRAY['step'], ARRAY['tip'], false, '{\"synthetic\": true}', "
        "'2026-01-01T00:00:00Z', '2025-01-01T00:00:00Z');"
    )
    legacy_query = f"SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM (SELECT {legacy_columns} FROM visa_types) v;"
    others_query = (
        "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v WHERE code <> 'BRIDGING';"
    )
    target_query = "SELECT to_jsonb(v) FROM visa_types v WHERE code='BRIDGING';"
    legacy_before = pg.sql(legacy_query)
    others_before = pg.sql(others_query)
    forward, rollback = (
        (MIGRATIONS / "342_visa_types_law_family_bridging.sql")
        .read_text()
        .split("-- === ROLLBACK ===", 1)
    )
    for _ in range(2):
        pg.sql(forward)
        assert pg.sql(legacy_query) == legacy_before
        assert pg.sql(others_query) == others_before
    projected = pg.sql(target_query)
    actual = json.loads(projected)
    (expected,) = rows_as_of("BRIDGING", 342)
    for column in (*importer.LAW_COLUMNS, "stay_options"):
        if column == "retrieved_at":
            assert datetime.fromisoformat(actual[column]) == datetime.fromisoformat(
                expected[column]
            )
        else:
            assert actual[column] == expected[column], column
    assert actual["permit_class"] == "BRIDGING"
    assert actual["apply_channel"] == "ONSHORE"
    assert actual["sponsor"] is actual["entry_type"] is actual["itap"] is None
    assert actual["extendable"] is actual["max_total_days"] is actual["max_total_months"] is None
    pg.sql(rollback)
    cleared = json.loads(pg.sql(target_query))
    for column in (*importer.LAW_COLUMNS, "stay_options"):
        assert cleared[column] == (
            []
            if column == "price_keys"
            else "TO_VERIFY"
            if column == "verification_status"
            else None
        ), column
    assert pg.sql(legacy_query) == legacy_before
    assert pg.sql(others_query) == others_before
    pg.sql(forward)
    assert pg.sql(target_query) == projected
    pg.sql("UPDATE visa_types SET code='bridging' WHERE code='BRIDGING';")
    all_query = "SELECT jsonb_agg(to_jsonb(v) ORDER BY code) FROM visa_types v;"
    before_collision = pg.sql(all_query)
    assert "non-uppercase duplicate" in pg.sql(forward, succeeds=False)
    assert pg.sql(all_query) == before_collision
    pg.sql("UPDATE visa_types SET code='BRIDGING' WHERE code='bridging';")
    pg.sql("DELETE FROM visa_types WHERE code='BRIDGING';")
    pg.sql(forward)
    fresh = json.loads(pg.sql(target_query))
    assert fresh["name"] == fresh["official_name_id"] == "Izin Tinggal Peralihan"
    assert fresh["category"] == "Special Visa"
    assert fresh["verification_status"] == "VERIFIED"
    assert fresh["stay_options"] == [{"value": 60, "unit": "DAY"}]
    assert pg.sql(others_query) == others_before
