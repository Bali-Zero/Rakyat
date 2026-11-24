"""An NIB read by OCR is written to the client card only when it is whole (13 digits)."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.routers.crm_clients_documents import NibExtractRequest, extract_nib


async def _extract(nib: Any, mock_db_pool, mock_current_user):
    request = NibExtractRequest(
        client_id=1, file=base64.b64encode(b"synthetic nib").decode(), file_name="nib.pdf"
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"response": "{}"}
    with (
        patch("backend.app.routers.crm_clients_documents.verify_client_access", new=AsyncMock()),
        patch(
            "backend.app.routers.crm_clients_documents.extract_json_from_llm_response",
            return_value={"nib": nib, "company_name": "PT Synthetic", "confidence": 0.5},
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        client = AsyncMock()
        client.post = AsyncMock(return_value=response)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client_cls.return_value = client
        return await extract_nib(
            request=request, current_user=mock_current_user, db_pool=mock_db_pool
        )


def _saved_nibs(mock_db_pool) -> list[str]:
    return [
        call.args[1]
        for call in mock_db_pool._mock_conn.execute.await_args_list
        if "SET nib" in call.args[0]
    ]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fragment",
    ["12345", "9" * 12, "9" * 14, "9" * 20, "٩" * 13, "91 2000٠000001"],
    ids=["five", "twelve", "fourteen", "twenty", "arabic-indic-13", "mixed-unicode"],
)
async def test_a_fragment_is_returned_but_never_saved(
    fragment: str, mock_db_pool, mock_current_user
) -> None:
    result = await _extract(fragment, mock_db_pool, mock_current_user)

    assert result.success is True
    assert _saved_nibs(mock_db_pool) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "stored"),
    [
        ("9120000000001", "9120000000001"),
        ("9120-0000-0000-1", "9120000000001"),
        (9120000000001, "9120000000001"),
    ],
    ids=["plain", "separators", "number"],
)
async def test_a_whole_nib_is_saved_as_its_13_digits(
    read: Any, stored: str, mock_db_pool, mock_current_user
) -> None:
    result = await _extract(read, mock_db_pool, mock_current_user)

    assert result.success is True
    assert result.nib == stored
    assert _saved_nibs(mock_db_pool) == [stored]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_fragment_is_still_shown_to_staff(mock_db_pool, mock_current_user) -> None:
    result = await _extract("NIB 12345", mock_db_pool, mock_current_user)

    assert result.nib == "12345"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [912000000001.0, 9.12000000001e11, 9120000000010.0, True],
    ids=["float-12-digits", "exponent", "float-that-looks-whole", "bool"],
)
async def test_a_float_or_bool_is_never_saved(read: Any, mock_db_pool, mock_current_user) -> None:
    result = await _extract(read, mock_db_pool, mock_current_user)

    assert result.success is True
    assert _saved_nibs(mock_db_pool) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("read", ["9120000000010", 9120000000010], ids=["string", "int"])
async def test_a_whole_string_or_int_is_saved_pin(
    read: Any, mock_db_pool, mock_current_user
) -> None:
    await _extract(read, mock_db_pool, mock_current_user)

    assert _saved_nibs(mock_db_pool) == ["9120000000010"]
