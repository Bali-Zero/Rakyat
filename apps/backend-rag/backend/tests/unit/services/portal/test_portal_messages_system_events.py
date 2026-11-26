"""Portal messages: automatic notifications are events, not staff chat (DAI-217).

Three faults, seen in the client portal on 12 Oct and 26 Nov 2026:

1. ``PATCH /api/crm/clients/{id}`` sent "Profile updated" whenever the Edit
   Client modal resubmitted a significant field, even with the same value
   (the modal always sends ``status``).
2. The message read "Your profile has been updated: status." — a raw field
   key, no value.
3. ``get_messages`` gave the notification the triggering staff member's name,
   so the portal drew it as that person writing in the chat.

All data here is fake: fake names, fake passport numbers, client id 7.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.app.routers.crm_clients as crm_clients_module
from backend.app.dependencies import get_current_user, get_database_pool
from backend.services.portal.portal_notification_service import (
    PortalNotificationService,
    describe_profile_change,
)
from backend.services.portal.portal_service import PortalService

CLIENT_ID = 7
STAFF_EMAIL = "staff.fake@example.test"


def _client_row(**overrides: object) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "id": CLIENT_ID,
        "uuid": "00000000-0000-0000-0000-000000000007",
        "full_name": "Fake Client",
        "email": "fake.client@example.test",
        "phone": "+6289000000007",
        "whatsapp": "+6289000000007",
        "status": "active",
        "client_type": "individual",
        "passport_number": "FAKE0001",
        "passport_expiry": date(2030, 1, 1),
        "address": "Fake Street 1",
        "created_at": now,
        "updated_at": now,
        **overrides,
    }


class _Patch:
    """A PATCH against a fake DB whose row holds ``before`` until the UPDATE."""

    def __init__(self, before: dict[str, Any]) -> None:
        self.before = before
        self.spawned: list[Any] = []
        self.conn = MagicMock()
        self.conn.execute = AsyncMock(return_value="OK")
        self.conn.fetchval = AsyncMock(return_value=101)
        self.conn.fetchrow = AsyncMock(side_effect=self._fetchrow)

        class _Ctx:
            async def __aenter__(inner) -> MagicMock:  # noqa: N805
                return self.conn

            async def __aexit__(inner, *_a: object) -> None:  # noqa: N805
                return None

        self.conn.transaction = MagicMock(return_value=_Ctx())
        self.pool = MagicMock()
        self.pool.acquire = MagicMock(side_effect=lambda: _Ctx())

    async def _fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        if sql.lstrip().upper().startswith("UPDATE"):
            return _client_row()
        return _client_row(**self.before)

    def send(self, body: dict[str, Any]) -> int:
        app = FastAPI()
        app.include_router(crm_clients_module.router)
        user = {"id": "1", "email": STAFF_EMAIL, "role": "admin"}
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: self.pool
        with (
            patch("backend.app.routers.crm_clients.verify_client_access", AsyncMock()),
            patch("backend.app.routers.crm_clients.invalidate_cache", AsyncMock()),
            patch("backend.app.routers.crm_clients.spawn", side_effect=self.spawned.append),
        ):
            response = TestClient(app, raise_server_exceptions=False).patch(
                f"/api/crm/clients/{CLIENT_ID}", json=body
            )
        return response.status_code

    def portal_messages(self) -> list[str]:
        """Run what the endpoint spawned; return the content of each portal message."""
        self.conn.fetchval.reset_mock()
        for coro in self.spawned:
            asyncio.run(coro)
        contents = []
        for call in self.conn.fetchval.await_args_list:
            sql, *params = call.args
            if "INSERT INTO portal_messages" in sql:
                contents.append(params[3])  # client_id, practice_id, subject, content
        return contents


def test_resubmitted_status_with_the_same_value_sends_no_message() -> None:
    req = _Patch(before={"status": "active"})
    assert req.send({"full_name": "Fake Renamed", "status": "active"}) == 200
    assert req.portal_messages() == []


def test_status_change_sends_one_message_with_the_human_status() -> None:
    req = _Patch(before={"status": "prospect"})
    assert req.send({"status": "active"}) == 200
    messages = req.portal_messages()
    assert len(messages) == 1
    assert "Status: Active" in messages[0]
    assert "_" not in messages[0]


def test_passport_number_change_names_the_field_never_the_value() -> None:
    req = _Patch(before={"passport_number": "FAKE0001"})
    assert req.send({"passport_number": "FAKE0002"}) == 200
    messages = req.portal_messages()
    assert len(messages) == 1
    assert "Passport number updated" in messages[0]
    assert "FAKE0002" not in messages[0]
    assert "FAKE0001" not in messages[0]


def test_only_the_fields_that_changed_are_listed() -> None:
    """Same status and same expiry date are resubmitted; only the address moved."""
    req = _Patch(
        before={"status": "active", "passport_expiry": date(2030, 1, 1), "address": "Old 1"}
    )
    body = {"status": "active", "passport_expiry": "2030-01-01", "address": "New 2"}
    assert req.send(body) == 200
    assert req.portal_messages() == ["Your profile has been updated. Address updated."]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("status", "active", "Status: Active"),
        ("status", "", "Status updated"),
        ("passport_expiry", date(2031, 3, 9), "Passport expiry: 9 Mar 2031"),
        ("visa_expiry", "2031-03-09T00:00:00", "Visa expiry: 9 Mar 2031"),
        ("visa_type", "investor_kitas", "Visa type: investor kitas"),
        ("date_of_birth", date(1990, 1, 1), "Date of birth updated"),
        ("address", "Fake Street 1", "Address updated"),
        ("city", "Fake City", "City updated"),
        ("province", "Fake Province", "Province updated"),
    ],
)
def test_each_field_reads_as_a_human_line(field: str, value: object, expected: str) -> None:
    assert describe_profile_change(field, value) == expected


@pytest.mark.asyncio
async def test_old_callers_without_values_still_get_labels_not_keys() -> None:
    conn = AsyncMock()
    conn.fetchval.return_value = 5
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await PortalNotificationService(pool).notify_profile_updated(
        CLIENT_ID, ["passport_expiry", "status"], STAFF_EMAIL
    )

    content = conn.fetchval.await_args.args[4]
    assert content == "Your profile has been updated. Passport expiry updated. Status updated."


def _message_row(**overrides: object) -> dict[str, object]:
    return {
        "id": 1,
        "subject": "Hello",
        "content": "Hello from the team",
        "direction": "team_to_client",
        "sent_by": STAFF_EMAIL,
        "read_at": None,
        "created_at": datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc),
        "practice_id": None,
        "practice_name": None,
        "sender_name": "Staff Fake",
        "is_system_generated": False,
        **overrides,
    }


def _messages_service() -> tuple[PortalService, AsyncMock]:
    conn = AsyncMock()
    conn.fetchrow.return_value = {"id": CLIENT_ID}
    conn.fetch.return_value = [
        _message_row(id=2, subject="Profile updated", is_system_generated=True),
        _message_row(id=1),
    ]
    conn.fetchval.side_effect = [2, 2]
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return PortalService(pool), conn


@pytest.mark.asyncio
async def test_get_messages_marks_system_rows_and_signs_them_bali_zero() -> None:
    service, conn = _messages_service()

    result = await service.get_messages(
        CLIENT_ID,
        current_user={"client_id": CLIENT_ID, "email": "fake.client@example.test"},
    )

    assert "m.is_system_generated" in conn.fetch.await_args.args[0]
    system, staff = result["messages"]
    assert system["is_system"] is True
    assert system["sent_by"] == "Bali Zero"
    assert system["from_team"] is True
    assert staff["is_system"] is False
    assert staff["sent_by"] == "Staff Fake"


@pytest.mark.asyncio
async def test_staff_view_keeps_the_triggering_staff_name() -> None:
    """The kita thread is not redesigned here: same rows, same sender as before."""
    service, _conn = _messages_service()

    result = await service.get_messages(
        CLIENT_ID,
        current_user={"client_id": CLIENT_ID, "email": STAFF_EMAIL, "impersonating": True},
        team_view=True,
    )

    system, staff = result["messages"]
    assert system["is_system"] is True
    assert system["sent_by"] == "Staff Fake"
    assert staff["sent_by"] == "Staff Fake"


def test_the_staff_route_asks_for_the_staff_view() -> None:
    import inspect

    from backend.app.routers import team_invites_messaging

    source = inspect.getsource(team_invites_messaging.get_client_messages)
    assert "team_view=True" in source
