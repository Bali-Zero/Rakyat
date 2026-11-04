"""Portal company "Shareholders & Officers": roles are read case-insensitively
and a director who holds no shares is not presented as a shareholder.

Measured on company_id 2938 (21 Oct 2026): kita stores link roles as
"Director" / "Shareholder" (AddCompanyModal option values), but
`get_company_detail` matched directors against lowercase literals only and
built `shareholders` from EVERY link. The client who is the company's
director therefore appeared to their co-shareholder as a "Shareholder" with
no percentage, and a lowercase-"director" link holding 50% appeared twice.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.portal.portal_service import PortalService


class _AsyncCtx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> bool:
        return False


def _service(links: list[dict[str, Any]]) -> PortalService:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncCtx())
    conn.fetchrow.side_effect = [
        {"role": "Director", "is_primary": True, "ownership_percentage": None},
        {
            "id": 2938,
            "company_name": "PT Example",
            "company_type": "PMA",
            "nib": None,
            "npwp_company": None,
            "kbli_code": None,
            "status": "active",
            "registered_address": None,
            "company_email": None,
            "company_phone": None,
            "akta_pendirian_no": None,
            "akta_pendirian_date": None,
            "sk_menhumkam_no": None,
            "custom_fields": None,
        },
    ]
    conn.fetch.side_effect = [[], [], links]
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return PortalService(pool)


def _ctx() -> dict[str, Any]:
    return {"client_id": 11470, "email": "client-11470@example.com"}


@pytest.mark.asyncio
async def test_kita_capitalised_director_without_shares_is_an_officer_not_a_shareholder() -> None:
    service = _service(
        [
            {"full_name": "Person A", "role": "Director", "ownership_percentage": None},
            {"full_name": "Person B", "role": "director", "ownership_percentage": 50},
        ]
    )
    result = await service.get_company_detail(11470, 2938, current_user=_ctx())

    assert result["directors"] == ["Person A", "Person B"]
    assert result["shareholders"] == [{"name": "Person B", "pct": 50.0}]


@pytest.mark.asyncio
async def test_shareholder_role_without_percentage_stays_listed() -> None:
    service = _service(
        [
            {"full_name": "Person C", "role": "Shareholder", "ownership_percentage": None},
            {"full_name": "Person D", "role": " Commissioner ", "ownership_percentage": None},
        ]
    )
    result = await service.get_company_detail(11470, 2938, current_user=_ctx())

    assert result["directors"] == ["Person D"]
    assert result["shareholders"] == [{"name": "Person C", "pct": None}]


@pytest.mark.asyncio
async def test_positive_control_lowercase_roles_unchanged() -> None:
    service = _service(
        [
            {"full_name": "Alice", "role": "director", "ownership_percentage": 75},
            {"full_name": "Bob", "role": "shareholder", "ownership_percentage": 25},
        ]
    )
    result = await service.get_company_detail(11470, 2938, current_user=_ctx())

    assert result["directors"] == ["Alice"]
    assert result["shareholders"] == [
        {"name": "Alice", "pct": 75.0},
        {"name": "Bob", "pct": 25.0},
    ]
