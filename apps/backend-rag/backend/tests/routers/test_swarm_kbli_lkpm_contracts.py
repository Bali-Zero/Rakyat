"""Contract tests for the KBLI / LKPM API parity fixes (swarm vertical 2).

Each test pins one backend half of a client/server contract that the mouth
frontend relies on. No database, no network, no real client data: services
are replaced with in-memory doubles and every identifier is synthetic.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.app.models.lkpm import (
    EmploymentData,
    InvestmentRealization,
    LKPMBatchItem,
    LKPMDraft,
    LKPMReadyPack,
    LKPMValidationResult,
    ValidationAlert,
    ValidationSeverity,
)
from backend.app.routers import kbli_notebook as kbli_notebook_router
from backend.app.routers import lkpm as lkpm_router
from backend.services import kbli_native_scopes as native_scopes
from backend.services.compliance.lkpm_service import LKPMService

_STAFF = {"role": "team", "user_id": "staff-1"}

# apps/backend-rag/backend/tests/routers/<this file> -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_MOUTH_SRC = _REPO_ROOT / "apps" / "mouth" / "src"


def _app(monkeypatch: pytest.MonkeyPatch, service: SimpleNamespace) -> FastAPI:
    """Mount the real LKPM router with auth and service doubles."""
    app = FastAPI()
    app.include_router(lkpm_router.router)
    app.dependency_overrides[lkpm_router.get_current_user] = lambda: _STAFF
    app.dependency_overrides[lkpm_router.get_database_pool] = lambda: object()
    monkeypatch.setattr(lkpm_router, "_require_lkpm_staff", lambda user, **_: user)
    monkeypatch.setattr(lkpm_router, "_get_service", lambda _pool: service)
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# --------------------------------------------------------------------------
# KBLI-3A-01: upload-receipt accepts BOTH JSON body and legacy query params
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_receipt_accepts_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(
        upload_receipt=AsyncMock(return_value={"success": True, "draft_id": 7}),
    )
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.post(
            "/api/v1/lkpm/upload-receipt/7",
            json={"receipt_number": "  SYNTH-001  ", "receipt_file_url": "https://example.test/r"},
        )

    assert resp.status_code == 200, resp.text
    service.upload_receipt.assert_awaited_once_with(7, "SYNTH-001", "https://example.test/r")


@pytest.mark.asyncio
async def test_upload_receipt_still_accepts_legacy_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        upload_receipt=AsyncMock(return_value={"success": True, "draft_id": 7}),
    )
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.post("/api/v1/lkpm/upload-receipt/7?receipt_number=SYNTH-002")

    assert resp.status_code == 200, resp.text
    service.upload_receipt.assert_awaited_once_with(7, "SYNTH-002", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, {}, {"receipt_number": "   "}])
async def test_upload_receipt_missing_receipt_number_is_422(
    monkeypatch: pytest.MonkeyPatch, payload: dict | None
) -> None:
    service = SimpleNamespace(upload_receipt=AsyncMock())
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.post("/api/v1/lkpm/upload-receipt/7", json=payload)

    assert resp.status_code == 422, resp.text
    service.upload_receipt.assert_not_awaited()


# --------------------------------------------------------------------------
# KBLI-3A-02: sync-jurnal accepts BOTH JSON body and legacy query params
# --------------------------------------------------------------------------


def _sync_service() -> SimpleNamespace:
    draft = LKPMDraft(id=5, client_id=1, quarter="Q1", year=2026)
    return SimpleNamespace(sync_jurnal=AsyncMock(return_value=draft))


@pytest.mark.asyncio
async def test_sync_jurnal_accepts_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _sync_service()
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.post("/api/v1/lkpm/sync-jurnal/12", json={"quarter": "Q2", "year": 2026})

    assert resp.status_code == 200, resp.text
    assert resp.json()["draft_id"] == 5
    service.sync_jurnal.assert_awaited_once_with(12, "Q2", 2026)


@pytest.mark.asyncio
async def test_sync_jurnal_still_accepts_legacy_query_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _sync_service()
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.post("/api/v1/lkpm/sync-jurnal/12?quarter=Q3&year=2026")

    assert resp.status_code == 200, resp.text
    service.sync_jurnal.assert_awaited_once_with(12, "Q3", 2026)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, {}, {"quarter": "Q1"}, {"year": 2026}],
)
async def test_sync_jurnal_missing_quarter_or_year_is_422(
    monkeypatch: pytest.MonkeyPatch, payload: dict | None
) -> None:
    service = _sync_service()
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.post("/api/v1/lkpm/sync-jurnal/12", json=payload)

    assert resp.status_code == 422, resp.text
    service.sync_jurnal.assert_not_awaited()


# --------------------------------------------------------------------------
# KBLI-3C-01: ready-pack never returns validation_summary: null
# --------------------------------------------------------------------------


def _ready_pack(validation: LKPMValidationResult | None) -> LKPMReadyPack:
    return LKPMReadyPack(
        draft_id=3,
        client_id=1,
        company_name="Synthetic Entity",
        quarter="Q1",
        year=2026,
        realized=InvestmentRealization(),
        cumulative=InvestmentRealization(),
        plan=InvestmentRealization(),
        employment=EmploymentData(),
        validation_summary=validation,
    )


@pytest.mark.asyncio
async def test_ready_pack_falls_back_to_empty_validation_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(get_ready_pack=AsyncMock(return_value=_ready_pack(None)))
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.get("/api/v1/lkpm/ready-pack/3")

    assert resp.status_code == 200, resp.text
    summary = resp.json()["ready_pack"]["validation_summary"]
    assert summary is not None
    assert summary["is_valid"] is False
    assert (summary["red_count"], summary["yellow_count"], summary["green_count"]) == (0, 0, 0)
    assert summary["alerts"] == []


@pytest.mark.asyncio
async def test_ready_pack_keeps_a_real_validation_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = LKPMValidationResult(is_valid=True, alerts=[], green_count=2)
    service = SimpleNamespace(get_ready_pack=AsyncMock(return_value=_ready_pack(real)))
    async with _client(_app(monkeypatch, service)) as http:
        resp = await http.get("/api/v1/lkpm/ready-pack/3")

    summary = resp.json()["ready_pack"]["validation_summary"]
    assert summary["is_valid"] is True
    assert summary["green_count"] == 2


# --------------------------------------------------------------------------
# KBLI-3C-01 (service side): _row_to_draft derives the severity counts
# --------------------------------------------------------------------------


def _draft_row(**overrides: object) -> dict:
    row: dict = {
        "id": 1,
        "client_id": 1,
        "company_id": 100,
        "quarter": "Q1",
        "year": 2026,
        "status": "validated",
        "validation_status": "invalid",
        "validation_alerts": None,
        "client_approved": False,
        "client_approved_at": None,
        "oss_submitted": False,
        "oss_submitted_at": None,
        "oss_receipt_number": None,
        "data_source": "manual",
        "has_ai_categorized_items": False,
        "ai_categorized_count": 0,
        "current_tki": 0,
        "current_tka": 0,
        "quarterly_revenue": 0,
        "annual_revenue": 0,
        "narrative_obstacles": None,
        "narrative_plans": None,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    for scope in ("realized", "cumulative"):
        for cat in (
            "equipment_domestic",
            "equipment_import",
            "building_domestic",
            "building_import",
            "vehicle_domestic",
            "vehicle_import",
            "land",
            "working_capital",
            "other",
        ):
            row[f"{scope}_{cat}"] = 0
    row.update(overrides)
    return row


def test_row_to_draft_counts_alert_severities() -> None:
    alerts = [
        {"field": "a", "severity": "red", "message": "r1"},
        {"field": "b", "severity": "red", "message": "r2"},
        {"field": "c", "severity": "yellow", "message": "y1"},
        {"field": "d", "severity": "green", "message": "g1"},
    ]
    draft = LKPMService._row_to_draft(_draft_row(validation_alerts=json.dumps(alerts)))

    assert draft.validation is not None
    assert draft.validation.is_valid is False
    assert (
        draft.validation.red_count,
        draft.validation.yellow_count,
        draft.validation.green_count,
    ) == (2, 1, 1)


def test_row_to_draft_without_validation_stays_none() -> None:
    draft = LKPMService._row_to_draft(_draft_row(validation_status="pending"))
    assert draft.validation is None
    assert draft.validation_alerts == []


# --------------------------------------------------------------------------
# KBLI-3C-02: LKPMDraft carries a flat validation_alerts mirror
# --------------------------------------------------------------------------


def _alert(severity: ValidationSeverity, message: str) -> ValidationAlert:
    return ValidationAlert(field="f", severity=severity, message=message)


def test_draft_mirrors_nested_alerts_into_flat_validation_alerts() -> None:
    result = LKPMValidationResult(
        is_valid=False,
        alerts=[_alert(ValidationSeverity.RED, "r"), _alert(ValidationSeverity.GREEN, "g")],
        red_count=1,
        green_count=1,
    )
    dumped = LKPMDraft(client_id=1, quarter="Q1", year=2026, validation=result).model_dump()

    assert [a["severity"] for a in dumped["validation"]["alerts"]] == ["red", "green"]
    assert [a["severity"] for a in dumped["validation_alerts"]] == ["red", "green"]
    for key in ("quarterly_revenue", "annual_revenue", "narrative_obstacles", "narrative_plans"):
        assert key in dumped


def test_draft_without_validation_has_empty_flat_alerts() -> None:
    dumped = LKPMDraft(client_id=1, quarter="Q1", year=2026).model_dump()
    assert dumped["validation"] is None
    assert dumped["validation_alerts"] == []


# --------------------------------------------------------------------------
# KBLI-3C-10: a LEFT JOIN null company_name no longer 500s the listing
# --------------------------------------------------------------------------


def test_batch_item_tolerates_null_company_name() -> None:
    item = LKPMBatchItem(
        id=1,
        client_id=1,
        company_name=None,  # type: ignore[arg-type]
        quarter="Q1",
        year=2026,
        status="draft",
    )
    assert item.company_name == ""


def test_row_to_batch_item_tolerates_missing_company_name() -> None:
    row = _draft_row(company_name=None, lkpm_assigned_to=None, validation_alerts="[]")
    assert LKPMService._row_to_batch_item(row).company_name == ""


# --------------------------------------------------------------------------
# KBLI-3C-03/04: LKPM_ASSIGNEES stays in sync with the frontend picker
# --------------------------------------------------------------------------

_TS_ASSIGNEES_RE = re.compile(
    r"export const LKPM_ASSIGNEES[^=]*=\s*\[(?P<body>.*?)\];",
    re.DOTALL,
)
_TS_VALUE_RE = re.compile(r"value:\s*\"([^\"]+)\"")


def test_frontend_lkpm_assignee_picker_matches_backend_allowlist() -> None:
    ts_path = _MOUTH_SRC / "lib" / "workspace" / "roster-directory.ts"
    if not ts_path.is_file():
        pytest.skip("apps/mouth is not part of this checkout")

    match = _TS_ASSIGNEES_RE.search(ts_path.read_text())
    assert match is not None, "LKPM_ASSIGNEES declaration not found in roster-directory.ts"
    picker = set(_TS_VALUE_RE.findall(match.group("body")))

    assert picker, "extracted an empty picker list: the extraction regex is broken"
    assert picker == set(lkpm_router.LKPM_ASSIGNEES)


# --------------------------------------------------------------------------
# KBLI-3C-05 / KBLI-3C-08: KBLI flat schema & 22 native scope pins stay synced
# --------------------------------------------------------------------------

_TS_ATTEST_RE = re.compile(r"\"(\d{5})\":\s*\{\s*rowsSha256:\s*\"([0-9a-f]{64})\"")


def test_frontend_native_scope_attestations_match_backend_pins() -> None:
    ts_path = _MOUTH_SRC / "lib" / "api" / "kbli.api.ts"
    if not ts_path.is_file():
        pytest.skip("apps/mouth is not part of this checkout")

    frontend = dict(_TS_ATTEST_RE.findall(ts_path.read_text()))

    assert frontend, "extracted no attestations: the extraction regex is broken"
    assert frontend == dict(native_scopes._NATIVE_ROWS_SHA256)


def test_kbli_search_result_and_detail_remain_flat() -> None:
    search_fields = set(kbli_notebook_router.KBLISearchResult.model_fields)
    for flat_bali_field in (
        "bali_status",
        "bali_blocked",
        "bali_needs_review",
        "bali_reason",
        "bali_closure_url",
        "bali_closure_scope",
        "bali_confidence",
    ):
        assert flat_bali_field in search_fields
    assert "metadata" not in search_fields

    detail_fields = set(kbli_notebook_router.KBLIDetail.model_fields)
    assert "licensing_content_inherited_from" in detail_fields
    assert "metadata" not in detail_fields
