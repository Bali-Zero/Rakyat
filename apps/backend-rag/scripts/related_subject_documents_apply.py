"""Send the documents of registered subjects to POST /api/crm/related-subjects/documents/register.

Dry run by default: every subject is sent with ``dry_run: true`` and the script prints the plan
sha256, the outcome counts and the sums of the answers' counts. ``--apply`` writes, and requires
the plan sha256 that the dry run printed. Re-runnable: the registry is idempotent and the ledger
skips what already ended in ``linked`` or ``already_linked`` for the same plan.

Plan: one JSON object per line with ``subject_ref``, ``kind``, ``identifiers`` (list of
``{type, value}``, mapped exactly as the slice-1 script maps them, so the keys are the registered
set) and ``documents`` (list of ``{sha256, byte_size, mime_type, doc_type, first_seen_on,
first_channel, drive: [{file_id, seen_on}]}``).

Preflight, before any call: a row with more than 40 documents, a document with more than 8 file
ids, or a body over 65,536 bytes is never sent; it is counted under a skip reason. The body is
measured as the exact bytes transmitted (compact UTF-8, no ASCII escaping) and those bytes, not a
second serialization, are what is sent.

Never prints or logs a hash, a file id or an identifier value: output is counts, plan ids and
codes. Sends only ``X-CRM-Write-Key`` and the content type: no query string, no cookie, no session
or user header, nothing derived from the plan in a header.

Env: ``WA_MIRROR_CRM_WRITE_KEY`` (scoped key), ``WA_MIRROR_BACKEND_URL`` (default Fly).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_S1_PATH = Path(__file__).resolve().with_name("related_subjects_apply.py")
_spec = importlib.util.spec_from_file_location("related_subjects_apply_standalone", _S1_PATH)
assert _spec and _spec.loader
s1 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = s1
_spec.loader.exec_module(s1)

ENDPOINT = "/api/crm/related-subjects/documents/register"
MAX_BODY_BYTES = 65536
MAX_DOCUMENTS = 40
MAX_DRIVE_IDS = 8
TERMINAL = frozenset({"linked", "already_linked"})
OUTCOMES = frozenset(
    {"subject_not_registered", "conflict", "would_link", "linked", "already_linked"}
)
REASONS = frozenset(
    {
        "none_owned",
        "set_differs",
        "partial_overlap",
        "two_owners",
        "kind_differs",
        "document_differs",
    }
)
COUNT_KEYS = ("documents", "sightings", "links", "descriptive_differences")
DOCUMENT_FIELDS = (
    "sha256",
    "byte_size",
    "mime_type",
    "doc_type",
    "first_seen_on",
    "first_channel",
)
DRY_RUN_NOTE = (
    "note: in a dry run the sum of documents counts a hash once per subject that submits it, "
    "so on an empty registry it equals the number of pairs, not of hashes"
)

plan_sha256 = s1.plan_sha256
load_plan = s1.load_plan
append_ledger = s1.append_ledger


def read_ledger(path: Path, sha256: str) -> set[str]:
    return s1.read_ledger(path, sha256, TERMINAL)


def _document(raw: Any) -> dict[str, Any] | None:
    """One plan document as a request document, or None when it is not shaped like one."""
    if not isinstance(raw, dict) or any(name not in raw for name in DOCUMENT_FIELDS):
        return None
    drive = raw.get("drive") or []
    if not isinstance(drive, list) or any(
        not isinstance(d, dict) or "file_id" not in d or "seen_on" not in d for d in drive
    ):
        return None
    out = {name: raw[name] for name in DOCUMENT_FIELDS}
    out["drive"] = [{"file_id": d["file_id"], "seen_on": d["seen_on"]} for d in drive]
    return out


def row_to_request(
    row: dict[str, Any], sha256: str, *, dry_run: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """Map one plan row to a documents request, or (None, reason) when it is not sent."""
    if row.get("kind") not in ("person", "company"):
        return None, "unknown_kind"
    items, _dropped = s1._identifier_items(row)
    if not items:
        return None, "no_key"
    if len(items) > s1.MAX_IDENTIFIERS:
        return None, "too_many_keys"
    ref = str(row.get("subject_ref"))
    if not s1._SAFE_REF.fullmatch(ref):
        return None, "invalid_reference"
    documents = row.get("documents")
    if not isinstance(documents, list) or not documents:
        return None, "no_documents"
    if len(documents) > MAX_DOCUMENTS:
        return None, "too_many_documents"
    prepared = []
    for raw in documents:
        document = _document(raw)
        if document is None:
            return None, "invalid_document"
        if len(document["drive"]) > MAX_DRIVE_IDS:
            return None, "too_many_file_ids"
        prepared.append(document)
    request = {
        "subject": {
            "kind": row["kind"],
            "identifiers": [{"type": i["type"], "value": i["value"]} for i in items],
        },
        "documents": prepared,
        "evidence": {"plan_sha256": sha256, "subject_ref": ref},
        "dry_run": dry_run,
    }
    return request, None


def encode(request: dict[str, Any]) -> bytes:
    """The exact bytes sent: compact UTF-8, no ASCII escaping."""
    return json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def preflight(
    row: dict[str, Any], sha256: str, *, dry_run: bool
) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    """(request, body, None) when the row can be sent, else (None, None, skip reason)."""
    request, skip = row_to_request(row, sha256, dry_run=dry_run)
    if request is None:
        return None, None, skip
    body = encode(request)
    if len(body) > MAX_BODY_BYTES:
        return None, None, "body_too_large"
    return request, body, None


def unique_totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Distinct documents, sightings and (subject, document) links of the plan's sendable rows."""
    hashes: set[str] = set()
    sightings: set[tuple[str, str]] = set()
    links: set[tuple[str, str]] = set()
    for row in rows:
        request, _ = row_to_request(row, "0" * 64, dry_run=True)
        if request is None:
            continue
        ref = request["evidence"]["subject_ref"]
        for doc in request["documents"]:
            hashes.add(doc["sha256"])
            links.add((ref, doc["sha256"]))
            sightings.update((d["file_id"], doc["sha256"]) for d in doc["drive"])
    return {"documents": len(hashes), "sightings": len(sightings), "links": len(links)}


def _allowed(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _counts(body: dict[str, Any]) -> dict[str, int]:
    raw = body.get("counts")
    if not isinstance(raw, dict):
        return {}
    return {
        key: raw[key]
        for key in COUNT_KEYS
        if isinstance(raw.get(key), int) and not isinstance(raw.get(key), bool) and raw[key] >= 0
    }


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    parser.add_argument(
        "--plan-sha256", help="sha256 printed by the dry run (required with --apply)"
    )
    parser.add_argument("--ledger", type=Path, help="default: <plan>.docs-apply-ledger.jsonl")
    parser.add_argument(
        "--base-url", default=os.environ.get("WA_MIRROR_BACKEND_URL", s1.DEFAULT_BASE_URL)
    )
    parser.add_argument("--pace", type=float, default=0.2, help="seconds between calls")
    parser.add_argument("--limit", type=int, default=0, help="send at most N subjects (0 = all)")
    args = parser.parse_args(argv)

    sha = plan_sha256(args.plan)
    print(f"plan_sha256={sha}")
    if args.apply and args.plan_sha256 != sha:
        print("refused: --apply needs --plan-sha256 equal to the plan's sha256 above")
        return 2
    key = os.environ.get("WA_MIRROR_CRM_WRITE_KEY", "").strip()
    if not key:
        print("refused: WA_MIRROR_CRM_WRITE_KEY is not set")
        return 2

    ledger = args.ledger or args.plan.with_name(args.plan.name + ".docs-apply-ledger.jsonl")
    done = read_ledger(ledger, sha) if args.apply else set()
    dry_run = not args.apply
    url = args.base_url.rstrip("/") + ENDPOINT
    if client is None:
        import httpx

        client = httpx.Client()

    rows = load_plan(args.plan)
    plan_totals = unique_totals(rows)
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sums: Counter[str] = Counter()
    sent = 0
    for row in rows:
        request, body, skip = preflight(row, sha, dry_run=dry_run)
        if request is None or body is None:
            counts[f"skipped:{skip}"] += 1
            continue
        ref = request["evidence"]["subject_ref"]
        if ref in done:
            counts["skipped:ledger_done"] += 1
            continue
        if args.limit and sent >= args.limit:
            break
        status, answer = s1.send(client, url, key, body)
        sent += 1
        if status == 200:
            outcome = _allowed(answer.get("outcome"), OUTCOMES) or s1.UNKNOWN
            raw_reason = answer.get("reason")
            reason = _allowed(raw_reason, REASONS) or (
                s1.UNKNOWN if raw_reason is not None else None
            )
            for name, number in _counts(answer).items():
                sums[f"{outcome}:{name}"] += number
        else:
            outcome = f"http_{status}" if isinstance(status, int) else s1.UNKNOWN
            raw_detail = answer.get("detail")
            reason = _allowed(raw_detail, s1.FAILURE_CODES) or (
                s1.UNKNOWN if isinstance(raw_detail, str) else None
            )
        counts[outcome] += 1
        if reason:
            reasons[f"{outcome}:{reason}"] += 1
        if args.apply:
            append_ledger(
                ledger,
                {"plan_sha256": sha, "subject_ref": ref, "outcome": outcome, "reason": reason},
            )
        if status in (401, 503) and sent == 1:
            print(f"stopped: first call answered {status}")
            break
        time.sleep(max(args.pace, 0.0))

    print(f"mode={'apply' if args.apply else 'dry-run'} sent={sent}")
    for name, number in plan_totals.items():
        print(f"plan unique {name}={number}")
    for name, number in sorted(counts.items()):
        print(f"{name}={number}")
    for name, number in sorted(reasons.items()):
        print(f"reason {name}={number}")
    for name, number in sorted(sums.items()):
        print(f"sum {name}={number}")
    if dry_run:
        print(DRY_RUN_NOTE)
    return 0 if not any(k.startswith("http_") for k in counts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
