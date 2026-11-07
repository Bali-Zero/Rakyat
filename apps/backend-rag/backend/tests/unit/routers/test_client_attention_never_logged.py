"""A failure while building client attention never writes client data to a log record.

The exception raised by the builder can echo the field value it choked on
(asyncpg, strptime and float() all do), and client fields are client data
(Builder Contract 4: logs are outputs). The log line may carry the exception
type, nothing else. The sentinel is pushed through the exception text, its
attributes, its chained cause and the visa-catalogue query failure.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SENTINEL = "QQSENTINEL7731"
ROUTER = "backend.app.routers.crm_enhanced"


def _exception_text(exc: BaseException, seen: set[int]) -> str:
    if id(exc) in seen:
        return ""
    seen.add(id(exc))
    parts = [repr(exc), repr(exc.args), repr(vars(exc))]
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            parts.append(_exception_text(linked, seen))
    return " ".join(parts)


def _leaks(caplog: pytest.LogCaptureFixture) -> list[str]:
    leaked = []
    for record in caplog.records:
        parts = [record.getMessage(), *(repr(v) for v in vars(record).values())]
        args = record.args if isinstance(record.args, tuple) else (record.args or {},)
        seen: set[int] = set()
        for arg in args:
            if isinstance(arg, BaseException):
                parts.append(_exception_text(arg, seen))
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(_exception_text(record.exc_info[1], seen))
            parts.append(logging.Formatter().formatException(record.exc_info))
        if SENTINEL in " ".join(parts):
            leaked.append(f"{record.name}:{record.lineno} {record.levelname}")
    return leaked


@pytest.fixture
def admin_user() -> dict[str, Any]:
    return {"email": "admin@balizero.com", "role": "admin", "id": "admin-1"}


@pytest.fixture
def client_row() -> dict[str, Any]:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {
        "id": 42,
        "uuid": "00000000-0000-0000-0000-000000000042",
        "full_name": "Synthetic Client",
        "email": "synthetic@example.invalid",
        "phone": None,
        "whatsapp": None,
        "nationality": None,
        "passport_number": None,
        "passport_expiry": None,
        "date_of_birth": None,
        "company_name": None,
        "google_drive_folder_id": None,
        "status": "active",
        "client_type": "individual",
        "assigned_to": "team@balizero.com",
        "address": None,
        "notes": None,
        "tags": None,
        "custom_fields": None,
        "first_contact_date": now,
        "last_interaction_date": now,
        "created_at": now,
        "updated_at": now,
    }


class TestAttentionFailureLogsNoClientData:
    @pytest.mark.asyncio
    async def test_builder_failure_logs_type_only(
        self,
        caplog: pytest.LogCaptureFixture,
        mock_db_pool: MagicMock,
        mock_db_conn: AsyncMock,
        admin_user: dict,
        client_row: dict,
    ) -> None:
        from backend.app.routers.crm_enhanced import get_client_profile

        try:
            raise ValueError(f"cause {SENTINEL}")
        except ValueError as cause:
            failure = RuntimeError(f"bad date {SENTINEL}")
            failure.__cause__ = cause
        with (
            patch(f"{ROUTER}.verify_client_access", new=AsyncMock()),
            patch(f"{ROUTER}.build_client_attention", side_effect=failure),
            caplog.at_level(logging.DEBUG),
        ):
            mock_db_conn.fetchrow = AsyncMock(return_value=client_row)
            mock_db_conn.fetch = AsyncMock(return_value=[])
            result = await get_client_profile(
                client_id=42, pool=mock_db_pool, current_user=admin_user
            )

        assert result["attention_error"] == "attention_build_failed"
        assert any(r.name == ROUTER and "RuntimeError" in r.getMessage() for r in caplog.records)
        assert _leaks(caplog) == []
