"""HRService card methods against a mocked pool (real-Postgres proofs: tests/migrations)."""

from datetime import date
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


@pytest.mark.asyncio
async def test_merge_card_on_coalesces_columns_and_passes_a_dict_to_the_codec():
    _, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    assert await HRService(None).merge_card_on(conn, "tm-1", {"npwp": "N-1"}, {"unit": "u"}) == 7
    sql, *params = conn.fetchrow.await_args.args
    assert "COALESCE($3, npwp)" in sql and "jsonb_typeof(profile) = 'object'" in sql
    assert "base_salary_idr" not in sql and "notes" not in sql
    assert params[0] == "tm-1" and params[2] == "N-1" and params[1] is None
    assert params[10] == {"unit": "u"}


@pytest.mark.asyncio
async def test_merge_card_on_inserts_then_refuses_a_missing_hire_date():
    _, conn = _pool()
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": 9, "profile_type": "object"}])
    assert (
        await HRService(None).merge_card_on(conn, "tm-9", {"hire_date": date(2025, 2, 1)}, {}) == 9
    )
    assert "NOT EXISTS" in conn.fetchrow.await_args_list[1].args[0]
    conn.fetchrow = AsyncMock(return_value=None)
    with pytest.raises(HRCardError) as exc:
        await HRService(None).merge_card_on(conn, "tm-9", {}, {})
    assert exc.value.code is HRCardCode.HIRE_DATE_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, [], "x", 0, False])
async def test_a_non_object_profile_docs_or_contract_is_rejected_unchanged(bad):
    _, conn = _pool()
    conn.fetchrow = AsyncMock()
    for fields, patch in (({}, bad), ({}, {"docs": bad}), ({}, {"contract": bad}), (bad, {})):
        with pytest.raises(HRCardError) as exc:
            await HRService(None).merge_card_on(conn, "tm-1", fields, patch)
        assert exc.value.code is HRCardCode.PROFILE_NOT_OBJECT
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_card_on_replaces_a_driver_error_by_a_code():
    _, conn = _pool()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("ZQXLEAK value"))
    with pytest.raises(HRCardError) as exc:
        await HRService(None).merge_card_on(conn, "tm-1", {}, {})
    assert exc.value.code is HRCardCode.DB_ERROR and "ZQXLEAK" not in str(exc.value)


@pytest.mark.asyncio
async def test_merge_card_on_accepts_an_iso_hire_date_string_and_rejects_a_bad_one():
    _, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 1, "profile_type": "object"})
    await HRService(None).merge_card_on(conn, "tm-1", {"hire_date": "2025-03-01"}, {})
    assert conn.fetchrow.await_args.args[2] == date(2025, 3, 1)
    with pytest.raises(HRCardError) as exc:
        await HRService(None).merge_card_on(conn, "tm-1", {"hire_date": "nope"}, {})
    assert exc.value.code is HRCardCode.INVALID_INPUT


def test_the_legacy_employee_shape_never_carries_the_profile():
    assert HRService._legacy_row({"id": 1, "profile": {"nik": "x"}, "npwp": "n"}) == {
        "id": 1,
        "npwp": "n",
    }


@pytest.mark.asyncio
async def test_merge_card_on_passes_the_cleared_keys_sorted_as_a_text_array():
    _, conn = _pool()
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    await HRService(None).merge_card_on(conn, "tm-1", {}, {}, clear=["nik", "bank_name"])
    sql, *params = conn.fetchrow.await_args.args
    assert params[11] == ["bank_name", "nik"] and "$12::text[]" in sql
    await HRService(None).merge_card_on(conn, "tm-1", {}, {})  # the import passes none
    assert conn.fetchrow.await_args.args[12] == []


@pytest.mark.asyncio
async def test_edit_card_locks_the_member_then_merges_with_a_lock_timeout():
    pool, conn = _pool()
    conn.fetchval = AsyncMock(return_value="tm-1")
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    assert await HRService(pool).edit_card("tm-1", "a@b.c", {"npwp": "N"}, {}, ["nik"]) == 7
    assert "lock_timeout" in conn.execute.await_args.args[0]
    assert "FOR SHARE" in conn.fetchval.await_args.args[0]
    assert conn.fetchrow.await_args.args[-1] == ["nik"]


@pytest.mark.asyncio
async def test_edit_card_returns_none_when_the_member_no_longer_matches():
    pool, conn = _pool()
    conn.fetchval = AsyncMock(return_value=None)
    assert await HRService(pool).edit_card("tm-1", "a@b.c", {}, {}, []) is None
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_card_failures_are_codes_without_driver_text():
    pool, conn = _pool()
    conn.fetchval = AsyncMock(return_value="tm-1")
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("ZQXLEAK"))
    with pytest.raises(HRCardError) as exc:
        await HRService(pool).edit_card("tm-1", "a@b.c", {}, {}, [])
    assert exc.value.code is HRCardCode.DB_ERROR and "ZQXLEAK" not in str(exc.value)
