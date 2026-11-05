"""Unit tests for CLI runners + JSON extractor."""

from __future__ import annotations

import pytest

from backend.services.council.cli_runners import (
    ClaudeCLIRunner,
    CLIRunnerError,
    GeminiCLIRunner,
    KimiCLIRunner,
    _extract_json,
)

# ── JSON extractor ─────────────────────────────────────────────────────


def test_extract_json_from_bare_json():
    out = '{"register": "analitico", "rationale": "x"}'
    parsed = _extract_json(out)
    assert parsed == {"register": "analitico", "rationale": "x"}


def test_extract_json_from_markdown_fence():
    out = 'preamble\n```json\n{"chosen_register": "tecnico"}\n```\ntrailing'
    parsed = _extract_json(out)
    assert parsed == {"chosen_register": "tecnico"}


def test_extract_json_from_generic_fence():
    out = '```\n{"a": 1}\n```'
    parsed = _extract_json(out)
    assert parsed == {"a": 1}


def test_extract_json_with_prose_around():
    out = (
        "Sure, here's my choice.\n"
        '{"register": "ironico", "example_headline": "hi"}\n'
        "Hope it helps."
    )
    parsed = _extract_json(out)
    assert parsed == {"register": "ironico", "example_headline": "hi"}


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


def test_extract_json_returns_none_if_all_candidates_invalid():
    """Extractor must not raise and must return None on malformed input."""
    out = "```\nnot { valid json\n```"
    parsed = _extract_json(out)
    assert parsed is None


@pytest.fixture(autouse=True)
def console_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    """RULED 2026-11-01: `claude` runs only on a Console seat — every test that
    reaches a (faked) subprocess needs one; the guilt test below removes it."""
    monkeypatch.setenv("ANTHROPIC_CONSOLE_KEY_1", "sk-test-seat")
    monkeypatch.setenv("ANTHROPIC_CONSOLE_WORKSPACE_1", "wrkspc_test1")


def _no_seat(monkeypatch: pytest.MonkeyPatch) -> None:
    for n in (1, 2, 3):
        monkeypatch.delenv(f"ANTHROPIC_CONSOLE_KEY_{n}", raising=False)
        monkeypatch.delenv(f"ANTHROPIC_CONSOLE_WORKSPACE_{n}", raising=False)


# ── ClaudeCLIRunner config ────────────────────────────────────────────


def test_claude_runner_default_binary_path():
    r = ClaudeCLIRunner(binary_path="/fake/claude")
    assert r.binary_path == "/fake/claude"
    assert r.name == "claude"


def test_gemini_runner_default():
    r = GeminiCLIRunner(binary_path="/fake/gemini")
    assert r.binary_path == "/fake/gemini"


def test_kimi_runner_default():
    r = KimiCLIRunner(binary_path="/fake/kimi")
    assert r.binary_path == "/fake/kimi"
    assert r.name == "kimi"


def test_kimi_runner_default_model():
    r = KimiCLIRunner(binary_path="/fake/kimi")
    assert r.model == "kimi-code/k3"


def test_kimi_runner_custom_model():
    r = KimiCLIRunner(binary_path="/fake/kimi", model="kimi-code/kimi-for-coding")
    assert r.model == "kimi-code/kimi-for-coding"


# ── ClaudeCLIRunner subprocess errors ──────────────────────────────────


@pytest.mark.asyncio
async def test_claude_runner_missing_binary_raises():
    r = ClaudeCLIRunner(binary_path="/does/not/exist/claude_xyz_9999")
    with pytest.raises(CLIRunnerError):
        await r.run("hello", timeout=2)


@pytest.mark.asyncio
async def test_gemini_runner_missing_binary_raises():
    r = GeminiCLIRunner(binary_path="/does/not/exist/gemini_xyz_9999")
    with pytest.raises(CLIRunnerError):
        await r.run("hello", timeout=2)


@pytest.mark.asyncio
async def test_kimi_runner_missing_binary_raises():
    r = KimiCLIRunner(binary_path="/does/not/exist/kimi_xyz_9999")
    with pytest.raises(CLIRunnerError):
        await r.run("hello", timeout=2)


# ── Echo test: use `echo` as a stand-in binary to exercise subprocess path ──


@pytest.mark.asyncio
async def test_subprocess_runner_success_with_echo(tmp_path):
    """Spin up ClaudeCLIRunner with binary=/bin/echo to verify the async
    subprocess path works end-to-end without needing claude installed."""
    runner = ClaudeCLIRunner(binary_path="/bin/echo", extra_args=["--"])
    result = await runner.run("hello world", timeout=5)
    assert result.ok is True
    assert "hello world" in result.output
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_subprocess_runner_timeout_kills_process(tmp_path):
    """Use a tiny sleeper script to verify timeout cleanly kills the subprocess."""
    sleeper = tmp_path / "sleeper.sh"
    sleeper.write_text("#!/bin/sh\nsleep 5\n")
    sleeper.chmod(0o755)
    runner = ClaudeCLIRunner(binary_path=str(sleeper))
    result = await runner.run("whatever", timeout=1)
    assert result.ok is False
    assert "timeout" in (result.error or "").lower()


# ── ClaudeCLIRunner seats (RULED 2026-11-01) ───────────────────────────


@pytest.mark.asyncio
async def test_claude_runner_refuses_without_a_seat(monkeypatch, tmp_path):
    """GUILT: no seat is no door — the binary is never launched, so a host with
    only its interactive OAuth session (the shut fallback) cannot be used."""
    _no_seat(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-oauth-is-not-a-seat")
    marker = tmp_path / "launched"
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\necho hi\n")
    fake.chmod(0o755)
    runner = ClaudeCLIRunner(binary_path=str(fake))
    with pytest.raises(CLIRunnerError, match="ANTHROPIC_CONSOLE_KEY_"):
        await runner.run("hello", timeout=5)
    assert not marker.exists(), "the CLI ran without a sanctioned seat"


@pytest.mark.asyncio
async def test_claude_runner_child_sees_the_seat_and_nothing_else(monkeypatch, tmp_path):
    """INNOCENCE: the child gets the seat's key + workspace header and no plan
    OAuth; the plugin root pin stays."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-oauth-must-not-reach-the-child")
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        'echo "key=${ANTHROPIC_API_KEY:-unset} hdr=${ANTHROPIC_CUSTOM_HEADERS:-unset} '
        'oauth=${CLAUDE_CODE_OAUTH_TOKEN:-unset} plugin=${CLAUDE_PLUGIN_ROOT:-unset}"\n'
    )
    fake.chmod(0o755)
    runner = ClaudeCLIRunner(binary_path=str(fake))
    result = await runner.run("hello", timeout=5)
    assert result.ok is True
    assert "key=sk-test-seat" in result.output
    assert "hdr=anthropic-workspace-id: wrkspc_test1" in result.output
    assert "oauth=unset" in result.output
    assert "plugin=/dev/null" in result.output
