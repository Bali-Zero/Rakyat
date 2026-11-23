from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.routers.crm_practices import (
    HistoricalTaxCompletionCreate,
    _can_import_historical_tax_completion,
    create_historical_tax_completion,
)


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return None


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Context(self.conn)


def _row():
    return {
        "id": 8,
        "uuid": uuid4(),
        "client_id": 1,
        "practice_type_id": 9,
        "status": "completed",
        "priority": "normal",
        "quoted_price": None,
        "actual_price": None,
        "payment_status": "unpaid",
        "assigned_to": "consultant@example.test",
        "start_date": None,
        "completion_date": datetime.fromisoformat(_payload()["completed_at"]),
        "metadata": {
            "historical_tax_completion": {
                "source": _payload()["source"],
                "evidence_references": _payload()["evidence_references"],
            }
        },
        "expiry_date": None,
        "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }


class _Conn:
    def __init__(self, *, existing=None, ambiguous=None):
        self.existing, self.ambiguous, self.calls = existing, ambiguous, []

    def transaction(self):
        return _Context(self)

    async def fetchval(self, query, *args):
        self.calls.append(query)
        if "team_members" in query or "client_company_links" in query:
            return 1
        if "practice_types" in query:
            return 9
        return self.ambiguous

    async def fetchrow(self, query, *args):
        self.calls.append(query)
        if query.startswith("SELECT *"):
            return self.existing
        return _row()


def _payload(**overrides):
    payload = {
        "client_id": 1,
        "company_id": 2,
        "assigned_to": "consultant@example.test",
        "service": "lkpm_quarterly",
        "fiscal_year": 2026,
        "fiscal_period": "Q3",
        "issuer_entity_key": "permata_bali_nol_konsultan",
        "completed_at": "2026-10-01T09:00:00+08:00",
        "evidence_references": ["receipt-1", "receipt-2"],
        "source": "controlled historical import",
    }
    payload.update(overrides)
    return payload


def test_historical_lkpm_accepts_quarter_and_wita_timestamp():
    result = HistoricalTaxCompletionCreate(**_payload())
    assert result.fiscal_period == "Q3"
    assert result.completed_at == datetime.fromisoformat("2026-10-01T09:00:00+08:00")


@pytest.mark.parametrize("period", ["Q0", "ANNUAL", "q3"])
def test_historical_lkpm_rejects_ambiguous_period(period):
    with pytest.raises(ValidationError):
        HistoricalTaxCompletionCreate(**_payload(fiscal_period=period))


def test_historical_date_validates_local_fiscal_period_and_current_time():
    valid = HistoricalTaxCompletionCreate(**_payload(completed_at="2026-09-30T16:00:00Z"))
    assert valid.completed_at.isoformat() == "2026-10-01T00:00:00+08:00"
    for invalid in ("2026-09-30T15:59:59Z", "2099-10-01T00:00:00+08:00"):
        with pytest.raises(ValidationError):
            HistoricalTaxCompletionCreate(**_payload(completed_at=invalid))


def test_historical_spt_is_annual_only():
    result = HistoricalTaxCompletionCreate(
        **_payload(service="spt_annual_company", fiscal_period="ANNUAL", fiscal_year=2025)
    )
    assert result.service == "spt_annual_company"


def test_historical_service_refuses_noncanonical_billing_entity():
    with pytest.raises(ValidationError):
        HistoricalTaxCompletionCreate(**_payload(issuer_entity_key="other"))


def test_only_privileged_tax_roles_can_import():
    assert _can_import_historical_tax_completion({"role": "tax supervisor"})
    assert not _can_import_historical_tax_completion({"role": "tax consultant"})


@pytest.mark.asyncio
async def test_route_creates_completed_board_practice_without_automation():
    conn = _Conn()
    result = await create_historical_tax_completion(
        HistoricalTaxCompletionCreate(**_payload()),
        _Pool(conn),
        {"role": "tax supervisor", "email": "lead@example.test"},
    )
    insert = next(query for query in conn.calls if "INSERT INTO practices" in query)
    assert result.status == "completed"
    assert "quoted_price" not in insert and "payment_status" not in insert
    assert "text::jsonb" in insert
    assert not any(
        "INSERT INTO invoices" in query or "bonus" in query.lower() for query in conn.calls
    )


@pytest.mark.asyncio
async def test_route_replay_returns_existing_and_never_inserts():
    conn = _Conn(existing=_row(), ambiguous=1)
    result = await create_historical_tax_completion(
        HistoricalTaxCompletionCreate(**_payload()),
        _Pool(conn),
        {"role": "tax supervisor", "email": "lead@example.test"},
    )
    assert result.id == 8
    assert not any("INSERT INTO practices" in query for query in conn.calls)


@pytest.mark.asyncio
async def test_route_replay_refuses_assignee_mismatch():
    existing = _row()
    existing["assigned_to"] = "other-tax@example.test"
    with pytest.raises(HTTPException, match="different details") as error:
        await create_historical_tax_completion(
            HistoricalTaxCompletionCreate(**_payload()),
            _Pool(_Conn(existing=existing)),
            {"role": "tax supervisor", "email": "lead@example.test"},
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_replay_checks_the_existing_evidence():
    class ConcurrentInsert(_Conn):
        async def fetchrow(self, query, *args):
            if "INSERT INTO practices" in query:
                self.existing = _row()
                self.existing["metadata"]["historical_tax_completion"]["source"] = "another filing"
                return None
            return await super().fetchrow(query, *args)

    with pytest.raises(HTTPException, match="different details") as error:
        await create_historical_tax_completion(
            HistoricalTaxCompletionCreate(**_payload()),
            _Pool(ConcurrentInsert()),
            {"role": "accounting", "email": "review@example.test"},
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_route_refuses_ambiguous_open_or_closed_practice():
    with pytest.raises(HTTPException, match="no historical scope") as error:
        await create_historical_tax_completion(
            HistoricalTaxCompletionCreate(**_payload()),
            _Pool(_Conn(ambiguous=1)),
            {"role": "tax supervisor", "email": "lead@example.test"},
        )
    assert error.value.status_code == 409
