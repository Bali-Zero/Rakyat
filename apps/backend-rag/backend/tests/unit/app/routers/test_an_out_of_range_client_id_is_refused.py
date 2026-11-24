"""An out-of-range client_id is refused with a validation error, never a 500.

clients.id is an INTEGER; asyncpg cannot bind a larger value and raised a
DataError that surfaced as an unhandled 500.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import compliance_obligations

_TOO_BIG = 2_147_483_648
_INT4_MAX = 2_147_483_647


class _Conn:
    async def fetchrow(self, sql: str, *args: Any) -> None:
        return None

    async def fetch(self, sql: str, *args: Any) -> list:
        return []

    def transaction(self) -> _Acquire:
        return _Acquire()


class _Acquire:
    async def __aenter__(self) -> _Conn:
        return _Conn()

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self) -> _Acquire:
        self.acquired += 1
        return _Acquire()


@pytest.fixture
def client_and_pool() -> tuple[TestClient, _Pool]:
    app = FastAPI()
    app.include_router(compliance_obligations.router)
    pool = _Pool()
    app.dependency_overrides[get_current_user] = lambda: {
        "email": "admin@example.invalid",
        "role": "admin",
    }
    app.dependency_overrides[get_database_pool] = lambda: pool
    return TestClient(app), pool


_BASE = "/api/compliance/obligations"


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", f"{_BASE}/generate", {"json": {"client_id": _TOO_BIG}}),
        ("get", f"{_BASE}/profile/{_TOO_BIG}", {}),
        ("patch", f"{_BASE}/profile/{_TOO_BIG}", {"json": {"company_type": "PT_PMA"}}),
        ("get", f"{_BASE}?client_id={_TOO_BIG}", {}),
    ],
    ids=["generate", "get-profile", "patch-profile", "list"],
)
def test_a_client_id_beyond_int4_is_a_422_before_the_database(
    client_and_pool, method, path, kwargs
) -> None:
    client, pool = client_and_pool

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 422
    assert pool.acquired == 0


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", f"{_BASE}/generate", {"json": {"client_id": _INT4_MAX}}),
        ("get", f"{_BASE}/profile/{_INT4_MAX}", {}),
    ],
    ids=["generate", "get-profile"],
)
def test_the_largest_int4_id_still_answers_client_not_found(
    client_and_pool, method, path, kwargs
) -> None:
    client, pool = client_and_pool

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 404
    assert pool.acquired == 1
