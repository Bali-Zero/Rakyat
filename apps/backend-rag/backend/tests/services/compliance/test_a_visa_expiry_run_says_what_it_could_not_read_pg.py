"""A visa expiry run says which document types it could not read.

The run reads visa, KITAS and passport expiries with three queries. A query that failed was
logged and skipped: the team leaders' mails and the summary went out without that document
type, worded as complete lists, and the run answered like any other. Here the three queries
run against a real ``clients`` table in a private schema, in the shapes that make one of them
fail.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import httpx
import pytest

import backend.services.compliance.visa_expiry_team_notifier as visa_mod
from backend.services.compliance.visa_expiry_team_notifier import VisaExpiryTeamNotifier

_DB_URL = os.environ.get("TEST_DATABASE_URL")


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

_COLUMNS = {
    "id": "INTEGER PRIMARY KEY",
    "full_name": "TEXT",
    "email": "TEXT",
    "phone": "TEXT",
    "nationality": "TEXT",
    "assigned_to": "TEXT",
    "current_visa_type": "TEXT",
    "current_visa_sponsor": "TEXT",
    "visa_expiry_date": "DATE",
    "kitas_expiry_date": "DATE",
    "passport_expiry": "DATE",
    "deleted_at": "TIMESTAMPTZ",
}
EVERY_COLUMN = dict(_COLUMNS)
# The KITAS query fails without a missing column: a date cannot be compared with a text.
KITAS_DATE_STORED_AS_TEXT = {**_COLUMNS, "kitas_expiry_date": "TEXT"}
NO_KITAS_COLUMN = {name: kind for name, kind in _COLUMNS.items() if name != "kitas_expiry_date"}
# Every one of the three queries selects this column.
NO_VISA_TYPE_COLUMN = {name: kind for name, kind in _COLUMNS.items() if name != "current_visa_type"}

LEADER_A = "leader.a@example.test"
LEADER_B = "leader.b@example.test"
NOTICE = "This list is incomplete."


def _in_days(days: int) -> date:
    return date.today() + timedelta(days=days)


@asynccontextmanager
async def _clients_table(
    columns: dict[str, str], rows: list[dict[str, Any]]
) -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"visa_expiry_pg_{uuid4().hex[:12]}"
    ddl = ", ".join(f"{name} {kind}" for name, kind in columns.items())
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=1, server_settings={"search_path": schema}
            )
            try:
                async with pool.acquire() as conn:
                    await conn.execute(f"CREATE TABLE clients ({ddl})")
                    for row in rows:
                        names = ", ".join(row)
                        marks = ", ".join(f"${index}" for index in range(1, len(row) + 1))
                        await conn.execute(
                            f"INSERT INTO clients ({names}) VALUES ({marks})", *row.values()
                        )
                yield pool
            finally:
                await pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


async def _run(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One run of the notifier; the mails it posted, as the send endpoint received them."""
    mails: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        mails.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "message": "Email sent"})

    monkeypatch.setattr(
        VisaExpiryTeamNotifier,
        "_get_http_client",
        staticmethod(lambda: httpx.AsyncClient(transport=httpx.MockTransport(answer))),
    )
    result = await VisaExpiryTeamNotifier(pool).check_and_notify()
    return result, mails


async def test_a_run_that_read_every_type_says_nothing_about_an_incomplete_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": 1,
            "full_name": "Zz One",
            "assigned_to": LEADER_A,
            "kitas_expiry_date": _in_days(10),
        },
        {"id": 2, "full_name": "Zz Two", "assigned_to": LEADER_A, "passport_expiry": _in_days(20)},
        {
            "id": 3,
            "full_name": "Zz Three",
            "assigned_to": LEADER_B,
            "visa_expiry_date": _in_days(5),
        },
    ]
    async with _clients_table(EVERY_COLUMN, rows) as pool:
        result, mails = await _run(pool, monkeypatch)

    assert result == {"total_alerts": 3, "emails_sent": 3, "emails_failed": 0}
    assert sorted(mail["to"] for mail in mails) == sorted(
        [LEADER_A, LEADER_B, visa_mod.ADMIN_EMAIL]
    )
    for mail in mails:
        assert "incomplete" not in mail["subject"].lower()
        assert "incomplete" not in mail["body"].lower()


@pytest.mark.parametrize(
    "columns",
    [KITAS_DATE_STORED_AS_TEXT, NO_KITAS_COLUMN],
    ids=["the query fails", "the column is missing"],
)
async def test_a_type_that_could_not_be_read_is_named_in_the_result_and_in_every_mail(
    columns: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"id": 1, "full_name": "Zz One", "assigned_to": LEADER_A, "passport_expiry": _in_days(20)},
    ]
    async with _clients_table(columns, rows) as pool:
        result, mails = await _run(pool, monkeypatch)

    # The passport query runs after the KITAS one, on the same connection, and is read.
    assert result == {
        "total_alerts": 1,
        "emails_sent": 2,
        "emails_failed": 0,
        "failed_queries": ["kitas"],
    }
    by_recipient = {mail["to"]: mail for mail in mails}
    assert set(by_recipient) == {LEADER_A, visa_mod.ADMIN_EMAIL}
    assert by_recipient[LEADER_A]["subject"].endswith("1 client to contact (incomplete list)")
    assert by_recipient[visa_mod.ADMIN_EMAIL]["subject"].endswith(
        "1 document expiring — team summary (incomplete)"
    )
    for mail in mails:
        assert NOTICE in mail["body"]
        assert "could not be read in this run: KITAS." in mail["body"]
        assert "Zz One" in mail["body"]


async def test_with_nothing_else_expiring_the_admin_is_still_told(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": 1, "full_name": "Zz One", "assigned_to": LEADER_A, "passport_expiry": _in_days(200)},
    ]
    async with _clients_table(KITAS_DATE_STORED_AS_TEXT, rows) as pool:
        result, mails = await _run(pool, monkeypatch)

    assert result == {
        "total_alerts": 0,
        "emails_sent": 1,
        "emails_failed": 0,
        "failed_queries": ["kitas"],
    }
    assert [mail["to"] for mail in mails] == [visa_mod.ADMIN_EMAIL]
    assert mails[0]["subject"].endswith("0 documents expiring — team summary (incomplete)")
    assert "could not be read in this run: KITAS." in mails[0]["body"]


async def test_a_run_that_read_no_type_names_all_three(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"id": 1, "full_name": "Zz One", "assigned_to": LEADER_A, "passport_expiry": _in_days(20)},
    ]
    async with _clients_table(NO_VISA_TYPE_COLUMN, rows) as pool:
        result, mails = await _run(pool, monkeypatch)

    assert result["failed_queries"] == ["visa", "kitas", "passport"]
    assert result["total_alerts"] == 0
    assert [mail["to"] for mail in mails] == [visa_mod.ADMIN_EMAIL]
    assert "could not be read in this run: VISA, KITAS, PASSPORT." in mails[0]["body"]


async def test_nothing_expiring_and_every_type_read_sends_no_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {"id": 1, "full_name": "Zz One", "assigned_to": LEADER_A, "passport_expiry": _in_days(200)},
    ]
    async with _clients_table(EVERY_COLUMN, rows) as pool:
        result, mails = await _run(pool, monkeypatch)

    assert result == {"total_alerts": 0, "emails_sent": 0, "emails_failed": 0}
    assert mails == []
