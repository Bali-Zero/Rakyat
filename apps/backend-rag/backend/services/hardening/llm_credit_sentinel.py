"""LLMCreditSentinel — catch a depleted LLM credit balance in minutes, not days.

Three times in one week the LLM prepayment credits hit zero and every LLM
call started failing with quota exhaustion. Because the agentic path is
what *decides which collections to search*, a dead LLM means
``collections_queried = []`` → zero chunks → the RAG abstains on everything →
the outbox worker treats the abstention as a failure and burns its five retries
→ the person on WhatsApp gets pure silence. On 2026-07-28 that ran for ~34
hours before anyone noticed, with the whole team testing against a mute bot.

This sentinel makes the smallest possible LLM call on a schedule (via the
Claude Console ``hardening.credit_sentinel`` lane) and classifies the outcome.
It alerts ONLY on a genuine credit/quota depletion.

Why the classifier is narrow on purpose
---------------------------------------
A transient per-minute rate limit is NOT depletion — it is self-healing.
Alerting on every rate limit would train everyone to ignore the alarm, so
:func:`classify_probe_error` distinguishes transient rate limits from
positively-identified credit/quota depletion (both Claude Console structured
``Last error class: ... quota`` tails and legacy billing depletion strings).
Anything it cannot positively identify is ``UNKNOWN``, which is logged and
never alerted: an unreachable API at 3am is not a billing event.
(Same doctrine as ``token_watchdog``: we don't cry wolf.)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Depletion and rate-limit signatures across Claude Console structured error
# tails ("Last error class: console_N: <class>") and legacy provider messages.
_CONSOLE_RATE_LIMIT_RE = re.compile(
    r"last\s+error\s+class:.*?\b(?:rate[\s_]+limited|rate_limit)\b",
    re.IGNORECASE,
)
_DEPLETION_RE = re.compile(
    r"prepayment\s+credits?\s+are\s+depleted"
    r"|billing\s+account.*(?:closed|disabled)"
    r"|has\s+been\s+depleted"
    r"|credit\s+balance\s+is\s+too\s+low"
    r"|last\s+error\s+class:.*?\bquota\b",
    re.IGNORECASE,
)
_RESOURCE_EXHAUSTED_RE = re.compile(
    r"RESOURCE_EXHAUSTED|\b429\b|\brate[\s_]+limit(?:ed)?\b",
    re.IGNORECASE,
)


class CreditState(str, Enum):
    """What the probe could establish. UNKNOWN never alerts."""

    OK = "ok"
    DEPLETED = "depleted"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CreditVerdict:
    """Outcome of one probe."""

    state: CreditState
    detail: str = ""

    @property
    def should_alert(self) -> bool:
        """Only a positively-identified depletion is worth waking anyone for."""
        return self.state is CreditState.DEPLETED


def classify_probe_error(exc: BaseException) -> CreditVerdict:
    """Map a failed probe onto a verdict.

    Depletion requires a positively-identified billing/quota depletion signal;
    a transient rate-limit signal on its own maps to RATE_LIMITED.
    """
    text = f"{type(exc).__name__}: {exc}"
    if _CONSOLE_RATE_LIMIT_RE.search(text):
        return CreditVerdict(CreditState.RATE_LIMITED, text[:400])
    if _DEPLETION_RE.search(text):
        return CreditVerdict(CreditState.DEPLETED, text[:400])
    if _RESOURCE_EXHAUSTED_RE.search(text):
        return CreditVerdict(CreditState.RATE_LIMITED, text[:400])
    return CreditVerdict(CreditState.UNKNOWN, text[:400])


ProbeFn = Callable[[], Awaitable[str]]
NotifyFn = Callable[[str], Awaitable[bool]]


class LLMCreditSentinel:
    """Probe the LLM, and on a confirmed depletion notify every channel given.

    Notifiers are injected so this module stays free of provider HTTP, and so
    the tests can assert on what would have been sent. Every notifier is tried
    even if an earlier one fails: an alert that reaches one channel out of two
    is still an alert, and a WhatsApp send is expected to fail whenever the
    Meta 24-hour window happens to be closed.
    """

    def __init__(self, probe: ProbeFn, notifiers: dict[str, NotifyFn]) -> None:
        self._probe = probe
        self._notifiers = notifiers

    async def check(self) -> CreditVerdict:
        try:
            reply = await self._probe()
        except BaseException as exc:  # broad on purpose: classifying, not handling
            verdict = classify_probe_error(exc)
        else:
            verdict = CreditVerdict(CreditState.OK, str(reply)[:120])

        if not verdict.should_alert:
            logger.info(
                "🔎 [LLMCreditSentinel] state=%s (nessun allarme) — %s",
                verdict.state.value,
                verdict.detail[:160],
            )
            return verdict

        logger.error("🚨 [LLMCreditSentinel] CREDITO ESAURITO — %s", verdict.detail[:300])
        for channel, notify in self._notifiers.items():
            try:
                ok = await notify(ALERT_TEXT)
                logger.info(
                    "🚨 [LLMCreditSentinel] alert su %s: %s",
                    channel,
                    "consegnato" if ok else "NON consegnato",
                )
            except Exception as exc:  # one dead channel must not mute the others
                logger.warning("🚨 [LLMCreditSentinel] alert su %s fallito: %s", channel, exc)
        return verdict


ALERT_TEXT = (
    "🚨 ZANTARA MATI — kredit Claude Console habis.\n\n"
    "Semua jawaban bot berhenti: RAG tidak bisa mencari, jadi tidak ada "
    "jawaban sama sekali (bukan jawaban salah — tidak ada balasan).\n\n"
    "Yang harus dilakukan: cek kuota & kredit organisasi Anthropic Console "
    "(atau AI Studio fallback) untuk project *Nuzantara*.\n\n"
    "Selama belum diisi, jangan minta tim tes bot: pertanyaan mereka hilang."
)
