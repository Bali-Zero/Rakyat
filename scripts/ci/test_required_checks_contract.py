"""The required-checks contract (P4 spec rev 3 items 2-5, the aggregator and scanner rules; rev 6 g1-g3 and item 9),
asserted on the real workflows and on a guilt/innocence corpus so the checker itself is proven able to go red.

For every name in .github/required-checks.yml: exactly one job reports it; its workflow triggers on pull_request
(opened/synchronize/reopened, branches [main]) and never on pull_request_target; it checks out with actions/checkout and
no ref/repository override and runs no git fetch/checkout/switch; it has no job-level `if:` other than always(); it runs
on a literal GitHub-hosted label. No workflow with a pull_request trigger references any secret. A required job that
needs other jobs runs `if: always()` and has an assert step that fails unless each needed result is exactly 'success'
(executed, not grepped). No step-level `if:` in a required job can skip a test/guard step on changed paths: a condition
may not read `needs.*`, nor the outputs of a step that inspects the diff (cache/setup/upload/report steps are exempt). The
Repo-wide scanners job names >= 1 existing test path.
Rev 6, every job of a pull_request workflow: g1 a job name is a literal or only `matrix.<axis>` expressions; g2 the matrix is
static (no expression in `strategy`, no include/exclude); g3 no job-level `uses:`. Item 9: no tracked path carries a newline
or a path component with edge whitespace."""
import itertools
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PR_TYPES = {"opened", "synchronize", "reopened"}
HOSTED = re.compile(r"(ubuntu|macos|windows)-[A-Za-z0-9.\-]+")
ALWAYS = {"always()", "${{ always() }}"}
SECRET_REF = re.compile(r"(?<![\w.\-])secrets(?![\w\-])", re.I)
GIT_REF_CMD = re.compile(r"\bgit\s+(fetch|checkout|switch)\b")
# A needed job the assert step may leave unchecked, with the reason. `changes` is the path classifier: it is skipped on
# push by design, and every reader of its outputs defaults to "run everything".
NEEDS_EXEMPT = {"changes": "classifier, skipped on push; consumers default its outputs to run-all"}
SCANNERS = "Repo-wide scanners"
MATRIX_EXPR = re.compile(r"\s*matrix\.[A-Za-z0-9_-]+\s*")
# Steps whose `if:` may depend on anything: they neither test nor guard (cache, toolchain setup, artifacts, reports).
NON_TEST_USES = re.compile(r"^actions/(cache|setup-[a-z]+|upload-artifact|download-artifact)(/[a-z]+)?@")
NON_TEST_NAME = re.compile(r"\b(cache|upload|report|summary)\b", re.I)
# What a path sentinel looks like from the inside: it reads the diff or the changed-file list.
DIFF_PROBE = re.compile(r"git\s+diff|--name-only|merge-base|hotzone_changed_files|BASE_SHA|base\.sha|base_ref|changed[_ -]?files?", re.I)


def _on(doc):
    return doc.get("on", doc.get(True)) or {}


def check_names(job_id, job):
    """The check-run names GitHub gives a job: its name, plus "(v1, v2)" per matrix leg when the name has no expression."""
    name = str(job.get("name", job_id))
    matrix = (job.get("strategy") or {}).get("matrix")
    if not matrix or "${{" in name or not isinstance(matrix, dict):
        return [name]
    axes = {k: v for k, v in matrix.items() if isinstance(v, list)}
    if not axes:
        return [name]
    legs = [dict(zip(axes, combo)) for combo in itertools.product(*axes.values())]
    fmt = lambda v: str(v).lower() if isinstance(v, bool) else str(v)
    return [f"{name} ({', '.join(fmt(v) for v in leg.values())})" for leg in legs] or [name]


def _secret_refs(node, key=None):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _secret_refs(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _secret_refs(v, key)
    elif isinstance(node, str):
        exprs = re.findall(r"\$\{\{(.*?)\}\}", node, flags=re.S)
        if key == "if" and "${{" not in node:
            exprs.append(node)
        for e in exprs:
            if SECRET_REF.search(e):
                yield e.strip()


def _assert_step_verdicts(job):
    """Run the job's assert step with every needed result at 'success', then each one at skipped/cancelled/failure."""
    needed = [n for n in ([job["needs"]] if isinstance(job["needs"], str) else job["needs"]) if n not in NEEDS_EXEMPT]
    for step in job.get("steps", []):
        env = {k: str(v) for k, v in (step.get("env") or {}).items()}
        bound = {n: k for n in needed for k, v in env.items() if re.fullmatch(rf"\$\{{\{{\s*needs\.{re.escape(n)}\.result\s*\}}\}}", v)}
        if not bound or "run" not in step:
            continue
        if set(bound) != set(needed):
            return [f"assert step binds {sorted(bound)} but the job needs {sorted(needed)}"]

        def rc(results):
            e = {**os.environ, **{bound[n]: results[n] for n in needed}, "GITHUB_OUTPUT": os.devnull, "GITHUB_STEP_SUMMARY": os.devnull}
            return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], env=e, capture_output=True).returncode

        ok = {n: "success" for n in needed}
        bad = [] if rc(ok) == 0 else ["assert step fails with every needed job at 'success'"]
        for n in needed:
            for verdict in ("skipped", "cancelled", "failure"):
                if rc({**ok, n: verdict}) == 0:
                    bad.append(f"assert step stays green with {n}={verdict}")
        return bad
    return [f"no step binds needs.<job>.result for {needed}"]


def _job_rules(job):
    """Rev 6 g1-g3 on one job of a pull_request workflow."""
    why = []
    name = job.get("name")
    if isinstance(name, str) and "${{" in name:
        exprs = re.findall(r"\$\{\{(.*?)\}\}", name, flags=re.S)
        if name.count("${{") != len(exprs) or not all(MATRIX_EXPR.fullmatch(e) for e in exprs):
            why.append(f"g1: job name carries a non-matrix expression ({name!r})")
    strategy = job.get("strategy")
    if strategy is not None:
        if "${{" in yaml.safe_dump(strategy):
            why.append("g2: strategy carries an expression")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        if matrix is not None and not isinstance(matrix, dict):
            why.append("g2: strategy.matrix is not a static mapping")
        elif isinstance(matrix, dict) and {"include", "exclude"} & {str(k).lower() for k in matrix}:
            why.append("g2: strategy.matrix uses include/exclude")
    if "uses" in job:
        why.append("g3: job-level uses (reusable-workflow call)")
    return why


def violations(workflows, required, root):
    """workflows: {filename: (raw text, parsed doc)}; required: list of context names. Returns human-readable violations."""
    bad = []
    for fname, (text, doc) in workflows.items():
        if "pull_request" in _on(doc) or "pull_request_target" in _on(doc):
            for ref in _secret_refs(doc):
                bad.append(f"{fname}: pull_request workflow references a secret ({ref})")
            for job_id, job in (doc.get("jobs") or {}).items():
                bad += [f"{fname}:{job_id}: {why}" for why in _job_rules(job)]
    reporters = {}
    for fname, (text, doc) in workflows.items():
        for job_id, job in (doc.get("jobs") or {}).items():
            for n in check_names(job_id, job):
                reporters.setdefault(n, []).append((fname, job_id, job, text, doc))
    for name in required:
        hits = reporters.get(name, [])
        if len(hits) != 1:
            bad.append(f"{name!r}: reported by {len(hits)} jobs {[f'{f}:{j}' for f, j, *_ in hits]}")
            continue
        fname, job_id, job, text, doc = hits[0]
        where = f"{name!r} ({fname}:{job_id})"
        on = _on(doc)
        pr = on.get("pull_request") if isinstance(on, dict) else None
        if "pull_request_target" in text:
            bad.append(f"{where}: the workflow mentions pull_request_target")
        if not isinstance(pr, dict) or set(pr.get("types") or []) != PR_TYPES or pr.get("branches") != ["main"]:
            bad.append(f"{where}: trigger must be pull_request types {sorted(PR_TYPES)} branches [main], got {pr!r}")
        if "if" in job and str(job["if"]).strip() not in ALWAYS:
            bad.append(f"{where}: job-level if can skip it: {' '.join(str(job['if']).split())[:120]}")
        labels = job.get("runs-on") if isinstance(job.get("runs-on"), list) else [job.get("runs-on")]
        if not all(isinstance(x, str) and HOSTED.fullmatch(x) for x in labels):
            bad.append(f"{where}: runs-on {job.get('runs-on')!r} is not a literal GitHub-hosted label")
        steps = job.get("steps") or []
        checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
        if not checkouts:
            bad.append(f"{where}: no actions/checkout step")
        for s in checkouts:
            if {"ref", "repository"} & set(s.get("with") or {}):
                bad.append(f"{where}: checkout overrides ref/repository")
        for s in steps:
            if GIT_REF_CMD.search(str(s.get("run", ""))):
                bad.append(f"{where}: step {s.get('name', '?')!r} runs git fetch/checkout/switch")
        by_id = {s.get("id"): s for s in steps if s.get("id")}
        for s in steps:
            if "if" not in s or NON_TEST_USES.match(str(s.get("uses", ""))) or NON_TEST_NAME.search(str(s.get("name", ""))):
                continue
            cond = str(s["if"])
            label = s.get("name", s.get("uses", "?"))
            if re.search(r"\bneeds\.", cond):
                bad.append(f"{where}: step {label!r} is conditioned on another job's outputs ({' '.join(cond.split())[:80]})")
            for sid in re.findall(r"\bsteps\.([A-Za-z0-9_-]+)\.outputs\b", cond):
                src = by_id.get(sid)
                if src is None or DIFF_PROBE.search(str(src.get("run", ""))):
                    bad.append(f"{where}: step {label!r} can be skipped by a diff-reading step ({sid})")
        if job.get("needs"):
            needed = [n for n in ([job["needs"]] if isinstance(job["needs"], str) else job["needs"]) if n not in NEEDS_EXEMPT]
            if needed:
                if str(job.get("if", "")).strip() not in ALWAYS:
                    bad.append(f"{where}: aggregator over {needed} without `if: always()` is skipped when one fails")
                bad += [f"{where}: {v}" for v in _assert_step_verdicts(job)]
        if name == SCANNERS:
            paths = []
            for s in steps:
                wd = root / s.get("working-directory", ".")
                for arg in re.findall(r"[\"']([^\"']+\.py(?:::[^\"']*)?)[\"']", str(s.get("run", ""))):
                    paths.append((wd / arg.split("::", 1)[0]).is_file())
            if not paths or not all(paths):
                bad.append(f"{where}: must name >= 1 test path and every one must exist (found {paths})")
    return bad


def load_tree(root=REPO):
    wf = {p.name: (p.read_text(), yaml.safe_load(p.read_text())) for p in sorted((root / ".github/workflows").glob("*.y*ml"))}
    req = yaml.safe_load((root / ".github/required-checks.yml").read_text())
    return wf, req


def bad_paths(paths):
    """Item 9: paths with a newline/CR or a component with leading/trailing whitespace."""
    return [p for p in paths if "\n" in p or "\r" in p or any(c != c.strip() for c in p.split("/"))]


def tree_paths(root=REPO):
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        return [p for p in os.fsdecode(out).split("\0") if p]
    return [(Path(d) / f).relative_to(root).as_posix() for d, _, fs in os.walk(root) for f in fs]


def test_the_required_checks_file_is_exactly_ten_unique_actions_contexts():
    _, req = load_tree()
    names = [c["name"] for c in req["contexts"]]
    assert len(names) == 10 and len(set(names)) == 10, names
    assert all(c.get("required_app_id") == 15368 for c in req["contexts"])


def test_the_real_workflows_meet_the_contract():
    wf, req = load_tree()
    assert violations(wf, [c["name"] for c in req["contexts"]], REPO) == []


def test_no_tree_path_carries_a_newline_or_edge_whitespace():
    assert bad_paths(tree_paths()) == []


def test_bad_paths_guilt_and_innocence():
    guilty = ["a/b\nc.py", "a/b\rc.py", " lead/x.py", "dir /x.py", "x/trail.py "]
    assert bad_paths(guilty) == guilty
    assert bad_paths(["a/b c.py", "a/b.py"]) == []


# ---- guilt / innocence: the checker must go red on each breach and stay green on the valid shape ----
GOOD_ON = {"pull_request": {"types": sorted(PR_TYPES), "branches": ["main"]}, "push": {"branches": ["main"]}}
CHECKOUT = {"uses": "actions/checkout@v7"}
FANIN_OK = {"name": "Assert", "env": {"A": "${{ needs.a.result }}"}, "run": '[ "$A" = "success" ] || exit 1'}
FANIN_FAILURE_ONLY = {"name": "Assert", "env": {"A": "${{ needs.a.result }}"}, "run": 'if [ "$A" = "failure" ]; then exit 1; fi'}


def _wf(jobs, on=None, extra=""):
    doc = {"name": "w", "on": on or GOOD_ON, "jobs": jobs}
    return {"w.yml": (yaml.safe_dump(doc) + extra, doc)}


def _job(**kw):
    j = {"name": "C", "runs-on": "ubuntu-latest", "steps": [CHECKOUT, {"run": "true"}]}
    j.update(kw)
    return j


def test_innocence_minimal_valid_job(tmp_path):
    assert violations(_wf({"c": _job()}), ["C"], tmp_path) == []


def test_innocence_aggregator_with_exact_success_assert(tmp_path):
    jobs = {"a": _job(name="A"), "c": _job(needs=["a", "changes"], **{"if": "always()"}, steps=[CHECKOUT, FANIN_OK])}
    assert violations(_wf(jobs), ["C"], tmp_path) == []


GLOBAL_RULE = {"name_expr": "g1", "matrix_include": "g2", "matrix_exclude": "g2", "matrix_expr": "g2", "matrix_list": "g2",
               "strategy_expr": "g2", "job_uses": "g3"}


@pytest.mark.parametrize("case", [
    "pull_request_target", "no_branches", "skip_if", "ref_override", "secret", "self_hosted", "git_fetch", "duplicate",
    "missing", "fanin_failure_only", "fanin_no_always", "no_checkout", "step_sentinel", "step_needs_output",
    "name_expr", "matrix_include", "matrix_exclude", "matrix_expr", "matrix_list", "strategy_expr", "job_uses",
])
def test_guilt(case, tmp_path):
    job, on, extra, names = _job(), None, "", ["C"]
    jobs = {"c": job}
    if case == "pull_request_target":
        on = {**GOOD_ON, "pull_request_target": None}
    elif case == "no_branches":
        on = {"pull_request": {"types": sorted(PR_TYPES)}}
    elif case == "skip_if":
        job["if"] = "needs.changes.outputs.run != 'false'"
    elif case == "ref_override":
        job["steps"] = [{"uses": "actions/checkout@v7", "with": {"ref": "${{ github.event.pull_request.head.sha }}"}}]
    elif case == "secret":
        job["steps"] = [CHECKOUT, {"run": "echo ${{ secrets.GITHUB_TOKEN }}"}]
    elif case == "self_hosted":
        job["runs-on"] = ["self-hosted", "zero"]
    elif case == "git_fetch":
        job["steps"] = [CHECKOUT, {"run": "git fetch origin main && pytest"}]
    elif case == "duplicate":
        jobs["d"] = _job()
    elif case == "missing":
        names = ["Nope"]
    elif case == "fanin_failure_only":
        jobs = {"a": _job(name="A"), "c": _job(needs=["a"], **{"if": "always()"}, steps=[CHECKOUT, FANIN_FAILURE_ONLY])}
    elif case == "fanin_no_always":
        jobs = {"a": _job(name="A"), "c": _job(needs=["a"], steps=[CHECKOUT, FANIN_OK])}
    elif case == "no_checkout":
        job["steps"] = [{"run": "true"}]
    elif case == "step_sentinel":
        job["steps"] = [CHECKOUT,
                        {"name": "Did a guarded surface change?", "id": "relevant",
                         "run": 'git diff --name-only "$BASE_SHA"...HEAD > /tmp/c.txt; echo "run=$(grep -q x /tmp/c.txt && echo true || echo false)" >> "$GITHUB_OUTPUT"'},
                        {"name": "The guard", "if": "steps.relevant.outputs.run == 'true'", "run": "pytest guard.py"}]
    elif case == "step_needs_output":
        job["steps"] = [CHECKOUT, {"name": "Tests", "if": "needs.changes.outputs.run_tests != 'false'", "run": "pytest"}]
    elif case == "name_expr":
        jobs["x"] = _job(name="X ${{ github.ref }}")
    elif case == "matrix_include":
        jobs["x"] = _job(name="X", strategy={"matrix": {"include": [{"a": 1}]}})
    elif case == "matrix_exclude":
        jobs["x"] = _job(name="X", strategy={"matrix": {"k": ["v"], "exclude": [{"k": "v"}]}})
    elif case == "matrix_expr":
        jobs["x"] = _job(name="X", strategy={"matrix": "${{ fromJSON(needs.a.outputs.m) }}"})
    elif case == "matrix_list":
        jobs["x"] = _job(name="X", strategy={"matrix": [{"k": "v"}]})
    elif case == "strategy_expr":
        jobs["x"] = _job(name="X", strategy={"matrix": {"k": ["v"]}, "max-parallel": "${{ inputs.n }}"})
    elif case == "job_uses":
        jobs["x"] = {"uses": "org/repo/.github/workflows/x.yml@main"}
    found = violations(_wf(jobs, on, extra), names, tmp_path)
    assert found, case
    if case in GLOBAL_RULE:
        assert any(f"w.yml:x: {GLOBAL_RULE[case]}:" in v for v in found), (case, found)


def test_innocence_matrix_expression_in_job_name(tmp_path):
    jobs = {"c": _job(), "x": _job(name="X (${{ matrix.k }})", strategy={"matrix": {"k": ["a", "b"]}})}
    assert violations(_wf(jobs), ["C"], tmp_path) == []


def test_innocence_constant_decide_step_and_cache_steps(tmp_path):
    job = _job(steps=[CHECKOUT, {"name": "Decide whether to run", "id": "decide", "run": 'echo "run=true" >> "$GITHUB_OUTPUT"'},
                      {"name": "Cache dependencies", "id": "c", "uses": "actions/cache/restore@v6", "if": "steps.decide.outputs.run == 'true'"},
                      {"name": "Run tests", "if": "steps.decide.outputs.run == 'true' && matrix.coverage", "run": "npm test"},
                      {"name": "Retry once", "if": "steps.c.outputs.cache-hit != 'true'", "run": "npm test"}])
    assert violations(_wf({"c": job}), ["C"], tmp_path) == []


def test_guilt_scanners_job_naming_no_existing_test(tmp_path):
    job = _job(name=SCANNERS, steps=[CHECKOUT, {"run": 'pytest "backend/tests/test_gone.py::t"', "working-directory": "apps/x"}])
    assert violations(_wf({"s": job}), [SCANNERS], tmp_path)
    (tmp_path / "apps/x/backend/tests").mkdir(parents=True)
    (tmp_path / "apps/x/backend/tests/test_gone.py").write_text("def t(): pass\n")
    assert violations(_wf({"s": job}), [SCANNERS], tmp_path) == []


def test_matrix_names_match_what_github_reports():
    assert check_names("f", {"name": "Frontend Tests (Next.js)", "strategy": {"matrix": {"app": ["mouth"], "coverage": [True]}}}) == [
        "Frontend Tests (Next.js) (mouth, true)"]
    assert check_names("c", {"name": "CodeQL Analysis", "strategy": {"matrix": {"language": ["python", "javascript"]}}}) == [
        "CodeQL Analysis (python)", "CodeQL Analysis (javascript)"]
