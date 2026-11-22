"""Creating a client logs the kind of failure, never its values.

The text of a database error ends with a DETAIL line that quotes the stored value, the text of
a validation error quotes the value of every failing field, and a traceback ends with that same
text. The service and the repository logged all three, and the repository's date parser put a
malformed date into a structured field of its line. Invented values only.

A value is looked for in everything a record holds, not in its text alone: a formatter that
prints structured fields prints the attributes ``extra`` sets, and the default text of a
captured log leaves them out.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest
from pydantic import ValidationError

from backend.app.core.exceptions import ResourceConflictError
from backend.db.repositories.client_repository import (
    ClientRepository,
    DuplicatePhoneError,
    _parse_date,
)
from backend.services.crm.client_service import ClientService

SERVICE_LOG = "backend.services.crm.client_service"
REPOSITORY_LOG = "backend.db.repositories.client_repository"

ADDRESS = "person.one@example.com"
NOT_AN_ADDRESS = "person.one.example.com"
SHORT_PHONE = "+62 811"
SPACED_PASSPORT = "ZZ 12345"
OWNER_NAME = "Person Two"
OWNER_STAFF = "staff.one@example.com"
COMPANY = "900101"
BIRTH = "31/12/1990"  # ten characters, not the ISO form the parser reads
EXPIRY = "2031-13-45x"  # eleven

UNIQUE = asyncpg.PostgresError.new(
    {
        "S": "ERROR",
        "C": "23505",
        "M": 'duplicate key value violates unique constraint "clients_email_key"',
        "D": f"Key (email)=({ADDRESS}) already exists.",
        "n": "clients_email_key",
    }
)
FOREIGN_KEY = asyncpg.PostgresError.new(
    {
        "S": "ERROR",
        "C": "23503",
        "M": 'insert or update on table "client_company_links" violates foreign key '
        'constraint "client_company_links_company_id_fkey"',
        "D": f'Key (company_id)=({COMPANY}) is not present in table "companies".',
        "n": "client_company_links_company_id_fkey",
    }
)
UNEXPECTED = RuntimeError(f"cannot store {ADDRESS}")
OWNED = DuplicatePhoneError(
    existing_client_id=7, existing_full_name=OWNER_NAME, existing_assigned_to=OWNER_STAFF
)


def test_the_text_of_these_errors_does_carry_the_values() -> None:
    """The premise: what must not reach a log is in the text of the exception."""
    assert isinstance(UNIQUE, asyncpg.UniqueViolationError)
    assert isinstance(FOREIGN_KEY, asyncpg.ForeignKeyViolationError)
    assert ADDRESS in str(UNIQUE)
    assert COMPANY in str(FOREIGN_KEY)


def _everything(caplog: pytest.LogCaptureFixture) -> str:
    """All that the captured records hold: text, arguments and every attribute, extras included."""
    return caplog.text + "".join(
        f"\n{record.getMessage()} {vars(record)!r}" for record in caplog.records
    )


def _messages(caplog: pytest.LogCaptureFixture, logger: str) -> list[tuple[str, str]]:
    return [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == logger
    ]


def _service(error: BaseException) -> ClientService:
    repository = MagicMock()
    repository.create_client_with_details = AsyncMock(side_effect=error)
    return ClientService(repository)


# ── the service ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_duplicate_is_logged_by_its_constraint(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ResourceConflictError) as conflict:
        await _service(UNIQUE).create_client({"full_name": "Person One", "email": ADDRESS})

    assert conflict.value.__cause__ is UNIQUE
    assert _messages(caplog, SERVICE_LOG) == [
        (
            "ERROR",
            "Conflitto di risorse: email o numero di telefono già presenti "
            "(vincolo clients_email_key)",
        )
    ]
    assert ADDRESS not in _everything(caplog)


@pytest.mark.asyncio
async def test_invalid_input_is_logged_by_field_and_kind(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    service = _service(AssertionError("the repository is not reached"))

    with pytest.raises(ValidationError) as refused:
        await service.create_client(
            {
                "full_name": "Person One",
                "email": NOT_AN_ADDRESS,
                "phone": SHORT_PHONE,
                "passport_number": SPACED_PASSPORT,
            }
        )

    assert SHORT_PHONE in str(refused.value)  # the premise, again
    assert _messages(caplog, SERVICE_LOG) == [
        (
            "ERROR",
            "Dati cliente non validi in create_client: [('email', 'value_error'), "
            "('passport_number', 'value_error'), ('phone', 'value_error')]",
        )
    ]
    for value in (NOT_AN_ADDRESS, SHORT_PHONE, SPACED_PASSPORT):
        assert value not in _everything(caplog)


@pytest.mark.asyncio
async def test_an_unexpected_failure_is_logged_by_kind_and_place(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError) as raised:
        await _service(UNEXPECTED).create_client({"full_name": "Person One", "email": ADDRESS})

    assert raised.value is UNEXPECTED
    [(level, message)] = _messages(caplog, SERVICE_LOG)
    first_line, _, place = message.partition("\n")
    assert (level, first_line) == (
        "ERROR",
        "Errore imprevisto nella logica di business di create_client (RuntimeError)",
    )
    assert "client_service.py" in place
    assert "in create_client" in place
    assert ADDRESS not in _everything(caplog)


@pytest.mark.asyncio
async def test_a_phone_that_has_an_owner_is_not_logged_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(DuplicatePhoneError) as raised:
        await _service(OWNED).create_client({"full_name": "Person One"})

    assert raised.value is OWNED
    assert _messages(caplog, SERVICE_LOG) == []


# ── the repository ────────────────────────────────────────────────────────────


class _Entered:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _repository(fetchrow: AsyncMock) -> ClientRepository:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Entered())
    conn.fetchrow = fetchrow
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Entered(conn))
    return ClientRepository(pool)


NEW_CLIENT = {"full_name": "Person One", "email": ADDRESS, "phone": "+62 811 5550 1000"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "line"),
    [
        (
            UNIQUE,
            "Violazione constraint di unicità nella creazione cliente (vincolo clients_email_key)",
        ),
        (
            FOREIGN_KEY,
            "Violazione Foreign Key durante la creazione cliente/compagnia "
            "(vincolo client_company_links_company_id_fkey)",
        ),
    ],
    ids=["unique", "foreign key"],
)
async def test_the_repository_logs_a_refused_insert_by_its_constraint(
    caplog: pytest.LogCaptureFixture, error: asyncpg.PostgresError, line: str
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(type(error)) as raised:
        await _repository(AsyncMock(side_effect=error)).create_client_with_details(NEW_CLIENT)

    assert raised.value is error
    assert _messages(caplog, REPOSITORY_LOG) == [("ERROR", line)]
    assert ADDRESS not in _everything(caplog)
    assert COMPANY not in _everything(caplog)


@pytest.mark.asyncio
async def test_the_repository_logs_an_unexpected_failure_by_kind_and_place(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(RuntimeError):
        await _repository(AsyncMock(side_effect=UNEXPECTED)).create_client_with_details(NEW_CLIENT)

    [(level, message)] = _messages(caplog, REPOSITORY_LOG)
    first_line, _, place = message.partition("\n")
    assert (level, first_line) == (
        "ERROR",
        "Fallimento critico e rollback nella transazione create_client_with_details (RuntimeError)",
    )
    assert "client_repository.py" in place
    assert ADDRESS not in _everything(caplog)


@pytest.mark.asyncio
async def test_the_repository_does_not_log_a_phone_that_has_an_owner_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    owner = {"id": 7, "full_name": OWNER_NAME, "assigned_to": OWNER_STAFF}

    with pytest.raises(DuplicatePhoneError) as raised:
        await _repository(AsyncMock(return_value=owner)).create_client_with_details(
            NEW_CLIENT, enforce_unique_phone_core=True
        )

    assert raised.value.existing_client_id == 7
    assert _messages(caplog, REPOSITORY_LOG) == []
    assert OWNER_NAME not in _everything(caplog)


# ── a malformed date on a creation that fails ─────────────────────────────────


def _date_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The structured fields of each line the date parser wrote."""
    return [
        {key: vars(record).get(key) for key in ("error_type", "value_type", "value_length", "raw")}
        for record in caplog.records
        if record.name == REPOSITORY_LOG
        and record.getMessage() == "client_repository.parse_date_failed"
    ]


WITH_DATES = {**NEW_CLIENT, "passport_expiry": EXPIRY, "date_of_birth": BIRTH}
TWO_DATES_REFUSED = [
    {"error_type": "ValueError", "value_type": "str", "value_length": 11, "raw": None},
    {"error_type": "ValueError", "value_type": "str", "value_length": 10, "raw": None},
]


@pytest.mark.asyncio
async def test_a_malformed_date_is_recorded_by_kind_and_length_in_every_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(asyncpg.UniqueViolationError):
        await _repository(AsyncMock(side_effect=UNIQUE)).create_client_with_details(WITH_DATES)

    assert _date_lines(caplog) == TWO_DATES_REFUSED
    for value in (BIRTH, EXPIRY, ADDRESS):
        assert value not in _everything(caplog)


@pytest.mark.asyncio
async def test_a_failed_creation_with_malformed_dates_leaves_no_value_through_both_layers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The service's validator lets any text through as a date: it reaches the parser."""
    caplog.set_level(logging.DEBUG)
    service = ClientService(_repository(AsyncMock(side_effect=UNIQUE)))

    with pytest.raises(ResourceConflictError):
        await service.create_client(WITH_DATES)

    assert _date_lines(caplog) == TWO_DATES_REFUSED
    assert [level for level, _ in _messages(caplog, SERVICE_LOG)] == ["ERROR"]
    for value in (BIRTH, EXPIRY, ADDRESS):
        assert value not in _everything(caplog)


def test_a_date_that_is_not_text_is_recorded_the_same_way(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    assert _parse_date(19901231) is None  # type: ignore[arg-type]

    assert _date_lines(caplog) == [
        {"error_type": "TypeError", "value_type": "int", "value_length": 8, "raw": None}
    ]
    assert "19901231" not in _everything(caplog)


# ── every field of the form, on every way a creation can end ─────────────────

EVERY_FIELD = {
    "full_name": "Zzq Person",
    "email": ADDRESS,
    "phone": "+62 811 5550 1000",
    "whatsapp": "+62 811 5550 2000",
    "nationality": "Zzlandian",
    "passport_number": "ZZP1234567",
    "address": "Jalan Zzq 77",
    "notes": "zzq-note",
    "tax_id": "ZZT-99",
    "company_name": "PT Zzq Sweep",
    "passport_expiry": EXPIRY,
    "date_of_birth": BIRTH,
}
THE_COMPANY = {"company_name": "PT Zzq Sweep", "nib": "ZZN-0001", "kbli_code": "62019"}
LOOKED_FOR = sorted(
    {*EVERY_FIELD.values(), *THE_COMPANY.values(), "81155501000", "81155502000", OWNER_NAME}
)
STORED = [{"id": 4242, "full_name": "Zzq Person"}, {"id": 77}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "ends_with"),
    [
        (STORED, None),
        ([UNIQUE], ResourceConflictError),
        ([{"id": 4242}, FOREIGN_KEY], asyncpg.ForeignKeyViolationError),
        ([UNEXPECTED], RuntimeError),
        ([{"id": 7, "full_name": OWNER_NAME, "assigned_to": OWNER_STAFF}], DuplicatePhoneError),
    ],
    ids=["stored", "duplicate", "foreign key", "unexpected", "phone has an owner"],
)
async def test_no_field_of_the_form_is_in_any_record_however_the_creation_ends(
    caplog: pytest.LogCaptureFixture, answers: list[Any], ends_with: type[Exception] | None
) -> None:
    caplog.set_level(logging.DEBUG)
    service = ClientService(_repository(AsyncMock(side_effect=answers)))
    create = service.create_client(
        dict(EVERY_FIELD),
        company_data=dict(THE_COMPANY),
        enforce_unique_phone_core=ends_with is DuplicatePhoneError,
    )

    if ends_with is None:
        assert (await create)["id"] == 4242
    else:
        with pytest.raises(ends_with):
            await create

    # A phone that has an owner is refused before the insert is prepared: nothing is
    # written on that path. Every other path must have written, or the sweep proves nothing.
    assert bool(caplog.records) is (ends_with is not DuplicatePhoneError)
    held = _everything(caplog)
    assert [value for value in LOOKED_FOR if value in held] == []


# ── a real duplicate, from a real server ──────────────────────────────────────

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database")
async def test_a_real_duplicate_from_postgresql_is_logged_without_the_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    schema = f"client_create_log_{uuid4().hex[:12]}"
    conn = await asyncpg.connect(_DB_URL)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            await conn.execute(f'CREATE TABLE "{schema}".clients (email text UNIQUE)')
            store = f'INSERT INTO "{schema}".clients (email) VALUES ($1)'
            await conn.execute(store, ADDRESS)

            async def insert_again(**_kwargs: Any) -> None:
                await conn.execute(store, ADDRESS)

            repository = MagicMock()
            repository.create_client_with_details = insert_again

            with pytest.raises(ResourceConflictError) as conflict:
                await ClientService(repository).create_client(
                    {"full_name": "Person One", "email": ADDRESS}
                )
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await conn.close()

    assert ADDRESS in str(conflict.value.__cause__)  # the server did put it in the text
    assert _messages(caplog, SERVICE_LOG) == [
        (
            "ERROR",
            "Conflitto di risorse: email o numero di telefono già presenti "
            "(vincolo clients_email_key)",
        )
    ]
    assert ADDRESS not in _everything(caplog)
