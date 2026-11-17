"""The one place that tells a human a WhatsApp conversation needs them.

Extracted from ``backend/app/routers/whatsapp_chat.py`` on 2026-08-11 with its
behaviour unchanged, so that a SECOND caller can reach it.

Why it had to move. `whatsapp_chat.py` is a FastAPI router; the other WhatsApp
surface — the inbox bot behind `wa_outbox_worker` — is a background service.
Today only the router escalates: it captures the persona's ``[ESCALATE]``
marker, strips it, sends the answer and then calls this function.
``wa_inbox_bot.py`` strips the same marker under a comment that says *"mirror
whatsapp_chat.py"* and calls nothing — it mirrored the FORM and not the EFFECT.
One notifier both paths import is the cure for that class; a second copy is how
the two paths drifted apart in the first place.

Measured before moving, so nobody defends this refactor with a breakage that
does not exist: a direct service→router import is **not** circular today
(importing `wa_inbox_bot` then `whatsapp_chat` in one interpreter succeeds).
The reason to extract is layering and drift, not an ImportError.

Three things the WA-inbox trigger added on 2026-08-11, each because the lane is
only worth having if the caller can tell what actually happened:

* **It returns whether Telegram ACCEPTED the message.** The send used to be
  wrapped in ``except Exception`` returning ``None``, so a caller could not tell
  "a human was told" from "the telling died". The boolean is named for what it
  proves and no more: **``True`` means Telegram accepted a message**, not that a
  person is on shift, has read it, or owns it. Client-facing copy may say the
  request was flagged; it may never promise a reply on this boolean.
* **The body can be withheld.** ``message_text=None`` sends the masked phone,
  the reason and the thread reference, and says the body was deliberately left
  out — the WA-inbox trigger uses it, because routing a new trigger through the
  cleartext payload would widen exactly the third-party exposure CLAUDE.md §14
  constrains. The router keeps passing the body; that behaviour is unchanged and
  pinned by a test.
* **One notification per thread per window.** Without it a client sending ten
  out-of-domain messages produces ten alerts. In-process TTL map, and it fails
  **open** — a duplicate alert is a nuisance, a suppressed one is the disease.
  Declared limit: the API runs on more than one Fly machine, so the real bound
  is one-per-process-per-window, not exactly one.

Still deliberately unchanged: the phone is masked but the ROUTER's payload still
carries the client's full message and last six turns in cleartext.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from backend.security.pii_log_identifier import redact_identifier_for_log

logger = logging.getLogger(__name__)

#: How long a thread stays "already escalated" for dedup purposes.
ESCALATION_DEDUP_WINDOW_S = 30 * 60

#: thread_ref -> monotonic timestamp of the last accepted notification.
#: Bounded by _prune_dedup below so a long-lived process cannot grow it forever.
_recent_escalations: dict[str, float] = {}
_DEDUP_MAX_ENTRIES = 2048


def _prune_dedup(now: float) -> None:
    """Drop entries older than the window; hard-cap the map as a backstop."""
    stale = [k for k, ts in _recent_escalations.items() if now - ts >= ESCALATION_DEDUP_WINDOW_S]
    for k in stale:
        _recent_escalations.pop(k, None)
    if len(_recent_escalations) > _DEDUP_MAX_ENTRIES:
        # Oldest-first eviction. Reaching this means the window is mis-sized or
        # traffic exploded; either way, dropping the oldest is the safe side —
        # it can only cause an EXTRA alert, never a suppressed one.
        for k, _ in sorted(_recent_escalations.items(), key=lambda kv: kv[1])[:512]:
            _recent_escalations.pop(k, None)


def _already_escalated(thread_ref: str | None) -> bool:
    """True if this thread was notified inside the window.

    Fails OPEN on any surprise: an exception here must never be the reason a
    human was not told.
    """
    if not thread_ref:
        return False
    try:
        now = time.monotonic()
        _prune_dedup(now)
        last = _recent_escalations.get(thread_ref)
        return last is not None and (now - last) < ESCALATION_DEDUP_WINDOW_S
    # Broad on purpose: dedup is a convenience, and no failure of it may ever be
    # the reason a human was not told.
    except Exception as exc:
        logger.warning("escalation dedup check failed, notifying anyway: %s", exc)
        return False


async def notify_human_telegram(
    phone: str,
    message_text: str | None = None,
    sender_name: str | None = None,
    reason: str = "personal_contact",
    client_profile: dict | None = None,
    conversation_history: list[dict] | None = None,
    thread_ref: str | None = None,
    db_pool: Any | None = None,
) -> bool:
    """Compatibility wrapper forwarding WhatsApp human escalations to Brevo + kita bell.

    Telegram is retired (2026-11-07). Retains the legacy parameter list so
    remaining callers do not break while discarding free-form PII fields
    (``message_text``, ``sender_name``, ``conversation_history``) and routing
    solely through ``wa_human_handoff.notify_human_handoff``.
    """
    from backend.services.integrations.wa_human_handoff import notify_human_handoff

    del message_text, sender_name, conversation_history
    raw_lang = (client_profile or {}).get("detected_language") if client_profile else None
    language = raw_lang if isinstance(raw_lang, str) and raw_lang.strip() else "en"
    logger.info(
        "WhatsApp human escalation routed via wa_human_handoff for %s (reason=%s)",
        redact_identifier_for_log(phone),
        reason,
    )
    return await notify_human_handoff(
        db_pool,
        thread_id=thread_ref,
        counterpart_phone=phone,
        language=language,
        reason=reason,
    )


notify_human_escalation = notify_human_telegram
