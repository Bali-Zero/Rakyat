"""The OSS credentials route answers for ONE company.

A client can report for several companies, and each (client, company) pair has its own
OSS login. The route took a client alone and returned whichever row came first, so the
login of one company could be shown on the row of another. It now takes the company of
the row, and refuses to guess when a client has several and none is named.

No database: the route runs against a pool that serves invented rows.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import lkpm

CLIENT = 900007
CONSULTANT = "consultant.one@example.test"


def _config(company_id: int | None, name: str, client_id: int = CLIENT) -> dict:
    return {
        "client_id": client_id,
        "company_id": company_id,
        "company_name": name,
        "oss_username": f"login-of-{name}",
        "oss_password": f"word-of-{name}",
        "oss_creds_updated_at": None,
        "oss_creds_updated_by": None,
    }


class _Conn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def _of(self, client_id: int) -> list[dict]:
        rows = [row for row in self._rows if row["client_id"] == client_id]
        # ORDER BY company_id NULLS FIRST
        return sorted(rows, key=lambda r: (r["company_id"] is not None, r["company_id"] or 0))

    async def fetch(self, sql: str, client_id: int) -> list[dict]:
        assert "FROM lkpm_client_config" in sql and "WHERE client_id = $1" in sql
        return self._of(client_id)

    async def fetchrow(self, sql: str, client_id: int) -> dict | None:
        rows = self._of(client_id)
        return rows[0] if rows else None


class _Pool:
    def __init__(self, rows: list[dict]) -> None:
        self._conn = _Conn(rows)

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _client(
    rows: list[dict], monkeypatch: pytest.MonkeyPatch, email: str = CONSULTANT
) -> TestClient:
    monkeypatch.setattr(lkpm, "LKPM_ASSIGNEES", {CONSULTANT})
    app = FastAPI()
    app.include_router(lkpm.router)
    app.dependency_overrides[lkpm.get_current_user] = lambda: {"email": email, "role": "member"}
    app.dependency_overrides[lkpm.get_database_pool] = lambda: _Pool(rows)
    return TestClient(app)


TWO_COMPANIES = [_config(11, "company-one"), _config(22, "company-two")]


@pytest.mark.parametrize(("company_id", "name"), [(11, "company-one"), (22, "company-two")])
def test_the_login_returned_is_the_one_of_the_company_asked_for(
    monkeypatch: pytest.MonkeyPatch, company_id: int, name: str
) -> None:
    client = _client(TWO_COMPANIES, monkeypatch)

    response = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": company_id})

    assert response.status_code == 200
    body = response.json()
    assert body["oss_username"] == f"login-of-{name}"
    assert body["oss_password"] == f"word-of-{name}"
    assert body["company_name"] == name
    assert body.get("company_id") == company_id


def test_a_client_with_several_companies_and_no_company_named_gets_no_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(TWO_COMPANIES, monkeypatch)

    response = client.get(f"/api/v1/lkpm/credentials/{CLIENT}")

    assert response.status_code == 409
    assert "oss_password" not in response.json()


def test_a_company_the_client_has_no_config_for_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(TWO_COMPANIES, monkeypatch)

    response = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": 33})

    assert response.status_code == 404
    assert "oss_password" not in response.json()


def test_a_single_config_is_returned_without_naming_the_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client([_config(11, "company-one")], monkeypatch)

    response = client.get(f"/api/v1/lkpm/credentials/{CLIENT}")

    assert response.status_code == 200
    assert response.json()["oss_username"] == "login-of-company-one"


def test_a_single_config_that_names_no_company_serves_any_row_of_that_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client([_config(None, "older-config")], monkeypatch)

    named = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": 11})
    unnamed = client.get(f"/api/v1/lkpm/credentials/{CLIENT}")

    assert named.status_code == unnamed.status_code == 200
    assert named.json()["oss_username"] == unnamed.json()["oss_username"] == "login-of-older-config"


def test_a_row_with_no_company_is_paired_with_the_config_that_names_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client([_config(None, "older-config"), _config(22, "company-two")], monkeypatch)

    unnamed = client.get(f"/api/v1/lkpm/credentials/{CLIENT}")
    named = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": 22})
    other = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": 11})

    assert unnamed.json()["oss_username"] == "login-of-older-config"
    assert named.json()["oss_username"] == "login-of-company-two"
    assert other.status_code == 404  # two configs, neither is company 11: no guess


def test_another_client_s_config_is_never_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_config(11, "company-one"), _config(11, "of-another-client", client_id=900008)]
    client = _client(rows, monkeypatch)

    response = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": 11})

    assert response.json()["oss_username"] == "login-of-company-one"


def test_a_client_with_no_config_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client([], monkeypatch)

    assert client.get(f"/api/v1/lkpm/credentials/{CLIENT}").status_code == 404


def test_who_may_read_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(TWO_COMPANIES, monkeypatch, email="someone.else@example.test")

    response = client.get(f"/api/v1/lkpm/credentials/{CLIENT}", params={"company_id": 11})

    assert response.status_code == 403
