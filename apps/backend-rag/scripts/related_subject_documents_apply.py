"""Send the documents of registered subjects to POST /api/crm/related-subjects/documents/register.

Dry run by default; ``--apply`` writes and needs the plan's sha256 as the dry run printed it. The
plan is read once: its sha256 and its rows come from the same bytes. There is no ledger: a re-run
sends every row again and the registry (insert-only, first write wins) answers ``already_linked``
for what is already written. What was written is proved on the database, never from this output.

Each plan row is one subject: ``subject_ref`` (an int from 0 to 999999999 or a string of 1 to 9
ASCII digits), ``kind``, ``identifiers`` and ``documents``. A row that preflight refuses (limits of
the route, body over 65,536 bytes) is never sent, is counted as ``skipped:<reason>`` and fails
the run.

From an answer the script reads the status and, for status 200 only, ``outcome`` and ``dry_run``;
nothing else, and no value of an answer is ever printed. It prints fixed text, the plan sha256,
numbers of rows and calls, the labels of ``classify`` and the plan totals. Exit 0: no failing
label, no skipped row, nothing refused. Exit 1: otherwise. Exit 2: refused before any call.

For the whole run Python warnings are discarded, logging is disabled and Python's ``sys.stderr``
is redirected to a sink that is never read: the script's own lines go to stdout only.

Env: ``WA_MIRROR_CRM_WRITE_KEY`` (scoped key), ``WA_MIRROR_BACKEND_URL`` (default Fly).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
import sys
import time
import warnings
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
OUTCOMES = frozenset(
    {"subject_not_registered", "conflict", "would_link", "linked", "already_linked"}
)
HTTP_LABELS = {401: "http_401", 422: "http_422", 503: "http_503", 0: "http_0"}
FAILING = frozenset({"http_401", "http_422", "http_503", "http_0", "http_other", "malformed"})
DOCUMENT_FIELDS = (
    "sha256",
    "byte_size",
    "mime_type",
    "doc_type",
    "first_seen_on",
    "first_channel",
)
MAX_REFERENCE = 999_999_999


def classify(status: Any, body: Any, dry_run_sent: bool) -> str:
    """The label of an answer: exactly one of eleven, by the first rule that applies."""
    if type(status) is not int:
        return "http_other"
    if status != 200:
        return HTTP_LABELS.get(status, "http_other")
    outcome = body.get("outcome") if isinstance(body, dict) else None
    if type(outcome) is not str or outcome not in OUTCOMES:
        return "malformed"
    dry_run = body.get("dry_run")
    if type(dry_run) is not bool or dry_run != dry_run_sent:
        return "malformed"
    if (outcome == "would_link" and not dry_run_sent) or (outcome == "linked" and dry_run_sent):
        return "malformed"
    return outcome


def reference(value: Any) -> str | None:
    """A plan reference as the string the request carries, or None when it is not acceptable."""
    if type(value) is int:
        return str(value) if 0 <= value <= MAX_REFERENCE else None
    if type(value) is str and 1 <= len(value) <= 9 and value.isascii() and value.isdigit():
        return value
    return None


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
    ref = reference(row.get("subject_ref"))
    if ref is None:
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


def unique_totals(rows: list[dict[str, Any]], sha256: str, *, dry_run: bool) -> dict[str, int]:
    """Distinct documents, (file id, hash) pairs and (reference, hash) pairs of the rows that pass
    preflight with this plan sha256 and this run's mode. Creates no counter and no failure."""
    hashes: set[str] = set()
    sightings: set[tuple[str, str]] = set()
    links: set[tuple[str, str]] = set()
    for row in rows:
        request, _body, _skip = preflight(row, sha256, dry_run=dry_run)
        if request is None:
            continue
        ref = request["evidence"]["subject_ref"]
        for doc in request["documents"]:
            hashes.add(doc["sha256"])
            links.add((ref, doc["sha256"]))
            sightings.update((d["file_id"], doc["sha256"]) for d in doc["drive"])
    return {"documents": len(hashes), "sightings": len(sightings), "links": len(links)}


class PlanError(Exception):
    pass


def read_plan(path: Path) -> tuple[str, list[dict[str, Any]]]:
    """Read the plan's bytes ONCE; the sha256 and the rows come from those bytes."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        rows = []
        for line in text.split("\n"):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise PlanError
            rows.append(row)
    except (OSError, ValueError, PlanError):
        raise PlanError from None
    return hashlib.sha256(raw).hexdigest(), rows


class _BadArguments(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # never echoes a value
        raise _BadArguments


def _limit(value: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError
    return number


def _run(argv: list[str] | None, client: Any) -> int:
    parser = _Parser(add_help=False, allow_abbrev=False)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-sha256")
    parser.add_argument(
        "--base-url", default=os.environ.get("WA_MIRROR_BACKEND_URL", s1.DEFAULT_BASE_URL)
    )
    parser.add_argument("--pace", type=float, default=0.2)
    parser.add_argument("--limit", type=_limit, default=0)
    try:
        args = parser.parse_args(argv)
    except _BadArguments:
        print("refused: invalid command line")
        return 2

    try:
        sha, rows = read_plan(args.plan)
    except PlanError:
        print("refused: the plan cannot be read as a plan")
        return 2
    print(f"plan_sha256={sha}")
    if args.apply and args.plan_sha256 != sha:
        print("refused: --apply needs the plan's sha256 as printed by the dry run")
        return 2
    key = os.environ.get("WA_MIRROR_CRM_WRITE_KEY", "").strip()
    if not key:
        print("refused: the write key is not set")
        return 2

    dry_run = not args.apply
    totals = unique_totals(rows, sha, dry_run=dry_run)
    url = args.base_url.rstrip("/") + ENDPOINT
    if client is None:
        import httpx

        client = httpx.Client()

    labels: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    sent = 0
    for row in rows:
        if args.limit and sent >= args.limit:
            break
        request, body, skip = preflight(row, sha, dry_run=dry_run)
        if request is None or body is None:
            skipped[str(skip)] += 1
            continue
        try:
            status, answer = s1.send(client, url, key, body)
        except s1.UnsafeRequest:
            print("stopped: a prepared request was refused before sending")
            return 1
        sent += 1
        label = classify(status, answer, dry_run)
        labels[label] += 1
        if sent == 1 and label in ("http_401", "http_503"):
            print(
                "stopped: the first call was refused as unauthorized"
                if label == "http_401"
                else "stopped: the first call found the registry unavailable"
            )
            break
        time.sleep(max(args.pace, 0.0))

    print(f"mode={'apply' if args.apply else 'dry-run'} sent={sent}")
    for name, number in totals.items():
        print(f"plan unique {name}={number}")
    for name, number in sorted(labels.items()):
        print(f"{name}={number}")
    for name, number in sorted(skipped.items()):
        print(f"skipped:{name}={number}")
    failed = any(name in FAILING for name in labels) or bool(skipped)
    return 1 if failed else 0


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    previous_disable = logging.root.manager.disable
    sink = io.StringIO()  # whatever Python-level code writes to stderr lands here and is dropped
    try:
        with warnings.catch_warnings(), contextlib.redirect_stderr(sink):
            warnings.simplefilter("ignore")
            logging.disable(logging.CRITICAL)
            try:
                return _run(argv, client)
            except SystemExit:
                raise
            except Exception:
                print("failed: unexpected producer failure")
                return 1
    finally:
        logging.disable(previous_disable)


if __name__ == "__main__":
    raise SystemExit(main())
