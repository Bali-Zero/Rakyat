"""apps/backend-rag/scripts/console_seats_env.sh — the pair files become the slot variables.

The snippet is SOURCED by headless wrappers (scripts/zoho-mail-loop.sh) so that no
wrapper outside the lane touches a credential itself (catE #40c judged the inline
mapping in the wrapper a renamed credential, 2026-11-03). These cases run it under
`bash -eu` exactly as a wrapper would, with synthetic pair files only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SNIPPET = Path(__file__).resolve().parents[4] / "scripts" / "console_seats_env.sh"

_PROBE = (
    '. "$SNIPPET"; '
    'printf "count=%s\\n" "$NUZANTARA_CONSOLE_SEAT_COUNT"; '
    'for n in 1 2 3; do eval "printf \\"k$n=%s w$n=%s\\n\\" \\"\\${ANTHROPIC_CONSOLE_KEY_$n:-unset}\\" '
    '\\"\\${ANTHROPIC_CONSOLE_WORKSPACE_$n:-unset}\\""; done; '
    'set | grep -c "^_console_" || true'
)


def _source(secrets: Path, seats: str | None = None) -> list[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(secrets.parent), "SNIPPET": str(SNIPPET),
           "NUZANTARA_CONSOLE_SECRETS_DIR": str(secrets)}
    if seats is not None:
        env["NUZANTARA_CONSOLE_SEATS"] = seats
    done = subprocess.run(["bash", "-euc", _PROBE], env=env, capture_output=True, text=True, check=True)
    assert done.stderr == "", done.stderr
    return done.stdout.splitlines()


def _pair(secrets: Path, org: str, key: str, workspace: str) -> None:
    (secrets / f"anthropic-console-{org}.key").write_text(key)
    (secrets / f"anthropic-console-{org}.workspace").write_text(workspace)


def test_slots_follow_the_org_order_and_skip_an_incomplete_pair(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    _pair(secrets, "antozero", "sk-one\n", "wrkspc_one\n")
    _pair(secrets, "kaiser", "", "wrkspc_two")  # empty key: not a seat
    _pair(secrets, "antosiano", "  sk-three ", "wrkspc_three")

    out = _source(secrets)

    assert out[0] == "count=2"
    assert out[1] == "k1=sk-one w1=wrkspc_one"  # whitespace trimmed
    assert out[2] == "k2=sk-three w2=wrkspc_three"  # the next org takes the next slot
    assert out[3] == "k3=unset w3=unset"
    assert out[4] == "0", "the snippet must not leave its own variables behind"


def test_the_order_is_configurable(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    _pair(secrets, "antozero", "sk-a", "wrkspc_a")
    _pair(secrets, "kaiser", "sk-k", "wrkspc_k")

    out = _source(secrets, seats="kaiser antozero")

    assert out[1] == "k1=sk-k w1=wrkspc_k"
    assert out[2] == "k2=sk-a w2=wrkspc_a"


def test_no_pair_files_is_zero_seats_not_an_error(tmp_path: Path) -> None:
    """INNOCENCE: a machine with no pair files sources cleanly under -eu and
    reports 0 — the consumer (draft.py) is the one that refuses, by name."""
    out = _source(tmp_path / "does-not-exist")

    assert out[0] == "count=0"
    assert out[1] == "k1=unset w1=unset"


def test_an_inherited_xtrace_never_prints_a_key_and_is_restored(tmp_path: Path) -> None:
    """GUILT (gate round 3, 2026-11-03): under the caller's `set -x` the shell
    itself printed `export ANTHROPIC_CONSOLE_KEY_1=<value>`. The snippet now
    suspends xtrace while it reads and exports, and gives it back after."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    _pair(secrets, "antozero", "sk-must-not-trace", "wrkspc_trace")
    done = subprocess.run(
        ["bash", "-euxc", '. "$SNIPPET"; case $- in *x*) echo XTRACE_RESTORED ;; esac'],
        env={"PATH": "/usr/bin:/bin", "SNIPPET": str(SNIPPET), "NUZANTARA_CONSOLE_SECRETS_DIR": str(secrets)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "sk-must-not-trace" not in done.stdout + done.stderr
    assert "wrkspc_trace" not in done.stdout + done.stderr
    assert "XTRACE_RESTORED" in done.stdout


def test_an_unreadable_pair_is_skipped_not_fatal(tmp_path: Path) -> None:
    """Under `set -eu` a failing read must skip the slot — never exit the
    caller, never export an empty key and count it as a seat."""
    import os

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    _pair(secrets, "antozero", "sk-locked", "wrkspc_locked")
    _pair(secrets, "kaiser", "sk-open", "wrkspc_open")
    locked = secrets / "anthropic-console-antozero.key"
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as root: chmod cannot make the file unreadable")
        out = _source(secrets)
    finally:
        locked.chmod(0o600)
    assert out[0] == "count=1"
    assert out[1] == "k1=sk-open w1=wrkspc_open"


def test_the_snippet_prints_nothing_itself(tmp_path: Path) -> None:
    """GUILT guard: a key must never reach stdout/stderr by sourcing alone."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    _pair(secrets, "antozero", "sk-must-not-print", "wrkspc_x")
    done = subprocess.run(
        ["bash", "-euc", '. "$SNIPPET"'],
        env={"PATH": "/usr/bin:/bin", "SNIPPET": str(SNIPPET), "NUZANTARA_CONSOLE_SECRETS_DIR": str(secrets)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == "" and done.stderr == ""
