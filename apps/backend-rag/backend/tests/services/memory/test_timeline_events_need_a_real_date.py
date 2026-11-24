from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import backend.services.memory.episodic_memory_service as episodic_memory_module
from backend.services.memory.episodic_memory_service import EpisodicMemoryService


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 9, 10, 30, tzinfo=tz or timezone.utc)


def test_extract_datetime_rejects_hyphenated_month_range() -> None:
    service = EpisodicMemoryService()

    assert service._extract_datetime("I need a visa for 2-3 months") is None


def test_extract_datetime_rejects_invalid_numeric_date() -> None:
    service = EpisodicMemoryService()

    assert service._extract_datetime("Processing takes 10-15 days") is None


@pytest.mark.asyncio
async def test_assistant_numeric_range_does_not_save_an_event() -> None:
    service = EpisodicMemoryService()
    store = AsyncMock(return_value={"status": "created"})
    service.add_event = store

    result = await service.extract_and_save_event(
        user_id="synthetic-user",
        message="Can you explain the visa requirements?",
        ai_response="It takes 7-14 working days.",
    )

    assert result is None
    store.assert_not_awaited()


def test_extract_datetime_keeps_full_and_relative_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(episodic_memory_module, "datetime", FixedDateTime)
    service = EpisodicMemoryService()

    assert service._extract_datetime("Appointment on 15/06/2026") == datetime(
        2026, 6, 15, 12, 0, tzinfo=timezone.utc
    )
    assert service._extract_datetime("The appointment is today") == datetime(
        2026, 8, 9, 12, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_assistant_relative_word_still_saves_an_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(episodic_memory_module, "datetime", FixedDateTime)
    service = EpisodicMemoryService()
    store = AsyncMock(return_value={"status": "created"})
    service.add_event = store

    result = await service.extract_and_save_event(
        user_id="synthetic-user",
        message="Can you explain the visa requirements?",
        ai_response="It was approved yesterday.",
    )

    assert result == {"status": "created"}
    assert store.await_args.kwargs["occurred_at"] == datetime(
        2026, 8, 8, 12, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("renewal due 15-03-2026", (2026, 3, 15)),
        ("renewal due 15/03/2026", (2026, 3, 15)),
        ("renewal due 1-2-26", (2026, 2, 1)),
    ],
)
def test_a_hyphenated_date_with_its_year_is_still_a_date(
    text: str, expected: tuple[int, int, int]
) -> None:
    service = EpisodicMemoryService()

    found = service._extract_datetime(text)

    assert found is not None
    assert (found.year, found.month, found.day) == expected


@pytest.mark.parametrize("text", ["it costs 10/15 of the fee", "ratio 32/13/2026"])
def test_slashed_numbers_that_are_not_a_calendar_date_are_not_a_date(text: str) -> None:
    service = EpisodicMemoryService()

    assert service._extract_datetime(text) is None
