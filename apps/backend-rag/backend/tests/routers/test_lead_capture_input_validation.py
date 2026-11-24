"""Input-safety contract for POST /api/lead/capture."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_database_pool
from backend.app.routers import lead_capture
from backend.app.setup.exception_handlers import register_exception_handlers


@pytest.fixture
def repository(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    intent = SimpleNamespace(
        id="li_testintent",
        whatsapp_url="https://wa.me/628123456789?text=hello",
        expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    repo = SimpleNamespace(insert=AsyncMock(return_value=intent))
    monkeypatch.setattr(lead_capture, "LeadIntentRepository", lambda _pool: repo)
    return repo


@pytest.fixture
def client(repository: SimpleNamespace) -> TestClient:
    app = FastAPI()
    app.include_router(lead_capture.router)
    register_exception_handlers(app)
    app.dependency_overrides[get_database_pool] = lambda: object()
    return TestClient(app)


@pytest.mark.parametrize(
    "body",
    [
        '{"source":"garuda_voa","context":{"note":"a\\u0000b"}}',
        '{"source":"garuda_voa","context":{"a\\u0000b":"value"}}',
        '{"source":"garuda_voa","context":{"x":NaN}}',
        '{"source":"garuda_voa","context":{"x":Infinity}}',
        '{"source":"garuda_voa","utm":{"campaign":"a\\u0000b"}}',
        '{"source":"garuda_voa","client_fingerprint":"a\\u0000b"}',
        '{"source":"garuda_voa","context":{"note":"a\\ud800b"}}',
        '{"source":"garuda_voa","context":{"note":"a\\udc00b"}}',
        '{"source":"garuda_voa","context":{"\\ud800":1}}',
        '{"source":"garuda_voa","context":{"n":[{"k":"x\\udfff"}]}}',
        '{"source":"garuda_voa","context":{"note":"\\ude00\\ud83d"}}',
        '{"source":"garuda_voa","utm":{"k":"\\ud800"}}',
        '{"source":"garuda_voa","utm":{"\\udbff":"v"}}',
    ],
    ids=(
        "context-value-nul",
        "context-key-nul",
        "context-nan",
        "context-infinity",
        "utm-nul",
        "fingerprint-nul",
        "context-value-lone-high-surrogate",
        "context-value-lone-low-surrogate",
        "context-key-lone-surrogate",
        "context-nested-list-lone-surrogate",
        "context-reversed-pair",
        "utm-value-lone-surrogate",
        "utm-key-lone-surrogate",
    ),
)
def test_rejects_values_postgres_cannot_persist(
    client: TestClient, repository: SimpleNamespace, body: str
) -> None:
    response = client.post(
        "/api/lead/capture", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422, response.text
    repository.insert.assert_not_awaited()


def test_accepts_unicode_and_nested_context(
    client: TestClient, repository: SimpleNamespace
) -> None:
    context = {"note": "Café 東京", "nested": [{"items": ["one", "two"]}]}

    response = client.post("/api/lead/capture", json={"source": "garuda_voa", "context": context})

    assert response.status_code == 201, response.text
    repository.insert.assert_awaited_once()
    assert repository.insert.await_args.kwargs["context"] == context


def test_a_surrogate_pair_is_one_character_and_is_accepted(
    client: TestClient, repository: SimpleNamespace
) -> None:
    body = '{"source":"garuda_voa","context":{"note":"ok \\ud83d\\ude00"},"utm":{"k":"\\ud83d\\ude00"}}'

    response = client.post(
        "/api/lead/capture", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 201, response.text
    assert repository.insert.await_args.kwargs["context"] == {"note": "ok \U0001f600"}


def test_a_refusal_never_echoes_the_submitted_value(client: TestClient) -> None:
    body = '{"source":"garuda_voa","context":{"note":"synthetic-marker\\u0000"}}'

    response = client.post(
        "/api/lead/capture", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert "synthetic-marker" not in response.text
    assert all(set(e) == {"loc", "msg", "type"} for e in response.json()["detail"])
