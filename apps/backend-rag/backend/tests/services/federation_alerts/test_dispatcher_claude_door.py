"""dispatch_via_ai_dispatch never reaches ai-dispatch.sh's Claude path (RULED 2026-11-01).

Gate round 3 (2026-11-03): `dispatch_via_ai_dispatch("claude-explain", ...)` ran
the script's `run_claude`, which tries CLAUDE_CODE_OAUTH_TOKEN and then the
keychain session — plan OAuth from the product perimeter. These cases pin the
refusal (guilt) and keep the non-Claude commands working (innocence).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.services.federation_alerts import dispatcher


class _Proc:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok", b""


@pytest.mark.parametrize(
    ("command", "prompt"),
    [
        ("claude-explain", "why"),
        ("claude-review", "diff"),
        ("claude-redteam", "plan"),
        ("parallel", 'gemini-scan:"a" claude-review:"b"'),
    ],
)
def test_a_claude_command_is_refused_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, command: str, prompt: str
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-plan")
    launched: list[tuple[Any, ...]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        launched.append(args)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = asyncio.run(dispatcher.dispatch_via_ai_dispatch(command, prompt))

    assert result.success is False
    assert "plan OAuth" in result.stderr
    assert launched == []


def test_a_non_claude_command_runs_without_any_anthropic_or_claude_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-plan")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_CONSOLE_KEY_1", "synthetic-seat")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/interactive")
    monkeypatch.setenv("NUZANTARA_SAFE_MARKER", "kept")
    seen: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        seen["args"] = args
        seen["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(dispatcher, "AI_DISPATCH_SCRIPT", dispatcher.PROJECT_ROOT / "README.md")
    result = asyncio.run(dispatcher.dispatch_via_ai_dispatch("gemini-scan", "x"))

    assert result.success is True
    assert seen["args"][1] == "gemini-scan"
    assert not any(k.startswith(("ANTHROPIC_", "CLAUDE_")) for k in seen["env"])
    assert seen["env"]["NUZANTARA_SAFE_MARKER"] == "kept"
