"""The HR card EDIT (STAFF_CARD.md C11) against a real Postgres: set, clear, create, serialise.

Reuses the throwaway-schema `pool` fixture of the write-path tests (same DSN, same production
jsonb codec). All names and numbers are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from typing import Any

import asyncpg
import pytest

from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService
from backend.tests.migrations.test_hr_card_write_path import pool  # noqa: F401  (fixture)

pytestmark = pytest.mark.integration

A = ("tm-a", "fake.a@balizero.com")
B = ("tm-b", "fake.b@balizero.com")
NIK = "3171234567890001"


async def _edit(
    pool: asyncpg.Pool,
    who: tuple[str, str],
    fields: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    clear: list[str] | None = None,
) -> Any:
    return await HRService(pool).edit_card(who[0], who[1], fields or {}, profile or {}, clear or [])


async def _row(pool: asyncpg.Pool, member: str) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM hr_employees WHERE team_member_id = $1", member)


async def _profile(pool: asyncpg.Pool, member: str) -> Any:
    async with pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT profile::text FROM hr_employees WHERE team_member_id = $1", member
        )
    return json.loads(raw)


@pytest.mark.asyncio
async def test_set_changes_only_the_named_keys(pool: asyncpg.Pool) -> None:  # noqa: F811
    assert await _edit(pool, A, {"bank_name": "Fake Bank"}, {"nik": NIK, "unit": "ops"}) is not None
    row = await _row(pool, "tm-a")
    assert (
        row["bank_name"] == "Fake Bank" and row["npwp"] == "10.01" and row["base_salary_idr"] == 5
    )
    assert await _profile(pool, "tm-a") == {"nik": NIK, "unit": "ops"}


@pytest.mark.asyncio
async def test_clear_is_a_real_null_and_a_real_key_removal(pool: asyncpg.Pool) -> None:  # noqa: F811
    await _edit(pool, A, {"bank_name": "Fake Bank"}, {"nik": NIK, "unit": "ops", "gender": "x"})
    await _edit(pool, A, clear=["npwp", "bank_name", "nik"])
    row = await _row(pool, "tm-a")
    assert row["npwp"] is None and row["bank_name"] is None
    assert await _profile(pool, "tm-a") == {"unit": "ops", "gender": "x"}  # key gone, not null


@pytest.mark.asyncio
async def test_set_and_clear_in_one_call(pool: asyncpg.Pool) -> None:  # noqa: F811
    await _edit(pool, A, profile={"unit": "ops", "address": "Fake Street 1"})
    await _edit(pool, A, {"bank_account": "123456"}, {"unit": "ops2"}, ["address", "npwp"])
    row = await _row(pool, "tm-a")
    assert row["bank_account"] == "123456" and row["npwp"] is None
    assert await _profile(pool, "tm-a") == {"unit": "ops2"}


@pytest.mark.asyncio
async def test_nested_contract_number_clear_keeps_the_contract_file(pool: asyncpg.Pool) -> None:  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"contract": {"number": "C-1", "file_id": "f9"}, "docs": {"ktp": "f1"}, "unit": "u"},
        )
    await _edit(pool, A, clear=["contract_number"])
    assert await _profile(pool, "tm-a") == {
        "contract": {"file_id": "f9"},
        "docs": {"ktp": "f1"},
        "unit": "u",
    }
    await _edit(pool, A, profile={"contract": {"number": "C-2"}})
    assert (await _profile(pool, "tm-a"))["contract"] == {"file_id": "f9", "number": "C-2"}


@pytest.mark.asyncio
async def test_clearing_contract_number_with_no_contract_creates_nothing(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    await _edit(pool, A, clear=["contract_number"])
    assert await _profile(pool, "tm-a") == {}  # no contract stored: nothing is invented


@pytest.mark.asyncio
async def test_docs_are_untouched_by_an_edit_that_does_not_mention_them(pool: asyncpg.Pool) -> None:  # noqa: F811
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE hr_employees SET profile = $1 WHERE team_member_id = 'tm-a'",
            {"docs": {"ktp": "f1", "kk": "f2"}, "contract": {"file_id": "f3"}},
        )
    await _edit(pool, A, {"bank_name": "Fake Bank"}, {"unit": "ops"}, ["npwp", "nik"])
    profile = await _profile(pool, "tm-a")
    assert profile["docs"] == {"ktp": "f1", "kk": "f2"}
    assert profile["contract"] == {"file_id": "f3"}


@pytest.mark.asyncio
async def test_create_needs_a_hire_date_and_with_one_makes_the_record(pool: asyncpg.Pool) -> None:  # noqa: F811
    assert await _row(pool, "tm-b") is None
    with pytest.raises(HRCardError) as exc:
        await _edit(pool, B, profile={"unit": "ops"})
    assert exc.value.code is HRCardCode.HIRE_DATE_REQUIRED
    assert await _row(pool, "tm-b") is None
    assert await _edit(pool, B, {"hire_date": date(2025, 5, 1)}, {"unit": "ops"}) is not None
    row = await _row(pool, "tm-b")
    assert row["hire_date"] == date(2025, 5, 1) and row["base_salary_idr"] == 0
    assert row["is_active"] is True and row["ptkp_status"] == "TK/0"
    assert await _profile(pool, "tm-b") == {"unit": "ops"}


@pytest.mark.asyncio
async def test_a_member_who_is_a_client_or_renamed_is_not_written(pool: asyncpg.Pool) -> None:  # noqa: F811
    assert (
        await _edit(pool, ("tm-c", "fake.c@balizero.com"), {"hire_date": date(2025, 1, 1)}) is None
    )
    assert await _edit(pool, ("tm-a", "other@balizero.com"), {"bank_name": "X"}) is None
    assert await _row(pool, "tm-c") is None
    assert (await _row(pool, "tm-a"))["bank_name"] is None


@pytest.mark.asyncio
async def test_two_concurrent_edits_on_one_member_serialise(pool: asyncpg.Pool) -> None:  # noqa: F811
    await asyncio.gather(
        *(
            _edit(pool, A, profile={"unit": f"u{i}"}, fields={"bank_name": f"b{i}"})
            for i in range(8)
        )
    )
    row = await _row(pool, "tm-a")
    profile = await _profile(pool, "tm-a")
    assert isinstance(profile, dict) and set(profile) == {"unit"}
    assert profile["unit"][1:] == row["bank_name"][1:]  # one writer's pair, never a mix


@pytest.mark.asyncio
async def test_a_concurrent_set_and_clear_of_different_keys_loses_neither(
    pool: asyncpg.Pool,
) -> None:  # noqa: F811
    await _edit(pool, A, profile={"address": "Fake Street 1"})
    await asyncio.gather(_edit(pool, A, profile={"unit": "ops"}), _edit(pool, A, clear=["address"]))
    assert await _profile(pool, "tm-a") == {"unit": "ops"}


@pytest.mark.asyncio
async def test_no_value_reaches_the_logs_or_an_error(pool: asyncpg.Pool, caplog: Any) -> None:  # noqa: F811
    marker_bank, marker_addr = "Bank Rahasia Contoh", "Jalan Rahasia 99"
    with caplog.at_level(logging.DEBUG):
        await _edit(pool, A, {"bank_name": marker_bank}, {"nik": NIK, "address": marker_addr})
        await _edit(pool, A, clear=["nik"])
        with pytest.raises(HRCardError) as exc:
            await _edit(pool, B, {"bank_name": marker_bank})
    text = caplog.text + str(exc.value) + repr(exc.value)
    for value in (marker_bank, marker_addr, NIK):
        assert value not in text


async def _set(pool: asyncpg.Pool, member: str, sql_set: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(f"UPDATE team_members SET {sql_set} WHERE id = $1", member)


@pytest.mark.asyncio
async def test_a_member_deactivated_after_the_lookup_is_not_written(pool: asyncpg.Pool) -> None:  # noqa: F811
    await _set(pool, "tm-a", "active = false")  # committed after the route's lookup
    assert await _edit(pool, A, {"bank_name": "Fake Bank"}, {"unit": "ops"}) is None
    assert (await _row(pool, "tm-a"))["bank_name"] is None
    assert await _profile(pool, "tm-a") == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["monitoring", "partner", "client"])
async def test_a_member_whose_role_changed_after_the_lookup_is_not_written(  # noqa: F811
    pool: asyncpg.Pool, role: str
) -> None:
    await _set(pool, "tm-a", f"role = '{role}'")
    assert await _edit(pool, A, {"bank_name": "Fake Bank"}) is None
    assert (await _row(pool, "tm-a"))["bank_name"] is None


@pytest.mark.asyncio
async def test_create_mode_passes_the_predicate_by_the_company_address_branch(  # noqa: F811
    pool: asyncpg.Pool,
) -> None:
    # tm-b has no hr_employees row: only '@balizero.com' plus a named staff role qualifies.
    assert await _edit(pool, B, {"hire_date": date(2025, 5, 1)}) is not None
    # the same shape with the test/bot role `member` is not a person in People: refused
    await _set(pool, "tm-q1", "role = 'member'")
    assert (
        await _edit(pool, ("tm-q1", "fake.q@balizero.com"), {"hire_date": date(2025, 5, 1)}) is None
    )
    assert await _row(pool, "tm-q1") is None


@pytest.mark.asyncio
async def test_a_concurrent_deactivation_waits_for_the_edits_commit(  # noqa: F811
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked, release = asyncio.Event(), asyncio.Event()
    original = HRService.merge_card_on

    async def held(self: Any, *args: Any, **kwargs: Any) -> Any:
        locked.set()  # the member lock is taken: hold the transaction open
        await release.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(HRService, "merge_card_on", held)
    edit = asyncio.create_task(_edit(pool, A, {"bank_name": "Fake Bank"}))
    await asyncio.wait_for(locked.wait(), 5)
    async with pool.acquire() as probe:
        await probe.execute("SET lock_timeout = '300ms'")
        with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
            await probe.execute("UPDATE team_members SET active = false WHERE id = 'tm-a'")
        await probe.execute("SET lock_timeout = 0")
    release.set()
    assert await edit is not None
    await _set(pool, "tm-a", "active = false")  # free once the edit committed
    assert (await _row(pool, "tm-a"))["bank_name"] == "Fake Bank"
