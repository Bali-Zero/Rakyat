"""
WhatsApp Chat Router

Meta WhatsApp Cloud API webhook: signature check, ack-first persistence to
``inbound_webhooks``, and routing by destination number.

- meta-inbox number: ledger persistence + media hand-off; replies are generated
  by the wa_outbox worker, not here.
- team-bot number: handed to the team-bot ingress seam (or dropped when its
  kill switch is off).
- any other number: the inline reply path that used to answer these was retired;
  the persisted row is closed with an explicit reason, never retried.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.core.config import settings
from backend.security.pii_log_identifier import redact_identifier_for_log
from backend.security.webhook_verifier import WebhookVerificationError, verify_meta_hmac
from backend.services.integrations.wa_outbox_worker import (
    META_INBOX_PHONE_NUMBER_IDS,
    _manners_enabled,
)
from backend.services.integrations.whatsapp_service import whatsapp_service
from backend.services.integrations.whatsapp_triage_service import whatsapp_triage_service

logger = logging.getLogger(__name__)


# NB: media downloads do NOT happen on Fly (Law 2 — PII stays on the Pro). The
# official-line media handoff publishes metadata only to events_outbox; the
# sovereign Pro worker (scripts/wa_media_pull_worker.py) owns the blob root.

router = APIRouter(prefix="/webhook/whatsapp", tags=["whatsapp"])
WHATSAPP_FAST_PATH_RECOVERY_DELAY_SECONDS = 30

# Claimant names for the wa_reply_claims race gate (2026-08-25 double-reply
# scar, migration 281). One row per Meta wamid, written before a reply is
# generated or sent.
WAMID_CLAIMANT_META_INBOX = "meta_inbox"


async def _claim_wamid_reply(conn: Any, wamid: str, claimant: str) -> bool:
    """Atomically claim ``wamid`` for ``claimant``; True iff this claimant owns it.

    ``ON CONFLICT (wamid) DO UPDATE SET wamid = EXCLUDED.wamid`` is a no-op
    write on the conflicting row's own primary key — deliberate, not
    decorative. ``DO NOTHING`` would return no row at all on conflict, which
    cannot distinguish "the OTHER path already claimed this wamid" from "I
    already claimed it, this is a legitimate retry of my own path" — the two
    need different handling (retry-safely proceed in the second case, never
    in the first). ``DO UPDATE`` always returns a row via ``RETURNING``, and
    because the update never touches ``claimed_by``, the value it returns on
    a conflict is the FIRST claimant's — untouched. So the caller compares
    the returned owner against its own claimant name: equal means either
    "I just won the claim" or "I already own it" (both safe to proceed —
    each path's own downstream inserts are independently idempotent);
    different means the other path already claimed it — log and discard.
    """
    owner = await conn.fetchval(
        """
        INSERT INTO wa_reply_claims (wamid, claimed_by)
        VALUES ($1, $2)
        ON CONFLICT (wamid) DO UPDATE SET wamid = EXCLUDED.wamid
        RETURNING claimed_by
        """,
        wamid,
        claimant,
    )
    return owner == claimant


class WhatsAppMessage(BaseModel):
    """WhatsApp message structure."""

    from_: str = Field(..., alias="from")  # Sender phone
    id: str  # Message ID
    timestamp: str
    type: str  # text, image, audio, etc.
    text: dict[str, Any] | None = None

    model_config = ConfigDict(populate_by_name=True)


class WhatsAppChange(BaseModel):
    """WhatsApp webhook change event."""

    field: str
    value: dict[str, Any]


class WhatsAppEntry(BaseModel):
    """WhatsApp webhook entry."""

    id: str
    changes: list[WhatsAppChange]


class WhatsAppWebhook(BaseModel):
    """WhatsApp webhook payload from Meta."""

    object: str  # Should be "whatsapp_business_account"
    entry: list[WhatsAppEntry]


def _get_db_pool(request: Request) -> Any:
    """Get database pool safely."""
    try:
        from backend.app.dependencies import get_database

        return get_database(request)
    except Exception as e:
        logger.error("Error getting database for WhatsApp chat: %s", type(e).__name__)
        return None


# ============================================================
# WA META INBOX — scoped persistence for phone_number_id=1104946272705747
# (read/write console; see backend/app/routers/wa_inbox.py + spec
#  docs/superpowers/specs/2026-06-03-wa-meta-inbox-design.md sezione 3).
# Only the target Business number flows through this ledger.
# ============================================================

# Map Meta delivery-receipt status → ledger status. Only forward transitions
# are accepted (sent < delivered < read); failed is terminal and always wins.
_META_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3}


def _change_phone_number_id(change: WhatsAppChange) -> str | None:
    """Extract value.metadata.phone_number_id from a webhook change."""
    metadata = change.value.get("metadata") or {}
    pnid = metadata.get("phone_number_id")
    return str(pnid) if pnid is not None else None


# Digits-only comparator for phone numbers: "+62 821-3465-159", "62821-3465159"
# and "628213465159" must all compare equal, mirroring the normalisation
# already used for WA memory subjects (_memory_identity.py).
_WA_NON_DIGITS_RE = re.compile(r"\D+")


def _change_display_phone_number(change: WhatsAppChange) -> str | None:
    """Extract value.metadata.display_phone_number from a webhook change."""
    metadata = change.value.get("metadata") or {}
    dpn = metadata.get("display_phone_number")
    return str(dpn) if dpn is not None else None


def _is_meta_inbox_public_number(display_phone_number: str | None) -> bool:
    """True if display_phone_number is the bot's public WA number.

    Defense-in-depth (2026-08-25 double-reply scar): a Meta webhook
    re-registration can arm a SECOND subscription for the SAME underlying
    business number, delivered with a phone_number_id that
    META_INBOX_PHONE_NUMBER_IDS does not (yet) recognise. Such a delivery
    still carries the real, public display number, so this catches it even
    when the id-based check in META_INBOX_PHONE_NUMBER_IDS misses it —
    it must never fall through to the legacy inline reply path.
    """
    if not display_phone_number:
        return False
    try:
        public_number = settings.SUPPORT_WHATSAPP
    except AttributeError:
        return False
    if not isinstance(public_number, str):
        # Defensive: a test/mocked settings object without SUPPORT_WHATSAPP
        # configured must never crash webhook parsing over this optional
        # extra check — fail closed (not a match), the id-based check in
        # META_INBOX_PHONE_NUMBER_IDS still applies.
        return False
    public_digits = _WA_NON_DIGITS_RE.sub("", public_number)
    if not public_digits:
        # SUPPORT_WHATSAPP not configured: an empty number must never match anything.
        return False
    return _WA_NON_DIGITS_RE.sub("", display_phone_number) == public_digits


def _change_belongs_to_meta_inbox(change: WhatsAppChange) -> bool:
    """True if this webhook change targets the meta-inbox business number.

    Two independent signals, either sufficient (2026-08-25 double-reply
    scar — a Meta webhook re-registration armed a second subscription for
    the same underlying number, delivered with a phone_number_id nobody had
    listed, and its messages fell into the legacy inline reply path beside
    the meta-inbox pipeline's own answer):
      1. phone_number_id is a KNOWN meta-inbox id (META_INBOX_PHONE_NUMBER_IDS).
      2. display_phone_number matches the bot's public WA number — this
         catches a second subscription for the SAME visible number even
         when its phone_number_id has not been added to the set yet.
    """
    if _change_phone_number_id(change) in META_INBOX_PHONE_NUMBER_IDS:
        return True
    return _is_meta_inbox_public_number(_change_display_phone_number(change))


# 2026-08-26 same-WABA finding (Zero, via WhatsApp Manager, relayed by the
# I DUE BOT team lead): the team-bot number +62 881-0383-18896 lives on the
# SAME WABA and the SAME Meta app as the public client number above. One app
# means one webhook, two numbers — so this router's single endpoint receives
# BOTH numbers' deliveries and must tell them apart itself; Meta gives no
# other signal.
#
# This set is DELIBERATELY SEPARATE from META_INBOX_PHONE_NUMBER_IDS and
# must NEVER be merged into it. Before this fix, the team-bot pnid matched
# neither META_INBOX_PHONE_NUMBER_IDS (unlisted id) nor the public-number
# display check (different visible number), so it fell through into the
# legacy inline triage flow below and a staff message got processed as if
# it were a client message. Simply adding the pnid to
# META_INBOX_PHONE_NUMBER_IDS would "fix" that fall-through by routing
# staff traffic into the CLIENT meta-inbox pipeline instead — a different
# wrong destination, not a fix. Team-bot traffic needs its own branch,
# checked BEFORE the meta-inbox check (see the webhook loop below), so it
# reaches neither.
#
# Extra ids come from TEAM_BOT_PHONE_NUMBER_IDS (comma-separated); the
# canonical id is always included even if the env var is unset or empty —
# mirrors META_INBOX_PHONE_NUMBER_IDS's own env-configurability contract.
TEAM_BOT_PHONE_NUMBER_ID = "1188469837692575"
TEAM_BOT_PHONE_NUMBER_IDS: frozenset[str] = frozenset(
    {TEAM_BOT_PHONE_NUMBER_ID}
    | {
        pid.strip()
        for pid in os.environ.get("TEAM_BOT_PHONE_NUMBER_IDS", "").split(",")
        if pid.strip()
    }
)


def _change_belongs_to_team_bot_ingress(change: WhatsAppChange) -> bool:
    """True if this webhook change targets the team-bot business number.

    Id-only check, unlike ``_change_belongs_to_meta_inbox``'s two-signal
    defense — there is exactly one team-bot number today and no
    double-reply-style scar yet to defend against. If a second team-bot
    subscription id shows up the same way the meta-inbox one did
    (2026-08-25), extend this the same way: add a
    ``display_phone_number``-based fallback keyed on the team-bot's own
    public number, not this one's.
    """
    return _change_phone_number_id(change) in TEAM_BOT_PHONE_NUMBER_IDS


def _team_bot_ingress_enabled() -> bool:
    """Reads the ``TEAM_BOT_INGRESS_ENABLED`` kill switch (registered in
    ``backend.services.client_bot.kill_switches``, owning lane B3 — "the
    first rung of the promotion ladder"). Fail-closed and born OFF: only
    the literal ``"true"`` (case-insensitive) turns it on. Unset, empty,
    ``"0"``, ``"false"``, or any typo all leave it OFF, matching the
    switch's own registered ``default_dark=True`` — a misconfigured value
    must degrade to the safe state, not the armed one.
    """
    return os.environ.get("TEAM_BOT_INGRESS_ENABLED", "").strip().lower() == "true"


async def _handle_team_bot_ingress_payload(change: WhatsAppChange) -> None:
    """Seam for the real team-bot ingress handler.

    Lane B3's handler does not exist on this branch as of this writing —
    ``apps/team-bot`` has no webhook-facing ingress code yet (see
    ``backend/tests/duebot/goldens/team_fixtures.py``'s own note on B3's
    construction state, and ``backend/services/team_bot_ingress/`` today
    only covers failover leader election, not per-message handling). This
    function recognises team-bot traffic and DROPS it — logged, no
    processing, no reply — which is exactly what
    ``TEAM_BOT_INGRESS_ENABLED``'s registered ``effect_when_off`` describes
    ("no inbound team-bot traffic is accepted at all"). Replace this body
    with a call into the real handler once B3 lands it; do not remove the
    kill-switch gate at the call site below when that happens — later
    rungs (``TEAM_BOT_REPLY_ENABLED`` etc.) gate what happens *after*
    ingress, not whether ingress itself runs.
    """
    # The id itself is deliberately NOT logged. It is a value WE configure
    # (our own business number), so it carries no information this line does
    # not already state — and CodeQL raises a HIGH clear-text-logging alert on
    # it. The alert is a false positive: the same helper wraps a real client
    # `phone` on 21 other lines of this file, none of them flagged, and it
    # returns a salted digest ("id:<12 hex>"), never the value. Rather than
    # argue with the scanner over a field that says nothing, drop the field.
    # If TEAM_BOT_PHONE_NUMBER_IDS ever holds more than one id, log the INDEX
    # that matched — not the id.
    logger.info(
        "Team-bot webhook message recognised — no ingress handler built yet "
        "(lane B3); dropping without processing."
    )


def _payload_without_team_bot_changes(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``payload`` with every team-bot *message* change removed, or
    ``None`` when nothing else was in it.

    Needed because the recovery net routes the payload as ONE unit (meta-inbox
    pipeline or legacy route), not change by change like the fast path does.
    Both numbers live on the SAME WABA, so a single ``entry`` can legitimately
    carry a team-bot change beside a client change — and an unconditional
    ``return`` after handling the team half would drop the client half in
    silence, on the very path that exists to catch what the fast path missed.
    That would be a worse defect than the one this lane cures: it loses a
    paying client's message instead of misrouting a staff one.

    Operates on the RAW dict (not the parsed model) because that dict is what
    the downstream handlers receive verbatim. An entry that ends up with no
    changes is kept (rebuilt with an empty ``changes`` list, not pruned) — the
    shape the downstream handlers already tolerate is the shape they keep.

    The "nothing else was in it" test counts EVERY surviving change, not only
    ``messages`` ones. Counting messages alone would drop a payload whose only
    remainder is some other change kind — and in this codebase status receipts
    ride INSIDE a ``field == "messages"`` change (``value["statuses"]``, see
    ``process_meta_inbox_payload``), so a message-only counter reads correctly
    today and would start lying the moment Meta adds a field. Count broadly:
    being wrong here means silently discarding a delivery.
    """
    remaining = 0
    entries: list[Any] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            entries.append(entry)
            continue
        kept: list[Any] = []
        for change in entry.get("changes") or []:
            if (
                isinstance(change, dict)
                and change.get("field") == "messages"
                and (((change.get("value") or {}).get("metadata") or {}).get("phone_number_id"))
                in TEAM_BOT_PHONE_NUMBER_IDS
            ):
                continue
            kept.append(change)
            remaining += 1
        entries.append({**entry, "changes": kept})
    if remaining == 0:
        return None
    return {**payload, "entry": entries}


async def _apply_status_callback(conn: Any, status_obj: dict[str, Any]) -> None:
    """Apply one Meta status receipt to the ledger (or stage it if orphan).

    NEVER touches last_customer_at — status receipts are not customer activity
    and must not extend the Meta 24h window (data invariant, spec sezione 2).
    """
    wamid = status_obj.get("id")
    new_status = status_obj.get("status")
    if not wamid or new_status not in ("sent", "delivered", "read", "failed"):
        return

    error_text: str | None = None
    if new_status == "failed":
        errors = status_obj.get("errors") or []
        if errors and isinstance(errors[0], dict):
            error_text = errors[0].get("title") or errors[0].get("message")

    existing = await conn.fetchrow(
        "SELECT id, status FROM meta_inbox_messages WHERE meta_message_id = $1",
        wamid,
    )
    if existing is None:
        # Orphan receipt (status raced ahead of the send commit) → stage it.
        await conn.execute(
            """
            INSERT INTO wa_status_pending (meta_message_id, status, error)
            VALUES ($1, $2, $3)
            ON CONFLICT (meta_message_id) DO UPDATE
            SET status = EXCLUDED.status, error = EXCLUDED.error,
                received_at = NOW()
            """,
            wamid,
            new_status,
            error_text,
        )
        return

    if new_status == "failed":
        await conn.execute(
            "UPDATE meta_inbox_messages SET status = 'failed', error = $2 WHERE id = $1",
            existing["id"],
            error_text,
        )
        return

    # Only advance forward (never regress read → delivered → sent).
    current_rank = _META_STATUS_RANK.get(existing["status"], 0)
    if _META_STATUS_RANK[new_status] > current_rank:
        await conn.execute(
            "UPDATE meta_inbox_messages SET status = $2 WHERE id = $1",
            existing["id"],
            new_status,
        )


async def _handle_meta_inbox_message(
    conn: Any, msg: dict[str, Any], sender_name: str | None, webhook_id: int | None
) -> None:
    """Persist one inbound customer message into the meta-inbox ledger (1 TX).

    Upsert thread (last_customer_at = GREATEST), insert inbound ledger row with
    ON CONFLICT(meta_message_id) DO NOTHING; only if the row is NEW and the
    thread is bot-handled, enqueue a bot reply (queued ledger row + wa_outbox
    needs_generation). The bot text itself is generated by the worker, NOT here.

    After the transaction commits, best-effort mark the inbound message read
    (blue tick — item C5) — deliberately OUTSIDE the transaction: this is a
    Meta Graph API call, and the sub-200ms webhook-ack contract (this whole
    function already runs in a BackgroundTask, see process_meta_inbox_payload)
    must never be extended by holding a DB transaction open across network I/O.
    Only fires for a genuinely NEW inbound row — a duplicate (Meta retry) is
    already acked from the first delivery.

    Gated by ``_manners_enabled()`` (``WA_OUTBOX_MANNERS_ENABLED``, DEFAULT
    OFF) — the SAME dedicated kill-switch as C3/C4 in wa_outbox_worker.py
    (gate review 2026-07-25: this send has no ``whatsapp_ack.ack_enabled()``
    equivalent to AND against, it doesn't go through that module at all).
    Ships dark: unset in prod today.
    """
    wamid = msg.get("id")
    phone = msg.get("from")
    if not wamid or not phone:
        return

    body = ""
    media_type = msg.get("type")
    if media_type == "text":
        body = (msg.get("text") or {}).get("body", "")

    # Meta timestamp is unix seconds (string).
    ts_raw = msg.get("timestamp")
    try:
        meta_ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc) if ts_raw else None
    except (TypeError, ValueError):
        meta_ts = None

    # CROSS-PATH ATOMIC CLAIM (2026-08-25 double-reply scar, migration 281).
    # Mirrors the claim the (since retired) legacy inline path made —
    # whichever path's INSERT into wa_reply_claims lands first for this wamid
    # wins and proceeds; the loser logs and discards. A legitimate Meta retry
    # of this SAME delivery re-claims its own prior win (owner comparison, see
    # _claim_wamid_reply) and proceeds normally into the idempotent ledger
    # insert below. Best-effort ONLY for "DB unreachable" — a claim-query
    # failure must never mute a genuinely meta-inbox message.
    try:
        won_claim = await _claim_wamid_reply(conn, wamid, WAMID_CLAIMANT_META_INBOX)
    except Exception as r4_exc:
        logger.error(
            "meta-inbox: cross-path claim failed (non-blocking) wamid=%s: %s",
            redact_identifier_for_log(wamid),
            type(r4_exc).__name__,
        )
        won_claim = True
    if not won_claim:
        logger.info(
            "meta-inbox path skipped — wamid=%s already claimed by the legacy path",
            redact_identifier_for_log(wamid),
        )
        return

    is_new_inbound = False
    async with conn.transaction():
        thread = await conn.fetchrow(
            """
            INSERT INTO meta_inbox_threads (
                counterpart_phone, counterpart_name, last_customer_at, last_message_at
            )
            VALUES ($1, $2, $3, $3)
            ON CONFLICT (counterpart_phone) DO UPDATE
            SET counterpart_name = COALESCE(EXCLUDED.counterpart_name,
                                            meta_inbox_threads.counterpart_name),
                last_customer_at = GREATEST(
                    meta_inbox_threads.last_customer_at,
                    EXCLUDED.last_customer_at
                ),
                last_message_at = GREATEST(
                    meta_inbox_threads.last_message_at,
                    EXCLUDED.last_message_at
                )
            RETURNING thread_id, human_handling
            """,
            phone,
            sender_name,
            meta_ts,
        )
        thread_id = thread["thread_id"]
        human_handling = thread["human_handling"]

        inbound = await conn.fetchrow(
            """
            INSERT INTO meta_inbox_messages (
                thread_id, meta_message_id, direction, sender_role, body,
                media_type, status, webhook_id
            )
            VALUES ($1, $2, 'inbound', 'customer', $3, $4, 'received', $5)
            ON CONFLICT (meta_message_id) WHERE meta_message_id IS NOT NULL
                DO NOTHING
            RETURNING id
            """,
            thread_id,
            wamid,
            body,
            media_type,
            webhook_id,
        )

        if inbound is not None:
            is_new_inbound = True

            # New inbound AND bot still owns the thread → enqueue a bot reply intent.
            if not human_handling:
                bot_row = await conn.fetchrow(
                    """
                    INSERT INTO meta_inbox_messages (
                        thread_id, direction, sender_role, status
                    )
                    VALUES ($1, 'outbound', 'bot', 'queued')
                    RETURNING id
                    """,
                    thread_id,
                )
                await conn.execute(
                    """
                    INSERT INTO wa_outbox (
                        thread_id, message_id, needs_generation, status,
                        inbound_message_id
                    )
                    VALUES ($1, $2, true, 'pending', $3)
                    ON CONFLICT (message_id) DO NOTHING
                    """,
                    thread_id,
                    bot_row["id"],
                    inbound["id"],
                )
        # else: duplicate inbound (Meta retry) → nothing more to do, transaction
        # commits as a no-op past this point.

    if is_new_inbound and _manners_enabled():
        try:
            await whatsapp_service.mark_message_read(wamid)
        except Exception as r4_exc:
            # Best-effort (C5): a failed read-receipt must never break
            # ingestion or the already-enqueued bot reply.
            logger.warning(
                "meta-inbox: mark_message_read failed for wamid=%s: %s",
                redact_identifier_for_log(wamid),
                type(r4_exc).__name__,
            )


async def _resolve_webhook_id(conn: Any, wamid: str) -> int | None:
    """Best-effort lookup of the inbound_webhooks row id for a wamid."""
    try:
        return await conn.fetchval(
            "SELECT id FROM inbound_webhooks WHERE channel = 'whatsapp' AND dedup_key = $1",
            wamid,
        )
    except Exception:
        return None


_WA_MEDIA_PENDING_CHANNEL = "whatsapp_media_pending"


async def _ingest_meta_inbox_media(raw_payload: dict[str, Any], request: Request) -> None:
    """Background task: hand off official-line media to the sovereign Pro worker.

    PULL architecture (Law 2 — PII never leaves the Pro): Fly does NOT download
    the media here. It enqueues a *metadata-only* hand-off row into
    ``events_outbox`` (channel ``whatsapp_media_pending``) — just the Meta
    ``media_id`` + provenance, never a single byte of the document. A worker on
    the Pro pulls these rows, downloads from Meta itself, and stores the blob on
    the Pro's local disk + local intake_queue. Fly ephemeral storage never
    touches client PII (passports, KTP, akta).

    Runs AFTER the 200 ACK like its sibling, so the webhook is never delayed.
    Best-effort: failures are logged, never raised (the webhook already ACKed;
    Meta will not be impacted — the outbox row, if written, is durable and the
    Pro worker is idempotent on the source_ref it derives).
    """
    # Cheap pre-check: only touch the DB if the payload actually carries media
    # on the official number.
    from backend.channels.whatsapp.media_webhook_parse import parse_media_webhook

    parsed = parse_media_webhook(raw_payload)
    official = [m for m in parsed.media if m.phone_number_id in META_INBOX_PHONE_NUMBER_IDS]
    if not official:
        return

    pool = _get_db_pool(request)
    if pool is None:
        logger.warning("meta-inbox media: no db pool, skipping %d media", len(official))
        return

    from backend.services.events import outbox

    published = 0
    try:
        async with pool.acquire() as conn:
            # One transaction for the whole batch: publish() is atomic with the
            # caller's tx (INSERT + NOTIFY both vanish on rollback). All-or-nothing
            # is fine here — the Pro worker reconciles whatever lands.
            async with conn.transaction():
                for media in official:
                    await outbox.publish(
                        conn,
                        _WA_MEDIA_PENDING_CHANNEL,
                        {
                            "media_id": media.media_id,
                            "mime_type": media.mime_type,
                            "message_type": media.message_type,
                            "filename": media.filename,
                            "declared_sha256": media.declared_sha256,
                            "wa_message_id": media.wa_message_id,
                            "from_phone": media.from_phone,
                            "phone_number_id": media.phone_number_id,
                            "sender_name": media.sender_name,
                        },
                    )
                    published += 1
        logger.info(
            "meta-inbox media handoff: found=%d published=%d (PULL: no download on Fly)",
            len(official),
            published,
        )
    except Exception as exc:
        logger.error("meta-inbox media handoff failed: %s", type(exc).__name__)


async def process_meta_inbox_payload(
    raw_payload: dict[str, Any],
    request: Request | None,
    *,
    db_pool: Any | None = None,
    mark_row: bool = True,
) -> bool:
    """Background task: drive the meta-inbox ledger for the target number only.

    Runs AFTER the webhook has persisted to inbound_webhooks and ACKed 200, so
    it never delays the Meta ACK. Scoped strictly to
    META_INBOX_PHONE_NUMBER_IDS (every phone_number_id known to route to this
    number, not just the canonical one); any other number is ignored here.

    ``db_pool`` lets a caller that has no ``Request`` (the WebhookProcessor
    recovery net, see ``route_whatsapp_recovery``) inject the pool directly
    instead of going through ``_get_db_pool(request)``. The normal webhook
    call site still passes ``request`` and leaves ``db_pool`` unset.

    ``mark_row`` (default True, the webhook/BackgroundTasks call site) marks
    the ``inbound_webhooks`` row processed itself after a message is handled
    without raising (2026-08-25 double-reply scar prong 1). The
    recovery net (``route_whatsapp_recovery``) passes ``mark_row=False``: its
    caller, ``WebhookProcessor._process_one``, already holds that SAME row
    ``FOR UPDATE SKIP LOCKED`` in an open transaction and marks it itself on
    handler success — a second ``mark_processed`` from THIS function, on a
    different pool connection, would block against that open transaction
    until ``statement_timeout``, stalling the shared drain loop for every
    channel (round-2 adversarial-gate finding, proven at SQL level on
    PG 17.10).

    Returns True iff every meta-inbox message in the payload was handled
    without ``_handle_meta_inbox_message`` raising (a parse failure or a
    missing db pool are non-retryable — no payload to retry against — and
    also return True). Returns False on a per-message handler failure, so
    that a ``mark_row=False`` caller can re-raise and let the
    WebhookProcessor's own retry ladder (backoff, MAX_ATTEMPTS, eventual
    "GIVING UP") apply to meta-inbox rows exactly as it does to every other
    channel — this function itself never raises (round-2 finding: silently
    swallowing every exception made those retry semantics dead for
    meta-inbox rows; a handler crash during recovery was permanent, silent
    message loss). The webhook/BackgroundTasks call site (``mark_row=True``)
    ignores the return value and must never see an exception either way —
    unaffected by this change.
    """
    pool = db_pool if db_pool is not None else _get_db_pool(request)
    if pool is None:
        return True

    try:
        webhook = WhatsAppWebhook(**raw_payload)
    except Exception as exc:
        logger.warning("meta-inbox: payload parse failed: %s", type(exc).__name__)
        return True

    try:
        from backend.services.channels import inbound_webhook_repo

        async with pool.acquire() as conn:
            for entry in webhook.entry:
                for change in entry.changes:
                    if change.field != "messages":
                        continue
                    if not _change_belongs_to_meta_inbox(change):
                        continue

                    value = change.value

                    # STATUS branch (delivery receipts) — never touches window.
                    for status_obj in value.get("statuses", []):
                        await _apply_status_callback(conn, status_obj)

                    # MESSAGE branch (inbound customer messages).
                    contacts = value.get("contacts", [])
                    sender_name = None
                    if contacts:
                        sender_name = contacts[0].get("profile", {}).get("name")

                    for msg in value.get("messages", []):
                        wamid = msg.get("id")
                        webhook_id = await _resolve_webhook_id(conn, wamid) if wamid else None
                        await _handle_meta_inbox_message(conn, msg, sender_name, webhook_id)

                        if not mark_row or not wamid:
                            continue
                        try:
                            await inbound_webhook_repo.mark_processed(
                                pool,
                                channel="whatsapp",
                                dedup_key=wamid,
                            )
                        except Exception as r4_exc:
                            logger.warning(
                                "meta-inbox: failed to mark inbound row processed (wamid=%s): %s",
                                redact_identifier_for_log(wamid),
                                type(r4_exc).__name__,
                            )
    except Exception as exc:
        logger.error("meta-inbox: processing failed: %s", type(exc).__name__)
        return False

    return True


# Reason recorded on a persisted ``inbound_webhooks`` row whose destination
# number is neither the meta-inbox nor the team-bot number. The inline reply
# path that used to answer such rows was retired; the row is closed with this
# text so it is neither lost silently nor retried.
LEGACY_PATH_RETIRED_REASON = (
    "skipped: whatsapp number is neither meta-inbox nor team-bot; inline legacy reply path retired"
)


async def _close_retired_legacy_rows(request: Request, message_ids: list[str]) -> None:
    """Mark persisted rows for the retired legacy path processed, with a reason."""
    db_pool = _get_db_pool(request)
    if db_pool is None:
        return

    from backend.services.channels import inbound_webhook_repo

    for message_id in message_ids:
        try:
            await inbound_webhook_repo.mark_processed(
                db_pool,
                channel="whatsapp",
                dedup_key=message_id,
                error_message=LEGACY_PATH_RETIRED_REASON,
            )
        except Exception as exc:
            logger.warning(
                "WhatsApp webhook: failed to close retired-path row (message_id=%s): %s",
                redact_identifier_for_log(message_id),
                type(exc).__name__,
            )


async def route_whatsapp_recovery(
    payload: dict[str, Any],
    *,
    db_pool: Any,
    legacy_route: Any,
) -> str | None:
    """WebhookProcessor recovery-net dispatch for the ``whatsapp`` channel.

    Keeps meta-inbox-targeted payloads out of the ChannelRouter pipeline
    (2026-08-25 double-reply scar prong 2): ``ChannelRouter`` classifies intent
    itself and sends its own reply, never passing through ``wa_reply_claims``,
    so routing a meta-inbox payload there on recovery produced a second,
    different reply.

    Outcomes (the return value is what the WebhookProcessor stores as the
    row's ``error_message`` while marking it processed):

    - payload cannot be fully classified (parse or classification raises):
      handed to ``legacy_route`` (the caller's ChannelRouter) exactly as
      before the legacy inline path was retired, so a deliverable message is
      never discarded on a classification error. Returns ``None``.
    - team-bot changes: handed to the ingress seam (or dropped when its switch
      is off).
    - meta-inbox changes: ``process_meta_inbox_payload`` with
      ``mark_row=False`` (the processor already holds the row ``FOR UPDATE SKIP
      LOCKED`` and marks it itself); a handler failure RE-RAISES so the
      processor's retry ladder applies. It is idempotent per message
      (``wa_reply_claims`` owner comparison + ``ON CONFLICT DO NOTHING``), so
      recovering several rows that carry the same whole payload delivers each
      meta message once.
    - any change that is neither team-bot nor meta-inbox (a number whose
      inline reply path is retired): not handled; the function returns
      ``LEGACY_PATH_RETIRED_REASON``. Every row stores the WHOLE payload, so
      this applies to whichever row is recovered: the unrelated-number row
      closes with the reason, and a meta row of the same payload is handled
      once AND carries the reason too, recording that part of its payload was
      skipped. A payload with only meta/team changes returns ``None``.
    """
    try:
        webhook = WhatsAppWebhook(**payload)
        changes = [change for entry in webhook.entry for change in entry.changes]
        # Team-bot changes are excluded from the meta verdict rather than argued
        # away by set-disjointness: `_change_belongs_to_meta_inbox` has a SECOND
        # signal (display_phone_number fallback, 2026-08-25 scar), so a team-bot
        # change carrying the public number would otherwise set it and send a
        # REMAINDER that is not meta-inbox traffic into the client pipeline.
        team_bot_changes = [
            c for c in changes if c.field == "messages" and _change_belongs_to_team_bot_ingress(c)
        ]
        is_meta_inbox = any(
            c.field == "messages"
            and not _change_belongs_to_team_bot_ingress(c)
            and _change_belongs_to_meta_inbox(c)
            for c in changes
        )
        has_unhandled_change = any(
            not (c.field == "messages" and _change_belongs_to_team_bot_ingress(c))
            and not (c.field == "messages" and _change_belongs_to_meta_inbox(c))
            for c in changes
        )
    except Exception as r4_exc:
        logger.warning(
            "whatsapp recovery: payload could not be classified, using the ChannelRouter route: %s",
            type(r4_exc).__name__,
        )
        await legacy_route(payload)
        return None

    if team_bot_changes:
        if _team_bot_ingress_enabled():
            for change in team_bot_changes:
                await _handle_team_bot_ingress_payload(change)
        else:
            logger.info(
                "whatsapp recovery: team-bot payload recognised but "
                "TEAM_BOT_INGRESS_ENABLED is off — dropping."
            )
        remainder = _payload_without_team_bot_changes(payload)
        if remainder is None:
            return None
        # Mixed payload: the team half is handled (or dropped) above, and the
        # rest MUST still be routed. `is_meta_inbox` was computed with team-bot
        # changes excluded, so it describes the remainder.
        logger.info(
            "whatsapp recovery: mixed payload — team-bot changes handled, "
            "routing the remaining message change(s) onward."
        )
        payload = remainder

    if is_meta_inbox:
        success = await process_meta_inbox_payload(
            raw_payload=payload, request=None, db_pool=db_pool, mark_row=False
        )
        if not success:
            raise RuntimeError(
                "meta-inbox recovery: process_meta_inbox_payload reported a "
                "handler failure — re-raising so the WebhookProcessor's own "
                "retry ladder applies"
            )
        return LEGACY_PATH_RETIRED_REASON if has_unhandled_change else None

    logger.info(
        "whatsapp recovery: payload targets neither the meta-inbox nor the "
        "team-bot number — closing the row, legacy path retired."
    )
    return LEGACY_PATH_RETIRED_REASON


@router.get("")
async def verify_webhook(request: Request) -> PlainTextResponse:
    """
    Verify webhook for Meta WhatsApp setup.

    Meta sends GET request with hub.mode, hub.verify_token, hub.challenge.
    We must return hub.challenge if verify_token matches.
    """
    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    logger.info("WhatsApp webhook verification requested")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        logger.info("✅ Webhook verification successful")
        return PlainTextResponse(content=challenge, status_code=200)

    logger.warning("❌ Webhook verification failed: invalid token or mode")
    raise HTTPException(status_code=403, detail="Invalid verify token")


def _verify_whatsapp_signature(body: bytes, signature_header: str | None) -> bool:
    """Verify X-Hub-Signature-256 HMAC-SHA256 from Meta WhatsApp webhook.

    Thin bool-returning wrapper over the shared, tested
    ``backend.security.webhook_verifier.verify_meta_hmac`` — the actual HMAC
    comparison lives there, once, shared with the Instagram router. This
    function's name and its ``(body, signature_header) -> bool`` contract
    are load-bearing: callers in this module, and every test in
    ``backend/tests/duebot/`` and ``backend/tests/channels/``, depend on
    both — do not rename it or change the return type.

    Args:
        body: Raw request body bytes
        signature_header: Value of X-Hub-Signature-256 header (format: "sha256=<hex>")

    Returns:
        True if the signature is valid, or verification was skipped because
        no app secret is configured (dev mode) AND
        ``settings.meta_webhook_require_signature`` is False. False on any
        verification failure, including a missing secret when
        ``meta_webhook_require_signature`` is True (the default since
        2026-08-26 — fail-closed by default on both WhatsApp and Instagram).
    """
    app_secret = settings.whatsapp_app_secret
    if not app_secret and not settings.meta_webhook_require_signature:
        # Fail-open is still the configured policy and no secret exists —
        # skip verification (dev mode), but LOUDLY: a production deploy
        # must never sit in this state quietly (F-class landmine, see
        # research/operations/2026-07-17-mutating-routes-authz-ledger.md).
        logger.warning(
            "⚠️ WhatsApp webhook: WHATSAPP_APP_SECRET not configured — "
            "signature verification SKIPPED (fail-open). Set "
            "WHATSAPP_APP_SECRET to enable it, or "
            "META_WEBHOOK_REQUIRE_SIGNATURE=true to reject instead."
        )
        return True

    try:
        verify_meta_hmac(
            body,
            signature_header,
            app_secret,
            provider="whatsapp",
            require_secret=settings.meta_webhook_require_signature,
        )
    except WebhookVerificationError as exc:
        logger.warning("⚠️ WhatsApp webhook signature verification failed: %s", exc.reason)
        return False
    return True


@router.post("")
async def whatsapp_webhook(
    background_tasks: BackgroundTasks,
    request: Request,
) -> dict[str, Any]:
    """
    Handle incoming WhatsApp messages — ack-first pattern (P0-6 audit 2026-04-29).

    Meta sends POST requests with message events.
    Verifies X-Hub-Signature-256 HMAC if WHATSAPP_APP_SECRET is configured.

    Flow:
      1. Verify HMAC signature (synchronous, fast).
      2. Parse + persist payload to ``inbound_webhooks`` (idempotent on
         Meta message_id via UNIQUE(channel, dedup_key)).
      3. Schedule fast-path processing via FastAPI BackgroundTasks (existing
         behaviour).
      4. Return 200 OK in <200ms — guaranteed by virtue of (1)+(2)+(3) all
         being O(1) async DB ops.

    The WebhookProcessor (services/channels/webhook_processor.py) drains
    any rows that the fast path missed (Fly machine crash mid-handler,
    handler exception, etc.) so no inbound message is silently lost.
    """
    # HMAC-SHA256 signature verification
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _verify_whatsapp_signature(body, signature):
        logger.warning("WhatsApp webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse body into webhook model
    try:
        raw_payload = json.loads(body)
        webhook = WhatsAppWebhook(**raw_payload)
    except Exception as e:
        logger.error("WhatsApp webhook body parse error: %s", type(e).__name__)
        raise HTTPException(status_code=400, detail="Invalid request body")

    logger.info("Webhook received: %s entries", len(webhook.entry))

    # ── Ack-first persistence (P0-6) ──
    # Persist the verified payload BEFORE any business processing so a Fly
    # machine crash mid-flight does not lose the webhook. Dedup key is the
    # Meta-provided message_id (one row per inbound DM, even if Meta retries
    # the webhook before our 200 OK lands).
    db_pool = _get_db_pool(request)
    persisted_message_inserted: dict[str, bool] = {}
    if db_pool is not None:
        for entry in webhook.entry:
            for change in entry.changes:
                if change.field != "messages":
                    continue
                for msg in change.value.get("messages", []):
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    try:
                        from backend.services.channels import inbound_webhook_repo

                        _, inserted = await inbound_webhook_repo.persist(
                            db_pool,
                            channel="whatsapp",
                            dedup_key=msg_id,
                            payload=raw_payload,
                            recovery_after_seconds=WHATSAPP_FAST_PATH_RECOVERY_DELAY_SECONDS,
                        )
                        persisted_message_inserted[msg_id] = inserted
                    except Exception as exc:
                        logger.warning(
                            "WhatsApp webhook: persist failed (message_id=%s): %s; falling back to BackgroundTasks-only path",
                            redact_identifier_for_log(msg_id),
                            type(exc).__name__,
                        )

    # WA Meta Inbox: if any change targets the dedicated Business number, run
    # its ledger persistence in a single background task (after ACK, so the
    # 200 is never delayed). The target number does NOT use the inline triage
    # flow below — its bot replies are generated by the wa_outbox worker.
    meta_inbox_in_payload = any(
        change.field == "messages" and _change_belongs_to_meta_inbox(change)
        for entry in webhook.entry
        for change in entry.changes
    )
    if meta_inbox_in_payload:
        background_tasks.add_task(
            process_meta_inbox_payload,
            raw_payload=raw_payload,
            request=request,
        )
        # Anello 1: pull any media attachments on the official line into the
        # intake pipeline (download → enqueue). Separate task so a media
        # download never delays the ledger or the Meta ACK.
        background_tasks.add_task(
            _ingest_meta_inbox_media,
            raw_payload=raw_payload,
            request=request,
        )

    for entry in webhook.entry:
        for change in entry.changes:
            if change.field != "messages":
                logger.debug("Ignoring non-message change")
                continue

            # Team-bot number (2026-08-26 same-WABA finding) — checked
            # BEFORE the meta-inbox branch below, on purpose: this number
            # matches neither _change_belongs_to_meta_inbox's id check nor
            # its display_phone_number fallback (it is a genuinely
            # different number), so without this branch it would fall all
            # the way through into the legacy inline triage flow and a
            # staff message would be processed as a client message.
            # Recognised regardless of the kill switch below — only whether
            # the recognised change is handed to the ingress seam or safely
            # dropped depends on that flag; either way it `continue`s here
            # and never reaches the meta-inbox check or the legacy flow.
            if _change_belongs_to_team_bot_ingress(change):
                if _team_bot_ingress_enabled():
                    background_tasks.add_task(_handle_team_bot_ingress_payload, change)
                else:
                    logger.info(
                        "Team-bot webhook message recognised but "
                        "TEAM_BOT_INGRESS_ENABLED is off — dropping."
                    )
                continue

            # Target Business number → handled by the meta-inbox task above.
            # _change_belongs_to_meta_inbox checks BOTH phone_number_id and
            # display_phone_number (2026-08-25 double-reply scar), so neither
            # an unlisted id nor a same-number resubscribe is mistaken for a
            # number that has no handler.
            if _change_belongs_to_meta_inbox(change):
                continue

            # Neither meta-inbox nor team-bot: the inline reply path that used
            # to answer these numbers is retired. The row persisted above is
            # closed with an explicit reason instead of being left pending,
            # which would hand it to the WebhookProcessor for endless retries.
            retired_ids = [
                msg_id
                for msg in change.value.get("messages", [])
                if (msg_id := msg.get("id")) and persisted_message_inserted.get(msg_id) is not None
            ]
            if retired_ids:
                logger.info(
                    "WhatsApp webhook: %d message(s) for a number with no handler — closing with reason",
                    len(retired_ids),
                )
                background_tasks.add_task(_close_retired_legacy_rows, request, retired_ids)

    # Record webhook metric
    try:
        from backend.app.metrics import metrics_collector

        metrics_collector.record_webhook_request(channel="whatsapp", status="success")
    except Exception:
        pass  # Metrics must never break webhook processing

    return {"status": "ok"}


@router.get("/status")
async def whatsapp_status() -> dict[str, Any]:
    """Check WhatsApp integration status."""
    configured = bool(settings.whatsapp_api_token and settings.whatsapp_phone_number_id)

    return {
        "configured": configured,
        "phone_number_id": settings.whatsapp_phone_number_id if configured else None,
        "triage_enabled": True,
        "personal_contacts_count": len(whatsapp_triage_service.personal_contacts),
        "ai_model": settings.whatsapp_openclaw_model,
        "ai_runtime": "openclaw_cli",
        "ai_thinking": settings.whatsapp_openclaw_thinking,
        "ai_persona": settings.whatsapp_openclaw_persona,
        "ai_autonomy_mode": settings.whatsapp_openclaw_autonomy_mode,
        "persona": settings.whatsapp_openclaw_persona,
    }
