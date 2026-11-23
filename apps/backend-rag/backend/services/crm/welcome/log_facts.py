"""What the welcome and partner senders may put in a log line.

Their log lines are read by people who must not learn a client's address or phone from
them. A filter over text written elsewhere cannot promise that, so these modules log no
outside text at all: every value they print is one of our own record ids, a member of a
set written here, or a word chosen here. Each function below is total and returns an exact
``str``, so a log record holds nothing a caller passed. Total means that nothing the VALUE
does can make the function raise, an interrupt class raised by the value's own code
included: a sender must not fail in a log call where it did not fail before.

The printed form is decided by the value's exact type, never by what the object says about
itself: an ``int`` subclass or a ``UUID`` subclass can print anything from ``__str__``.
"""

from __future__ import annotations

from typing import get_args
from uuid import UUID

import asyncpg
import httpx

from backend.services.crm.partners.models import CommissionEntryType, CommissionStatus

UNKNOWN = "?"

LANGUAGES = frozenset({"en", "id", "it", "ru", "uk"})
COMMISSION_STATUSES = frozenset(get_args(CommissionStatus))
COMMISSION_ENTRY_TYPES = frozenset(get_args(CommissionEntryType))
# The three sets as they were built at import: a later rebinding of the public names does
# not change what ``choice_fact`` asks.
_LANGUAGES, _STATUSES, _ENTRY_TYPES = LANGUAGES, COMMISSION_STATUSES, COMMISSION_ENTRY_TYPES

_MAX_ID = 2_147_483_647  # the id columns are 32-bit integers
_UUID_INTS = 1 << 128

# First match wins: a timeout is not a connection error, an asyncpg data error is a
# database error before it is a value error.
_ERROR_WORDS: tuple[tuple[str, tuple[type[BaseException], ...]], ...] = (
    ("timeout", (httpx.TimeoutException, TimeoutError)),
    ("connection", (httpx.TransportError, ConnectionError)),
    ("http", (httpx.HTTPError,)),
    ("database", (asyncpg.PostgresError, asyncpg.InterfaceError)),
    ("os", (OSError,)),
    ("lookup", (LookupError,)),
    ("value", (ValueError,)),
    ("type", (TypeError,)),
    ("attribute", (AttributeError,)),
)


def error_word(exc: object) -> str:
    """One word for an error, by the real class of the object and by nothing it carries."""
    real_class = type(exc)
    for word, classes in _ERROR_WORDS:
        if issubclass(real_class, classes):
            return word
    return "other"


def status_fact(value: object) -> str:
    """An HTTP status as digits, only for an exact ``int`` in the status range."""
    if type(value) is int and 100 <= value <= 599:
        return str(value)
    return UNKNOWN


def id_fact(value: object) -> str:
    """A record id as digits, only for an exact ``int`` in the id columns' range."""
    if type(value) is int and 1 <= value <= _MAX_ID:
        return str(value)
    return UNKNOWN


def uuid_fact(value: object) -> str:
    """The canonical text of a UUID, rebuilt from one read of its integer.

    The driver returns a ``UUID`` subclass, so the type is not required to be exact. The
    integer is read once: a property could answer one value to a check and another to the
    conversion.

    Asking the value what it is, and reading its integer, run the value's own code. Whatever
    that code raises gives "?", ``BaseException`` included and with no class let through: a
    value that cannot be read is not a UUID. The cost is said here: an interrupt that
    arrives while this guard runs is taken for the value's own and ends as "?".
    """
    try:
        if isinstance(value, UUID):
            n = value.int
            if type(n) is int and 0 <= n < _UUID_INTS:
                return str(UUID(int=n))
    except BaseException:
        return UNKNOWN
    return UNKNOWN


def choice_fact(value: object, choices: frozenset[str]) -> str:
    """The value itself, only for an exact ``str`` that is in one of the three sets above.

    ``choices`` must BE one of them, the same object. A set a caller built is never asked,
    so no code of a set, of its members or of the value runs here, nothing can raise, and
    nothing but a literal written in this module can be returned.
    """
    known = choices is _LANGUAGES or choices is _STATUSES or choices is _ENTRY_TYPES
    if type(value) is str and known and value in choices:
        return value
    return UNKNOWN
