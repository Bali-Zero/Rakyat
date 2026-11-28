"""Migration 398 on a migrated schema: the tax catalogue as the owner ruled it (2026-11-27).

`db_tx` is a rolled-back transaction on a database where `apply-all` has run, so the first
tests read what the migration left; the others re-run the forward SQL (twice) and the rollback
inside the same transaction.
"""

from __future__ import annotations

import asyncpg
import pytest

from backend.db.migration_base import split_migration_sql
from backend.services.crm.delivery_catalog import delivery_policy
from backend.tests.services.pricing.test_tax_catalogue_398_release_order import MIGRATION
from backend.tests.services.pricing.test_tax_catalogue_2026_11_28 import (
    NEW_BANDS,
    NEW_SERVICES,
    SOURCE_OF_NEW_BAND,
    WIDE_CODES,
    expected_rates,
)

pytestmark = pytest.mark.integration

FORWARD, ROLLBACK = split_migration_sql(MIGRATION.read_text(encoding="utf-8"))


async def _rates(conn: asyncpg.Connection) -> dict[str, int]:
    rows = await conn.fetch(
        "SELECT practice_type_code, amount_idr FROM hr_bonus_rates "
        "WHERE practice_type_code LIKE 'tax\\_%' AND is_active AND notes LIKE '%tax catalogue'"
    )
    return {r["practice_type_code"]: int(r["amount_idr"]) for r in rows}


async def _monthly(conn: asyncpg.Connection) -> dict[str, bool]:
    rows = await conn.fetch(
        "SELECT code, is_active FROM practice_types WHERE code ~ '^tax_monthly_(basic|bundled)_'"
    )
    return {r["code"]: r["is_active"] for r in rows}


@pytest.mark.asyncio
async def test_the_migrated_schema_has_18_active_bands_and_4_inactive_wide_codes(db_tx) -> None:
    monthly = await _monthly(db_tx)
    assert sum(monthly.values()) == 18 and len(monthly) == 22
    assert {code for code, active in monthly.items() if not active} == set(WIDE_CODES)
    # The wide codes stay resolvable for the practices that already use them.
    for code in WIDE_CODES:
        assert await db_tx.fetchval("SELECT id FROM practice_types WHERE code=$1", code)
        assert delivery_policy(code) is not None


@pytest.mark.asyncio
async def test_the_migrated_schema_carries_the_38_rates_and_no_rate_for_registration(
    db_tx,
) -> None:
    assert await _rates(db_tx) == expected_rates()
    assert not await db_tx.fetchval(
        "SELECT count(*) FROM hr_bonus_rates WHERE practice_type_code = ANY($1::text[])",
        ["tax_registration", *WIDE_CODES],
    )
    # Every rate starts on 2026-11-01 and carries no forfeit rule.
    assert not await db_tx.fetchval(
        "SELECT count(*) FROM hr_bonus_rates WHERE notes LIKE '%tax catalogue' "
        "AND (effective_from <> DATE '2026-11-01' OR forfeit_below_idr IS NOT NULL "
        "OR effective_to IS NOT NULL)"
    )


@pytest.mark.asyncio
async def test_every_new_band_has_the_category_and_price_of_the_list_band_that_contains_it(
    db_tx,
) -> None:
    for family in ("basic", "bundled"):
        for band in NEW_BANDS:
            new = await db_tx.fetchrow(
                "SELECT category, base_price FROM practice_types WHERE code=$1",
                f"tax_monthly_{family}_{band}",
            )
            src = await db_tx.fetchrow(
                "SELECT category, base_price FROM practice_types WHERE code=$1",
                f"tax_monthly_{family}_{SOURCE_OF_NEW_BAND[band]}",
            )
            assert new and src and dict(new) == dict(src), (family, band)


@pytest.mark.asyncio
async def test_the_new_services_have_the_ruled_price_category_and_a_delivery_slot(db_tx) -> None:
    for code, (name, price, _bonus) in NEW_SERVICES.items():
        row = await db_tx.fetchrow(
            "SELECT name, category, base_price, is_active FROM practice_types WHERE code=$1", code
        )
        assert row is not None, code
        assert row["name"] == name and row["category"] == "tax" and row["is_active"], code
        assert (None if row["base_price"] is None else int(row["base_price"])) == price, code
        assert delivery_policy(code) is not None, code
    renamed = await db_tx.fetchrow(
        "SELECT name, base_price FROM practice_types WHERE code='tax_npwp_personal_coretax'"
    )
    assert renamed["name"] == "NPWP Personal + Coretax Activation (with KITAS)"
    assert int(renamed["base_price"]) == 1000000


@pytest.mark.asyncio
async def test_forward_twice_changes_nothing_and_keeps_a_price_staff_changed(db_tx) -> None:
    await db_tx.execute(
        "UPDATE practice_types SET base_price = 7000000 WHERE code='tax_monthly_bundled_250_300'"
    )
    await db_tx.execute(
        "UPDATE hr_bonus_rates SET amount_idr = 1 WHERE practice_type_code='tax_meeting'"
    )
    before = await db_tx.fetchval("SELECT count(*) FROM practice_types")
    await db_tx.execute(FORWARD)
    await db_tx.execute(FORWARD)
    assert await db_tx.fetchval("SELECT count(*) FROM practice_types") == before
    assert await _rates(db_tx) == expected_rates()
    assert sum((await _monthly(db_tx)).values()) == 18
    # An upsert of the catalogue never rewrites a price that staff changed since.
    assert (
        int(
            await db_tx.fetchval(
                "SELECT base_price FROM practice_types WHERE code='tax_monthly_bundled_250_300'"
            )
        )
        == 7000000
    )


@pytest.mark.asyncio
async def test_the_postcondition_refuses_a_catalogue_that_does_not_read_as_ruled(db_tx) -> None:
    # A wide code that stayed active (a hand edit between two runs) must stop the migration.
    await db_tx.execute("UPDATE practice_types SET is_active = true WHERE code = $1", WIDE_CODES[0])
    forward = FORWARD.replace(
        "UPDATE practice_types\nSET is_active = false,",
        "UPDATE practice_types\nSET is_active = true,",
    )
    assert forward != FORWARD
    with pytest.raises(asyncpg.RaiseError, match="migration 398"):
        async with db_tx.transaction():
            await db_tx.execute(forward)


async def _snapshot(conn: asyncpg.Connection) -> tuple[list, list]:
    types = await conn.fetch(
        "SELECT code, name, description, category, base_price, is_active FROM practice_types "
        "ORDER BY code"
    )
    rates = await conn.fetch(
        "SELECT practice_type_code, amount_idr FROM hr_bonus_rates ORDER BY practice_type_code"
    )
    return [tuple(r) for r in types], [tuple(r) for r in rates]


@pytest.mark.asyncio
async def test_the_rollback_restores_the_pre_398_catalogue_when_nothing_references_it(
    db_tx,
) -> None:
    await db_tx.execute(ROLLBACK)
    types, rates = await _snapshot(db_tx)
    by_code = {t[0]: t for t in types}
    new_codes = {f"tax_monthly_{f}_{b}" for f in ("basic", "bundled") for b in NEW_BANDS}
    new_codes |= set(NEW_SERVICES)
    # The 24 new rows are DELETED, not left inactive; the old ones are back as they were.
    assert not new_codes & set(by_code)
    assert all(by_code[code][5] for code in WIDE_CODES)
    assert by_code["tax_npwp_personal_coretax"][1] == "NPWP Personal + Coretax Activation"
    assert not {r[0] for r in rates} & set(expected_rates())
    # Forward then rollback again lands on exactly the same state.
    await db_tx.execute(FORWARD)
    assert await _rates(db_tx) == expected_rates()
    assert await _snapshot(db_tx) != (types, rates)
    await db_tx.execute(ROLLBACK)
    assert await _snapshot(db_tx) == (types, rates)


@pytest.mark.asyncio
async def test_the_rollback_keeps_a_new_type_a_practice_uses_inactive_and_says_so(db_tx) -> None:
    client = await db_tx.fetchval(
        "INSERT INTO clients(full_name) VALUES ('Synthetic Client') RETURNING id"
    )
    await db_tx.execute(
        "INSERT INTO practices(client_id, status, practice_type_code) "
        "VALUES ($1, 'on_process', 'tax_sertel')",
        client,
    )
    notices: list[str] = []
    db_tx.add_log_listener(lambda _c, message: notices.append(message.message))
    await db_tx.execute(ROLLBACK)
    kept = await db_tx.fetch("SELECT code, is_active FROM practice_types WHERE code = 'tax_sertel'")
    assert [(r["code"], r["is_active"]) for r in kept] == [("tax_sertel", False)]
    assert not await db_tx.fetchval("SELECT count(*) FROM practice_types WHERE code='tax_meeting'")
    assert any("tax_sertel" in n for n in notices)
    assert not any("tax_meeting" in n for n in notices)


@pytest.mark.asyncio
async def test_the_rollback_refuses_once_a_receipt_exists_for_a_priced_code(db_tx) -> None:
    client = await db_tx.fetchval(
        "INSERT INTO clients(full_name) VALUES ('Synthetic Client') RETURNING id"
    )
    practice = await db_tx.fetchval(
        "INSERT INTO practices(client_id, status, practice_type_code) "
        "VALUES ($1, 'completed', 'tax_meeting') RETURNING id",
        client,
    )
    await db_tx.execute(
        """INSERT INTO hr_delivery_bonus_events(practice_id, service_code, event_kind, earned_at, awarded_at)
           VALUES ($1, 'tax_meeting', 'service_delivered', now(), now())""",
        practice,
    )
    with pytest.raises(asyncpg.RaiseError, match="Preserve the receipts"):
        async with db_tx.transaction():
            await db_tx.execute(ROLLBACK)
