"""An open compliance alert follows the deadline it is about (real PostgreSQL, private schema).

The engine keeps one open alert per dedup key. An alert whose deadline had passed stayed open
for ever, so a renewed visa, KITAS or passport under the same key never got an alert of its
own; and a promoted alert kept the deadline, the texts and the cost of the run that created it,
so it was dispatched saying "80 days" six days before the deadline.

The table is created from migration 114 itself, so its unique index on open keys and its status
check are the real ones.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
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
from backend.services.compliance.alert_dedup import should_promote
from backend.services.compliance.alert_repository import (
    _SEVERITY_ORDER,
    AlertRepository,
    AlertRow,
)
from backend.services.compliance.alerts_engine import AlertsEngine
from backend.services.compliance.predictive_engine import ComplianceForecast
from backend.services.compliance.severity_calculator import AlertSeverity

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


# ── a promotion carries the deadline, texts and cost of its forecast ────────────────────


async def test_a_promoted_alert_carries_the_deadline_texts_and_cost_of_its_forecast(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=20))
    engine, dispatcher = _engine(pool, {"kitas.renewal": 9_000_000})

    [alert] = await engine.generate_alerts(
        [
            _forecast(
                today,
                expiry=today + timedelta(days=6),
                urgency="urgent",
                pricing_key="kitas.renewal",
            )
        ]
    )

    assert alert.alert_id == STORED
    row = (await _rows(pool, VISA_KEY))[STORED]
    assert row["status"] == "pending"
    assert row["severity"] == "urgent"
    assert row["upgrade_count"] == 1
    assert row["days_until"] == 6
    assert row["deadline"] == today + timedelta(days=6)
    assert row["message_en"] == "Your E33G visa will expire in 6 days. " + (
        "To avoid overstay penalties, start the renewal process now."
    )
    assert row["message_it"].startswith("Il visto E33G scadrà tra 6 giorni.")
    assert row["message_id"].startswith("Visa E33G Anda akan habis dalam 6 hari.")
    assert row["suggested_action"] != "stored text"
    assert row["estimated_cost_idr"] == 9_000_000
    [sent] = dispatcher.sent
    assert (sent.alert_id, sent.message_en, sent.deadline) == (
        STORED,
        row["message_en"],
        today + timedelta(days=6),
    )


async def test_a_failed_render_or_an_unknown_cost_keeps_the_stored_values(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=20))
    monkeypatch.setattr(engine_module, "render_template", _failing_render)
    engine, dispatcher = _engine(pool)

    await engine.generate_alerts(
        [
            _forecast(
                today,
                expiry=today + timedelta(days=6),
                urgency="urgent",
                pricing_key="kitas.renewal",
            )
        ]
    )

    row = (await _rows(pool, VISA_KEY))[STORED]
    assert row["severity"] == "urgent"
    assert row["deadline"] == today + timedelta(days=6)
    for column in ("message_it", "message_en", "message_id", "suggested_action"):
        assert row[column] == "stored text"
    assert row["estimated_cost_idr"] == 1_000
    assert len(dispatcher.sent) == 1


async def test_a_promotion_given_only_severity_and_days_keeps_everything_else(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=20))

    promoted = await AlertRepository(pool).promote(STORED, new_severity="urgent", new_days_until=6)

    assert (promoted.severity, promoted.days_until, promoted.upgrade_count) == ("urgent", 6, 1)
    assert promoted.deadline == today + timedelta(days=20)
    assert promoted.message_en == "stored text"
    assert promoted.suggested_action == "stored text"
    assert promoted.estimated_cost_idr == 1_000


# ── one key, two runs at once ────────────────────────────────────────────────────────────


async def test_a_promotion_raises_an_open_alert_and_only_upward(pool: asyncpg.Pool) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=20))
    repo = AlertRepository(pool)

    first = await repo.promote(
        STORED, new_severity="critical", new_days_until=3, message_en="critical text"
    )
    lower = await repo.promote(
        STORED, new_severity="warning", new_days_until=15, message_en="warning text"
    )
    same = await repo.promote(
        STORED, new_severity="critical", new_days_until=2, message_en="again text"
    )

    assert first is not None and first.severity == "critical"
    assert lower is None
    assert same is None
    row = (await _rows(pool, VISA_KEY))[STORED]
    assert (row["severity"], row["days_until"], row["message_en"], row["upgrade_count"]) == (
        "critical",
        3,
        "critical text",
        1,
    )
    with pytest.raises(LookupError):
        await repo.promote("alert_missing", new_severity="critical", new_days_until=1)


@pytest.mark.parametrize("status", ["expired", "resolved"])
async def test_a_closed_alert_is_never_promoted(pool: asyncpg.Pool, status: str) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3), status=status)

    promoted = await AlertRepository(pool).promote(
        STORED, new_severity="critical", new_days_until=1, deadline=today + timedelta(days=1)
    )

    assert promoted is None
    row = (await _rows(pool, VISA_KEY))[STORED]
    assert (row["severity"], row["deadline"], row["upgrade_count"]) == (
        "info",
        today - timedelta(days=3),
        0,
    )


def test_the_statement_ranks_severities_as_the_engine_does() -> None:
    ranked = [AlertSeverity(name) for name in _SEVERITY_ORDER]
    assert set(ranked) == set(AlertSeverity)
    for lower, higher in zip(ranked, ranked[1:], strict=False):
        assert should_promote(lower, higher)
        assert not should_promote(higher, lower)


class _Gate:
    """The first two runs to arrive wait for each other, so both decide on the same read."""

    def __init__(self) -> None:
        self.arrived = 0
        self.open = asyncio.Event()

    async def arrive(self) -> None:
        if self.open.is_set():
            return
        self.arrived += 1
        if self.arrived == 2:
            self.open.set()
        else:
            await asyncio.wait_for(self.open.wait(), 5)


Hook = Callable[[], Awaitable[None]]


class _Run(AlertRepository):
    """The real repository, with what one run did written down.

    ``writes`` lists every write the run attempted, applied or refused. With a gate, the run's
    first read waits for the other run's; with ``after``, its writes wait until the other run
    has finished; ``hooks`` run what a third session does right after the run's n-th read.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        gate: _Gate | None = None,
        after: asyncio.Event | None = None,
        hooks: dict[int, Hook] | None = None,
    ) -> None:
        super().__init__(pool)
        self.gate = gate
        self.after = after
        self.hooks = hooks or {}
        self.reads = 0
        self.writes: list[str] = []

    async def find_active_by_dedup_key(self, dedup_key: str) -> AlertRow | None:
        found = await super().find_active_by_dedup_key(dedup_key)
        self.reads += 1
        hook = self.hooks.get(self.reads)
        if hook is not None:
            await hook()
        if self.gate is not None:
            await self.gate.arrive()
        return found

    async def _write(self, name: str) -> None:
        self.writes.append(name)
        if self.after is not None:
            await asyncio.wait_for(self.after.wait(), 5)

    async def promote(self, alert_id: str, **values: Any) -> AlertRow | None:
        await self._write("promote")
        return await super().promote(alert_id, **values)

    async def replace_passed(self, stored_alert_id: str, row: AlertRow) -> AlertRow | None:
        await self._write("replace_passed")
        return await super().replace_passed(stored_alert_id, row)

    async def insert(self, row: AlertRow) -> AlertRow:
        await self._write("insert")
        return await super().insert(row)


def _counted(
    pool: asyncpg.Pool, hooks: dict[int, Hook] | None = None
) -> tuple[AlertsEngine, _Dispatcher, _Run]:
    engine, dispatcher = _engine(pool)
    repo = _Run(pool, hooks=hooks)
    engine._repo = repo
    return engine, dispatcher, repo


@dataclass
class _Side:
    returned: list[AlertRow]
    sent: list[AlertRow]
    writes: list[str]


async def _race(
    pool: asyncpg.Pool,
    first_forecast: ComplianceForecast,
    second_forecast: ComplianceForecast,
    *,
    unrendered: tuple[str, ...] = (),
) -> tuple[_Side, _Side]:
    """Two runs read the key together; the first then does all it does, and only then the
    second writes, on what it read before. A run named in ``unrendered`` cannot render texts."""
    gate = _Gate()
    first_done = asyncio.Event()
    sides = {}
    engines = {}
    for name, after in (("first", None), ("second", first_done)):
        engine, dispatcher = _engine(pool)
        engine._repo = _Run(pool, gate=gate, after=after)
        if name in unrendered:
            engine._render = lambda *_a, **_k: None
        engines[name] = (engine, dispatcher)

    async def first() -> list[AlertRow]:
        try:
            return await engines["first"][0].generate_alerts([first_forecast])
        finally:
            first_done.set()

    returned = await asyncio.wait_for(
        asyncio.gather(first(), engines["second"][0].generate_alerts([second_forecast])), 10
    )
    for name, alerts in zip(("first", "second"), returned, strict=True):
        engine, dispatcher = engines[name]
        sides[name] = _Side(alerts, dispatcher.sent, engine._repo.writes)
    return sides["first"], sides["second"]


def _visa(today: date, days: int, urgency: str) -> ComplianceForecast:
    return _forecast(today, expiry=today + timedelta(days=days), urgency=urgency)


def _values(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["status"],
        row["severity"],
        row["deadline"],
        row["days_until"],
        row["upgrade_count"],
    )


def _says(row: dict[str, Any], days: int) -> bool:
    return row["message_en"].startswith(f"Your E33G visa will expire in {days} days.")


async def _close(pool: asyncpg.Pool, alert_id: str = STORED) -> None:
    await pool.execute(
        "UPDATE compliance_alerts SET status = 'resolved' WHERE alert_id = $1", alert_id
    )


async def test_a_lower_promotion_read_at_the_same_time_cannot_overwrite_a_higher_one(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=20))

    first, second = await _race(pool, _visa(today, 3, "critical"), _visa(today, 15, "warning"))

    row = (await _rows(pool, VISA_KEY))[STORED]
    assert _values(row) == ("pending", "critical", today + timedelta(days=3), 3, 1)
    assert _says(row, 3)
    assert [a.alert_id for a in first.sent] == [STORED]
    assert second.sent == []
    assert first.returned[0].alert_id == second.returned[0].alert_id == STORED
    assert second.returned[0].severity == "critical"
    assert first.writes == second.writes == ["promote"]


@pytest.mark.parametrize("urgency", ["info", "urgent"])
async def test_a_replacement_read_at_the_same_time_answers_with_the_new_alert(
    pool: asyncpg.Pool, urgency: str
) -> None:
    today = await _today(pool)
    passed = today - timedelta(days=3)
    await _store(pool, deadline=passed)

    first, second = await _race(pool, _visa(today, 60, urgency), _visa(today, 60, urgency))

    [new] = first.returned
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED, new.alert_id}
    assert second.returned[0].alert_id == new.alert_id
    assert _values(rows[STORED]) == ("expired", "info", passed, 80, 0)
    assert [a.alert_id for a in first.sent] == [new.alert_id]
    assert second.sent == []
    assert first.writes == second.writes == ["replace_passed"]


# ── a run acts only on the alert it read ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("first_days", "first_urgency", "second_days", "second_urgency"),
    [(60, "info", 6, "urgent"), (6, "urgent", 60, "info")],
    ids=["info-first", "urgent-first"],
)
async def test_differing_forecasts_read_at_the_same_time_keep_the_first_writers_alert(
    pool: asyncpg.Pool,
    first_days: int,
    first_urgency: str,
    second_days: int,
    second_urgency: str,
) -> None:
    today = await _today(pool)
    passed = today - timedelta(days=3)
    await _store(pool, deadline=passed)

    first, second = await _race(
        pool, _visa(today, first_days, first_urgency), _visa(today, second_days, second_urgency)
    )

    [new] = first.returned
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED, new.alert_id}
    assert _values(rows[new.alert_id]) == (
        "pending",
        first_urgency,
        today + timedelta(days=first_days),
        first_days,
        0,
    )
    assert _says(rows[new.alert_id], first_days)
    assert _values(rows[STORED]) == ("expired", "info", passed, 80, 0)
    assert [a.alert_id for a in second.returned] == [new.alert_id]
    assert [a.alert_id for a in first.sent] == [new.alert_id]
    assert second.sent == []
    assert first.writes == second.writes == ["replace_passed"]


async def test_the_alert_it_read_still_open_after_nothing_was_replaced_is_promoted_on_a_rise(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=10))
    engine, dispatcher, repo = _counted(pool)

    [alert] = await engine.generate_alerts([_visa(today, 30, "warning")])

    row = (await _rows(pool, VISA_KEY))[STORED]
    assert alert.alert_id == STORED
    assert _values(row) == ("pending", "warning", today + timedelta(days=30), 30, 1)
    assert [a.severity for a in dispatcher.sent] == ["warning"]
    assert repo.writes == ["replace_passed", "promote"]


async def test_a_key_left_with_no_open_alert_after_nothing_was_replaced_gets_a_new_one(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    passed = today - timedelta(days=3)
    await _store(pool, deadline=passed)
    engine, dispatcher, repo = _counted(pool, hooks={1: lambda: _close(pool)})

    [alert] = await engine.generate_alerts([_visa(today, 60, "info")])

    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED, alert.alert_id}
    assert _values(rows[STORED]) == ("resolved", "info", passed, 80, 0)
    assert _values(rows[alert.alert_id]) == ("pending", "info", today + timedelta(days=60), 60, 0)
    assert [a.alert_id for a in dispatcher.sent] == [alert.alert_id]
    assert repo.writes == ["replace_passed", "insert"]


@pytest.mark.parametrize(
    ("stored_days", "path_writes"),
    [(20, ["promote"]), (0, ["replace_passed", "promote"])],
    ids=["open-deadline", "deadline-today"],
)
async def test_two_ascending_promotions_are_two_escalations(
    pool: asyncpg.Pool, stored_days: int, path_writes: list[str]
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today + timedelta(days=stored_days))

    first, second = await _race(pool, _visa(today, 15, "warning"), _visa(today, 6, "urgent"))

    row = (await _rows(pool, VISA_KEY))[STORED]
    assert _values(row) == ("pending", "urgent", today + timedelta(days=6), 6, 2)
    assert _says(row, 6)
    assert [(a.alert_id, a.severity) for a in first.sent] == [(STORED, "warning")]
    assert [(a.alert_id, a.severity) for a in second.sent] == [(STORED, "urgent")]
    assert first.writes == second.writes == path_writes


async def test_a_replacement_first_refuses_the_promotion_of_the_alert_it_closed(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    passed = today - timedelta(days=3)
    await _store(pool, deadline=passed)

    first, second = await _race(
        pool, _visa(today, 60, "info"), _visa(today, 6, "urgent"), unrendered=("second",)
    )

    [new] = first.returned
    rows = await _rows(pool, VISA_KEY)
    assert set(rows) == {STORED, new.alert_id}
    assert _values(rows[STORED]) == ("expired", "info", passed, 80, 0)
    assert _values(rows[new.alert_id]) == ("pending", "info", today + timedelta(days=60), 60, 0)
    assert [a.alert_id for a in second.returned] == [new.alert_id]
    assert [a.alert_id for a in first.sent] == [new.alert_id]
    assert second.sent == []
    assert first.writes == ["replace_passed"]
    assert second.writes == ["promote"]


@pytest.mark.parametrize(
    ("second_urgency", "second_writes", "ends"),
    [
        ("info", ["replace_passed"], ("pending", "urgent", 6, 1)),
        ("critical", ["replace_passed", "promote"], ("pending", "critical", 60, 2)),
    ],
    ids=["lower-returns-it", "higher-promotes-it"],
)
async def test_a_promotion_first_leaves_nothing_to_replace(
    pool: asyncpg.Pool,
    second_urgency: str,
    second_writes: list[str],
    ends: tuple[str, str, int, int],
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3))

    first, second = await _race(
        pool, _visa(today, 6, "urgent"), _visa(today, 60, second_urgency), unrendered=("first",)
    )

    rows = await _rows(pool, VISA_KEY)
    status, severity, days, upgrades = ends
    assert set(rows) == {STORED}
    assert _values(rows[STORED]) == (
        status,
        severity,
        today + timedelta(days=days),
        days,
        upgrades,
    )
    assert [a.alert_id for a in second.returned] == [STORED]
    assert [(a.alert_id, a.severity) for a in first.sent] == [(STORED, "urgent")]
    assert first.writes == ["promote"]
    assert second.writes == second_writes
    if second_urgency == "critical":
        assert [(a.alert_id, a.severity) for a in second.sent] == [(STORED, "critical")]
        assert _says(rows[STORED], 60)
    else:
        assert second.sent == []
        assert rows[STORED]["message_en"] == "stored text"


async def test_an_alert_closed_after_the_second_read_is_not_promoted_and_nothing_is_inserted(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today)
    engine, dispatcher, repo = _counted(pool, hooks={2: lambda: _close(pool)})

    alerts = await engine.generate_alerts([_visa(today, 15, "warning")])

    rows = await _rows(pool, VISA_KEY)
    assert alerts == []
    assert set(rows) == {STORED}
    assert _values(rows[STORED]) == ("resolved", "info", today, 80, 0)
    assert dispatcher.sent == []
    assert repo.writes == ["replace_passed", "promote"]


async def test_a_key_taken_after_the_second_read_is_answered_with_the_alert_that_took_it(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    passed = today - timedelta(days=3)
    await _store(pool, deadline=passed)

    async def take() -> None:
        await _store(pool, deadline=today + timedelta(days=30), alert_id="alert_other")

    engine, dispatcher, repo = _counted(pool, hooks={1: lambda: _close(pool), 2: take})

    [alert] = await engine.generate_alerts([_visa(today, 60, "info")])

    rows = await _rows(pool, VISA_KEY)
    assert alert.alert_id == "alert_other"
    assert set(rows) == {STORED, "alert_other"}
    assert _values(rows["alert_other"]) == ("pending", "info", today + timedelta(days=30), 80, 0)
    assert dispatcher.sent == []
    assert repo.writes == ["replace_passed", "insert"]


async def test_a_nearer_more_severe_alert_stays_against_a_later_milder_forecast(
    pool: asyncpg.Pool,
) -> None:
    today = await _today(pool)
    await _store(pool, deadline=today - timedelta(days=3))
    engine, dispatcher, _ = _counted(pool)
    [urgent] = await engine.generate_alerts([_visa(today, 6, "urgent")])
    later, later_sent, later_repo = _counted(pool)

    [alert] = await later.generate_alerts([_visa(today, 60, "info")])

    row = (await _rows(pool, VISA_KEY))[urgent.alert_id]
    assert alert.alert_id == urgent.alert_id
    assert _values(row) == ("pending", "urgent", today + timedelta(days=6), 6, 0)
    assert _says(row, 6)
    assert later_sent.sent == []
    assert later_repo.writes == ["replace_passed"]


@pytest.mark.parametrize(
    ("urgency", "writes", "ends"),
    [
        ("warning", [], ("pending", "warning", -3, 0)),
        ("urgent", ["promote"], ("pending", "urgent", 60, 1)),
    ],
    ids=["same-severity", "higher-severity"],
)
async def test_an_e33_guarantee_alert_is_never_replaced_only_promoted_in_place(
    pool: asyncpg.Pool, urgency: str, writes: list[str], ends: tuple[str, str, int, int]
) -> None:
    today = await _today(pool)
    await _store(
        pool,
        deadline=today - timedelta(days=3),
        key=GUARANTEE_KEY,
        category="guarantee_proof",
        severity="warning",
    )
    engine, dispatcher, repo = _counted(pool)

    [alert] = await engine.generate_alerts(
        [
            _forecast(
                today,
                expiry=today + timedelta(days=60),
                document_type="e33_guarantee",
                urgency=urgency,
            )
        ]
    )

    rows = await _rows(pool, GUARANTEE_KEY)
    status, severity, days, upgrades = ends
    assert set(rows) == {STORED}
    assert alert.alert_id == STORED
    assert (rows[STORED]["status"], rows[STORED]["severity"], rows[STORED]["deadline"]) == (
        status,
        severity,
        today + timedelta(days=days),
    )
    assert rows[STORED]["upgrade_count"] == upgrades
    assert [a.severity for a in dispatcher.sent] == ([urgency] if upgrades else [])
    assert repo.writes == writes
