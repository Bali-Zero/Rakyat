"""Staff HR card import: the WORKER. Not run by hand: `import_staff_cards.py` starts it.

The worker validates, resolves, locks and merges the cards in ONE transaction, exactly
as STAFF_CARD.md describes. Its stdout and stderr are discarded by its parent and its
exit code is advisory: the only thing that counts is the record stream it writes on the
inherited result pipe (`--result-fd=N`), one compact JSON line per phase:

    {"phase":"decided","committed":true|false,"outcomes":{"<key>":<int>,...}}
    {"phase":"failed","code":"<code>"}
    {"phase":"closed","close_ok":true|false}

`decided` is written and flushed the moment the transaction is decided (right after
COMMIT or ROLLBACK returned, or when nothing was ever begun), before the connection is
closed. Records carry closed literals and integers only: never a value.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import sys
import unicodedata
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(BACKEND_DIR))
sys.path.append(str(BACKEND_DIR / "backend"))

if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "postgresql://localhost:5432/nuzantara_dev"

try:
    from pydantic import ValidationError

    from backend.app.modules.identity.service import IdentityService
    from backend.app.services.hr.hr_card_models import StaffCardPatch
    from backend.app.services.hr.hr_service import HRCardError, HRService
    from backend.core.pg_json_codec import init_asyncpg_connection

    IMPORTS_OK = True
except BaseException:  # reported as the code import_failed; nothing is printed either way
    IMPORTS_OK = False

_FINAL_KEYS = frozenset({"not_found", "applied", "would_update", "would_insert"})
LOCK_TIMEOUT_MS = 5000  # transaction-local: a blocked lock rejects the card, never hangs the batch
_OUTER_KEYS = frozenset({"email", "fields", "profile", "is_active"})
_CANONICAL_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
# The ambiguity form: case-folded AND stripped of every whitespace character, on both sides.
_NORMALISED = "lower(regexp_replace({}, '[[:space:]]', '', 'g'))"


def _email_is_canonical(email: str) -> bool:
    """A plain single-token address, byte for byte what would be stored: no leading or
    trailing space, no whitespace at all, no control / format / unassigned character."""
    if len(email) > 320 or _CANONICAL_EMAIL.fullmatch(email) is None:
        return False
    return not any(unicodedata.category(ch).startswith(("C", "Z")) for ch in email)


def validate_card(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return ({email, fields, profile}, None) or (None, reason_code)."""
    if not isinstance(raw, dict):
        return None, "bad_shape"
    if not set(raw) <= _OUTER_KEYS:  # an unknown outer key is rejected, never dropped
        return None, "unknown_key"
    email = raw.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "email_missing"
    if not _email_is_canonical(email):  # the identity is never normalised, only refused
        return None, "email_noncanonical"
    # Absent is fine; present means an object. An explicit null, [], false, 0 or ""
    # is rejected as it came, never normalised to {} (no silent drop).
    fields = raw.get("fields", {})
    profile = raw.get("profile", {})
    if not isinstance(fields, dict) or not isinstance(profile, dict):
        return None, "bad_shape"
    if "profile" in fields:  # a profile nested inside `fields` is a mistake, never overwritten
        return None, "unknown_key"
    body = {**fields, "profile": profile}
    if raw.get("is_active") is not None:
        body["is_active"] = raw["is_active"]
    try:
        patch = StaffCardPatch.model_validate(body)
    except ValidationError as exc:
        # include_input=False and loc[0] only: pydantic renders the input, a deep loc holds keys
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        locs = {str(e["loc"][0]) if e["loc"] else "" for e in errors}
        if "ptkp_status" in locs:
            return None, "invalid_ptkp"
        if "profile" in locs:
            return None, "invalid_profile"
        return None, "invalid_fields"
    return {
        "email": email,  # byte for byte: this is the exact-match key and the lock re-check
        "fields": patch.model_dump(exclude={"profile"}, exclude_none=True),  # real date objects
        "profile": patch.profile.model_dump(mode="json", exclude_none=True, exclude_unset=True),
    }, None


async def run(
    conn: Any,
    document: Any,
    apply: bool,
    skip_not_found: bool = False,
    on_decided: Callable[[bool, Counter[str]], None] | None = None,
) -> tuple[Counter[str], bool]:
    """Classify every card (and write when apply). Returns (counts, ok).

    Dry-run writes nothing and reports what it can know: rejected:<code>, not_found,
    would_update / would_insert. Apply writes in one transaction and records, per
    card, applied / rejected:<code> / not_found; it commits only if all are fine.
    `on_decided(committed, counts)` is called the moment the transaction is decided
    (after COMMIT or ROLLBACK returned; for a dry-run, when classification ends); a
    COMMIT or ROLLBACK that raises is never reported as decided.
    """
    counts: Counter[str] = Counter()
    cards = document.get("cards") if isinstance(document, dict) else None
    if not isinstance(cards, list):
        counts["rejected:bad_document"] += 1
        if on_decided is not None:
            on_decided(False, counts)  # nothing was begun
        return counts, False

    service = HRService(None)  # type: ignore[arg-type]  # merge_card_on uses the given connection
    batch = conn.transaction() if apply else None
    if batch is not None:
        await batch.start()
    try:
        if batch is not None:
            await conn.execute(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}")
        pending: list[tuple[str, int, dict[str, Any]]] = []
        for index, raw in enumerate(cards):
            resolved = await _classify_card(conn, raw, apply, counts)
            if resolved is not None:
                pending.append((resolved[0], index, resolved[1]))
        if apply:
            # Deterministic lock order: two batches can never take the same members in
            # opposite order (document order breaks ties for the same member).
            for member_id, _, card in sorted(pending, key=lambda item: (item[0], item[1])):
                await _apply_card(conn, service, member_id, card, counts)
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
            if on_decided is not None:
                on_decided(clean, counts)
            counts["committed:yes" if clean else "committed:no"] = 1
        elif on_decided is not None:
            on_decided(False, counts)
        return counts, clean if apply else True  # a dry-run only informs
    except BaseException:
        final = sum(n for k, n in counts.items() if k.startswith("rejected:") or k in _FINAL_KEYS)
        if len(cards) > final:  # every card is accounted for, even on an unexpected failure
            counts["unclassified"] = len(cards) - final
        if batch is not None:
            await batch.rollback()
            if on_decided is not None:
                on_decided(False, counts)  # the rollback returned: nothing was committed
        raise


async def _classify_card(
    conn: Any, raw: Any, apply: bool, counts: Counter[str]
) -> tuple[str, dict[str, Any]] | None:
    """Validate and resolve one card. Returns (member_id, card) when it is to be written."""
    card, reason = validate_card(raw)
    if card is None:
        counts[f"rejected:{reason}"] += 1
        return None
    candidates = await conn.fetch(
        f"SELECT id, email FROM team_members WHERE {_NORMALISED.format('email')} "
        f"= {_NORMALISED.format('$1')} AND COALESCE(role, '') <> 'client'",
        card["email"],
    )
    # a padded or case twin among the staff rows is a second person behind this address
    if len(candidates) > 1:  # never pick one of several people behind one address
        counts["rejected:ambiguous_email"] += 1
        return None
    member = candidates[0] if candidates and candidates[0]["email"] == card["email"] else None
    if member is None:
        counts["not_found"] += 1
        return None
    counts["resolved"] += 1
    has_row = await conn.fetchval(
        "SELECT 1 FROM hr_employees WHERE team_member_id = $1", member["id"]
    )
    if not has_row and card["fields"].get("hire_date") is None:
        counts["rejected:hire_date_required"] += 1
        return None
    if not apply:
        counts["would_update" if has_row else "would_insert"] += 1
        return None
    return member["id"], card


async def _apply_card(
    conn: Any, service: HRService, member_id: str, card: dict[str, Any], counts: Counter[str]
) -> None:
    try:
        # Lock the resolved row and re-check exact email + staff in the same statement: a
        # member renamed or turned into a client since the resolve is rejected, not written.
        if not await service.lock_staff_member(conn, member_id, card["email"]):
            counts["rejected:member_changed"] += 1
            return
        row_id = await service.merge_card_on(conn, member_id, card["fields"], card["profile"])
    except HRCardError as exc:
        counts[f"rejected:{exc.code.value}"] += 1
        return
    if row_id is None:
        counts["rejected:not_applied"] += 1
        return
    counts["applied"] += 1


class Reporter:
    """The result pipe. Writes records made of closed literals and integers, nothing else;
    a pipe that is gone (parent died) is not an error worth a word."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def _send(self, record: dict[str, Any]) -> None:
        data = (json.dumps(record, separators=(",", ":")) + "\n").encode("ascii")
        try:
            while data:
                data = data[os.write(self.fd, data) :]
        except OSError:
            pass

    def decided(self, committed: bool, counts: Counter[str] | dict[str, int]) -> None:
        outcomes = {k: int(v) for k, v in counts.items() if v and not k.startswith("committed:")}
        self._send({"phase": "decided", "committed": bool(committed), "outcomes": outcomes})

    def failed(self, code: str) -> None:
        self._send({"phase": "failed", "code": code})

    def closed(self, close_ok: bool) -> None:
        self._send({"phase": "closed", "close_ok": bool(close_ok)})


async def work(apply: bool, skip_not_found: bool, raw: bytes, rep: Reporter) -> bool:
    """Do the import and report it. Returns True when it ended without a failure record."""
    conn: Any = None
    failed = False
    try:
        try:
            document = json.loads(raw)
        except BaseException:
            rep.decided(False, {})  # nothing was begun
            rep.failed("bad_input")
            return False
        try:
            conn = await IdentityService().get_db_connection()
        except BaseException:
            rep.decided(False, {})
            rep.failed("connect_failed")
            return False
        try:
            await init_asyncpg_connection(conn)  # the production jsonb codec
        except BaseException:
            rep.decided(False, {})
            rep.failed("unexpected")
            failed = True
        else:
            try:
                await run(conn, document, apply, skip_not_found, on_decided=rep.decided)
            except HRCardError as exc:
                rep.failed(exc.code.value)
                failed = True
            except BaseException:  # never the message: it may carry payload values
                rep.failed("unexpected")
                failed = True
    finally:
        close_ok = True
        if conn is not None:
            try:
                await conn.close()
            except BaseException:
                close_ok = False
        rep.closed(close_ok)
    return not failed and close_ok


def _parse(argv: list[str]) -> tuple[bool, bool, int] | None:
    apply = skip = False
    fd: int | None = None
    for arg in argv:
        if arg == "--apply" and not apply:
            apply = True
        elif arg == "--skip-not-found" and not skip:
            skip = True
        elif arg.startswith("--result-fd=") and fd is None and arg[12:].isdigit():
            fd = int(arg[12:])
        else:
            return None
    return None if fd is None else (apply, skip, fd)


def main(argv: list[str]) -> int:
    parsed = _parse(argv)
    if parsed is None:
        return 2  # run by hand or with a wrong argument: it refuses, silently
    apply, skip, fd = parsed
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return 2
    except OSError:
        return 2
    rep = Reporter(fd)
    raw = sys.stdin.buffer.read()
    if not IMPORTS_OK:
        rep.decided(False, {})
        rep.failed("import_failed")
        rep.closed(True)
        return 1
    loop = asyncio.new_event_loop()
    try:
        ok = loop.run_until_complete(work(apply, skip, raw, rep))
    finally:
        loop.close()
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except BaseException:
        code = 1
    os._exit(code)
