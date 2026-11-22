"""Tests for claude_oauth_client — subprocess behavior mocked end-to-end.

Since 2026-11-03 the client runs on Console seats (``ANTHROPIC_CONSOLE_KEY_<n>``
+ ``ANTHROPIC_CONSOLE_WORKSPACE_<n>``, RULED 2026-11-01): the fakes below read
the seat under test from the child's ``ANTHROPIC_API_KEY``, and there is no
keychain sentinel to fall through to — a second seat is configured explicitly
wherever a test needs a cascade.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any

import pytest


def _seat(monkeypatch: pytest.MonkeyPatch, n: int, key: str, workspace: str | None = None) -> None:
    """Configure Console slot ``n`` (key + a well-formed workspace id)."""
    monkeypatch.setenv(f"ANTHROPIC_CONSOLE_KEY_{n}", key)
    monkeypatch.setenv(f"ANTHROPIC_CONSOLE_WORKSPACE_{n}", workspace or f"wrkspc_test{n}")


@pytest.fixture
def clear_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "ANTHROPIC_CONSOLE_KEY_1",
        "ANTHROPIC_CONSOLE_KEY_2",
        "ANTHROPIC_CONSOLE_KEY_3",
        "ANTHROPIC_CONSOLE_WORKSPACE_1",
        "ANTHROPIC_CONSOLE_WORKSPACE_2",
        "ANTHROPIC_CONSOLE_WORKSPACE_3",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_1",
        "CLAUDE_CODE_OAUTH_TOKEN_2",
        "CLAUDE_CODE_OAUTH_TOKEN_3",
        "CLAUDE_CODE_OAUTH_TOKEN_4",
        "CLAUDE_CODE_OAUTH_TOKEN_5",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "CLOUD_ML_REGION",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def isolate_seat_cooldowns(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep the module-level cooldown memo out of every other test.

    ``_seat_cooldowns`` is process state on an imported (therefore cached)
    module, so without this a seat cooled in one test would silently be skipped
    in the next — the W96 class of defect, one scope down. Autouse so it also
    protects the tests that predate the cooldown. Also clears the tuning env
    var so a developer's shell cannot change what the suite measures.
    """
    from backend.llm import claude_oauth_client as mod

    monkeypatch.delenv("CLAUDE_OAUTH_SEAT_COOLDOWN_S", raising=False)
    monkeypatch.delenv("NUZANTARA_CLAUDE_DEFAULT_MODEL", raising=False)
    mod._reset_seat_cooldowns()
    mod._audit_warned.clear()
    yield
    mod._reset_seat_cooldowns()
    mod._audit_warned.clear()


def _fake_proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> Any:
    class _P:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout, stderr

        async def wait(self) -> None:
            return None

        def kill(self) -> None:
            self.returncode = -9

    return _P()


class _BlockingProc:
    """Fake child that only exits after the client kills and reaps it."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def wait(self) -> None:
        self.waited = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _xyz(mod: Any) -> Any:
    """A throwaway seat for the env-shaping tests."""
    return mod.ConsoleSeat(key="tok_xyz", workspace="wrkspc_xyz", label="console_1")


@pytest.mark.asyncio
async def test_collect_seats_ordering(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1", "wrkspc_01SYNTHETICabcdefghijkl")
    _seat(monkeypatch, 2, "tok2")
    _seat(monkeypatch, 3, "tok3")
    # Slot 4 does not exist (three plans, three orgs) and plan OAuth is not a seat.
    _seat(monkeypatch, 4, "tok4")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok_legacy")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_1", "tok_plan_1")

    got = mod.collect_seats()

    assert [s.label for s in got] == ["console_1", "console_2", "console_3"]
    assert [s.key for s in got] == ["tok1", "tok2", "tok3"]
    assert got[0].workspace == "wrkspc_01SYNTHETICabcdefghijkl"


@pytest.mark.asyncio
async def test_collect_seats_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "same")
    _seat(monkeypatch, 2, "same")
    _seat(monkeypatch, 3, "other")

    assert [s.label for s in mod.collect_seats()] == ["console_1", "console_3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workspace",
    [
        None,  # key without its workspace
        "   ",
        "not-a-workspace",
        "wrkspc_abc\nX-Injected: 1",  # a second header line must never reach the child
        "wrkspc_",
    ],
)
async def test_collect_seats_skips_a_half_configured_or_malformed_slot(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    workspace: str | None,
) -> None:
    """A user-scoped key answers 400 without its workspace header (measured
    2026-11-03), and the id is forwarded as a header line — so a slot is a
    seat only when its workspace is present AND well-formed. Slot 2 is intact
    to prove the skip is per-slot, not a refusal of the whole cascade."""
    from backend.llm import claude_oauth_client as mod

    monkeypatch.setenv("ANTHROPIC_CONSOLE_KEY_1", "tok1")
    if workspace is not None:
        monkeypatch.setenv("ANTHROPIC_CONSOLE_WORKSPACE_1", workspace)
    _seat(monkeypatch, 2, "tok2")

    assert [s.label for s in mod.collect_seats()] == ["console_2"]


@pytest.mark.asyncio
async def test_build_seat_env_strips_every_inherited_credential(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """The child sees ONE key and ONE workspace header: the seat's. Everything
    inherited — a shell's own API key or custom headers, plan OAuth, Bedrock,
    Vertex, the sibling seats' material — is stripped."""
    import os

    from backend.llm import claude_oauth_client as mod

    provider_vars = {
        "ANTHROPIC_API_KEY": "api-secret",
        "ANTHROPIC_AUTH_TOKEN": "auth-secret",
        "ANTHROPIC_BASE_URL": "https://paid.invalid",
        "ANTHROPIC_CUSTOM_HEADERS": "anthropic-workspace-id: wrkspc_inherited",
        "CLAUDE_CODE_OAUTH_TOKEN": "plan-oauth-secret",
        "CLAUDE_CODE_OAUTH_TOKEN_1": "plan-oauth-seat-1",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AWS_BEDROCK_RUNTIME_ENDPOINT": "https://bedrock.invalid",
        "BEDROCK_API_KEY": "bedrock-secret",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": "vertex-project",
        "VERTEX_AI_ENDPOINT": "https://vertex.invalid",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/vertex.json",
        "GOOGLE_CLOUD_PROJECT": "cloud-project",
        "CLOUD_ML_REGION": "asia-southeast1",
        "ANTHROPIC_CONSOLE_KEY_1": "other-seat-1",
        "ANTHROPIC_CONSOLE_WORKSPACE_1": "wrkspc_other1",
        "ANTHROPIC_CONSOLE_KEY_3": "other-seat-3",
        "ANTHROPIC_CONSOLE_WORKSPACE_3": "wrkspc_other3",
    }
    for key, value in provider_vars.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NUZANTARA_SAFE_MARKER", "preserved")

    seat = mod.ConsoleSeat(key="tok_xyz", workspace="wrkspc_xyz", label="console_2")
    env = mod.build_seat_env(seat)

    assert provider_vars.keys().isdisjoint(env.keys() - {"ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS"})
    assert env["ANTHROPIC_API_KEY"] == "tok_xyz"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_xyz"
    # Allowlist (gate round 3, 2026-11-03): an unlisted variable does not pass,
    # however harmless it looks; the binary lookup does.
    assert "NUZANTARA_SAFE_MARKER" not in env
    assert env["PATH"] == os.environ["PATH"]
    assert not any(k.startswith("CLAUDE_CODE_OAUTH_TOKEN") for k in env)
    assert not any(k.startswith("ANTHROPIC_CONSOLE_") for k in env)


@pytest.mark.asyncio
async def test_build_env_pins_sterile_headless_config_dir(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """W132 guilt case: an inherited interactive CLAUDE_CONFIG_DIR must never
    leak into the child. The interactive config dirs carry the control-plane
    hooks (Stop-time cross-machine mailbox delivery) that extend a one-shot
    ``-p`` past its answer — the CLI then prints the LAST turn, so the caller
    receives fleet chatter instead of its completion."""
    import os

    from backend.llm import claude_oauth_client as mod

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/someone/.claude-kaiser")
    monkeypatch.delenv(mod.HEADLESS_CONFIG_DIR_ENV, raising=False)

    env = mod.build_seat_env(_xyz(mod))

    assert env["CLAUDE_CONFIG_DIR"] == os.path.expanduser(mod.DEFAULT_HEADLESS_CONFIG_DIR)
    assert env["CLAUDE_CONFIG_DIR"] != "/Users/someone/.claude-kaiser"
    # Innocence: the pin is present even with no inherited value at all.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    env2 = mod.build_seat_env(_xyz(mod))
    assert env2["CLAUDE_CONFIG_DIR"] == os.path.expanduser(mod.DEFAULT_HEADLESS_CONFIG_DIR)


@pytest.mark.asyncio
async def test_build_env_headless_config_dir_env_override(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    tmp_path: Any,
) -> None:
    """The escape hatch (CLAUDE_OAUTH_HEADLESS_CONFIG_DIR) wins over the
    default, and a blank value falls back instead of pinning an empty path."""
    import os

    from backend.llm import claude_oauth_client as mod

    custom = str(tmp_path / "headless-cfg")
    monkeypatch.setenv(mod.HEADLESS_CONFIG_DIR_ENV, custom)
    env = mod.build_seat_env(_xyz(mod))
    assert env["CLAUDE_CONFIG_DIR"] == custom

    monkeypatch.setenv(mod.HEADLESS_CONFIG_DIR_ENV, "   ")
    env2 = mod.build_seat_env(_xyz(mod))
    assert env2["CLAUDE_CONFIG_DIR"] == os.path.expanduser(mod.DEFAULT_HEADLESS_CONFIG_DIR)


@pytest.mark.asyncio
async def test_complete_async_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")

    calls: list[dict[str, Any]] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        env = kwargs.get("env", {})
        calls.append(
            {
                "args": args,
                "api_key": env.get("ANTHROPIC_API_KEY"),
                "headers": env.get("ANTHROPIC_CUSTOM_HEADERS"),
                "env_config_dir": env.get("CLAUDE_CONFIG_DIR"),
            }
        )
        return _fake_proc(stdout=b"hello world", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("ping", model="claude-sonnet-4-6")

    assert resp.text == "hello world"
    assert resp.token_label == "console_1"
    assert resp.attempts == 1
    assert len(calls) == 1
    # The seat's pair reaches the child: key + its workspace header (measured
    # 2026-11-02: a user-scoped key is 400 without the header).
    assert calls[0]["api_key"] == "tok1"
    assert calls[0]["headers"] == "anthropic-workspace-id: wrkspc_test1"
    # W132 wiring: the sterile config-dir pin must reach the actual subprocess,
    # and the one-shot must not persist a session (the transcript-tail is what
    # let fleet mail extend the session past its answer).
    import os as _os

    assert calls[0]["env_config_dir"] == _os.path.expanduser(mod.DEFAULT_HEADLESS_CONFIG_DIR)
    assert "--no-session-persistence" in calls[0]["args"]


@pytest.mark.asyncio
async def test_complete_async_rate_limit_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")

    attempts: list[int] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            return _fake_proc(stdout=b"", stderr=b"Error: rate limit exceeded", returncode=1)
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("ping")

    assert resp.text == "ok"
    assert resp.token_label == "console_2"
    assert resp.attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic",
    [
        b"Error: quota exceeded",
        b"You've hit your weekly limit; resets tomorrow",
        b"Authentication failed",
        b"HTTP 401: Unauthorized",
        b"Error: token_revoked",
        b"Error: refresh_token_reused",
    ],
)
async def test_complete_async_exit_zero_diagnostic_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    diagnostic: bytes,
) -> None:
    """Short, strongly framed exit-0 diagnostics must rotate to the next seat."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    attempts = 0

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _fake_proc(stdout=diagnostic, returncode=0)
        return _fake_proc(stdout=b"valid answer", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("ping")

    assert resp.text == "valid answer"
    assert resp.token_label == "console_2"
    assert resp.attempts == 2


@pytest.mark.asyncio
async def test_complete_async_legitimate_auth_content_is_not_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Diagnostic keywords inside normal prose must not trigger rotation."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    answer = (
        b"The string token_revoked is an OAuth error code. "
        b"A quota is simply an allocated allowance."
    )
    calls = 0

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return _fake_proc(stdout=answer, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("explain OAuth errors")

    assert resp.text == answer.decode()
    assert resp.token_label == "console_1"
    assert calls == 1


@pytest.mark.asyncio
async def test_complete_async_falls_through_to_third_seat(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    for slot in range(1, 4):
        _seat(monkeypatch, slot, f"t{slot}")
    # An inherited key (the shell's own, say) is never one of the seats.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-propagate")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-oauth-must-not-propagate")

    attempted: list[tuple[str, str]] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        env = kwargs["env"]
        attempted.append((env["ANTHROPIC_API_KEY"], env["ANTHROPIC_CUSTOM_HEADERS"]))
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        if len(attempted) < 3:
            return _fake_proc(stdout=b"", stderr=b"OAuth quota exhausted", returncode=1)
        return _fake_proc(stdout=b"slot three succeeded", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("ping")

    assert attempted == [
        ("t1", "anthropic-workspace-id: wrkspc_test1"),
        ("t2", "anthropic-workspace-id: wrkspc_test2"),
        ("t3", "anthropic-workspace-id: wrkspc_test3"),
    ]
    assert resp.text == "slot three succeeded"
    assert resp.token_label == "console_3"
    assert resp.attempts == 3


@pytest.mark.asyncio
async def test_complete_async_fails_closed_without_a_seat(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """RULED 2026-11-01: no seat means no door. The former cascade ended in a
    keychain sentinel, so an unconfigured process silently ran on whatever
    plan-OAuth session the host had; now it raises BEFORE any subprocess, and
    a plan-OAuth token in the environment does not count as a seat."""
    from backend.llm import claude_oauth_client as mod

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-oauth")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_1", "plan-oauth-1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-bare-key-is-not-a-seat-either")
    launched = 0

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        nonlocal launched
        launched += 1
        return _fake_proc(stdout=b"must not run", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthNotAvailable, match="ANTHROPIC_CONSOLE_KEY_"):
        await mod.complete_async("ping")

    assert launched == 0


@pytest.mark.asyncio
async def test_complete_async_empty_output_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")

    counter = {"n": 0}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        if counter["n"] == 1:
            return _fake_proc(stdout=b"   \n", returncode=0)  # empty after trim
        return _fake_proc(stdout=b"fine", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("ping")

    assert resp.text == "fine"
    assert resp.token_label == "console_2"
    assert resp.attempts == 2


@pytest.mark.asyncio
async def test_complete_async_all_fail(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=b"", stderr=b"generic failure", returncode=2)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthError):
        await mod.complete_async("ping")


@pytest.mark.asyncio
async def test_complete_async_per_seat_timeout_kills_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Seat 1's per-seat timeout fires, kill+reap runs, and the loop falls
    through to seat 2, which succeeds.

    Deterministic by construction (2026-07-26 —
    discovery_prepush_fixed_wallclock_budgets_fail_under_fleet_contention):
    the original version of this test raced a real 10ms asyncio.wait_for
    timeout against real event-loop scheduling. It failed on M5 under fleet
    CPU contention (load average 72.5) — not because complete_async is
    broken, but because seat 2's "instant" mock also missed its own fresh
    10ms wall-clock budget when the OS didn't schedule this process's
    thread in time. A timeout is a ceiling, not an assertion about
    scheduler latency.

    Fixed by faking asyncio.wait_for itself: seat 1's communicate() call is
    scripted to raise TimeoutError immediately (proving the real
    kill+reap+fallthrough control flow — the actual thing under test — not
    asyncio's own timer), every other wait_for call just awaits its target
    with no timer racing a scheduler at all. timeout_s/total_timeout_s no
    longer drive real timing; kept as small/realistic values purely as
    documentation of intent.
    """
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    blocked = _BlockingProc()
    calls = 0

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return blocked
        return _fake_proc(stdout=b"second seat succeeded", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    # wait_for call order for this scenario: (1) launch seat 1, (2)
    # communicate seat 1 [scripted timeout — the ONLY call that doesn't just
    # pass through], (3) launch seat 2, (4) communicate seat 2.
    wait_calls = 0

    async def fake_wait_for(fut: Any, timeout: float) -> Any:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 2:
            if asyncio.iscoroutine(fut):
                fut.close()
            raise asyncio.TimeoutError()
        return await fut

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    resp = await mod.complete_async("ping", timeout_s=0.01, total_timeout_s=0.5)

    assert blocked.killed is True
    assert blocked.waited is True
    assert resp.token_label == "console_2"
    assert resp.attempts == 2


@pytest.mark.asyncio
async def test_complete_async_global_deadline_kills_reaps_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """The global deadline (not the per-seat one) fires after seat 1's
    communicate() times out, so the loop stops instead of trying seat 2.

    total_timeout_s raised from the original 0.01s (2026-07-26, same scar as
    the per-seat test above —
    discovery_prepush_fixed_wallclock_budgets_fail_under_fleet_contention):
    unlike that test, this one's assertion genuinely needs REAL elapsed
    wall-clock time — it proves `time.monotonic() >= deadline` fires AFTER
    one real seat's launch+communicate cycle, not before and not after two.
    A call-scripted fake clock could deliver that, but only by hard-coding
    how many times complete_async happens to read time.monotonic()
    internally — more coupled to implementation detail than the bug this is
    fixing, and silently wrong the next time that internal call count
    changes. 0.5s (50x the original budget) is the "defensible on a shared
    workstation" number the scar calls for: a single Python bytecode
    stretch is never realistically delayed hundreds of ms by OS scheduling,
    even under the load average 72.5 measured live when the sibling test
    flaked.

    Guilt-proven manually during review, TWICE (caught undercounting my own
    first pass — cross-family review flagged it before merge). complete_async
    sets "global_deadline" at FIVE sites in TWO phases, not three in one:
    the capability-probe phase (:648 pre-check, :661 TimeoutError handler,
    guarded by `if supports is None` — only reached when a caller passes
    json_schema AND the process-wide `_JSON_SCHEMA_SUPPORTED` memo is still
    unset) and the seat-loop phase this test actually drives (:674 loop-top
    pre-check, :722 launch-except, :737 communicate-except). This test calls
    complete_async without json_schema, so only the seat-loop three are live
    here; neutralizing any one or two of THOSE three alone left this test
    passing anyway, saved by whichever check was still live — genuine
    defense-in-depth. Only neutralizing all three together produced the
    expected RED: `attempts_made == 3`, `last_error == "keychain:
    launch_timeout"`, `pytest.raises(..., match="global_deadline")`
    correctly failed to match. Restored and reconfirmed 45/45 green.

    The other two (:648/:661) are NOT verified redundancy — grepped the
    whole suite (`grep -rn "_JSON_SCHEMA_SUPPORTED" backend/tests/`, then the
    same for `_cli_supports_json_schema`) and every test that calls
    complete_async(json_schema=...)
    monkeypatches `_JSON_SCHEMA_SUPPORTED` straight to True/False via the
    `force_schema_supported`/`force_schema_unsupported` fixtures — the
    `supports is None` branch, and both deadline checks inside it, are never
    entered by any test in this repo. That's a coverage gap wearing the
    label of defense-in-depth, not a fourth/fifth layer proven to hold;
    logged separately rather than silently folded into this PR's scope.
    """
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    blocked = _BlockingProc()
    calls = 0

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return blocked

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthError, match="global_deadline"):
        await mod.complete_async("ping", timeout_s=1, total_timeout_s=0.5)

    assert calls == 1
    assert blocked.killed is True
    assert blocked.waited is True


@pytest.mark.asyncio
async def test_complete_async_never_logs_or_raises_raw_stderr(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    fake_secret = "synthetic-secret-must-not-leak"
    fake_refresh = "synthetic-refresh-token-must-not-leak"

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(
            stdout=b"",
            stderr=(
                f"Authentication failed secret={fake_secret} refresh_token={fake_refresh}"
            ).encode(),
            returncode=2,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with caplog.at_level("WARNING"), pytest.raises(mod.ClaudeOAuthError) as exc_info:
        await mod.complete_async("ping")

    surfaced = f"{caplog.text}\n{exc_info.value}"
    assert fake_secret not in surfaced
    assert fake_refresh not in surfaced
    assert "stderr=" not in surfaced
    # Something identifying the failure MUST surface — a silent redaction that
    # reported nothing would satisfy the three assertions above vacuously.
    # This used to read `cli_exit_2`; since 2026-07-25 the non-zero exit path
    # runs the diagnostic classifier too, so a framed auth error is now named
    # rather than reduced to its exit code. Equally redacted (the classifier
    # returns a fixed class and never the raw text), strictly more useful.
    assert "authentication" in surfaced


@pytest.mark.asyncio
async def test_complete_async_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")  # a seat exists; it is the BINARY that is missing

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("no claude binary")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthNotAvailable, match="CLI not found"):
        await mod.complete_async("ping")


@pytest.mark.asyncio
async def test_complete_async_argv_is_hardened(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """The built argv must drop bypassPermissions, end with a ``--`` sentinel
    immediately before the prompt, and reject non-allowlisted model slugs."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")

    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured["cmd"] = list(args)
        captured["stdin"] = kwargs.get("stdin")
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    # (iv) a valid, allowlisted slug passes and reaches the subprocess.
    resp = await mod.complete_async("untrusted prompt", model="claude-sonnet-4-6")
    assert resp.text == "ok"

    cmd = captured["cmd"]
    # (o) lean: no hooks / CLAUDE.md / auto-memory, and NO tool schemas at all
    # (`--tools ""` — the empty operand is what disables every tool).
    assert "--bare" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd.index("--output-format") < cmd.index("--")
    # (i) the dangerous permission bypass is gone.
    assert "bypassPermissions" not in cmd
    assert "--permission-mode" not in cmd
    # tools are explicitly disallowed instead.
    assert "--disallowedTools" in cmd
    # (ii) the `--` sentinel is present and sits immediately before the prompt.
    assert "--" in cmd
    assert cmd[-2] == "--"
    assert cmd[-1] == "untrusted prompt"
    # the model slug was forwarded.
    assert "--model" in cmd
    assert "claude-sonnet-4-6" in cmd
    # the prompt is in argv: stdin is closed, not inherited (an inherited pipe
    # costs a 3 s wait, measured 2026-11-03).
    assert captured["stdin"] == asyncio.subprocess.DEVNULL

    # (iii) a non-allowlisted model (flag-shaped) raises before any subprocess.
    with pytest.raises(ValueError):
        await mod.complete_async("ping", model="--evil-flag")


def test_module_imports_without_anthropic_sdk() -> None:
    """Integrity test: the OAuth client must not import the Anthropic SDK.

    The entire point of this module is to remove the SDK dependency for
    Claude calls.
    """
    import backend.llm.claude_oauth_client as mod

    importlib.reload(mod)

    src = open(mod.__file__).read()
    assert "import anthropic" not in src
    assert "from anthropic" not in src


# ---------------------------------------------------------------------------
# --json-schema structured-output path (SPEC v2)
# ---------------------------------------------------------------------------

_ENVELOPE_OK = (
    b'{"type":"result","subtype":"success","is_error":false,'
    b'"result":"The domain is visa.",'
    b'"structured_output":{"domain":"visa","confidence":0.95},'
    b'"usage":{"input_tokens":120,"output_tokens":18}}'
)


@pytest.fixture
def force_schema_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the memoized --json-schema capability probe to True."""
    from backend.llm import claude_oauth_client as mod

    monkeypatch.setattr(mod, "_JSON_SCHEMA_SUPPORTED", True)


@pytest.fixture
def force_schema_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.llm import claude_oauth_client as mod

    monkeypatch.setattr(mod, "_JSON_SCHEMA_SUPPORTED", False)


_SCHEMA = {
    "type": "object",
    "properties": {"domain": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["domain", "confidence"],
}


def test_json_schema_probe_exception_retries_on_next_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient probe failure must not disable structured output forever."""
    import subprocess

    from backend.llm import claude_oauth_client as mod

    monkeypatch.setattr(mod, "_JSON_SCHEMA_SUPPORTED", None)
    calls = 0

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(argv, 0.01)
        return subprocess.CompletedProcess(argv, 0, "--json-schema", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert mod._cli_supports_json_schema("synthetic-claude") is False
    assert mod._cli_supports_json_schema("synthetic-claude") is True
    assert calls == 2


@pytest.mark.parametrize(("help_text", "expected"), [("--json-schema", True), ("", False)])
def test_json_schema_completed_probe_is_memoized(
    monkeypatch: pytest.MonkeyPatch,
    help_text: str,
    expected: bool,
) -> None:
    """A completed capability probe, positive or negative, stays memoized."""
    import subprocess

    from backend.llm import claude_oauth_client as mod

    monkeypatch.setattr(mod, "_JSON_SCHEMA_SUPPORTED", None)
    calls = 0

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, help_text, "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert mod._cli_supports_json_schema("synthetic-claude") is expected
    assert mod._cli_supports_json_schema("synthetic-claude") is expected
    assert calls == 1


@pytest.mark.asyncio
async def test_json_schema_none_adds_no_schema_flag(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """json_schema=None must NOT add --json-schema (D1). Since 2026-11-03 every
    call asks for the JSON envelope (real usage for the ledger), and stdout that
    is no envelope still comes back as the answer, unchanged."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured["cmd"] = list(args)
        return _fake_proc(stdout=b"plain text", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("ping")

    assert resp.text == "plain text"
    assert resp.structured is None
    assert "--json-schema" not in captured["cmd"]
    cmd = captured["cmd"]
    assert cmd[cmd.index("--output-format") + 1] == "json"


@pytest.mark.asyncio
async def test_json_schema_envelope_parsed(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """A well-formed envelope yields structured + flags placed before `--` (D2/D6)."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured["cmd"] = list(args)
        return _fake_proc(stdout=_ENVELOPE_OK, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async(
        "classify", model="claude-haiku-4-5-00000011", json_schema=_SCHEMA
    )

    # structured_output is the real answer; text is the PROSE result (D2).
    assert resp.structured == {"domain": "visa", "confidence": 0.95}
    assert resp.text == "The domain is visa."
    cmd = captured["cmd"]
    # flags present and the `--` sentinel still sits immediately before the prompt (D6).
    assert "--output-format" in cmd and "json" in cmd
    assert "--json-schema" in cmd
    assert cmd[-2] == "--"
    assert cmd[-1] == "classify"
    # the schema blob never precedes the model flag chaos — it's a single argv token.
    schema_idx = cmd.index("--json-schema") + 1
    assert cmd[schema_idx].startswith("{") and "domain" in cmd[schema_idx]


@pytest.mark.asyncio
async def test_json_schema_ndjson_last_result_wins(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """NDJSON stream: the last type==result object provides the structured answer."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    ndjson = (
        b'{"type":"system","subtype":"init"}\n'
        b'{"type":"assistant","message":"thinking"}\n' + _ENVELOPE_OK
    )

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=ndjson, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("classify", json_schema=_SCHEMA)
    assert resp.structured == {"domain": "visa", "confidence": 0.95}


@pytest.mark.asyncio
async def test_json_schema_is_error_envelope_falls_through(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """is_error:true with an auth diagnostic rotates even when the CLI exits 0."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    counter = {"n": 0}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        if counter["n"] == 1:
            return _fake_proc(
                stdout=(
                    b'{"type":"result","is_error":true,"result":"Error: token_revoked (HTTP 401)"}'
                ),
                returncode=0,
            )
        return _fake_proc(stdout=_ENVELOPE_OK, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("classify", json_schema=_SCHEMA)
    assert resp.structured == {"domain": "visa", "confidence": 0.95}
    assert resp.attempts == 2


@pytest.mark.asyncio
async def test_json_schema_unsupported_cli_degrades_to_text(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_unsupported: None,
) -> None:
    """A CLI without the flag drops the schema and runs the plain text path (D4)."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured["cmd"] = list(args)
        return _fake_proc(stdout=b"plain answer", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("classify", json_schema=_SCHEMA)

    assert resp.text == "plain answer"
    assert resp.structured is None
    assert "--json-schema" not in captured["cmd"]


@pytest.mark.asyncio
async def test_json_schema_oversize_drops_to_text(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """A schema over the argv cap is dropped; the text path runs (D6 size-cap)."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured["cmd"] = list(args)
        return _fake_proc(stdout=b"text fallback", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    huge_schema = {
        "type": "object",
        "properties": {f"f{i}": {"type": "string"} for i in range(5000)},
    }
    resp = await mod.complete_async("classify", json_schema=huge_schema)

    assert resp.text == "text fallback"
    assert "--json-schema" not in captured["cmd"]


@pytest.mark.asyncio
async def test_json_schema_rate_limit_on_exit_nonzero_scans_stdout(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """In schema mode, a rate-limit error on STDOUT with exit!=0 must still be
    caught (panel fix: isolate-to-stderr only on exit 0)."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    counter = {"n": 0}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        counter["n"] += 1
        if counter["n"] == 1:
            # rate-limit reported on stdout, empty stderr, non-zero exit.
            return _fake_proc(stdout=b'{"error":"rate limit exceeded"}', stderr=b"", returncode=1)
        return _fake_proc(stdout=_ENVELOPE_OK, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    resp = await mod.complete_async("classify", json_schema=_SCHEMA)
    assert resp.token_label == "console_2"
    assert resp.structured == {"domain": "visa", "confidence": 0.95}


def test_parse_json_envelope_unit() -> None:
    """Direct unit coverage of the envelope parser's return contract."""
    from backend.llm.claude_oauth_client import _parse_json_envelope

    text, structured, usage, is_error = _parse_json_envelope(_ENVELOPE_OK.decode())
    assert text == "The domain is visa."
    assert structured == {"domain": "visa", "confidence": 0.95}
    assert (usage.input_tokens, usage.output_tokens, usage.prompt_tokens) == (120, 18, 120)
    assert usage.model is None and usage.cli_cost_usd is None
    assert is_error is False

    # empty / unparseable → all-None contract
    assert _parse_json_envelope("") == ("", None, None, False)
    assert _parse_json_envelope("not json at all") == ("", None, None, False)

    # envelope without structured_output → text+usage but structured None
    no_struct = '{"type":"result","is_error":false,"result":"hi","usage":{"input_tokens":3,"output_tokens":1}}'
    t, s, u, error = _parse_json_envelope(no_struct)
    assert t == "hi" and s is None and (u.input_tokens, u.output_tokens) == (3, 1)
    assert error is False

    error_envelope = '{"type":"result","is_error":true,"result":"Error: refresh_token_reused"}'
    t, s, u, error = _parse_json_envelope(error_envelope)
    assert t == "Error: refresh_token_reused"
    assert s is None and u is None and error is True


def test_serialize_schema_compact_and_capped() -> None:
    from backend.llm.claude_oauth_client import _MAX_SCHEMA_BYTES, _serialize_schema

    blob = _serialize_schema({"a": 1, "b": 2})
    assert blob == '{"a":1,"b":2}'  # compact separators (no spaces)

    with pytest.raises(ValueError):
        _serialize_schema({"x": "y" * (_MAX_SCHEMA_BYTES + 100)})


# ---------------------------------------------------------------------------
# Seat cooldown — an exhausted seat must be recognised AND remembered
# ---------------------------------------------------------------------------
# The message below is the REAL one, copied from a live probe on 2026-07-25
# (`claude -p PONG` against a weekly-exhausted Max seat): exit 1, no rate-limit
# wording the loose RATE_LIMIT_PATTERN would catch. Before the fix it logged as
# `cli_exit_1` and the dead seat was re-spawned on every single call.
_EXHAUSTED_SEAT_MESSAGE = b"You've hit your weekly limit \xc2\xb7 resets 9am (Asia/Makassar)"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_exhausted_seat_is_classified_as_quota_not_a_bare_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    stream: str,
) -> None:
    """The live message must reach the classifier on the NON-zero exit path."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        if stream == "stdout":
            return _fake_proc(stdout=_EXHAUSTED_SEAT_MESSAGE, returncode=1)
        return _fake_proc(stdout=b"", stderr=_EXHAUSTED_SEAT_MESSAGE, returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthError) as excinfo:
        await mod.complete_async("ping")

    message = str(excinfo.value)
    assert "quota" in message
    assert "cli_exit_1" not in message
    # The raw diagnostic itself never leaks into the error surface.
    assert "Makassar" not in message


@pytest.mark.asyncio
async def test_exhausted_seat_is_skipped_on_the_following_call(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """The whole point: stop re-spawning a CLI for a seat known to be dead."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    attempted: list[str] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        seat = kwargs["env"]["ANTHROPIC_API_KEY"]
        attempted.append(seat)
        if seat == "t1":
            return _fake_proc(stdout=b"", stderr=_EXHAUSTED_SEAT_MESSAGE, returncode=1)
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    first = await mod.complete_async("ping")
    assert attempted == ["t1", "t2"]
    assert first.token_label == "console_2"

    second = await mod.complete_async("ping")
    assert attempted == ["t1", "t2", "t2"], "dead seat re-probed"
    assert second.token_label == "console_2"
    assert second.attempts == 1, "the skipped seat must not be counted as an attempt"


@pytest.mark.asyncio
async def test_a_plain_crash_is_not_a_cooldown(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """INNOCENCE: a seat that merely crashed is still healthy — retry it."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    attempted: list[str] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        seat = kwargs["env"]["ANTHROPIC_API_KEY"]
        attempted.append(seat)
        if seat == "t1":
            return _fake_proc(stdout=b"", stderr=b"Segmentation fault", returncode=139)
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await mod.complete_async("ping")
    await mod.complete_async("ping")

    assert attempted == ["t1", "t2", "t1", "t2"]
    assert not mod._seat_cooldowns


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic",
    [
        b"Error: HTTP 429 Too Many Requests",
        b"Error: rate limited, please retry",
        b"Error: too many requests",
    ],
)
async def test_transient_rate_limit_is_recognised_but_never_cooled(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    diagnostic: bytes,
) -> None:
    """GUILT+INNOCENCE pair for the cross-family finding (2026-07-25): a 429
    used to share the `quota` class with a genuine weekly-limit exhaustion, so
    it earned the same 15-minute seat cooldown even though it is transient by
    nature.

    Deliberately targets the exit==0 / no-schema classify call site (line
    ~769: `if exit_code == 0 and not use_schema`), NOT the exit!=0 site: an
    exit!=0 stderr containing "429"/"rate limit"/"too many requests" is
    intercepted EARLIER by the pre-existing, unrelated `RATE_LIMIT_PATTERN`
    short-circuit (only guards exit_code != 0), which `continue`s WITHOUT
    ever calling `_mark_seat_cooling` — so that site can never discriminate
    pre/post-fix (confirmed empirically: it passes either way). The exit==0
    stdout site has no such guard, so it is where the granularity bug
    actually lived and is actually fixed.

    Two things must both hold: the diagnostic IS recognised (rotates to the
    next seat WITHIN this call, same as any other framed diagnostic — that
    part is correct and pre-dates this fix), but it must NEVER be
    persisted as a cooldown, so the very next call retries the same seat.

    This test FAILS on the pre-fix code (rate-limit shared the `quota` class,
    so `t1` would still be missing from `attempted` on the second call) and
    PASSES after `_RATE_LIMIT_DIAGNOSTIC_PATTERN` is classified separately
    from `_QUOTA_DIAGNOSTIC_PATTERN` and excluded from `_COOLDOWN_CLASSES`.
    """
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    attempted: list[str] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        seat = kwargs["env"]["ANTHROPIC_API_KEY"]
        attempted.append(seat)
        if seat == "t1":
            return _fake_proc(stdout=diagnostic, stderr=b"", returncode=0)
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    first = await mod.complete_async("ping")
    assert attempted == ["t1", "t2"], "429 must still rotate within the same call"
    assert first.token_label == "console_2"
    assert not mod._seat_cooldowns, "a transient 429 must never be memoised as cooling"

    second = await mod.complete_async("ping")
    assert attempted == [
        "t1",
        "t2",
        "t1",
        "t2",
    ], "t1 must be retried on the very next call, not skipped as if exhausted"
    assert second.token_label == "console_2"


def test_transient_429_and_durable_quota_classify_differently() -> None:
    """Unit-level guilt+innocence on the classifier itself, no subprocess."""
    from backend.llm import claude_oauth_client as mod

    assert mod._classify_cli_diagnostic("Error: HTTP 429 Too Many Requests") == "rate_limit"
    assert mod._classify_cli_diagnostic("Error: rate limited") == "rate_limit"
    assert mod._classify_cli_diagnostic("Error: too many requests") == "rate_limit"
    assert (
        mod._classify_cli_diagnostic("You've hit your weekly limit · resets 9am")
        == "quota"
    )
    assert mod._classify_cli_diagnostic("Error: quota exceeded") == "quota"
    assert "rate_limit" not in mod._COOLDOWN_CLASSES
    assert "quota" in mod._COOLDOWN_CLASSES


@pytest.mark.asyncio
async def test_long_prose_mentioning_a_weekly_limit_is_not_a_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """INNOCENCE: the classifier decides on error SHAPE, never on a substring."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    prose = (
        b"The regulation says the applicant has hit your weekly limit only when "
        b"the filing window closes, and the weekly limit is counted from the first "
        b"business day, which for this particular case means the deadline lands on "
        b"a Monday rather than the Friday the client assumed when they filed."
    )

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=prose, stderr=b"", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthError) as excinfo:
        await mod.complete_async("ping")

    assert "cli_exit_1" in str(excinfo.value)
    assert not mod._seat_cooldowns, "prose must never cool a healthy seat"


@pytest.mark.asyncio
async def test_cooldown_can_be_disabled_by_env(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Kill switch: 0 keeps the honest classification, drops the memo."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    monkeypatch.setenv("CLAUDE_OAUTH_SEAT_COOLDOWN_S", "0")
    attempted: list[str] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        seat = kwargs["env"]["ANTHROPIC_API_KEY"]
        attempted.append(seat)
        if seat == "t1":
            return _fake_proc(stdout=b"", stderr=_EXHAUSTED_SEAT_MESSAGE, returncode=1)
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    await mod.complete_async("ping")
    await mod.complete_async("ping")

    assert attempted == ["t1", "t2", "t1", "t2"]
    assert not mod._seat_cooldowns


def test_seat_key_never_contains_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cicatrix #4: the memo holds a fingerprint, never the credential."""
    from backend.llm import claude_oauth_client as mod

    secret = "sk-ant-usr-VERY-SECRET-VALUE"
    seat = mod.ConsoleSeat(key=secret, workspace="wrkspc_abc", label="console_1")
    key = mod._seat_key(seat)

    assert secret not in key
    assert "VERY-SECRET" not in key
    assert key.startswith("console_1:")
    assert key != mod._seat_key(mod.ConsoleSeat(key=secret + "x", workspace="wrkspc_abc", label="console_1"))
    # Re-pinning the same key to another workspace is a different pairing too.
    assert key != mod._seat_key(mod.ConsoleSeat(key=secret, workspace="wrkspc_xyz", label="console_1"))


def test_rotated_key_does_not_inherit_the_old_cooldown() -> None:
    """A new credential under the same label starts clean."""
    from backend.llm import claude_oauth_client as mod

    old = mod.ConsoleSeat(key="old", workspace="wrkspc_a", label="console_1")
    new = mod.ConsoleSeat(key="new", workspace="wrkspc_a", label="console_1")
    mod._seat_cooldowns[mod._seat_key(old)] = time.monotonic() + 900

    assert mod._drop_cooling_seats([old]) == [old], "the only seat is cooling — must fail open"
    assert mod._drop_cooling_seats([old, new]) == [new]


def test_expired_cooldown_is_dropped_and_the_seat_returns() -> None:
    from backend.llm import claude_oauth_client as mod

    s1 = mod.ConsoleSeat(key="t1", workspace="wrkspc_a", label="console_1")
    s2 = mod.ConsoleSeat(key="t2", workspace="wrkspc_b", label="console_2")
    mod._seat_cooldowns[mod._seat_key(s1)] = time.monotonic() - 1.0
    seats = [s1, s2]

    assert mod._drop_cooling_seats(seats) == seats
    assert not mod._seat_cooldowns, "expired entries must be purged, not accumulated"


def test_all_seats_cooling_fails_open() -> None:
    """A cooldown is an optimisation — it may never starve the cascade."""
    from backend.llm import claude_oauth_client as mod

    seats = [
        mod.ConsoleSeat(key=f"t{i}", workspace=f"wrkspc_{i}", label=f"console_{i}") for i in (1, 2, 3)
    ]
    for seat in seats:
        mod._seat_cooldowns[mod._seat_key(seat)] = time.monotonic() + 900

    assert mod._drop_cooling_seats(seats) == seats


# ---------------------------------------------------------------------------
# Console door diagnostics — messages measured on Pro, 2026-11-03
# ---------------------------------------------------------------------------
# Each of these was printed by `claude -p … --model claude-haiku-4-5` with a
# Console seat (bad key / wrong workspace / no workspace header), exit 1. None
# starts with "Error:", so before the Console landing every one of them fell
# through to the opaque `cli_exit_1` and the dead seat was re-probed each call.
_CONSOLE_BAD_KEY = "Invalid API key · Fix external API key"
_CONSOLE_WRONG_WORKSPACE = (
    "There's an issue with the selected model (claude-haiku-4-5). It may not exist "
    "or you may not have access to it. Run --model to pick a different model."
)
_CONSOLE_NO_HEADER = (
    "API Error: 400 This API key is not scoped to a workspace, so this request must "
    "include the anthropic-workspace-id header with the ID of the workspace to use."
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_CONSOLE_BAD_KEY, "authentication"),
        (_CONSOLE_NO_HEADER, "authentication"),
        (_CONSOLE_WRONG_WORKSPACE, "model_access"),
        ("API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}", "authentication"),
        (
            'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":'
            '"Your credit balance is too low to access the Anthropic API. Please go to Plans & '
            'Billing to upgrade or purchase credits."}}',
            "quota",
        ),
        # Not a Console shape and no origin/main frame: None here. On a non-zero
        # exit the loose RATE_LIMIT_PATTERN short-circuit still rotates the seat
        # within the call, and never cools it.
        ("API Error: 429 rate limit exceeded", None),
        # Gate round 3 (2026-11-03): ordinary prose that BEGINS like a Console
        # refusal — the general frame is origin/main's again, the Console shapes
        # are full matches, so neither can be extended by an explanation.
        ("API Error: invalid API key means the provided credential is wrong; replace it in your settings.", None),
        (
            "There's an issue with the selected model (claude-haiku-4-5). It may not exist or you may "
            "not have access to it. That is what the CLI prints when the workspace is wrong.",
            None,
        ),
    ],
)
def test_console_diagnostics_are_classified(message: str, expected: str | None) -> None:
    from backend.llm import claude_oauth_client as mod

    assert mod._classify_cli_diagnostic(message) == expected


def test_console_durable_classes_cool_and_rate_limit_does_not() -> None:
    from backend.llm import claude_oauth_client as mod

    assert {"authentication", "quota", "model_access"} <= mod._COOLDOWN_CLASSES
    assert "rate_limit" not in mod._COOLDOWN_CLASSES


def test_prose_about_models_or_keys_is_not_a_diagnostic() -> None:
    """INNOCENCE for the new frames: the words alone, unframed, never classify."""
    from backend.llm import claude_oauth_client as mod

    assert mod._classify_cli_diagnostic("Rotate the key when an invalid API key is reported.") is None
    assert (
        mod._classify_cli_diagnostic(
            "If there's an issue with the selected model in the UI, pick another one."
        )
        is None
    )
    # Gate round 1 (2026-11-03): prose that BEGINS with the measured words
    # cooled a healthy seat under the first, looser frames. The frames are now
    # pinned to the measured shape (end-anchored / with the slug and its next
    # sentence), so an answer about keys or models stays an answer.
    assert (
        mod._classify_cli_diagnostic(
            "Invalid API key is a common configuration mistake. "
            "This guide explains how to prevent it."
        )
        is None
    )
    assert (
        mod._classify_cli_diagnostic(
            "There's an issue with the selected model in this example story; "
            "the author resolves it in the next paragraph."
        )
        is None
    )
    assert mod._classify_cli_diagnostic("Invalid API key") == "authentication"
    assert mod._classify_cli_diagnostic("Invalid API key · Fix external API key") == "authentication"
    # Gate round 2: a parenthesised non-slug and a different second sentence
    # are prose; the frame wants a claude-* slug AND the CLI's own sentence.
    assert (
        mod._classify_cli_diagnostic(
            "There is an issue with the selected model (linear regression). It may not "
            "exist in the package; install scikit-learn before running the example."
        )
        is None
    )
    assert (
        mod._classify_cli_diagnostic(
            "There's an issue with the selected model (claude-haiku-4-5). It may not exist "
            "in your notes; check the roster before running the example."
        )
        is None
    )


@pytest.mark.asyncio
async def test_wrong_workspace_seat_is_cooled_and_the_next_seat_answers(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT: a key pinned to the wrong workspace is durable for that seat —
    rotate within the call AND remember it, exactly like a revoked key."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    _seat(monkeypatch, 2, "t2")
    attempted: list[str] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        seat = kwargs["env"]["ANTHROPIC_API_KEY"]
        attempted.append(seat)
        if seat == "t1":
            return _fake_proc(stdout=_CONSOLE_WRONG_WORKSPACE.encode(), returncode=1)
        return _fake_proc(stdout=b"ok", returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    first = await mod.complete_async("ping")
    assert first.token_label == "console_2"
    second = await mod.complete_async("ping")
    assert attempted == ["t1", "t2", "t2"], "the mis-pinned seat must not be re-probed"
    assert second.attempts == 1


@pytest.mark.asyncio
async def test_schema_mode_bad_key_envelope_on_exit_nonzero_is_classified(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """Measured 2026-11-03: with ``--output-format json`` a bad key exits 1 AND
    prints the JSON envelope (is_error:true, result "Invalid API key …"). The
    envelope is past the classifier's size gate; its ``result`` field is not,
    so the exit≠0 branch must read it rather than log a bare ``cli_exit_1``."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    padding = '"usage":{"input_tokens":0,"output_tokens":0,' + ",".join(
        f'"k{i}":0' for i in range(120)
    ) + "}"
    envelope = (
        '{"type":"result","subtype":"success","is_error":true,'
        f'"result":"{_CONSOLE_BAD_KEY}","terminal_reason":"api_error",{padding}}}'
    ).encode()
    assert len(envelope) > mod._MAX_DIAGNOSTIC_CHARS

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=envelope, returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthError) as excinfo:
        await mod.complete_async("classify", json_schema=_SCHEMA)

    assert "authentication" in str(excinfo.value)
    assert "cli_exit_1" not in str(excinfo.value)
    assert mod._seat_cooldowns, "a bad key is durable — the seat must cool"


@pytest.mark.asyncio
async def test_schema_mode_nonzero_exit_with_a_real_answer_does_not_cool(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """INNOCENCE for the envelope branch (gate round 1, 2026-11-03): an exit≠0
    around an envelope that says is_error:false — a hook's exit code, say —
    must not let the answer's prose reach the classifier and cool the seat."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "t1")
    prose = (
        "Invalid API key is a common configuration mistake. "
        "This guide explains how to prevent it."
    )
    padding = ",".join(f'"k{i}":0' for i in range(120))
    envelope = (
        '{"type":"result","subtype":"success","is_error":false,'
        f'"result":"{prose}","usage":{{"input_tokens":1,"output_tokens":1,{padding}}}}}'
    ).encode()
    assert len(envelope) > mod._MAX_DIAGNOSTIC_CHARS

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=envelope, returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(mod.ClaudeOAuthError) as excinfo:
        await mod.complete_async("classify", json_schema=_SCHEMA)

    assert "cli_exit_1" in str(excinfo.value)
    assert not mod._seat_cooldowns, "a healthy seat must not cool on its own answer"


# Every variable an inherited environment could use to steer the child to a
# credential or a cloud that is not the seat's. The three spawners that build
# a child environment must all strip every one of them (gate round 1,
# 2026-11-03: the client kept the Foundry trio, the scripts kept BEDROCK_REGION,
# VERTEX_PROJECT and the Google Cloud selectors).
_ANTHROPIC_DOOR_POISON = {
    "ANTHROPIC_API_KEY": "x",
    "ANTHROPIC_AUTH_TOKEN": "x",
    "ANTHROPIC_BASE_URL": "x",
    "ANTHROPIC_CUSTOM_HEADERS": "x",
    "CLAUDE_CODE_OAUTH_TOKEN": "x",
    "CLAUDE_CODE_OAUTH_TOKEN_1": "x",
    "CLAUDE_CODE_OAUTH_TOKEN_4": "x",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "ANTHROPIC_FOUNDRY_API_KEY": "x",
    "ANTHROPIC_FOUNDRY_RESOURCE": "x",
    "ANTHROPIC_BEDROCK_BASE_URL": "x",
    "ANTHROPIC_VERTEX_PROJECT_ID": "x",
    "AWS_ACCESS_KEY_ID": "x",
    "AWS_SECRET_ACCESS_KEY": "x",
    "AWS_REGION": "x",
    "BEDROCK_REGION": "x",
    "BEDROCK_API_KEY": "x",
    "VERTEX_PROJECT": "x",
    "VERTEX_AI_ENDPOINT": "x",
    "GOOGLE_APPLICATION_CREDENTIALS": "x",
    "GOOGLE_CLOUD_PROJECT": "x",
    "GOOGLE_CLOUD_QUOTA_PROJECT": "x",
    "CLOUD_ML_REGION": "x",
    "ANTHROPIC_CONSOLE_KEY_1": "other-1",
    "ANTHROPIC_CONSOLE_WORKSPACE_1": "wrkspc_other1",
    "ANTHROPIC_CONSOLE_KEY_3": "other-3",
    "ANTHROPIC_CONSOLE_WORKSPACE_3": "wrkspc_other3",
    # Gate round 2 (2026-11-03): names the installed CLI honours that no list
    # of names had — the resolver reads ANTHROPIC_BASE_URL || CLAUDE_CODE_API_BASE_URL.
    "ANTHROPIC_UNIX_SOCKET": "/synthetic.sock",
    "CLAUDE_CODE_API_BASE_URL": "https://synthetic.invalid",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "3",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN": "x",
    # And two that do not exist yet: the scrub is by FAMILY, so a selector the
    # CLI grows next cycle is stripped before anyone learns its name.
    "ANTHROPIC_FUTURE_SELECTOR_2027": "x",
    "CLAUDE_CODE_FUTURE_SWITCH_2027": "x",
    # Gate round 3: outside both families, honoured by the installed CLI — a
    # credential-store redirect, a bridge token, a proxy auth token, gcloud.
    "CLAUDE_SECURESTORAGE_CONFIG_DIR": "/interactive",
    "CLAUDE_BG_AUTH_SNAPSHOT_PATH": "/interactive/snap",
    "CLAUDE_BRIDGE_OAUTH_TOKEN": "x",
    "AGENT_PROXY_AUTH_TOKEN": "x",
    "GCLOUD_PROJECT": "x",
    "CLOUDSDK_CONFIG": "/interactive/gcloud",
    # And a name nobody has invented yet, outside every family: an allowlist
    # drops it without knowing it.
    "ZZ_UNKNOWN_SELECTOR_2028": "x",
}


def _load_script(name: str) -> Any:
    """Import apps/backend-rag/scripts/<name>.py as a module (import-free scripts)."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[4] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_scrub_parity_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # the scripts' dataclasses resolve annotations by module name
    spec.loader.exec_module(module)
    return module


def test_seat_env_scrub_parity_across_spawners(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client and the two import-free scripts hand the child the SAME
    Anthropic-door environment: none of the poison survives, exactly the
    seat's pair and the sterile config dir are set, unrelated variables pass."""
    from backend.llm import claude_oauth_client as mod

    for name in list(_ANTHROPIC_DOOR_POISON) + ["CLAUDE_CONFIG_DIR", mod.HEADLESS_CONFIG_DIR_ENV]:
        monkeypatch.delenv(name, raising=False)
    for name, value in _ANTHROPIC_DOOR_POISON.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/someone/.claude-interactive")
    monkeypatch.setenv("NUZANTARA_SAFE_MARKER", "preserved")

    seat = mod.ConsoleSeat(key="tok_xyz", workspace="wrkspc_xyz", label="console_2")
    envs = {
        "client": mod.build_seat_env(seat),
        "auto_verifier": _load_script("auto_verifier")._verifier_seat_env("tok_xyz", "wrkspc_xyz"),
        "verified_generator": _load_script("verified_generator")._gen_seat_env("tok_xyz", "wrkspc_xyz"),
    }
    import os as _os

    for who, env in envs.items():
        leaked = sorted(k for k in env if k in _ANTHROPIC_DOOR_POISON and k not in {"ANTHROPIC_API_KEY", "ANTHROPIC_CUSTOM_HEADERS"})
        assert not leaked, f"{who} forwards {leaked}"
        assert env["ANTHROPIC_API_KEY"] == "tok_xyz", who
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_xyz", who
        assert env["CLAUDE_CONFIG_DIR"] == _os.path.expanduser(mod.DEFAULT_HEADLESS_CONFIG_DIR), who
        assert "NUZANTARA_SAFE_MARKER" not in env, who  # allowlist: unlisted never passes
        assert env["PATH"] == _os.environ["PATH"], who
        assert not any(k.startswith(("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CONSOLE_", "CLAUDE_CODE_USE_")) for k in env), who
    # The three builders hand the child exactly the same environment.
    assert envs["client"] == envs["auto_verifier"] == envs["verified_generator"]


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("claude-explain", ("synthetic",)),
        ("claude-review", ("diff",)),
        ("claude-redteam", ("plan",)),
        ("parallel", ('gemini-scan:"a"', 'claude-review:"b"')),
    ],
)
def test_verified_generator_dispatch_never_reaches_ai_dispatch_claude(
    monkeypatch: pytest.MonkeyPatch, command: str, args: tuple[str, ...]
) -> None:
    """GUILT (gate round 4, 2026-11-03): run_dispatch() handed claude-* to
    ai-dispatch.sh, whose run_claude is plan OAuth then keychain. Refused before
    any subprocess, with a plan token in the environment."""
    import subprocess

    vg = _load_script("verified_generator")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-plan")
    launched: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: launched.append(a) or None)

    assert vg.run_dispatch(command, *args) == ""
    assert launched == []


def test_verified_generator_dispatch_strips_the_door_from_other_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INNOCENCE: a non-Claude command still runs, with no ANTHROPIC_* or
    CLAUDE_* variable in its child."""
    import subprocess
    from pathlib import Path

    vg = _load_script("verified_generator")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-plan")
    monkeypatch.setenv("ANTHROPIC_CONSOLE_KEY_1", "synthetic-seat")
    monkeypatch.setattr(vg, "AI_DISPATCH", Path(vg.__file__))  # any existing file: the run is faked
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["cmd"] = cmd
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert vg.run_dispatch("gemini-scan", "x") == "ok"
    assert seen["cmd"][1] == "gemini-scan"  # type: ignore[index]
    env = seen["env"]
    assert isinstance(env, dict)
    assert not any(k.startswith(("ANTHROPIC_", "CLAUDE_")) for k in env)


def test_every_claude_spawner_in_the_perimeter_passes_an_env() -> None:
    """Unit mirror of the ENTITY rule the #40f grep cannot see (gate round 1,
    2026-11-03): a `claude` child that INHERITS the environment runs on
    whatever plan-OAuth session the host has. Every subprocess call in
    apps/backend-rag whose argv starts with the claude binary must pass
    ``env=`` explicitly. Limits: argv must be a list literal whose first
    element is the string "claude" or a name/attribute containing "claude",
    "binary_path" or "cli"; a binary resolved under another name is not seen."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    spawn_calls = {"run", "Popen", "check_output", "check_call", "call", "create_subprocess_exec"}
    # Thin wrappers around a spawn that take the argv as their first positional
    # argument or as ``cmd=``: council/cli_runners._run_subprocess, the two
    # scripts' _run_process_group. A wrapper fed by a builder (consiglio's
    # _build_cmd) is outside what this walk can resolve.
    spawn_wrappers = {"_run_subprocess", "_run_process_group"}
    probe_flags = {"--help", "--version"}  # a capability probe never reaches a model
    offenders: list[str] = []
    seen: list[str] = []

    def _is_claude_binary(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and node.value.rsplit("/", 1)[-1] == "claude"
        if isinstance(node, ast.Name):
            return "claude" in node.id.lower() or node.id in {"cli", "binary_path"}
        if isinstance(node, ast.Attribute):
            return "claude" in node.attr.lower() or node.attr in {"binary_path", "cli"}
        return False

    def _argv_list(node: ast.AST, assigned: dict[str, ast.List]) -> ast.List | None:
        """The argv list literal behind a call's first argument, following one
        plain `name = [...]` assignment in the same function (`cmd`, `argv`)."""
        if isinstance(node, ast.Starred):
            node = node.value
        if isinstance(node, ast.List):
            return node
        if isinstance(node, ast.Name):
            return assigned.get(node.id)
        return None

    for path in sorted(root.glob("**/*.py")):
        rel = path.relative_to(root).as_posix()
        if any(part in rel for part in (".venv/", "node_modules/", "/tests/", "test_", "/.git/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except SyntaxError:
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigned: dict[str, ast.List] = {}
            for node in ast.walk(func):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    target = node.targets[0] if isinstance(node, ast.Assign) else node.target
                    if isinstance(target, ast.Name) and isinstance(node.value, ast.List):
                        assigned[target.id] = node.value
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    callee = node.func.attr
                elif isinstance(node.func, ast.Name):
                    callee = node.func.id
                else:
                    continue
                if callee not in spawn_calls and callee not in spawn_wrappers:
                    continue
                argv_node: ast.AST | None = node.args[0] if node.args else None
                if argv_node is None and callee in spawn_wrappers:
                    argv_node = next((kw.value for kw in node.keywords if kw.arg == "cmd"), None)
                if argv_node is None:
                    continue
                argv = _argv_list(argv_node, assigned)
                if argv is None or not argv.elts or not _is_claude_binary(argv.elts[0]):
                    continue
                if any(isinstance(e, ast.Constant) and e.value in probe_flags for e in argv.elts):
                    continue
                seen.append(f"{rel}:{node.lineno}")
                if not any(kw.arg == "env" for kw in node.keywords):
                    offenders.append(f"{rel}:{node.lineno}")

    assert len(seen) >= 8, f"the walk found too few claude spawners to be trusted: {seen}"
    assert not offenders, f"claude spawned on the inherited environment: {offenders}"


def test_every_claude_argv_in_the_perimeter_is_lean() -> None:
    """Every `claude` argv in apps/backend-rag runs ``--bare --tools ""``
    (2026-11-03). Measured on a Console seat: the default CLI prefix — hooks,
    CLAUDE.md discovery, every tool schema — was an 18.6K-token cache write per
    cold call ($0.023 on Haiku 4.5 against $0.0003 lean), and two scripts ran
    with --dangerously-skip-permissions over a full tool set.

    The walk reads argv-shaped LIST LITERALS whose first element names the
    binary: the string "claude", a name/attribute/call containing "claude",
    draft.py's ``_resolve_cli()`` (directly or through one assignment in the
    same function), or ``self.binary_path`` inside a Claude* class. Probes
    (--help/--version) never reach a model. One exemption, by file: the Canva
    invoker needs the Canva MCP server registered in its inherited config dir,
    which --bare would skip."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    resolvers = {"_resolve_cli"}
    exempt = {"backend/services/canva_renderer/claude_invoker.py"}
    # The client appends --model by `cmd.extend` (the caller's or default_model());
    # its own tests pin that. Every other argv names the model in the literal.
    model_exempt = {"backend/llm/claude_oauth_client.py"}
    probe_flags = {"--help", "--version"}
    seen: list[str] = []
    offenders: list[str] = []
    unpinned: list[str] = []

    def names_claude(node: ast.AST, cls: str | None, assigned: dict[str, ast.AST]) -> bool:
        if isinstance(node, ast.Starred):
            node = node.value
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and node.value.rsplit("/", 1)[-1] == "claude"
        if isinstance(node, ast.Name):
            if "claude" in node.id.lower():
                return True
            source = assigned.get(node.id)
            return source is not None and names_claude(source, cls, {})
        if isinstance(node, ast.Attribute):
            if "claude" in node.attr.lower():
                return True
            return node.attr == "binary_path" and "claude" in (cls or "").lower()
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            return "claude" in name.lower() or name in resolvers
        return False

    def visit(node: ast.AST, rel: str, cls: str | None, assigned: dict[str, ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            child_cls = child.name if isinstance(child, ast.ClassDef) else cls
            child_assigned = assigned
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_assigned = {
                    n.targets[0].id: n.value
                    for n in ast.walk(child)
                    if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                }
            if isinstance(child, ast.List) and child.elts and names_claude(child.elts[0], cls, assigned):
                consts = [e.value if isinstance(e, ast.Constant) else None for e in child.elts]
                if not probe_flags.intersection(c for c in consts if isinstance(c, str)):
                    seen.append(f"{rel}:{child.lineno}")
                    lean = "--bare" in consts and any(
                        c == "--tools" and i + 1 < len(consts) and consts[i + 1] == ""
                        for i, c in enumerate(consts)
                    )
                    if not lean and rel not in exempt:
                        offenders.append(f"{rel}:{child.lineno}")
                    if "--model" not in consts and rel not in model_exempt:
                        unpinned.append(f"{rel}:{child.lineno}")
            visit(child, rel, child_cls, child_assigned)

    for path in sorted(root.glob("**/*.py")):
        rel = path.relative_to(root).as_posix()
        if any(part in rel for part in (".venv/", "node_modules/", "/tests/", "/.git/")):
            continue
        if path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        visit(tree, rel, None, {})

    files = {entry.rsplit(":", 1)[0] for entry in seen}
    assert len(files) >= 10, f"the walk found too few claude argv files to be trusted: {sorted(files)}"
    assert "backend/services/mail_loop/draft.py" in files, "draft.py's argv is no longer seen"
    assert not offenders, f"claude argv without --bare --tools \"\": {offenders}"
    # Unpinned, the CLI answers on its priciest model (claude-fable-5-1 at $10/$50).
    assert not unpinned, f"claude argv without --model: {unpinned}"


def test_product_perimeter_reads_no_plan_oauth() -> None:
    """Unit mirror of catE #40f (``catE-sovereignty-lint.yml``): the four files
    the 2026-11-02 baseline tolerated are drained by this landing, and the
    two standalone scripts share the seat contract by its env-var names."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]  # apps/backend-rag
    drained = [
        root / "backend/llm/claude_oauth_client.py",
        root / "backend/services/mail_loop/draft.py",
        root / "scripts/auto_verifier.py",
        root / "scripts/verified_generator.py",
        # Gate round 2: the two paths the grep never saw — a plan-OAuth cascade
        # wrapper (portal_judge) and a bare key on the Messages API (kbli).
        root / "backend/scripts/visa_engine/portal_judge.py",
        root / "scripts/kbli_enrichment_pipeline.py",
    ]
    reader = re.compile(
        r"""(getenv|environ(\.get)?)\s*[\(\[]\s*f?["'](CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_AUTH_TOKEN)"""
        r"""|["'](CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_AUTH_TOKEN)["']\s*\]\s*=[^=]|\bauth_token="""
    )
    for path in drained:
        src = path.read_text(encoding="utf-8")
        assert reader.search(src) is None, f"{path.name} still reads plan OAuth"
        assert '"claude-cascade.sh"' not in src, f"{path.name} still routes through the plan-OAuth cascade wrapper"
        assert 'environ.get("ANTHROPIC_API_KEY"' not in src and "environ['ANTHROPIC_API_KEY'" not in src, (
            f"{path.name} reads a bare key instead of a seat"
        )
        assert "ANTHROPIC_CONSOLE_KEY_" in src and "ANTHROPIC_CONSOLE_WORKSPACE_" in src, (
            f"{path.name} does not speak the Console seat contract"
        )


def test_malformed_cooldown_env_degrades_to_a_safe_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.llm import claude_oauth_client as mod

    monkeypatch.setenv("CLAUDE_OAUTH_SEAT_COOLDOWN_S", "not-a-number")
    assert mod._seat_cooldown_s() == mod._DEFAULT_SEAT_COOLDOWN_S

    monkeypatch.setenv("CLAUDE_OAUTH_SEAT_COOLDOWN_S", "nan")
    assert mod._seat_cooldown_s() == 0.0, "NaN must disable, never strand a seat"

    monkeypatch.setenv("CLAUDE_OAUTH_SEAT_COOLDOWN_S", "-5")
    assert mod._seat_cooldown_s() == 0.0


# ---------------------------------------------------------------------------
# Real usage from the envelope (2026-11-03): every call is --output-format json
# ---------------------------------------------------------------------------

# Shapes copied from the measured envelope (Pro, CLI 2.1.316, Console seat):
# cache tokens sit OUTSIDE input_tokens, output_tokens include thinking, and
# modelUsage names the model that answered.
_MEASURED_HAIKU = (
    b'{"type":"result","subtype":"success","is_error":false,"result":"PONG",'
    b'"usage":{"input_tokens":81,"cache_creation_input_tokens":0,'
    b'"cache_read_input_tokens":0,"output_tokens":51,'
    b'"output_tokens_details":{"thinking_tokens":42},'
    b'"cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":0}},'
    b'"modelUsage":{"claude-haiku-4-5":{"inputTokens":81,"outputTokens":51,'
    b'"costUSD":0.000336}},"total_cost_usd":0.000336}'
)
_UNPINNED_FABLE = (
    b'{"type":"result","is_error":false,"result":"PONG",'
    b'"usage":{"input_tokens":71,"output_tokens":5}, '
    b'"modelUsage":{"claude-fable-5-1":{"inputTokens":71,"outputTokens":5}},'
    b'"total_cost_usd":0.00096}'
)
_COLD_CACHE = (
    b'{"type":"result","is_error":false,"result":"PONG",'
    b'"usage":{"input_tokens":3,"cache_creation_input_tokens":18576,'
    b'"cache_read_input_tokens":2000,"output_tokens":10,'
    b'"cache_creation":{"ephemeral_5m_input_tokens":18576,"ephemeral_1h_input_tokens":0}},'
    b'"modelUsage":{"claude-haiku-4-5":{}},"total_cost_usd":0.023475}'
)


def _ledger_spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from backend.services import observability

    rows: list[dict[str, Any]] = []

    async def fake_record(**kwargs: Any) -> dict[str, bool]:
        rows.append(kwargs)
        return {}

    monkeypatch.setattr(observability, "record_llm_call", fake_record)
    return rows


async def _run_with_stdout(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes, *, model: str | None, returncode: int = 0
) -> Any:
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    return await mod.complete_async("ping", model=model, endpoint="t")


@pytest.mark.asyncio
async def test_text_path_answer_is_the_envelope_result_with_real_usage(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """The text path returns ``result`` (not the JSON blob) and the ledger gets
    the measured counts priced to the CLI's own figure."""
    rows = _ledger_spy(monkeypatch)
    resp = await _run_with_stdout(monkeypatch, _MEASURED_HAIKU, model="claude-haiku-4-5")
    assert resp.text == "PONG"
    assert resp.structured is None
    (row,) = rows
    assert (row["input_tokens"], row["output_tokens"]) == (81, 51)
    assert row["model"] == "claude-haiku-4-5"
    # 81 × $1 + 51 × $5 per MTok — thinking is inside output_tokens, not added twice.
    assert row["cost_usd"] == pytest.approx(0.000336, abs=1e-9)
    assert row["provider"] == "claude_console" and row["success"] is True


@pytest.mark.asyncio
async def test_unpinned_call_is_billed_as_the_model_that_answered(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT: with no --model the CLI answered with claude-fable-5-1 ($10/$50,
    measured 2026-11-03); the ledger used to write "claude-unknown" at $1/$3."""
    rows = _ledger_spy(monkeypatch)
    await _run_with_stdout(monkeypatch, _UNPINNED_FABLE, model=None)
    (row,) = rows
    assert row["model"] == "claude-fable-5-1"
    assert row["cost_usd"] == pytest.approx(0.00096, abs=1e-9)


@pytest.mark.asyncio
async def test_cache_reads_and_writes_reach_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT: the old reader kept input/output only, so the 18.6K-token cache
    write every cold call paid was invisible — the ledger said $0.00005 for a
    $0.023 call."""
    rows = _ledger_spy(monkeypatch)
    await _run_with_stdout(monkeypatch, _COLD_CACHE, model="claude-haiku-4-5")
    (row,) = rows
    assert row["input_tokens"] == 3 + 18576 + 2000
    assert row["cache_hit_tokens"] == 2000
    expected = (3 * 1.00 + 18576 * 1.25 + 2000 * 0.10 + 10 * 5.00) / 1_000_000
    assert row["cost_usd"] == pytest.approx(expected, abs=1e-6)


@pytest.mark.asyncio
async def test_unlisted_model_is_billed_at_the_cli_list_price(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """A model pricing.py does not list (Opus 4, $15/$75) must not fall to the
    $1/$3 default when the envelope carries the CLI's own bill."""
    rows = _ledger_spy(monkeypatch)
    envelope = (
        b'{"type":"result","is_error":false,"result":"ok",'
        b'"usage":{"input_tokens":1000,"output_tokens":100},'
        b'"modelUsage":{"claude-opus-4-20250514":{}},"total_cost_usd":0.0225}'
    )
    await _run_with_stdout(monkeypatch, envelope, model="claude-opus-4-20250514")
    (row,) = rows
    assert row["cost_usd"] == pytest.approx(0.0225, abs=1e-9)


@pytest.mark.asyncio
async def test_stale_price_table_is_logged_not_trusted_silently(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The CLI's list price audits pricing.py: a disagreement says so."""
    _ledger_spy(monkeypatch)
    envelope = _MEASURED_HAIKU.replace(b'"total_cost_usd":0.000336', b'"total_cost_usd":0.0099')
    with caplog.at_level("WARNING"):
        await _run_with_stdout(monkeypatch, envelope, model="claude-haiku-4-5")
    assert "price tables may be stale" in caplog.text


@pytest.mark.asyncio
async def test_text_path_error_envelope_cools_the_seat_and_cascades(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT: in JSON mode a bad key is an is_error envelope (measured: exit 1,
    result "Invalid API key · Fix external API key"); the text path must read
    its ``result``, cool the seat and move on — never return the JSON."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "bad")
    _seat(monkeypatch, 2, "good")
    bad = (
        b'{"type":"result","is_error":true,"result":"Invalid API key \xc2\xb7 Fix external API key",'
        b'"usage":{"input_tokens":0,"output_tokens":0},"modelUsage":{},"total_cost_usd":0}'
    )

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        if kwargs["env"]["ANTHROPIC_API_KEY"] == "bad":
            return _fake_proc(stdout=bad, returncode=1)
        return _fake_proc(stdout=_MEASURED_HAIKU, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    resp = await mod.complete_async("ping", model="claude-haiku-4-5")
    assert resp.text == "PONG"
    assert resp.token_label == "console_2"
    assert mod._drop_cooling_seats(mod.collect_seats())[0].label == "console_2"


@pytest.mark.asyncio
async def test_failed_exit_around_an_answer_is_not_read_for_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """INNOCENCE: a non-zero exit around a NON-error envelope is an answer; its
    prose ("rate limit", "quota") must not be read as the seat's verdict."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    answer = (
        b'{"type":"result","is_error":false,'
        b'"result":"Error: rate limit exceeded. Your weekly usage limit reached.",'
        b'"usage":{"input_tokens":1,"output_tokens":1}}'
    )

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        return _fake_proc(stdout=answer, returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    with pytest.raises(mod.ClaudeOAuthError) as exc_info:
        await mod.complete_async("ping")
    assert "cli_exit_1" in str(exc_info.value)
    assert mod._drop_cooling_seats(mod.collect_seats())[0].label == "console_1"


def test_cache_write_split_bills_unattributed_writes_at_the_1h_rate() -> None:
    """A write the 5m/1h split does not attribute may over-state, never under-state."""
    from backend.llm.claude_oauth_client import _parse_json_envelope

    no_split = '{"is_error":false,"result":"x","usage":{"input_tokens":1,"output_tokens":1,"cache_creation_input_tokens":100}}'
    _, _, usage, _ = _parse_json_envelope(no_split, expect_structured=False)
    assert (usage.cache_write_5m_tokens, usage.cache_write_1h_tokens) == (0, 100)
    assert usage.prompt_tokens == 101


# ── gate round 1 (gpt-6.1-sol, 2026-11-03) ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "Invalid API key · Fix external API key",
        "Error: authentication failed because the token expired. Request a new token.",
    ],
)
async def test_success_envelope_quoting_a_diagnostic_is_an_answer(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    answer: str,
) -> None:
    """INNOCENCE (Sol r1 BLOCK, CONFIRMED): with an envelope, its is_error bit
    is the verdict. A user who asks to explain an error gets the error's words
    back; classifying that answer discarded it and cooled a healthy seat 900 s."""
    import json as _json

    from backend.llm import claude_oauth_client as mod

    _ledger_spy(monkeypatch)
    envelope = _json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": answer,
            "usage": {"input_tokens": 9, "output_tokens": 9},
            "modelUsage": {"claude-haiku-4-5": {}},
        }
    ).encode()
    resp = await _run_with_stdout(monkeypatch, envelope, model="claude-haiku-4-5")
    assert resp.text == answer
    assert resp.token_label == "console_1"
    assert mod._drop_cooling_seats(mod.collect_seats())[0].label == "console_1"
    assert not mod._seat_cooldowns


@pytest.mark.asyncio
async def test_raw_json_that_is_no_envelope_is_still_the_answer(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """INNOCENCE (Sol r1 note 3, CONFIRMED): any JSON object used to count as an
    envelope, so a raw JSON answer came back as empty_output and was retried."""
    _ledger_spy(monkeypatch)
    resp = await _run_with_stdout(monkeypatch, b'{"domain":"visa","confidence":0.95}', model=None)
    assert resp.text == '{"domain":"visa","confidence":0.95}'


@pytest.mark.asyncio
async def test_failed_attempts_tokens_reach_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    force_schema_supported: None,
) -> None:
    """GUILT (Sol r1 note 8, CONFIRMED): seat 1 answered without the structured
    output and billed 100/10; only seat 2's 50/5 reached the ledger."""
    from backend.llm import claude_oauth_client as mod

    rows = _ledger_spy(monkeypatch)
    _seat(monkeypatch, 1, "first")
    _seat(monkeypatch, 2, "second")
    unstructured = (
        b'{"type":"result","is_error":false,"result":"prose only",'
        b'"usage":{"input_tokens":100,"output_tokens":10},"modelUsage":{"claude-haiku-4-5":{}}}'
    )
    structured = (
        b'{"type":"result","is_error":false,"result":"ok","structured_output":{"domain":"visa","confidence":1},'
        b'"usage":{"input_tokens":50,"output_tokens":5},"modelUsage":{"claude-haiku-4-5":{}}}'
    )

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        first = kwargs["env"]["ANTHROPIC_API_KEY"] == "first"
        return _fake_proc(stdout=unstructured if first else structured, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    resp = await mod.complete_async("classify", model="claude-haiku-4-5", json_schema=_SCHEMA)
    assert resp.structured == {"domain": "visa", "confidence": 1}
    (row,) = rows
    assert (row["input_tokens"], row["output_tokens"]) == (150, 15)
    assert row["cost_usd"] == pytest.approx((150 * 1.00 + 15 * 5.00) / 1e6, abs=1e-9)


@pytest.mark.asyncio
async def test_a_call_that_fails_everywhere_still_books_what_it_spent(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT (Sol r1 note 8): the failure row used to carry the len//4 prompt
    estimate and 0 output, whatever the seats had billed."""
    from backend.llm import claude_oauth_client as mod

    rows = _ledger_spy(monkeypatch)
    answer_on_exit_1 = (
        b'{"type":"result","is_error":false,"result":"partial",'
        b'"usage":{"input_tokens":200,"output_tokens":20},"modelUsage":{"claude-sonnet-4-6":{}}}'
    )
    with pytest.raises(mod.ClaudeOAuthError):
        await _run_with_stdout(monkeypatch, answer_on_exit_1, model="claude-sonnet-4-6", returncode=1)
    (row,) = rows
    assert row["success"] is False
    assert (row["input_tokens"], row["output_tokens"]) == (200, 20)
    assert row["cost_usd"] == pytest.approx((200 * 3.00 + 20 * 15.00) / 1e6, abs=1e-9)


# ── the production CLI pin (2.1.236, scripts/install-claude-code.sh) ────────

# Measured 2026-11-03 on a Console seat with CLI 2.1.236, `--bare --tools ""`,
# no --model: the answer came from claude-fable-5 while a claude-haiku-4-5
# side call ran too. The top-level usage counts the main model only; only
# total_cost_usd sums both.
_PIN_236_UNPINNED = (
    b'{"type":"result","is_error":false,"result":"PONG",'
    b'"usage":{"input_tokens":170,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
    b'"output_tokens":5,"cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":0}},'
    b'"modelUsage":{'
    b'"claude-haiku-4-5-20251001":{"inputTokens":881,"outputTokens":9,"cacheReadInputTokens":0,'
    b'"cacheCreationInputTokens":0,"costUSD":0.000926},'
    b'"claude-fable-5":{"inputTokens":170,"outputTokens":5,"cacheReadInputTokens":0,'
    b'"cacheCreationInputTokens":0,"costUSD":0.00195}},'
    b'"total_cost_usd":0.002876}'
)


@pytest.mark.asyncio
async def test_a_side_call_on_another_model_is_booked_and_billed(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT: reading the top-level usage alone booked 170/5 and dropped the
    881-token Haiku side call. The attempt's counts are the entries' sums, its
    cost the CLI's total, and the row names the model the top-level usage
    describes, not the side call."""
    rows = _ledger_spy(monkeypatch)
    resp = await _run_with_stdout(monkeypatch, _PIN_236_UNPINNED, model=None)
    assert resp.text == "PONG"
    (row,) = rows
    assert (row["input_tokens"], row["output_tokens"]) == (170 + 881, 5 + 9)
    assert row["model"] == "claude-fable-5"
    # fable-5: 170×$10 + 5×$50; haiku-4-5: 881×$1 + 9×$5 — the CLI's own total.
    assert row["cost_usd"] == pytest.approx(0.002876, abs=1e-9)


def test_a_multi_model_envelope_reads_as_one_attempt_billed_at_the_cli_total() -> None:
    from backend.llm.claude_oauth_client import _parse_json_envelope

    _, _, usage, _ = _parse_json_envelope(_PIN_236_UNPINNED.decode(), expect_structured=False)
    assert usage.model == "claude-fable-5"  # its entry equals the top-level usage
    assert (usage.input_tokens, usage.output_tokens) == (170 + 881, 5 + 9)
    assert usage.bill_cli_cost and usage.cli_cost_usd == pytest.approx(0.002876)


@pytest.mark.asyncio
async def test_an_unpinned_call_that_fails_names_the_model_that_spent(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Gate round 2 note: the failure row kept "claude-unknown" while its cost
    was the Fable answer's. Since the default pin (2026-11-03) no call is sent
    unpinned; the row still names the model the CLI reports having spent on."""
    from backend.llm import claude_oauth_client as mod

    rows = _ledger_spy(monkeypatch)
    spent = (
        b'{"type":"result","is_error":false,"result":"partial",'
        b'"usage":{"input_tokens":200,"output_tokens":20},"modelUsage":{"claude-fable-5-1":{}}}'
    )
    with pytest.raises(mod.ClaudeOAuthError):
        await _run_with_stdout(monkeypatch, spent, model=None, returncode=1)
    (row,) = rows
    assert row["model"] == "claude-fable-5-1"
    assert row["cost_usd"] == pytest.approx((200 * 10.00 + 20 * 50.00) / 1e6, abs=1e-9)


# ── gate round 3 (gpt-6.1-sol, 2026-11-03) ──────────────────────────────────


def _multi(top: dict[str, Any], entries: dict[str, Any], total: float | None) -> bytes:
    import json as _json

    body: dict[str, Any] = {"type": "result", "is_error": False, "result": "ok", "usage": top, "modelUsage": entries}
    if total is not None:
        body["total_cost_usd"] = total
    return _json.dumps(body).encode()


_FABLE_TOP_5M = {
    "input_tokens": 170,
    "output_tokens": 5,
    "cache_read_input_tokens": 2000,
    "cache_creation_input_tokens": 10000,
    "cache_creation": {"ephemeral_5m_input_tokens": 10000, "ephemeral_1h_input_tokens": 0},
}
_FABLE_ENTRY_5M = {
    "inputTokens": 170,
    "outputTokens": 5,
    "cacheReadInputTokens": 2000,
    "cacheCreationInputTokens": 10000,
    "costUSD": 0.12895,
}
_HAIKU_SIDE = {"inputTokens": 881, "outputTokens": 9, "costUSD": 0.000926}


@pytest.mark.asyncio
async def test_multi_model_cache_writes_are_billed_at_the_cli_total(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT (Sol r3 BLOCK, CONFIRMED): modelUsage entries carry no 5m/1h split,
    so pricing them from the table billed 10,000 five-minute writes at the 1h
    rate — $0.204876 for a $0.129876 call. The CLI's total applied each model's
    own split: fable-5 170×$10 + 10,000×$12.50 + 2,000×$1 + 5×$50, plus the
    Haiku side call 881×$1 + 9×$5."""
    rows = _ledger_spy(monkeypatch)
    envelope = _multi(
        _FABLE_TOP_5M, {"claude-haiku-4-5-20251001": _HAIKU_SIDE, "claude-fable-5": _FABLE_ENTRY_5M}, 0.129876
    )
    await _run_with_stdout(monkeypatch, envelope, model=None)
    (row,) = rows
    assert row["cost_usd"] == pytest.approx(0.129876, abs=1e-9)
    assert row["model"] == "claude-fable-5"
    assert row["cache_hit_tokens"] == 2000
    assert row["input_tokens"] == 170 + 2000 + 10000 + 881


@pytest.mark.asyncio
async def test_a_malformed_side_entry_does_not_drop_the_bill(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Sol r3 note 3: one entry without outputTokens zeroed every entry and the
    pinned Fable call was booked without its side call."""
    rows = _ledger_spy(monkeypatch)
    broken_side = {"inputTokens": 881, "costUSD": 0.000926}
    envelope = _multi(
        {"input_tokens": 170, "output_tokens": 5},
        {"claude-fable-5": {"inputTokens": 170, "outputTokens": 5}, "claude-haiku-4-5": broken_side},
        0.002876,
    )
    await _run_with_stdout(monkeypatch, envelope, model="claude-fable-5")
    (row,) = rows
    assert row["cost_usd"] == pytest.approx(0.002876, abs=1e-9)


@pytest.mark.asyncio
async def test_a_single_entry_without_top_level_usage_is_still_real(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Sol r3 note 3: a valid single modelUsage entry with no `usage` block fell
    to the len//4 estimate."""
    import json as _json

    rows = _ledger_spy(monkeypatch)
    envelope = _json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "ok",
            "modelUsage": {"claude-haiku-4-5": {"inputTokens": 400, "outputTokens": 40}},
        }
    ).encode()
    await _run_with_stdout(monkeypatch, envelope, model="claude-haiku-4-5")
    (row,) = rows
    assert (row["input_tokens"], row["output_tokens"]) == (400, 40)
    assert row["cost_usd"] == pytest.approx((400 * 1.00 + 40 * 5.00) / 1e6, abs=1e-9)


@pytest.mark.asyncio
async def test_the_answering_model_is_the_one_matching_the_top_level_usage(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """Sol r3 note 4: "costliest" named a 10,000-token Haiku side call as the
    answer; the main model is the entry the top-level usage describes."""
    rows = _ledger_spy(monkeypatch)
    big_side = {"inputTokens": 10000, "outputTokens": 9, "costUSD": 0.010045}
    small_main = {"inputTokens": 170, "outputTokens": 5, "costUSD": 0.00195}
    envelope = _multi(
        {"input_tokens": 170, "output_tokens": 5},
        {"claude-haiku-4-5": big_side, "claude-fable-5": small_main},
        0.011995,
    )
    await _run_with_stdout(monkeypatch, envelope, model=None)
    (row,) = rows
    assert row["model"] == "claude-fable-5"
    assert row["cost_usd"] == pytest.approx(0.011995, abs=1e-9)


# ── the default model pin (2026-11-03, owner's pick: Claude Sonnet 5.5) ─────


@pytest.mark.asyncio
async def test_a_call_that_names_no_model_runs_on_the_default_pin(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """GUILT: with no --model the CLI answered on claude-fable-5-1 ($10/$50,
    measured); a call that names none now runs on claude-sonnet-5-5 ($2/$10)."""
    from backend.llm import claude_oauth_client as mod

    _seat(monkeypatch, 1, "tok1")
    captured: dict[str, Any] = {}

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        captured["cmd"] = list(args)
        return _fake_proc(stdout=_MEASURED_HAIKU, returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    _ledger_spy(monkeypatch)
    await mod.complete_async("ping")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5" == mod.DEFAULT_MODEL
    assert cmd.index("--model") < cmd.index("--")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("  claude-opus-5-5  ", "claude-opus-5-5"),
        ("--evil-flag", "claude-sonnet-5-5"),
        ("gpt-6", "claude-sonnet-5-5"),
        ("", "claude-sonnet-5-5"),
    ],
)
def test_the_default_model_variable_takes_only_a_claude_slug(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    """The override reaches argv: a flag-shaped or foreign value falls back to the pin."""
    from backend.llm import claude_oauth_client as mod

    monkeypatch.setenv("NUZANTARA_CLAUDE_DEFAULT_MODEL", value)
    assert mod.default_model() == expected


def test_the_scripts_restate_the_same_pin_and_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two import-free scripts restate DEFAULT_MODEL / DEFAULT_MODEL_ENV."""
    from backend.llm import claude_oauth_client as mod

    av = _load_script("auto_verifier")
    vg = _load_script("verified_generator")
    for script in (av, vg):
        assert script._DEFAULT_MODEL == mod.DEFAULT_MODEL
        assert script._DEFAULT_MODEL_ENV == mod.DEFAULT_MODEL_ENV
    monkeypatch.setenv(mod.DEFAULT_MODEL_ENV, "claude-haiku-4-5")
    assert av._verifier_model() == vg._gen_model() == mod.default_model() == "claude-haiku-4-5"
    monkeypatch.setenv(mod.DEFAULT_MODEL_ENV, "--evil-flag")
    assert av._verifier_model() == vg._gen_model() == mod.default_model() == "claude-sonnet-5-5"


@pytest.mark.asyncio
async def test_a_price_disagreement_is_said_once_per_model(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Fly pin (CLI 2.1.236) estimates Sonnet 5.5 at $3/$15 (measured: 172/5
    → $0.000591) against the official $2/$10 ($0.000394): every call disagrees,
    so the warning is said once per model per process and the ledger keeps the
    table's figure."""
    rows = _ledger_spy(monkeypatch)
    envelope = (
        b'{"type":"result","is_error":false,"result":"PONG",'
        b'"usage":{"input_tokens":172,"output_tokens":5},'
        b'"modelUsage":{"claude-sonnet-5-5":{}},"total_cost_usd":0.000591}'
    )
    with caplog.at_level("WARNING"):
        await _run_with_stdout(monkeypatch, envelope, model=None)
        await _run_with_stdout(monkeypatch, envelope, model=None)
    assert caplog.text.count("price tables may be stale") == 1
    assert [row["cost_usd"] for row in rows] == [pytest.approx(0.000394, abs=1e-9)] * 2


# ── CLI overhead (2026-11-03): non-essential traffic off, API time reported ─


def test_the_child_runs_without_non_essential_traffic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on Pro (CLI 2.1.317): 3.0 s per call with the CLI's telemetry /
    error-reporting / update traffic, 1.75 s without, the API ~1.1 s either way.
    The three env builders set it alike (the parity test pins the rest)."""
    from backend.llm import claude_oauth_client as mod

    monkeypatch.delenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", raising=False)
    seat = mod.ConsoleSeat(key="sk-test", workspace="wrkspc_test1", label="console_1")
    assert mod.build_seat_env(seat)["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    av = _load_script("auto_verifier")
    vg = _load_script("verified_generator")
    assert av._verifier_seat_env("sk-test", "wrkspc_test1")["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert vg._gen_seat_env("sk-test", "wrkspc_test1")["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


@pytest.mark.asyncio
async def test_the_response_carries_the_api_share_of_the_call(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    """``elapsed_s - api_s`` is the CLI's own time: 22 s in the Fly container
    against ~1.1 s of API on Pro is what this split exists to locate."""
    _ledger_spy(monkeypatch)
    envelope = _MEASURED_HAIKU.replace(b'"result":"PONG",', b'"result":"PONG","duration_api_ms":1184,')
    resp = await _run_with_stdout(monkeypatch, envelope, model="claude-haiku-4-5")
    assert resp.api_s == pytest.approx(1.184)


@pytest.mark.asyncio
async def test_no_envelope_no_api_share(
    monkeypatch: pytest.MonkeyPatch,
    clear_oauth_env: None,
) -> None:
    _ledger_spy(monkeypatch)
    resp = await _run_with_stdout(monkeypatch, b"plain answer", model="claude-haiku-4-5")
    assert resp.text == "plain answer" and resp.api_s is None
