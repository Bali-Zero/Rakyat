"""A refusal the link holder must not learn answers exactly as an unknown link.

`GET /api/portal/invite/validate/{token}` is public. When the client behind an
invitation is a staff login, or another login already holds the invited
address, the answer, its status and its headers are the ones an unknown token
gets, through the real application with its middleware, the rate limiter in
the same state before each request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import main_api as _main_api_module
from backend.app.routers.portal_invite import get_invite_service
from backend.middleware import rate_limiter as _limiter_module
from backend.services.portal.invite_service import InviteService
from backend.services.portal.portal_profile_service import PLACEHOLDER_PIN_HASH

_APP = _main_api_module.app
#: Headers whose value changes from one request to the next whatever the answer
#: (a clock reading or a request id); their presence is still compared.
_PER_REQUEST = {
    "date",
    "x-request-id",
    "x-correlation-id",
    "x-process-time",
    "x-response-time",
    "x-ratelimit-reset",
}


class _Conn:
    def __init__(self, rows: list) -> None:
        self.rows = list(rows)

    async def fetchrow(self, query: str, *args):
        return self.rows.pop(0)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, rows: list) -> None:
        self.conn = _Conn(rows)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _invitation() -> dict:
    return {
        "id": 9,
        "client_id": 7,
        "email": "client@example.test",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "used_at": None,
        "client_name": "Client Name",
        "client_email": "client@example.test",
    }


def _state(role: str | None = None, holder: str | None = None) -> dict:
    return {
        "linked_id": "tm-1" if role else None,
        "linked_active": True if role else None,
        "linked_pin_hash": PLACEHOLDER_PIN_HASH if role else None,
        "linked_role": role,
        "holder_id": holder,
    }


async def _ask(rows: list) -> tuple[int, dict, dict]:
    _limiter_module._rate_limit_storage.clear()
    _APP.dependency_overrides[get_invite_service] = lambda: InviteService(_Pool(rows))
    try:
        async with AsyncClient(
            transport=ASGITransport(app=_APP), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/portal/invite/validate/one-token")
    finally:
        _APP.dependency_overrides.pop(get_invite_service, None)
        _limiter_module._rate_limit_storage.clear()
    headers = {
        name.lower(): "<per request>" if name.lower() in _PER_REQUEST else value
        for name, value in response.headers.items()
    }
    return response.status_code, response.json(), headers


@pytest.mark.asyncio
async def test_a_staff_login_or_a_held_address_answers_as_an_unknown_link() -> None:
    unknown = await _ask([None, _state()])
    assert unknown[0] == 200
    assert unknown[1]["error"] == "invalid_token"

    assert await _ask([_invitation(), _state(role="group_staff")]) == unknown
    assert await _ask([_invitation(), _state(holder="tm-2")]) == unknown


@pytest.mark.asyncio
async def test_the_comparison_sees_an_answer_that_differs() -> None:
    unknown = await _ask([None, _state()])

    registrable = await _ask([_invitation(), _state(role="client")])

    assert registrable[1]["valid"] is True
    assert registrable != unknown
