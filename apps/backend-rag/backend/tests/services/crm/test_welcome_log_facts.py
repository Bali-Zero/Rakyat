"""The five functions that decide what a welcome or partner sender may log.

``services/crm/welcome/log_facts.py`` turns a value into text a log line may carry: one word
for an error, digits for a status or a record id, the canonical text of a UUID, a member of a
set written in the source. Each function is total and returns an exact ``str``, so the object a
caller passed never reaches a log record. The samples here are the ones a sender cannot be
trusted to never meet: subclasses that print an address from ``__str__``, a property that
answers one value to a check and another to a conversion, the phone's digits as an integer.
"""

from __future__ import annotations

import asyncio
import http
import os
import re
import uuid
from typing import Any, get_args
from urllib.parse import urlparse

import asyncpg
import httpx
import pytest
from asyncpg.pgproto.pgproto import UUID as DriverUUID

from backend.services.crm.birthday_notifier_service import NATIONALITY_LANGUAGE_MAP
from backend.services.crm.partners.models import CommissionEntryType, CommissionStatus
from backend.services.crm.welcome import log_facts
from backend.services.crm.welcome.log_facts import (
    COMMISSION_ENTRY_TYPES,
    COMMISSION_STATUSES,
    LANGUAGES,
    choice_fact,
    error_word,
    id_fact,
    status_fact,
    uuid_fact,
)

ADDRESS = "zz.client.probe@example.test"
DIGITS = 6281200004242
CANONICAL = "123e4567-e89b-12d3-a456-426614174000"
TOP_ID = 2_147_483_647


class _Abort(BaseException):
    """Not an ``Exception``: what a guard written for ordinary errors lets through."""


class _CannotSayWhatItIs:
    """An object whose ``__class__``, which ``isinstance`` may ask for, aborts."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        raise _Abort(ADDRESS)


class _LyingInt(int):
    """An integer that prints an address wherever it is asked for text."""

    def __str__(self) -> str:
        return ADDRESS

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        return ADDRESS


class _LyingUUID(uuid.UUID):
    def __str__(self) -> str:
        return ADDRESS

    __repr__ = __str__


class _LyingStr(str):
    def __str__(self) -> str:
        return ADDRESS

    __repr__ = __str__


class _OversizedLiar(int):
    """An integer above the UUID range that answers every comparison as if inside it."""

    def __lt__(self, other: object) -> bool:
        return True

    __le__ = __ge__ = __gt__ = __lt__


def _uuid_answering(*answers: Any) -> tuple[uuid.UUID, list[int]]:
    """A UUID whose ``.int`` gives ``answers`` in turn, the last one for ever; and its reads."""
    reads: list[int] = []

    class Answering(uuid.UUID):
        def __init__(self) -> None:  # the real initialiser would store an integer
            pass

        @property
        def int(self) -> Any:  # type: ignore[override]
            reads.append(len(reads))
            answer = answers[min(len(reads), len(answers)) - 1]
            if isinstance(answer, BaseException):
                raise answer
            return answer

    return Answering(), reads


def _misnamed(base: type[BaseException], name: str) -> type[BaseException]:
    """A class named ``name`` that presents itself as a known library's connection error."""
    cls = type(name, (base,), {})
    cls.__module__ = "httpx"
    cls.__qualname__ = "ConnectError"
    return cls


class _ClaimsAnotherClass(RuntimeError):
    """An error whose ``__class__`` says it is a connection error; its real class is this."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        return httpx.ConnectError


class _InterfaceDataError(asyncpg.InterfaceError, ValueError):
    """The driver's own shape for bad query input: an interface error and a value error."""


# ── error_word ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("error", "word"),
    [
        (httpx.ConnectTimeout("zz"), "timeout"),  # a transport error too: timeout comes first
        (httpx.ReadTimeout("zz"), "timeout"),
        (TimeoutError("zz"), "timeout"),  # an OSError too
        (httpx.ConnectError("zz"), "connection"),
        (httpx.ReadError("zz"), "connection"),
        (ConnectionResetError("zz"), "connection"),  # an OSError too
        (httpx.DecodingError("zz"), "http"),
        (httpx.TooManyRedirects("zz"), "http"),
        (asyncpg.PostgresError("zz"), "database"),
        (asyncpg.UniqueViolationError("zz"), "database"),
        (asyncpg.DataError("zz"), "database"),
        (_InterfaceDataError("zz"), "database"),  # a ValueError too: database comes first
        (asyncpg.InterfaceError("zz"), "database"),
        (FileNotFoundError("zz"), "os"),
        (IsADirectoryError("zz"), "os"),
        (KeyError("zz"), "lookup"),
        (IndexError("zz"), "lookup"),
        (ValueError("zz"), "value"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "zz"), "value"),
        (TypeError("zz"), "type"),
        (AttributeError("zz"), "attribute"),
        (RuntimeError("zz"), "other"),
        (ExceptionGroup("zz", [ValueError("zz"), KeyError("zz")]), "other"),  # noqa: F821
        (KeyboardInterrupt(), "other"),
        (_misnamed(ValueError, ADDRESS)("zz"), "value"),
        (_misnamed(KeyError, f"E{DIGITS}")("zz"), "lookup"),
        (_misnamed(RuntimeError, ADDRESS)("zz"), "other"),
        (_ClaimsAnotherClass("zz"), "other"),
        (None, "other"),
        (ADDRESS, "other"),
        (ValueError, "other"),  # the class itself is not an error that happened
    ],
    ids=lambda value: value if isinstance(value, str) and "@" not in value else None,
)
def test_an_error_is_one_word_chosen_by_its_real_class(error: object, word: str) -> None:
    assert error_word(error) == word


def test_the_words_are_a_closed_set() -> None:
    words = {
        error_word(error)
        for error in (
            httpx.ReadTimeout("zz"),
            httpx.ConnectError("zz"),
            httpx.DecodingError("zz"),
            asyncpg.PostgresError("zz"),
            OSError("zz"),
            KeyError("zz"),
            ValueError("zz"),
            TypeError("zz"),
            AttributeError("zz"),
            RuntimeError("zz"),
        )
    }

    assert words == {
        "timeout",
        "connection",
        "http",
        "database",
        "os",
        "lookup",
        "value",
        "type",
        "attribute",
        "other",
    }


# ── status_fact, id_fact ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (100, "100"),
        (200, "200"),
        (400, "400"),
        (599, "599"),
        (99, "?"),
        (600, "?"),
        (0, "?"),
        (-400, "?"),
        (DIGITS, "?"),
        (True, "?"),
        ("400", "?"),
        (400.0, "?"),
        (http.HTTPStatus.BAD_REQUEST, "?"),
        (_LyingInt(400), "?"),
        (None, "?"),
    ],
)
def test_a_status_is_digits_only_for_an_exact_int_in_the_status_range(
    value: object, text: str
) -> None:
    assert status_fact(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (1, "1"),
        (7, "7"),
        (TOP_ID, "2147483647"),
        (TOP_ID + 1, "?"),
        (0, "?"),
        (-7, "?"),
        (DIGITS, "?"),
        (True, "?"),
        ("12", "?"),
        (12.0, "?"),
        (_LyingInt(7), "?"),
        (None, "?"),
    ],
)
def test_an_id_is_digits_only_for_an_exact_int_in_the_id_range(value: object, text: str) -> None:
    assert id_fact(value) == text


# ── uuid_fact ────────────────────────────────────────────────────────────────


def test_a_uuid_is_its_canonical_text_whatever_the_object_prints() -> None:
    exact = uuid.UUID(CANONICAL)
    driver = DriverUUID(CANONICAL)

    assert type(driver) is not uuid.UUID and isinstance(driver, uuid.UUID)
    assert uuid_fact(exact) == CANONICAL
    assert uuid_fact(driver) == CANONICAL
    assert str(_LyingUUID(CANONICAL)) == ADDRESS
    assert uuid_fact(_LyingUUID(CANONICAL)) == CANONICAL
    assert uuid_fact(uuid.UUID(int=0)) == "00000000-0000-0000-0000-000000000000"
    assert uuid_fact(uuid.UUID(int=2**128 - 1)) == "ffffffff-ffff-ffff-ffff-ffffffffffff"


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError(ADDRESS),
        _Abort(ADDRESS),
        KeyboardInterrupt(),
        SystemExit(1),
        GeneratorExit(),
        asyncio.CancelledError(),
        ADDRESS,
        CANONICAL,
        None,
        True,
        12.0,
        -1,
        2**128,
        _LyingInt(5),
        _OversizedLiar(2**200),
    ],
    ids=[
        "raises",
        "raises what is not an Exception",
        "raises KeyboardInterrupt",
        "raises SystemExit",
        "raises GeneratorExit",
        "raises CancelledError",
        "a string",
        "the canonical text",
        "none",
        "a bool",
        "a float",
        "negative",
        "above the range",
        "an int subclass",
        "an oversized int subclass with lying comparisons",
    ],
)
def test_a_uuid_whose_integer_is_not_an_exact_int_in_range_is_unknown(answer: object) -> None:
    value, reads = _uuid_answering(answer)

    assert uuid_fact(value) == "?"
    assert len(reads) == 1


def test_the_integer_of_a_uuid_is_read_exactly_once() -> None:
    """A property that answers a valid integer first and an oversized liar afterwards.

    Checking one read and converting another would print the second: 51 hexadecimal digits
    of the caller's choosing. The text must be the canonical form of the FIRST answer.
    """
    first = uuid.UUID(CANONICAL).int
    value, reads = _uuid_answering(first, _OversizedLiar(2**200 + DIGITS))

    text = uuid_fact(value)

    assert text == CANONICAL
    assert len(text) == 36
    assert len(reads) == 1


@pytest.mark.parametrize(
    "value", [CANONICAL, ADDRESS, None, 5, uuid.UUID(CANONICAL).bytes, object()]
)
def test_what_is_not_a_uuid_is_unknown(value: object) -> None:
    assert uuid_fact(value) == "?"


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


_DB_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.asyncio
@pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database")
async def test_a_uuid_read_from_the_database_keeps_its_canonical_text() -> None:
    """The driver's own object, as a query returns it: a subclass, printed as main prints it."""
    connection = await asyncpg.connect(_DB_URL)
    try:
        value = await connection.fetchval("SELECT $1::uuid", CANONICAL)
    finally:
        await connection.close()

    assert type(value) is not uuid.UUID and isinstance(value, uuid.UUID)
    assert uuid_fact(value) == CANONICAL == str(value)


# ── choice_fact and the three sets ───────────────────────────────────────────


@pytest.mark.parametrize(
    "choices",
    [LANGUAGES, COMMISSION_STATUSES, COMMISSION_ENTRY_TYPES],
    ids=["lang", "status", "type"],
)
def test_every_member_of_a_set_is_printed_as_itself(choices: frozenset[str]) -> None:
    assert {choice_fact(member, choices) for member in choices} == set(choices)


@pytest.mark.parametrize(
    "value",
    [ADDRESS, "EN", "en ", "", _LyingStr("en"), None, 7, b"en", ["en"], ("en",), object()],
    ids=[
        "another string",
        "another case",
        "a trailing space",
        "empty",
        "a str subclass equal to a member",
        "none",
        "an int",
        "bytes",
        "a list",
        "a tuple",
        "an object",
    ],
)
def test_what_is_not_an_exact_member_is_unknown(value: object) -> None:
    assert choice_fact(value, LANGUAGES) == "?"


def test_a_value_that_aborts_when_asked_what_it_is_is_unknown_to_every_function() -> None:
    value = _CannotSayWhatItIs()

    assert uuid_fact(value) == "?"
    assert (error_word(value), status_fact(value), id_fact(value)) == ("other", "?", "?")
    assert choice_fact(value, LANGUAGES) == "?"


def test_a_set_that_is_not_one_of_the_three_is_never_asked() -> None:
    """Only the three sets written in the module are asked, and by identity.

    So a caller's set cannot answer yes to an address, cannot raise, and no code of a set,
    of its members or of the value runs inside the function.
    """
    asked: list[object] = []

    class SaysYes(frozenset):  # type: ignore[type-arg]
        def __contains__(self, item: object) -> bool:
            asked.append(item)
            return True

    class Aborts(frozenset):  # type: ignore[type-arg]
        def __contains__(self, item: object) -> bool:
            asked.append(item)
            raise _Abort(ADDRESS)

    class EqualsAnything:
        def __hash__(self) -> int:
            return hash(ADDRESS)

        def __eq__(self, other: object) -> bool:
            asked.append(other)
            return True

    class AbortingStr(str):
        def __hash__(self) -> int:
            asked.append(self)
            raise _Abort(ADDRESS)

        def __eq__(self, other: object) -> bool:
            asked.append(self)
            raise _Abort(ADDRESS)

    assert choice_fact(ADDRESS, SaysYes({"en"})) == "?"
    assert choice_fact("en", SaysYes({"en"})) == "?"
    assert choice_fact("en", Aborts({"en"})) == "?"
    assert choice_fact(ADDRESS, frozenset({EqualsAnything()})) == "?"  # type: ignore[arg-type]
    assert choice_fact("en", frozenset(LANGUAGES)) == "en"  # frozenset() of one returns it
    assert choice_fact("en", frozenset({"en", "id", "it", "ru", "uk"})) == "?"  # equal, another
    assert choice_fact("en", {"en"}) == "?"  # type: ignore[arg-type]
    assert choice_fact("en", None) == "?"  # type: ignore[arg-type]
    assert choice_fact(AbortingStr("en"), LANGUAGES) == "?"
    assert asked == []


def test_rebinding_a_public_set_does_not_change_what_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SaysYes(frozenset):  # type: ignore[type-arg]
        def __contains__(self, item: object) -> bool:
            return True

    original = LANGUAGES
    monkeypatch.setattr(log_facts, "LANGUAGES", SaysYes({"en"}))

    assert choice_fact(ADDRESS, log_facts.LANGUAGES) == "?"
    assert choice_fact("en", log_facts.LANGUAGES) == "?"
    assert choice_fact("en", original) == "en"


def test_the_three_sets_are_exact_frozensets_of_exact_strings() -> None:
    for choices in (LANGUAGES, COMMISSION_STATUSES, COMMISSION_ENTRY_TYPES):
        assert type(choices) is frozenset
        assert [member for member in choices if type(member) is not str] == []


def test_the_sets_hold_what_the_senders_can_print() -> None:
    assert LANGUAGES == {"en", "id", "it", "ru", "uk"}
    assert set(NATIONALITY_LANGUAGE_MAP.values()) | {"en"} <= LANGUAGES
    assert (
        COMMISSION_STATUSES
        == set(get_args(CommissionStatus))
        == {
            "accrued",
            "approved",
            "paid",
            "clawback_pending",
            "offset_applied",
            "waived",
            "repaid",
        }
    )
    assert (
        COMMISSION_ENTRY_TYPES
        == set(get_args(CommissionEntryType))
        == {
            "accrual",
            "clawback",
            "manual_adjustment",
        }
    )


# ── total, and an exact str every time ───────────────────────────────────────

_ANYTHING: list[Any] = [
    None,
    True,
    0,
    7,
    400,
    DIGITS,
    12.0,
    "12",
    ADDRESS,
    b"zz",
    ["en"],
    {"en": 1},
    object(),
    ValueError,
    RuntimeError(ADDRESS),
    httpx.ConnectError(ADDRESS),
    http.HTTPStatus.BAD_REQUEST,
    _LyingInt(400),
    _LyingStr("en"),
    _LyingUUID(CANONICAL),
    _OversizedLiar(2**200),
    _ClaimsAnotherClass(ADDRESS),
    uuid.UUID(CANONICAL),
    DriverUUID(CANONICAL),
    _uuid_answering(RuntimeError(ADDRESS))[0],
    _uuid_answering(_Abort(ADDRESS))[0],
    _uuid_answering(KeyboardInterrupt())[0],
    _uuid_answering(ADDRESS)[0],
]


@pytest.mark.parametrize("value", _ANYTHING, ids=lambda value: type(value).__name__)
def test_every_function_answers_any_object_with_an_exact_str_and_never_the_object(
    value: object,
) -> None:
    answers = [
        error_word(value),
        status_fact(value),
        id_fact(value),
        uuid_fact(value),
        choice_fact(value, LANGUAGES),
        choice_fact(value, COMMISSION_STATUSES),
    ]

    assert [type(answer) for answer in answers] == [str] * len(answers)
    assert all(answer is not value for answer in answers if type(value) is not str)
    assert [answer for answer in answers if ADDRESS in answer or "62812" in answer] == []
