"""Unit & security tests for Tube 3 controlled batch triage & 4-lock apply gate."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from backend.services.intake import batch_triage

DSN = os.environ.get("INTAKE_TEST_DSN", "postgresql://nuzantara@localhost:5432/nuzantara_test")


async def _dsn_reachable() -> bool:
    try:
        conn = await asyncpg.connect(DSN)
        await conn.close()
        return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def pg_pool() -> AsyncIterator[asyncpg.Pool]:
    if not await _dsn_reachable():
        pytest.skip(f"local intake DB not reachable at {DSN}")
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _synthetic_row(
    pid: int,
    *,
    pipeline_version: str = "intake-v2",
    decision: str = "AUTO_ATTACH",
    client_id: int | None = 100,
    company_id: int | None = None,
    doc_type: str = "passport",
    id_verified: bool | None = True,
    ocr_text: str = "SYNTHETIC DOC SAMPLE TEXT LONG ENOUGH FOR OCR",
    file_name: str = "doc.pdf",
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": pid,
        "queue_id": pid * 10,
        "doc_index": 0,
        "pipeline_version": pipeline_version,
        "queue_pipeline_version": pipeline_version,
        "status": "review_pending",
        "entity_resolution": {
            "decision": decision,
            "candidates": [{"id": client_id or company_id or 1, "id_verified": id_verified}],
        },
        "routing": {
            "client_id": client_id,
            "company_id": company_id,
            "doc_type": doc_type,
            "fields": fields or {},
        },
        "commit_gate": {"auto_attach_eligible": decision == "AUTO_ATTACH"},
        "stage_output": {
            "file_name": file_name,
            "ocr": {"text": ocr_text},
            "extract": {"fields": fields or {}},
        },
    }


@pytest.mark.asyncio
async def test_r3_6_and_gate_11_enforce_giallo_hold() -> None:
    item_r36, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(1, pipeline_version="v2.3-drive-autocreate", decision="AUTO_ATTACH")
    )
    assert item_r36.bucket == batch_triage.GIALLO
    assert item_r36.reason_code == "r3_6_suppressed_pipeline_version"

    item_rec, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(2, pipeline_version="recovery-v1", decision="AUTO_ATTACH")
    )
    assert item_rec.bucket == batch_triage.GIALLO
    assert item_rec.reason_code == "r3_6_suppressed_pipeline_version"

    item_r36_colon, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(
            21,
            pipeline_version="v2.3-drive-autocreate:d16f4831",
            decision="AUTO_ATTACH",
        )
    )
    assert item_r36_colon.bucket == batch_triage.GIALLO
    assert item_r36_colon.reason_code == "r3_6_suppressed_pipeline_version"

    item_rec_colon, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(22, pipeline_version="recovery-v1:batch42", decision="AUTO_ATTACH")
    )
    assert item_rec_colon.bucket == batch_triage.GIALLO
    assert item_rec_colon.reason_code == "r3_6_suppressed_pipeline_version"

    item_nonsuppressed, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(23, pipeline_version="v2.3-drive-autocreate-other", decision="AUTO_ATTACH")
    )
    assert item_nonsuppressed.bucket == batch_triage.VERDE

    item_g11, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(3, decision="AUTO_ATTACH", id_verified=False)
    )
    assert item_g11.bucket == batch_triage.GIALLO
    assert item_g11.reason_code == "gate_11_id_unverified"


@pytest.mark.asyncio
async def test_live_classify_per_page_ocr_and_source_path_reclassify() -> None:
    row = {
        "id": 99,
        "queue_id": 990,
        "doc_index": 0,
        "pipeline_version": "intake-v1",
        "queue_pipeline_version": "intake-v1",
        "source_path": "/drive/folder/NIB_Contoh.pdf",
        "blob_path": "/blobs/00" + "a" * 62 + ".pdf",
        "status": "review_pending",
        "entity_resolution": {"decision": "NO_MATCH", "candidates": []},
        "routing": {"client_id": None, "company_id": None, "doc_type": "unknown"},
        "stage_output": {
            "classify": {
                "doc_type": "unknown",
                "ocr_text_per_page": [{"page": 1, "text": "NOMOR INDUK BERUSAHA OSS RBA"}],
            }
        },
    }
    item, _ = await batch_triage.classify_proposal_row(row)
    assert item.canonical_doc_type == "nib"
    assert item.classification_via == "deterministic"
    assert item.bucket == batch_triage.GIALLO


@pytest.mark.asyncio
async def test_triage_buckets_read_only_and_strict_pii_firewall() -> None:
    canary_subject = "CANARY_TEST_ENTITY_XYZ"
    canary_ref = "REF_TOKEN_CANARY_777"
    canary_nib = "000000" + "0000123"
    nib_hash = hashlib.sha256(canary_nib.encode("utf-8")).hexdigest()

    fake_rows = [
        # 1 -> VERDE client
        _synthetic_row(10, decision="AUTO_ATTACH", client_id=11, doc_type="passport"),
        # 2 -> GIALLO standalone company (HITL-held, counted as company_attach_candidate)
        _synthetic_row(20, decision="AUTO_ATTACH", client_id=None, company_id=22, doc_type="nib"),
        # 3 -> GIALLO unknown doctype reclassified to nib via deterministic + NO_MATCH hit against PROD hash
        _synthetic_row(
            30,
            decision="NO_MATCH",
            client_id=None,
            company_id=None,
            doc_type="unknown",
            file_name=f"{canary_subject}_nib.pdf",
            ocr_text=f"NOMOR INDUK BERUSAHA OSS RBA {canary_subject} {canary_ref}",
            fields={"nib_number": canary_nib, "contact": canary_ref},
        ),
        # 4 -> ROSSO low-signal unreadable garbage
        _synthetic_row(
            40,
            decision="NO_MATCH",
            client_id=None,
            company_id=None,
            doc_type="unknown",
            ocr_text="blur",
            file_name="scan001.jpg",
            fields={},
        ),
    ]

    conn = AsyncMock()
    tx_cm = AsyncMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: tx_cm
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[fake_rows, []])

    summary = await batch_triage.run_dry_run_triage(
        conn,
        batch_size=100,
        prod_company_hashes={nib_hash},
    )

    assert conn.execute.await_args_list[0].args[0] == "SET LOCAL TRANSACTION READ ONLY"
    assert summary.scanned == 4
    assert summary.counts_by_bucket == {"VERDE": 1, "GIALLO": 2, "ROSSO": 1}
    assert summary.verde_proposal_ids == [10]
    assert summary.company_attach_candidates == 1
    assert summary.reclassified_via_deterministic == 1
    assert summary.prod_company_snapshot_hits == 1
    assert len(summary.plan_sha256) == 64

    serialized = json.dumps(summary.to_public_dict())
    assert canary_subject not in serialized
    assert canary_ref not in serialized
    assert canary_nib not in serialized


@pytest.mark.asyncio
async def test_apply_triage_plan_enforces_all_four_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = AsyncMock()

    with pytest.raises(PermissionError, match="LOCK 1"):
        await batch_triage.apply_triage_plan(pool, apply=False)

    monkeypatch.delenv("INTAKE_BATCH_APPLY_ENABLED", raising=False)
    with pytest.raises(PermissionError, match="LOCK 2"):
        await batch_triage.apply_triage_plan(pool, apply=True)

    monkeypatch.setenv("INTAKE_BATCH_APPLY_ENABLED", "true")
    with pytest.raises(ValueError, match="LOCK 3"):
        await batch_triage.apply_triage_plan(pool, apply=True, max_apply=501)

    with pytest.raises(ValueError, match="LOCK 4"):
        await batch_triage.apply_triage_plan(
            pool, apply=True, max_apply=10, confirm_plan_sha256=None
        )


@pytest.mark.asyncio
async def test_verde_boundary_excludes_reclassified_company_linked_and_normalizes_decision() -> None:
    reclassified_item, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(
            101,
            decision="AUTO_ATTACH",
            client_id=11,
            doc_type="unknown",
            file_name="NIB_Contoh.pdf",
            ocr_text="NOMOR INDUK BERUSAHA OSS RBA",
        )
    )
    assert reclassified_item.bucket == batch_triage.GIALLO
    assert reclassified_item.reason_code == "auto_attach_reclassified_needs_review"
    assert reclassified_item.classification_via == "deterministic"

    linked_item, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(102, decision="AUTO_ATTACH", client_id=11, company_id=22, doc_type="nib")
    )
    assert linked_item.bucket == batch_triage.GIALLO
    assert linked_item.target_kind == "linked"
    assert linked_item.reason_code == "auto_attach_linked_needs_review"

    company_item, _ = await batch_triage.classify_proposal_row(
        _synthetic_row(103, decision="AUTO_ATTACH", client_id=None, company_id=22, doc_type="nib")
    )
    assert company_item.bucket == batch_triage.GIALLO
    assert company_item.reason_code == "auto_attach_company_needs_review"
    assert company_item.company_attach_candidate is True

    canary_decision = "CANARY_DECISION_LEAK_XYZ"
    row_malformed = _synthetic_row(104, decision=canary_decision, client_id=None, doc_type="passport")
    conn = AsyncMock()
    tx_cm = AsyncMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = lambda: tx_cm
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(side_effect=[[row_malformed], []])

    summary = await batch_triage.run_dry_run_triage(conn)
    assert summary.counts_by_decision == {"UNKNOWN": 1}
    assert canary_decision not in json.dumps(summary.to_public_dict())


def test_plan_sha256_binds_targets_via_and_fingerprint_without_exposing_pii() -> None:
    base = batch_triage.TriageItem(
        proposal_id=10,
        bucket=batch_triage.VERDE,
        reason_code="auto_attach_client_ready",
        decision="AUTO_ATTACH",
        raw_doc_type="passport",
        canonical_doc_type="passport",
        target_kind="client",
        classification_via="existing",
        client_id=11,
        company_id=None,
        practice_id=None,
        row_fingerprint="f" * 64,
    )
    summary_dict = batch_triage.TriageSummary(verde_proposal_ids=[10]).to_public_dict()
    for internal_key in ("client_id", "company_id", "practice_id", "row_fingerprint", "verde_proposal_ids"):
        assert internal_key not in summary_dict

    base_hash = batch_triage.compute_plan_sha256([base])
    for mutated_kwargs in (
        {"client_id": 99},
        {"company_id": 22},
        {"practice_id": 33},
        {"canonical_doc_type": "nib"},
        {"classification_via": "deterministic"},
        {"row_fingerprint": "e" * 64},
    ):
        mutated = batch_triage.TriageItem(**{**base.__dict__, **mutated_kwargs})
        assert batch_triage.compute_plan_sha256([mutated]) != base_hash


async def _seed_triage_proposal(
    conn: asyncpg.Connection,
    *,
    tag: str,
    decision: str,
    client_id: int | None,
    doc_type: str,
    sender_phone: str | None,
    passport_number: str | None,
    id_verified: bool | None = True,
) -> tuple[int, int, int]:
    bh = (uuid.uuid4().hex + uuid.uuid4().hex)[:64]
    inst_id = await conn.fetchval(
        """
        INSERT INTO document_instances (blob_hash, pipeline_version, blob_path, first_source)
        VALUES ($1, 'intake-v2', $2, 'whatsapp')
        RETURNING id
        """,
        bh,
        f"/tmp/{tag}-{uuid.uuid4().hex[:6]}.pdf",
    )
    source_ref = f"{tag}-{uuid.uuid4().hex[:8]}"
    qid = await conn.fetchval(
        """
        INSERT INTO intake_queue
            (instance_id, source, source_ref, blob_path, blob_hash, pipeline_version,
             status, stage_output, intake_key, sender_phone)
        VALUES ($1, 'whatsapp', $2, $3, $4, 'intake-v2', 'review_pending', $5::jsonb, $6, $7)
        RETURNING id
        """,
        inst_id,
        source_ref,
        f"/tmp/{source_ref}.pdf",
        bh,
        json.dumps(
            {
                "doc_type": doc_type,
                "file_id": f"gd-{source_ref}",
                "file_name": f"{doc_type}_{source_ref}.pdf",
                "ocr": {"pages": [{"page": 1, "text": "PASSPORT REPUBLIC OF INDONESIA"}]},
            }
        ),
        f"wa|{source_ref}|{bh}|intake-v2|{uuid.uuid4().hex[:6]}",
        sender_phone,
    )
    cands = []
    if client_id is not None and passport_number:
        cands.append(
            {
                "table": "clients",
                "id": client_id,
                "method": "passport_number",
                "score": 0.99,
                "matched_value": passport_number,
                "id_verified": id_verified,
            }
        )
    pid = await conn.fetchval(
        """
        INSERT INTO document_routing_proposal
            (queue_id, doc_index, pipeline_version, routing_key,
             entity_resolution, routing, commit_gate, status)
        VALUES ($1, 0, 'intake-v2', $2, $3::jsonb, $4::jsonb, $5::jsonb, 'review_pending')
        RETURNING id
        """,
        qid,
        f"{tag}-{uuid.uuid4().hex[:8]}",
        json.dumps({"decision": decision, "candidates": cands}),
        json.dumps({"client_id": client_id, "company_id": None, "doc_type": doc_type, "fields": {}}),
        json.dumps({"auto_attach_eligible": decision == "AUTO_ATTACH", "decision": decision}),
    )
    return int(pid), int(qid), int(inst_id)


@pytest.mark.asyncio
async def test_pg_dry_run_triage_enforces_readonly_tx_and_sanitizes_decision(
    pg_pool: asyncpg.Pool,
) -> None:
    tag = f"t3ro-{uuid.uuid4().hex[:8]}"
    created: dict[str, list[int]] = {"proposals": [], "queues": [], "instances": []}
    canary_dec = "CANARY_DB_DECISION_LEAK"

    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL TRANSACTION READ ONLY")
            with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                await conn.execute("INSERT INTO clients (full_name) VALUES ($1)", f"{tag}-blocked")

        pid, qid, iid = await _seed_triage_proposal(
            conn,
            tag=tag,
            decision=canary_dec,
            client_id=None,
            doc_type="passport",
            sender_phone=None,
            passport_number=None,
        )
        created["proposals"].append(pid)
        created["queues"].append(qid)
        created["instances"].append(iid)

    try:
        summary = await batch_triage.run_dry_run_triage(pg_pool)
        assert summary.read_only_enforced is True
        assert summary.counts_by_decision.get("UNKNOWN", 0) >= 1
        assert canary_dec not in json.dumps(summary.to_public_dict())
    finally:
        async with pg_pool.acquire() as conn:
            for pid in created["proposals"]:
                await conn.execute("DELETE FROM document_routing_proposal WHERE id = $1", pid)
            for qid in created["queues"]:
                await conn.execute("DELETE FROM intake_queue WHERE id = $1", qid)
            for iid in created["instances"]:
                await conn.execute("DELETE FROM document_instances WHERE id = $1", iid)


@pytest.mark.asyncio
async def test_pg_apply_triage_plan_commits_verde_and_skips_locked_or_nonconcordant(
    pg_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag = f"t3apply-{uuid.uuid4().hex[:8]}"
    created: dict[str, list[int]] = {
        "clients": [],
        "proposals": [],
        "queues": [],
        "instances": [],
    }
    phone_ok = "6281" + str(uuid.uuid4().int % 10**8).zfill(8)
    pass_ok = "T" + uuid.uuid4().hex[:7].upper()
    pass_bad = "T" + uuid.uuid4().hex[:7].upper()
    pass_lock = "T" + uuid.uuid4().hex[:7].upper()
    phone_lock = "6282" + str(uuid.uuid4().int % 10**8).zfill(8)
    pass_qlock = "T" + uuid.uuid4().hex[:7].upper()
    phone_qlock = "6283" + str(uuid.uuid4().int % 10**8).zfill(8)

    async with pg_pool.acquire() as conn:
        cid_ok = int(
            await conn.fetchval(
                "INSERT INTO clients (full_name, phone_normalized, passport_number) "
                "VALUES ($1, $2, $3) RETURNING id",
                f"{tag}-ok",
                f"+{phone_ok}",
                pass_ok,
            )
        )
        cid_bad = int(
            await conn.fetchval(
                "INSERT INTO clients (full_name, passport_number) VALUES ($1, $2) RETURNING id",
                f"{tag}-bad",
                pass_bad,
            )
        )
        cid_lock = int(
            await conn.fetchval(
                "INSERT INTO clients (full_name, phone_normalized, passport_number) "
                "VALUES ($1, $2, $3) RETURNING id",
                f"{tag}-lock",
                f"+{phone_lock}",
                pass_lock,
            )
        )
        cid_qlock = int(
            await conn.fetchval(
                "INSERT INTO clients (full_name, phone_normalized, passport_number) "
                "VALUES ($1, $2, $3) RETURNING id",
                f"{tag}-qlock",
                f"+{phone_qlock}",
                pass_qlock,
            )
        )
        created["clients"].extend([cid_ok, cid_bad, cid_lock, cid_qlock])

        pid_ok, q_ok, i_ok = await _seed_triage_proposal(
            conn,
            tag=f"{tag}-ok",
            decision="AUTO_ATTACH",
            client_id=cid_ok,
            doc_type="passport",
            sender_phone=phone_ok,
            passport_number=pass_ok,
        )
        pid_bad, q_bad, i_bad = await _seed_triage_proposal(
            conn,
            tag=f"{tag}-bad",
            decision="AUTO_ATTACH",
            client_id=cid_bad,
            doc_type="passport",
            sender_phone=None,
            passport_number=pass_bad,
        )
        pid_lock, q_lock, i_lock = await _seed_triage_proposal(
            conn,
            tag=f"{tag}-lock",
            decision="AUTO_ATTACH",
            client_id=cid_lock,
            doc_type="passport",
            sender_phone=phone_lock,
            passport_number=pass_lock,
        )
        pid_qlock, q_qlock, i_qlock = await _seed_triage_proposal(
            conn,
            tag=f"{tag}-qlock",
            decision="AUTO_ATTACH",
            client_id=cid_qlock,
            doc_type="passport",
            sender_phone=phone_qlock,
            passport_number=pass_qlock,
        )
        created["proposals"].extend([pid_ok, pid_bad, pid_lock, pid_qlock])
        created["queues"].extend([q_ok, q_bad, q_lock, q_qlock])
        created["instances"].extend([i_ok, i_bad, i_lock, i_qlock])

    monkeypatch.setenv("INTAKE_BATCH_APPLY_ENABLED", "true")
    monkeypatch.setenv("INTAKE_AUTO_ATTACH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")

    lock_conn: asyncpg.Connection | None = None
    lock_tx: Any = None
    try:
        summary = await batch_triage.run_dry_run_triage(pg_pool)
        assert {pid_ok, pid_bad, pid_lock, pid_qlock}.issubset(
            set(summary.verde_proposal_ids)
        )

        with pytest.raises(ValueError, match="plan_sha256 mismatch"):
            await batch_triage.apply_triage_plan(
                pg_pool,
                apply=True,
                max_apply=10,
                confirm_plan_sha256="0" * 64,
            )

        # Hold FOR UPDATE on pid_lock and on intake_queue q_qlock so FOR UPDATE OF p, q SKIP LOCKED skips both.
        lock_conn = await asyncpg.connect(DSN)
        lock_tx = lock_conn.transaction()
        await lock_tx.start()
        await lock_conn.fetchrow(
            "SELECT id FROM document_routing_proposal WHERE id = $1 FOR UPDATE",
            pid_lock,
        )
        await lock_conn.fetchrow(
            "SELECT id FROM intake_queue WHERE id = $1 FOR UPDATE",
            q_qlock,
        )

        deliver_mock = AsyncMock(
            return_value={
                "status": "pushed",
                "fly_doc_id": 9901,
                "file_url": "https://drive.example/t3-ok",
            }
        )
        with patch(
            "backend.services.intake.crm_delivery.deliver_committed_to_crm",
            deliver_mock,
        ):
            res = await batch_triage.apply_triage_plan(
                pg_pool,
                apply=True,
                max_apply=10,
                confirm_plan_sha256=summary.plan_sha256,
                committed_by="system:batch-triage-test",
            )

        assert res["committed_ids"] == [pid_ok]
        assert res["delivered_ids"] == [pid_ok]
        assert res["committed_count"] == 1
        assert res["delivered_count"] == 1
        assert res["skipped_count"] >= 3
        assert deliver_mock.await_count == 1

        async with pg_pool.acquire() as conn:
            status_ok = await conn.fetchval(
                "SELECT status FROM document_routing_proposal WHERE id = $1", pid_ok
            )
            doc_cnt_ok = await conn.fetchval(
                "SELECT count(*) FROM documents WHERE client_id = $1", cid_ok
            )
            status_bad = await conn.fetchval(
                "SELECT status FROM document_routing_proposal WHERE id = $1", pid_bad
            )
            status_qlock = await conn.fetchval(
                "SELECT status FROM document_routing_proposal WHERE id = $1", pid_qlock
            )
            assert status_ok == "auto_routed"
            assert doc_cnt_ok == 1
            assert status_bad == "review_pending"
            assert status_qlock == "review_pending"
    finally:
        if lock_tx is not None:
            try:
                await lock_tx.rollback()
            except Exception:
                pass
        if lock_conn is not None:
            await lock_conn.close()
        async with pg_pool.acquire() as conn:
            for cid in created["clients"]:
                await conn.execute("DELETE FROM documents WHERE client_id = $1", cid)
                await conn.execute("DELETE FROM intake_commit_audit WHERE client_id = $1", cid)
            for pid in created["proposals"]:
                await conn.execute("DELETE FROM intake_commit_audit WHERE proposal_id = $1", pid)
                await conn.execute("DELETE FROM document_routing_proposal WHERE id = $1", pid)
            for qid in created["queues"]:
                await conn.execute("DELETE FROM intake_queue WHERE id = $1", qid)
            for iid in created["instances"]:
                await conn.execute("DELETE FROM document_instances WHERE id = $1", iid)
            for cid in created["clients"]:
                await conn.execute("DELETE FROM clients WHERE id = $1", cid)


@pytest.mark.asyncio
async def test_row_fingerprint_and_plan_sha256_detect_queue_blob_and_field_mutations() -> None:
    """Any commit-bearing mutation must perturb row_fingerprint and plan_sha256."""
    base_row = _synthetic_row(
        500,
        decision="AUTO_ATTACH",
        client_id=11,
        doc_type="passport",
        fields={"passport_number": "SYNTH1"},
    )
    base_row["source"] = "whatsapp"
    base_row["source_ref"] = "msg-500"
    base_row["blob_hash"] = "a" * 64
    base_row["source_path"] = "/drive/pass.pdf"
    base_row["blob_path"] = "/blobs/500.pdf"
    base_row["sender_phone"] = "SYNTH_PHONE_A"

    base_item, _ = await batch_triage.classify_proposal_row(base_row)
    assert base_item.bucket == batch_triage.VERDE
    assert len(base_item.row_fingerprint) == 64
    base_plan_hash = batch_triage.compute_plan_sha256([base_item])

    mutations: list[tuple[str, dict[str, Any]]] = [
        ("queue_id", {**base_row, "queue_id": 9999}),
        ("doc_index", {**base_row, "doc_index": 2}),
        ("source", {**base_row, "source": "drive"}),
        ("source_ref", {**base_row, "source_ref": "msg-999"}),
        ("blob_hash", {**base_row, "blob_hash": "b" * 64}),
        ("source_path", {**base_row, "source_path": "/drive/other.pdf"}),
        ("blob_path", {**base_row, "blob_path": "/blobs/other.pdf"}),
        ("sender_phone", {**base_row, "sender_phone": "SYNTH_PHONE_B"}),
        (
            "routing.fields",
            {
                **base_row,
                "routing": {
                    **base_row["routing"],
                    "fields": {"passport_number": "SYNTH2"},
                },
            },
        ),
        (
            "stage_output.file_name",
            {
                **base_row,
                "stage_output": {
                    **base_row["stage_output"],
                    "file_name": "renamed_passport.pdf",
                },
            },
        ),
        (
            "entity_resolution.candidates",
            {
                **base_row,
                "entity_resolution": {
                    **base_row["entity_resolution"],
                    "candidates": [{"id": 11, "id_verified": True, "score": 0.77}],
                },
            },
        ),
        (
            "commit_gate",
            {
                **base_row,
                "commit_gate": {**base_row["commit_gate"], "note": "changed"},
            },
        ),
    ]

    for label, mutated_row in mutations:
        mutated_item, _ = await batch_triage.classify_proposal_row(mutated_row)
        assert mutated_item.bucket == batch_triage.VERDE, label
        assert mutated_item.row_fingerprint != base_item.row_fingerprint, label
        assert batch_triage.compute_plan_sha256([mutated_item]) != base_plan_hash, label


@pytest.mark.asyncio
async def test_apply_triage_plan_skips_when_locked_row_mutated_before_for_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-flight mutations between plan scan and FOR UPDATE SKIP LOCKED must skip commit."""
    monkeypatch.setenv("INTAKE_BATCH_APPLY_ENABLED", "true")
    monkeypatch.setenv("INTAKE_AUTO_ATTACH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")

    scanned_row = _synthetic_row(
        600,
        decision="AUTO_ATTACH",
        client_id=11,
        doc_type="passport",
        fields={"passport_number": "SYNTH-ORIG"},
    )
    scanned_row["blob_path"] = "/blobs/orig.pdf"
    scanned_row["sender_phone"] = "SYNTH_PHONE_A"
    scanned_item, _ = await batch_triage.classify_proposal_row(scanned_row)
    assert scanned_item.bucket == batch_triage.VERDE

    mutated_locked_row = _synthetic_row(
        600,
        decision="AUTO_ATTACH",
        client_id=11,
        doc_type="passport",
        fields={"passport_number": "SYNTH-MUTATED"},
    )
    mutated_locked_row["blob_path"] = "/blobs/mutated.pdf"
    mutated_locked_row["sender_phone"] = "SYNTH_PHONE_A"

    summary = batch_triage.TriageSummary(
        scanned=1,
        counts_by_bucket={batch_triage.VERDE: 1, batch_triage.GIALLO: 0, batch_triage.ROSSO: 0},
        verde_proposal_ids=[600],
        plan_sha256=batch_triage.compute_plan_sha256([scanned_item]),
    )

    from unittest.mock import MagicMock

    conn = AsyncMock()
    tx_cm = AsyncMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_cm)
    conn.fetchrow = AsyncMock(return_value=mutated_locked_row)

    acq_cm = AsyncMock()
    acq_cm.__aenter__ = AsyncMock(return_value=conn)
    acq_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq_cm)

    with (
        patch.object(
            batch_triage,
            "_run_readonly_scan_with_items",
            new=AsyncMock(return_value=(summary, [scanned_item])),
        ),
        patch("backend.services.intake.writer.plan_commit", new=AsyncMock()) as plan_commit_mock,
    ):
        res = await batch_triage.apply_triage_plan(
            pool,
            apply=True,
            max_apply=10,
            confirm_plan_sha256=summary.plan_sha256,
        )

    assert res["committed_count"] == 0
    assert res["skipped_count"] == 1
    plan_commit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_pg_apply_triage_plan_rejects_after_blob_path_or_fields_mutation(
    pg_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-PG mutating intake_queue.blob_path or routing.fields must invalidate plan_sha256."""
    tag = f"t3mut-{uuid.uuid4().hex[:8]}"
    created: dict[str, list[int]] = {
        "clients": [],
        "proposals": [],
        "queues": [],
        "instances": [],
    }
    phone_val = "6281" + str(uuid.uuid4().int % 10**8).zfill(8)
    pass_val = "M" + uuid.uuid4().hex[:7].upper()

    async with pg_pool.acquire() as conn:
        cid = int(
            await conn.fetchval(
                "INSERT INTO clients (full_name, phone_normalized, passport_number) "
                "VALUES ($1, $2, $3) RETURNING id",
                f"{tag}-client",
                f"+{phone_val}",
                pass_val,
            )
        )
        created["clients"].append(cid)
        pid, qid, iid = await _seed_triage_proposal(
            conn,
            tag=tag,
            decision="AUTO_ATTACH",
            client_id=cid,
            doc_type="passport",
            sender_phone=phone_val,
            passport_number=pass_val,
        )
        created["proposals"].append(pid)
        created["queues"].append(qid)
        created["instances"].append(iid)

    monkeypatch.setenv("INTAKE_BATCH_APPLY_ENABLED", "true")
    monkeypatch.setenv("INTAKE_AUTO_ATTACH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")

    try:
        s1 = await batch_triage.run_dry_run_triage(pg_pool)
        assert pid in s1.verde_proposal_ids

        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE intake_queue SET blob_path = $1 WHERE id = $2",
                f"/tmp/{tag}-mutated.pdf",
                qid,
            )
        with pytest.raises(ValueError, match="plan_sha256 mismatch"):
            await batch_triage.apply_triage_plan(
                pg_pool,
                apply=True,
                max_apply=10,
                confirm_plan_sha256=s1.plan_sha256,
            )

        s2 = await batch_triage.run_dry_run_triage(pg_pool)
        assert s2.plan_sha256 != s1.plan_sha256

        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE document_routing_proposal "
                "SET routing = jsonb_set(routing, '{fields}', $1::jsonb) "
                "WHERE id = $2",
                json.dumps({"passport_number": f"{pass_val}-NEW"}),
                pid,
            )
        with pytest.raises(ValueError, match="plan_sha256 mismatch"):
            await batch_triage.apply_triage_plan(
                pg_pool,
                apply=True,
                max_apply=10,
                confirm_plan_sha256=s2.plan_sha256,
            )
    finally:
        async with pg_pool.acquire() as conn:
            for pid_d in created["proposals"]:
                await conn.execute("DELETE FROM intake_commit_audit WHERE proposal_id = $1", pid_d)
                await conn.execute("DELETE FROM document_routing_proposal WHERE id = $1", pid_d)
            for qid_d in created["queues"]:
                await conn.execute("DELETE FROM intake_queue WHERE id = $1", qid_d)
            for iid_d in created["instances"]:
                await conn.execute("DELETE FROM document_instances WHERE id = $1", iid_d)
            for cid_d in created["clients"]:
                await conn.execute("DELETE FROM clients WHERE id = $1", cid_d)


@pytest.mark.asyncio
async def test_apply_triage_plan_locks_both_p_and_q_and_never_rereads_unlocked_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """apply_triage_plan must lock p and q together and pass locked_queue to plan_commit."""
    from unittest.mock import MagicMock

    from backend.services.intake import auto_attach, crm_delivery, writer

    monkeypatch.setenv("INTAKE_BATCH_APPLY_ENABLED", "true")
    monkeypatch.setenv("INTAKE_AUTO_ATTACH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_WRITER_ENABLED", "true")
    monkeypatch.setenv("INTAKE_CRM_PUSH_ENABLED", "true")
    monkeypatch.setenv("INTAKE_CRM_PUSH_WRITE_KEY", "stub-key")

    approved_blob_hash = "c" * 64
    approved_blob_path = "/tmp/approved_doc.pdf"
    locked_row = _synthetic_row(
        700,
        decision="AUTO_ATTACH",
        client_id=11,
        doc_type="passport",
        fields={"passport_number": "SYNTH-LOCKED"},
    )
    locked_row["source"] = "whatsapp"
    locked_row["source_ref"] = "wa-approved-700"
    locked_row["blob_hash"] = approved_blob_hash
    locked_row["blob_path"] = approved_blob_path
    locked_row["sender_phone"] = "SYNTH_PHONE_LOCKED"

    scanned_item, _ = await batch_triage.classify_proposal_row(locked_row)
    summary = batch_triage.TriageSummary(
        scanned=1,
        counts_by_bucket={batch_triage.VERDE: 1, batch_triage.GIALLO: 0, batch_triage.ROSSO: 0},
        verde_proposal_ids=[700],
        plan_sha256=batch_triage.compute_plan_sha256([scanned_item]),
    )

    seen_lock_sql: list[str] = []

    async def _fetchrow(sql: str, *args: Any) -> dict[str, Any] | None:
        if "FOR UPDATE OF p, q SKIP LOCKED" in sql:
            seen_lock_sql.append(sql)
            return dict(locked_row)
        if "FROM intake_queue WHERE id = $1" in sql:
            raise AssertionError("unlocked intake_queue re-read must not happen inside plan_commit")
        if "FROM clients WHERE id = $1" in sql:
            return {"id": 11, "deleted_at": None}
        if "FROM documents" in sql or "FROM family_members" in sql or "FROM companies" in sql:
            return None
        return None

    conn = AsyncMock()
    tx_cm = AsyncMock()
    tx_cm.__aenter__ = AsyncMock(return_value=None)
    tx_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_cm)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetchval = AsyncMock(return_value=None)

    acq_cm = AsyncMock()
    acq_cm.__aenter__ = AsyncMock(return_value=conn)
    acq_cm.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq_cm)

    captured_plans: list[writer.CommitPlan] = []

    async def _fake_execute_commit(
        plan: writer.CommitPlan,
        conn_arg: Any,
        **kwargs: Any,
    ) -> writer.CommitResult:
        captured_plans.append(plan)
        return writer.CommitResult(
            proposal_id=plan.proposal_id,
            dry_run=False,
            outcome="committed",
            doc_id=501,
            practice_id=None,
            audit_id=601,
        )

    deliver_mock = AsyncMock(return_value={"status": "pushed", "fly_doc_id": 801})

    with (
        patch.object(
            batch_triage,
            "_run_readonly_scan_with_items",
            new=AsyncMock(return_value=(summary, [scanned_item])),
        ),
        patch.object(
            auto_attach,
            "evaluate_concordance",
            new=AsyncMock(return_value={"concordant": True}),
        ),
        patch.object(
            auto_attach,
            "_strong_id_still_owned",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(writer, "execute_commit", side_effect=_fake_execute_commit),
        patch.object(crm_delivery, "deliver_committed_to_crm", deliver_mock),
    ):
        res = await batch_triage.apply_triage_plan(
            pool,
            apply=True,
            max_apply=10,
            confirm_plan_sha256=summary.plan_sha256,
        )

    assert len(seen_lock_sql) == 1
    assert "JOIN intake_queue q ON q.id = p.queue_id" in seen_lock_sql[0]
    assert "LEFT JOIN" not in seen_lock_sql[0]
    assert res["committed_count"] == 1
    assert captured_plans[0].blob_hash == approved_blob_hash
    assert deliver_mock.await_args.kwargs["expected_blob_path"] == approved_blob_path
    assert deliver_mock.await_args.kwargs["expected_blob_hash"] == approved_blob_hash

    # Verify deliver_committed_to_crm rejects swapped queue blob_path or diverged file bytes.
    tmp_blob = tmp_path / f"intake_delivery_check_{uuid.uuid4().hex[:8]}.pdf"
    tmp_blob.write_bytes(b"tampered-bytes")
    try:
        dlv_conn = AsyncMock()
        dlv_conn.execute = AsyncMock()
        dlv_conn.fetchrow = AsyncMock(
            side_effect=[
                {"blob_path": "/tmp/other_swapped.pdf", "blob_hash": approved_blob_hash, "mime_type": "application/pdf"},
                None,
            ]
        )
        dlv_acq = AsyncMock()
        dlv_acq.__aenter__ = AsyncMock(return_value=dlv_conn)
        dlv_acq.__aexit__ = AsyncMock(return_value=False)
        dlv_pool = MagicMock()
        dlv_pool.acquire = MagicMock(return_value=dlv_acq)

        out_path_mismatch = await crm_delivery.deliver_committed_to_crm(
            pool=dlv_pool,
            queue_id=7000,
            plan=captured_plans[0],
            result=writer.CommitResult(
                proposal_id=700, dry_run=False, outcome="committed", doc_id=501, practice_id=None, audit_id=601
            ),
            expected_blob_path=str(tmp_blob),
            expected_blob_hash=approved_blob_hash,
        )
        assert out_path_mismatch["status"] == "blob_mismatch"

        dlv_conn.fetchrow = AsyncMock(
            side_effect=[
                {"blob_path": str(tmp_blob), "blob_hash": approved_blob_hash, "mime_type": "application/pdf"},
                None,
            ]
        )
        out_byte_mismatch = await crm_delivery.deliver_committed_to_crm(
            pool=dlv_pool,
            queue_id=7000,
            plan=captured_plans[0],
            result=writer.CommitResult(
                proposal_id=700, dry_run=False, outcome="committed", doc_id=501, practice_id=None, audit_id=601
            ),
            expected_blob_path=str(tmp_blob),
            expected_blob_hash=approved_blob_hash,
        )
        assert out_byte_mismatch["status"] == "blob_mismatch"
    finally:
        tmp_blob.unlink(missing_ok=True)
