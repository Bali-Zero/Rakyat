"""
Guard: who may create a company (POST /api/crm/companies).

The tax team (Tax Consultant / Lead / Supervisor / Responsible) may create a
company besides the CRM admins. Edit (PATCH) and delete stay admin-only.
Synthetic users only (@example.test), no real identifiers.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_for_testing_only_min_32_chars_long")
os.environ.setdefault("API_KEYS", "test_api_key_1,test_api_key_2")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-for-testing-only")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-api-key")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/test")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test_whatsapp_verify_token")
os.environ.setdefault("INSTAGRAM_VERIFY_TOKEN", "test_instagram_verify_token")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.modules.crm.company_router import router

TAX_ROLES = ["Tax Consultant", "tax lead", "Tax Supervisor", " Tax Responsible "]
REFUSED_ROLES = ["consultant", "junior consultant", "tax care", "reception", "team leader", ""]


def _user(role: str) -> dict:
    return {"id": "user-synthetic", "email": "synthetic.staff@example.test", "role": role}


def _pool() -> tuple[MagicMock, AsyncMock]:
    pool = MagicMock()
    conn = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    row = MagicMock()
    row.__getitem__ = lambda s, k: {
        "id": 1,
        "uuid": "uuid-synthetic",
        "company_name": "Synthetic PT",
        "company_type": "PT PMA",
        "status": "active",
    }[k]
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock(return_value="DELETE 1")
    return pool, conn


def _client(user: dict) -> tuple[TestClient, AsyncMock]:
    app = FastAPI()
    app.include_router(router)
    pool, conn = _pool()
    app.dependency_overrides[get_database_pool] = lambda: pool
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), conn


def _create(tc: TestClient):
    return tc.post(
        "/api/crm/companies",
        json={"company_name": "Synthetic PT", "company_type": "PT PMA"},
    )


@pytest.mark.parametrize("role", TAX_ROLES)
def test_tax_team_can_create_company(role):
    tc, conn = _client(_user(role))
    with tc:
        resp = _create(tc)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Company created successfully"
    conn.fetchrow.assert_awaited_once()


@pytest.mark.parametrize("role", ["admin", "Founder", "CEO", "board member"])
def test_admin_roles_still_create_company(role):
    tc, _ = _client(_user(role))
    with tc:
        resp = _create(tc)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("role", REFUSED_ROLES)
def test_other_roles_are_refused_and_write_nothing(role):
    tc, conn = _client(_user(role))
    with tc:
        resp = _create(tc)
    assert resp.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("role", TAX_ROLES)
def test_tax_team_still_cannot_edit_company(role):
    tc, conn = _client(_user(role))
    with tc:
        resp = tc.patch("/api/crm/companies/1", json={"company_name": "Renamed PT"})
    assert resp.status_code == 403
    conn.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("role", TAX_ROLES)
def test_tax_team_still_cannot_delete_company(role):
    tc, conn = _client(_user(role))
    with tc:
        resp = tc.delete("/api/crm/companies/1")
    assert resp.status_code == 403
    conn.execute.assert_not_awaited()


def test_can_create_company_helper():
    from backend.app.utils.crm_utils import can_create_company

    assert can_create_company(_user("Tax Consultant")) is True
    assert can_create_company(_user("admin")) is True
    assert can_create_company(_user("consultant")) is False
    assert can_create_company({}) is False
