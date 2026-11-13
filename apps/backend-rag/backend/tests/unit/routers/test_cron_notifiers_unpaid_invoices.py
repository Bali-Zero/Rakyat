"""The unpaid-invoice reminder has two production entry points; both must reach
UnpaidInvoiceNotifier: POST /unpaid-invoices (manual) and POST /all (the daily
Mini launchd fire, com.balizero.zero-cron.notifiers-all, 00:00 WITA)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import cron_notifiers

KEY = "test-key-123"
RESULT = {"due_count": 2, "notified": True, "skipped": False}
TARGET = "backend.services.invoicing.unpaid_invoice_notifier.UnpaidInvoiceNotifier.check_and_notify"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron_notifiers, "_API_KEY", KEY)


@pytest.fixture
def client(mock_db_pool) -> TestClient:
    app = FastAPI()
    app.include_router(cron_notifiers.router)
    app.state.db_pool = mock_db_pool
    return TestClient(app)


def test_daily_all_run_executes_the_unpaid_invoice_reminder(client):
    with patch(TARGET, new=AsyncMock(return_value=RESULT)) as run:
        resp = client.post("/api/cron/notifiers/all", headers={"X-API-Key": KEY})
    assert resp.status_code == 200
    assert resp.json()["results"]["unpaid_invoices"] == RESULT
    run.assert_awaited_once()


def test_dedicated_endpoint_executes_it(client):
    with patch(TARGET, new=AsyncMock(return_value=RESULT)) as run:
        resp = client.post("/api/cron/notifiers/unpaid-invoices", headers={"X-API-Key": KEY})
    assert resp.status_code == 200 and resp.json() == {"service": "unpaid_invoices", **RESULT}
    run.assert_awaited_once()


def test_wrong_key_never_runs_it(client):
    with patch(TARGET, new=AsyncMock(return_value=RESULT)) as run:
        resp = client.post("/api/cron/notifiers/unpaid-invoices", headers={"X-API-Key": "nope"})
    assert resp.status_code == 401
    run.assert_not_awaited()
