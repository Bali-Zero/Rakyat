"""One-off fill of the staff HR cards from a JSON document on STDIN.

    python backend/scripts/import_staff_cards.py            # dry-run, counts only
    python backend/scripts/import_staff_cards.py --apply    # one transaction, all or nothing

Every card ends in exactly one of: applied, rejected:<code>, not_found (STAFF_CARD.md).
`--apply` commits only when every card is applied (not_found tolerated with
--skip-not-found); otherwise the whole batch rolls back and `committed: no` is printed.

STDIN shape: {"cards": [{"email": "...", "fields": {...}, "profile": {...},
"is_active": true|false|null}]}. The payload never travels in argv or a file
argument, and nothing from it (name, email, any value) is ever printed or
logged: the output is counts and reason codes only. Writes go through
HRService.merge_card_on, the non-clobbering merge. A card is resolved by EXACT email
on staff rows (role <> 'client'); an email that matches several staff rows once
lower-cased is rejected:ambiguous_email, never resolved to one of them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BACKEND_DIR))
sys.path.append(str(BACKEND_DIR / "backend"))

if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "postgresql://localhost:5432/nuzantara_dev"

from pydantic import ValidationError

from backend.app.modules.identity.service import IdentityService
from backend.app.services.hr.hr_card_models import StaffCardPatch
from backend.app.services.hr.hr_service import HRCardError, HRService
from backend.core.pg_json_codec import init_asyncpg_connection

# The service logs ids on every merge; this script prints counts only.
logging.getLogger().setLevel(logging.WARNING)
logging.getLogger("backend.app.services.hr.hr_service").setLevel(logging.WARNING)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Strict flags: anything unknown exits 2 before any DB access."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    parser.add_argument(
        "--skip-not-found",
        action="store_true",
        help="with --apply: skip emails that resolve to no staff member instead of stopping",
    )
    return parser.parse_args(argv)


def validate_card(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return ({email, fields, profile}, None) or (None, reason_code)."""
    if not isinstance(raw, dict):
        return None, "bad_shape"
    email = raw.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "email_missing"
    # Absent is fine; present means an object. An explicit null, [], false, 0 or ""
    # is rejected as it came, never normalised to {} (no silent drop).
    fields = raw.get("fields", {})
    profile = raw.get("profile", {})
    if not isinstance(fields, dict) or not isinstance(profile, dict):
        return None, "bad_shape"
    body = {**fields, "profile": profile}
    if raw.get("is_active") is not None:
        body["is_active"] = raw["is_active"]
    try:
        patch = StaffCardPatch.model_validate(body)
    except ValidationError as exc:
        locs = {str(e["loc"][0]) if e["loc"] else "" for e in exc.errors()}
        if "ptkp_status" in locs:
            return None, "invalid_ptkp"
        if "profile" in locs:
            return None, "invalid_profile"
        return None, "invalid_fields"
    return {
        "email": email.strip(),
        "fields": patch.model_dump(exclude={"profile"}, exclude_none=True),  # real date objects
        "profile": patch.profile.model_dump(mode="json", exclude_none=True, exclude_unset=True),
    }, None


async def run(
    conn: Any, document: Any, apply: bool, skip_not_found: bool = False
) -> tuple[Counter[str], bool]:
    """Classify every card (and write when apply). Returns (counts, ok).

    Dry-run writes nothing and reports what it can know: rejected:<code>, not_found,
    would_update / would_insert. Apply writes in one transaction and records, per
    card, applied / rejected:<code> / not_found; it commits only if all are fine.
    """
    counts: Counter[str] = Counter()
    cards = document.get("cards") if isinstance(document, dict) else None
    if not isinstance(cards, list):
        counts["rejected:bad_document"] += 1
        return counts, False

    service = HRService(None)  # type: ignore[arg-type]  # merge_card_on uses the given connection
    batch = conn.transaction() if apply else None
    if batch is not None:
        await batch.start()
    try:
        for raw in cards:
            await _process_card(conn, service, raw, apply, counts)
        clean = not any(k.startswith("rejected:") for k in counts) and (
            skip_not_found or not counts["not_found"]
        )
        if not skip_not_found and counts["not_found"]:
            counts["blocked:not_found"] = counts["not_found"]
        if batch is not None:
            open_batch, batch = batch, None
            if clean:
                await open_batch.commit()
            else:
                await open_batch.rollback()
            counts["committed:yes" if clean else "committed:no"] = 1
        return counts, clean if apply else True  # a dry-run only informs
    except BaseException:
        if batch is not None:
            await batch.rollback()
        raise


async def _process_card(
    conn: Any, service: HRService, raw: Any, apply: bool, counts: Counter[str]
) -> None:
    card, reason = validate_card(raw)
    if card is None:
        counts[f"rejected:{reason}"] += 1
        return
    candidates = await conn.fetch(
        "SELECT id, email FROM team_members WHERE LOWER(email) = LOWER($1) "
        "AND COALESCE(role, '') <> 'client'",
        card["email"],
    )
    if len(candidates) > 1:  # never pick one of several people behind one address
        counts["rejected:ambiguous_email"] += 1
        return
    member = candidates[0] if candidates and candidates[0]["email"] == card["email"] else None
    if member is None:
        counts["not_found"] += 1
        return
    counts["resolved"] += 1
    has_row = await conn.fetchval(
        "SELECT 1 FROM hr_employees WHERE team_member_id = $1", member["id"]
    )
    if not has_row and card["fields"].get("hire_date") is None:
        counts["rejected:hire_date_required"] += 1
        return
    if not apply:
        counts["would_update" if has_row else "would_insert"] += 1
        return
    try:
        row_id = await service.merge_card_on(conn, member["id"], card["fields"], card["profile"])
    except HRCardError as exc:
        counts[f"rejected:{exc.code.value}"] += 1
        return
    if row_id is None:
        counts["rejected:not_applied"] += 1
        return
    counts["applied"] += 1


def format_counts(counts: Counter[str], apply: bool) -> str:
    lines = [f"mode: {'apply' if apply else 'dry-run'}"]
    keys = (
        ("resolved", "not_found", "applied")
        if apply
        else ("resolved", "not_found", "would_update", "would_insert")
    )
    for key in keys:
        lines.append(f"{key}: {counts.get(key, 0)}")
    for key in sorted(k for k in counts if k.startswith(("rejected:", "blocked:", "committed:"))):
        lines.append(f"{key}: {counts[key]}")
    return "\n".join(lines)


async def amain(argv: list[str], stdin_text: str) -> int:
    args = parse_args(argv)  # SystemExit(2) on an unknown flag, before anything else
    try:
        document = json.loads(stdin_text)
    except ValueError:
        print("rejected:bad_json")  # noqa: T201
        return 1
    conn = await IdentityService().get_db_connection()
    try:
        await init_asyncpg_connection(conn)  # the production jsonb codec (S3)
        counts, ok = await run(conn, document, args.apply, args.skip_not_found)
    except HRCardError as exc:
        print(f"failed: {exc.code.value} (transaction rolled back)")  # noqa: T201
        return 1
    except Exception as exc:  # never echo the message: it may carry payload values
        print(f"failed: {type(exc).__name__} (transaction rolled back)")  # noqa: T201
        return 1
    finally:
        await conn.close()
    print(format_counts(counts, args.apply))  # noqa: T201
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:], sys.stdin.read())))
