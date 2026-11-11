"""One-off fill of the staff HR cards from a JSON document on STDIN.

    python backend/scripts/import_staff_cards.py            # dry-run, counts only
    python backend/scripts/import_staff_cards.py --apply    # one transaction

STDIN shape: {"cards": [{"email": "...", "fields": {...}, "profile": {...},
"is_active": true|false|null}]}. The payload never travels in argv or a file
argument, and nothing from it (name, email, any value) is ever printed or
logged: the output is counts and reason codes only. Writes go through
HRService.merge_card_on, the same non-clobbering merge the PATCH route uses.
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
from backend.app.routers.hr import StaffCardPatch
from backend.app.services.hr.hr_service import HRService

# The service logs ids on every merge; this script prints counts only.
logging.getLogger().setLevel(logging.WARNING)
logging.getLogger("backend.app.services.hr.hr_service").setLevel(logging.WARNING)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Strict flags: anything unknown exits 2 before any DB access."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    return parser.parse_args(argv)


def validate_card(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return ({email, fields, profile}, None) or (None, reason_code)."""
    if not isinstance(raw, dict):
        return None, "bad_shape"
    email = raw.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "email_missing"
    fields = raw.get("fields") or {}
    profile = raw.get("profile") or {}
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
        "fields": patch.model_dump(mode="json", exclude={"profile"}, exclude_none=True),
        "profile": patch.profile.model_dump(mode="json", exclude_none=True)
        if patch.profile
        else {},
    }, None


async def run(conn: Any, document: Any, apply: bool) -> tuple[Counter[str], bool]:
    """Classify every card (and write when apply). Returns (counts, ok)."""
    counts: Counter[str] = Counter()
    cards = document.get("cards") if isinstance(document, dict) else None
    if not isinstance(cards, list):
        counts["rejected:bad_document"] += 1
        return counts, False

    work: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for raw in cards:
        card, reason = validate_card(raw)
        if card is None:
            counts[f"rejected:{reason}"] += 1
            continue
        member = await conn.fetchrow(
            "SELECT id FROM team_members WHERE LOWER(email) = LOWER($1) "
            "AND COALESCE(role, '') <> 'client'",
            card["email"],
        )
        if member is None:
            counts["not_found"] += 1
            continue
        counts["resolved"] += 1
        has_row = await conn.fetchval(
            "SELECT 1 FROM hr_employees WHERE team_member_id = $1", member["id"]
        )
        if has_row:
            counts["would_update"] += 1
        elif card["fields"].get("hire_date") is None:
            counts["rejected:hire_date_required"] += 1
            continue
        else:
            counts["would_insert"] += 1
        work.append((member["id"], card["fields"], card["profile"]))

    rejected = sum(n for k, n in counts.items() if k.startswith("rejected:"))
    if not apply:
        return counts, True
    if rejected:
        return counts, False
    service = HRService(None)  # type: ignore[arg-type]  # merge_card_on uses the given connection
    async with conn.transaction():
        for member_id, fields, profile in work:
            await service.merge_card_on(conn, member_id, fields, profile)
    return counts, True


def format_counts(counts: Counter[str], apply: bool) -> str:
    lines = [f"mode: {'apply' if apply else 'dry-run'}"]
    for key in ("resolved", "not_found", "would_update", "would_insert"):
        lines.append(f"{key}: {counts.get(key, 0)}")
    for key in sorted(k for k in counts if k.startswith("rejected:")):
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
        counts, ok = await run(conn, document, args.apply)
    except Exception as exc:  # never echo the message: it may carry payload values
        print(f"failed: {type(exc).__name__} (transaction rolled back)")  # noqa: T201
        return 1
    finally:
        await conn.close()
    print(format_counts(counts, args.apply))  # noqa: T201
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv[1:], sys.stdin.read())))
