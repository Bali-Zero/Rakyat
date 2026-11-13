"""Caplog regressions for the PII-bearing log sites cleaned in Rakyat PR #276.

Every value below is synthetic. Each test drives the real handler or service method with a
fake identity value in the place the old log line used to carry it, then asserts the value
is in none of: the formatted message, any extra/context attribute, exc_text. Only loggers
under ``backend.`` are inspected (an earlier order-dependent failure captured an httpx line).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

FAKE_EMAIL = "fake.person.zq@example.invalid"
FAKE_NAME = "Fakey Mcfakeface ZQ"
FAKE_NIB = "9999000011112"
FAKE_INVOICE = "INV-FAKE-ZQ-0001"
FAKE_DOCTYPE = "synthetic-doctype-zq"
FAKE_STATUS = "synthetic-status-zq"
FAKE_CODE = "SYNTHCODE-ZQ"
FAKE_CLAIM = "synthetic_claim_key_zq"
FAKE_EXC = "synthetic exception text zq"
FAKE_FULL_NAME = "Qwxzy Synthetic Vellum"
FAKE_CLIENT_EMAIL = "qwxzy.vellum@example.com"
FAKE_COMPANY_NIB = "9120000055577"

_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def _flatten(value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            _flatten(v, out)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _flatten(v, out)
    else:
        out.append(str(value))


def _all_text(caplog: pytest.LogCaptureFixture) -> str:
    """Message + every extra/context attribute + exc_text, for backend.* loggers only."""
    parts: list[str] = []
    for record in caplog.records:
        if not record.name.startswith("backend."):
            continue
        parts.append(record.getMessage())
        for key, value in record.__dict__.items():
            if key in _STANDARD:
                continue
            parts.append(key)
            _flatten(value, parts)
        if record.exc_text:
            parts.append(record.exc_text)
        if record.exc_info:
            parts.append(repr(record.exc_info[1]))
    return "\n".join(parts)


def _backend_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("backend.")]


def _assert_clean(caplog: pytest.LogCaptureFixture, *forbidden: str) -> str:
    assert _backend_records(caplog), "the handler logged nothing: the regression proves nothing"
    text = _all_text(caplog)
    for token in forbidden:
        assert token not in text, f"{token!r} reached a log record"
    return text


# --------------------------------------------------------------------------- fixtures


class _Ctx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> bool:
        return False


@pytest.fixture
def pool_conn() -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_Ctx(conn))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    return pool, conn


@pytest.fixture
def admin_user() -> dict[str, Any]:
    return {"id": "admin-1", "email": "admin@example.invalid", "role": "admin"}


# --------------------------------------------------------------------------- hr_service


class TestHRServiceLogs:
    @pytest.fixture
    def svc(self, pool_conn):
        from backend.app.services.hr.hr_service import HRService

        return HRService(db_pool=pool_conn[0]), pool_conn[1]

    @pytest.mark.asyncio
    async def test_approve_bonus_names_the_bonus_not_the_approver(self, svc, caplog):
        service, conn = svc
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"status": "pending", "employee_id": 1, "m": 4, "y": 2026},
                {"id": 7, "status": "approved", "approved_by": FAKE_EMAIL},
            ]
        )
        conn.fetchval = AsyncMock(side_effect=[None, True])
        with caplog.at_level(logging.INFO):
            await service.approve_bonus(7, FAKE_EMAIL)
        text = _assert_clean(caplog, FAKE_EMAIL)
        assert "Bonus 7 approved" in text

    @pytest.mark.asyncio
    async def test_approve_payroll_names_the_period_not_the_approver(self, svc, caplog):
        service, conn = svc
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"payroll_month": 4, "payroll_year": 2026},
                {"id": 3, "status": "calculated", "payroll_month": 4, "payroll_year": 2026},
                {"id": 3, "status": "approved", "approved_by": FAKE_EMAIL},
            ]
        )
        conn.fetchval = AsyncMock(return_value=None)
        with caplog.at_level(logging.INFO):
            await service.approve_payroll(3, FAKE_EMAIL)
        text = _assert_clean(caplog, FAKE_EMAIL)
        assert "Payroll period 3 approved" in text

    @pytest.mark.asyncio
    async def test_leave_decisions_name_the_request_not_the_reviewer(self, svc, caplog):
        service, conn = svc
        row = {
            "id": 11,
            "status": "approved",
            "total_days": 2,
            "employee_id": 1,
            "leave_type_id": 1,
            "start_date": date(2026, 5, 1),
        }
        conn.fetchrow = AsyncMock(return_value=row)
        conn.execute = AsyncMock()
        with caplog.at_level(logging.INFO):
            await service.approve_leave(11, FAKE_EMAIL)
            await service.reject_leave(11, FAKE_EMAIL, FAKE_EXC)
        text = _assert_clean(caplog, FAKE_EMAIL, FAKE_EXC)
        assert "Leave request 11 approved" in text
        assert "Leave request 11 rejected" in text

    @pytest.mark.asyncio
    async def test_upsert_employee_logs_the_row_id_not_the_team_member_key(self, svc, caplog):
        service, conn = svc
        conn.fetchrow = AsyncMock(return_value={"id": 21, "team_member_id": FAKE_EMAIL})
        data = {
            "team_member_id": FAKE_EMAIL,
            "hire_date": date(2024, 1, 1),
            "base_salary_idr": 5000000,
            "ptkp_status": "TK/0",
        }
        with caplog.at_level(logging.INFO):
            await service.upsert_employee(data)
        text = _assert_clean(caplog, FAKE_EMAIL)
        assert "employee_id=21" in text

    @pytest.mark.asyncio
    async def test_bonus_rate_log_has_no_practice_type(self, svc, caplog):
        service, conn = svc
        conn.fetchrow = AsyncMock(return_value={"id": 42})
        data = {"practice_type_code": FAKE_CODE, "amount_idr": 987654}
        with caplog.at_level(logging.INFO):
            await service.upsert_bonus_rate(data)
        text = _assert_clean(caplog, FAKE_CODE, "987654")
        assert "rate_id=42" in text


# --------------------------------------------------------------------------- crm_practices


class TestCrmPracticesLogs:
    @pytest.mark.asyncio
    async def test_add_required_document_logs_ids_not_the_document_type(
        self, pool_conn, admin_user, caplog
    ):
        from backend.app.routers.crm_practices import RequiredDocumentCreate, add_required_document

        pool, conn = pool_conn
        practice = {"id": 1, "client_id": 2, "created_by": FAKE_EMAIL, "assigned_to": None}
        doc_row = {
            "id": 5,
            "practice_id": 1,
            "document_type": FAKE_DOCTYPE,
            "document_label": "x",
            "description": None,
            "is_required": True,
            "uploaded_by_client": False,
            "uploaded_file_id": None,
            "uploaded_at": None,
            "client_notes": None,
            "team_member_notes": None,
            "status": "pending",
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        conn.fetchrow = AsyncMock(side_effect=[practice, doc_row])
        conn.execute = AsyncMock()
        with (
            caplog.at_level(logging.INFO),
            patch("backend.app.routers.crm_practices.invalidate_cache", new=AsyncMock()),
        ):
            await add_required_document(
                practice_id=1,
                doc=RequiredDocumentCreate(document_type=FAKE_DOCTYPE, document_label="x"),
                current_user=admin_user,
                db_pool=pool,
            )
        text = _assert_clean(caplog, FAKE_DOCTYPE)
        assert "Added required document to practice 1" in text

    @pytest.mark.asyncio
    async def test_update_required_document_logs_no_status(self, pool_conn, admin_user, caplog):
        from backend.app.routers.crm_practices import (
            RequiredDocumentUpdate,
            update_required_document,
        )

        pool, conn = pool_conn
        practice = {"created_by": FAKE_EMAIL, "assigned_to": None}
        doc_row = {
            "id": 5,
            "practice_id": 1,
            "document_type": "x",
            "document_label": "x",
            "description": None,
            "is_required": True,
            "uploaded_by_client": False,
            "uploaded_file_id": None,
            "uploaded_at": None,
            "client_notes": None,
            "team_member_notes": None,
            "status": FAKE_STATUS,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        conn.fetchrow = AsyncMock(side_effect=[practice, doc_row])
        with (
            caplog.at_level(logging.INFO),
            patch("backend.app.routers.crm_practices.invalidate_cache", new=AsyncMock()),
        ):
            await update_required_document(
                practice_id=1,
                doc_id=5,
                update=RequiredDocumentUpdate(status=FAKE_STATUS),
                current_user=admin_user,
                db_pool=pool,
            )
        text = _assert_clean(caplog, FAKE_STATUS)
        assert "Updated required document 5 for practice 1" in text

    @pytest.mark.asyncio
    async def test_regenerate_invoice_logs_no_invoice_number(self, pool_conn, admin_user, caplog):
        from backend.app.routers.crm_practices import regenerate_invoice

        pool, conn = pool_conn
        conn.fetchrow = AsyncMock(return_value={"status": "sending_invoice"})
        service = MagicMock()
        service.regenerate_invoice = AsyncMock(
            return_value={"success": True, "invoice_number": FAKE_INVOICE}
        )
        with (
            caplog.at_level(logging.INFO),
            patch(
                "backend.app.routers.crm_practices.InvoiceAutomationService", return_value=service
            ),
        ):
            result = await regenerate_invoice(
                request=MagicMock(), practice_id=9, db_pool=pool, current_user=admin_user
            )
        assert result["success"] is True  # C3: the response still carries the number
        assert FAKE_INVOICE in result["message"]
        _assert_clean(caplog, FAKE_INVOICE)

    def test_a_bad_month_logs_no_raw_value(self, caplog):
        from backend.app.routers.crm_practices import _parse_month_param

        with caplog.at_level(logging.DEBUG):
            assert _parse_month_param("zz-" + FAKE_NAME[:8]) is None
        _assert_clean(caplog, FAKE_NAME[:8])

    @pytest.mark.asyncio
    async def test_create_practice_hook_failure_logs_the_type_only(self, caplog):
        from backend.app.routers.crm_practices import _create_hr_bonus_on_completed

        class _Boom(Exception):
            pass

        conn = AsyncMock()
        conn.fetchval = AsyncMock(side_effect=_Boom(FAKE_EXC))
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_Ctx(conn))
        with caplog.at_level(logging.INFO):
            await _create_hr_bonus_on_completed(pool, 4, {"assigned_to": FAKE_EMAIL})
        text = _assert_clean(caplog, FAKE_EXC, FAKE_EMAIL)
        assert "_Boom" in text


# --------------------------------------------------------------------------- crm_clients


class TestCrmClientsLogs:
    @pytest.mark.asyncio
    async def test_missing_email_logs_no_token_claim_names(self, pool_conn, caplog):
        from backend.app.routers.crm_clients import ClientCreate, create_client

        pool, _ = pool_conn
        with caplog.at_level(logging.INFO), pytest.raises(HTTPException):
            await create_client(
                client=ClientCreate(full_name=FAKE_NAME),
                background_tasks=MagicMock(),
                current_user={"email": "", FAKE_CLAIM: FAKE_EMAIL},
                client_service=MagicMock(),
                db_pool=pool,
            )
        _assert_clean(caplog, FAKE_CLAIM, FAKE_EMAIL, FAKE_NAME)

    @pytest.mark.asyncio
    async def test_company_dedup_by_nib_logs_the_company_id_only(self, pool_conn, caplog):
        from backend.app.routers import crm_clients as module

        pool, conn = pool_conn
        conn.fetchval = AsyncMock(return_value=77)  # existing company found by NIB
        row = {
            "id": 5,
            "uuid": "11111111-1111-1111-1111-111111111111",
            "full_name": FAKE_FULL_NAME,
            "email": FAKE_CLIENT_EMAIL,
            "phone": None,
            "whatsapp": None,
            "company_name": "Synthetic Co",
            "nationality": None,
            "passport_number": None,
            "passport_expiry": None,
            "date_of_birth": None,
            "gender": None,
            "birthplace": None,
            "status": "active",
            "client_type": "individual",
            "assigned_to": None,
            "tax_consultant": None,
            "avatar_url": None,
            "address": None,
            "notes": None,
            "first_contact_date": None,
            "last_interaction_date": None,
            "last_sentiment": None,
            "last_interaction_summary": None,
            "tags": [],
            "lead_source": None,
            "service_interest": [],
            "custom_fields": {},
            "tax_id": None,
            "npwp": None,
            "nib": None,
            "current_visa_type": None,
            "current_visa_sponsor": None,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "created_by": FAKE_EMAIL,
        }
        service = MagicMock()
        service.create_client = AsyncMock(return_value=row)
        real_dump = module.ClientCreate.model_dump

        def dump_with_nib(self, *args, **kwargs):
            return {**real_dump(self, *args, **kwargs), "nib": FAKE_COMPANY_NIB}

        with (
            patch.object(module.ClientCreate, "model_dump", dump_with_nib),
            patch("backend.app.routers.crm_clients.invalidate_cache", AsyncMock()),
            patch(
                "backend.services.integrations.service_account_drive_service.ServiceAccountDriveService",
                return_value=MagicMock(),
            ),
            patch(
                "backend.services.crm.welcome.welcome_whatsapp_service.send_client_welcome",
                AsyncMock(return_value=None),
            ),
            patch(
                "backend.services.crm.welcome.welcome_email_service.schedule_client_welcome_email",
                AsyncMock(return_value=None),
            ),
            patch(
                "backend.services.portal.portal_profile_service.PortalProfileService",
                return_value=MagicMock(ensure_portal_profile=AsyncMock(return_value=None)),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            await module.create_client(
                client=module.ClientCreate(full_name=FAKE_FULL_NAME, company_name="Synthetic Co"),
                background_tasks=MagicMock(),
                current_user={"email": FAKE_EMAIL, "role": "admin"},
                client_service=service,
                db_pool=pool,
            )
        text = _assert_clean(
            caplog, FAKE_COMPANY_NIB, FAKE_FULL_NAME, FAKE_CLIENT_EMAIL, FAKE_EMAIL
        )
        assert "Company dedup: found existing company 77" in text

    @pytest.mark.asyncio
    async def test_ai_summary_served_logs_no_schema_or_fingerprint(
        self, pool_conn, admin_user, caplog
    ):
        from backend.app.routers.crm_clients import get_client_ai_summary

        pool, conn = pool_conn
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 3,
                "ai_summary": {"k": "v"},
                "ai_summary_generated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "ai_summary_schema_version": "schema-" + FAKE_CODE,
                "ai_summary_file_hash": "fp" + FAKE_NIB,
            }
        )
        with caplog.at_level(logging.INFO):
            await get_client_ai_summary(client_id=3, current_user=admin_user, db_pool=pool)
        text = _assert_clean(caplog, FAKE_CODE, FAKE_NIB)
        assert "client_id=3" in text


# --------------------------------------------------------------------------- client_core


@pytest.fixture
def crm_service(pool_conn):
    with (
        patch("backend.services.crm.client_core.crm_cache") as cache,
        patch("backend.services.crm.client_core.query_cache"),
        patch("backend.services.crm.client_core.CRMQueryOptimizer"),
        patch("backend.services.crm.client_core.invalidate_client_cache"),
        patch("backend.services.crm.client_core.health_check_crm_tables"),
    ):
        cache.get = AsyncMock(return_value=None)
        cache.set = AsyncMock()
        from backend.services.crm.client_core import EnhancedCRMService

        svc = EnhancedCRMService(pool_conn[0])
        svc.auditor = MagicMock()
        svc.auditor.log_practice_status_change = AsyncMock()
        yield svc, pool_conn[1]


class TestClientCoreLogs:
    @pytest.mark.asyncio
    async def test_status_change_logs_the_practice_id_not_the_statuses(self, crm_service, caplog):
        svc, conn = crm_service
        conn.fetchrow = AsyncMock(
            side_effect=[
                {"status": FAKE_STATUS + "-old", "client_id": 1},
                {"id": 8, "status": FAKE_STATUS + "-new", "client_id": 1},
            ]
        )
        with caplog.at_level(logging.INFO):
            await svc.update_practice_status(8, FAKE_STATUS + "-new")
        text = _assert_clean(caplog, FAKE_STATUS)
        assert "Practice 8 status changed" in text

    @pytest.mark.asyncio
    async def test_bonus_skip_logs_the_practice_id_not_the_type_code(self, crm_service, caplog):
        svc, conn = crm_service
        conn.fetchval = AsyncMock(return_value=True)
        conn.fetchrow = AsyncMock(side_effect=[{"code": FAKE_CODE}, None])
        with caplog.at_level(logging.DEBUG):
            await svc._create_hr_bonus_entry(8, {"assigned_to": FAKE_EMAIL})
        text = _assert_clean(caplog, FAKE_CODE, FAKE_EMAIL)
        assert "no active rate for practice 8" in text

    @pytest.mark.asyncio
    async def test_status_update_failure_logs_the_type_only(self, crm_service, caplog):
        from backend.services.crm.client_core import DatabaseError

        svc, conn = crm_service
        conn.fetchrow = AsyncMock(
            side_effect=[{"status": "a", "client_id": 1}, RuntimeError(FAKE_EXC + FAKE_EMAIL)]
        )
        with caplog.at_level(logging.INFO), pytest.raises(DatabaseError):
            await svc.update_practice_status(8, "b")
        text = _assert_clean(caplog, FAKE_EXC, FAKE_EMAIL)
        assert "RuntimeError" in text

    @pytest.mark.asyncio
    async def test_client_created_logs_the_id_not_the_name_or_email(self, crm_service, caplog):
        svc, conn = crm_service
        conn.fetchval = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(
            return_value={
                "id": 4242,
                "full_name": FAKE_FULL_NAME,
                "email": FAKE_CLIENT_EMAIL,
                "phone": None,
                "whatsapp": None,
                "nationality": None,
                "passport_number": None,
                "status": "active",
                "client_type": "individual",
                "assigned_to": None,
                "custom_fields": {},
            }
        )
        svc.auditor.log_client_created = AsyncMock()
        with caplog.at_level(logging.DEBUG):
            await svc.create_client({"full_name": FAKE_FULL_NAME, "email": FAKE_CLIENT_EMAIL})
        text = _assert_clean(caplog, FAKE_FULL_NAME, "Qwxzy", FAKE_CLIENT_EMAIL)
        assert "Created client 4242" in text
