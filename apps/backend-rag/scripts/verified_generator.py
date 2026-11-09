#!/usr/bin/env python3
"""Verified Generator — Multi-Model Orchestrator for NB Knowledge Population.

7-step pipeline ("verifica sempre la verità con la legge"):
  1. Load claims_db for domain
  2. NLM Deep Research (Gemini) — trova normative aggiornate sul web
  3. Gemini CLI search — verifica vigenza delle leggi (Google Search grounded)
  4. NLM Oracolo (notebook di dominio) — citazioni precise dai PDF legali caricati
  5. Reasoning gate — fail-closed finché il topology adapter non è armato
  6. Claude CLI — assembla documento T2 con [CLAIM-ID] markers (Max subscription)
  7. auto_verifier — CRAG-light: verifica fedeltà ≥95% di ogni citazione

Each model contributes its strength:
  - NLM Deep Research: 40+ web sources, autonomous Gemini research
  - Gemini Search: Google Search grounded, verifies current law status
  - NLM Oracolo: citations directly from uploaded legal PDFs
  - Reasoning gate: blocks until a topology adapter is armed; explicit opt-out only
  - Claude CLI: final T2 assembly with [CLAIM-ID] markers
  - auto_verifier: CRAG-light faithfulness check on every cited claim

Usage:
    python scripts/verified_generator.py \\
        --domain immigration \\
        --topic "KITAS E31 Rinnovo: Procedura Completa" \\
        --output /tmp/nb2_kitas_renewal.txt

    # Skip slow steps for quick iteration:
    python scripts/verified_generator.py \\
        --domain property \\
        --topic "HGB: Diritto di Costruzione per Stranieri" \\
        --output /tmp/nb5_hgb.txt \\
        --skip-research --skip-reasoning
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys

# Standalone scripts also resolve the shared, non-reenableable retirement policy.
import sys as _policy_sys
import tempfile
import time
from pathlib import Path
from pathlib import Path as _PolicyPath
from typing import Any

_policy_root = str(_PolicyPath(__file__).resolve().parents[1])
if _policy_root not in _policy_sys.path:
    _policy_sys.path.insert(0, _policy_root)
from backend.core.telegram_retirement import telegram_is_retired

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPTS_DIR.parents[2]
CLAIMS_DB_DIR = SCRIPTS_DIR / "claims_db"
AI_DISPATCH = PROJECT_ROOT / "scripts" / "ai-dispatch.sh"
CLAIM_ID_PATTERN = re.compile(r"\[([A-Z]{2,3}-\d{3})\]")


def _run_process_group(
    cmd: list[str],
    *,
    timeout: float,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI in its own session and reap its full process tree on timeout."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        else:
            time.sleep(0.1)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

# NLM notebook IDs per dominio (da ai-dispatch.sh oracolo-nb mapping)
DOMAIN_NB_TAGS: dict[str, str] = {
    "immigration": "immigration",
    "property": "property",
    "tax": "tax",
    "company": "company",
}

GENERATION_PROMPT = """You are a Bali Zero knowledge author writing operational guides for staff.

Domain: {domain}
Topic: {topic}
Language: Italian (professional, precise)

RESEARCH CONTEXT (from multi-model analysis):
{research_context}

LEGAL GROUNDING (from NLM notebooks — PDF citations):
{oracolo_context}

LEGAL REASONING (present only when a topology adapter is armed):
{reasoning_context}

CRITICAL RULES:
1. Every normative claim MUST be followed by its [CLAIM-ID] marker from claims_db
2. Only make claims that exist in the claims_db listed below
3. If you need to state something not in claims_db, write [NEEDS-VERIFICATION] instead
4. Structure: Introduction → Requirements → Procedure → Timing → Costs → FAQ
5. Ground every claim in actual Indonesian law — "verifica sempre la verità con la legge"

AVAILABLE CLAIMS (use [CLAIM-ID] after each referenced claim):
{claims_summary}

Write the complete operational guide now. Be thorough (800-1200 words).
Every sentence that asserts a legal fact MUST have a [CLAIM-ID] marker."""

REVISION_PROMPT = """You are a Bali Zero knowledge author revising an existing operational guide for staff.

Domain: {domain}
Topic: {topic}
Language: Italian (professional, precise)

EXISTING DOCUMENT TO REVISE:
{existing_text}

RESEARCH CONTEXT (from multi-model analysis):
{research_context}

LEGAL GROUNDING (from NLM notebooks — PDF citations):
{oracolo_context}

CRITICAL RULES:
1. Every normative claim MUST be followed by its [CLAIM-ID] marker from claims_db
2. Only make claims that exist in the claims_db listed below
3. Replace any [NEEDS-VERIFICATION] markers or unsupported claims with verified [CLAIM-ID] markers
4. Preserve the document structure where correct; fix only what is wrong
5. Ground every claim in actual Indonesian law — "verifica sempre la verità con la legge"

AVAILABLE CLAIMS (use [CLAIM-ID] after each referenced claim):
{claims_summary}

Revise the document now. Keep 800-1200 words.
Every sentence that asserts a legal fact MUST have a [CLAIM-ID] marker."""


def load_claims_db(domain: str) -> dict[str, dict[str, Any]]:
    """Load claims_db for a domain. Exits if not found."""
    path = CLAIMS_DB_DIR / f"{domain}_claims_db.json"
    if not path.exists():
        logger.error("Claims DB not found: %s — run claims_extractor.py first", path)
        sys.exit(1)
    with path.open() as f:
        raw: list[dict[str, Any]] = json.load(f)
    return {c["claim_id"]: c for c in raw if "claim_id" in c}


def build_claims_summary(claims_db: dict[str, dict[str, Any]]) -> str:
    """Build compact claims list for generation prompt (cap at 80)."""
    lines = [f"[{cid}] {c['claim']} (ref: {c['pasal_ref']})" for cid, c in list(claims_db.items())[:80]]
    if len(claims_db) > 80:
        lines.append(f"... and {len(claims_db) - 80} more claims available")
    return "\n".join(lines)


def _dispatch_reaches_claude(command: str, args: tuple[str, ...]) -> bool:
    """True when this ai-dispatch.sh invocation would run its ``run_claude``.

    ``run_claude`` (claude-review / claude-redteam / claude-explain) is the
    script's plan-OAuth cascade — CLAUDE_CODE_OAUTH_TOKEN, then the keychain
    session — shut for the product perimeter by RULED 2026-11-01 (gate round 4,
    2026-11-03). ``parallel`` re-invokes the script per ``cmd:prompt`` spec, so
    a claude-* spec inside it counts too. Same rule as
    backend.services.federation_alerts.dispatcher._reaches_claude, restated
    here because this script is import-free.
    """
    if command.startswith("claude"):
        return True
    return command == "parallel" and any(
        re.match(r"[\"']?claude[\w-]*:", arg.strip()) for arg in args
    )


def run_dispatch(command: str, *args: str, timeout: int = 300) -> str:
    """Run ai-dispatch.sh command and return stdout. Empty string on failure.

    Its Claude commands are refused before any subprocess (plan OAuth, see
    _dispatch_reaches_claude): Claude is reached in this script only through
    generate_document()'s Console seats. The child of the other commands
    carries no ANTHROPIC_* / CLAUDE_* variable.
    """
    if _dispatch_reaches_claude(command, args):
        logger.warning(
            "ai-dispatch %s refused: it reaches Claude through plan OAuth (RULED 2026-11-01)",
            command,
        )
        return ""
    if not AI_DISPATCH.exists():
        logger.warning("ai-dispatch.sh not found at %s", AI_DISPATCH)
        return ""
    cmd = [str(AI_DISPATCH), command] + list(args)
    child_env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("ANTHROPIC_", "CLAUDE_"))
    }
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(PROJECT_ROOT),
            env=child_env,
        )
        if result.returncode != 0:
            logger.warning(
                "ai-dispatch %s failed (rc=%d)",
                command,
                result.returncode,
            )
            return ""
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("ai-dispatch %s timed out after %ds", command, timeout)
        return ""
    except Exception as e:
        logger.warning("ai-dispatch %s error: %s", command, e)
        return ""


def step_nlm_research(domain: str, topic: str, skip: bool) -> str:
    """Step 2: NLM Deep Research — Gemini autonomous web research (~40 sources)."""
    if skip:
        logger.info("  [SKIP] NLM Deep Research")
        return ""
    print("\nStep 2: NLM Deep Research (Gemini) — searching web sources...")
    query = f"Indonesian law {domain}: {topic} — current regulations, requirements, procedures 2024-2025"
    output = run_dispatch("research", query, "fast", timeout=120)
    if output:
        # research start is async — extract any immediate context from the response
        print(f"  Research initiated: {output[:200]}...")
        # Return the initiation confirmation as context (actual results in NB-9)
        return f"[NLM Deep Research initiated for: {query}]\n{output[:500]}"
    print("  Research unavailable — proceeding without web research")
    return ""


def step_gemini_search(domain: str, topic: str, skip: bool) -> str:
    """Step 3: Gemini CLI search — Google Search grounded law verification."""
    if skip:
        logger.info("  [SKIP] Gemini search")
        return ""
    print("\nStep 3: Gemini CLI search — verifying current law status...")
    query = (
        f"Indonesian {domain} law: {topic}. "
        f"What are the current legal requirements, procedures, timeframes, and fees? "
        f"Cite specific regulations (UU, PP, Peraturan Menteri) with article numbers."
    )
    output = run_dispatch("search", query, timeout=120)
    if output:
        print(f"  Gemini search: {len(output)} chars of grounded context")
        return output
    print("  Gemini search unavailable — proceeding without")
    return ""


def step_nlm_oracolo(domain: str, topic: str, skip: bool) -> str:
    """Step 4: NLM Oracolo — citations from domain-specific legal PDF notebook."""
    if skip:
        logger.info("  [SKIP] NLM Oracolo")
        return ""
    nb_tag = DOMAIN_NB_TAGS.get(domain, domain)
    print(f"\nStep 4: NLM Oracolo (NB: {nb_tag}) — querying legal PDF citations...")
    question = (
        f"What are the specific legal requirements, procedures, timeframes, and fees for: {topic}? "
        f"Provide exact citations with Pasal numbers."
    )
    output = run_dispatch("oracolo-nb", nb_tag, question, timeout=120)
    if output:
        print(f"  Oracolo: {len(output)} chars of cited legal context")
        return output
    print("  Oracolo unavailable — proceeding without PDF citations")
    return ""


def step_reasoning_gate(skip: bool) -> str:
    """Block generation until a topology adapter exists or an operator opts out."""
    if skip:
        logger.warning("  [EXPLICIT SKIP] Topology reasoning layer disabled by operator")
        return ""
    logger.error(
        "Required reasoning adapter is not armed; aborting verified generation "
        "before any provider call. The legacy direct DeepSeek dispatcher route "
        "is retired. Use "
        "--skip-reasoning only to proceed explicitly without this layer."
    )
    raise SystemExit(2)


_GEN_RATE_LIMIT_RE = re.compile(
    r"rate.?limit|too many requests|(?<![\d/])429(?![\d/])|exhausted|quota|hit your limit|"
    r"usage limit|weekly limit|timeout after 90s|possibly rate limit|"
    r"capacity|overloaded",
    re.IGNORECASE,
)
_GEN_AUTH_RE = re.compile(
    r"authentication (?:failed|required|expired)|auth required|login required|"
    r"please (?:log in|login)|not logged in|not authenticated|"
    r"invalid[_ ](?:grant|token)|token[_ ]revoked|refresh_token|"
    r"unauthori[sz]ed|(?<![\d/])401(?![\d/])",
    re.IGNORECASE,
)
_GEN_RATE_DIAGNOSTIC_RE = re.compile(
    r"\s*(?:error(?:\s*[:\-]\s*|\s+))?(?:rate.?limit(?:ed| reached| exceeded)?|"
    r"too many requests|(?:http(?: status)?\s*)?429(?:\b.*)?|"
    r"(?:quota|usage limit|weekly limit|capacity)\s+"
    r"(?:exhausted|exceeded|reached|unavailable)(?:\b.*)?|"
    r"(?:you(?:'ve| have)\s+)?hit your limit(?:\b.*)?|"
    r"out of extra usage(?:\b.*)?|service (?:is )?overloaded(?:\b.*)?)\s*",
    re.IGNORECASE | re.DOTALL,
)
_GEN_AUTH_DIAGNOSTIC_RE = re.compile(
    r"\s*(?:error(?:\s*[:\-]\s*|\s+))?(?:401\s+unauthori[sz]ed(?:\b.*)?|"
    r"unauthori[sz]ed(?:\b.*)?|authentication (?:failed|required|expired)(?:\b.*)?|"
    r"auth required(?:\b.*)?|login required(?:\b.*)?|"
    r"please (?:log in|login)(?:\b.*)?|not (?:logged in|authenticated)(?:\b.*)?|"
    r"invalid[_ ](?:grant|token)(?:\b.*)?|token[_ ]revoked(?:\b.*)?|"
    r"refresh_token(?:\b.*)?|"
    # Console seat refusals, measured 2026-11-03 (bad key / header missing /
    # key pinned to the wrong workspace — the CLI words the last as a model problem).
    # This regex is FULL-matched on stdout: the three shapes carry no prose tail,
    # so an answer that merely begins with the same words stays an answer.
    r"invalid api key(?:\s*[\u00b7\u2022\-]\s*fix external api key)?\.?|"
    r"api error:\s*400\s+this api key is not scoped to a workspace, so this request must include the "
    r"anthropic-workspace-id header with the id of the workspace to use\.(?:\s+add the header, "
    r"or use an api key that is scoped to a workspace\.)?|"
    r"there(?:'s| is) an issue with the selected model \(claude-[a-z0-9][a-z0-9._-]{1,63}\)\. "
    r"it may not exist or you may not have access to it\.(?:\s*run --model to pick a different model\.)?)\s*",
    re.IGNORECASE | re.DOTALL,
)
_GEN_EXHAUSTED: dict[str, str] = {}
# Console seats — RULED 2026-11-01: a product lane never falls back to plan
# OAuth (CLAUDE_CODE_OAUTH_TOKEN) or the CLI's own session. Same contract as
# backend.llm.claude_oauth_client (ANTHROPIC_CONSOLE_KEY_<n> pinned to
# ANTHROPIC_CONSOLE_WORKSPACE_<n>, n in 1..3), restated here on purpose: this
# script is import-free so it runs under launchd/cron on an interpreter that
# has no backend package. test_claude_oauth_client.py::
# test_product_perimeter_reads_no_plan_oauth keeps both on the same contract.
_CONSOLE_SEAT_SLOTS = (1, 2, 3)
_WORKSPACE_ID_RE = re.compile(r"^wrkspc_[A-Za-z0-9]{1,64}$")
# What the child MAY inherit — an ALLOWLIST, the same one as
# backend.llm.claude_oauth_client._CHILD_ENV_ALLOW_* (pinned by
# test_seat_env_scrub_parity_across_spawners). Three gate rounds (2026-11-03)
# each found more variables the CLI honours to pick a credential, an endpoint
# or a credential store; a denylist can never be complete (cicatrix #3).
_CHILD_ENV_ALLOW_EXACT = frozenset(
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
_CHILD_ENV_ALLOW_PREFIXES = ("LC_",)


# The model this script runs on (2026-11-03, the owner's pick: Claude Sonnet 5.5,
# $2/$10 per MTok). Unpinned, the CLI's default answered on claude-fable-5-1 at
# $10/$50. Restates backend.llm.claude_oauth_client.DEFAULT_MODEL /
# DEFAULT_MODEL_ENV (this script runs without the backend package); a unit test
# pins the two copies together.
_DEFAULT_MODEL = "claude-sonnet-5-5"
_DEFAULT_MODEL_ENV = "NUZANTARA_CLAUDE_DEFAULT_MODEL"
_MODEL_SLUG_RE = re.compile(r"^claude-[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")


def _gen_model() -> str:
    """The pinned model, or ``$NUZANTARA_CLAUDE_DEFAULT_MODEL`` when it is a plain
    ``claude-*`` slug (it reaches argv)."""
    override = os.environ.get(_DEFAULT_MODEL_ENV, "").strip()
    return override if _MODEL_SLUG_RE.match(override) else _DEFAULT_MODEL


def _gen_seat_env(key: str, workspace: str) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name in _CHILD_ENV_ALLOW_EXACT or name.startswith(_CHILD_ENV_ALLOW_PREFIXES)
    }
    env["ANTHROPIC_API_KEY"] = key
    # A user-scoped key answers 400 without its workspace header (measured 2026-11-03).
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"anthropic-workspace-id: {workspace}"
    # W132: a sterile config dir — no control-plane hooks, and no session to
    # fall back to, which with a Console seat is a feature.
    headless = os.environ.get("CLAUDE_OAUTH_HEADLESS_CONFIG_DIR", "").strip()
    env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(headless or "~/.claude-oauth-headless")
    # The CLI's non-essential traffic (telemetry, error reporting, update checks)
    # is waited on around every one-shot call: on Pro, CLI 2.1.317 answered in
    # 3.0 s with it and 1.75 s without, the API's share ~1.1 s either way
    # (measured 2026-11-03). It carries nothing a headless call needs.
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def _gen_seat_chain() -> list[tuple[str, str, str]]:
    """``(label, key, workspace)`` per configured Console slot, in order.

    A slot is a seat only when both variables are set and the workspace id is
    well-formed (it travels as a header line). Empty list = no door: the
    caller reports it instead of trying a credential it must not use.
    """
    chain: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for i in _CONSOLE_SEAT_SLOTS:
        key = os.environ.get(f"ANTHROPIC_CONSOLE_KEY_{i}", "").strip()
        workspace = os.environ.get(f"ANTHROPIC_CONSOLE_WORKSPACE_{i}", "").strip()
        if not key or not _WORKSPACE_ID_RE.match(workspace) or key in seen:
            continue
        seen.add(key)
        chain.append((f"console_{i}", key, workspace))
    return chain


def _gen_retry_reason(stdout: str, stderr: str) -> str | None:
    if _GEN_RATE_LIMIT_RE.search(stderr or ""):
        return "rate_limit"
    if _GEN_AUTH_RE.search(stderr or ""):
        return "auth"
    stripped = (stdout or "").strip()
    if not stripped:
        return None
    try:
        envelope = json.loads(stripped)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and (
        envelope.get("is_error")
        or envelope.get("type") == "error"
        or (
            envelope.get("type") == "result"
            and envelope.get("subtype") not in (None, "success")
        )
    ):
        diagnostic = " ".join(
            str(envelope.get(key, ""))
            for key in ("error", "message", "result", "subtype")
        )
        if _GEN_RATE_LIMIT_RE.search(diagnostic):
            return "rate_limit"
        if _GEN_AUTH_RE.search(diagnostic):
            return "auth"
        return None
    if _GEN_RATE_DIAGNOSTIC_RE.fullmatch(stripped):
        return "rate_limit"
    if _GEN_AUTH_DIAGNOSTIC_RE.fullmatch(stripped):
        return "auth"
    return None


def generate_document(
    domain: str,
    topic: str,
    claims_db: dict[str, dict[str, Any]],
    research_context: str,
    oracolo_context: str,
    reasoning_context: str,
    existing_text: str | None = None,
) -> str:
    """Step 6: Generate T2 document via the claude CLI on a Console seat.
    Fallback across the configured seats: console_1→2→3 (RULED 2026-11-01:
    no plan-OAuth or keychain seat)."""
    claims_summary = build_claims_summary(claims_db)

    research_ctx = (research_context[:2000] if research_context else "Not available — proceed from claims_db only")
    oracolo_ctx = (oracolo_context[:3000] if oracolo_context else "Not available — proceed from claims_db only")
    reasoning_ctx = (reasoning_context[:2000] if reasoning_context else "Not available")

    if existing_text is not None:
        prompt = REVISION_PROMPT.format(
            domain=domain,
            topic=topic,
            existing_text=existing_text,
            research_context=research_ctx,
            oracolo_context=oracolo_ctx,
            claims_summary=claims_summary,
        )
    else:
        prompt = GENERATION_PROMPT.format(
            domain=domain,
            topic=topic,
            research_context=research_ctx,
            oracolo_context=oracolo_ctx,
            reasoning_context=reasoning_ctx,
            claims_summary=claims_summary,
        )

    deadline = time.monotonic() + 300
    chain = _gen_seat_chain()
    if not chain:
        logger.error(
            "generate_document: no Console seat configured "
            "(ANTHROPIC_CONSOLE_KEY_<n> + ANTHROPIC_CONSOLE_WORKSPACE_<n>)"
        )
        sys.exit(1)
    for position, (label, key, workspace) in enumerate(chain):
        if label in _GEN_EXHAUSTED:
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempt_timeout = max(0.1, remaining / (len(chain) - position))
        env = _gen_seat_env(key, workspace)

        try:
            result = _run_process_group(
                # Lean and toolless (2026-11-03): research, grounding and claims
                # are in the prompt, so no tool is needed — and with none there
                # is nothing for a permission bypass to wave through.
                ["claude", "--print", "--bare", "--tools", "",
                 "--model", _gen_model(), "--max-budget-usd", "3"],
                input=prompt,
                timeout=attempt_timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            logger.warning("generate_document: %s timed out", label)
            _GEN_EXHAUSTED[label] = "timeout"
            continue

        retry_reason = _gen_retry_reason(result.stdout, result.stderr)
        if retry_reason is not None:
            logger.warning("generate_document: %s unavailable (%s)", label, retry_reason)
            _GEN_EXHAUSTED[label] = retry_reason
            continue
        if result.returncode != 0:
            logger.error("claude CLI error via %s (exit=%s)", label, result.returncode)
            sys.exit(1)

        output = result.stdout.strip()
        if not output:
            logger.warning("generate_document: %s returned empty output", label)
            _GEN_EXHAUSTED[label] = "empty_output"
            continue
        return output

    logger.error("generate_document: all Console seats exhausted")
    sys.exit(1)


def validate_markers(document_text: str, claims_db: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Step 6b: Check all [CLAIM-ID] markers exist in claims_db. Returns (valid, missing)."""
    found = list(set(CLAIM_ID_PATTERN.findall(document_text)))
    missing = [cid for cid in found if cid not in claims_db]
    valid = [cid for cid in found if cid in claims_db]
    return valid, missing


def run_auto_verifier(document_path: str, claims_db_path: str) -> tuple[int, str]:
    """Step 7: Run auto_verifier.py subprocess. Returns (exit_code, report_path)."""
    fd, report_path = tempfile.mkstemp(suffix="_verification_report.json")
    os.close(fd)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "auto_verifier.py"),
         "--document", document_path,
         "--claims-db", claims_db_path,
         "--output", report_path],
        capture_output=True, text=True,
    )
    return result.returncode, report_path


def run_telegram_review(document_path: str, report_path: str, document_name: str) -> str:
    """Optional: Run telegram_reviewer.py subprocess. Returns decision string."""
    if telegram_is_retired():
        return "retired"
    reviewer = SCRIPTS_DIR / "telegram_reviewer.py"
    if not reviewer.exists():
        logger.info("telegram_reviewer.py not found — skipping human review")
        return "skipped"
    result = subprocess.run(
        [sys.executable, str(reviewer),
         "--report", report_path,
         "--document-name", document_name],
        capture_output=False,
    )
    return "approved" if result.returncode == 0 else "rejected"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Multi-model Verified Generation Pipeline for NB knowledge population")
    parser.add_argument("--domain", required=True, choices=["immigration", "property", "tax", "company"])
    parser.add_argument("--topic", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--existing", default=None, help="Path to existing document to revise (optional)")
    parser.add_argument("--skip-research", action="store_true", help="Skip NLM Deep Research (faster)")
    parser.add_argument("--skip-search", action="store_true", help="Skip Gemini search (faster)")
    parser.add_argument("--skip-oracolo", action="store_true", help="Skip NLM Oracolo (faster)")
    parser.add_argument(
        "--skip-reasoning",
        action="store_true",
        help="Explicitly accept generation while the required topology adapter is unarmed",
    )
    parser.add_argument("--skip-telegram", action="store_true", help="Skip Telegram human review")
    args = parser.parse_args()

    # The topology reasoner adapter is not armed. Enforce the explicit opt-out
    # before research, search, NLM, generation, or any other provider call.
    reasoning_context = step_reasoning_gate(args.skip_reasoning)

    print(f"\nVerified Generation Pipeline — {args.domain} / {args.topic}")
    print("=" * 70)
    print(
        "Models: NLM Deep Research → Gemini Search → NLM Oracolo → "
        "Reasoning gate → Claude → CRAG"
    )

    # Step 1: Load claims_db
    print("\nStep 1: Loading claims_db...")
    claims_db = load_claims_db(args.domain)
    print(f"  {len(claims_db)} claims loaded")

    # Step 2: NLM Deep Research (Gemini autonomous web search)
    research_context = step_nlm_research(args.domain, args.topic, args.skip_research)

    # Step 3: Gemini CLI search (Google Search grounded)
    search_context = step_gemini_search(args.domain, args.topic, args.skip_search)

    # Step 4: NLM Oracolo (domain legal PDF citations)
    oracolo_context = step_nlm_oracolo(args.domain, args.topic, args.skip_oracolo)

    # Step 5: the gate was evaluated before Step 1 and yielded explicit opt-out context.

    # Step 6: Claude CLI assembles T2 document with all context
    existing_text: str | None = None
    if args.existing:
        existing_text = Path(args.existing).read_text(encoding="utf-8")
        print("\nStep 6: Revising existing document via Claude CLI (Max)...")
    else:
        print("\nStep 6: Generating document via Claude CLI (Max)...")

    document_text = generate_document(
        args.domain, args.topic, claims_db,
        research_context or search_context,  # prefer research, fallback to search
        oracolo_context,
        reasoning_context,
        existing_text,
    )
    output_path = Path(args.output)
    output_path.write_text(document_text, encoding="utf-8")
    print(f"  Generated {len(document_text)} chars → {output_path}")

    # Step 6b: Validate [CLAIM-ID] markers
    print("\nStep 6b: Validating [CLAIM-ID] markers...")
    valid_ids, missing_ids = validate_markers(document_text, claims_db)
    print(f"  Valid: {len(valid_ids)}, Missing from DB: {len(missing_ids)}")
    if missing_ids:
        print(f"  WARNING: Unknown claim IDs will be flagged UNFAITHFUL: {missing_ids}")

    # Step 7: CRAG-light auto-verification
    print("\nStep 7: Running auto-verifier (CRAG-light)...")
    claims_db_path = str(CLAIMS_DB_DIR / f"{args.domain}_claims_db.json")
    exit_code, report_path = run_auto_verifier(str(output_path), claims_db_path)

    if exit_code == 0:
        print("  PASSED — all claims verified ≥95%")
        print(f"\nPipeline COMPLETE — document ready: {output_path}")
        print("  Next: upload to NLM using: nlm source_add --notebook-id <NB_ID> --source-type text")
        sys.exit(0)

    # Verification failed — load report
    try:
        with open(report_path) as f:
            report = json.load(f)
        ratio = report.get("verified_ratio", 0)
        print(f"  BLOCKED — {ratio:.0%} verified (need ≥95%)")
        print(f"  Blocked claims: {report.get('blocked_claims', [])}")
    except (OSError, json.JSONDecodeError):
        print("  BLOCKED — could not load verification report")

    # Optional: Telegram human review
    if not args.skip_telegram and not telegram_is_retired():
        print("\nStep 7b: Requesting human review via Telegram...")
        decision = run_telegram_review(str(output_path), report_path, f"{args.domain} — {args.topic}")
        if decision == "approved":
            print(f"\nApproved — document ready: {output_path}")
            sys.exit(0)
        elif decision == "skipped":
            print(f"\nVerification failed — review report: {report_path}")
            sys.exit(1)
        else:
            print("\nRejected — document NOT uploaded. Fix required.")
            print(f"  Review report: {report_path}")
            sys.exit(1)
    else:
        print(f"\nVerification failed — review report: {report_path}")
        sys.exit(1)


if __name__ == "__main__":
    main()
