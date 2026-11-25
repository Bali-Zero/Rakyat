"""Rollback of an already used delivery workflow must preserve the staff evidence."""

from pathlib import Path

import asyncpg
import pytest

from backend.tests.db.test_kita_document_delivery_pg import pool  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "migration,table,seed",
    [
        (
            "383_practice_delivery.sql",
            "practice_delivery_titles",
            "INSERT INTO practice_delivery_titles VALUES(7,'visa_c1_tourism',ARRAY['Issued permit'])",
        ),
        (
            "385_practice_visa_followups.sql",
            "practice_visa_followups",
            """INSERT INTO practice_visa_followups(practice_id,service_code,slot_key,started_on,due_on,allow_postpone)
               VALUES(7,'visa_c1_tourism','document_2',current_date,current_date+30,true)""",
        ),
    ],
)
async def test_rollback_preserves_recorded_evidence(pool, migration, table, seed):  # noqa: F811
    directory = Path(__file__).resolve().parents[2] / "db/migrations_v2"
    rollback = (directory / migration).read_text().split("-- === ROLLBACK ===", 1)[1]
    async with pool.acquire() as conn:
        await conn.execute(seed)
        with pytest.raises(asyncpg.RaiseError, match="Preserve recorded practice evidence"):
            async with conn.transaction():
                await conn.execute(rollback)
        assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE practice_id=7") == 1
