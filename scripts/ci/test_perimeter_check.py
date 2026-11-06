"""perimeter_check.py must go red on a lying PERIMETER.txt (guilt) and stay green on an honest one (innocence)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import perimeter_check as pc  # noqa: E402

HEAD = "# PERIMETER\n\napps/mouth/\nscripts/a.sh\n\n"
MOVED = "# Moved to backstage: x\napps/mouth/t/gone_test.py\napps/mouth/t/kept_test.py::test_one\n\n"
OVERLAY = "# P2 changes: y\nscripts/ov.py\twhy\n\n# Added by P2 (z)\n.github/workflows/w.yml\n\n"
REFS = "# Known references from prod code to paths outside\napps/mouth/ref.py\tscripts/lib/x.py\tguarded\n"
FULL = HEAD + MOVED + OVERLAY + REFS
TREE = ["PERIMETER.txt", "apps/mouth/a.ts", "apps/mouth/t/kept_test.py", "scripts/a.sh", "scripts/ov.py",
        ".github/workflows/w.yml", "apps/mouth/ref.py"]


def run(text, files):
    d = Path(pytest_tmp)
    (d / "PERIMETER.txt").write_text(text)
    return pc.check(d, files)


def setup_module(_):
    global pytest_tmp
    import tempfile
    pytest_tmp = tempfile.mkdtemp()


def test_innocence_honest_perimeter_is_clean():
    r = run(FULL, TREE)
    assert r == {"uncovered": [], "dangling": [], "excluded": [], "rebuilt": []}


def test_guilt_unlisted_path_is_uncovered():
    assert run(FULL, TREE + ["infra/new/thing.json"])["uncovered"] == ["infra/new/thing.json"]


def test_guilt_prefix_sibling_is_not_covered_by_a_root():
    assert run(FULL, TREE + ["apps/mouth-evil/x"])["uncovered"] == ["apps/mouth-evil/x"]


def test_guilt_listed_path_missing_from_tree_is_dangling():
    assert run(FULL, [f for f in TREE if f != "scripts/a.sh"])["dangling"] == ["scripts/a.sh"]


def test_innocence_moved_whole_files_are_not_expected_in_the_tree():
    assert "apps/mouth/t/gone_test.py" not in run(FULL, TREE)["dangling"]


def test_guilt_moved_function_keeps_its_file_in_the_tree():
    assert run(FULL, [f for f in TREE if f != "apps/mouth/t/kept_test.py"])["dangling"] == ["apps/mouth/t/kept_test.py"]


def test_guilt_unnormalized_text_is_flagged():
    assert run(FULL.replace("scripts/a.sh", "scripts/a.sh   "), TREE)["rebuilt"] == ["PERIMETER.txt"]


def test_the_real_perimeter_of_this_repository_is_clean():
    root = Path(__file__).resolve().parents[2]
    if not (root / "PERIMETER.txt").is_file():
        return
    rep = pc.check(root)
    assert not any(rep.values()), rep


def test_cli_exit_codes(tmp_path):
    (tmp_path / "PERIMETER.txt").write_text(FULL)
    r = subprocess.run([sys.executable, str(Path(pc.__file__)), "--root", str(tmp_path)], capture_output=True)
    assert r.returncode == 2 or r.returncode == 1  # not a git repo: refuses, never passes


def test_guilt_file_declaration_does_not_cover_descendants():
    tree = [f for f in TREE if f != "scripts/a.sh"] + ["scripts/a.sh/unrelated.txt"]
    r = run(FULL, tree)
    assert r["uncovered"] == ["scripts/a.sh/unrelated.txt"] and r["dangling"] == ["scripts/a.sh"]


def test_guilt_directory_declaration_that_is_now_a_file_is_dangling():
    tree = [f for f in TREE if not f.startswith("apps/mouth/")] + ["apps/mouth"]
    assert "apps/mouth/" in run(FULL, tree)["dangling"]


def test_guilt_file_declared_moved_but_still_tracked_is_excluded():
    assert run(FULL, TREE + ["apps/mouth/t/gone_test.py"])["excluded"] == ["apps/mouth/t/gone_test.py"]


def test_innocence_moved_function_row_does_not_exclude_its_file():
    assert run(FULL, TREE)["excluded"] == []


def test_guilt_exclusion_is_exact_not_prefix():
    assert run(FULL, TREE + ["apps/mouth/t/gone_test.py.bak"])["excluded"] == []
