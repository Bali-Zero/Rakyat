"""Guilt + innocence for scripts/check_ban_predicates.py.

Two things need proving and they are different kinds of claim.

TRANSCRIPTION. The script re-states two regexes that live in
`.github/workflows/catE-sovereignty-lint.yml`. A transcription that drifts is
worse than no local check at all: it reports green on something CI will red, or
red on something CI allows. The first tests shell-unquote the workflow's own
`grep -rnE` arguments and compare them character for character.

BEHAVIOUR. The rest run the script against synthetic trees. Every banned
literal is ASSEMBLED at runtime rather than typed here — the predicates read
the FIXTURE, never this file, so the corpus is exactly as guilty either way,
and a spelled-out literal in a test source is what spent the incumbent's
budget in the first place.

Run:  python3 -m pytest scripts/tests/test_ban_predicates.py -q
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "catE-sovereignty-lint.yml"

_spec = importlib.util.spec_from_file_location(
    "check_ban_predicates", REPO_ROOT / "scripts" / "check_ban_predicates.py"
)
cbp = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(cbp)

# The banned spellings, never written out in this file.
PAID_CALL = "Anthropic(" + "api_key" + "=os.environ['X'])"
OAUTH_CALL = "Anthropic(" + "auth_token" + "=os.environ['X'])"
QUOTED_KEY = '{"' + "api_key" + '": "opaque-value-here"}'


def _grep_args() -> list[list[str]]:
    """Shell-unquote every `grep -rnE ...` invocation in the workflow."""
    out = []
    for line in WORKFLOW.read_text().splitlines():
        marker = "grep -rnE "
        if marker not in line:
            continue
        rest = line.split(marker, 1)[1].rstrip()
        if rest.endswith("\\"):
            rest = rest[:-1]
        out.append(shlex.split(rest))
    return out


def _grep_v_args() -> list[str]:
    out = []
    for line in WORKFLOW.read_text().splitlines():
        marker = "grep -vE "
        if marker not in line:
            continue
        rest = line.split(marker, 1)[1].rstrip()
        if rest.endswith("\\"):
            rest = rest[:-1]
        parsed = shlex.split(rest)
        if parsed:
            out.append(parsed[0])
    return out


def test_p1_transcription_is_the_workflow_regex():
    patterns = [a[0] for a in _grep_args()]
    assert cbp.P1_PATTERN in patterns, (
        "the paid-constructor regex in check_ban_predicates.py is no longer "
        f"one the workflow runs; workflow has {patterns}"
    )


def test_p2_transcription_is_the_workflow_regex():
    patterns = [a[0] for a in _grep_args()]
    assert cbp.P2_PATTERN in patterns, (
        "the key-assignment regex in check_ban_predicates.py is no longer "
        f"one the workflow runs; workflow has {patterns}"
    )


def test_both_exclusion_lists_are_the_workflow_ones():
    excludes = _grep_v_args()
    assert cbp.P1_EXCLUDE in excludes
    assert cbp.P2_EXCLUDE in excludes


def test_p5_transcription_is_the_workflow_regex():
    """#40e (2026-11-02): the reseller route, transcribed like P1/P2."""
    patterns = [a[0] for a in _grep_args()]
    assert cbp.P5_PATTERN in patterns, (
        "the cloud-reseller regex in check_ban_predicates.py is no longer "
        f"one the workflow runs; workflow has {patterns}"
    )
    assert cbp.P5_EXCLUDE in _grep_v_args()


def test_p6_transcription_is_the_workflow_regex():
    """#40f (2026-11-02): plan OAuth in the product perimeter, transcribed like P1/P2/P5."""
    patterns = [a[0] for a in _grep_args()]
    assert cbp.P6_PATTERN in patterns, (
        "the plan-OAuth regex in check_ban_predicates.py is no longer "
        f"one the workflow runs; workflow has {patterns}"
    )
    assert cbp.P6_EXCLUDE in _grep_v_args()
    # the workflow greps exactly the product perimeter this script scopes P6 to:
    # the grep's continuation line names the directory, nothing wider
    lines = WORKFLOW.read_text().splitlines()
    idx = next(i for i, l in enumerate(lines) if "grep -rnE " in l and "auth_token=" in l)
    # ONE directory: the workflow greps a single path, so a second prefix in
    # the script would be a perimeter the workflow does not scan (Sol gate, round 2)
    assert len(cbp.PRODUCT_PREFIXES) == 1
    assert lines[idx + 1].strip() == f"--include='*.py' {cbp.PRODUCT_PREFIXES[0].rstrip('/')} \\", lines[idx + 1]
    assert f"BASELINE_FILE={cbp.PLAN_OAUTH_BASELINE_REL}" in "\n".join(lines)


def test_the_lane_manifest_path_is_the_workflow_one():
    """The workflow reads LANES_FILE=<path>; the transcription must read the same file."""
    text = WORKFLOW.read_text()
    assert f"LANES_FILE={cbp.LANES_REL}" in text, (
        f"workflow no longer reads {cbp.LANES_REL} as its headless-lane manifest"
    )
    # and the committed manifest names at least one lane, so main is not a
    # tree where the sanctioned shape has nowhere to live
    live = (REPO_ROOT / cbp.LANES_REL).read_text().splitlines()
    assert any(l.strip() and not l.strip().startswith("#") for l in live)


def test_posix_class_translation_keeps_the_meaning():
    rx = cbp._posix_bracket_to_python(cbp.P2_PATTERN)
    import re

    assert re.search(rx, "export ANTHROPIC_API_KEY=value")
    assert not re.search(rx, "unset ANTHROPIC_API_KEY")


# ── behaviour ───────────────────────────────────────────────────────────────


def _empty_baseline() -> str:
    """The repo's own baseline, emptied of results — same plugin set, no rows."""
    import json

    data = json.loads((REPO_ROOT / ".secrets.baseline").read_text())
    data["results"] = {}
    return json.dumps(data)


def _run(monkeypatch, tmp_path: Path, *names: str) -> int:
    """Judge RELATIVE paths inside a fake repo root.

    Absolute paths would carry pytest's own tmp dir name into the predicate,
    and every exclusion list here contains `test_` — the fixtures would excuse
    themselves and every guilt case would pass.
    """
    monkeypatch.setattr(cbp, "REPO_ROOT", tmp_path)
    baseline = tmp_path / ".secrets.baseline"
    if not baseline.exists():
        baseline.write_text(_empty_baseline())
    monkeypatch.setattr(cbp, "BASELINE", baseline)
    old = sys.argv
    sys.argv = ["check_ban_predicates.py", *names]
    try:
        return cbp.main()
    finally:
        sys.argv = old


def test_guilt_a_new_paid_call_site_is_refused(tmp_path, capsys, monkeypatch):
    (tmp_path / "app.py").write_text(f"client = {PAID_CALL}\n")
    assert _run(monkeypatch, tmp_path, "app.py") == 1
    assert "P1 catE #40" in capsys.readouterr().out


def test_innocence_the_oauth_constructor_passes(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text(f"client = {OAUTH_CALL}\n")
    assert _run(monkeypatch, tmp_path, "app.py") == 0


# ── the headless lane (RULED 2026-10-26 + 2026-11-01, guards re-pointed 2026-11-02)


def _manifest(root: Path, *prefixes: str) -> None:
    f = root / cbp.LANES_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# test manifest\n" + "".join(f"{p}\n" for p in prefixes))


def test_innocence_the_constructor_inside_a_headless_lane_is_sanctioned(tmp_path, capsys, monkeypatch):
    _manifest(tmp_path, "apps/svc/backend/")
    (tmp_path / "apps/svc/backend").mkdir(parents=True)
    (tmp_path / "apps/svc/backend/client.py").write_text(f"client = {PAID_CALL}\n")
    assert _run(monkeypatch, tmp_path, "apps/svc/backend/client.py") == 0
    out = capsys.readouterr().out
    assert "sanctioned" in out and "apps/svc/backend/client.py:1" in out


def test_guilt_the_same_constructor_outside_every_lane_is_refused(tmp_path, capsys, monkeypatch):
    _manifest(tmp_path, "apps/svc/backend/")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/seat_launcher.py").write_text(f"client = {PAID_CALL}\n")
    assert _run(monkeypatch, tmp_path, "scripts/seat_launcher.py") == 1
    assert "P1 catE #40" in capsys.readouterr().out


def test_guilt_a_missing_manifest_sanctions_nothing(tmp_path, capsys, monkeypatch):
    """Fails closed: no manifest, no lane — the pre-2026-11-02 verdict."""
    (tmp_path / "apps/svc/backend").mkdir(parents=True)
    (tmp_path / "apps/svc/backend/client.py").write_text(f"client = {PAID_CALL}\n")
    assert _run(monkeypatch, tmp_path, "apps/svc/backend/client.py") == 1


def test_a_lane_is_a_prefix_not_a_substring(tmp_path, monkeypatch):
    _manifest(tmp_path, "apps/svc/backend/")
    (tmp_path / "vendor/apps/svc/backend").mkdir(parents=True)
    (tmp_path / "vendor/apps/svc/backend/client.py").write_text(f"client = {PAID_CALL}\n")
    # P1's exclusion list does not carry vendor/, so this reaches the lane test
    assert _run(monkeypatch, tmp_path, "vendor/apps/svc/backend/client.py") == 1


def test_a_malformed_prefix_sanctions_nothing(tmp_path, monkeypatch):
    """Sol gate, 2026-11-02: a bare `a` would sanction apps/mouth/. A prefix is
    a directory — ending in `/`, relative, no glob, no `..`, no space/colon."""
    # the last two are ONE line each: a \x0b or \x0c inside a line must not make
    # a prefix out of its first half (str.splitlines() would; the workflow's
    # read does not — Sol gate, 2026-11-02)
    _manifest(
        tmp_path, "a", "apps", "apps/*/", "/apps/svc/", "apps/../svc/", "apps/svc backend/",
        "apps/svc/backend/\x0bgarbage", "apps/svc/backend/\x0c/",
        # and a bare \r: read_text() would have turned it into a line break
        # (round 2 of the same gate), so the readers decode bytes instead
        "apps/svc/backend/\rgarbage/",
    )
    (tmp_path / "apps/svc/backend").mkdir(parents=True)
    (tmp_path / "apps/svc/backend/client.py").write_text(f"client = {PAID_CALL}\n")
    monkeypatch.setattr(cbp, "REPO_ROOT", tmp_path)
    assert cbp.headless_lane_prefixes() == []
    assert _run(monkeypatch, tmp_path, "apps/svc/backend/client.py") == 1


def test_the_lane_never_pardons_an_exported_key(tmp_path, capsys, monkeypatch):
    """#40b is lane-blind by design: an export reaches an interactive claude."""
    _manifest(tmp_path, "apps/svc/backend/")
    (tmp_path / "apps/svc/backend").mkdir(parents=True)
    (tmp_path / "apps/svc/backend/run.sh").write_text("export ANTHROPIC_API_KEY=$KEY\n")
    assert _run(monkeypatch, tmp_path, "apps/svc/backend/run.sh") == 1
    assert "P2 catE #40b" in capsys.readouterr().out


# ── the cloud-reseller route (#40e / P5)

RESELLER_CALL = "Anthropic" + "Bedrock" + "(aws_region='x')"
RESELLER_SWITCH = "export CLAUDE_CODE_USE_" + "BEDROCK" + "=1"


def test_guilt_a_reseller_client_is_refused_even_inside_a_lane(tmp_path, capsys, monkeypatch):
    _manifest(tmp_path, "apps/svc/backend/")
    (tmp_path / "apps/svc/backend").mkdir(parents=True)
    (tmp_path / "apps/svc/backend/client.py").write_text(f"client = {RESELLER_CALL}\n")
    assert _run(monkeypatch, tmp_path, "apps/svc/backend/client.py") == 1
    assert "P5 catE #40e" in capsys.readouterr().out


def test_guilt_the_reseller_switch_exported_on_is_refused(tmp_path, capsys, monkeypatch):
    (tmp_path / "run.sh").write_text(f"{RESELLER_SWITCH}\n")
    assert _run(monkeypatch, tmp_path, "run.sh") == 1
    assert "P5 catE #40e" in capsys.readouterr().out


def test_guilt_a_quoted_reseller_switch_is_refused_too(tmp_path, capsys, monkeypatch):
    """Sol gate, 2026-11-02: `="1"` turns the switch on exactly as `=1` does."""
    (tmp_path / "run.sh").write_text('export CLAUDE_CODE_USE_' + 'VERTEX' + '="1"\n')
    (tmp_path / "run2.sh").write_text("export CLAUDE_CODE_USE_" + "FOUNDRY" + "='true'\n")
    assert _run(monkeypatch, tmp_path, "run.sh", "run2.sh") == 1
    assert capsys.readouterr().out.count("P5 catE #40e") == 2


def test_innocence_an_emptied_reseller_switch_is_a_strip(tmp_path, monkeypatch):
    (tmp_path / "run.sh").write_text('export CLAUDE_CODE_USE_' + 'BEDROCK' + '=""\n')
    assert _run(monkeypatch, tmp_path, "run.sh") == 0


# ── plan OAuth in the product perimeter (#40f / P6)

OAUTH_READ = 'tok = os.environ.get("CLAUDE_CODE_OAUTH' + '_TOKEN", "")'
OAUTH_SET = 'env["CLAUDE_CODE_OAUTH' + '_TOKEN"] = tok'
OAUTH_POP = 'env.pop("CLAUDE_CODE_OAUTH' + '_TOKEN", None)'
OAUTH_MSG = 'raise RuntimeError("set CLAUDE_CODE_OAUTH' + '_TOKEN first")'


def _baseline(root: Path, *paths: str) -> None:
    f = root / cbp.PLAN_OAUTH_BASELINE_REL
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# test baseline\n" + "".join(f"{p}\n" for p in paths))


def _product_file(root: Path, rel: str, text: str) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text + "\n")


def test_guilt_plan_oauth_in_an_unbaselined_product_file_is_refused(tmp_path, capsys, monkeypatch):
    _product_file(tmp_path, "apps/backend-rag/backend/newroute.py", OAUTH_READ)
    assert _run(monkeypatch, tmp_path, "apps/backend-rag/backend/newroute.py") == 1
    assert "P6 catE #40f" in capsys.readouterr().out


def test_guilt_the_oauth_kwarg_and_the_env_set_are_refused_too(tmp_path, capsys, monkeypatch):
    _product_file(tmp_path, "apps/backend-rag/backend/a.py", f"client = {OAUTH_CALL}")
    _product_file(tmp_path, "apps/backend-rag/scripts/b.py", OAUTH_SET)
    assert _run(monkeypatch, tmp_path, "apps/backend-rag/backend/a.py", "apps/backend-rag/scripts/b.py") == 1
    assert capsys.readouterr().out.count("P6 catE #40f") == 2


def test_innocence_a_baselined_product_file_is_reported_not_refused(tmp_path, capsys, monkeypatch):
    _baseline(tmp_path, "apps/backend-rag/backend/client.py")
    _product_file(tmp_path, "apps/backend-rag/backend/client.py", OAUTH_READ)
    assert _run(monkeypatch, tmp_path, "apps/backend-rag/backend/client.py") == 0
    assert "baselined until drained" in capsys.readouterr().out


def test_innocence_the_same_read_outside_the_product_perimeter_is_not_p6(tmp_path, capsys, monkeypatch):
    _product_file(tmp_path, "scripts/cron_seat.py", OAUTH_READ)
    assert _run(monkeypatch, tmp_path, "scripts/cron_seat.py") == 0


def test_innocence_a_pop_and_a_message_are_not_reads(tmp_path, monkeypatch):
    _product_file(tmp_path, "apps/backend-rag/backend/strip.py", OAUTH_POP + "\n" + OAUTH_MSG)
    assert _run(monkeypatch, tmp_path, "apps/backend-rag/backend/strip.py") == 0


def test_the_oauth_constructor_stays_innocent_outside_the_product_perimeter(tmp_path, monkeypatch):
    """The human-seat path: auth_token= in a seat script is not this predicate's."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/seat.py").write_text(f"client = {OAUTH_CALL}\n")
    assert _run(monkeypatch, tmp_path, "scripts/seat.py") == 0


def test_innocence_a_reseller_strip_list_passes(tmp_path, monkeypatch):
    """The shape on main today: the variable NAMES listed in order to unset them."""
    (tmp_path / "strip.sh").write_text(
        "for v in CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX CLAUDE_CODE_USE_FOUNDRY; do unset $v; done\n"
    )
    (tmp_path / "strip.py").write_text(
        'for v in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):\n    os.environ.pop(v, None)\n'
    )
    assert _run(monkeypatch, tmp_path, "strip.sh", "strip.py") == 0


def test_innocence_a_guilt_corpus_under_test_prefix_passes(tmp_path, monkeypatch):
    (tmp_path / "test_fixture.py").write_text(f"client = {PAID_CALL}\n")
    assert _run(monkeypatch, tmp_path, "test_fixture.py") == 0


def test_innocence_prose_that_describes_the_shape_passes(tmp_path, monkeypatch):
    (tmp_path / "doc.py").write_text(
        '"""We ban the vendor SDK constructor taking a per-token key."""\n'
    )
    assert _run(monkeypatch, tmp_path, "doc.py") == 0


def test_guilt_prose_that_spells_the_constructor_is_refused(tmp_path, monkeypatch):
    """The exact shape that reddened catE on PR #6926: a docstring quoting it."""
    (tmp_path / "doc.py").write_text(f'"""Do not write {PAID_CALL} here."""\n')
    assert _run(monkeypatch, tmp_path, "doc.py") == 1


@pytest.mark.skipif(
    shutil.which("detect-secrets") is None, reason="detect-secrets not installed"
)
def test_guilt_a_quoted_key_in_prose_is_refused_by_the_scanner(tmp_path, capsys, monkeypatch):
    """The shape that reddened Detect Secrets on #6926 and #6935.

    No grep predicate sees this one — it is the detect-secrets half, and it is
    the half that caught two of the four occurrences.
    """
    (tmp_path / "note.py").write_text(f"# the rule misses {QUOTED_KEY}\n")
    rc = _run(monkeypatch, tmp_path, "note.py")
    assert rc == 1
    assert "P3 detect-secrets" in capsys.readouterr().out


def test_a_missing_scanner_is_announced_and_never_silently_green(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cbp.shutil, "which", lambda _: None)
    (tmp_path / "app.py").write_text("x = 1\n")
    assert _run(monkeypatch, tmp_path, "app.py") == 0
    out = capsys.readouterr().out
    assert "NOT run" in out and "detect-secrets" in out
    assert out.count("ban-predicates] scanner half") == 1, (
        "the skip notice prints on every commit on a machine without the "
        "scanner; more than one line and it becomes wallpaper"
    )


def test_the_kill_switch_exits_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BAN_PREDICATES_ENFORCEMENT", "false")
    (tmp_path / "app.py").write_text(f"client = {PAID_CALL}\n")
    assert _run(monkeypatch, tmp_path, "app.py") == 0
    assert "DISABLED" in capsys.readouterr().out


# ── staged mode: only what this commit ADDS ─────────────────────────────────


def _repo(tmp_path: Path) -> Path:
    d = tmp_path / "wt"
    d.mkdir()
    run = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "t")
    return d


def test_staged_mode_judges_added_lines_not_the_whole_file(tmp_path, monkeypatch, capsys):
    """A pre-existing offending line in a file you touch is not yours.

    Two such lines live in catE-sovereignty-lint.yml itself, so a whole-file
    rule would make that workflow uneditable without the kill switch — a gate
    you must disable to touch is a gate being trained out of existence.
    """
    d = _repo(tmp_path)
    (d / "app.py").write_text(f"client = {PAID_CALL}\n")
    subprocess.run(["git", "add", "app.py"], cwd=d, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed", "--no-verify"], cwd=d, check=True, capture_output=True
    )
    (d / "app.py").write_text(f"client = {PAID_CALL}\nunrelated = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=d, check=True, capture_output=True)

    monkeypatch.setattr(cbp, "REPO_ROOT", d)
    monkeypatch.setattr(cbp, "BASELINE", d / ".secrets.baseline")
    old = sys.argv
    sys.argv = ["check_ban_predicates.py", "--staged"]
    try:
        rc = cbp.main()
    finally:
        sys.argv = old
    assert rc == 0, capsys.readouterr().out


def test_staged_mode_refuses_an_added_offending_line(tmp_path, monkeypatch, capsys):
    d = _repo(tmp_path)
    (d / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=d, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "seed", "--no-verify"], cwd=d, check=True, capture_output=True
    )
    (d / "app.py").write_text(f"x = 1\nclient = {PAID_CALL}\n")
    subprocess.run(["git", "add", "app.py"], cwd=d, check=True, capture_output=True)

    monkeypatch.setattr(cbp, "REPO_ROOT", d)
    monkeypatch.setattr(cbp, "BASELINE", d / ".secrets.baseline")
    old = sys.argv
    sys.argv = ["check_ban_predicates.py", "--staged"]
    try:
        rc = cbp.main()
    finally:
        sys.argv = old
    assert rc == 1
    assert "app.py:2" in capsys.readouterr().out


# ── the --baseline flag this gate needed, and the near-miss that shaped it ──
#
# P3 asks "what WOULD a scan say" and must not answer by mutating the tracked
# baseline. That is why `--baseline PATH` exists on both detect-secrets
# scripts. It is also why they now REFUSE an unknown flag: run as
# `--apply --baseline /tmp/copy.json` on a checkout that predated the flag,
# the old `set(sys.argv[1:])` parse dropped flag and path on the floor and
# rewrote the real `.secrets.baseline` — 616 lines deleted, caught only by
# `git status`.


def _run_script(name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / name), *args],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize(
    "script", ["detect_secrets_check_unaudited.py"]
)
def test_an_unknown_flag_is_refused_not_absorbed(script):
    r = _run_script(script, "--definitely-not-a-flag")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "definitely-not-a-flag" in (r.stdout + r.stderr)


# ── council findings, each one a case that used to pass ─────────────────────


def test_innocence_unsetenv_is_a_strip_not_an_assignment():
    """codex-gpt-5.6-sol: "unsetenv" ends in "setenv".

    Without a word boundary the #40b alternative flagged the defensive STRIP
    the workflow's own comment calls allowed. Latent in CI, immediate here.
    """
    strip = "os." + "unsetenv" + '("ANTHROPIC_API_KEY")'
    assert cbp.grep_predicates([("app.py", 1, strip)]) == []


def test_guilt_a_real_setenv_assignment_still_fires():
    call = "os." + "setenv" + '("ANTHROPIC_API_KEY", "v")'
    assert cbp.grep_predicates([("app.py", 1, call)]) != []


def test_exclusion_reads_the_whole_output_line_exactly_as_ci_does():
    """kimi-code/k3: catE pipes `path:lineno:content` into `grep -vE`.

    A path-only copy is STRICTER than the workflow — it refuses a line CI
    lets through — and that is the direction that trains people to disable it.
    """
    line = "client = " + "Anthropic(" + "api_key" + "=contest_winner)"
    assert cbp.grep_predicates([("app.py", 1, line)]) == []
    plain = "client = " + "Anthropic(" + "api_key" + "=winner)"
    assert cbp.grep_predicates([("app.py", 1, plain)]) != []


def test_a_quoted_path_header_never_blames_the_previous_file(monkeypatch, capsys):
    """kimi-code/k3 and codex-gpt-5.6-sol, independently.

    git quotes any non-ASCII path, and the first parser only knew `+++ b/…`,
    so the quoted file's added lines were recorded under the PREVIOUS file in
    the diff. Hide one behind an excluded neighbour and it was never judged.
    `core.quotePath=false` removes that case; anything still unparseable must
    be COUNTED and announced, never attributed.
    """
    call = "client = " + "Anthropic(" + "api_key" + "=os.environ['X'])"
    diff = (
        "diff --git a/tests/a.py b/tests/a.py\n"
        "--- a/tests/a.py\n"
        "+++ b/tests/a.py\n"
        "@@ -0,0 +1 @@\n"
        "+x=1\n"
        'diff --git "a/caf\\\\303\\\\251.py" "b/caf\\\\303\\\\251.py"\n'
        '--- "a/caf\\\\303\\\\251.py"\n'
        '+++ "b/caf\\\\303\\\\251.py"\n'
        "@@ -0,0 +1 @@\n"
        f"+{call}\n"
    )
    monkeypatch.setattr(cbp, "_git", lambda *a: diff)
    entries, unattributed = cbp.staged_added_lines()
    assert unattributed == 1, entries
    assert all(path == "tests/a.py" for path, _, _ in entries)
    assert all(call not in text for _, _, text in entries)


def test_non_utf8_bytes_in_a_staged_file_do_not_crash_the_gate(tmp_path, monkeypatch, capsys):
    """kimi-code/k3: `text=True` decodes strictly.

    A Latin-1 byte anywhere in a staged file made `git diff` output
    undecodable, and the traceback came out of the pre-commit hook as a hard
    block on a commit with nothing wrong in it.
    """
    d = _repo(tmp_path)
    (d / "app.py").write_bytes(b'x = "\xe9"\n')
    subprocess.run(["git", "add", "app.py"], cwd=d, check=True, capture_output=True)
    monkeypatch.setattr(cbp, "REPO_ROOT", d)
    monkeypatch.setattr(cbp, "BASELINE", d / ".secrets.baseline")
    (d / ".secrets.baseline").write_text(_empty_baseline())
    old = sys.argv
    sys.argv = ["check_ban_predicates.py", "--staged"]
    try:
        rc = cbp.main()
    finally:
        sys.argv = old
    assert rc == 0, capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("detect-secrets") is None, reason="detect-secrets not installed"
)
def test_the_scanner_judges_the_staged_blob_not_the_working_tree(tmp_path, monkeypatch, capsys):
    """kimi-code/k3: after `git add -p` the unstaged remainder is still on disk.

    Scanning the file as it sits refuses a commit for content the commit does
    not carry — and contradicts this script's own stated scope.
    """
    d = _repo(tmp_path)
    (d / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=d, check=True, capture_output=True)
    # The offending line exists ONLY in the working tree, never in the index.
    (d / "app.py").write_text('x = 1\n' + "# " + '{"api_key": "opaque-value-here"}' + "\n")
    monkeypatch.setattr(cbp, "REPO_ROOT", d)
    baseline = d / ".secrets.baseline"
    baseline.write_text(_empty_baseline())
    monkeypatch.setattr(cbp, "BASELINE", baseline)
    old = sys.argv
    sys.argv = ["check_ban_predicates.py", "--staged"]
    try:
        rc = cbp.main()
    finally:
        sys.argv = old
    assert rc == 0, capsys.readouterr().out


def test_the_scanner_half_hands_back_numbers_and_never_a_string(tmp_path, monkeypatch):
    """The structural form of the CodeQL fix, asserted rather than trusted.

    Cutting the scanner's stdout out of the path was not enough: CodeQL kept
    calling this py/clear-text-logging-sensitive-data because any sentence
    built beside `BASELINE.read_text()` is downstream of that read. The cure
    is a type, not a filter — so this test guards the type. If someone
    reintroduces a message here, it fails before a reviewer has to notice.
    """
    d = _repo(tmp_path)
    (d / "note.py").write_text("# " + QUOTED_KEY + "\n")
    subprocess.run(["git", "add", "note.py"], cwd=d, check=True, capture_output=True)
    monkeypatch.setattr(cbp, "REPO_ROOT", d)
    baseline = d / ".secrets.baseline"
    baseline.write_text(_empty_baseline())
    monkeypatch.setattr(cbp, "BASELINE", baseline)

    located, skipped = cbp.detect_secrets_predicate(["note.py"], True)
    for item in located:
        assert isinstance(item, tuple) and len(item) == 2
        assert all(isinstance(v, int) for v in item), item
    if skipped is not None:
        code, detail = skipped
        assert code in cbp._SKIP_TEXT, code
        assert isinstance(detail, int)


def test_every_skip_code_has_a_message_and_formats_without_raising():
    for code, template in cbp._SKIP_TEXT.items():
        assert template.format(n=1, cap=cbp.SCAN_CAP)


# ── P3 and the baseline itself: excluded by PATH ENTITY ─────────────────────

_NEEDS_SCANNER = pytest.mark.skipif(
    shutil.which("detect-secrets") is None, reason="detect-secrets not installed"
)


def _stage_and_run(monkeypatch, d: Path, *argv: str) -> int:
    monkeypatch.setattr(cbp, "REPO_ROOT", d)
    baseline = d / ".secrets.baseline"
    if not baseline.exists():
        baseline.write_text(_empty_baseline())
    monkeypatch.setattr(cbp, "BASELINE", baseline)
    old = sys.argv
    sys.argv = ["check_ban_predicates.py", *argv]
    try:
        return cbp.main()
    finally:
        sys.argv = old


def test_the_baseline_predicate_is_an_exact_path_not_a_substring():
    assert cbp._is_the_baseline(".secrets.baseline")
    assert cbp._is_the_baseline("./.secrets.baseline")
    assert not cbp._is_the_baseline("docs/.secrets.baseline")
    assert not cbp._is_the_baseline("x.secrets.baseline")
    assert not cbp._is_the_baseline(".secrets.baseline.bak")
    assert not cbp._is_the_baseline("baseline.json")


@_NEEDS_SCANNER
def test_innocence_a_commit_touching_only_the_real_baseline_passes_enforced(
    tmp_path, monkeypatch, capsys
):
    """Today's baseline, staged alone, enforcement ON: the audit needs no bypass."""
    monkeypatch.delenv("BAN_PREDICATES_ENFORCEMENT", raising=False)
    d = _repo(tmp_path)
    (d / ".secrets.baseline").write_bytes((REPO_ROOT / ".secrets.baseline").read_bytes())
    subprocess.run(["git", "add", ".secrets.baseline"], cwd=d, check=True, capture_output=True)
    rc = _stage_and_run(monkeypatch, d, "--staged")
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "P3" not in out


@_NEEDS_SCANNER
def test_innocence_the_baseline_named_explicitly_passes_enforced(tmp_path, monkeypatch):
    monkeypatch.delenv("BAN_PREDICATES_ENFORCEMENT", raising=False)
    (tmp_path / ".secrets.baseline").write_bytes(
        (REPO_ROOT / ".secrets.baseline").read_bytes()
    )
    assert _run(monkeypatch, tmp_path, ".secrets.baseline") == 0


@_NEEDS_SCANNER
def test_guilt_a_tracked_file_that_is_not_the_baseline_is_still_refused(
    tmp_path, monkeypatch, capsys
):
    d = _repo(tmp_path)
    (d / "note.py").write_text("# the rule misses " + QUOTED_KEY + "\n")
    subprocess.run(["git", "add", "note.py"], cwd=d, check=True, capture_output=True)
    assert _stage_and_run(monkeypatch, d, "--staged") == 1
    assert "P3 detect-secrets" in capsys.readouterr().out


@_NEEDS_SCANNER
@pytest.mark.parametrize("name", ["docs/.secrets.baseline", "x.secrets.baseline"])
def test_guilt_a_namesake_of_the_baseline_is_still_refused(
    tmp_path, monkeypatch, capsys, name
):
    """Entity, not substring: only the repo-root ledger is excused."""
    d = _repo(tmp_path)
    f = d / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# the rule misses " + QUOTED_KEY + "\n")
    subprocess.run(["git", "add", name], cwd=d, check=True, capture_output=True)
    assert _stage_and_run(monkeypatch, d, "--staged") == 1
    assert "P3 detect-secrets" in capsys.readouterr().out


@_NEEDS_SCANNER
def test_guilt_the_baseline_does_not_excuse_a_file_staged_beside_it(
    tmp_path, monkeypatch, capsys
):
    d = _repo(tmp_path)
    (d / ".secrets.baseline").write_bytes((REPO_ROOT / ".secrets.baseline").read_bytes())
    (d / "note.py").write_text("# the rule misses " + QUOTED_KEY + "\n")
    subprocess.run(
        ["git", "add", ".secrets.baseline", "note.py"], cwd=d, check=True, capture_output=True
    )
    assert _stage_and_run(monkeypatch, d, "--staged") == 1
    out = capsys.readouterr().out
    assert "note.py" in out and ".secrets.baseline:" not in out
