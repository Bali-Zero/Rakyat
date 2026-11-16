"""Send the proposable subjects of a registry plan to POST /api/crm/related-subjects/register.

Dry run by default: every subject is sent with ``dry_run: true`` and the script prints the plan
sha256 and the outcome counts. ``--apply`` writes, and requires the plan sha256 that the dry
run printed. Re-runnable: the registry is idempotent and the ledger skips what already ended
in ``created`` or ``already_registered`` for the same plan.

Plan: one JSON object per line with ``subject_id``, ``kind``, ``tier``, ``proposable``,
``identifiers`` (list of ``{type, value, files, independent_files, strengths}``), optional
``mrz`` (dict keyed by passport number) and the unsent ``queue_ids`` / ``flags``.

Never prints or logs a name or an identifier value: output is counts, plan ids and codes.
Sends only ``X-CRM-Write-Key``: no session header, no user header.

Env: ``WA_MIRROR_CRM_WRITE_KEY`` (scoped key), ``WA_MIRROR_BACKEND_URL`` (default Fly).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

_IDENT_PATH = Path(__file__).resolve().parents[1] / "backend/services/crm/related_identifiers.py"
_spec = importlib.util.spec_from_file_location("related_identifiers_standalone", _IDENT_PATH)
assert _spec and _spec.loader
ident = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ident
_spec.loader.exec_module(ident)

DEFAULT_BASE_URL = "https://nuzantara-rag.fly.dev"
ENDPOINT = "/api/crm/related-subjects/register"
MAX_IDENTIFIERS = 20
TERMINAL = {"created", "already_registered"}
OUTCOMES = frozenset(
    {
        "created",
        "would_create",
        "already_registered",
        "conflict",
        "existing_client",
        "existing_family_member",
        "existing_company",
    }
)
REASONS = frozenset({"set_differs", "partial_overlap", "two_owners", "contradiction"})
FAILURE_CODES = frozenset(
    {
        "unauthorized",
        "registry_unavailable",
        "registry_busy",
        "database_error",
        "internal_error",
    }
)
UNKNOWN = "unknown"
_SAFE_WORD = re.compile(r"[A-Za-z0-9_.:+-]{1,32}")
_SAFE_TIER = re.compile(r"[A-Za-z0-9_.:+-]{1,16}")
_SAFE_REF = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")
_SHA = re.compile(r"[0-9a-f]{64}")


def plan_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_plan(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _iso_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _identifier_items(row: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Keep the valid keys of the subject's kind. Returns (items, dropped_count)."""
    kind = row.get("kind")
    kept: dict[tuple[str, str], dict[str, Any]] = {}
    dropped = 0
    for raw in row.get("identifiers") or []:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        id_type, value = raw.get("type"), raw.get("value")
        try:
            if ident.kind_of(id_type) != kind:
                raise ident.InvalidIdentifier("kind")
            norm = ident.canonicalize(id_type, value)
        except (ident.InvalidIdentifier, TypeError, AttributeError):
            dropped += 1
            continue
        if (id_type, norm) in kept:
            continue
        evidence: dict[str, Any] = {}
        for name in ("files", "independent_files"):
            number = raw.get(name)
            if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 10**6:
                evidence[name] = number
        strengths = [
            s
            for s in (raw.get("strengths") or [])
            if isinstance(s, str) and _SAFE_WORD.fullmatch(s)
        ][:16]
        if strengths:
            evidence["strengths"] = strengths
        item: dict[str, Any] = {"type": id_type, "value": value}
        if evidence:
            item["evidence"] = evidence
        kept[(id_type, norm)] = item
    return [kept[k] for k in sorted(kept)], dropped


def _person_details(row: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Name, date of birth, nationality and sex from the MRZ of the kept passports."""
    mrz = row.get("mrz")
    if row.get("kind") != "person" or not isinstance(mrz, dict):
        return {}
    by_norm: dict[str, dict[str, Any]] = {}
    for key, entry in mrz.items():
        try:
            by_norm.setdefault(ident.canonicalize("passport", key), entry)
        except (ident.InvalidIdentifier, TypeError):
            continue
    entries = []
    for item in items:
        if item["type"] == "passport":
            entry = by_norm.get(ident.canonicalize("passport", item["value"]))
            if isinstance(entry, dict):
                entries.append(entry)
    if not entries:
        return {}
    out: dict[str, Any] = {}
    first = entries[0]
    names = [str(first[k]).strip() for k in ("given", "surname") if first.get(k)]
    if names:
        out["display_name"] = " ".join(names)[:200]
    dobs = {d for d in (_iso_date(e.get("dob")) for e in entries) if d}
    if len(dobs) == 1:
        out["date_of_birth"] = dobs.pop()
    for field, source in (("nationality", "nationality"), ("sex", "sex")):
        value = next(
            (e[source] for e in entries if isinstance(e.get(source), str) and e[source]), None
        )
        if value:
            out[field] = value[:64] if field == "nationality" else value[:8]
    return out


def row_to_request(
    row: dict[str, Any], sha256: str, *, dry_run: bool
) -> tuple[dict[str, Any] | None, str | None]:
    """Map one plan row to a register request, or (None, reason) when it is not sent."""
    if row.get("proposable") is not True:
        return None, "not_proposable"
    if row.get("kind") not in ("person", "company"):
        return None, "unknown_kind"
    items, _dropped = _identifier_items(row)
    if not items:
        return None, "no_key"
    if len(items) > MAX_IDENTIFIERS:
        return None, "too_many_keys"
    ref, tier = str(row.get("subject_id")), str(row.get("tier"))
    if not _SAFE_REF.fullmatch(ref) or not _SAFE_TIER.fullmatch(tier):
        return None, "invalid_reference"
    request = {
        "kind": row["kind"],
        "identifiers": items,
        "evidence": {"plan_sha256": sha256, "subject_ref": ref, "tier": tier},
        "dry_run": dry_run,
    }
    request.update(_person_details(row, items))
    return request, None


def _allowed(value: Any, allowed: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def read_ledger(path: Path, sha256: str) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("plan_sha256") == sha256 and entry.get("outcome") in TERMINAL:
            done.add(str(entry.get("subject_ref")))
    return done


def append_ledger(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def send(client: Any, url: str, key: str, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST once; one retry after a short pause on 503 or a transport error."""
    last: tuple[int, dict[str, Any]] = (0, {})
    for attempt in range(2):
        try:
            resp = client.post(url, json=request, headers={"X-CRM-Write-Key": key}, timeout=30.0)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            last = (resp.status_code, body if isinstance(body, dict) else {})
            if resp.status_code != 503:
                return last
        except Exception:
            last = (0, {})
        if attempt == 0:
            time.sleep(2.0)
    return last


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    parser.add_argument(
        "--plan-sha256", help="sha256 printed by the dry run (required with --apply)"
    )
    parser.add_argument("--ledger", type=Path, help="default: <plan>.apply-ledger.jsonl")
    parser.add_argument(
        "--base-url", default=os.environ.get("WA_MIRROR_BACKEND_URL", DEFAULT_BASE_URL)
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

    ledger = args.ledger or args.plan.with_name(args.plan.name + ".apply-ledger.jsonl")
    done = read_ledger(ledger, sha) if args.apply else set()
    dry_run = not args.apply
    url = args.base_url.rstrip("/") + ENDPOINT
    if client is None:
        import httpx

        client = httpx.Client()

    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    sent = 0
    for row in load_plan(args.plan):
        request, skip = row_to_request(row, sha, dry_run=dry_run)
        if request is None:
            counts[f"skipped:{skip}"] += 1
            continue
        ref = request["evidence"]["subject_ref"]
        if ref in done:
            counts["skipped:ledger_done"] += 1
            continue
        if args.limit and sent >= args.limit:
            break
        status, body = send(client, url, key, request)
        sent += 1
        if status == 200:
            outcome = _allowed(body.get("outcome"), OUTCOMES) or UNKNOWN
            raw_reason = body.get("reason")
            reason = _allowed(raw_reason, REASONS) or (UNKNOWN if raw_reason is not None else None)
        else:
            outcome = f"http_{status}" if isinstance(status, int) else UNKNOWN
            raw_detail = body.get("detail")
            reason = _allowed(raw_detail, FAILURE_CODES) or (
                UNKNOWN if isinstance(raw_detail, str) else None
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
    for name, number in sorted(counts.items()):
        print(f"{name}={number}")
    for name, number in sorted(reasons.items()):
        print(f"reason {name}={number}")
    print(f"subjects_left_with_no_key={counts['skipped:no_key']}")
    return 0 if not any(k.startswith("http_") for k in counts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
