#!/usr/bin/env python3
"""Auto-Verifier — Step 4 of the Verified Generation Pipeline.

Verifies every [CLAIM-ID] marker in a generated T2 document against
claims_db.json using CRAG-light (Claude Haiku 4.5 as evaluator).

Exit codes:
  0 — verification passed (>=95% claims verified)
  1 — verification failed (<95% or DB errors)

Usage:
    python scripts/auto_verifier.py \\
        --document /tmp/nb2_visa_guide.txt \\
        --claims-db scripts/claims_db/immigration_claims_db.json \\
        --output /tmp/verification_report.json
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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CLAIM_ID_PATTERN = re.compile(r"\[([A-Z]{2,3}-\d{3})\]")
MIN_VERIFIED_RATIO = 0.95


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


@dataclass
class ClaimVerificationResult:
    claim_id: str
    verdict: str        # "FAITHFUL" | "UNFAITHFUL" | "UNCERTAIN"
    reason: str


@dataclass
class VerificationReport:
    total_claims: int
    verified_count: int                                                     # count of FAITHFUL verdicts
    verified_ratio: float
    passed: bool                                                            # ratio >= MIN_VERIFIED_RATIO
    results: list[ClaimVerificationResult] = field(default_factory=list)   # ALL results
    blocked_claims: list[str] = field(default_factory=list)                # IDs with UNFAITHFUL/UNCERTAIN


def extract_claim_ids(document_text: str) -> list[str]:
    """Extract unique [CLAIM-ID] markers from document, in order of first appearance."""
    seen: set[str] = set()
    unique: list[str] = []
    for cid in CLAIM_ID_PATTERN.findall(document_text):
        if cid not in seen:
            seen.add(cid)
            unique.append(cid)
    return unique


def load_claims_db(path: str) -> dict[str, dict[str, Any]]:
    """Load claims_db.json into a dict keyed by claim_id."""
    with open(path) as f:
        raw: list[dict[str, Any]] = json.load(f)
    return {c["claim_id"]: c for c in raw if "claim_id" in c}


def build_haiku_verification_prompt(claim: str, document_excerpt: str) -> str:
    return f"""You are a legal accuracy evaluator. Determine if the following claim faithfully and accurately represents the source document excerpt.

CLAIM (Italian): {claim}
DOCUMENT EXCERPT (source text): {document_excerpt}

Answer with exactly one of: FAITHFUL, UNFAITHFUL, or UNCERTAIN.
Then on a new line explain in one sentence why.

Format your response as:
VERDICT: <FAITHFUL|UNFAITHFUL|UNCERTAIN>
REASON: <one sentence>"""


_VERIFIER_RATE_LIMIT_RE = re.compile(
    r"rate.?limit|too many requests|(?<![\d/])429(?![\d/])|exhausted|quota|hit your limit|"
    r"usage limit|weekly limit|timeout after 90s|possibly rate limit|"
    r"capacity|overloaded",
    re.IGNORECASE,
)
_VERIFIER_AUTH_RE = re.compile(
    r"authentication (?:failed|required|expired)|auth required|login required|"
    r"please (?:log in|login)|not logged in|not authenticated|"
    r"invalid[_ ](?:grant|token)|token[_ ]revoked|refresh_token|"
    r"unauthori[sz]ed|(?<![\d/])401(?![\d/])",
    re.IGNORECASE,
)
_VERIFIER_RATE_DIAGNOSTIC_RE = re.compile(
    r"\s*(?:error(?:\s*[:\-]\s*|\s+))?(?:rate.?limit(?:ed| reached| exceeded)?|"
    r"too many requests|(?:http(?: status)?\s*)?429(?:\b.*)?|"
    r"(?:quota|usage limit|weekly limit|capacity)\s+"
    r"(?:exhausted|exceeded|reached|unavailable)(?:\b.*)?|"
    r"(?:you(?:'ve| have)\s+)?hit your limit(?:\b.*)?|"
    r"out of extra usage(?:\b.*)?|service (?:is )?overloaded(?:\b.*)?)\s*",
    re.IGNORECASE | re.DOTALL,
)
_VERIFIER_AUTH_DIAGNOSTIC_RE = re.compile(
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
_VERIFIER_EXHAUSTED: dict[str, str] = {}
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


def _verifier_model() -> str:
    """The pinned model, or ``$NUZANTARA_CLAUDE_DEFAULT_MODEL`` when it is a plain
    ``claude-*`` slug (it reaches argv)."""
    override = os.environ.get(_DEFAULT_MODEL_ENV, "").strip()
    return override if _MODEL_SLUG_RE.match(override) else _DEFAULT_MODEL


def _verifier_seat_env(key: str, workspace: str) -> dict[str, str]:
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


def _verifier_seat_chain() -> list[tuple[str, str, str]]:
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


def _verifier_retry_reason(stdout: str, stderr: str) -> str | None:
    """Classify account-local diagnostics without scanning valid stdout prose."""
    if _VERIFIER_RATE_LIMIT_RE.search(stderr or ""):
        return "rate_limit"
    if _VERIFIER_AUTH_RE.search(stderr or ""):
        return "auth"

    stripped = (stdout or "").strip()
    if not stripped:
        return None
    diagnostic = ""
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
        if _VERIFIER_RATE_LIMIT_RE.search(diagnostic):
            return "rate_limit"
        if _VERIFIER_AUTH_RE.search(diagnostic):
            return "auth"
        return None
    if _VERIFIER_RATE_DIAGNOSTIC_RE.fullmatch(stripped):
        return "rate_limit"
    if _VERIFIER_AUTH_DIAGNOSTIC_RE.fullmatch(stripped):
        return "auth"
    return None


def call_claude_verifier(
    claim_id: str, claim_text: str, document_excerpt: str
) -> ClaimVerificationResult:
    """Verify a single claim via the claude CLI on a Console seat.
    Fallback across the configured seats: console_1→2→3 (RULED 2026-11-01:
    no plan-OAuth or keychain seat)."""
    prompt = build_haiku_verification_prompt(claim_text, document_excerpt)
    deadline = time.monotonic() + 60
    chain = _verifier_seat_chain()
    if not chain:
        return ClaimVerificationResult(
            claim_id=claim_id,
            verdict="UNCERTAIN",
            reason="no Console seat configured (ANTHROPIC_CONSOLE_KEY_<n> + "
            "ANTHROPIC_CONSOLE_WORKSPACE_<n>)",
        )

    for position, (label, key, workspace) in enumerate(chain):
        if label in _VERIFIER_EXHAUSTED:
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempt_timeout = max(0.1, remaining / (len(chain) - position))
        env = _verifier_seat_env(key, workspace)

        try:
            result = _run_process_group(
                # Lean and toolless (2026-11-03): the claim and the excerpt are
                # in the prompt, so no tool is needed — and with none there is
                # nothing for a permission bypass to wave through.
                ["claude", "--print", "--bare", "--tools", "",
                 "--model", _verifier_model(), "--max-budget-usd", "1"],
                input=prompt,
                timeout=attempt_timeout, env=env,
            )
        except subprocess.TimeoutExpired:
            _VERIFIER_EXHAUSTED[label] = "timeout"
            continue

        retry_reason = _verifier_retry_reason(result.stdout, result.stderr)
        if retry_reason is not None:
            _VERIFIER_EXHAUSTED[label] = retry_reason
            continue
        if result.returncode != 0:
            return ClaimVerificationResult(
                claim_id=claim_id,
                verdict="UNCERTAIN",
                reason=f"Claude CLI failed via {label}",
            )

        text = result.stdout.strip()
        if not text:
            _VERIFIER_EXHAUSTED[label] = "empty_output"
            continue
        verdict, reason = "UNCERTAIN", text
        for line in text.split("\n"):
            if line.startswith("VERDICT:"):
                verdict = line.split(":", 1)[1].strip()
            elif line.startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
        return ClaimVerificationResult(claim_id=claim_id, verdict=verdict, reason=reason)

    # All seats exhausted
    return ClaimVerificationResult(
        claim_id=claim_id, verdict="UNCERTAIN", reason="All Console seats exhausted"
    )


def verify_document(document_text: str, claims_db_path: str) -> VerificationReport:
    """Run full CRAG-light verification on a document."""
    claims_db = load_claims_db(claims_db_path)
    claim_ids = extract_claim_ids(document_text)
    total_claims = len(claim_ids)

    all_results: list[ClaimVerificationResult] = []
    verified_count = 0

    for cid in claim_ids:
        if cid not in claims_db:
            logger.warning("Claim %s not found in claims_db", cid)
            result = ClaimVerificationResult(
                claim_id=cid,
                verdict="UNFAITHFUL",
                reason="Claim ID not found in claims_db",
            )
        else:
            cd = claims_db[cid]
            document_excerpt = f"{cd.get('verbatim', '')} ({cd.get('pasal_ref', '')})"
            result = call_claude_verifier(cid, cd["claim"], document_excerpt)
            if result.verdict == "FAITHFUL":
                verified_count += 1
            else:
                logger.warning("Claim %s: %s — %s", cid, result.verdict, result.reason)

        all_results.append(result)

    verified_ratio = verified_count / total_claims if total_claims > 0 else 0.0
    passed = verified_ratio >= MIN_VERIFIED_RATIO
    blocked_claims = [r.claim_id for r in all_results if r.verdict != "FAITHFUL"]

    return VerificationReport(
        total_claims=total_claims,
        verified_count=verified_count,
        verified_ratio=verified_ratio,
        passed=passed,
        results=all_results,
        blocked_claims=blocked_claims,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--document", required=True)
    parser.add_argument("--claims-db", required=True)
    parser.add_argument("--output", default="/tmp/verification_report.json")
    args = parser.parse_args()

    document_text = Path(args.document).read_text(encoding="utf-8")
    report = verify_document(document_text, args.claims_db)

    with open(args.output, "w") as f:
        json.dump(asdict(report), f, ensure_ascii=False, indent=2)

    status = "PASSED" if report.passed else "BLOCKED"
    print(f"\n{'OK' if report.passed else 'FAIL'} {status} — Verified {report.verified_count}/{report.total_claims} ({report.verified_ratio:.1%})")
    if not report.passed:
        print(f"Blocked: {report.blocked_claims}")
    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
