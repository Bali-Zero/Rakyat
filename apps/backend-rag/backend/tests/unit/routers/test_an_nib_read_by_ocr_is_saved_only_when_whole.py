"""An NIB read by OCR is written to the client card only when it is whole (13 digits)."""

from __future__ import annotations

import base64
import logging
from decimal import Decimal
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
    ["12345", "9" * 12, "9" * 14, "9" * 20],
    ids=["five", "twelve", "fourteen", "twenty"],
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


async def _assert_refused(read: Any, mock_db_pool, mock_current_user) -> None:
    result = await _extract(read, mock_db_pool, mock_current_user)

    assert result.success is True
    assert result.nib is None
    assert _saved_nibs(mock_db_pool) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        "- 9120000000001",
        "\u22129120000000001",
        "\uff0b9120000000001",
        "\u20109120000000001",
        "\u00b29120000000001",
        "\u24609120000000001",
        "NIB \u06639120000000001",
        "NIB-9120000000001",
        " : 9120000000001",
        "\u4e009120000000001",
        "NIB \u4e009120000000001",
        "NIB \u4e8c9120000000001",
        "NIB: .9120000000001",
        "NIB . 9120000000001",
        "NIB .:9120000000001",
        "NIB.9120000000001",
        "No.9120000000001",
    ],
    ids=[
        "space-hyphen",
        "minus-sign",
        "fullwidth-plus",
        "unicode-hyphen",
        "superscript-two",
        "circled-one",
        "arabic-indic-in-label",
        "hyphen-after-label",
        "colon-only-label",
        "cjk-one",
        "cjk-one-after-label",
        "cjk-two-after-label",
        "dot-after-colon",
        "dot-after-space",
        "dot-colon",
        "dot-touching-digit",
        "abbreviation-touching-digit",
    ],
)
async def test_a_label_outside_the_closed_alphabet_is_refused(
    read: str, mock_db_pool, mock_current_user
) -> None:
    await _assert_refused(read, mock_db_pool, mock_current_user)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        "912000000001.0",
        "9.1200000001e11",
        "912000000001e1",
        "1.0e12",
        ".9120000000001",
        "-9120000000001",
        "+9120000000001",
    ],
    ids=["fraction", "exponent", "exponent-int", "small-exponent", "leading-dot", "minus", "plus"],
)
async def test_a_numeric_string_never_manufactures_digits(
    read: str, mock_db_pool, mock_current_user
) -> None:
    await _assert_refused(read, mock_db_pool, mock_current_user)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        912000000001.0,
        9.12000000001e11,
        9120000000010.0,
        Decimal("9120000000001"),
        -9120000000001,
        [9],
        True,
    ],
    ids=[
        "float-12-digits",
        "exponent",
        "float-that-looks-whole",
        "decimal",
        "negative-int",
        "list",
        "bool",
    ],
)
async def test_a_read_that_is_not_text_or_a_whole_number_answers_none(
    read: Any, mock_db_pool, mock_current_user
) -> None:
    await _assert_refused(read, mock_db_pool, mock_current_user)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "stored"),
    [
        ("9120000000001", "9120000000001"),
        ("9120-0000-0000-1", "9120000000001"),
        ("9120 0000 00001", "9120000000001"),
        ("NIB 9120000000001", "9120000000001"),
        ("NIB: 9120000000001", "9120000000001"),
        ("NIB9120000000001", "9120000000001"),
        ("No. 9120000000001", "9120000000001"),
        ("N.I.B. 9120000000001", "9120000000001"),
        ("NIB.: 9120000000001", "9120000000001"),
        ("Nomor Induk Berusaha: 9120 0000 00001", "9120000000001"),
        (" 0120000000001 ", "0120000000001"),
        (9120000000001, "9120000000001"),
    ],
    ids=[
        "plain",
        "hyphens",
        "spaces",
        "label-space",
        "label-colon",
        "label-glued",
        "abbreviation",
        "dotted-abbreviation",
        "dot-colon-label",
        "long-label",
        "leading-zero-padded",
        "int",
    ],
)
async def test_a_plain_digit_block_of_13_is_saved_and_shown(
    read: Any, stored: str, mock_db_pool, mock_current_user
) -> None:
    result = await _extract(read, mock_db_pool, mock_current_user)

    assert result.success is True
    assert result.nib == stored
    assert _saved_nibs(mock_db_pool) == [stored]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "shown"),
    [
        ("NIB 12345", "12345"),
        ("12345", "12345"),
        ("9" * 12, "9" * 12),
        ("9" * 14, "9" * 14),
        ("9" * 20, "9" * 20),
    ],
    ids=["labelled-five", "five", "twelve", "fourteen", "twenty"],
)
async def test_a_faithful_fragment_is_shown_not_saved(
    read: str, shown: str, mock_db_pool, mock_current_user
) -> None:
    result = await _extract(read, mock_db_pool, mock_current_user)

    assert result.success is True
    assert result.nib == shown
    assert _saved_nibs(mock_db_pool) == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "read",
    [
        "\u0669" * 13,
        "91 2000\u0660000001",
        "9120000000001.",
        "9120000000001 (NIB)",
        "9120  0000 00001",
        "NIB 9120000000001 / 9120000000002",
    ],
    ids=[
        "arabic-indic-13",
        "mixed-unicode",
        "trailing-dot",
        "trailing-text",
        "double-space",
        "two-blocks",
    ],
)
async def test_a_read_outside_the_plain_block_grammar_is_refused(
    read: str, mock_db_pool, mock_current_user
) -> None:
    await _assert_refused(read, mock_db_pool, mock_current_user)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_refused_read_leaves_the_stored_nib_untouched(
    mock_db_pool, mock_current_user
) -> None:
    await _extract("912000000001.0", mock_db_pool, mock_current_user)

    assert not any(
        "SET nib" in call.args[0] for call in mock_db_pool._mock_conn.execute.await_args_list
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read", "text"),
    [
        ("912000000001.0", "NIB OCR: read not saved (not a plain digit block)"),
        ("NIB 12345", "NIB OCR: read not saved (not thirteen digits)"),
    ],
    ids=["not-a-block", "not-thirteen"],
)
async def test_the_warning_is_a_fixed_text_without_digits(
    read: str, text: str, mock_db_pool, mock_current_user, caplog
) -> None:
    module = "backend.app.routers.crm_clients_documents"
    with caplog.at_level(logging.DEBUG, logger=module):
        await _extract(read, mock_db_pool, mock_current_user)

    warnings = [r for r in caplog.records if r.name == module and r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == [text]
    assert all(not r.args for r in warnings)
    for record in warnings:
        assert not any(c.isdigit() for c in record.getMessage()), record.getMessage()
