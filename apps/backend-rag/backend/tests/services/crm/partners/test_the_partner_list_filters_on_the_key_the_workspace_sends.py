"""The partner list filters on ``onboarding_status``, the key the workspace sends.

The workspace's list page and its referrer picker build this request
(apps/mouth, src/lib/api/partners/partners.ts; the test beside it pins the address).
The route runs here over the real service, the real repository and the real table:
the key the server reads filters, and ``status``, the key the page sent before,
filters nothing because the route does not declare it.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

import backend.app.routers.partners as partners_module
from backend.app.dependencies import get_current_user, get_database_pool

NAMES = {"pending_approval": "Zz Pending", "active": "Zz Active", "inactive": "Zz Inactive"}


class _OneConnection:
    """A pool that lends the test's own connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _OneConnection:
        return self

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


async def _listed(db_conn: Any, query: str) -> list[str]:
    """The names of this test's partners in the answer to ``GET /api/partners?<query>``."""
    application = FastAPI()
    application.include_router(partners_module.router)
    application.dependency_overrides[get_current_user] = lambda: {
        "user_id": "team-1",
        "email": "team@example.test",
        "role": "admin",
        "permissions": [],
    }
    application.dependency_overrides[get_database_pool] = lambda: _OneConnection(db_conn)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://kita.test"
    ) as client:
        response = await client.get(f"/api/partners?page_size=200&{query}")
    assert response.status_code == 200, response.text
    return sorted(
        row["full_name"]
        for row in response.json()["partners"]
        if row["full_name"] in NAMES.values()
    )


@pytest.fixture
async def three_partners(db_conn: Any, partner_factory: Any) -> None:
    for status, name in NAMES.items():
        partner = await partner_factory(full_name=name)
        await db_conn.execute(
            "UPDATE partners SET onboarding_status = $2 WHERE id = $1", partner, status
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending_approval", "active", "inactive"])
async def test_the_key_the_server_reads_keeps_the_partners_of_that_status(
    db_conn: Any, three_partners: None, status: str
) -> None:
    assert await _listed(db_conn, f"onboarding_status={status}") == [NAMES[status]]


@pytest.mark.asyncio
async def test_the_key_the_page_sent_before_filters_nothing(
    db_conn: Any, three_partners: None
) -> None:
    everyone = ["Zz Active", "Zz Inactive", "Zz Pending"]

    assert await _listed(db_conn, "status=pending_approval") == everyone
    assert await _listed(db_conn, "page=1") == everyone
