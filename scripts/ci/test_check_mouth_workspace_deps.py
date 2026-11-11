"""check_mouth_workspace_deps.py must go red on an undeclared workspace import (guilt) and stay green otherwise (innocence)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check_mouth_workspace_deps as cmw  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def make_repo(tmp_path, mouth_manifest, files):
    (tmp_path / "package.json").write_text(json.dumps({"workspaces": ["apps/mouth", "packages/core"]}))
    core = tmp_path / "packages" / "core"
    core.mkdir(parents=True)
    (core / "package.json").write_text(json.dumps({"name": "@balizero/core", "version": "0.1.0"}))
    mouth = tmp_path / "apps" / "mouth"
    mouth.mkdir(parents=True)
    (mouth / "package.json").write_text(json.dumps({"name": "mouth", **mouth_manifest}))
    for rel, text in files.items():
        path = mouth / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


IMPORTS_CORE = {"src/a.tsx": 'import { Button } from "@balizero/core/components/button";\n'}


def test_guilt_undeclared_workspace_import(tmp_path):
    root = make_repo(tmp_path, {"dependencies": {"next": "16"}}, IMPORTS_CORE)
    assert cmw.check(root) == {"@balizero/core": ["apps/mouth/src/a.tsx"]}
    assert cmw.main([str(root)]) == 1


def test_guilt_dynamic_import_and_require(tmp_path):
    files = {
        "src/lazy.ts": 'const m = await import("@balizero/core");\n',
        "next.config.js": "const c = require('@balizero/core/tailwind');\n",
    }
    root = make_repo(tmp_path, {}, files)
    assert sorted(cmw.check(root)["@balizero/core"]) == ["apps/mouth/next.config.js", "apps/mouth/src/lazy.ts"]


def test_innocence_declared_in_dependencies_or_dev(tmp_path):
    root = make_repo(tmp_path, {"dependencies": {"@balizero/core": "*"}}, IMPORTS_CORE)
    assert cmw.check(root) == {}
    assert cmw.main([str(root)]) == 0
    other = tmp_path / "dev"
    other.mkdir()
    root = make_repo(other, {"devDependencies": {"@balizero/core": "*"}}, IMPORTS_CORE)
    assert cmw.check(root) == {}


def test_innocence_registry_packages_relative_paths_and_build_output(tmp_path):
    files = {
        "src/b.tsx": 'import * as D from "@radix-ui/react-dialog";\nimport x from "./x";\nimport "react";\n',
        "node_modules/dep/index.js": 'require("@balizero/core");\n',
        ".next/server/page.js": 'require("@balizero/core");\n',
        "src/notes.md": 'import "@balizero/core"\n',
    }
    root = make_repo(tmp_path, {}, files)
    assert cmw.check(root) == {}


def test_the_real_tree_declares_what_it_imports():
    # The guard on this repository itself: removing @balizero/core from apps/mouth/package.json
    # (or importing a new workspace without declaring it) fails here.
    assert cmw.check(REPO) == {}
    assert cmw.package_name("@balizero/core/components/x") == "@balizero/core"
    assert "@balizero/core" in cmw.workspace_packages(REPO)
