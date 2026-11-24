"""PostgreSQL coverage for the intake login-gate document count."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio

from backend.app.routers.intake_review import list_review_queue
from backend.services.intake.gate_evaluator import evaluate_gate_status

DSN = os.environ.get("INTAKE_TEST_DSN", "postgresql://localhost:5432/nuzantara_test")


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    try:
        value = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    except Exception:
        pytest.skip(f"local intake DB not reachable at {DSN}")
    yield value
    await value.close()


async def _insert_pending_proposal(
    pool: asyncpg.Pool,
    *,
    source: str,
    received_by: str,
    status: str = "review_pending",
    lease_owner: str | None = None,
    lease_expires_at: str | None = None,
) -> tuple[int, int]:
    token = uuid.uuid4().hex
    blob_hash = hashlib.sha256(token.encode()).hexdigest()
    async with pool.acquire() as conn:
        instance_id = await conn.fetchval(
            """
            INSERT INTO document_instances
                (blob_hash, pipeline_version, blob_path, first_source, first_source_ref)
            VALUES ($1, 'gate-test-v1', $2, $3, $4)
            RETURNING id
            """,
            blob_hash,
            f"/tmp/{token}.pdf",
            source,
            f"synthetic-{token}",
        )
        queue_id = await conn.fetchval(
            """
            INSERT INTO intake_queue
                (instance_id, source, source_ref, blob_path, blob_hash, pipeline_version,
                 status, intake_key, received_by)
            VALUES ($1, $2, $3, $4, $5, 'gate-test-v1', 'review_pending', $6, $7)
            RETURNING id
            """,
            instance_id,
            source,
            f"synthetic-{token}",
            f"/tmp/{token}.pdf",
            blob_hash,
            f"{source}|synthetic-{token}|{blob_hash}|gate-test-v1",
            received_by,
        )
        await conn.execute(
            """
            INSERT INTO document_routing_proposal
                (queue_id, doc_index, pipeline_version, routing_key,
                 entity_resolution, routing, commit_gate, status, lease_owner,
                 lease_expires_at)
            VALUES ($1, 0, 'gate-test-v1', $2, $3::jsonb, $4::jsonb, $5::jsonb,
                    $6, $7, $8::timestamptz)
            """,
            queue_id,
            f"synthetic-routing-{token}",
            json.dumps({"decision": "NO_MATCH", "candidates": []}),
            json.dumps({"client_id": None, "company_id": None, "fields": {}}),
            json.dumps({"decision": "NO_MATCH"}),
            status,
            lease_owner,
            datetime.fromisoformat(lease_expires_at) if lease_expires_at else None,
        )
        return instance_id, queue_id


async def _delete_proposal_fixture(pool: asyncpg.Pool, fixture: tuple[int, int]) -> None:
    instance_id, queue_id = fixture
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM intake_queue WHERE id = $1", queue_id)
        await conn.execute("DELETE FROM document_instances WHERE id = $1", instance_id)


async def _insert_deadline_alert_client(
    pool: asyncpg.Pool, *, staff_email: str, deleted: bool
) -> tuple[int, str]:
    client_name = f"Synthetic Gate Client {uuid.uuid4().hex}"
    alert_id = f"synthetic-gate-alert-{uuid.uuid4().hex}"
    async with pool.acquire() as conn:
        client_id = await conn.fetchval(
            """
            INSERT INTO clients (full_name, assigned_to, deleted_at)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            client_name,
            staff_email,
            datetime.now() if deleted else None,
        )
        await conn.execute(
            """
            INSERT INTO compliance_alerts
                (alert_id, client_id, category, severity, status, deadline, days_until, dedup_key)
            VALUES ($1, $2, 'visa_expiry', 'urgent', 'pending', $3, 1, $4)
            """,
            alert_id,
            client_id,
            date.today() + timedelta(days=1),
            f"synthetic-gate:{client_id}:{alert_id}",
        )
    return client_id, alert_id


async def _delete_deadline_alert_client(
    pool: asyncpg.Pool, *, client_id: int, alert_id: str
) -> None:
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM compliance_alerts WHERE alert_id = $1", alert_id)
        await conn.execute("DELETE FROM clients WHERE id = $1", client_id)


@pytest.mark.asyncio
async def test_gate_does_not_count_deadline_alert_for_soft_deleted_client(
    pool: asyncpg.Pool,
) -> None:
    staff_email = f"synthetic-gate-{uuid.uuid4().hex}@example.test"
    client_id, alert_id = await _insert_deadline_alert_client(
        pool, staff_email=staff_email, deleted=True
    )
    try:
        result = await evaluate_gate_status(pool, user_email=staff_email)
    finally:
        await _delete_deadline_alert_client(pool, client_id=client_id, alert_id=alert_id)

    assert result["sections"]["deadlines"]["count"] == 0


@pytest.mark.asyncio
async def test_gate_counts_deadline_alert_for_live_client(
    pool: asyncpg.Pool,
) -> None:
    staff_email = f"synthetic-gate-{uuid.uuid4().hex}@example.test"
    live_fixture = await _insert_deadline_alert_client(
        pool, staff_email=staff_email, deleted=False
    )
    deleted_fixture = await _insert_deadline_alert_client(
        pool, staff_email=staff_email, deleted=True
    )
    try:
        result = await evaluate_gate_status(pool, user_email=staff_email)
    finally:
        for client_id, alert_id in (live_fixture, deleted_fixture):
            await _delete_deadline_alert_client(pool, client_id=client_id, alert_id=alert_id)

    assert result["sections"]["deadlines"]["count"] == 1


@pytest.mark.asyncio
async def test_gate_does_not_hold_staff_for_pending_drive_proposal_not_in_review_feed(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTAKE_GATE_USE_MIRROR", raising=False)
    monkeypatch.delenv("INTAKE_REVIEW_SOURCES", raising=False)
    staff_email = f"synthetic-gate-{uuid.uuid4().hex}@example.test"
    fixture = await _insert_pending_proposal(
        pool, source="drive", received_by=staff_email
    )
    try:
        feed = await list_review_queue(
            user={"email": staff_email}, pool=pool, status="review_pending", source=None,
            decision=None, limit=50, offset=0,
        )
        result = await evaluate_gate_status(pool, user_email=staff_email)
    finally:
        await _delete_proposal_fixture(pool, fixture)

    assert feed["total"] == 0
    assert result["sections"]["documents"]["count"] == feed["total"]
    assert result["blocked"] is False


@pytest.mark.asyncio
async def test_gate_holds_staff_for_pending_whatsapp_proposal_in_review_feed(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTAKE_GATE_USE_MIRROR", raising=False)
    monkeypatch.delenv("INTAKE_REVIEW_SOURCES", raising=False)
    staff_email = f"synthetic-gate-{uuid.uuid4().hex}@example.test"
    fixtures = [
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by=staff_email
        ),
        await _insert_pending_proposal(
            pool, source="drive", received_by=staff_email
        ),
    ]
    try:
        feed = await list_review_queue(
            user={"email": staff_email}, pool=pool, status="review_pending", source=None,
            decision=None, limit=50, offset=0,
        )
        result = await evaluate_gate_status(pool, user_email=staff_email)
    finally:
        for fixture in fixtures:
            await _delete_proposal_fixture(pool, fixture)

    assert feed["total"] == 1
    assert result["sections"]["documents"]["count"] == feed["total"]
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_gate_and_feed_normalize_receiver_case_identically(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTAKE_GATE_USE_MIRROR", raising=False)
    monkeypatch.delenv("INTAKE_REVIEW_SOURCES", raising=False)
    receiver_email = f"Synthetic-Case-{uuid.uuid4().hex}@example.test"
    counted_fixture = await _insert_pending_proposal(
        pool, source="whatsapp", received_by=receiver_email.upper()
    )
    other_fixture = await _insert_pending_proposal(
        pool, source="whatsapp", received_by="another-receiver@example.test"
    )
    try:
        feed = await list_review_queue(
            user={"email": receiver_email}, pool=pool, status="review_pending", source=None,
            decision=None, limit=50, offset=0,
        )
        result = await evaluate_gate_status(pool, user_email=receiver_email)
    finally:
        await _delete_proposal_fixture(pool, counted_fixture)
        await _delete_proposal_fixture(pool, other_fixture)

    assert result["sections"]["documents"]["count"] == 1
    assert feed["total"] == 1
    assert [item["queue_id"] for item in feed["items"]] == [counted_fixture[1]]


@pytest.mark.asyncio
async def test_gate_counts_reclaimable_review_proposals_for_receiver(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTAKE_GATE_USE_MIRROR", raising=False)
    monkeypatch.delenv("INTAKE_REVIEW_SOURCES", raising=False)
    staff_email = f"synthetic-gate-{uuid.uuid4().hex}@example.test"
    colleague_email = f"synthetic-colleague-{uuid.uuid4().hex}@example.test"
    fixtures = [
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by=staff_email.upper(),
        ),
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by=staff_email,
            status="review_claimed", lease_owner=staff_email,
            lease_expires_at="2999-01-01T00:00:00+00:00",
        ),
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by=staff_email,
            status="review_claimed", lease_owner=colleague_email,
            lease_expires_at="2999-01-01T00:00:00+00:00",
        ),
        await _insert_pending_proposal(
            pool, source="drive", received_by=staff_email,
            status="review_claimed", lease_owner=colleague_email,
            lease_expires_at="2000-01-01T00:00:00+00:00",
        ),
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by=staff_email,
            status="review_claimed", lease_owner=colleague_email,
            lease_expires_at="2000-01-01T00:00:00+00:00",
        ),
    ]
    try:
        result = await evaluate_gate_status(pool, user_email=staff_email)
    finally:
        for fixture in fixtures:
            await _delete_proposal_fixture(pool, fixture)

    assert result["sections"]["documents"]["count"] == 3
    assert result["blocked"] is True


@pytest.mark.asyncio
async def test_admin_gate_counts_only_proposals_received_by_admin(
    pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("INTAKE_GATE_USE_MIRROR", raising=False)
    monkeypatch.delenv("INTAKE_REVIEW_SOURCES", raising=False)
    admin_email = f"synthetic-admin-{uuid.uuid4().hex}@example.test"
    fixtures = [
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by=admin_email,
        ),
        await _insert_pending_proposal(
            pool, source="drive", received_by=admin_email,
        ),
        await _insert_pending_proposal(
            pool, source="whatsapp", received_by="other-staff@example.test",
        ),
    ]
    try:
        result = await evaluate_gate_status(
            pool, user_email=admin_email, is_admin=True
        )
    finally:
        for fixture in fixtures:
            await _delete_proposal_fixture(pool, fixture)

    assert result["sections"]["documents"]["count"] == 1
    assert result["blocked"] is False
