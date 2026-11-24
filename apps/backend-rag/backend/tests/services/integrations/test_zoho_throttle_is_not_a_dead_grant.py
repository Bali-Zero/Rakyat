from __future__ import annotations

from typing import Any

import pytest

from backend.services.integrations.zoho_oauth_service import ZohoOAuthService


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.execute_calls.append((sql, args))
        return "OK"


class FakeResponse:
    status_code = 200
    content = b"payload"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def post(self, _url: str, **_kwargs: Any) -> FakeResponse:
        return self.response


def make_service(connection: FakeConnection, response: FakeResponse) -> ZohoOAuthService:
    service = ZohoOAuthService.__new__(ZohoOAuthService)
    service.db_pool = FakePool(connection)
    service.client_id = "client-900001"
    service.client_secret = "secret-900001"  # pragma: allowlist secret
    service.accounts_url = "https://accounts.zoho.test"
    service._get_client = lambda: FakeClient(response)  # type: ignore[method-assign]
    return service


def invalidated(connection: FakeConnection) -> bool:
    return any(
        "SET token_expires_at = NOW() - INTERVAL '1 year'" in sql
        for sql, _ in connection.execute_calls
    )


@pytest.mark.asyncio
async def test_documented_throttle_does_not_invalidate_a_healthy_grant() -> None:
    connection = FakeConnection()
    service = make_service(
        connection,
        FakeResponse(
            {
                "error": "Access Denied",
                "error_description": "You have made too many requests continuously. Please try again after some time.",
            },
        ),
    )

    with pytest.raises(ValueError) as excinfo:
        await service._refresh_token("user-900001", "acct-900001", "tok-a")

    assert "retryable" in str(excinfo.value)
    assert "/admin/zoho/auth" not in str(excinfo.value)
    assert not connection.execute_calls


@pytest.mark.asyncio
async def test_normalized_throttle_body_does_not_invalidate_a_healthy_grant() -> None:
    connection = FakeConnection()
    service = make_service(
        connection,
        FakeResponse(
            {
                "error": "access_denied",
                "error_description": "TOO MANY REQUESTS",
            },
        ),
    )

    with pytest.raises(ValueError, match="retryable") as excinfo:
        await service._refresh_token("user-900001", "acct-900001", "tok-a")

    assert "/admin/zoho/auth" not in str(excinfo.value)
    assert not connection.execute_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"error": "invalid_code"},
        {"error": "Access Denied"},
        {"error": "Access Denied", "error_description": "Try again tomorrow"},
    ],
)
async def test_other_error_bodies_still_invalidate_the_grant(payload: dict[str, str]) -> None:
    connection = FakeConnection()
    service = make_service(connection, FakeResponse(payload))

    with pytest.raises(ValueError, match="/admin/zoho/auth"):
        await service._refresh_token("user-900001", "acct-900001", "tok-a")

    assert invalidated(connection)


@pytest.mark.asyncio
async def test_invalid_client_still_leaves_the_grant_alone() -> None:
    connection = FakeConnection()
    service = make_service(connection, FakeResponse({"error": "invalid_client"}))

    with pytest.raises(ValueError, match="invalid_client"):
        await service._refresh_token("user-900001", "acct-900001", "tok-a")

    assert not connection.execute_calls


@pytest.mark.asyncio
async def test_good_refresh_still_stores_the_new_token() -> None:
    connection = FakeConnection()
    service = make_service(
        connection,
        FakeResponse({"access_token": "tok-b", "expires_in": 3600}),
    )

    assert await service._refresh_token("user-900001", "acct-900001", "tok-a") == "tok-b"
    assert len(connection.execute_calls) == 1
    assert "SET access_token = $1" in connection.execute_calls[0][0]
    assert connection.execute_calls[0][1][0] == "tok-b"
