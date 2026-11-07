"""Draft one reply, via the `claude` CLI. Never sends.

Model access rule (Builder Contract 3 + RULED 2026-11-01, `docs/rules/RULINGS.md`):
the Anthropic SDK is BANNED in this repo, and a product lane reaches a Claude
model only through a Console seat — `ANTHROPIC_CONSOLE_KEY_<n>` pinned to
`ANTHROPIC_CONSOLE_WORKSPACE_<n>`, the three linked-org keys — never through plan
OAuth (`CLAUDE_CODE_OAUTH_TOKEN`) or the CLI's own session. The transport is
still the `claude` CLI; the seats and the child's environment come from
`backend.llm.claude_oauth_client`. Do not "simplify" this into an SDK call.

PII posture. To draft a reply the model has to read the client's message — there
is no way around that — so the safeguard is applied to WHAT it reads, not to
whether it reads: hard identifiers (passport, NPWP, NIK, phone, e-mail, amounts)
are redacted before the prompt is built. None of them are needed to write a
reply, and their removal is the difference between "the model saw a question
about a KITAS renewal" and "the model saw a passport number". Transit of the
remaining prose is lawful under UU PDP Art. 56 with the Workspace DPA in place
(CLAUDE.md §14, 2026-06-20 amendment); the raw message itself is never written
to our disk.

Regulatory posture is carried IN the prompt, because the model is what writes
the sentence: no invented KBLI code, visa type, tax rate, capital threshold or
deadline; no improvised price; anything uncertain is deferred to the team rather
than guessed. This mirrors the persona rules that the WhatsApp bridge learned the
hard way across W68/W72/W73/W77 — and the reason those live in the prompt rather
than in a post-hoc guard is that a guard which rewrites a correct answer is its
own failure mode.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

from backend.llm.claude_oauth_client import build_seat_env, collect_seats
from backend.services.mail_loop.style import redact

logger = logging.getLogger(__name__)

CLAUDE_MODEL = "claude-sonnet-5"
TIMEOUT_SECONDS = 180
MAX_BODY_CHARS = 6000

_LANGUAGE_NAME = {
    "en": "English",
    "it": "Italian",
    "id": "Bahasa Indonesia",
    "ru": "Russian",
}

_SYSTEM_RULES = """\
You are drafting an e-mail reply on behalf of Bali Zero, an Indonesian
business-services agency (immigration/KITAS, PT PMA setup, tax, property) whose
clients are expat founders, investors and immigrants in Bali.

You are writing a DRAFT. A human reads it and presses send. Never write as if
the message is already sent, and never promise that something has been filed,
paid or submitted.

HARD RULES — a violation makes the draft unusable:
1. Never invent or guess a regulation, KBLI code, visa type, tax rate, capital
   requirement or legal deadline. If you are not certain, say plainly that the
   team will confirm the specific figure. Do not approximate a number to sound
   helpful.
2. Never quote or break down a price. Bali Zero prices are all-inclusive and
   come only from official pricing. Point the client at the official quotation
   instead.
3. State STABLE PUBLISHED FACTS directly — what a visa type is, how two permits
   differ, that working in Indonesia requires a work permit. Deferring a
   textbook fact to "let me check with the team" reads as evasive and is its own
   error. Defer only the client-specific parts: their eligibility, their
   timeline, their filing status, their price.
4. Keep Bahasa terms verbatim: KITAS, KITAP, PT PMA, KBLI, NIB, NPWP, hak pakai,
   hak milik, LKPM. Never translate them.
5. No emoji. No exclamation marks. No hospitality-brochure warmth.
6. Never state or imply that a nominee arrangement (an Indonesian holding title
   on a foreigner's behalf) is merely risky: under Indonesian agrarian law it is
   illegal and the arrangement is void.

Write only the body of the reply. No subject line, no headers, no commentary
about your own reasoning, no markdown fences.
"""


@dataclass(frozen=True)
class DraftRequest:
    """Everything needed to write one reply."""

    subject: str
    body: str
    sender_name: str
    language: str
    intent: str
    style_block: str


def build_prompt(request: DraftRequest) -> str:
    """Assemble the prompt. Pure function so the contract is testable.

    The client's message is redacted here, not by the caller: putting the
    redaction at the boundary that builds the prompt means there is exactly one
    place where prose can leak, and it is the place a reviewer will look.
    """
    safe_body = redact(request.body or "")[:MAX_BODY_CHARS]
    safe_subject = redact(request.subject or "")
    language = _LANGUAGE_NAME.get(request.language, "English")
    salutation_name = (request.sender_name or "").split("@")[0].strip() or "the client"

    return f"""{_SYSTEM_RULES}

Reply in {language}. The client wrote in that language; match it.

Topic lane: {request.intent}

LEARNED STYLE — how Zero actually answers this kind of mail. These lines were
derived by comparing earlier drafts against what he really sent. Follow them
over your own instincts:
{request.style_block}

--- CLIENT MESSAGE (identifiers already redacted; do not ask about the tokens) ---
From: {salutation_name}
Subject: {safe_subject}

{safe_body}
--- END CLIENT MESSAGE ---

Draft the reply body now."""


class DraftUnavailable(RuntimeError):
    """The CLI could not produce a draft. Caller decides whether that is fatal."""


def _resolve_cli() -> str:
    explicit = os.environ.get("CLAUDE_CLI_PATH")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    # A login shell finds it; a launchd cron often does not. Say which, because
    # "claude: not found" from inside a cron is the least informative failure in
    # this repo's history.
    raise DraftUnavailable(
        "claude CLI not on PATH. Set CLAUDE_CLI_PATH to the absolute path — "
        "a launchd job does not inherit a login shell's PATH."
    )


def generate(request: DraftRequest, *, dry_run: bool = False) -> str:
    """Produce the reply body, or raise DraftUnavailable.

    Failure is loud on purpose. A loop that silently files mail without ever
    managing to draft anything would keep reporting success while doing half its
    job — the exact shape of superscar #2.
    """
    prompt = build_prompt(request)

    if dry_run:
        logger.info(
            "draft: DRY-RUN, not calling the model (intent=%s lang=%s prompt=%d chars)",
            request.intent,
            request.language,
            len(prompt),
        )
        return (
            "[DRY-RUN] no model call was made. "
            f"Prompt was {len(prompt)} chars for lane {request.intent}/{request.language}."
        )

    cli = _resolve_cli()
    seats = collect_seats()
    if not seats:
        # Fail closed. Until RULED 2026-11-01 this was a WARNING that let the
        # CLI run on its own OAuth session when no token variable was set —
        # and that session is the interactive seat's plan quota, the exact
        # door the ruling shuts for the product perimeter. So "no seat" is no
        # longer a proxy for "cannot authenticate": it IS the outcome, and the
        # mail-loop wrapper has to carry the seat pair (Pro keeps them in
        # ~/nuzantara/.secrets/, 0600). The SDK ban is untouched: the only
        # path below is still the CLI.
        raise DraftUnavailable(
            "no Console seat configured: export ANTHROPIC_CONSOLE_KEY_<n> and "
            "ANTHROPIC_CONSOLE_WORKSPACE_<n> in the mail-loop environment "
            "(no plan-OAuth fallback, RULED 2026-11-01)"
        )

    # The prompt goes in on STDIN, never in argv.
    #
    # It contains the client's message: their name in the salutation, their
    # address in the From line, whatever they wrote. A process's command line is
    # world-readable on macOS — `ps -A -ww -o args` returns the full argv of
    # every process on the box regardless of owner — so passing it as `-p
    # <prompt>` published a client's mail to every account on the machine for
    # the length of the call, up to TIMEOUT_SECONDS.
    #
    # That is the same threat model as the two log lines cured beside this, with
    # a strictly larger payload: the logs leaked a subject, this leaked the whole
    # message. Found by adversarial review, which measured it rather than
    # asserting it — 299 processes owned by other users had readable argv on this
    # machine at the time.
    #
    # `claude -p` with no argument reads the prompt from stdin, so this is the
    # same invocation, minus the exposure. SYMBIOSIS Law 2 / UU PDP Art. 67-68.
    # Lean (2026-11-03): no hooks, no CLAUDE.md discovery, no tool schemas —
    # a draft is text in, text out (see backend.llm.claude_oauth_client).
    argv = [cli, "-p", "--bare", "--tools", "", "--model", CLAUDE_MODEL]
    last_failure = "no seat was attempted"
    for seat in seats:
        try:
            # No pipeline, no shell: the exit code must reach us intact. Piping
            # this into anything is how an rc gets masked (W97). The child's
            # environment is the seat's and nothing else (one key, one
            # workspace header, sterile config dir — see build_seat_env).
            completed = subprocess.run(  # argv list, never a shell string
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
                check=False,
                env=build_seat_env(seat),
            )
        except subprocess.TimeoutExpired as exc:
            raise DraftUnavailable(
                f"claude CLI timed out after {TIMEOUT_SECONDS}s via {seat.label}"
            ) from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:400]
            last_failure = (
                f"claude CLI exited {completed.returncode} via {seat.label}: "
                f"{stderr or '<no stderr>'}"
            )
            logger.warning(
                "draft: %s exited %d — trying the next seat", seat.label, completed.returncode
            )
            continue

        text = (completed.stdout or "").strip()
        if not text:
            last_failure = f"claude CLI exited 0 but produced no text via {seat.label}"
            logger.warning("draft: %s produced no text — trying the next seat", seat.label)
            continue

        return _strip_fences(text)

    raise DraftUnavailable(last_failure)


def _strip_fences(text: str) -> str:
    """Remove a markdown fence the model may have wrapped the body in."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return stripped
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()
