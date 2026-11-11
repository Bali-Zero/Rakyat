"""HRService card read against a mocked pool (real-Postgres proofs: tests/migrations)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService


def _pool():
    conn = AsyncMock()
    conn.transaction = MagicMock(
        return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    )
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


@pytest.mark.asyncio
async def test_get_card_for_member_is_keyed_by_primary_key_with_explicit_columns():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile": {}})
    assert await HRService(pool).get_card_for_member("tm-1") == {"id": 7, "profile": {}}
    sql, arg = conn.fetchrow.await_args.args
    assert arg == "tm-1" and "WHERE tm.id = $1" in sql
    assert "e.*" not in sql and "notes" not in sql and "LOWER" not in sql and "email" not in sql
    assert "COALESCE(tm.role, '') <> 'client'" in sql
    assert "tm.active = true" in sql
    conn.fetchrow = AsyncMock(return_value=None)
    assert await HRService(pool).get_card_for_member("tm-2") is None


@pytest.mark.asyncio
async def test_a_read_failure_is_a_code_without_driver_text():
    pool, conn = _pool()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("ZQXLEAK"))
    with pytest.raises(HRCardError) as exc:
        await HRService(pool).get_card_for_member("tm-1")
    assert exc.value.code is HRCardCode.DB_ERROR and "ZQXLEAK" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_the_legacy_employee_shape_never_carries_the_profile():
    assert HRService._legacy_row({"id": 1, "profile": {"nik": "x"}, "npwp": "n"}) == {
        "id": 1,
        "npwp": "n",
    }
