"""Dedup wall (LEVA 3) is version-preserving for ``recovery-v1``.

The type-level wall parks the second document of a type as 'duplicate'. The
client-document-recovery producer publishes successive VERSIONS of one type (an expired
and a renewed passport, a draft and its signed final), so for its tag the wall compares
BYTES: a different version of the same type is kept for review, a byte-identical blob is
still parked. GUILT: the same bytes are still caught. INNOCENCE: a new version is not,
and every other tag, near-misses included, keeps the type-level rule.

The last tests pin the bypass's one dependency: a recovery-v1 row it admits (review_pending,
the state auto-attach reads) must stay out of every attach gate, switches armed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from backend.services.intake import auto_attach
from backend.services.intake import writer as intake_writer
from backend.services.intake.enqueue import enqueue
from backend.services.intake.routing import (
    VERSION_PRESERVING_PIPELINE_VERSIONS,
    _pipeline_version_preserves_versions,
    _pipeline_version_suppressed,
    client_already_has_blob,
    route_stage,
)

_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://nuzantara@localhost:5432/nuzantara_test",
)


# --------------------------------------------------------------------------- #
# The tag policy and the predicate's short-circuits, DB-free
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("recovery-v1", True),
        ("recovery-v1:cdr-batch-20261103", True),
        # innocence (guard family #3): exact-or-separator, never a bare prefix
        ("recovery-v10", False),
        ("recovery-v1-other", False),
        ("xrecovery-v1", False),
        ("RECOVERY-V1", False),
        ("intake-v1", False),
        ("v2.3-drive-autocreate", False),
        ("", False),
        (None, False),
    ],
)
def test_version_preserving_tag_policy(tag: str | None, expected: bool):
    assert _pipeline_version_preserves_versions(tag) is expected


class _FakeConn:
    def __init__(self, row: object) -> None:
        self.row, self.calls = row, []

    async def fetchrow(self, *args: Any) -> object:
        self.calls.append(args)
        return self.row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client_id", "doc_type", "blob_hash"),
    [
        (None, "passport", "a" * 64),
        (7, "", "a" * 64),
        (7, "unknown", "a" * 64),
        (7, "passport", None),
        (7, "passport", ""),
    ],
)
async def test_blob_predicate_short_circuits_without_querying(client_id, doc_type, blob_hash):
    conn = _FakeConn(row={"?column?": 1})
    assert await client_already_has_blob(conn, client_id, doc_type, blob_hash) is False
    assert conn.calls == []


@pytest.mark.asyncio
async def test_blob_predicate_binds_client_type_and_hash_in_order():
    conn = _FakeConn(row={"?column?": 1})
    assert await client_already_has_blob(conn, 7, "passport", "b" * 64) is True
    assert conn.calls[0][1:] == (7, "passport", "b" * 64)
    assert await client_already_has_blob(_FakeConn(row=None), 7, "passport", "b" * 64) is False


# --------------------------------------------------------------------------- #
# route_stage end to end, real SQL on the local nuzantara_test book
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(_DB_URL, min_size=2, max_size=8)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
def _wall_armed_attach_off(monkeypatch):
    """Wall-mechanics tests isolate the wall: attach is off so a row's fate is the wall's
    alone. The live worker's armed posture is exercised by the last tests of this file."""
    monkeypatch.setenv("INTAKE_DEDUP_WALL_ENABLED", "1")
    monkeypatch.delenv("INTAKE_AUTO_ATTACH_ENABLED", raising=False)
    monkeypatch.delenv("INTAKE_WRITER_ENABLED", raising=False)


@pytest_asyncio.fixture
async def client(pool: asyncpg.Pool, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A synthetic client the entity resolver matches on its passport number."""
    ctx: dict[str, Any] = {
        "name": f"cdr-dedup-{uuid.uuid4().hex[:8]}",
        "passport": "CDR" + uuid.uuid4().hex[:9].upper(),
        "queue_ids": [],
        "instance_ids": [],
        "dir": tmp_path,
    }
    async with pool.acquire() as conn:
        ctx["id"] = await conn.fetchval(
            "INSERT INTO clients (full_name, passport_number) VALUES ($1, $2) RETURNING id",
            ctx["name"],
            ctx["passport"],
        )
    try:
        yield ctx
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM intake_commit_audit WHERE client_id = $1", ctx["id"])
            await conn.execute("DELETE FROM documents WHERE client_id = $1", ctx["id"])
            await conn.execute(
                "DELETE FROM intake_queue WHERE id = ANY($1::bigint[])", ctx["queue_ids"]
            )
            await conn.execute(
                "DELETE FROM document_instances WHERE id = ANY($1::bigint[])", ctx["instance_ids"]
            )
            await conn.execute("DELETE FROM clients WHERE id = $1", ctx["id"])


def _pdf() -> bytes:
    return f"%PDF-1.4 {uuid.uuid4()}".encode()


async def _route_full(
    pool: asyncpg.Pool,
    client: dict[str, Any],
    tag: str,
    content: bytes,
    fields: dict[str, str] | None = None,
):
    """Enqueue ``content`` under ``tag`` (real sha256), run the real route stage.

    Returns (route_stage result, proposal status, commit_gate).
    """
    blob = client["dir"] / f"{uuid.uuid4().hex}.pdf"
    blob.write_bytes(content)
    queued = await enqueue(
        pool,
        source="drive",
        source_ref=f"cdr:{uuid.uuid4().hex[:12]}",
        blob_path=str(blob),
        pipeline_version=tag,
    )
    client["queue_ids"].append(queued.queue_id)
    client["instance_ids"].append(queued.instance_id)
    fields = fields or {"passport_no": client["passport"], "name": client["name"]}
    stage_output = {
        "classify": {"doc_type": "passport", "type_confidence": 0.9},
        "extract": {
            "doc_type": "passport",
            "fields": {
                k: {"value": v, "confidence": 0.85, "source_page": 1} for k, v in fields.items()
            },
        },
        "validate": {"valid": True, "rule_failures": []},
    }
    # Persisted like the worker does: the attach gates re-read the queue row's stage_output.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE intake_queue SET stage_output = $2::jsonb WHERE id = $1",
            queued.queue_id,
            json.dumps(stage_output),
        )
    result = await route_stage(
        {"id": queued.queue_id, "pipeline_version": tag, "stage_output": stage_output},
        "route",
        pool,
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, commit_gate FROM document_routing_proposal WHERE queue_id = $1",
            queued.queue_id,
        )
    gate = row["commit_gate"]
    return result, row["status"], json.loads(gate) if isinstance(gate, str) else gate


async def _route(pool: asyncpg.Pool, client: dict[str, Any], tag: str, content: bytes):
    result, status, gate = await _route_full(pool, client, tag, content)
    return result["proposal_id"], status, gate


async def _file_on_profile(
    pool: asyncpg.Pool, client: dict[str, Any], proposal_id: int, document_type: str = "passport"
) -> None:
    """The first document, committed by the intake writer from that proposal."""
    async with pool.acquire() as conn:
        await intake_writer.write_client_document(
            conn,
            client["id"],
            {
                "document_type": document_type,
                "document_category": "immigration",
                "file_name": "passport__abc123def456.pdf",
                "notes": "intake:cdr:abc123def456",
                "ocr_status": "completed",
            },
            proposal_id=proposal_id,
            idempotency_key=f"ik:{uuid.uuid4().hex}",
        )


RECOVERY_TAGS = ["recovery-v1", "recovery-v1:cdr-batch-20261103"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", RECOVERY_TAGS)
async def test_byte_identical_blob_is_still_parked_as_duplicate(pool, client, tag):
    blob = _pdf()
    proposal_id, status, _ = await _route(pool, client, tag, blob)
    assert status == "review_pending"  # nothing on the profile yet
    await _file_on_profile(pool, client, proposal_id)

    _, status, gate = await _route(pool, client, tag, blob)

    assert status == "duplicate"
    assert gate["dedup_already_on_profile"] is True
    assert gate["dedup_basis"] == "blob_hash"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", RECOVERY_TAGS)
async def test_a_different_version_of_the_same_type_is_kept_for_review(pool, client, tag):
    proposal_id, _, _ = await _route(pool, client, tag, _pdf())
    await _file_on_profile(pool, client, proposal_id)

    _, status, gate = await _route(pool, client, tag, _pdf())  # other bytes, same type

    assert status == "review_pending"
    assert gate["dedup_already_on_profile"] is False
    assert gate["dedup_basis"] == "blob_hash"  # the audit trail names the rule that applied


@pytest.mark.asyncio
async def test_a_profile_document_with_no_intake_link_cannot_park_a_recovery_row(pool, client):
    async with pool.acquire() as conn:  # a staff upload: same type, no proposal behind it
        await conn.execute(
            "INSERT INTO documents (client_id, document_type, file_name) "
            "VALUES ($1, 'passport', 'staff-upload.pdf')",
            client["id"],
        )

    _, status, gate = await _route(pool, client, "recovery-v1", _pdf())

    assert status == "review_pending"  # identity cannot be proven, so the row is kept
    assert gate["dedup_already_on_profile"] is False


@pytest.mark.asyncio
async def test_the_type_still_scopes_the_wall_for_identical_bytes(pool, client):
    """Narrower than the type-level wall, never wider: the same bytes filed under
    ANOTHER type on the profile do not park a passport row."""
    blob = _pdf()
    proposal_id, _, _ = await _route(pool, client, "recovery-v1", blob)
    await _file_on_profile(pool, client, proposal_id, document_type="visa")

    _, status, gate = await _route(pool, client, "recovery-v1", blob)

    assert status == "review_pending"
    assert gate["dedup_already_on_profile"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["intake-v1", "recovery-v10", "recovery-v1-other"])
async def test_every_other_tag_keeps_the_type_level_wall(pool, client, tag):
    proposal_id, _, _ = await _route(pool, client, tag, _pdf())
    await _file_on_profile(pool, client, proposal_id)

    _, status, gate = await _route(pool, client, tag, _pdf())  # other bytes, same type

    assert status == "duplicate"  # as before: the type alone parks it
    assert gate["dedup_already_on_profile"] is True
    assert "dedup_basis" not in gate  # the commit gate of an ordinary row is unchanged


@pytest.mark.asyncio
async def test_a_disarmed_wall_parks_nothing_even_for_identical_bytes(pool, client, monkeypatch):
    blob = _pdf()
    proposal_id, _, _ = await _route(pool, client, "recovery-v1", blob)
    await _file_on_profile(pool, client, proposal_id)
    monkeypatch.delenv("INTAKE_DEDUP_WALL_ENABLED")

    _, status, gate = await _route(pool, client, "recovery-v1", blob)

    assert status == "review_pending"  # the kill-switch still decides whether to park
    assert gate["dedup_already_on_profile"] is True  # the verdict is still recorded


# --------------------------------------------------------------------------- #
# The bypass depends on auto-attach suppression - pinned in the live worker's posture
# --------------------------------------------------------------------------- #
# The bypass admits a new version of a type already on the profile as review_pending, the one
# state auto-attach reads. recovery-v1 stays out of the attach gates only through
# AUTO_ATTACH_SUPPRESSED_PIPELINE_VERSIONS; the tests below fail if that coupling is lost.
ATTACH_SWITCHES = (
    "INTAKE_WRITER_ENABLED",
    "INTAKE_AUTO_ATTACH_ENABLED",
    "INTAKE_DIRECT_PHONE_AUTO_ATTACH_ENABLED",
    "INTAKE_NAMEID_AUTO_ATTACH_ENABLED",
)
ATTACH_GATES = ("try_auto_attach", "try_direct_phone_auto_attach", "try_nameid_auto_attach")


def test_every_version_preserving_tag_is_also_auto_attach_suppressed():
    for tag in VERSION_PRESERVING_PIPELINE_VERSIONS:
        assert _pipeline_version_suppressed(tag)
        assert _pipeline_version_suppressed(f"{tag}:batch")


def _arm_like_the_live_worker(monkeypatch) -> list[str]:
    """Writer and every attach switch on; returns the list the gate spies append to.

    The spies call through, so a gate that does run really can commit and the footprint
    check is not vacuous. CRM push stays off: a real commit never reaches Fly from a test.
    """
    for switch in ATTACH_SWITCHES:
        monkeypatch.setenv(switch, "true")
    monkeypatch.setenv("INTAKE_CRM_PUSH_ENABLED", "0")
    entered: list[str] = []
    for name in ATTACH_GATES:
        real = getattr(auto_attach, name)

        async def spy(*args, _name=name, _real=real, **kwargs):
            entered.append(_name)
            return await _real(*args, **kwargs)

        monkeypatch.setattr(auto_attach, name, spy)
    return entered


async def _footprint(pool: asyncpg.Pool, client_id: int) -> tuple[int, int]:
    """(documents, commit-audit rows) of the client: a real commit moves both."""
    async with pool.acquire() as conn:
        return (
            await conn.fetchval("SELECT count(*) FROM documents WHERE client_id = $1", client_id),
            await conn.fetchval(
                "SELECT count(*) FROM intake_commit_audit WHERE client_id = $1", client_id
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", RECOVERY_TAGS)
@pytest.mark.parametrize("decision", ["AUTO_ATTACH", "LINK_CANDIDATE"])
async def test_a_row_admitted_by_the_bypass_never_reaches_an_attach_gate(
    pool, client, monkeypatch, tag, decision
):
    first_proposal, _, _ = await _route(pool, client, tag, _pdf())
    await _file_on_profile(pool, client, first_proposal)
    before = await _footprint(pool, client["id"])
    entered = _arm_like_the_live_worker(monkeypatch)

    # AUTO_ATTACH: strong id + name. LINK_CANDIDATE: name only, one typo (the direct-phone path).
    fields = None if decision == "AUTO_ATTACH" else {"name": client["name"][:-1]}
    result, status, gate = await _route_full(pool, client, tag, _pdf(), fields)

    assert result["decision"] == decision  # the row takes the path this case is about
    assert gate["dedup_already_on_profile"] is False  # admitted by the bypass, not parked
    assert entered == []  # no attach gate was consulted
    assert result["auto_attach"]["skipped"] == "suppressed_pipeline_version"
    assert status == "review_pending"
    assert await _footprint(pool, client["id"]) == before  # and nothing was committed


@pytest.mark.asyncio
async def test_control_an_unsuppressed_tag_in_the_same_posture_does_reach_the_gates(
    pool, client, monkeypatch
):
    """Innocence for the test above: its spies and footprint do see a gate run and commit."""
    before = await _footprint(pool, client["id"])
    entered = _arm_like_the_live_worker(monkeypatch)

    result, _, _ = await _route_full(pool, client, "intake-v1", _pdf())

    assert entered, "no gate ran"
    assert result["routed"] is True, (entered, result["auto_attach"])
    assert await _footprint(pool, client["id"]) == (before[0] + 1, before[1] + 1)
