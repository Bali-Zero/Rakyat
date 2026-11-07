"""PII log hygiene for CollectiveMemoryService's contribute, confirm and refute lines.

Class B of the 2026-11-04 PII-log window. ``user_id`` here is the contributor's email
(``refute_fact``'s own docstring), and the new-fact line also logged the first 50
characters of the fact. The three INFO lines now carry the redacted contributor and,
for a new fact, its length. GUILT: no email and no fact fragment reaches a log record.
INNOCENCE: each line is still emitted with the memory id and the redacted contributor,
and the service result is unchanged.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.security.pii_log_identifier import redact_identifier_for_log
from backend.services.memory.collective_memory_service import CollectiveMemoryService

_LOGGER = "backend.services.memory.collective_memory_service"
_EMAIL = "contributor.probe@example.com"
_FACT = "SENTINELFACT the client from Milan pays the KITAS fee in cash"


def _service(conn: AsyncMock) -> CollectiveMemoryService:
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=None)
    transaction.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction)

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return CollectiveMemoryService(pool=pool)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER]


def _assert_no_pii(lines: list[str]) -> None:
    blob = "\n".join(lines)
    assert _EMAIL not in blob
    assert "@example.com" not in blob
    assert "SENTINELFACT" not in blob


@pytest.mark.asyncio
async def test_new_fact_line_carries_the_redacted_contributor_and_the_length(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=11)
    conn.execute = AsyncMock()
    service = _service(conn)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.add_contribution(user_id=_EMAIL, content=_FACT, category="pricing")

    assert result["status"] == "created"
    assert result["memory_id"] == 11
    lines = _messages(caplog)
    _assert_no_pii(lines)
    assert lines == [
        f"🧠 [Collective] New fact #11 from {redact_identifier_for_log(_EMAIL)}: {len(_FACT)} chars"
    ]


@pytest.mark.asyncio
async def test_confirm_line_carries_the_redacted_contributor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"id": 7, "source_count": 1, "is_promoted": False},
            None,
            {"source_count": 2, "is_promoted": False, "confidence": 0.6},
        ]
    )
    conn.fetchval = AsyncMock(return_value=2)
    conn.execute = AsyncMock()
    service = _service(conn)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.add_contribution(user_id=_EMAIL, content=_FACT, category="pricing")

    assert result["status"] == "confirmed"
    assert result["source_count"] == 2
    lines = _messages(caplog)
    _assert_no_pii(lines)
    assert lines == [
        f"🧠 [Collective] User {redact_identifier_for_log(_EMAIL)} confirmed fact #7 "
        "(sources: 2, promoted: False)"
    ]


@pytest.mark.asyncio
async def test_refute_line_carries_the_redacted_contributor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"confidence": 0.5, "is_promoted": False})
    service = _service(conn)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caplog.clear()
        result = await service.refute_fact(user_id=_EMAIL, memory_id=7, reason=_FACT)

    assert result["status"] == "refuted"
    lines = _messages(caplog)
    _assert_no_pii(lines)
    assert lines == [
        f"⚠️ [Collective] Fact #7 refuted by {redact_identifier_for_log(_EMAIL)} (conf: 0.50)"
    ]
