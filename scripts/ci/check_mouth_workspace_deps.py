"""apps/mouth must declare every workspace package it imports.

Vercel skips the build of a monorepo project that a commit "didn't affect", and it reads
"affect" from the package manager's workspace graph: a project depends on a workspace only if
its package.json names it. apps/mouth reached @balizero/core through a tsconfig `paths` entry
alone, so a commit touching only packages/core got its production build cancelled at creation
(Rakyat #202, 2026-11-11) before the ignoreCommand was even consulted. This check fails when an
import of a workspace package from apps/mouth has no matching entry in apps/mouth/package.json.

Exit 0 = every imported workspace is declared, 1 = at least one is missing.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CONSUMER = "apps/mouth"
CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
SKIP_DIRS = {"node_modules", ".next", "out", "coverage", ".vercel"}
SPECIFIER = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+)["']([^"'\s]+)["']"""
)


def package_name(specifier: str) -> str | None:
    if specifier.startswith("@"):
        parts = specifier.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else None
    if specifier.startswith((".", "/")):
        return None
    return specifier.split("/")[0]


def workspace_packages(root: Path) -> dict[str, str]:
    """Name -> directory of every workspace whose package.json exists."""
    entries = json.loads((root / "package.json").read_text()).get("workspaces") or []
    if isinstance(entries, dict):
        entries = entries.get("packages") or []
    found: dict[str, str] = {}
    for entry in entries:
        dirs = sorted(root.glob(entry)) if any(c in entry for c in "*?[") else [root / entry]
        for d in dirs:
            manifest = d / "package.json"
            if manifest.is_file():
                name = json.loads(manifest.read_text()).get("name")
                if name:
                    found[name] = d.relative_to(root).as_posix()
    return found


def code_files(base: Path):
    for path in base.rglob("*"):
        if path.suffix in CODE_SUFFIXES and path.is_file():
            if not SKIP_DIRS.intersection(path.relative_to(base).parts):
                yield path


def check(root: Path) -> dict[str, list[str]]:
    """Map each undeclared workspace package to the files of the consumer that import it."""
    workspaces = workspace_packages(root)
    base = root / CONSUMER
    manifest = json.loads((base / "package.json").read_text())
    declared = set(manifest.get("dependencies") or {}) | set(manifest.get("devDependencies") or {})
    own = manifest.get("name")
    missing: dict[str, list[str]] = {}
    for path in code_files(base):
        text = path.read_text(errors="replace")
        for specifier in SPECIFIER.findall(text):
            name = package_name(specifier)
            if name in workspaces and name != own and name not in declared:
                rel = path.relative_to(root).as_posix()
                if rel not in missing.setdefault(name, []):
                    missing[name].append(rel)
    return missing


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    root = Path(args[0]) if args else Path(__file__).resolve().parents[2]
    missing = check(root)
    if not missing:
        print(f"{CONSUMER}: every imported workspace package is declared in package.json")
        return 0
    for name, files in sorted(missing.items()):
        print(
            f"{CONSUMER} imports {name} ({len(files)} file(s), e.g. {files[0]}) "
            f"but {CONSUMER}/package.json does not declare it: add \"{name}\": \"*\" to dependencies"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
