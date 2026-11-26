"""Round-12 F16 — under-lock duplicate-phone re-check in ClientRepository.

The router's dedup pre-check runs OUTSIDE the create transaction: two
concurrent creates can both pass it, then serialize on the phonecore advisory
lock and insert twice. The repository must re-check under the held lock and
raise DuplicatePhoneError instead of double-inserting.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.db.repositories.client_repository import (
    ClientRepository,
    DuplicatePhoneError,
    _parse_date,
    _parse_document_date,
)


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _make_repo(dup_row: dict | None) -> tuple[ClientRepository, AsyncMock]:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Tx())
    conn.execute.return_value = None
    # First fetchrow = the dedup re-check; later ones = the INSERT RETURNING.
    conn.fetchrow.side_effect = [
        dup_row,
        {"id": 900, "full_name": "New Client"},
    ]

    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    return ClientRepository(pool), conn


@pytest.mark.asyncio
async def test_create_raises_duplicate_phone_under_lock():
    """Guilt: the under-lock re-check finds an owner the pre-check missed —
    the INSERT must never run."""
    repo, conn = _make_repo(
        dup_row={"id": 11654, "full_name": "Existing", "assigned_to": "k@balizero.com"}
    )

    with pytest.raises(DuplicatePhoneError) as exc_info:
        await repo.create_client_with_details(
            {"full_name": "New Client", "phone": "+62 821-3454-721"},
            enforce_unique_phone_core=True,
        )

    assert exc_info.value.existing_client_id == 11654
    # No INSERT INTO clients ever ran (only the dedup fetchrow was consumed).
    for call in conn.fetchrow.await_args_list[1:]:
        assert "INSERT INTO clients" not in call.args[0]
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_create_proceeds_when_no_duplicate_under_lock():
    """Innocence: no owner under lock — the create completes."""
    repo, conn = _make_repo(dup_row=None)

    record = await repo.create_client_with_details(
        {"full_name": "New Client", "phone": "+62 821-3454-721"},
        enforce_unique_phone_core=True,
    )

    assert record["id"] == 900
    # The second fetchrow was the INSERT ... RETURNING.
    assert "INSERT INTO clients" in conn.fetchrow.await_args_list[1].args[0]


@pytest.mark.asyncio
async def test_create_skips_recheck_when_duplicates_allowed():
    """allow_duplicate_phone=True path: no re-check, straight to INSERT."""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Tx())
    conn.execute.return_value = None
    conn.fetchrow.side_effect = [{"id": 901, "full_name": "Shared Phone"}]

    pool = MagicMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)

    repo = ClientRepository(pool)
    record = await repo.create_client_with_details(
        {"full_name": "Shared Phone", "phone": "+62 821-3454-721"},
        enforce_unique_phone_core=False,
    )

    assert record["id"] == 901
    assert "INSERT INTO clients" in conn.fetchrow.await_args_list[0].args[0]


@pytest.mark.parametrize("invalid", ["0001-01-01", "9999-12-31"])
def test_client_repository_drops_out_of_range_document_date(invalid):
    assert _parse_document_date(invalid) is None


def test_client_repository_keeps_normal_document_date():
    assert _parse_document_date("2030-01-01").isoformat() == "2030-01-01"


def test_client_repository_does_not_range_check_date_of_birth():
    assert _parse_date("1890-05-01").isoformat() == "1890-05-01"


def test_client_repository_document_date_accepts_a_datetime():
    assert _parse_document_date(datetime(2030, 1, 1, 12, 0)) == datetime(2030, 1, 1, 12, 0)
    assert _parse_document_date(datetime(1, 1, 1)) is None


_ATTRIBUTION_COLUMNS = ("referrer_url", "landing_page", "first_touch_at")


@pytest.mark.asyncio
async def test_create_keeps_an_explicit_lead_source_unchanged():
    """Innocence: an explicit lead_source reaches the INSERT as given."""
    repo, conn = _make_repo(dup_row=None)

    await repo.create_client_with_details(
        {"full_name": "New Client", "lead_source": "referral"},
    )

    args = conn.fetchrow.await_args_list[0].args
    assert "referral" in args[1:]
    assert args[1:].count("referral") == 1


@pytest.mark.asyncio
async def test_create_without_lead_source_stores_none():
    """Innocence: no lead_source and no attribution keys -> NULL lead_source."""
    repo, conn = _make_repo(dup_row=None)

    await repo.create_client_with_details({"full_name": "New Client"})

    sql = conn.fetchrow.await_args_list[0].args[0]
    values = conn.fetchrow.await_args_list[0].args[1:]
    columns = sql.split("INSERT INTO clients (", 1)[1].split(")", 1)[0]
    lead_source = [c.strip() for c in columns.split(",")].index("lead_source")
    assert values[lead_source] is None


@pytest.mark.asyncio
async def test_create_insert_names_no_attribution_column_and_ignores_the_keys():
    """Guard: the INSERT no longer names the dead attribution columns, and a
    dict that still carries those keys does not turn them into values."""
    repo, conn = _make_repo(dup_row=None)

    await repo.create_client_with_details(
        {
            "full_name": "New Client",
            "landing_page": "/visa?utm_source=whatsapp",
            "referrer_url": "https://example.test/ref",
            "first_touch_at": "2026-01-01T00:00:00",
        },
    )

    args = conn.fetchrow.await_args_list[0].args
    for column in _ATTRIBUTION_COLUMNS:
        assert column not in args[0]
    assert args[0].count("$") == len(args) - 1
    assert "/visa?utm_source=whatsapp" not in args[1:]
    values = args[1:]
    columns = args[0].split("INSERT INTO clients (", 1)[1].split(")", 1)[0]
    assert [c.strip() for c in columns.split(",")].count("lead_source") == 1
    assert values[[c.strip() for c in columns.split(",")].index("lead_source")] is None
