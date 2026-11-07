"""Tests for ConsiglioV1 orchestrator — claims, voting, Gate 6 predicate."""

from __future__ import annotations

from backend.services.research.consiglio_orchestrator import (
    ConsiglioClaim,
    ConsiglioResult,
    ConsiglioV1,
)


def test_claim_agreement_count():
    c = ConsiglioClaim(
        key="cadence_instagram_posts_per_day",
        value=1.0,
        votes={"claude": True, "gemini": True, "kimi": True, "notebooklm": False},
    )
    assert c.agreement_count() == 3
    assert c.is_disputed() is False


def test_claim_disputed_when_below_3():
    c = ConsiglioClaim(
        key="format_linkedin_authority_tecnico",
        value="long_post",
        votes={"claude": True, "gemini": True, "kimi": False, "notebooklm": False},
    )
    assert c.agreement_count() == 2
    assert c.is_disputed() is True


def test_claim_is_disputed_respects_custom_threshold():
    """3-LLM minimum is default; with only 2 responding LLMs, 2/2 passes."""
    c = ConsiglioClaim(
        key="x",
        value="y",
        votes={"claude": True, "kimi": True},  # gemini + notebooklm missing
    )
    # With 2 present votes + both True → agreement=2. Default threshold
    # is 3, so this is disputed. But lowered to 2, it's not.
    assert c.agreement_count() == 2
    assert c.is_disputed(min_agreement=3) is True
    assert c.is_disputed(min_agreement=2) is False


def test_result_gate_6_passes_when_all_claims_reach_quorum():
    claims = [
        ConsiglioClaim(
            key="k1",
            value="v",
            votes={"claude": True, "gemini": True, "kimi": True, "notebooklm": True},
        ),
        ConsiglioClaim(
            key="k2",
            value="v",
            votes={"claude": True, "gemini": True, "kimi": True, "notebooklm": False},
        ),
    ]
    result = ConsiglioResult(claims=claims, meta={})
    assert result.gate_6_passes()


def test_result_gate_6_fails_if_any_claim_disputed():
    claims = [
        ConsiglioClaim(
            key="k1",
            value="v",
            votes={"claude": True, "gemini": True, "kimi": True, "notebooklm": False},
        ),
        ConsiglioClaim(
            key="k2",
            value="v",
            votes={"claude": True, "gemini": False, "kimi": False, "notebooklm": False},
        ),
    ]
    result = ConsiglioResult(claims=claims, meta={})
    assert result.gate_6_passes() is False
    assert result.disputed_keys() == ["k2"]


def test_result_gate_6_adapts_to_available_voters():
    """If only 2 LLMs responded across all claims, Gate 6 requires 2/2."""
    claims = [
        ConsiglioClaim(
            key="k1",
            value="v",
            votes={"claude": True, "kimi": True},
        ),
        ConsiglioClaim(
            key="k2",
            value="v",
            votes={"claude": True, "kimi": False},
        ),
    ]
    result = ConsiglioResult(claims=claims, meta={"active_llms": 2})
    # With only 2 voters, threshold auto-adjusts to 2 (full agreement required)
    assert result.gate_6_passes(min_agreement=2) is False  # k2 has only 1/2
    assert result.disputed_keys(min_agreement=2) == ["k2"]


def test_values_agree_fuzzy_strings():
    assert ConsiglioV1._values_agree("long_post", "long_post") is True
    assert ConsiglioV1._values_agree("Long_Post ", "long_post") is True
    assert ConsiglioV1._values_agree("long_post", "carousel") is False


def test_values_agree_scalars():
    assert ConsiglioV1._values_agree(1.0, 1.0) is True
    assert ConsiglioV1._values_agree(1.0, 1.0001) is False  # strict equality
    assert ConsiglioV1._values_agree([1, 2], [1, 2]) is True
    assert ConsiglioV1._values_agree(None, None) is True


def test_values_agree_type_mismatch():
    assert ConsiglioV1._values_agree("1.0", 1.0) is False


def test_a_permanently_failing_member_never_flips_agreement_or_dispute():
    """Pins the PENDING-ARMS correction (2026-08-29, follow-up on #5211):
    a member that never responds at all gets recorded as an explicit
    `False` vote on every claim (a known, separately-ledgered merge-loop
    bug), but this can never change `agreement_count()`/`is_disputed()`
    because those only sum/compare TRUE votes against a threshold — a
    `False`-only phantom entry contributes nothing to either side of
    that comparison. The only thing a phantom entry changes is
    `len(votes)`, a display-only artifact never read by a gate.

    If this test ever goes red, the phantom-vote bug has stopped being
    outcome-inert and the PENDING-ARMS severity assessment must be
    revisited — do not silently adjust the assertion to match new
    behavior.
    """
    votes_without_phantom = {"claude": True, "gemini": True, "notebooklm": False}
    votes_with_phantom = {**votes_without_phantom, "deepseek": False}

    without = ConsiglioClaim(key="k", value="v", votes=votes_without_phantom)
    with_phantom = ConsiglioClaim(key="k", value="v", votes=votes_with_phantom)

    assert without.agreement_count() == with_phantom.agreement_count()
    for threshold in (1, 2, 3, 4):
        assert without.is_disputed(min_agreement=threshold) == with_phantom.is_disputed(
            min_agreement=threshold
        )
    # The one thing that DOES differ — confirms the bug is real, not that
    # nothing changed at all.
    assert len(with_phantom.votes) == len(without.votes) + 1


# ── the claude member runs on a Console seat (RULED 2026-11-01) ──────────────


def _no_seat(monkeypatch) -> None:
    for n in (1, 2, 3):
        monkeypatch.delenv(f"ANTHROPIC_CONSOLE_KEY_{n}", raising=False)
        monkeypatch.delenv(f"ANTHROPIC_CONSOLE_WORKSPACE_{n}", raising=False)


def test_ask_claude_refuses_without_a_console_seat(monkeypatch):
    """GUILT: no seat is no door — `claude` is never run on the inherited
    environment, whose plan-OAuth session is the shut fallback."""
    from unittest.mock import patch

    _no_seat(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-oauth-is-not-a-seat")
    with patch("subprocess.run") as run_mock:
        try:
            ConsiglioV1()._ask("claude", "question", None)
        except RuntimeError as exc:
            assert "ANTHROPIC_CONSOLE_KEY_" in str(exc)
        else:  # pragma: no cover - guilt case
            raise AssertionError("claude ran without a sanctioned seat")
    run_mock.assert_not_called()


def test_ask_claude_runs_on_the_seat_env_and_the_others_inherit(monkeypatch):
    """INNOCENCE: with a seat the claude child gets the seat's key + header
    and no plan OAuth; a non-claude member keeps its own auth (env=None)."""
    from unittest.mock import patch

    monkeypatch.setenv("ANTHROPIC_CONSOLE_KEY_1", "sk-test-seat")
    monkeypatch.setenv("ANTHROPIC_CONSOLE_WORKSPACE_1", "wrkspc_test1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "plan-oauth-must-not-reach-the-child")
    fake_proc = type("P", (), {"returncode": 0, "stdout": '{"claims": []}', "stderr": ""})()
    with patch("subprocess.run", return_value=fake_proc) as run_mock:
        ConsiglioV1()._ask("claude", "question", None)
        env = run_mock.call_args.kwargs["env"]
        assert env["ANTHROPIC_API_KEY"] == "sk-test-seat"
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == "anthropic-workspace-id: wrkspc_test1"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        ConsiglioV1()._ask("gemini", "question", None)
        assert run_mock.call_args.kwargs["env"] is None
