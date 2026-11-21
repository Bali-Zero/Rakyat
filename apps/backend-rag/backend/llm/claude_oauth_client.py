"""Claude via the Console door (subprocess to ``claude -p``).

The headless client of the product perimeter. RULED 2026-10-26 / 2026-11-01
(``docs/rules/RULINGS.md``): the three Ultra plans are linked to three Console
orgs, and a product lane reaches a Claude model ONLY through a linked-org key
pinned to its workspace — never through plan OAuth (``CLAUDE_CODE_OAUTH_TOKEN``,
the interactive seats' credential) and never through a keychain session.
RULED 2026-11-02 consequence (c): this landing drained the plan-OAuth baseline
(``.github/workflows/catE-plan-oauth-product-baseline.txt``) to zero.

Seats: ``ANTHROPIC_CONSOLE_KEY_<n>`` + ``ANTHROPIC_CONSOLE_WORKSPACE_<n>`` for
``n`` in 1..3, tried in order (see :func:`collect_seats`). The CLI is still the
transport — the Python SDK stays out of this repo (Builder Contract 3) — so the
child receives ``ANTHROPIC_API_KEY`` plus the ``anthropic-workspace-id`` header
via ``ANTHROPIC_CUSTOM_HEADERS`` (measured on Pro 2026-11-02: a user-scoped key
answers 400 without the header, 200 with it, ``is_error`` with the wrong one).

Lean by construction (2026-11-03, measured on Pro with a Console seat): every
call runs ``--bare --tools ""`` — no hooks, no CLAUDE.md discovery, no tool
schemas, no auto-memory — and ``--output-format json``. The default CLI prefix
cost a 18.6K-token cache write per cold call ($0.023 on Haiku 4.5); bare and
toolless the same "PONG" costs $0.0003. The JSON envelope carries the REAL
usage (cache reads and writes included) and the model that actually answered,
which is what the cost ledger records.

Kept intentionally minimal — no streaming, no tool-use — because every backend
consumer (``article_composer``, ``coreference``, ``mail_loop.draft``, …)
does the same thing: one-shot prompt → one-shot
completion. Module and class names keep their ``OAuth`` spelling on purpose:
they are the public API of a dozen call sites and the ledger history; only the
credential behind them changed.

Never import this from a module that also imports ``anthropic``. The whole
point is to not need the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)


def _start_claude_cli_span(model_name: str, prompt_len_tokens: int) -> Any:
    """Open an OTEL span for the upcoming `claude` CLI invocation.

    This is the only LLM path in the codebase that's not patchable by an
    OpenInference instrumentor (it shells out to a CLI). The span flows
    to the same Langfuse project as everything else because the Langfuse
    SDK registers itself as the global OTEL tracer provider during
    `init_observability()`. When OTEL is not installed or no provider is
    registered, this returns a `nullcontext` and emits nothing — the
    function is purely additive.

    Returns a context manager. Callers should:
        with _start_claude_cli_span(...) as span:
            ...subprocess work...
            span.set_attribute("gen_ai.usage.output_tokens", N)  # if span
    """
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("nuzantara.claude_oauth")
        return tracer.start_as_current_span(
            "claude_cli.invoke",
            attributes={
                # OTEL gen_ai semantic conventions:
                # https://opentelemetry.io/docs/specs/semconv/gen-ai/
                "gen_ai.system": "claude-cli",
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": model_name,
                # Estimated tokens (CLI doesn't report metadata).
                "gen_ai.usage.input_tokens": prompt_len_tokens,
            },
        )
    except Exception:
        return nullcontext()


DEFAULT_TIMEOUT_S: Final[int] = 120
DEFAULT_TOTAL_TIMEOUT_S: Final[int] = 300
RATE_LIMIT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"rate.limit|too many requests|429|exhausted|quota|hit your limit|capacity|overloaded",
    re.IGNORECASE,
)
_MAX_DIAGNOSTIC_CHARS: Final[int] = 512
_MAX_DIAGNOSTIC_LINES: Final[int] = 6
_DIAGNOSTIC_FRAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^(?:"
    r"(?:error|fatal)(?:\[[^\]]+\])?\s*[:\-]|"
    r"you(?:'ve| have)\s+hit\s+(?:your\s+)?(?:weekly|usage)\s+limit\b|"
    r"(?:weekly|usage)\s+(?:limit|quota)\s+(?:reached|exceeded|exhausted)\b|"
    r"quota\s+(?:exceeded|exhausted)\b|"
    r"(?:http(?:\s+status)?\s*)?401(?:\s|:|$)|"
    r"unauthorized\b|"
    r"auth(?:entication)?\s+(?:failed|error|required|expired)\b|"
    r"oauth\s+(?:failed|error|required|expired)\b|"
    r"token_revoked\b|"
    r"refresh_token(?:_reused|_revoked|_expired)?\b"
    r")",
    re.IGNORECASE,
)
_AUTH_DIAGNOSTIC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    r"token_revoked|refresh_token(?:_reused|_revoked|_expired)?|"
    r"401|unauthorized|"
    r"auth(?:entication)?\s+(?:failed|error|required|expired)|"
    r"oauth\s+(?:failed|error|required|expired)"
    r")\b",
    re.IGNORECASE,
)
_QUOTA_DIAGNOSTIC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    r"out of extra usage|"
    r"(?:weekly|usage)\s+(?:limit|quota)|"
    r"quota\s+(?:exceeded|exhausted)"
    r")\b",
    re.IGNORECASE,
)
# Console door refusals, classified by FULL MATCH against the measured shapes
# (Pro, 2026-11-02/03, `claude -p` with a Console seat) and nothing looser.
# The general frame/keyword patterns above are byte-identical to origin/main:
# three gate rounds (2026-11-03) showed that every widening of them — an "API
# Error:" frame, a bare "Invalid API key" frame, a model-access phrase —
# turned some ordinary answer that begins with the same words into a 15-minute
# cooldown of a healthy seat. A full match cannot be extended by prose: the
# text must BE the CLI's message (after whitespace collapse), not start with it.
_CONSOLE_DIAGNOSTICS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # Bad key, measured: "Invalid API key · Fix external API key".
    (
        re.compile(r"invalid api key(?:\s*[·•\-]\s*fix external api key)?\.?", re.IGNORECASE),
        "authentication",
    ),
    # User-scoped key sent without its workspace header, measured: "API Error:
    # 400 This API key is not scoped to a workspace, so this request must
    # include the anthropic-workspace-id header with the ID of the workspace
    # to use. Add the header, or use an API key that is scoped to a workspace."
    (
        re.compile(
            r"api error:\s*400\s+this api key is not scoped to a workspace, so this request must "
            r"include the anthropic-workspace-id header with the id of the workspace to use\."
            r"(?:\s*add the header, or use an api key that is scoped to a workspace\.)?",
            re.IGNORECASE,
        ),
        "authentication",
    ),
    # The API's own JSON authentication refusal relayed by the CLI. Unmeasured
    # wording around it, so the full match demands the JSON error object.
    (
        re.compile(
            r"api error:\s*40[13]\s*\{.*\"type\"\s*:\s*\"(?:authentication|permission)_error\".*\}",
            re.IGNORECASE,
        ),
        "authentication",
    ),
    # A Console org at its monthly cap: the API's billing refusal relayed by the
    # CLI. NOT measured live (no org has reached $200 yet), so the full match
    # demands the JSON error object around the API's own sentence.
    (
        re.compile(
            r"api error:\s*400\s*\{.*your credit balance is too low to access the (?:anthropic|claude) api.*\}",
            re.IGNORECASE,
        ),
        "quota",
    ),
    # A key pinned to the WRONG workspace, measured: "There's an issue with the
    # selected model (claude-haiku-4-5). It may not exist or you may not have
    # access to it. Run --model to pick a different model." Durable for that
    # seat (the pairing is wrong, not the model), so it cools.
    (
        re.compile(
            r"there(?:'s| is) an issue with the selected model \(claude-[a-z0-9][a-z0-9._-]{1,63}\)\. "
            r"it may not exist or you may not have access to it\."
            r"(?:\s*run --model to pick a different model\.)?",
            re.IGNORECASE,
        ),
        "model_access",
    ),
)
# Distinct from _QUOTA_DIAGNOSTIC_PATTERN on purpose (found by cross-family
# review 2026-07-25): a 429/rate-limit is a TRANSIENT condition that can
# clear in seconds, unlike a weekly/usage quota exhaustion which persists for
# days. Both used to share the "quota" class, which meant a single transient
# 429 earned the same 15-minute seat cooldown as a durable exhaustion — this
# class exists so only the durable signal ever reaches _COOLDOWN_CLASSES.
_RATE_LIMIT_DIAGNOSTIC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    r"rate[ -]?limit(?:ed| exceeded)?|"
    r"too many requests|429"
    r")\b",
    re.IGNORECASE,
)
# What the child MAY inherit — an ALLOWLIST. Three gate rounds (2026-11-03)
# each found more variables the installed CLI honours to pick a credential, an
# endpoint or a credential store: Foundry, then ANTHROPIC_UNIX_SOCKET and
# CLAUDE_CODE_API_BASE_URL, then CLAUDE_SECURESTORAGE_CONFIG_DIR,
# CLAUDE_BRIDGE_OAUTH_TOKEN, AGENT_PROXY_AUTH_TOKEN and the gcloud selectors.
# A denylist of names (then of families) can never be complete against a CLI
# that grows selectors every release — cicatrix #3, a guard on spelling. The
# child of this client needs to find node and its CA bundle, reach the network
# (through the host's proxy if there is one), write its config dir, and nothing
# else; :func:`build_seat_env` then sets exactly the seat's pair and the
# sterile config dir. Declared cost: a spawner that needs one more variable in
# its child sets it on the returned dict, by name, where a reviewer sees it
# (cli_runners sets CLAUDE_PLUGIN_ROOT; the Canva invoker restores the
# inherited CLAUDE_CONFIG_DIR). The same allowlist is restated in
# scripts/auto_verifier.py and scripts/verified_generator.py and pinned by
# test_claude_oauth_client.py::test_seat_env_scrub_parity_across_spawners.
_CHILD_ENV_ALLOW_EXACT: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TZ",
        "LANG",
        "LANGUAGE",
        "TERM",
        "NO_COLOR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_CHILD_ENV_ALLOW_PREFIXES: Final[tuple[str, ...]] = ("LC_",)
# ── Console seats — RULED 2026-10-26 / 2026-11-01 ───────────────────────────
# Three Ultra plans → three Console orgs; one user-scoped key per org, pinned
# to one workspace. Slot n is a seat only when BOTH variables are set.
CONSOLE_SEAT_SLOTS: Final[tuple[int, ...]] = (1, 2, 3)
CONSOLE_KEY_ENV_PREFIX: Final[str] = "ANTHROPIC_CONSOLE_KEY_"
CONSOLE_WORKSPACE_ENV_PREFIX: Final[str] = "ANTHROPIC_CONSOLE_WORKSPACE_"
WORKSPACE_HEADER: Final[str] = "anthropic-workspace-id"
# The workspace id is forwarded as one line of ``ANTHROPIC_CUSTOM_HEADERS``
# (one header per line): shape-validate it so a value carrying a newline can
# never smuggle a second header into the child. All three live ids match.
_WORKSPACE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^wrkspc_[A-Za-z0-9]{1,64}$")
# Cost-ledger provider. Distinct from the retired ``claude_oauth`` rows on
# purpose: those were subscription calls (cost estimated, not billed); these
# are metered per token against a Console org, so the ledger must not blend them.
LEDGER_PROVIDER: Final[str] = "claude_console"
CLAUDE_CLI_CANDIDATES: Final[tuple[Path, ...]] = (
    Path("/opt/homebrew/bin/claude"),
    Path.home() / ".local/bin" / "claude",
    Path.home() / ".npm-global/bin" / "claude",
    Path("/usr/local/bin/claude"),
)
# W132 (2026-08-28): pin every child CLI to a STERILE config dir. The default
# (~/.claude) is the interactive control-plane: its Stop-hook mailbox receptor
# (mailbox_inject.py) re-opens a "finished" -p session with cross-machine fleet
# mail, the model answers the mail turn after turn, and `-p` prints the LAST
# turn — so the caller receives fleet chatter instead of its answer (measured:
# transcript c68e9228 2026-08-28 — the correct DeckPlan JSON sat at turn 1,
# discarded; the WR2 draft-generator failed 3/3 on every draft for 8 days on
# this). A bare config dir has no hooks BY CONSTRUCTION, so for THIS client's
# consumers the class dies here rather than hook-by-hook (sibling `claude -p`
# spawners — zantara-gateway, intel enricher — have their own config-dir
# handling and are tracked separately). The CLI bootstraps a missing dir on its own
# (verified live, nested paths included) and auth is unaffected: the seat's
# key arrives via ANTHROPIC_API_KEY, never from the config dir (the former
# ("", "keychain") sentinel seat — profile-scoped plan OAuth — is gone with
# RULED 2026-11-01; a sterile dir has no session to fall back to BY
# CONSTRUCTION, which is now a feature). Escape hatch: HEADLESS_CONFIG_DIR_ENV
# points the children at any other hook-free dir. Concurrent -p children share
# the one sterile dir exactly as they shared ~/.claude before (same
# .claude.json write races, smaller blast radius — the dir is disposable).
HEADLESS_CONFIG_DIR_ENV: Final[str] = "CLAUDE_OAUTH_HEADLESS_CONFIG_DIR"
DEFAULT_HEADLESS_CONFIG_DIR: Final[str] = "~/.claude-oauth-headless"
# Model slug allowlist-by-shape: the value is forwarded to ``--model``, so
# reject anything that isn't a plain ``claude-*`` slug (blocks argv/flag
# smuggling via the model field). All current call-site slugs pass:
# ``claude-sonnet-4-6``, ``claude-haiku-4-5``, ``claude-haiku-4-5-20251001``.
_ALLOWED_MODEL_RE: Final[re.Pattern[str]] = re.compile(r"^claude-[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
# The model a call runs on when its caller names none (2026-11-03, the owner's
# pick: Claude Sonnet 5.5, $2/$10 per MTok). Unpinned, the CLI answers on its
# own default — claude-fable-5-1 at $10/$50 on CLI 2.1.316, claude-fable-5 plus
# a Haiku side call on the Fly pin 2.1.236 (measured on a Console seat). The
# variable overrides it per host (an A/B, a rollback) without a deploy of code;
# the restated copies in scripts/auto_verifier.py and verified_generator.py
# read the same variable.
DEFAULT_MODEL_ENV: Final[str] = "NUZANTARA_CLAUDE_DEFAULT_MODEL"
DEFAULT_MODEL: Final[str] = "claude-sonnet-5-5"
# Pure text-completion never needs tools. Block the destructive/exfil ones
# defense-in-depth so an untrusted prompt (e.g. a raw user query routed
# through ``surface_router``) can never reach a tool — replaces the unsafe
# ``--permission-mode bypassPermissions`` this client used to carry.
_DISALLOWED_TOOLS: Final[tuple[str, ...]] = (
    "Bash",
    "Edit",
    "Write",
    "WebFetch",
    "WebSearch",
    "NotebookEdit",
)


class ClaudeOAuthError(RuntimeError):
    """Raised when every configured Console seat failed."""


class ClaudeOAuthNotAvailable(RuntimeError):
    """Raised when there is no door at all: the ``claude`` CLI binary is
    missing from PATH, or no Console seat is configured (fail closed — there
    is no plan-OAuth or keychain fallback, RULED 2026-11-01)."""


@dataclass(frozen=True)
class ClaudeOAuthResponse:
    """Minimal response envelope returned by :func:`complete`."""

    text: str
    token_label: str
    elapsed_s: float
    attempts: int
    # Populated ONLY when ``json_schema`` is passed to :func:`complete_async`
    # and the CLI returned a well-formed ``--output-format json`` envelope with
    # a ``structured_output`` object. ``None`` on the default text path (keeps
    # the dataclass backward-compatible for positional/keyword construction).
    structured: dict[str, Any] | None = None
    # The API's share of the call (the envelope's ``duration_api_ms``), so
    # ``elapsed_s - api_s`` is the CLI's own overhead. None without an envelope.
    api_s: float | None = None


@dataclass(frozen=True)
class ConsoleSeat:
    """One Console org: a user-scoped key and the workspace it is pinned to.

    ``label`` (``console_<n>``) is the only field that may ever be logged,
    recorded or put in an error message (cicatrix #4).
    """

    key: str
    workspace: str
    label: str


@dataclass(frozen=True)
class CliUsage:
    """Token counts and model read off one ``--output-format json`` envelope.

    Claude reports cache tokens OUTSIDE ``input_tokens`` (measured 2026-11-03),
    so :attr:`prompt_tokens` sums the three — the ledger's ``input_tokens`` has
    always meant the whole prompt (Gemini's count already includes its cache).
    ``output_tokens`` already includes thinking (the CLI's own cost for Haiku
    4.5, 81 in / 51 out of which 42 thinking, is exactly 81×$1 + 51×$5 per MTok).
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    # The model these counts belong to (a ``modelUsage`` key) — None when the
    # envelope names no model.
    model: str | None = None
    # The CLI's own list-price bill for these counts.
    cli_cost_usd: float | None = None
    # True when ``cli_cost_usd`` IS the bill: a multi-model attempt, whose
    # per-model 5m/1h write split only the CLI knows (gate round 3).
    bill_cli_cost: bool = False

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
        )


def collect_seats() -> list[ConsoleSeat]:
    """Return the ordered Console seats to try: ``console_1`` → ``console_3``.

    Slot ``n`` is a seat only when BOTH ``ANTHROPIC_CONSOLE_KEY_<n>`` and
    ``ANTHROPIC_CONSOLE_WORKSPACE_<n>`` are set and the workspace id has the
    documented shape. A half-configured slot is skipped with a warning rather
    than run without its header: a user-scoped key answers 400 without the
    workspace (measured 2026-11-03), so the call would only burn a launch. A
    key repeated in a later slot collapses onto its first slot.

    There is no plan-OAuth seat and no keychain sentinel any more. An empty
    list means this process has no sanctioned door to a Claude model, and the
    caller fails closed on it (:class:`ClaudeOAuthNotAvailable`).
    """
    seats: list[ConsoleSeat] = []
    seen: set[str] = set()
    for i in CONSOLE_SEAT_SLOTS:
        label = f"console_{i}"
        key = os.getenv(f"{CONSOLE_KEY_ENV_PREFIX}{i}", "").strip()
        workspace = os.getenv(f"{CONSOLE_WORKSPACE_ENV_PREFIX}{i}", "").strip()
        if not key:
            continue
        if not workspace:
            logger.warning(
                "%s: %s%d is set but %s%d is not — seat skipped",
                label,
                CONSOLE_KEY_ENV_PREFIX,
                i,
                CONSOLE_WORKSPACE_ENV_PREFIX,
                i,
            )
            continue
        if not _WORKSPACE_ID_RE.match(workspace):
            logger.warning(
                "%s: %s%d is not a workspace id (expected wrkspc_…) — seat skipped",
                label,
                CONSOLE_WORKSPACE_ENV_PREFIX,
                i,
            )
            continue
        if key in seen:
            continue
        seen.add(key)
        seats.append(ConsoleSeat(key=key, workspace=workspace, label=label))
    return seats


def build_seat_env(seat: ConsoleSeat) -> dict[str, str]:
    """Env vars for the ``claude`` subprocess running on ``seat``.

    Inherits ONLY the allowlisted variables (:data:`_CHILD_ENV_ALLOW_EXACT`,
    ``LC_*``: binary lookup, locale, CA bundle, proxy, temp dir), then sets
    exactly the selected seat's pair: ``ANTHROPIC_API_KEY`` and the workspace
    header in ``ANTHROPIC_CUSTOM_HEADERS``. No inherited credential, endpoint,
    credential store, plan OAuth seat or sibling Console seat can reach the
    child, whatever the CLI names it next. The child can reach one Console org
    and nothing else.

    Also pins ``CLAUDE_CONFIG_DIR`` to a sterile headless dir (W132): the
    inherited/interactive config dirs carry the control-plane hooks whose
    Stop-time mailbox delivery extends a one-shot ``-p`` session past its
    answer. The override is unconditional — an inherited ``CLAUDE_CONFIG_DIR``
    (e.g. an interactive session spawning this client) must never leak through.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _CHILD_ENV_ALLOW_EXACT or key.startswith(_CHILD_ENV_ALLOW_PREFIXES)
    }
    env["ANTHROPIC_API_KEY"] = seat.key
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"{WORKSPACE_HEADER}: {seat.workspace}"
    headless_dir = os.environ.get(HEADLESS_CONFIG_DIR_ENV, "").strip() or DEFAULT_HEADLESS_CONFIG_DIR
    env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(headless_dir)
    # The CLI's non-essential traffic (telemetry, error reporting, update checks)
    # is waited on around every one-shot call: on Pro, CLI 2.1.317 answered in
    # 3.0 s with it and 1.75 s without, the API's share ~1.1 s either way
    # (measured 2026-11-03). It carries nothing a headless call needs.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _classify_cli_diagnostic(text: str) -> str | None:
    """Return a safe diagnostic class for a short, clearly framed CLI error.

    Claude may occasionally exit 0 while printing a quota/auth diagnostic.
    Scanning arbitrary successful content for words such as "quota" causes
    false positives, so this gate only examines bounded, low-line-count text
    that starts like a CLI error. The returned value is a fixed class suitable
    for logs; raw output is never returned.
    """
    sample = (text or "").strip()
    if (
        not sample
        or len(sample) > _MAX_DIAGNOSTIC_CHARS
        or len(sample.splitlines()) > _MAX_DIAGNOSTIC_LINES
    ):
        return None

    compact = " ".join(sample.split())
    for pattern, diagnostic_class in _CONSOLE_DIAGNOSTICS:
        if pattern.fullmatch(compact):
            return diagnostic_class
    if _DIAGNOSTIC_FRAME_PATTERN.search(compact) is None:
        return None
    if _AUTH_DIAGNOSTIC_PATTERN.search(compact):
        return "authentication"
    if _QUOTA_DIAGNOSTIC_PATTERN.search(compact):
        return "quota"
    if _RATE_LIMIT_DIAGNOSTIC_PATTERN.search(compact):
        return "rate_limit"
    return None


# ---------------------------------------------------------------------------
# Seat cooldown — measured 2026-07-25, not theorised
# ---------------------------------------------------------------------------
# An exhausted Max seat does NOT go quiet: it exits 1 and prints
#   "You've hit your weekly limit · resets 9am (Asia/Makassar)"
# Two consequences, both verified on disk before this was written:
#   1. `_classify_cli_diagnostic` WOULD label that message `quota` — but it was
#      only ever called on the exit==0 branch, so the seat logged as the opaque
#      `cli_exit_1` and nobody could tell exhaustion from a crash.
#   2. With no memory of the verdict, every single call re-spawned the CLI for
#      every exhausted seat. Two dead ~2s process launches per request, forever,
#      until the weekly window rolls over.
# So: remember the verdict for a while and skip that seat.
#
# The TTL is deliberately much shorter than the condition it tracks (a weekly
# limit can be days from resetting). Cheap to be wrong in the recovery
# direction — one probe per seat per window — and expensive to be wrong in the
# other, which is exactly the bug being fixed.
#
# NOT wired to `RATE_LIMIT_PATTERN`: that pattern is a loose word-list
# (`quota|capacity|exhausted|...`) matching anywhere in the output, and a
# transient 429 is not a durable condition. Only the FRAMED classes from
# `_classify_cli_diagnostic` — which requires a bounded, error-shaped message —
# are durable enough to earn a skip. Widening the loose pattern instead would
# be cicatrix #3 (guard deciding on substring, not entity).
#
# CORRECTION (cross-family review, 2026-07-25): being FRAMED is necessary but
# not sufficient for durability — a 429/rate-limit is just as tightly framed
# as a weekly-limit message, but resolves in seconds, not days. It used to
# share the `quota` class with genuine exhaustion for exactly that reason and
# would have earned the same 15-minute cooldown. `_RATE_LIMIT_DIAGNOSTIC_PATTERN`
# now classifies it separately as `rate_limit`, which is deliberately absent
# from `_COOLDOWN_CLASSES` below — still rotates to the next seat WITHIN the
# current call (via `_classify_cli_diagnostic` returning non-None), never
# remembered ACROSS calls.
_DEFAULT_SEAT_COOLDOWN_S: Final[float] = 900.0
# ``model_access`` joined 2026-11-03 with the Console door: a key pinned to the
# wrong workspace is as durable as a revoked one (see the pattern's comment).
_COOLDOWN_CLASSES: Final[frozenset[str]] = frozenset({"quota", "authentication", "model_access"})
# label+fingerprint -> monotonic deadline. Process-local by design: no shared
# store, so a cron that lives 30s carries nothing, and the long-lived Fly
# service self-heals on restart. Racy writes between coroutines are harmless
# (worst case: one duplicate probe).
_seat_cooldowns: dict[str, float] = {}


def _seat_cooldown_s() -> float:
    """Cooldown TTL in seconds. ``CLAUDE_OAUTH_SEAT_COOLDOWN_S=0`` disables."""
    raw = os.getenv("CLAUDE_OAUTH_SEAT_COOLDOWN_S", "").strip()
    if not raw:
        return _DEFAULT_SEAT_COOLDOWN_S
    try:
        parsed = float(raw)
    except ValueError:
        return _DEFAULT_SEAT_COOLDOWN_S
    # Also catches NaN (never > 0) — degrades to "disabled", never to a
    # poisoned deadline that would strand a seat forever.
    return parsed if parsed > 0 else 0.0


def _seat_key(seat: ConsoleSeat) -> str:
    """Stable, non-secret identity for a seat.

    Keyed on a FINGERPRINT of key+workspace, not the label alone: rotating
    ``ANTHROPIC_CONSOLE_KEY_1`` (or re-pinning its workspace) keeps the label
    but must not inherit the old pairing's cooldown. Never stores or logs the
    key itself (cicatrix #4).
    """
    digest = hashlib.sha256(f"{seat.key}\n{seat.workspace}".encode()).hexdigest()[:8]
    return f"{seat.label}:{digest}"


def _mark_seat_cooling(seat: ConsoleSeat, diagnostic_class: str | None) -> None:
    """Record that this seat reported a durable failure. No-op for other classes."""
    ttl = _seat_cooldown_s()
    if ttl <= 0 or diagnostic_class not in _COOLDOWN_CLASSES:
        return
    _seat_cooldowns[_seat_key(seat)] = time.monotonic() + ttl
    logger.warning("%s: %s — skipping this seat for %.0fs", seat.label, diagnostic_class, ttl)


def _drop_cooling_seats(seats: list[ConsoleSeat]) -> list[ConsoleSeat]:
    """Filter out seats still inside their cooldown window.

    FAIL-OPEN by construction: if that would leave nothing to try, the full
    list is returned unchanged. A cooldown is an optimisation and must never be
    able to starve the cascade down to zero candidates.
    """
    now = time.monotonic()
    live: list[ConsoleSeat] = []
    skipped: list[str] = []
    for seat in seats:
        key = _seat_key(seat)
        until = _seat_cooldowns.get(key)
        if until is None or until <= now:
            _seat_cooldowns.pop(key, None)
            live.append(seat)
        else:
            skipped.append(seat.label)
    if not live:
        if skipped:
            logger.warning(
                "all %d seat(s) cooling; ignoring cooldown and trying anyway",
                len(skipped),
            )
        return seats
    if skipped:
        logger.info("skipping %d cooling seat(s): %s", len(skipped), ", ".join(skipped))
    return live


def _reset_seat_cooldowns() -> None:
    """Clear the in-process cooldown memo. For tests — never called in prod."""
    _seat_cooldowns.clear()


async def _kill_and_reap(proc: Any) -> None:
    """Terminate a timed-out child and wait for it so no zombie remains."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


def _resolve_claude_cli() -> str:
    """Return an executable `claude` path for launchd-safe subprocess calls.

    LaunchAgents often run with a minimal PATH. Resolve the binary once from
    the current PATH, then fall back to the install locations used on Pro/Mini.
    Returning the bare command keeps the old FileNotFoundError behavior when
    the CLI is genuinely absent.
    """
    resolved = shutil.which("claude")
    if resolved:
        return resolved

    for candidate in CLAUDE_CLI_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return "claude"


# Public name for the spawners that run the CLI themselves (a stdin prompt, a
# model alias the allowlist here would refuse) but must still resolve the
# binary the launchd-safe way and run it on a seat from build_seat_env().
resolve_claude_cli = _resolve_claude_cli


def default_model() -> str:
    """The model for a call that names none: ``$NUZANTARA_CLAUDE_DEFAULT_MODEL``
    when it is a plain ``claude-*`` slug (it reaches argv), else
    :data:`DEFAULT_MODEL`."""
    override = os.environ.get(DEFAULT_MODEL_ENV, "").strip()
    if override and _ALLOWED_MODEL_RE.match(override):
        return override
    if override:
        logger.warning(
            "%s is not a plain claude-* slug; using %s",
            DEFAULT_MODEL_ENV,
            DEFAULT_MODEL,
        )
    return DEFAULT_MODEL


# Cap the serialized schema length so a pathological schema can never blow past
# the OS argv limit (`ARG_MAX`) — keeps the structured path from silently
# producing an E2BIG OSError mid-cron. 16 KiB is generous for any realistic
# Pydantic-derived schema (review D6).
_MAX_SCHEMA_BYTES: Final[int] = 16 * 1024

# Cached result of "does this `claude` binary support --json-schema?". None =
# not yet probed. Probed once per process via `_cli_supports_json_schema`.
_JSON_SCHEMA_SUPPORTED: bool | None = None


def _cli_supports_json_schema(claude_cli: str, timeout_s: float = 10) -> bool:
    """Return True iff the resolved `claude` CLI advertises ``--json-schema``.

    Probed once and memoized (review D4): a CLI that predates the flag answers
    "unknown option" with a non-zero exit, which the retry loop would otherwise
    misread as a generic error and burn every token before failing. We check
    ``--help`` up front and route to the plain text path when absent, instead of
    trial-and-erroring on real model calls.
    """
    global _JSON_SCHEMA_SUPPORTED
    if _JSON_SCHEMA_SUPPORTED is not None:
        return _JSON_SCHEMA_SUPPORTED
    try:
        import subprocess

        out = subprocess.run(
            [claude_cli, "--help"],
            capture_output=True,
            text=True,
            timeout=max(0.001, min(10, timeout_s)),
        )
        _JSON_SCHEMA_SUPPORTED = "--json-schema" in (out.stdout + out.stderr)
    except Exception:
        # If we can't even probe, assume unsupported and fall back to text.
        _JSON_SCHEMA_SUPPORTED = False
    return _JSON_SCHEMA_SUPPORTED


def _serialize_schema(json_schema: dict[str, Any]) -> str:
    """Compact-serialize a JSON Schema for the ``--json-schema`` argv value.

    Compact separators minimize argv length. Never log the value (it may carry
    field descriptions). Raises ValueError if it exceeds ``_MAX_SCHEMA_BYTES``
    so the caller can fall back to the text path rather than hit E2BIG.
    """
    import json as _json

    blob = _json.dumps(json_schema, separators=(",", ":"))
    if len(blob.encode("utf-8")) > _MAX_SCHEMA_BYTES:
        raise ValueError(
            f"json_schema too large for argv ({len(blob)} chars > {_MAX_SCHEMA_BYTES})"
        )
    return blob


def _count(value: object) -> int:
    """A usage field as a non-negative token count; 0 for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _cost_field(value: object) -> float | None:
    """A cost field as a float, or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _split_writes(written: int, written_5m: object) -> tuple[int, int]:
    """(5m, 1h) cache writes. Write tokens the split does not attribute bill at
    the 1-hour (higher) rate: the ledger may over-state a write, never under-state one."""
    five = min(written, _count(written_5m))
    return five, written - five


def _read_usage(envelope: dict[str, Any]) -> CliUsage | None:
    """The top-level ``usage`` block, or None when it carries no token counts."""
    usage_raw = envelope.get("usage")
    if not isinstance(usage_raw, dict):
        return None
    it = usage_raw.get("input_tokens")
    ot = usage_raw.get("output_tokens")
    if isinstance(it, bool) or isinstance(ot, bool):
        return None
    if not isinstance(it, int) or not isinstance(ot, int):
        return None
    split = usage_raw.get("cache_creation")
    split = split if isinstance(split, dict) else {}
    written_5m, written_1h = _split_writes(
        _count(usage_raw.get("cache_creation_input_tokens")),
        split.get("ephemeral_5m_input_tokens"),
    )
    return CliUsage(
        input_tokens=max(0, it),
        output_tokens=max(0, ot),
        cache_read_tokens=_count(usage_raw.get("cache_read_input_tokens")),
        cache_write_5m_tokens=written_5m,
        cache_write_1h_tokens=written_1h,
        cli_cost_usd=_cost_field(envelope.get("total_cost_usd")),
    )


def _entry_counts(mu: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """(input, output, cache read, cache write) of one ``modelUsage`` entry, or
    None when its two required counts are not integers."""
    it, ot = mu.get("inputTokens"), mu.get("outputTokens")
    if isinstance(it, bool) or isinstance(ot, bool) or not isinstance(it, int) or not isinstance(ot, int):
        return None
    return (
        max(0, it),
        max(0, ot),
        _count(mu.get("cacheReadInputTokens")),
        _count(mu.get("cacheCreationInputTokens")),
    )


def _read_usages(envelope: dict[str, Any]) -> list[CliUsage]:
    """The real usage of one envelope, as a list the ledger prices entry by entry.

    One model in ``modelUsage`` (every pinned call measured): the top-level
    ``usage`` — the block that carries the 5m/1h write split — under that
    model's name, priced from pricing.py and audited by ``total_cost_usd``.

    Several models: CLI 2.1.236, Fly's pin, ran an unpinned call on
    claude-fable-5 (170/5) PLUS a claude-haiku-4-5-20251001 side call (881/9);
    the top-level block counted the main model only, ``total_cost_usd`` both
    (measured 2026-11-03). ``modelUsage`` entries carry no 5m/1h split in the
    documented contract, so pricing them from the table cannot be exact (gate
    round 3: every write at the 1h rate over-billed a 5m write by 60%). The
    CLI's ``total_cost_usd`` is the one figure that applied each model's own
    split, so it IS the bill for such an attempt; the counts are the entries'
    sums and the model is the one whose entry equals the top-level usage.
    """
    top = _read_usage(envelope)
    models = envelope.get("modelUsage")
    models = models if isinstance(models, dict) else {}
    entries = [(name, mu) for name, mu in models.items() if isinstance(name, str) and name and isinstance(mu, dict)]
    if len(models) <= 1:
        if top is not None:
            return [replace(top, model=entries[0][0] if entries else None)]
        counts = _entry_counts(entries[0][1]) if entries else None
        if counts is None:
            return []
        it, ot, read, written = counts
        return [
            CliUsage(
                input_tokens=it,
                output_tokens=ot,
                cache_read_tokens=read,
                cache_write_1h_tokens=written,  # no split: the higher rate
                model=entries[0][0],
                cli_cost_usd=_cost_field(envelope.get("total_cost_usd")),
            )
        ]

    counted = [(name, mu, _entry_counts(mu)) for name, mu in entries]
    valid = [(name, mu, c) for name, mu, c in counted if c is not None]
    main = None
    if top is not None:
        same = [name for name, _, c in valid if (c[0], c[1]) == (top.input_tokens, top.output_tokens)]
        main = same[0] if len(same) == 1 else None
    if main is None and valid:
        main = max(valid, key=lambda v: (_cost_field(v[1].get("costUSD")) or 0.0, v[2][1]))[0]
    total = _cost_field(envelope.get("total_cost_usd"))
    if total is not None and valid:
        return [
            CliUsage(
                input_tokens=sum(c[0] for _, _, c in valid),
                output_tokens=sum(c[1] for _, _, c in valid),
                cache_read_tokens=sum(c[2] for _, _, c in valid),
                cache_write_5m_tokens=sum(c[3] for _, _, c in valid),
                model=main,
                cli_cost_usd=total,
                bill_cli_cost=True,
            )
        ]
    if total is not None and top is not None:
        return [replace(top, model=main, cli_cost_usd=total, bill_cli_cost=True)]
    # No CLI total (never measured): each entry from the table, writes at the
    # 1h rate — the ledger may over-state a write, never under-state one.
    per_model = [
        CliUsage(
            input_tokens=c[0],
            output_tokens=c[1],
            cache_read_tokens=c[2],
            cache_write_1h_tokens=c[3],
            model=name,
            cli_cost_usd=_cost_field(mu.get("costUSD")),
        )
        for name, mu, c in valid
    ]
    if per_model:
        return per_model
    return [replace(top, model=main)] if top is not None else []


def _answering(usages: list[CliUsage]) -> CliUsage | None:
    """The usage of the model that answered: the costliest entry (a side call
    on a cheaper model is not the answer)."""
    if not usages:
        return None
    return max(usages, key=lambda u: (u.cli_cost_usd or 0.0, u.output_tokens))


def _is_envelope(obj: object) -> bool:
    """A CLI result envelope, not just any JSON object: ``type == "result"``
    or an explicit ``is_error`` bit (gate round 1, 2026-11-03: a raw JSON answer
    must not be mistaken for an envelope and discarded as empty)."""
    return isinstance(obj, dict) and (obj.get("type") == "result" or "is_error" in obj)


def _load_envelope(stdout: str) -> dict[str, Any] | None:
    """The result envelope on stdout, or None when stdout is not one."""
    import json as _json

    envelope: dict[str, Any] | None = None
    text = (stdout or "").strip()
    if not text:
        return None

    # Fast path: whole stdout is one JSON object.
    try:
        obj = _json.loads(text)
        if _is_envelope(obj):
            envelope = obj
    except _json.JSONDecodeError:
        # NDJSON / events: take the last well-formed object whose type=="result".
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                envelope = obj
                break
            if _is_envelope(obj) and envelope is None:
                envelope = obj  # last-resort: an envelope-shaped object
    return envelope


def _read_envelope(
    envelope: dict[str, Any],
    *,
    expect_structured: bool = True,
) -> tuple[str, dict[str, Any] | None, list[CliUsage], bool]:
    """``(text, structured, usages, is_error)`` off a loaded envelope."""
    structured = envelope.get("structured_output")
    if not isinstance(structured, dict):
        structured = None

    result_text = envelope.get("result")
    text_out = result_text if isinstance(result_text, str) else ""

    is_error = envelope.get("is_error") is True
    if expect_structured and not is_error and structured is None:
        # Diagnostic: an envelope arrived but carried no structured_output. The
        # caller will retry; only log the public envelope type, never payload.
        logger.warning(
            "claude --json-schema envelope had no structured_output (type=%s)",
            envelope.get("type"),
        )

    return text_out, structured, _read_usages(envelope), is_error


def _parse_json_envelope(
    stdout: str,
    *,
    expect_structured: bool = True,
) -> tuple[str, dict[str, Any] | None, CliUsage | None, bool]:
    """Parse JSON stdout into ``(text, structured, usage, is_error)``.

    Defensive per review D2/D5:
    - Accepts a single JSON object OR newline-delimited events; in the NDJSON
      case the last object with ``type == "result"`` wins (ignores log/event
      noise — NO generic balanced-brace scan).
    - ``structured`` comes from the ``structured_output`` field (the actual
      answer); ``text`` is the human-prose ``result`` field (NOT the answer —
      do not feed it to a schema validator).
    - ``usage`` is the REAL usage (:class:`CliUsage`: input, output, cache
      read, cache writes split 5m/1h, the answering model, the CLI's own
      list-price cost) for the cost ledger.
    - Preserves the envelope's explicit ``is_error`` bit so an exit-0 error
      cannot be mistaken for a successful structured response.
    - Returns ``("", None, None, False)`` for unparseable input so the caller
      routes it through the existing empty-output gate.
    """
    envelope = _load_envelope(stdout)
    if envelope is None:
        return "", None, None, False
    text, structured, usages, is_error = _read_envelope(envelope, expect_structured=expect_structured)
    return text, structured, _answering(usages), is_error


async def complete_async(
    prompt: str,
    *,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S,
    endpoint: str | None = None,
    request_id: str | None = None,
    json_schema: dict[str, Any] | None = None,
) -> ClaudeOAuthResponse:
    """Run ``claude -p`` across the Console seats and return the completion.

    When ``json_schema`` is provided AND the CLI supports ``--json-schema``,
    the call adds ``--json-schema <schema>`` and the parsed
    ``structured_output`` is returned in ``ClaudeOAuthResponse.structured``.
    Without a schema the answer is the envelope's ``result`` — the same text
    the CLI's plain mode printed. If the CLI lacks the flag, the schema is
    silently dropped and the text path runs (caller validates).

    Every call runs ``--bare --tools "" --output-format json`` and — successful
    or not — is recorded through
    :func:`backend.services.observability.record_llm_call` with provider
    ``claude_console`` (:data:`LEDGER_PROVIDER`), with the envelope's REAL
    usage (cache reads and writes included) and the model that answered.
    Only stdout that is no envelope at all falls back to the ``len // 4``
    estimate.

    Args:
        prompt: The user prompt (system-prompt handling is deferred to the
            CLI — pass everything inline for now).
        model: Optional model id forwarded as ``claude -p --model <id>``.
            Accepts any slug the CLI accepts (``claude-opus-4-7``,
            ``claude-sonnet-4-6``, ``claude-haiku-4-5``, …).
        timeout_s: Per-seat wall-clock timeout.
        total_timeout_s: Hard wall-clock budget for the entire fallback call.
            Every seat receives ``min(timeout_s, remaining_global_budget)``.
        endpoint: Caller identifier for cost attribution. Strongly
            recommended.
        request_id: HTTP correlation id if available.

    Returns:
        :class:`ClaudeOAuthResponse` with the completion text and the label
        of the seat that succeeded (``console_<n>``).

    Raises:
        :class:`ClaudeOAuthNotAvailable`: the ``claude`` binary is missing,
            or no Console seat is configured.
        :class:`ClaudeOAuthError`: every seat hit rate-limit / empty output
            / non-zero exit.
    """
    if not prompt:
        raise ValueError("prompt must be non-empty")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if total_timeout_s <= 0:
        raise ValueError("total_timeout_s must be positive")

    seats = collect_seats()
    if not seats:
        # Fail closed. The former cascade always ended in a keychain sentinel,
        # so "nothing configured" silently became "use whatever plan-OAuth
        # session this host has" — the exact fallback RULED 2026-11-01 bans.
        raise ClaudeOAuthNotAvailable(
            "no Console seat configured: set "
            f"{CONSOLE_KEY_ENV_PREFIX}<n> and {CONSOLE_WORKSPACE_ENV_PREFIX}<n> "
            f"for n in {CONSOLE_SEAT_SLOTS[0]}..{CONSOLE_SEAT_SLOTS[-1]} "
            "(no plan-OAuth or keychain fallback, RULED 2026-11-01)"
        )
    seats = _drop_cooling_seats(seats)
    start = time.monotonic()
    deadline = start + total_timeout_s
    last_error = ""
    attempts_made = 0
    # Reported only at the end (success OR failure)
    # Never unpinned: the CLI's own default is its most expensive model.
    model = model or default_model()
    _recorder_model = model
    _recorder_input_tokens = max(1, len(prompt) // 4)
    _recorder_output_tokens = 0
    # Every attempt's real usage, failed ones included: a seat that answered
    # without the structured output still billed its tokens (gate round 1).
    _recorder_usages: list[CliUsage] = []
    _recorder_success = False
    _recorder_error_class: str | None = None
    # Provenance of the token counts reported to the cost ledger (review D7):
    # "cli_envelope" = real counts parsed from --output-format json,
    # "estimated" = len//4 rule-of-thumb (the legacy default).
    _recorder_usage_source = "estimated"

    # OTEL span wraps the whole retry loop so the dashboard sees one
    # invocation per logical call (with attempts as an attribute), not
    # one per token-fallback attempt — matches the granularity the
    # other LLM clients emit through their auto-instrumentors.
    # Started here, ended in the success-path `finally` and in the
    # all-tokens-failed path below; both code paths set output_tokens
    # and success attributes on _span before the manager exits.
    # Note: in the unlikely path of `ClaudeOAuthNotAvailable` raised
    # from `create_subprocess_exec`, the span context is leaked. The
    # leak is harmless when OTEL is disabled (nullcontext) and
    # surfaces as a long-running span when enabled — acceptable for
    # Phase 0; a follow-up can migrate this to a proper async with.
    _otel_span_ctx = _start_claude_cli_span(_recorder_model, _recorder_input_tokens)
    _span = _otel_span_ctx.__enter__()
    _span_set: Any = _span if hasattr(_span, "set_attribute") else None
    claude_cli = _resolve_claude_cli()

    # Decide ONCE whether this call runs the structured path. Conditions (all
    # must hold): caller passed a schema, the CLI advertises --json-schema, and
    # the schema serializes within the argv cap. Any failure → text path
    # (the binding/caller still validates locally). Keeps the schema branch
    # fully isolated from the unchanged text branch (review D3).
    schema_blob: str | None = None
    if json_schema is not None:
        # Probe the CLI's --help off the event loop: the first call shells out
        # synchronously (memoized after), and a blocking subprocess.run inside a
        # coroutine would freeze sibling tasks on a shared loop (panel: both LLMs
        # flagged the cron-headless risk). asyncio.to_thread keeps the loop free.
        supports = _JSON_SCHEMA_SUPPORTED
        if supports is None:
            probe_budget_s = deadline - time.monotonic()
            if probe_budget_s <= 0:
                supports = False
                last_error = "global_deadline"
            else:
                try:
                    supports = await asyncio.wait_for(
                        asyncio.to_thread(
                            _cli_supports_json_schema,
                            claude_cli,
                            probe_budget_s,
                        ),
                        timeout=probe_budget_s,
                    )
                except asyncio.TimeoutError:
                    supports = False
                    last_error = "global_deadline"
        if supports:
            try:
                schema_blob = _serialize_schema(json_schema)
            except ValueError as exc:
                logger.warning("json_schema dropped, falling back to text: %s", exc)
        else:
            logger.info("claude CLI lacks --json-schema; using text path")
    use_schema = schema_blob is not None

    for attempt, seat in enumerate(seats, start=1):
        label = seat.label
        remaining_global_s = deadline - time.monotonic()
        if remaining_global_s <= 0:
            last_error = "global_deadline"
            logger.warning(
                "claude Console global deadline exhausted after %d attempt(s)",
                attempts_made,
            )
            break
        attempts_made = attempt
        seat_deadline = min(deadline, time.monotonic() + timeout_s)

        # No bypassPermissions: tools are explicitly disallowed and the
        # default permission mode fails-closed on any tool the model still
        # attempts. The `--` sentinel guarantees the (possibly untrusted)
        # prompt is parsed as an operand, never as a flag.
        cmd: list[str] = [
            claude_cli,
            "-p",
            # Lean (measured 2026-11-03 on a Console seat): --bare drops hooks,
            # CLAUDE.md discovery, auto-memory and plugin sync; --tools "" drops
            # every tool schema. Together they were the 18.6K-token cache write
            # every cold call paid ($0.023 → $0.0003 on Haiku 4.5).
            "--bare",
            "--tools",
            "",
            # W132: a one-shot completion must not persist a session — the
            # transcript trail is what a resumable session leaves behind, and
            # it also stops unbounded projects/*.jsonl growth in the sterile
            # config dir (same flag the zantara-gateway sibling already uses).
            "--no-session-persistence",
            "--disallowedTools",
            *_DISALLOWED_TOOLS,
        ]
        if model:
            if not _ALLOWED_MODEL_RE.match(model):
                raise ValueError(f"model not allowed: {model!r}")
            cmd.extend(["--model", model])
        # Every call returns the JSON envelope: the answer, the REAL usage and
        # the model that answered. Output flags go BEFORE the `--` sentinel so
        # the prompt operand stays last and unambiguous (review D6).
        cmd.extend(["--output-format", "json"])
        if use_schema:
            cmd.extend(["--json-schema", schema_blob])  # type: ignore[list-item]
        cmd.append("--")
        cmd.append(prompt)

        env = build_seat_env(seat)

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    # The prompt is in argv. A child that inherits a pipe on
                    # stdin waits 3 s for it before answering (measured
                    # 2026-11-03: "no stdin data received in 3s").
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=max(0.0, seat_deadline - time.monotonic()),
            )
        except FileNotFoundError as exc:
            raise ClaudeOAuthNotAvailable(
                "`claude` CLI not found in PATH. Install: https://claude.ai/code",
            ) from exc
        except asyncio.TimeoutError:
            if time.monotonic() >= deadline:
                last_error = "global_deadline"
                logger.warning("claude Console global deadline exhausted during CLI launch")
                break
            last_error = f"{label}: launch_timeout"
            logger.warning("%s: CLI launch timed out, trying next", label)
            continue

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=max(0.0, seat_deadline - time.monotonic()),
            )
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            if time.monotonic() >= deadline:
                last_error = "global_deadline"
                logger.warning("claude Console global deadline exhausted during CLI attempt")
                break
            last_error = f"{label}: attempt_timeout"
            logger.warning("%s: CLI attempt timed out, trying next", label)
            continue

        exit_code = proc.returncode or 0
        output = (stdout_b.decode(errors="replace") or "").strip()
        stderr = (stderr_b.decode(errors="replace") or "").strip()

        # Every stdout is an envelope now (``--output-format json``). Load it
        # once; stdout that is NOT one (an older CLI, a crash before the
        # envelope) keeps the legacy raw-text handling below.
        envelope = _load_envelope(output)
        env_text = ""
        env_structured: dict[str, Any] | None = None
        env_usages: list[CliUsage] = []
        env_is_error = False
        if envelope is not None:
            env_text, env_structured, env_usages, env_is_error = _read_envelope(
                envelope,
                expect_structured=use_schema and exit_code == 0,
            )
            _recorder_usages.extend(env_usages)
        # What a failure may be READ for. An is_error envelope offers its
        # ``result`` (the framed API message); an envelope that is NOT an error
        # is an answer, and an answer's prose must never skip or cool a seat
        # (gate round 1, 2026-11-03); no envelope at all is the raw stream.
        if envelope is None:
            failure_text = output
        elif env_is_error:
            failure_text = env_text
        else:
            failure_text = ""

        if exit_code != 0 and RATE_LIMIT_PATTERN.search(f"{failure_text}\n{stderr}"):
            last_error = f"{label}: rate limited"
            logger.warning("%s: rate limited, trying next", label)
            continue

        parsed_text = output
        parsed_structured: dict[str, Any] | None = None
        parsed_api_s: float | None = None
        parsed_usage: CliUsage | None = None
        if exit_code == 0 and envelope is not None:
            if env_is_error:
                diagnostic_class = _classify_cli_diagnostic(env_text) or (
                    "structured_error" if use_schema else "envelope_error"
                )
                _mark_seat_cooling(seat, diagnostic_class)
                last_error = f"{label}: {diagnostic_class}"
                logger.warning(
                    "%s: CLI envelope error (%s), trying next",
                    label,
                    diagnostic_class,
                )
                continue
            parsed_text, parsed_structured = env_text, env_structured
            api_ms = envelope.get("duration_api_ms")
            if isinstance(api_ms, (int, float)) and not isinstance(api_ms, bool):
                parsed_api_s = api_ms / 1000
            parsed_usage = _answering(env_usages)
        elif exit_code == 0 and use_schema:
            # Schema path with no envelope: nothing structured to return.
            parsed_text = ""
        if use_schema and exit_code == 0:
            # Structured path only "succeeds" if we got a structured_output.
            # Missing envelope → empty so the token loop retries, and the
            # caller's text fallback can run on a clean separate call.
            effective_output = parsed_text if parsed_structured is not None else ""
        else:
            effective_output = parsed_text

        # With an envelope its is_error bit is the verdict: a success envelope
        # is an answer even when the answer QUOTES a diagnostic ("explain this
        # error: Invalid API key …") — classifying it cooled a healthy seat for
        # 900 s and discarded the answer (gate round 1, 2026-11-03). The
        # full-match classifier reads only stdout that is no envelope at all.
        if exit_code == 0 and not use_schema and envelope is None:
            diagnostic_class = _classify_cli_diagnostic(output)
            if diagnostic_class is None:
                diagnostic_class = _classify_cli_diagnostic(stderr)
            if diagnostic_class is not None:
                _mark_seat_cooling(seat, diagnostic_class)
                last_error = f"{label}: {diagnostic_class}"
                logger.warning(
                    "%s: CLI diagnostic (%s), trying next",
                    label,
                    diagnostic_class,
                )
                continue

        if not effective_output and exit_code in (0, 143) and parsed_structured is None:
            last_error = f"{label}: empty_output"
            logger.warning("%s: empty CLI output, trying next", label)
            continue

        if exit_code != 0:
            # Classify BEFORE falling back to the bare exit code. An exhausted
            # seat exits NON-zero while printing its diagnostic (measured
            # 2026-07-25); without this branch it logged as `cli_exit_1`, which
            # is indistinguishable from a crash and carries no verdict to
            # remember — so every later call re-probed the dead seat.
            # The CLI wraps the API refusal in its JSON envelope even on a
            # non-zero exit (measured 2026-11-03: bad key → exit 1,
            # is_error:true, result "Invalid API key …"). The envelope as a
            # whole is far past the classifier's size gate; its ``result``
            # field is exactly the framed message. ONLY an envelope that says
            # is_error:true is read this way — a non-zero exit around a
            # legitimate answer must not let the answer's prose cool the seat
            # (gate round 1, 2026-11-03).
            diagnostic_class = None
            if env_is_error:
                diagnostic_class = _classify_cli_diagnostic(env_text)
            if diagnostic_class is None and envelope is None:
                diagnostic_class = _classify_cli_diagnostic(output)
            if diagnostic_class is None:
                diagnostic_class = _classify_cli_diagnostic(stderr)
            if diagnostic_class is not None:
                _mark_seat_cooling(seat, diagnostic_class)
                last_error = f"{label}: {diagnostic_class}"
                logger.warning(
                    "%s: CLI diagnostic (%s, exit=%d), trying next",
                    label,
                    diagnostic_class,
                    exit_code,
                )
                continue
            last_error = f"{label}: cli_exit_{exit_code}"
            logger.warning("%s: CLI failed (exit=%d), trying next", label, exit_code)
            continue

        elapsed = time.monotonic() - start
        logger.info(
            "claude -p success via %s (%.2fs wall, %s API, attempt %d)",
            label,
            elapsed,
            f"{parsed_api_s:.2f}s" if parsed_api_s is not None else "?",
            attempt,
        )
        # Prefer the REAL usage from the envelope; else estimate.
        if parsed_usage is not None:
            _recorder_usage_source = "cli_envelope"
            if parsed_usage.model:
                # The model that ANSWERED, not the one asked for: an unpinned
                # call is billed at the CLI default's rate (claude-fable-5-1
                # on 2026-11-03), which "claude-unknown" used to hide.
                _recorder_model = parsed_usage.model
        else:
            # Reached only when stdout carried no usage-bearing envelope.
            # Estimate on the FULL stdout stream, not parsed_text: in the schema
            # path parsed_text is only the short prose ``result`` while the real
            # payload lives in structured_output — estimating on parsed_text
            # would under-bill the ledger by orders of magnitude (panel: Gemini
            # CRITICO / DeepSeek MEDIO). Earlier attempts' real usage still
            # counts; this one joins the list as an estimate.
            _recorder_usage_source = "estimated"
            _recorder_usages.append(
                CliUsage(
                    input_tokens=max(1, len(prompt) // 4),
                    output_tokens=max(1, len(output) // 4),
                )
            )
        _recorder_input_tokens = sum(u.prompt_tokens for u in _recorder_usages)
        _recorder_output_tokens = sum(u.output_tokens for u in _recorder_usages)
        _recorder_success = True
        if _span_set is not None:
            _span_set.set_attribute("gen_ai.usage.input_tokens", _recorder_input_tokens)
            _span_set.set_attribute("gen_ai.usage.output_tokens", _recorder_output_tokens)
            _span_set.set_attribute("nuzantara.claude_oauth.token_label", label)
            _span_set.set_attribute("nuzantara.claude_oauth.attempts", attempt)
        try:
            return ClaudeOAuthResponse(
                text=parsed_text,
                token_label=label,
                elapsed_s=elapsed,
                api_s=parsed_api_s,
                attempts=attempt,
                structured=parsed_structured,
            )
        finally:
            _otel_span_ctx.__exit__(None, None, None)
            await _record_claude_oauth_call(
                model=_recorder_model,
                input_tokens=_recorder_input_tokens,
                output_tokens=_recorder_output_tokens,
                success=_recorder_success,
                latency_ms=int((time.monotonic() - start) * 1000),
                endpoint=endpoint,
                request_id=request_id,
                error_class=_recorder_error_class,
                usage_source=_recorder_usage_source,
                usages=_recorder_usages,
            )

    err = ClaudeOAuthError(
        f"Claude Console seats failed after {attempts_made} attempt(s). "
        f"Last error class: {last_error or 'unknown'}",
    )
    _recorder_error_class = type(err).__name__
    if _span_set is not None:
        try:
            from opentelemetry.trace import Status, StatusCode

            _span_set.set_attribute("nuzantara.claude_oauth.attempts", attempts_made)
            _span_set.set_attribute("error.message", last_error[:200])
            _span_set.set_status(Status(StatusCode.ERROR, "all Console seats failed"))
        except Exception:
            pass
    _otel_span_ctx.__exit__(type(err), err, err.__traceback__)
    if _recorder_usages:
        _recorder_input_tokens = sum(u.prompt_tokens for u in _recorder_usages)
        _recorder_output_tokens = sum(u.output_tokens for u in _recorder_usages)
        _recorder_usage_source = "cli_envelope"
        spent_on = _answering([u for u in _recorder_usages if u.model])
        if spent_on is not None:
            # Failed: name the model that spent the money, as the CLI reported
            # it (gate round 2 note, 2026-11-03).
            _recorder_model = spent_on.model or _recorder_model
    await _record_claude_oauth_call(
        model=_recorder_model,
        input_tokens=_recorder_input_tokens,
        output_tokens=_recorder_output_tokens,
        success=False,
        latency_ms=int((time.monotonic() - start) * 1000),
        endpoint=endpoint,
        request_id=request_id,
        error_class=_recorder_error_class,
        usage_source=_recorder_usage_source,
        usages=_recorder_usages,
    )
    raise err


# Models whose table/CLI price disagreement was already logged in this process.
_audit_warned: set[str] = set()


def _price_attempt(usage: CliUsage, model: str) -> float:
    """One attempt's cost: pricing.py on the real counts, audited by the CLI's
    own list-price figure, which also bills a model the table does not list."""
    from backend.services.llm_clients.pricing import (
        LLM_PRICING,
        calculate_cost,
        resolve_pricing,
    )

    if usage.bill_cli_cost and usage.cli_cost_usd is not None:
        return usage.cli_cost_usd
    listed = resolve_pricing(model) is not LLM_PRICING["unknown"]
    if not listed and usage.cli_cost_usd is not None:
        return usage.cli_cost_usd
    cost = calculate_cost(
        usage.prompt_tokens,
        usage.output_tokens,
        model,
        cached_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_5m_tokens,
        cache_write_1h_tokens=usage.cache_write_1h_tokens,
    )
    cli_cost = usage.cli_cost_usd
    if (
        cli_cost is not None
        and abs(cost - cli_cost) > max(1e-4, 0.05 * cli_cost)
        and model not in _audit_warned
    ):
        # Either table can be the stale one: the Fly pin (CLI 2.1.236) predates
        # Claude Sonnet 5.5 and estimates it at $3/$15 against the official
        # $2/$10 (measured 2026-11-03). The ledger keeps pricing.py's figure;
        # the disagreement is said once per model per process, not per call.
        _audit_warned.add(model)
        logger.warning(
            "%s cost-ledger: pricing.py says $%.6f, the CLI's own estimate says $%.6f "
            "for %s — one of the two price tables may be stale (the ledger keeps "
            "pricing.py's; said once per model per process)",
            LEDGER_PROVIDER,
            cost,
            cli_cost,
            model,
        )
    return cost


async def _record_claude_oauth_call(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    success: bool,
    latency_ms: int,
    endpoint: str | None,
    request_id: str | None,
    error_class: str | None,
    usage_source: str = "estimated",
    usages: list[CliUsage] | None = None,
) -> None:
    """Record a Claude Console call to the cost ledger. Never raises.

    Provider is :data:`LEDGER_PROVIDER` (``claude_console``): these calls are
    metered per token against a Console org, unlike the retired
    ``claude_oauth`` rows whose cost was an estimate on a subscription.

    ``usage_source`` ("cli_envelope" | "estimated") records whether the token
    counts are real (parsed from the ``--output-format json`` envelope) or the
    ``len // 4`` estimate (review D7). It is logged, NOT pushed into
    ``record_llm_call``'s schema — that DB contract is shared and must not drift
    from a single side (superscar #9).

    With ``usages`` (one per attempt that reported any, failed ones included)
    each attempt is priced from ``pricing.py`` at the model that answered it —
    cache reads at the hit rate, cache writes at the 5m/1h write rates — and
    the row carries the sum. The envelope's own ``total_cost_usd`` (the CLI's
    list-price bill) audits each figure: a disagreement is logged as a stale
    table, and a model the table does not know is billed at the CLI's figure
    instead of the $1/$3 default.
    """
    try:
        from backend.services.llm_clients.pricing import calculate_cost
        from backend.services.observability import record_llm_call

        cache_hit_tokens = 0
        if not usages:
            cost_usd = calculate_cost(input_tokens, output_tokens, model)
        else:
            cost_usd = 0.0
            for usage in usages:
                cache_hit_tokens += usage.cache_read_tokens
                cost_usd += _price_attempt(usage, usage.model or model)
            cost_usd = round(cost_usd, 6)
        if usage_source == "cli_envelope":
            logger.debug(
                "%s cost-ledger: real tokens in=%d out=%d (cli_envelope)",
                LEDGER_PROVIDER,
                input_tokens,
                output_tokens,
            )
        await record_llm_call(
            provider=LEDGER_PROVIDER,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            success=success,
            latency_ms=latency_ms,
            endpoint=endpoint,
            request_id=request_id,
            error_class=error_class,
            cache_hit_tokens=cache_hit_tokens,
        )
    except Exception as exc:
        logger.warning("llm_cost recorder failed for %s: %s", LEDGER_PROVIDER, exc)


def complete(
    prompt: str,
    *,
    model: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S,
) -> ClaudeOAuthResponse:
    """Synchronous wrapper around :func:`complete_async`.

    Useful for the 3 existing call sites that are sync code paths. Does NOT
    work inside an already-running event loop — use
    :func:`complete_async` from async code.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        raise RuntimeError(
            "complete() is sync; inside a running event loop call complete_async() instead",
        )

    return asyncio.run(
        complete_async(
            prompt,
            model=model,
            timeout_s=timeout_s,
            total_timeout_s=total_timeout_s,
        )
    )
