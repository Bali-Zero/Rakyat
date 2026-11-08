"""The client profile carries `as_of`, the WITA day its attention rows were computed for.

The kita card derives every document date from this one value, so the day the server
used for the attention ledger and the day the card shows can never differ.
"""

from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROUTER = "backend.app.routers.crm_enhanced"
FIXED_DAY = date(2026, 11, 7)


@pytest.fixture
def admin_user() -> dict[str, Any]:
    return {"email": "admin@balizero.com", "role": "admin", "id": "admin-1"}


@pytest.fixture
def client_row() -> dict[str, Any]:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {
        "id": 42,
        "uuid": "00000000-0000-0000-0000-000000000042",
        "full_name": "Synthetic Client",
        "email": "synthetic@example.invalid",
        "phone": None,
        "whatsapp": None,
        "nationality": None,
        "passport_number": None,
        "passport_expiry": None,
        "date_of_birth": None,
        "company_name": None,
        "google_drive_folder_id": None,
        "status": "active",
        "client_type": "individual",
        "assigned_to": "team@balizero.com",
        "address": None,
        "notes": None,
        "tags": None,
        "custom_fields": None,
        "first_contact_date": now,
        "last_interaction_date": now,
        "created_at": now,
        "updated_at": now,
    }


class TestProfileAsOf:
    @pytest.mark.asyncio
    async def test_as_of_is_the_day_passed_to_the_attention_builder(
        self,
        mock_db_pool: MagicMock,
        mock_db_conn: AsyncMock,
        admin_user: dict,
        client_row: dict,
    ) -> None:
        from backend.app.routers.crm_enhanced import get_client_profile

        builder = MagicMock(
            return_value={"attention": [], "attention_stats": {"expired": 0}}
        )
        with (
            patch(f"{ROUTER}.verify_client_access", new=AsyncMock()),
            patch(f"{ROUTER}.today_wita", return_value=FIXED_DAY),
            patch(f"{ROUTER}.build_client_attention", builder),
        ):
            mock_db_conn.fetchrow = AsyncMock(return_value=client_row)
            mock_db_conn.fetch = AsyncMock(return_value=[])
            result = await get_client_profile(
                client_id=42, pool=mock_db_pool, current_user=admin_user
            )

        assert builder.call_args.kwargs["today"] == FIXED_DAY
        assert result["as_of"] == FIXED_DAY.isoformat()
        assert result["attention_error"] is None

    @pytest.mark.asyncio
    async def test_as_of_is_present_when_the_attention_build_fails(
        self,
        mock_db_pool: MagicMock,
        mock_db_conn: AsyncMock,
        admin_user: dict,
        client_row: dict,
    ) -> None:
        from backend.app.routers.crm_enhanced import get_client_profile

        with (
            patch(f"{ROUTER}.verify_client_access", new=AsyncMock()),
            patch(f"{ROUTER}.today_wita", return_value=FIXED_DAY),
            patch(f"{ROUTER}.build_client_attention", side_effect=RuntimeError("boom")),
        ):
            mock_db_conn.fetchrow = AsyncMock(return_value=client_row)
            mock_db_conn.fetch = AsyncMock(return_value=[])
            result = await get_client_profile(
                client_id=42, pool=mock_db_pool, current_user=admin_user
            )

        assert result["attention_error"] == "attention_build_failed"
        assert result["as_of"] == FIXED_DAY.isoformat()
