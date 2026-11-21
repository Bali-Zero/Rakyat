"""Controlled intake backlog batch triage and gated auto-unblocker (Tube 3).

Safety contract:
- ``--dry-run`` (default) executes strictly inside a Postgres READ ONLY
  transaction (``SET LOCAL TRANSACTION READ ONLY``) with keyset pagination
  and ZERO ``FOR UPDATE`` locks.
- Enforces R3-6 suppression (``v2.3-drive-autocreate*``, ``recovery-v1*``) and
  GATE-11 (``id_verified=False``) holds as ``GIALLO`` before any ``VERDE`` vote.
- Strictly PII-free output: reports contain only integer counters, canonical
  enum histograms, timing metrics, and deterministic ``plan_sha256``.
- ``--apply`` refuses unless ALL FOUR locks hold: explicit flag, matching
  ``confirm_plan_sha256``, ``INTAKE_BATCH_APPLY_ENABLED=true``, and bounded
  ``1 <= max_apply <= 500``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from backend.services.intake import closed_taxonomy

VERDE = "VERDE"
GIALLO = "GIALLO"
ROSSO = "ROSSO"

MIN_LEGIBLE_OCR_CHARS = 20
MAX_BATCH_APPLY = 500
SUPPRESSED_PIPELINE_PREFIXES: tuple[str, ...] = (
    "v2.3-drive-autocreate",
    "recovery-v1",
)
_ALLOWED_DECISIONS: frozenset[str] = frozenset(
    {
        "AUTO_ATTACH",
        "LINK_CANDIDATE",
        "AMBIGUOUS",
        "NO_MATCH",
        "QUARANTINE",
        "UNKNOWN",
    }
)

_ID_DIGITS_RE = re.compile(r"\D+")

_TRIAGE_SELECT_SQL = """
SELECT p.id,
       p.queue_id,
       p.doc_index,
       p.pipeline_version,
       p.status,
       p.entity_resolution,
       p.routing,
       p.commit_gate,
       q.pipeline_version AS queue_pipeline_version,
       q.source,
       q.source_ref,
       q.blob_hash,
       q.source_path,
       q.blob_path,
       q.sender_phone,
       q.stage_output
FROM document_routing_proposal p
JOIN intake_queue q ON q.id = p.queue_id
WHERE p.status = 'review_pending'
  AND p.id > $1
ORDER BY p.id ASC
LIMIT $2
"""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _coerce_opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        return int(value.strip())
    return None


def _unwrap_field_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def _pipeline_version_is_suppressed(pipeline_version: str | None) -> bool:
    pv = (pipeline_version or "").strip().lower()
    if not pv:
        return False
    return any(
        pv == prefix or pv.startswith(f"{prefix}:")
        for prefix in SUPPRESSED_PIPELINE_PREFIXES
    )


def _has_gate11_unverified_id(entity_resolution: dict[str, Any]) -> bool:
    candidates = entity_resolution.get("candidates")
    if not isinstance(candidates, list):
        return False
    for cand in candidates:
        if isinstance(cand, dict) and cand.get("id_verified") is False:
            return True
    return False


_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _extract_file_name(row: dict[str, Any], stage_output: dict[str, Any]) -> str | None:
    fn = stage_output.get("file_name")
    if isinstance(fn, str) and fn.strip():
        return fn.strip()
    source_path = row.get("source_path")
    if isinstance(source_path, str) and source_path.strip():
        base = os.path.basename(source_path.strip())
        if base:
            return base
    blob_path = row.get("blob_path")
    if isinstance(blob_path, str) and blob_path.strip():
        base = os.path.basename(blob_path.strip())
        stem = os.path.splitext(base)[0]
        if base and not _HEX64_RE.match(stem):
            return base
    return None


def _extract_ocr_text(stage_output: dict[str, Any]) -> tuple[str, int]:
    """Extract OCR text and length in memory without ever exporting it."""
    classify_out = _as_dict(stage_output.get("classify"))
    pages = classify_out.get("ocr_text_per_page")
    page_texts: list[str] = []
    if isinstance(pages, list):
        for p in pages:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str) and t.strip():
                    page_texts.append(t.strip())
            elif isinstance(p, str) and p.strip():
                page_texts.append(p.strip())
    if page_texts:
        joined = "\n".join(page_texts)
        total_chars = sum(len(t) for t in page_texts)
        return joined, total_chars

    ocr = _as_dict(stage_output.get("ocr"))
    extract = _as_dict(stage_output.get("extract"))
    raw_text = (
        ocr.get("text")
        or extract.get("text")
        or stage_output.get("ocr_text")
        or stage_output.get("text")
        or ""
    )
    text = raw_text if isinstance(raw_text, str) else ""
    explicit_chars = _coerce_opt_int(stage_output.get("ocr_chars"))
    if explicit_chars is None:
        explicit_chars = _coerce_opt_int(ocr.get("chars"))
    char_count = max(len(text.strip()), explicit_chars or 0)
    return text, char_count


def _extract_fields(routing: dict[str, Any], stage_output: dict[str, Any]) -> dict[str, Any]:
    fields = routing.get("fields")
    if isinstance(fields, dict):
        return fields
    extract = _as_dict(stage_output.get("extract"))
    fields = extract.get("fields")
    return fields if isinstance(fields, dict) else {}


def _has_nonempty_fields(fields: dict[str, Any]) -> bool:
    for val in fields.values():
        unwrapped = _unwrap_field_value(val)
        if unwrapped is not None and str(unwrapped).strip():
            return True
    return False


def hash_company_identifier(value: str | None) -> str | None:
    """Return canonical SHA-256 of normalized digits for PII-free matching."""
    if not value:
        return None
    digits = _ID_DIGITS_RE.sub("", str(value))
    if len(digits) < 13:
        return None
    return hashlib.sha256(digits.encode("utf-8")).hexdigest()


def _matches_company_hash_snapshot(
    fields: dict[str, Any],
    prod_company_hashes: frozenset[str] | set[str],
) -> bool:
    if not prod_company_hashes:
        return False
    for key in ("nib", "nib_number", "npwp", "npwp_number", "company_npwp", "document_number"):
        raw = _unwrap_field_value(fields.get(key))
        digest = hash_company_identifier(str(raw) if raw is not None else None)
        if digest and digest in prod_company_hashes:
            return True
    return False


@dataclass(frozen=True)
class TriageItem:
    """Strictly PII-free triage verdict for one ``document_routing_proposal`` row."""

    proposal_id: int
    bucket: str  # VERDE | GIALLO | ROSSO
    reason_code: str
    decision: str
    raw_doc_type: str
    canonical_doc_type: str
    target_kind: str  # client | company | linked | none
    classification_via: str  # existing | deterministic | local_llm | abstain
    prod_company_snapshot_hit: bool = False
    # Internal plan-binding identifiers (included in plan_sha256, never serialized publicly).
    client_id: int | None = None
    company_id: int | None = None
    practice_id: int | None = None
    row_fingerprint: str = ""
    company_attach_candidate: bool = False


@dataclass
class TriageSummary:
    """Aggregated strictly PII-free backlog summary."""

    scanned: int = 0
    counts_by_bucket: dict[str, int] = field(
        default_factory=lambda: {VERDE: 0, GIALLO: 0, ROSSO: 0}
    )
    counts_by_reason: dict[str, int] = field(default_factory=dict)
    counts_by_decision: dict[str, int] = field(default_factory=dict)
    counts_by_canonical_doc_type: dict[str, int] = field(default_factory=dict)
    counts_by_target_kind: dict[str, int] = field(default_factory=dict)
    reclassified_from_unknown: int = 0
    reclassified_via_deterministic: int = 0
    reclassified_via_local_llm: int = 0
    llm_attempts: int = 0
    r3_6_suppressed_holds: int = 0
    gate_11_holds: int = 0
    prod_company_snapshot_hits: int = 0
    company_attach_candidates: int = 0
    min_proposal_id: int | None = None
    max_proposal_id: int | None = None
    verde_proposal_ids: list[int] = field(default_factory=list)
    plan_sha256: str = ""
    elapsed_seconds: float = 0.0
    read_only_enforced: bool = True
    limit: int | None = None
    with_local_llm: bool = False
    llm_limit: int = 40
    has_prod_company_hashes: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize PII-free summary (excludes raw items and any target IDs)."""
        return {
            "scanned": self.scanned,
            "counts_by_bucket": dict(self.counts_by_bucket),
            "counts_by_reason": dict(sorted(self.counts_by_reason.items())),
            "counts_by_decision": dict(sorted(self.counts_by_decision.items())),
            "counts_by_canonical_doc_type": dict(sorted(self.counts_by_canonical_doc_type.items())),
            "counts_by_target_kind": dict(sorted(self.counts_by_target_kind.items())),
            "reclassified_from_unknown": self.reclassified_from_unknown,
            "reclassified_via_deterministic": self.reclassified_via_deterministic,
            "reclassified_via_local_llm": self.reclassified_via_local_llm,
            "llm_attempts": self.llm_attempts,
            "r3_6_suppressed_holds": self.r3_6_suppressed_holds,
            "gate_11_holds": self.gate_11_holds,
            "prod_company_snapshot_hits": self.prod_company_snapshot_hits,
            "company_attach_candidates": self.company_attach_candidates,
            "min_proposal_id": self.min_proposal_id,
            "max_proposal_id": self.max_proposal_id,
            "verde_count": len(self.verde_proposal_ids),
            "plan_sha256": self.plan_sha256,
            "elapsed_seconds": self.elapsed_seconds,
            "read_only_enforced": self.read_only_enforced,
            "limit": self.limit,
            "with_local_llm": self.with_local_llm,
            "llm_limit": self.llm_limit,
            "has_prod_company_hashes": self.has_prod_company_hashes,
        }


def compute_plan_sha256(items: list[TriageItem]) -> str:
    """Deterministic digest binding recipients, doc types, buckets, and input state."""
    canonical = sorted(
        (
            item.proposal_id,
            item.bucket,
            item.reason_code,
            item.canonical_doc_type,
            item.classification_via,
            item.target_kind,
            item.client_id,
            item.company_id,
            item.practice_id,
            item.row_fingerprint,
        )
        for item in items
    )
    encoded = json.dumps(canonical, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def classify_proposal_row(
    row: Any,
    *,
    with_local_llm: bool = False,
    allow_llm_call: bool = False,
    ollama_url: str | None = None,
    ollama_model: str = closed_taxonomy.DEFAULT_OLLAMA_MODEL,
    prod_company_hashes: frozenset[str] | set[str] | None = None,
) -> tuple[TriageItem, bool]:
    """Classify one ``review_pending`` proposal deterministically (+ bounded LLM).

    Returns ``(TriageItem, used_llm_call)``. Zero PII leaves summary exports.
    """
    r = dict(row)
    proposal_id = int(r["id"])
    routing = _as_dict(r.get("routing"))
    entity_resolution = _as_dict(r.get("entity_resolution"))
    commit_gate = _as_dict(r.get("commit_gate"))
    stage_output = _as_dict(r.get("stage_output"))

    raw_decision = (
        str(entity_resolution.get("decision") or routing.get("decision") or "UNKNOWN")
        .strip()
        .upper()
    )
    decision = raw_decision if raw_decision in _ALLOWED_DECISIONS else "UNKNOWN"

    client_id = _coerce_opt_int(routing.get("client_id"))
    company_id = _coerce_opt_int(routing.get("company_id"))
    practice_id = _coerce_opt_int(routing.get("practice_id"))
    if client_id is not None and company_id is not None:
        target_kind = "linked"
    elif company_id is not None:
        target_kind = "company"
    elif client_id is not None:
        target_kind = "client"
    else:
        target_kind = "none"

    raw_doc_type = str(
        routing.get("doc_type")
        or entity_resolution.get("doc_type")
        or _as_dict(stage_output.get("classify")).get("doc_type")
        or _as_dict(stage_output.get("extract")).get("doc_type")
        or stage_output.get("doc_type")
        or "unknown"
    ).strip().lower()
    canonical_doc_type = closed_taxonomy.normalize_to_canonical_code(raw_doc_type)

    effective_file_name = _extract_file_name(r, stage_output)
    ocr_text, ocr_chars = _extract_ocr_text(stage_output)
    fields = _extract_fields(routing, stage_output)
    nonempty_fields = _has_nonempty_fields(fields)

    classification_via = "existing" if canonical_doc_type != closed_taxonomy.ABSTAIN_CODE else "abstain"
    llm_used = False

    inferred_subject_kind = (
        "company" if target_kind == "company" else ("person" if target_kind == "client" else None)
    )
    if canonical_doc_type == closed_taxonomy.ABSTAIN_CODE and (
        ocr_chars >= MIN_LEGIBLE_OCR_CHARS or effective_file_name
    ):
        det = closed_taxonomy.classify_taxonomy_deterministic(
            file_name=effective_file_name,
            ocr_text=ocr_text,
            subject_kind=inferred_subject_kind,
        )
        if not det.abstained:
            canonical_doc_type = det.code
            classification_via = "deterministic"
        elif with_local_llm and allow_llm_call and ocr_chars >= MIN_LEGIBLE_OCR_CHARS:
            llm_used = True
            llm_dec = await closed_taxonomy.classify_taxonomy(
                file_name=effective_file_name,
                ocr_text=ocr_text,
                subject_kind=inferred_subject_kind,
                allow_local_llm=True,
                ollama_url=ollama_url,
                model=ollama_model or closed_taxonomy.DEFAULT_OLLAMA_MODEL,
            )
            if not llm_dec.abstained:
                canonical_doc_type = llm_dec.code
                classification_via = "local_llm"

    prod_hit = False
    if decision == "NO_MATCH" and prod_company_hashes and nonempty_fields:
        prod_hit = _matches_company_hash_snapshot(fields, prod_company_hashes)

    suppressed = _pipeline_version_is_suppressed(
        str(r.get("pipeline_version") or "")
    ) or _pipeline_version_is_suppressed(str(r.get("queue_pipeline_version") or ""))
    gate11_unverified = _has_gate11_unverified_id(entity_resolution)
    company_attach_cand = bool(commit_gate.get("company_attach_eligible")) or (
        decision == "AUTO_ATTACH"
        and target_kind in ("company", "linked")
        and canonical_doc_type != closed_taxonomy.ABSTAIN_CODE
        and not suppressed
        and not gate11_unverified
    )

    if suppressed:
        bucket = GIALLO
        reason_code = "r3_6_suppressed_pipeline_version"
    elif gate11_unverified:
        bucket = GIALLO
        reason_code = "gate_11_id_unverified"
    elif decision == "AUTO_ATTACH":
        if target_kind == "none":
            bucket = GIALLO
            reason_code = "auto_attach_missing_target"
        elif canonical_doc_type == closed_taxonomy.ABSTAIN_CODE:
            bucket = GIALLO
            reason_code = "auto_attach_unknown_doctype"
        elif classification_via != "existing":
            bucket = GIALLO
            reason_code = "auto_attach_reclassified_needs_review"
        elif target_kind == "company":
            bucket = GIALLO
            reason_code = "auto_attach_company_needs_review"
        elif target_kind == "linked":
            bucket = GIALLO
            reason_code = "auto_attach_linked_needs_review"
        else:
            bucket = VERDE
            reason_code = "auto_attach_ready"
    elif decision == "LINK_CANDIDATE":
        bucket = GIALLO
        reason_code = "link_candidate_review"
    elif decision == "AMBIGUOUS":
        bucket = GIALLO
        reason_code = "ambiguous_review"
    elif decision == "NO_MATCH":
        if ocr_chars < MIN_LEGIBLE_OCR_CHARS and not nonempty_fields:
            bucket = ROSSO
            reason_code = "quarantine_low_signal"
        elif canonical_doc_type == closed_taxonomy.ABSTAIN_CODE and not nonempty_fields:
            bucket = ROSSO
            reason_code = "quarantine_unmatched_unknown"
        else:
            bucket = GIALLO
            reason_code = (
                "no_match_prod_company_candidate"
                if prod_hit
                else "no_match_legible_needs_entity"
            )
    else:
        bucket = GIALLO
        reason_code = "unknown_decision_review"

    row_fp_payload = {
        "proposal_id": proposal_id,
        "queue_id": _coerce_opt_int(r.get("queue_id")),
        "doc_index": _coerce_opt_int(r.get("doc_index")),
        "status": str(r.get("status") or ""),
        "pipeline_version": str(r.get("pipeline_version") or ""),
        "queue_pipeline_version": str(r.get("queue_pipeline_version") or ""),
        "source": str(r.get("source") or ""),
        "source_ref": str(r.get("source_ref") or ""),
        "blob_hash": str(r.get("blob_hash") or ""),
        "source_path": str(r.get("source_path") or ""),
        "blob_path": str(r.get("blob_path") or ""),
        "sender_phone": str(r.get("sender_phone") or ""),
        "entity_resolution": entity_resolution,
        "routing": routing,
        "commit_gate": commit_gate,
        "stage_output": stage_output,
        "decision": decision,
        "client_id": client_id,
        "company_id": company_id,
        "practice_id": practice_id,
        "raw_doc_type": raw_doc_type,
        "canonical_doc_type": canonical_doc_type,
        "classification_via": classification_via,
        "suppressed": suppressed,
        "gate11_unverified": gate11_unverified,
    }
    row_fp = hashlib.sha256(
        json.dumps(
            row_fp_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()

    return (
        TriageItem(
            proposal_id=proposal_id,
            bucket=bucket,
            reason_code=reason_code,
            decision=decision,
            raw_doc_type=raw_doc_type or "unknown",
            canonical_doc_type=canonical_doc_type,
            target_kind=target_kind,
            classification_via=classification_via,
            prod_company_snapshot_hit=prod_hit,
            client_id=client_id,
            company_id=company_id,
            practice_id=practice_id,
            row_fingerprint=row_fp,
            company_attach_candidate=company_attach_cand,
        ),
        llm_used,
    )


async def _scan_pending_proposals(
    conn: asyncpg.Connection,
    *,
    batch_size: int = 1000,
    limit: int | None = None,
    with_local_llm: bool = False,
    llm_limit: int = 40,
    ollama_url: str | None = None,
    ollama_model: str = closed_taxonomy.DEFAULT_OLLAMA_MODEL,
    prod_company_hashes: frozenset[str] | set[str] | None = None,
) -> tuple[TriageSummary, list[TriageItem]]:
    t0 = time.monotonic()
    summary = TriageSummary(
        read_only_enforced=True,
        limit=limit,
        with_local_llm=bool(with_local_llm),
        llm_limit=llm_limit,
        has_prod_company_hashes=bool(prod_company_hashes),
    )
    items: list[TriageItem] = []

    last_id = 0
    remaining = limit
    while remaining is None or remaining > 0:
        fetch_n = batch_size if remaining is None else min(batch_size, remaining)
        rows = await conn.fetch(_TRIAGE_SELECT_SQL, last_id, fetch_n)
        if not rows:
            break

        for row in rows:
            item, used_llm = await classify_proposal_row(
                row,
                with_local_llm=with_local_llm,
                allow_llm_call=summary.llm_attempts < llm_limit,
                ollama_url=ollama_url,
                ollama_model=ollama_model,
                prod_company_hashes=prod_company_hashes,
            )
            items.append(item)
            summary.scanned += 1
            if used_llm:
                summary.llm_attempts += 1

            summary.counts_by_bucket[item.bucket] = (
                summary.counts_by_bucket.get(item.bucket, 0) + 1
            )
            summary.counts_by_reason[item.reason_code] = (
                summary.counts_by_reason.get(item.reason_code, 0) + 1
            )
            summary.counts_by_decision[item.decision] = (
                summary.counts_by_decision.get(item.decision, 0) + 1
            )
            summary.counts_by_canonical_doc_type[item.canonical_doc_type] = (
                summary.counts_by_canonical_doc_type.get(item.canonical_doc_type, 0) + 1
            )
            summary.counts_by_target_kind[item.target_kind] = (
                summary.counts_by_target_kind.get(item.target_kind, 0) + 1
            )

            if item.classification_via in ("deterministic", "local_llm"):
                summary.reclassified_from_unknown += 1
                if item.classification_via == "deterministic":
                    summary.reclassified_via_deterministic += 1
                else:
                    summary.reclassified_via_local_llm += 1
            if item.reason_code == "r3_6_suppressed_pipeline_version":
                summary.r3_6_suppressed_holds += 1
            if item.reason_code == "gate_11_id_unverified":
                summary.gate_11_holds += 1
            if item.prod_company_snapshot_hit:
                summary.prod_company_snapshot_hits += 1
            if item.company_attach_candidate:
                summary.company_attach_candidates += 1
            if item.bucket == VERDE:
                summary.verde_proposal_ids.append(item.proposal_id)

            if summary.min_proposal_id is None or item.proposal_id < summary.min_proposal_id:
                summary.min_proposal_id = item.proposal_id
            if summary.max_proposal_id is None or item.proposal_id > summary.max_proposal_id:
                summary.max_proposal_id = item.proposal_id

            last_id = item.proposal_id

        if remaining is not None:
            remaining -= len(rows)
        if len(rows) < fetch_n:
            break

    summary.plan_sha256 = compute_plan_sha256(items)
    summary.elapsed_seconds = round(time.monotonic() - t0, 3)
    return summary, items


async def _run_readonly_scan_with_items(
    pool_or_conn: asyncpg.Pool | asyncpg.Connection,
    *,
    batch_size: int = 1000,
    limit: int | None = None,
    with_local_llm: bool = False,
    llm_limit: int = 40,
    ollama_url: str | None = None,
    ollama_model: str = closed_taxonomy.DEFAULT_OLLAMA_MODEL,
    prod_company_hashes: frozenset[str] | set[str] | None = None,
) -> tuple[TriageSummary, list[TriageItem]]:
    async def _run_in_readonly_tx(conn: asyncpg.Connection) -> tuple[TriageSummary, list[TriageItem]]:
        async with conn.transaction():
            await conn.execute("SET LOCAL TRANSACTION READ ONLY")
            return await _scan_pending_proposals(
                conn,
                batch_size=batch_size,
                limit=limit,
                with_local_llm=with_local_llm,
                llm_limit=llm_limit,
                ollama_url=ollama_url,
                ollama_model=ollama_model,
                prod_company_hashes=prod_company_hashes,
            )

    is_pool = isinstance(pool_or_conn, asyncpg.Pool) or (
        "acquire" in getattr(pool_or_conn, "__dict__", {})
        or hasattr(type(pool_or_conn), "acquire")
    )
    if is_pool:
        async with pool_or_conn.acquire() as conn:  # type: ignore[union-attr]
            return await _run_in_readonly_tx(conn)
    return await _run_in_readonly_tx(pool_or_conn)  # type: ignore[arg-type]


async def run_dry_run_triage(
    pool_or_conn: asyncpg.Pool | asyncpg.Connection,
    *,
    batch_size: int = 1000,
    limit: int | None = None,
    with_local_llm: bool = False,
    llm_limit: int = 40,
    ollama_url: str | None = None,
    ollama_model: str = closed_taxonomy.DEFAULT_OLLAMA_MODEL,
    prod_company_hashes: frozenset[str] | set[str] | None = None,
) -> TriageSummary:
    """Execute read-only triage over ``review_pending`` proposals (never mutates DB)."""
    summary, _items = await _run_readonly_scan_with_items(
        pool_or_conn,
        batch_size=batch_size,
        limit=limit,
        with_local_llm=with_local_llm,
        llm_limit=llm_limit,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        prod_company_hashes=prod_company_hashes,
    )
    return summary


async def apply_triage_plan(
    pool: asyncpg.Pool,
    *,
    apply: bool = False,
    confirm_plan_sha256: str | None = None,
    max_apply: int | None = None,
    committed_by: str = "system:batch-triage",
    batch_size: int = 1000,
    limit: int | None = None,
    with_local_llm: bool = False,
    llm_limit: int = 40,
    ollama_url: str | None = None,
    ollama_model: str = closed_taxonomy.DEFAULT_OLLAMA_MODEL,
    prod_company_hashes: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Commit ``VERDE`` rows ONLY if all 4 explicit locks AND LEVA-2 gates hold."""
    if not apply:
        raise PermissionError("LOCK 1 FAILED: --apply flag required for write execution")
    if os.environ.get("INTAKE_BATCH_APPLY_ENABLED", "").strip().lower() != "true":
        raise PermissionError(
            "LOCK 2 FAILED: INTAKE_BATCH_APPLY_ENABLED must equal 'true'"
        )
    if (
        not isinstance(max_apply, int)
        or isinstance(max_apply, bool)
        or not (1 <= max_apply <= MAX_BATCH_APPLY)
    ):
        raise ValueError(f"LOCK 3 FAILED: max_apply must be between 1 and {MAX_BATCH_APPLY}")
    if not confirm_plan_sha256:
        raise ValueError("LOCK 4 FAILED: --confirm-plan-sha256 required")

    from backend.services.intake import auto_attach, client_enricher
    from backend.services.intake import crm_delivery as intake_crm_delivery
    from backend.services.intake import writer as intake_writer

    if not auto_attach.auto_attach_enabled():
        raise PermissionError("INTAKE_AUTO_ATTACH_ENABLED must be truthy for --apply")
    if not intake_writer.writer_enabled():
        raise PermissionError("INTAKE_WRITER_ENABLED must be truthy for --apply")

    summary, items = await _run_readonly_scan_with_items(
        pool,
        batch_size=batch_size,
        limit=limit,
        with_local_llm=with_local_llm,
        llm_limit=llm_limit,
        ollama_url=ollama_url,
        ollama_model=ollama_model,
        prod_company_hashes=prod_company_hashes,
    )
    if summary.plan_sha256 != confirm_plan_sha256:
        raise ValueError(
            "LOCK 4 FAILED: plan_sha256 mismatch — backlog shifted since dry-run"
        )

    approved_by_id = {it.proposal_id: it for it in items if it.bucket == VERDE}
    applied_ids: list[int] = []
    delivered_ids: list[int] = []
    delivery_pending_ids: list[int] = []
    delivery_status_counts: dict[str, int] = {}
    skipped_ids: list[int] = []
    blocked_ids: list[int] = []
    error_ids: list[int] = []
    target_ids = summary.verde_proposal_ids[:max_apply]

    for proposal_id in target_ids:
        approved_item = approved_by_id.get(proposal_id)
        if approved_item is None:
            skipped_ids.append(proposal_id)
            continue

        committed_plan: Any = None
        committed_res: Any = None
        committed_blob_path: str | None = None
        committed_blob_hash: str | None = None
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    prop = await conn.fetchrow(
                        """
                        SELECT p.id, p.queue_id, p.doc_index, p.pipeline_version, p.status,
                               p.entity_resolution, p.routing, p.commit_gate,
                               q.pipeline_version AS queue_pipeline_version,
                               q.source, q.source_ref, q.blob_hash,
                               q.source_path, q.blob_path, q.sender_phone,
                               q.stage_output
                        FROM document_routing_proposal p
                        JOIN intake_queue q ON q.id = p.queue_id
                        WHERE p.id = $1
                        FOR UPDATE OF p, q SKIP LOCKED
                        """,
                        proposal_id,
                    )
                    if prop is None or prop["status"] != "review_pending":
                        skipped_ids.append(proposal_id)
                        continue

                    prop_dict = dict(prop)
                    locked_item, _ = await classify_proposal_row(
                        prop_dict,
                        with_local_llm=False,
                        prod_company_hashes=prod_company_hashes,
                    )
                    if (
                        locked_item.bucket != VERDE
                        or locked_item.row_fingerprint != approved_item.row_fingerprint
                        or (
                            locked_item.client_id,
                            locked_item.company_id,
                            locked_item.practice_id,
                            locked_item.canonical_doc_type,
                        )
                        != (
                            approved_item.client_id,
                            approved_item.company_id,
                            approved_item.practice_id,
                            approved_item.canonical_doc_type,
                        )
                    ):
                        skipped_ids.append(proposal_id)
                        continue

                    approved_snapshot = {
                        "routing": {
                            "decision": approved_item.decision,
                            "client_id": approved_item.client_id,
                        },
                        "entity_resolution": {"decision": approved_item.decision},
                    }
                    divergence = auto_attach._fresh_vs_locked_divergence(
                        approved_snapshot,
                        locked_item.decision,
                        locked_item.client_id,
                    )
                    if divergence:
                        skipped_ids.append(proposal_id)
                        continue

                    try:
                        verdict = await auto_attach.evaluate_concordance(
                            conn,
                            decision=locked_item.decision,
                            client_id=locked_item.client_id,
                            sender_phone=prop_dict.get("sender_phone"),
                        )
                        if not verdict.get("concordant"):
                            skipped_ids.append(proposal_id)
                            continue
                        owned, _ = await auto_attach._strong_id_still_owned(
                            conn,
                            _as_dict(prop_dict.get("entity_resolution")),
                            locked_item.client_id,
                        )
                        if not owned:
                            skipped_ids.append(proposal_id)
                            continue
                    except client_enricher.StrongIdLockBusy:
                        skipped_ids.append(proposal_id)
                        continue

                    locked_queue_snapshot = {
                        "id": prop_dict.get("queue_id"),
                        "source": prop_dict.get("source"),
                        "source_ref": prop_dict.get("source_ref"),
                        "blob_hash": prop_dict.get("blob_hash"),
                        "pipeline_version": prop_dict.get("queue_pipeline_version"),
                        "stage_output": prop_dict.get("stage_output"),
                    }
                    plan = await intake_writer.plan_commit(
                        prop_dict,
                        conn,
                        committed_by=committed_by,
                        locked_queue=locked_queue_snapshot,
                    )
                    if plan.blocked:
                        await intake_writer.execute_commit(plan, conn, dry_run=False)
                        blocked_ids.append(proposal_id)
                        continue
                    res = await intake_writer.execute_commit(
                        plan,
                        conn,
                        dry_run=False,
                        advance_from="review_pending",
                        advance_to="auto_routed",
                    )
                    if res.outcome == "committed":
                        committed_plan = plan
                        committed_res = res
                        committed_blob_path = str(prop_dict.get("blob_path") or "") or None
                        committed_blob_hash = str(prop_dict.get("blob_hash") or "") or None
                    else:
                        skipped_ids.append(proposal_id)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, RuntimeError, ValueError):
            error_ids.append(proposal_id)
            continue

        if committed_res is not None and committed_plan is not None:
            applied_ids.append(proposal_id)
            delivery = await intake_crm_delivery.deliver_committed_to_crm(
                pool=pool,
                queue_id=committed_plan.queue_id,
                plan=committed_plan,
                result=committed_res,
                expected_blob_path=committed_blob_path,
                expected_blob_hash=committed_blob_hash,
            )
            d_status = str((delivery or {}).get("status") or "unknown")
            delivery_status_counts[d_status] = delivery_status_counts.get(d_status, 0) + 1
            if d_status in ("pushed", "already_delivered"):
                delivered_ids.append(proposal_id)
            else:
                delivery_pending_ids.append(proposal_id)

    return {
        "plan_sha256": summary.plan_sha256,
        "requested_max_apply": max_apply,
        "verde_candidates": len(summary.verde_proposal_ids),
        "committed_count": len(applied_ids),
        "delivered_count": len(delivered_ids),
        "delivery_pending_count": len(delivery_pending_ids),
        "skipped_count": len(skipped_ids),
        "blocked_count": len(blocked_ids),
        "error_count": len(error_ids),
        "committed_ids": applied_ids,
        "delivered_ids": delivered_ids,
        "delivery_pending_ids": delivery_pending_ids,
        "delivery_status_counts": delivery_status_counts,
        "error_ids": error_ids,
    }
