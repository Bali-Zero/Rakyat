"""
Guard: creating a company whose NIB or NPWP another company already holds
(POST /api/crm/companies) answers 409 naming the owning company id, not 500.

``companies.nib`` and ``companies.npwp_company`` are unique (company_models.py:
``unique=True``; the production-shape fixture spells them ``nib VARCHAR UNIQUE``,
``npwp_company VARCHAR UNIQUE``). The insert used to let asyncpg's
UniqueViolationError reach the generic handler, which shows staff
"Internal server error". Synthetic values only (NIB 0000000000000).
"""

import logging
import os
from unittest.mock import AsyncMock, MagicMock

import asyncpg
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

USER = {"id": "user-synthetic", "email": "synthetic.staff@example.test", "role": "admin"}
NIB = "0000000000000"
NPWP = "00.000.000.0-000.000"
OWNER_ID = 8


def _unique_violation(constraint: str) -> asyncpg.UniqueViolationError:
    err = asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
    err.constraint_name = constraint
    return err


def _client(insert_error: Exception, owner_id: int | None = OWNER_ID):
    pool = MagicMock()
    conn = AsyncMock()
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_cm)
    conn.fetchrow = AsyncMock(side_effect=insert_error)
    conn.fetchval = AsyncMock(return_value=owner_id)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_database_pool] = lambda: pool
    app.dependency_overrides[get_current_user] = lambda: USER
    # raise_server_exceptions=False: an unhandled error is the 500 staff saw.
    return TestClient(app, raise_server_exceptions=False), conn


def _create(tc: TestClient, **fields):
    return tc.post(
        "/api/crm/companies",
        json={"company_name": "PT Synthetic Contoh", "company_type": "PT PMA", **fields},
    )


@pytest.mark.parametrize("constraint", ["companies_nib_key", "ix_companies_nib"])
def test_an_nib_already_in_use_answers_409_naming_the_owner(constraint, caplog):
    tc, conn = _client(_unique_violation(constraint))
    with caplog.at_level(logging.DEBUG), tc:
        resp = _create(tc, nib=NIB)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_nib"
    assert detail["field"] == "nib"
    assert detail["existing_company_id"] == OWNER_ID
    assert f"#{OWNER_ID}" in detail["message"]
    assert NIB not in detail["message"]
    sql, value = conn.fetchval.await_args.args
    assert "WHERE nib = $1" in sql
    assert value == NIB
    assert NIB not in caplog.text


@pytest.mark.parametrize("constraint", ["companies_npwp_company_key", "ix_companies_npwp_company"])
def test_an_npwp_already_in_use_answers_409_naming_the_owner(constraint, caplog):
    tc, conn = _client(_unique_violation(constraint), owner_id=12)
    with caplog.at_level(logging.DEBUG), tc:
        resp = _create(tc, nib=NIB, npwp_company=NPWP)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "duplicate_npwp_company"
    assert detail["field"] == "npwp_company"
    assert detail["existing_company_id"] == 12
    assert NPWP not in detail["message"]
    sql, value = conn.fetchval.await_args.args
    assert "WHERE npwp_company = $1" in sql
    assert value == NPWP
    assert NPWP not in caplog.text
    assert NIB not in caplog.text


def test_an_owner_gone_by_lookup_time_still_answers_409():
    tc, _ = _client(_unique_violation("companies_nib_key"), owner_id=None)
    with tc:
        resp = _create(tc, nib=NIB)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["existing_company_id"] is None
    assert "another company" in detail["message"]


def test_another_unique_violation_is_not_reported_as_a_duplicate_identifier():
    tc, conn = _client(_unique_violation("companies_uuid_key"))
    with tc:
        resp = _create(tc, nib=NIB)

    assert resp.status_code == 500
    conn.fetchval.assert_not_awaited()


def test_a_value_too_long_for_its_column_answers_422():
    err = asyncpg.StringDataRightTruncationError("value too long for type character varying(50)")
    tc, _ = _client(err)
    with tc:
        resp = _create(tc, nib="0" * 60)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "value_too_long"
