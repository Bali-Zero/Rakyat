"""Payment-time emails go out ONCE, against a PROD-SHAPED schema (real Postgres).

Rules under test (owner decisions 2026-11-13):

* the payment thank-you (client) and "Payment In" (team) go out only on the FIRST transition
  to paid; the "already sent" fact is a blocking row in ``email_send_log``;
* a practice that is already paid gets NO start email on On Process (the payment emails
  already said it); an On Process BEFORE any payment still sends the start emails, and their
  text claims no payment;
* two paid ticks racing send once.

A mock cannot see the advisory lock, the CHECK constraints or the ``practices.payment_status``
read, so the tables below are shaped like prod (``email_send_log`` is migration 126, verbatim
constraints). Fixtures are placeholders only: no client data.

Requires the mandate-owned disposable PostgreSQL (``NUZ_UNIVERSAL_PG_SYSTEM_ID`` /
``NUZ_UNIVERSAL_PG_DSN``); skipped otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from backend.services.crm.practice_status_listener import PracticeStatusListener
from backend.services.notifications.email_audit import claim_email_once
from backend.services.notifications.email_health_monitor import EmailHealthMonitor

_SCHEMA_SQL = """
CREATE TABLE clients (
    id serial PRIMARY KEY, full_name text, email text, phone text, address text,
    nationality text, drive_folder_id text, drive_folder_url text
);
CREATE TABLE practice_types (id serial PRIMARY KEY, code text, name text);
CREATE TABLE practices (
    id serial PRIMARY KEY, client_id integer, practice_type_id integer,
    assigned_to text, created_by text, status text, payment_status text,
    quoted_price numeric
);
CREATE TABLE activity_log (
    id serial PRIMARY KEY, entity_type text, entity_id integer, action text,
    performed_by text, description text
);
CREATE TABLE email_send_log (
    id BIGSERIAL PRIMARY KEY,
    email_type VARCHAR(50) NOT NULL, practice_id INTEGER, client_id INTEGER,
    to_email VARCHAR(255) NOT NULL, subject VARCHAR(500), provider VARCHAR(20),
    status VARCHAR(20) NOT NULL, error_message TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1 CHECK (attempt_number >= 1),
    payload_cache JSONB,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMP WITH TIME ZONE, retry_after TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_email_send_log_status
        CHECK (status IN ('sending','sent','failed','retry_scheduled',
                          'skipped_idempotent','escalated','superseded')),
    CONSTRAINT ck_email_send_log_provider
        CHECK (provider IS NULL OR provider IN ('brevo','zoho','skipped','unknown'))
);
"""

TEAM = "team.member@example.test"
CLIENT = "client@example.test"


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    expected = os.environ.get("NUZ_UNIVERSAL_PG_SYSTEM_ID")
    dsn = os.environ.get("NUZ_UNIVERSAL_PG_DSN")
    if not expected or not dsn:
        pytest.skip("Requires mandate-owned disposable PostgreSQL")
    admin = await asyncpg.connect(dsn)
    schema = "payonce_" + uuid4().hex
    created = None
    try:
        identity = await admin.fetchrow(
            "SELECT current_database() AS db, system_identifier::text AS system_id "
            "FROM pg_control_system()"
        )
        if dict(identity) != {"db": "universal_db_test", "system_id": expected}:
            pytest.fail("Refusing a database outside this disposable test cluster")
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        created = await asyncpg.create_pool(
            dsn, min_size=1, max_size=6, server_settings={"search_path": schema}
        )
        await created.execute(_SCHEMA_SQL)
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def _seed(pool: asyncpg.Pool, *, email: str | None = CLIENT, payment: str = "unpaid") -> int:
    await pool.execute(
        "INSERT INTO practice_types (code, name) VALUES ('CODE_X', 'Service Display Name')"
    )
    client = await pool.fetchval(
        "INSERT INTO clients (full_name, email) VALUES ('Placeholder Client', $1) RETURNING id",
        email,
    )
    return await pool.fetchval(
        "INSERT INTO practices (client_id, practice_type_id, assigned_to, created_by, status,"
        " payment_status, quoted_price) VALUES ($1, 1, $2, NULL, 'sending_invoice', $3, 1000)"
        " RETURNING id",
        client,
        TEAM,
        payment,
    )


class _Posts:
    """Records every email handed to the internal send API; optionally fails them."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = False

    def client_cls(self) -> Any:
        posts = self

        class _Resp:
            def raise_for_status(self) -> None:
                if posts.fail:
                    raise RuntimeError("provider down")

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None: ...

            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

            async def post(self, url: str, headers: Any = None, json: Any = None) -> _Resp:
                posts.sent.append(json)
                return _Resp()

        return _Client


@pytest.fixture
def posts():
    p = _Posts()
    cls = p.client_cls()
    with (
        patch("backend.services.crm.practice_status_listener.httpx.AsyncClient", cls),
        patch("backend.services.crm.automation.httpx.AsyncClient", cls),
        patch("backend.services.notifications.email_audit.notify_email_failure_critical"),
        patch("backend.services.crm.practice_status_listener.notify_email_failure_critical"),
        patch("backend.services.crm.automation.notify_email_failure_critical"),
    ):
        yield p


def _listener(pool: asyncpg.Pool) -> PracticeStatusListener:
    return PracticeStatusListener(db_dsn="unused", db_pool=pool)


def _payload(pid: int, **kw: Any) -> str:
    data = {"practice_id": pid, "assigned_to": TEAM}
    data.update(kw)
    return json.dumps(data)


async def _paid(listener: PracticeStatusListener, pid: int, old: str = "unpaid") -> None:
    await listener._handle_notification(
        _payload(
            pid,
            old_payment=old,
            new_payment="paid",
            old_status="sending_invoice",
            new_status="sending_invoice",
        )
    )


async def _to_on_process(listener: PracticeStatusListener, pid: int) -> None:
    await listener._handle_notification(
        _payload(
            pid,
            old_status="sending_invoice",
            new_status="on_process",
            old_payment="unpaid",
            new_payment="unpaid",
        )
    )


async def _log(pool: asyncpg.Pool) -> list[tuple[str, str]]:
    rows = await pool.fetch("SELECT email_type, status FROM email_send_log ORDER BY id")
    return [(r["email_type"], r["status"]) for r in rows]


def _kinds(posts: _Posts) -> list[str]:
    out = []
    for m in posts.sent:
        s = m["subject"]
        out.append(
            "pay_client"
            if "Payment Received" in s
            else "pay_team"
            if "[PAYMENT IN]" in s
            else "start_client"
            if "We're Starting" in s
            else "start_team"
            if "[TEAM]" in s and "Let's Go" in s
            else s
        )
    return out


# ── GUILT: the duplicates the owner saw ─────────────────────────────────────


@pytest.mark.asyncio
async def test_paid_then_on_process_sends_one_client_and_one_team_email(pool, posts):
    """Spec B-1: paid, then On Process: exactly the two payment emails, no start emails."""
    pid = await _seed(pool)
    listener = _listener(pool)
    await _paid(listener, pid)
    await pool.execute("UPDATE practices SET payment_status = 'paid', status = 'on_process'")
    await _to_on_process(listener, pid)

    assert sorted(_kinds(posts)) == ["pay_client", "pay_team"]
    log = await _log(pool)
    assert log == [("payment_client", "sent"), ("payment_team", "sent")]
    skipped = await pool.fetchval(
        "SELECT count(*) FROM activity_log WHERE action = 'process_started'"
        " AND description LIKE 'Process started - no start emails%'"
    )
    assert skipped == 1


@pytest.mark.asyncio
async def test_a_later_unpaid_to_paid_cycle_sends_nothing(pool, posts):
    """Spec B-2: the thank-you is for the FIRST transition to paid only."""
    pid = await _seed(pool)
    listener = _listener(pool)
    await _paid(listener, pid)
    await _paid(listener, pid, old="partial")
    await _paid(listener, pid)
    assert sorted(_kinds(posts)) == ["pay_client", "pay_team"]
    assert len(await _log(pool)) == 2


@pytest.mark.asyncio
async def test_two_paid_ticks_racing_send_once(pool, posts):
    """Spec B-3: the advisory lock makes check+claim atomic."""
    pid = await _seed(pool)
    listener = _listener(pool)
    await asyncio.gather(*[_paid(listener, pid) for _ in range(5)])
    assert sorted(_kinds(posts)) == ["pay_client", "pay_team"]


@pytest.mark.asyncio
async def test_client_without_email_gets_the_team_mail_once(pool, posts):
    """Spec B-4: no client address: the team mail takes the claim and is also once-only."""
    pid = await _seed(pool, email=None)
    listener = _listener(pool)
    await _paid(listener, pid)
    await _paid(listener, pid)
    assert _kinds(posts) == ["pay_team"]
    assert await _log(pool) == [("payment_team", "sent")]


# ── INNOCENCE: what must keep working ───────────────────────────────────────


@pytest.mark.asyncio
async def test_on_process_before_payment_sends_start_emails_without_a_payment_claim(pool, posts):
    """Spec B-5: On Process first: both start emails go out, and say nothing about payment."""
    pid = await _seed(pool, payment="unpaid")
    listener = _listener(pool)
    await _to_on_process(listener, pid)

    assert sorted(_kinds(posts)) == ["start_client", "start_team"]
    for m in posts.sent:
        assert "Payment Confirmed" not in m["subject"]
        assert "received your payment" not in m["body"]
        assert "payment has just been confirmed" not in m["body"]
        assert "confirmed the payment" not in m["body"]
    assert await _log(pool) == [("process_start_client", "sent"), ("process_start_team", "sent")]


@pytest.mark.asyncio
async def test_payment_after_an_early_on_process_still_sends_its_emails(pool, posts):
    """Spec B-6: the first paid still thanks the client, even after an early On Process."""
    pid = await _seed(pool)
    listener = _listener(pool)
    await _to_on_process(listener, pid)
    await _paid(listener, pid)
    assert sorted(_kinds(posts)) == ["pay_client", "pay_team", "start_client", "start_team"]


@pytest.mark.asyncio
async def test_partial_payment_does_not_suppress_the_start_emails(pool, posts):
    """Spec B-7: only paid covers the start; partial does not."""
    pid = await _seed(pool, payment="partial")
    await _to_on_process(_listener(pool), pid)
    assert sorted(_kinds(posts)) == ["start_client", "start_team"]


@pytest.mark.asyncio
async def test_a_failed_first_attempt_does_not_block_the_next_paid(pool, posts):
    """Spec B-8: a payment mail that never reached anyone may be sent by a later paid."""
    pid = await _seed(pool)
    listener = _listener(pool)
    posts.fail = True
    await _paid(listener, pid)
    assert posts.sent  # attempted
    assert {s for _, s in await _log(pool)} == {"failed"}
    posts.fail = False
    posts.sent.clear()
    await _paid(listener, pid, old="partial")
    assert sorted(_kinds(posts)) == ["pay_client", "pay_team"]


@pytest.mark.asyncio
async def test_a_recovered_send_closes_the_earlier_failed_attempts(pool, posts):
    """Gate finding P2: both first sends fail and schedule a retry; a later paid succeeds.
    The old failed rows must leave the retry worker's reach, or the team mail goes twice."""
    pid = await _seed(pool)
    listener = _listener(pool)
    posts.fail = True
    await _paid(listener, pid)
    await pool.execute(
        "UPDATE email_send_log SET retry_after = NOW() - interval '1 minute' WHERE practice_id = $1",
        pid,
    )
    posts.fail = False
    posts.sent.clear()
    await _paid(listener, pid, old="partial")
    assert sorted(_kinds(posts)) == ["pay_client", "pay_team"]
    assert not await pool.fetch(
        "SELECT 1 FROM email_send_log WHERE practice_id = $1 AND status = 'failed'", pid
    ), "a failed row survived the recovery and the retry worker would resend it"
    assert await pool.fetchval(
        "SELECT count(*) FROM email_send_log WHERE practice_id = $1 AND status = 'superseded'", pid
    )
    assert not await pool.fetch(
        "SELECT 1 FROM email_send_log WHERE practice_id = $1 AND retry_after IS NOT NULL "
        "AND status = 'failed'",
        pid,
    )


@pytest.mark.asyncio
async def test_the_payment_mail_promises_no_separate_start_email(pool, posts):
    """Gate finding P2: a paid practice gets no start email, so the thank-you must not promise one."""
    pid = await _seed(pool)
    await _paid(_listener(pool), pid)
    client = [m for m in posts.sent if "Payment Received" in m["subject"]]
    assert len(client) == 1
    text = json.dumps(client[0])
    assert "separate email" not in text and "kick off" not in text


# ── the retry worker against the claim ─────────────────────────────────────


class _RaceConn:
    """Runs a hook right after the worker's SELECT returns: the rows it read are then stale."""

    def __init__(self, conn: Any, hook: Any) -> None:
        self._conn = conn
        self._hook = hook

    async def fetch(self, *a: Any, **k: Any) -> Any:
        rows = await self._conn.fetch(*a, **k)
        if self._hook is not None:
            hook, self._hook = self._hook, None
            await hook()
        return rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _RacePool:
    def __init__(self, pool: asyncpg.Pool, hook: Any = None) -> None:
        self._pool = pool
        self._hook = hook

    def acquire(self) -> Any:
        outer = self

        class _Cm:
            async def __aenter__(self) -> _RaceConn:
                self._c = await outer._pool.acquire()
                return _RaceConn(self._c, outer._hook)

            async def __aexit__(self, *a: Any) -> None:
                await outer._pool.release(self._c)

        return _Cm()


class _Retries:
    """The retry worker's send client: records '[RETRY]' mails, optionally failing."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail = False

    def patch(self) -> Any:
        me = self

        async def post(url: str, headers: Any = None, json: Any = None) -> Any:
            me.sent.append(json)
            if me.fail:
                raise RuntimeError("provider down")
            return MagicMock()

        client = MagicMock()
        client.post = post
        return patch(
            "backend.services.notifications.email_health_monitor.get_email_client",
            new=AsyncMock(return_value=client),
        )


async def _fail_first_paid_and_make_retries_due(pool: asyncpg.Pool, posts: _Posts) -> int:
    pid = await _seed(pool)
    posts.fail = True
    await _paid(_listener(pool), pid)
    posts.fail = False
    posts.sent.clear()
    await pool.execute(
        "UPDATE email_send_log SET retry_after = NOW() - interval '1 minute'"
        " WHERE status = 'failed' AND retry_after IS NOT NULL"
    )
    assert await pool.fetchval("SELECT count(*) FROM email_send_log WHERE status = 'failed'") == 2
    assert (
        await pool.fetchval("SELECT count(*) FROM email_send_log WHERE retry_after IS NOT NULL")
        == 1
    ), "only the team mail is retryable"
    return pid


@pytest.mark.asyncio
async def test_a_worker_that_read_a_failed_row_cannot_revive_it_after_recovery(pool, posts):
    """SPEC A-1 (the gate's reproduction): the worker SELECTs the failed team row, a later paid
    recovers and supersedes it, the worker then runs its claim step: it must send nothing."""
    pid = await _fail_first_paid_and_make_retries_due(pool, posts)
    listener = _listener(pool)
    retries = _Retries()

    async def recover() -> None:
        await _paid(listener, pid, old="partial")

    monitor = EmailHealthMonitor(_RacePool(pool, recover))
    with retries.patch():
        stats = await monitor.check_and_retry_failed_emails()
    assert retries.sent == [], "the worker mailed a [RETRY] duplicate of a recovered send"
    assert stats["considered"] == 0 and stats["retried"] == 0
    assert sorted(_kinds(posts)) == ["pay_client", "pay_team"]
    assert not await pool.fetch(
        "SELECT 1 FROM email_send_log WHERE status IN ('failed', 'retry_scheduled')"
    )


@pytest.mark.asyncio
async def test_a_genuine_repeat_failure_is_still_retried_and_stays_actionable(pool, posts):
    """SPEC A-2: no later paid; the due failed row is claimed and retried, and a second failure
    leaves a failed successor with the next retry_after."""
    await _fail_first_paid_and_make_retries_due(pool, posts)
    retries = _Retries()
    retries.fail = True
    with retries.patch():
        stats = await EmailHealthMonitor(pool).check_and_retry_failed_emails()
    assert stats["considered"] == 1 and stats["retried"] == 1 and stats["failed_again"] == 1
    assert len(retries.sent) == 1
    row = await pool.fetchrow(
        "SELECT attempt_number, retry_after FROM email_send_log"
        " WHERE status = 'failed' AND email_type = 'payment_team'"
    )
    assert row is not None and row["attempt_number"] == 2 and row["retry_after"] is not None


@pytest.mark.asyncio
async def test_two_workers_over_one_failed_row_send_one_retry(pool, posts):
    """SPEC A-3."""
    await _fail_first_paid_and_make_retries_due(pool, posts)
    retries = _Retries()
    with retries.patch():
        await asyncio.gather(
            EmailHealthMonitor(pool).check_and_retry_failed_emails(),
            EmailHealthMonitor(pool).check_and_retry_failed_emails(),
        )
    assert len(retries.sent) == 1


@pytest.mark.asyncio
async def test_a_paid_after_the_worker_claimed_sends_no_second_mail(pool, posts):
    """SPEC A-4: the worker's retry_scheduled claim blocks the later paid's claim."""
    pid = await _fail_first_paid_and_make_retries_due(pool, posts)
    await pool.execute(
        "UPDATE email_send_log SET status = 'retry_scheduled' WHERE email_type = 'payment_team'"
    )
    await _paid(_listener(pool), pid, old="partial")
    assert posts.sent == []


# ── the claim itself ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_is_per_practice_and_only_blocked_by_live_rows(pool):
    """Spec B-9: another practice is independent; failed/escalated rows do not block."""
    group = ("payment_client", "payment_team")

    async def claim(pid: int) -> bool:
        ok, _ = await claim_email_once(
            pool,
            email_type="payment_client",
            once_group=group,
            to_email=CLIENT,
            subject="s",
            practice_id=pid,
        )
        return ok

    assert await claim(1) is True
    assert await claim(1) is False
    assert await claim(2) is True
    await pool.execute("UPDATE email_send_log SET status = 'failed' WHERE practice_id = 1")
    assert await claim(1) is True
    await pool.execute("UPDATE email_send_log SET status = 'escalated' WHERE practice_id = 1")
    assert await claim(1) is True


@pytest.mark.asyncio
async def test_concurrent_claims_have_exactly_one_winner(pool):
    """Spec B-10: eight claims at once for one practice: one winner, one row.

    Without the per-practice advisory lock every claim reads "no row yet" and inserts.
    """
    group = ("payment_client", "payment_team")

    async def claim() -> bool:
        ok, _ = await claim_email_once(
            pool,
            email_type="payment_client",
            once_group=group,
            to_email=CLIENT,
            subject="s",
            practice_id=7,
        )
        return ok

    for _ in range(5):
        await pool.execute("DELETE FROM email_send_log")
        results = await asyncio.gather(*[claim() for _ in range(8)])
        assert results.count(True) == 1
        assert await pool.fetchval("SELECT count(*) FROM email_send_log") == 1
