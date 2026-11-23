"""An open compliance alert follows the deadline it is about (real PostgreSQL, private schema).

The engine keeps one open alert per dedup key. An alert whose deadline had passed stayed open
for ever, so a renewed visa, KITAS or passport under the same key never got an alert of its
own.

The table is created from migration 114 itself, so its unique index on open keys and its status
check are the real ones.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncGenerator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import jinja2
import pytest
import pytest_asyncio

from backend.services.compliance import alerts_engine as engine_module
from backend.services.compliance.alert_repository import AlertRepository, AlertRow
from backend.services.compliance.alerts_engine import AlertsEngine
from backend.services.compliance.predictive_engine import ComplianceForecast

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_MIGRATION = (
    Path(__file__).resolve().parents[3] / "db" / "migrations_v2" / "114_compliance_alerts.sql"
)


def _is_safe_test_database(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    name = re.sub(r"_gw\d+$", "", parsed.path.lstrip("/"))
    return parsed.hostname in {"localhost", "127.0.0.1"} and (
        name == "test" or name.endswith("_test")
    )


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _is_safe_test_database(_DB_URL), reason="needs a local test database"),
]

CLIENT = 900_731
RULE = "kitas_e33"
VISA_KEY = f"visa:{CLIENT}:{RULE}"
PASSPORT_KEY = f"document_expiry:{CLIENT}"
LICENSE_KEY = f"license_renewal:{CLIENT}"
GUARANTEE_KEY = f"guarantee_proof:{CLIENT}"
STORED = "alert_stored"


def _alerts_ddl() -> str:
    text = _MIGRATION.read_text(encoding="utf-8")
    start = text.index("CREATE TABLE IF NOT EXISTS compliance_alerts")
    end = text.index("-- Seed system_settings")
    return text[start:end]


@pytest_asyncio.fixture()
async def pool() -> AsyncGenerator[asyncpg.Pool, None]:
    schema = f"alert_deadline_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(_DB_URL)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            db_pool = await asyncpg.create_pool(
                _DB_URL, min_size=1, max_size=2, server_settings={"search_path": schema}
            )
            try:
                await db_pool.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY)")
                await db_pool.execute(_alerts_ddl())
                await db_pool.execute("INSERT INTO clients (id) VALUES ($1)", CLIENT)
                yield db_pool
            finally:
                await db_pool.close()
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await admin.close()


class _Dispatcher:
    def __init__(self) -> None:
        self.sent: list[AlertRow] = []

    async def dispatch(self, alert: AlertRow) -> bool:
        self.sent.append(alert)
        return True


class _Pricing:
    def __init__(self, prices: dict[str, int] | None = None) -> None:
        self.prices = prices or {}

    def get_price(self, key: str) -> int | None:
        return self.prices.get(key)


async def _today(pool: asyncpg.Pool) -> date:
    return await pool.fetchval("SELECT CURRENT_DATE")


def _forecast(
    today: date,
    *,
    expiry: date,
    document_type: str = "kitas",
    urgency: str = "info",
    pricing_key: str | None = None,
) -> ComplianceForecast:
    return ComplianceForecast(
        client_id=CLIENT,
        client_name="Synthetic Client",
        assigned_to=None,
        document_type=document_type,
        current_visa_type="E33G",
        expiry_date=expiry,
        days_until_expiry=(expiry - today).days,
        matched_rule_id=RULE,
        processing_days=14,
        lead_time_start=expiry - timedelta(days=14),
        recommended_action_by=expiry - timedelta(days=21),
        days_until_action=(expiry - today).days - 21,
        estimated_revenue_idr=None,
        renewal_pricing_key=pricing_key,
        priority_score=0.5,
        urgency_level=urgency,
        required_docs=[],
        has_active_renewal_practice=False,
        notes="",
    )


async def _store(
    pool: asyncpg.Pool,
    *,
    deadline: date,
    alert_id: str = STORED,
    key: str = VISA_KEY,
    category: str = "visa_expiry",
    severity: str = "info",
    status: str = "pending",
) -> None:
    await AlertRepository(pool).insert(
        AlertRow(
            alert_id=alert_id,
            client_id=CLIENT,
            category=category,
            severity=severity,
            status=status,
            deadline=deadline,
            days_until=80,
            compliance_item_ref=RULE,
            dedup_key=key,
            message_it="stored text",
            message_en="stored text",
            message_id="stored text",
            suggested_action="stored text",
            estimated_cost_idr=1_000,
            evidence_refs=[],
            nb2_ref=None,
        )
    )


async def _rows(pool: asyncpg.Pool, key: str) -> dict[str, dict[str, Any]]:
    records = await pool.fetch("SELECT * FROM compliance_alerts WHERE dedup_key = $1", key)
    return {r["alert_id"]: dict(r) for r in records}


def _engine(pool: asyncpg.Pool, prices: dict[str, int] | None = None):
    dispatcher = _Dispatcher()
    return AlertsEngine(pool, pricing=_Pricing(prices), dispatcher=dispatcher), dispatcher


def _failing_render(*_args: Any, **_kwargs: Any) -> str:
    raise jinja2.UndefinedError("synthetic template failure")


# ── a passed deadline gives way to the renewed document ─────────────────────────────────


VISA_BODY_60 = (
    "Your E33G visa will expire in 60 days. "
    "To avoid overstay penalties, start the renewal process now."
)


@pytest.mark.parametrize("status", ["pending", "sent", "acknowledged"])
@pytest.mark.parametrize(
    ("document_type", "category", "key", "body"),
    [
        ("kitas", "visa_expiry", VISA_KEY, VISA_BODY_60),
        ("visa", "visa_expiry", VISA_KEY, VISA_BODY_60),
        ("passport", "document_expiry", PASSPORT_KEY, "Document passport expires in 60 days."),
    ],
)
async def test_an_open_alert_whose_deadline_has_passed_gives_way_to_the_renewed_document(
    pool: asyncpg.Pool, status: str, document_type: str, category: str, key: str, body: str
) -> None:
    today = await _today(pool)
    await _store(
        pool,
        deadline=today - timedelta(days=3),
        key=key,
        category=category,
        severity="critical",
        status=status,
    )
    engine, dispatcher = _engine(pool)

    [alert] = await engine.generate_alerts(
        [_forecast(today, expiry=today + timedelta(days=60), document_type=document_type)]
    )

    rows = await _rows(pool, key)
    assert set(rows) == {STORED, alert.alert_id}
    stored, new = rows[STORED], rows[alert.alert_id]
    assert stored["status"] == "expired"
    assert stored["severity"] == "critical"
    assert stored["resolved_at"] is None
    assert stored["deadline"] == today - timedelta(days=3)
    assert new["status"] == "pending"
    assert new["category"] == category
    assert new["deadline"] == today + timedelta(days=60)
    assert new["days_until"] == 60
    assert new["severity"] == "info"
    assert new["message_en"] == body
    assert [a.alert_id for a in dispatcher.sent] == [alert.alert_id]


async def test_the_next_run_keeps_the_renewed_alert_and_sends_nothing(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3))
    engine, dispatcher = _engine(pool)
    forecast = _forecast(today, expiry=today + timedelta(days=60))

    [first] = await engine.generate_alerts([forecast])
    [second] = await engine.generate_alerts([forecast])

    assert second.alert_id == first.alert_id
    assert [a.alert_id for a in dispatcher.sent] == [first.alert_id]
    assert len(await _rows(pool, VISA_KEY)) == 2


async def test_a_deadline_of_today_has_not_passed(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today)
    engine, dispatcher = _engine(pool)

    [alert] = await engine.generate_alerts([_forecast(today, expiry=today + timedelta(days=60))])

    assert alert.alert_id == STORED
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED}
    assert rows[STORED]["status"] == "pending"
    assert dispatcher.sent == []


async def test_an_earlier_open_deadline_keeps_its_alert(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=10))
    engine, dispatcher = _engine(pool)

    [alert] = await engine.generate_alerts([_forecast(today, expiry=today + timedelta(days=200))])

    assert alert.alert_id == STORED
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED}
    assert rows[STORED]["deadline"] == today + timedelta(days=10)
    assert dispatcher.sent == []


@pytest.mark.parametrize(
    ("document_type", "category", "key"),
    [
        ("license", "license_renewal", LICENSE_KEY),
        ("e33_guarantee", "guarantee_proof", GUARANTEE_KEY),
    ],
)
async def test_a_key_that_gathers_several_obligations_keeps_its_passed_alert(
    pool: asyncpg.Pool, document_type: str, category: str, key: str
) -> None:
    today = await _today(pool)
    await _store(
        pool, deadline=today - timedelta(days=3), key=key, category=category, severity="critical"
    )
    engine, dispatcher = _engine(pool)

    [alert] = await engine.generate_alerts(
        [_forecast(today, expiry=today + timedelta(days=60), document_type=document_type)]
    )

    assert alert.alert_id == STORED
    rows = await _rows(pool, key)
    assert set(rows) == {STORED}
    assert rows[STORED]["status"] == "pending"
    assert dispatcher.sent == []


async def test_a_failed_render_leaves_the_passed_alert_open(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3), severity="critical")
    monkeypatch.setattr(engine_module, "render_template", _failing_render)
    engine, dispatcher = _engine(pool)

    [alert] = await engine.generate_alerts([_forecast(today, expiry=today + timedelta(days=60))])

    assert alert.alert_id == STORED
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED}
    assert rows[STORED]["status"] == "pending"
    assert dispatcher.sent == []


# ── the repository closes and inserts together, or changes nothing ──────────────────────


def _replacement(today: date, *, deadline: date, key: str = VISA_KEY, **changes: Any) -> AlertRow:
    values: dict[str, Any] = {
        "alert_id": "alert_replacement",
        "client_id": CLIENT,
        "category": "visa_expiry",
        "severity": "info",
        "status": "pending",
        "deadline": deadline,
        "days_until": (deadline - today).days,
        "compliance_item_ref": RULE,
        "dedup_key": key,
        "message_it": "new",
        "message_en": "new",
        "message_id": "new",
        "suggested_action": "new",
        "estimated_cost_idr": None,
        "evidence_refs": [],
        "nb2_ref": None,
    }
    values.update(changes)
    return AlertRow(**values)


@pytest.mark.parametrize("days_after_stored", [0, -2])
async def test_the_repository_replaces_only_with_a_later_deadline(
    pool: asyncpg.Pool, days_after_stored: int
) -> None:
    today = await _today(pool)
    stored_deadline = today - timedelta(days=3)
    await _store(pool, deadline=stored_deadline)

    replaced = await AlertRepository(pool).replace_passed(
        STORED, _replacement(today, deadline=stored_deadline + timedelta(days=days_after_stored))
    )

    assert replaced is None
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED}
    assert rows[STORED]["status"] == "pending"


@pytest.mark.parametrize("status", ["resolved", "expired"])
async def test_the_repository_never_replaces_a_closed_alert(
    pool: asyncpg.Pool, status: str
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3), status=status)

    replaced = await AlertRepository(pool).replace_passed(
        STORED, _replacement(today, deadline=today + timedelta(days=60))
    )

    assert replaced is None
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED}
    assert rows[STORED]["status"] == status


async def test_an_insert_that_fails_leaves_the_stored_alert_open(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3))

    with pytest.raises(asyncpg.CheckViolationError):
        await AlertRepository(pool).replace_passed(
            STORED, _replacement(today, deadline=today + timedelta(days=60), severity="bogus")
        )

    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED}
    assert rows[STORED]["status"] == "pending"


async def test_a_key_taken_meanwhile_leaves_the_stored_alert_open(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3))
    await _store(
        pool,
        deadline=today + timedelta(days=30),
        alert_id="alert_other",
        key=PASSPORT_KEY,
        category="document_expiry",
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await AlertRepository(pool).replace_passed(
            STORED, _replacement(today, deadline=today + timedelta(days=60), key=PASSPORT_KEY)
        )

    assert (await _rows(pool, VISA_KEY))[STORED]["status"] == "pending"
    assert set(await _rows(pool, PASSPORT_KEY)) == {"alert_other"}


async def test_the_replacement_is_returned_as_stored(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3))

    replaced = await AlertRepository(pool).replace_passed(
        STORED, _replacement(today, deadline=today + timedelta(days=60))
    )

    assert replaced is not None
    assert replaced.alert_id == "alert_replacement"
    assert replaced.status == "pending"
    assert replaced.deadline == today + timedelta(days=60)
    assert replaced.created_at is not None


# ── what does not change ────────────────────────────────────────────────────────────


async def test_the_same_severity_changes_nothing_and_sends_nothing(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=20))
    engine, dispatcher = _engine(pool, {"kitas.renewal": 9_000_000})

    await engine.generate_alerts(
        [_forecast(today, expiry=today + timedelta(days=6), pricing_key="kitas.renewal")]
    )

    row = (await _rows(pool, VISA_KEY))[STORED]
    assert (row["severity"], row["upgrade_count"]) == ("info", 0)
    assert row["deadline"] == today + timedelta(days=20)
    assert row["message_en"] == "stored text"
    assert row["estimated_cost_idr"] == 1_000
    assert dispatcher.sent == []


async def test_a_forecast_with_no_open_alert_gets_a_new_one(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    engine, dispatcher = _engine(pool, {"kitas.renewal": 9_000_000})

    [alert] = await engine.generate_alerts(
        [_forecast(today, expiry=today + timedelta(days=30), pricing_key="kitas.renewal")]
    )

    row = (await _rows(pool, VISA_KEY))[alert.alert_id]
    assert (row["status"], row["deadline"], row["days_until"]) == (
        "pending",
        today + timedelta(days=30),
        30,
    )
    assert row["message_en"].startswith("Your E33G visa will expire in 30 days.")
    assert row["estimated_cost_idr"] == 9_000_000
    assert [a.alert_id for a in dispatcher.sent] == [alert.alert_id]
