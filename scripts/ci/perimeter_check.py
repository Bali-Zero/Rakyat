#!/usr/bin/env python3
"""Check that PERIMETER.txt explains the tracked tree of this repository.

PERIMETER.txt is the authority for what lives on the Rakyat track (Builder Contract 5). The builder that writes it needs
the canonical monorepo and its manifests, so it cannot run here; this check enforces the part that can be measured from
this tree alone:

  uncovered  tracked paths that no PERIMETER entry explains (a path joined the tree without joining the perimeter)
  dangling   perimeter entries that are in the tree by claim (roots, overlays, additions) but absent from it
  excluded   paths declared "Moved to backstage" that are nevertheless tracked here
  rebuilt    the normalized form of the committed file differs from its regeneration (ordering, whitespace, duplicates)

Exit 0 clean, 1 findings, 2 usage. Prints paths and counts only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PERIMETER = "PERIMETER.txt"
SECTION_MOVED = "# Moved to backstage"
SECTION_OVERLAY = "# P2 changes"
SECTION_ADDED = "# Added by P2"
SECTION_REFS = "# Known references from prod code"
SECTION_ADDITIONS = "# A.7 additions"
# An entry ending in "/" is a directory and covers its descendants; any other entry is exactly one file.
# Files every repository carries by construction; they need no per-file perimeter entry.
IMPLICIT = frozenset({PERIMETER})


def parse(text: str) -> dict[str, list[str]]:
    """Group perimeter entries by section. 'tree' entries are claimed to exist here, 'moved' ones are claimed absent."""
    out: dict[str, list[str]] = {"roots": [], "moved": [], "overlay": [], "added": [], "refs": []}
    section = "roots"
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("#"):
            for marker, name in ((SECTION_MOVED, "moved"), (SECTION_OVERLAY, "overlay"), (SECTION_ADDED, "added"),
                                 (SECTION_REFS, "refs"), (SECTION_ADDITIONS, "roots")):
                if line.startswith(marker):
                    section = name
            continue
        path = line.split("\t", 1)[0].strip()
        out[section].append(path)
    return out


def tree_entries(parsed: dict[str, list[str]]) -> list[str]:
    """Entries that explain tracked paths: roots, overlays, additions, the referrer side of external refs, and the
    test functions moved out of a file that stays (path::test), which keep the file itself in the tree."""
    paths = [*parsed["roots"], *parsed["overlay"], *parsed["added"], *parsed["refs"]]
    paths += [m.split("::", 1)[0] for m in parsed["moved"] if "::" in m]
    return sorted(set(paths))


def matches(path: str, entry: str) -> bool:
    return path.startswith(entry) if entry.endswith("/") else path == entry


def covered(path: str, entries: list[str]) -> bool:
    return path in IMPLICIT or any(matches(path, e) for e in entries)


def excluded_entries(parsed: dict[str, list[str]]) -> list[str]:
    """Whole files declared moved to backstage. 'path::test' rows move one function and keep the file, so they are not here."""
    return sorted({m for m in parsed["moved"] if "::" not in m})


def tracked(root: Path) -> list[str]:
    p = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(2)
    return sorted(x for x in p.stdout.decode(errors="surrogateescape").split("\0") if x)


def normalize(text: str) -> str:
    """Deterministic rendering: rstrip every line, collapse blank runs, one trailing newline. Order is meaningful and kept."""
    lines, blank = [], False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            if not blank and lines:
                lines.append("")
            blank = True
            continue
        blank = False
        lines.append(line)
    return "\n".join(lines).rstrip("\n") + "\n"


def check(root: Path, files: list[str] | None = None) -> dict[str, list[str]]:
    text = (root / PERIMETER).read_text()
    parsed = parse(text)
    entries = tree_entries(parsed)
    files = tracked(root) if files is None else files
    present = set(files)
    uncovered = [f for f in files if not covered(f, entries)]
    dangling = [e for e in entries if not any(matches(f, e) for f in present)]
    excluded = [f for f in files if any(matches(f, x) for x in excluded_entries(parsed))]
    rebuilt = [] if normalize(text) == text else [PERIMETER]
    return {"uncovered": uncovered, "dangling": dangling, "excluded": excluded, "rebuilt": rebuilt}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".")
    ap.add_argument("--allow-dangling", action="store_true", help="report dangling entries without failing")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve()
    if not (root / PERIMETER).is_file():
        print(f"perimeter-check: {PERIMETER} missing in {root}")
        return 2
    rep = check(root)
    bad = False
    for key in ("uncovered", "dangling", "excluded", "rebuilt"):
        items = rep[key]
        print(f"{key}: {len(items)}")
        for it in items:
            print(f"  {it}")
        if items and not (key == "dangling" and a.allow_dangling):
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
