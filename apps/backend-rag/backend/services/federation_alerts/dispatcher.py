"""Subprocess wrapper around scripts/ai-dispatch.sh + ConsiglioV1.

Per spec B1: the daemon dispatches multi-LLM via subprocess to the
existing federation CLI cascade — no aspirational ADK+A2A layer.

Per spec B9: ConsiglioV1.deliberate() is sync (subprocess.run inside).
We wrap it in asyncio.to_thread() with a hard timeout so the daemon
event loop never blocks indefinitely if 2/4 LLMs are down.

Per RULED 2026-11-01: no Anthropic credential or Claude selector reaches the
ai-dispatch.sh child (``ANTHROPIC_*`` and ``CLAUDE_*`` are stripped), and the
script's Claude commands — its plan-OAuth cascade — are refused before any
subprocess; product code reaches Claude through backend.llm.claude_oauth_client.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[5]
AI_DISPATCH_SCRIPT = PROJECT_ROOT / "scripts" / "ai-dispatch.sh"


def _safe_env() -> dict[str, str]:
    """Return os.environ without any Anthropic credential or Claude selector.

    ai-dispatch.sh runs non-Claude CLIs here; none of them needs an
    ``ANTHROPIC_*`` or ``CLAUDE_*`` variable, and none may reach a child that
    could hand it to a ``claude`` it starts (RULED 2026-11-01).
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANTHROPIC_", "CLAUDE_"))
    }


def _reaches_claude(command: str, prompt: str) -> bool:
    """True when this ai-dispatch.sh invocation would run its ``run_claude``.

    ``run_claude`` (claude-review / claude-redteam / claude-explain) is the
    script's plan-OAuth cascade — CLAUDE_CODE_OAUTH_TOKEN, then the keychain
    session — which RULED 2026-11-01 shuts for the product perimeter (gate
    round 3, 2026-11-03). ``parallel`` re-invokes the script per ``cmd:prompt``
    spec, so a claude-* spec inside it counts too.
    """
    if command.startswith("claude"):
        return True
    return command == "parallel" and re.search(r"(?:^|\s)[\"']?claude[\w-]*:", prompt) is not None


@dataclass(frozen=True)
class DispatchResult:
    """Structured result of one ai-dispatch.sh invocation."""

    success: bool
    stdout: str
    stderr: str
    return_code: int
    duration_sec: float
    timed_out: bool = False


async def dispatch_via_ai_dispatch(
    command: str,
    prompt: str,
    *,
    timeout_sec: int = 120,
    cwd: Path | None = None,
) -> DispatchResult:
    """Run scripts/ai-dispatch.sh with the given command + prompt.

    The shell script itself implements the LLM cascade for the non-Claude
    CLIs. Its Claude commands (claude-*, or a ``parallel`` spec naming one) are
    refused before any subprocess: they run on plan OAuth (RULED 2026-11-01).

    Returns DispatchResult — never raises on subprocess failure. The
    daemon decides whether a non-zero return code is a hard error or
    just degraded mode.
    """
    if _reaches_claude(command, prompt):
        # No subprocess: the script's Claude path is plan OAuth. A product
        # caller that needs Claude uses backend.llm.claude_oauth_client, which
        # runs on a Console seat.
        return DispatchResult(
            success=False,
            stdout="",
            stderr=(
                f"refused: ai-dispatch.sh {command} reaches Claude through plan OAuth "
                "(RULED 2026-11-01) — use backend.llm.claude_oauth_client (Console seats)"
            ),
            return_code=-1,
            duration_sec=0.0,
        )
    if not AI_DISPATCH_SCRIPT.exists():
        return DispatchResult(
            success=False,
            stdout="",
            stderr=f"ai-dispatch.sh not found at {AI_DISPATCH_SCRIPT}",
            return_code=-1,
            duration_sec=0.0,
        )

    proc = await asyncio.create_subprocess_exec(
        str(AI_DISPATCH_SCRIPT),
        command,
        prompt,
        cwd=str(cwd or PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_safe_env(),
    )

    loop = asyncio.get_event_loop()
    started_at = loop.time()
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        elapsed = loop.time() - started_at
        logger.warning(
            "ai-dispatch timeout: %s after %.1fs (rc=killed)",
            command,
            elapsed,
        )
        return DispatchResult(
            success=False,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            return_code=-9,
            duration_sec=elapsed,
            timed_out=True,
        )

    elapsed = loop.time() - started_at
    return DispatchResult(
        success=(proc.returncode == 0),
        stdout=(stdout_bytes or b"").decode("utf-8", errors="replace"),
        stderr=(stderr_bytes or b"").decode("utf-8", errors="replace"),
        return_code=proc.returncode if proc.returncode is not None else -1,
        duration_sec=elapsed,
    )


async def deliberate_with_deadline(
    consiglio: Any,
    question_prompt: str,
    *,
    deadline_sec: int = 180,
    members: tuple[str, ...] | None = None,
    context_files: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap blocking ConsiglioV1.deliberate() with hard async timeout.

    Returns a normalized dict with keys:
        passed: bool          — gate_6_passes equivalent (≥3/4 active)
        active_llms: int
        claims: list[dict]    — per-claim votes
        meta: dict
        errors: dict          — present if timeout or crash

    On timeout, returns {passed: False, errors: {deadline: ...}} so
    callers can quarantine the proposal without blocking forever.
    """

    def _run_sync() -> Any:
        kwargs: dict[str, Any] = {}
        if members is not None:
            kwargs["members"] = members
        if context_files is not None:
            kwargs["context_files"] = context_files
        return consiglio.deliberate(question_prompt, **kwargs)

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_run_sync), timeout=deadline_sec)
    except asyncio.TimeoutError:
        logger.warning("consiglio_deadline_exceeded after %ds", deadline_sec)
        return {
            "passed": False,
            "active_llms": 0,
            "claims": [],
            "meta": {},
            "errors": {"deadline": f"exceeded {deadline_sec}s"},
        }
    except Exception as exc:
        logger.exception("consiglio_crashed: %s", exc)
        return {
            "passed": False,
            "active_llms": 0,
            "claims": [],
            "meta": {},
            "errors": {"crash": str(exc)[:500]},
        }

    # Normalize ConsiglioResult dataclass to a plain dict
    active = int(result.meta.get("active_llms", 0))
    return {
        "passed": active >= 3,  # Gate 6 default ≥3/4
        "active_llms": active,
        "claims": [{"key": c.key, "value": c.value, "votes": dict(c.votes)} for c in result.claims],
        "meta": dict(result.meta),
        "errors": {},
    }


def quick_subprocess_check() -> bool:
    """Synchronous startup probe: confirm ai-dispatch.sh is executable.

    Returns True on success. The daemon refuses to leave 'observe' mode
    if this probe fails (B1 mitigation: fail closed).
    """
    if not AI_DISPATCH_SCRIPT.exists():
        return False
    try:
        result = subprocess.run(
            [str(AI_DISPATCH_SCRIPT), "help"],
            capture_output=True,
            timeout=10,
            check=False,
            env=_safe_env(),
        )
        return result.returncode in (0, 1)  # help often exits 1; just need it to RUN
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ai-dispatch probe failed: %s", exc)
        return False


__all__ = [
    "DispatchResult",
    "deliberate_with_deadline",
    "dispatch_via_ai_dispatch",
    "quick_subprocess_check",
]
