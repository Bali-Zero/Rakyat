"""Real PostgreSQL proof of the retirement predicate, in a TEMP-only queue.

No production DSN or customer data: the pre-existing localhost scratch database
is used only for a session-local TEMP table shadowing the production queue.
"""

import os
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import asyncpg
import pytest

from backend.services.garuda_orders import outbox_consumer
from backend.services.garuda_orders.outbox_consumer import (
    count_undrained,
    drain_once,
)

_STAFF_PAGE_TYPES = (
    "staff_page_duplicate_charge",
    "staff_page_late_paid_after_refund",
    "staff_page_late_paid_after_terminal",
    "staff_page_charge_without_webhook",
    "staff_page_payment_failure",
    "staff_page_refund_out_of_order",
)

_SYNTHETIC_RETIRED_TYPES = tuple(f"synthetic_retired_tg_{i}" for i in range(6))


@pytest.mark.asyncio
async def test_retired_jobs_are_held_while_email_is_claimed(monkeypatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    target = urlsplit(dsn)
    if target.hostname not in {"localhost", "127.0.0.1", "::1"} or not target.path.endswith(
        ("_test", "/test")
    ):
        pytest.skip("TEMP proof requires an explicitly local test-named database")
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip(
            "local PostgreSQL scratch database unavailable; hosted schema suite also covers this predicate"
        )
    try:
        # A forwarded operational database is not an acceptable scratch peer.
        peer = await conn.fetchval("SELECT inet_server_addr()::text")
        if peer not in {"127.0.0.1/32", "::1/128", "127.0.0.1", "::1", None}:
            pytest.skip("PostgreSQL scratch peer is not local")
        await conn.execute("""
            CREATE TEMP TABLE garuda_order_outbox (
                id bigserial PRIMARY KEY, order_id text NOT NULL,
                journal_event_id text NOT NULL, job_type text NOT NULL,
                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                attempts integer NOT NULL DEFAULT 0,
                dispatched_at timestamptz,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        monkeypatch.setenv("GARUDA_OUTBOX_CONSUMER_ENABLED", "true")
        assert outbox_consumer.RETIRED_TELEGRAM_JOB_TYPES == ()

        # Already-queued staff_page_* jobs drain via the email channel without migration.
        for kind in _STAFF_PAGE_TYPES:
            await conn.execute(
                "INSERT INTO garuda_order_outbox(order_id,journal_event_id,job_type) VALUES('synthetic-order','synthetic-event',$1)",
                kind,
            )
        await conn.execute(
            "INSERT INTO garuda_order_outbox(order_id,journal_event_id,job_type) VALUES('synthetic-order','synthetic-event','payment_paid_email')"
        )
        live_handler = AsyncMock()
        live_handlers = dict.fromkeys(_STAFF_PAGE_TYPES, live_handler)
        live_handlers["payment_paid_email"] = live_handler
        live_stats = await drain_once(conn, live_handlers)
        assert live_stats.claimed == live_stats.dispatched == 7
        assert live_stats.failed == live_stats.unroutable == 0
        counts = await count_undrained(conn)
        assert counts["retired_internal_attention"] == counts["undispatched"] == 0

        await conn.execute("TRUNCATE garuda_order_outbox")
        monkeypatch.setattr(outbox_consumer, "RETIRED_TELEGRAM_JOB_TYPES", _SYNTHETIC_RETIRED_TYPES)
        for kind in _SYNTHETIC_RETIRED_TYPES:
            await conn.execute(
                "INSERT INTO garuda_order_outbox(order_id,journal_event_id,job_type) VALUES('synthetic-order','synthetic-event',$1)",
                kind,
            )
        await conn.execute(
            "INSERT INTO garuda_order_outbox(order_id,journal_event_id,job_type) VALUES('synthetic-order','synthetic-event','payment_paid_email')"
        )
        handler = AsyncMock()
        handlers = dict.fromkeys(_SYNTHETIC_RETIRED_TYPES, handler)
        handlers["payment_paid_email"] = handler
        stats = await drain_once(conn, handlers)
        assert stats.claimed == stats.dispatched == 1
        assert stats.failed == stats.unroutable == 0
        assert handler.await_args.args[0].job_type == "payment_paid_email"
        held = await conn.fetch(
            "SELECT attempts,dispatched_at FROM garuda_order_outbox WHERE job_type <> 'payment_paid_email'"
        )
        assert len(held) == 6
        assert all(row["attempts"] == 0 and row["dispatched_at"] is None for row in held)
        counts = await count_undrained(conn)
        assert counts["retired_internal_attention"] == counts["undispatched"] == 6
        assert counts["exhausted"] == 0

        # Intentional holds must not turn service health stale after 24 hours,
        # including rows that were already exhausted before retirement.
        await conn.execute(
            "UPDATE garuda_order_outbox SET created_at=now()-interval '25 hours', attempts=5 "
            "WHERE job_type <> 'payment_paid_email'"
        )
        counts = await count_undrained(conn)
        assert counts["retired_internal_attention"] == counts["undispatched"] == 6
        assert counts["older_than_1h"] == counts["older_than_24h"] == counts["exhausted"] == 0
        assert (await drain_once(conn, handlers)).claimed == 0
        held = await conn.fetch(
            "SELECT attempts,dispatched_at FROM garuda_order_outbox WHERE job_type <> 'payment_paid_email'"
        )
        assert all(row["attempts"] == 5 and row["dispatched_at"] is None for row in held)

        # A genuinely pending customer email must still degrade the age metrics.
        await conn.execute(
            "INSERT INTO garuda_order_outbox(order_id,journal_event_id,job_type,created_at) "
            "VALUES('synthetic-order','synthetic-overdue-email','payment_paid_email',now()-interval '25 hours')"
        )
        counts = await count_undrained(conn)
        assert counts["retired_internal_attention"] == 6 and counts["undispatched"] == 7
        assert counts["older_than_1h"] == counts["older_than_24h"] == 1
        assert counts["exhausted"] == 0
        await conn.execute(
            "UPDATE garuda_order_outbox SET attempts=5 WHERE journal_event_id='synthetic-overdue-email'"
        )
        counts = await count_undrained(conn)
        assert counts["exhausted"] == 1
    finally:
        await conn.close()
