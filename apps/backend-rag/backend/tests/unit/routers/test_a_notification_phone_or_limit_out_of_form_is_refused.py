"""A notification phone or limit that is out of form is refused before anything is stored.

The WhatsApp number of a client's notification preferences was checked with ``match`` against
a pattern ending in ``$``, which in Python also matches just before a final newline, and with
``\\d``, which takes the digits of every script. So the server stored values that the page's
own check, written with the same pattern, refuses, and the reminder job read them back as the
number to send to. The notifications list took a limit with no lower bound: a negative one
reached the query and came back as "temporarily unavailable".
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_database_pool
from backend.app.routers import portal_notifications
from backend.app.routers.portal import get_current_client
from backend.app.routers.portal_notification_prefs import router as prefs_router

CLIENT = {"client_id": 42, "user_id": "550e8400-e29b-41d4-a716-446655440000"}
NUMBER = "6281234567890"


class _Pool:
    def __init__(self, conn: AsyncMock) -> None:
        self._conn = conn

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _app(conn: AsyncMock) -> TestClient:
    app = FastAPI()
    app.include_router(prefs_router)
    app.include_router(portal_notifications.router)
    app.dependency_overrides[get_current_client] = lambda: CLIENT
    app.dependency_overrides[get_database_pool] = lambda: _Pool(conn)
    return TestClient(app)


def _put(conn: AsyncMock, phone: Any) -> Any:
    return _app(conn).put(
        "/api/portal/notifications/prefs",
        json={"email_enabled": True, "wa_enabled": True, "wa_phone": phone},
    )


@pytest.mark.parametrize(
    "phone",
    [
        NUMBER + "\n",
        NUMBER + "\r\n",
        NUMBER + " ",
        " " + NUMBER,
        "62812 34567890",
        "+" + NUMBER,
        "0" + NUMBER,
        "62٨١٢٣٤٥٦٧٨٩٠",
        "62８１２３４５６",
        "123456",
        "1234567890123456",
    ],
    ids=[
        "final newline",
        "final CRLF",
        "final space",
        "leading space",
        "space inside",
        "plus sign",
        "leading zero",
        "Arabic-Indic digits",
        "fullwidth digits",
        "six digits",
        "sixteen digits",
    ],
)
def test_a_phone_that_is_not_ascii_digits_from_end_to_end_is_refused_and_not_stored(
    phone: str,
) -> None:
    conn = AsyncMock()

    answer = _put(conn, phone)

    assert answer.status_code == 422
    conn.execute.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("phone", [NUMBER, "1234567", "123456789012345"])
def test_a_phone_in_form_is_stored_as_it_was_sent(phone: str) -> None:
    conn = AsyncMock()

    answer = _put(conn, phone)

    assert answer.status_code == 200
    (stored,) = [call for call in conn.mock_calls if phone in call.args]
    assert phone in stored.args


@pytest.mark.parametrize("limit", ["0", "-1", "-100", "101"])
def test_a_limit_outside_one_to_a_hundred_is_refused_before_the_query(limit: str) -> None:
    conn = AsyncMock()
    with patch.object(portal_notifications, "_get_notifications", new=AsyncMock()) as query:
        answer = _app(conn).get("/api/portal/notifications", params={"limit": limit})

    assert answer.status_code == 422
    query.assert_not_awaited()
    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize(("limit", "asked"), [(None, 50), ("1", 1), ("100", 100)])
def test_a_limit_in_range_reaches_the_query(limit: str | None, asked: int) -> None:
    conn = AsyncMock()
    params = {} if limit is None else {"limit": limit}
    with patch.object(
        portal_notifications, "_get_notifications", new=AsyncMock(return_value=[])
    ) as query:
        answer = _app(conn).get("/api/portal/notifications", params=params)

    assert answer.status_code == 200
    assert answer.json() == {"success": True, "data": []}
    assert query.await_args.args[1:] == (42, asked)
