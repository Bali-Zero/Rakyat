#!/usr/bin/env python3
"""Resolve every tracked file of the repo to exactly one sector of sectors.json.

Usage:
  python3 resolve_sectors.py --repo . --check                      # exit 1 if any tracked file has no owner
  python3 resolve_sectors.py --repo . --sector be-crm --out f.txt  # write "LOC<TAB>path" for one sector
  python3 resolve_sectors.py --repo . --owner path/to/file.py      # print the owner of one path
  python3 resolve_sectors.py --repo . --diff origin/main...HEAD    # owners of the files a diff touches

Resolution order (first match wins):
  1. backend service package rule   apps/backend-rag/backend/services/<pkg>/
  2. backend test mirror rule       .../tests/services/<pkg>/ and .../tests/unit/services/<pkg>/
  3. backend router rule            .../app/routers/<name>.py matched against each sector's router_regex,
                                    and router test files matched by the longest router-name prefix
  4. most specific path rule: an exact file, a directory prefix ending in "/", or a glob with "*"
     (fnmatch, where "*" also crosses "/"); longer literal text wins
"""
from __future__ import annotations

import argparse
import collections
import fnmatch
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_EXT = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sql", ".sh"}


def load(cfg_path: str):
    cfg = json.load(open(cfg_path))
    be = cfg["backend_root"]
    sectors = cfg["sectors"]
    pkg_owner: dict[str, str] = {}
    for s in sectors:
        for p in s.get("service_pkgs", []):
            if p in pkg_owner:
                raise SystemExit(f"service package {p!r} is claimed by two sectors")
            pkg_owner[p] = s["id"]
    catch_all = cfg.get("router_default", "be-platform")
    router_rules = [(s["id"], re.compile(s["router_regex"])) for s in sectors if s.get("router_regex") and s["id"] != catch_all]
    # Specificity: an exact file or directory prefix counts its length; a glob counts the literal text before its first '*'
    # plus a half, so 'lib/kbli-*' beats 'lib/' but loses to a longer literal prefix.
    def specificity(p):
        return len(p.split('*', 1)[0]) + 0.5 if '*' in p else len(p)
    prefix_rules = sorted(((p, s["id"]) for s in sectors for p in s.get("prefixes", [])), key=lambda x: -specificity(x[0]))
    return be, sectors, pkg_owner, router_rules, prefix_rules


def make_owner(be, pkg_owner, router_rules, prefix_rules):
    def router_owner(name: str):
        for sid, rx in router_rules:
            if rx.match(name):
                return sid
        return None

    def router_owner_fuzzy(stem: str):
        parts = stem.split("_")
        for n in range(len(parts), 0, -1):
            o = router_owner("_".join(parts[:n]))
            if o:
                return o
        return None

    def owner(path: str):
        for root in ("services/", "tests/services/", "tests/unit/services/"):
            full = be + root
            if path.startswith(full):
                rest = path[len(full):]
                if "/" in rest and rest.split("/", 1)[0] in pkg_owner:
                    return pkg_owner[rest.split("/", 1)[0]]
        if path.startswith(be + "app/routers/") and path.endswith(".py"):
            o = router_owner(os.path.basename(path)[:-3])
            if o:
                return o
        for tp in ("tests/routers/", "tests/unit/routers/", "tests/unit/app/routers/", "tests/app/routers/", "tests/api/"):
            if path.startswith(be + tp):
                stem = os.path.basename(path)
                if stem.startswith("test_") and stem.endswith(".py"):
                    o = router_owner_fuzzy(stem[5:-3])
                    if o:
                        return o
        for p, sid in prefix_rules:
            if '*' in p:
                if fnmatch.fnmatchcase(path, p):
                    return sid
            elif path == p or (p.endswith("/") and path.startswith(p)):
                return sid
        return None

    return owner


def tracked(repo: str) -> list[str]:
    out = subprocess.run(["git", "-C", repo, "ls-files", "-z"], capture_output=True, check=True).stdout
    return [f for f in out.decode(errors="surrogateescape").split("\0") if f]


def loc(repo: str, path: str) -> int:
    if os.path.splitext(path)[1] not in CODE_EXT:
        return 0
    try:
        with open(os.path.join(repo, path), "rb") as fh:
            return fh.read().count(b"\n")
    except OSError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--config", default=os.path.join(HERE, "sectors.json"))
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sector")
    ap.add_argument("--out")
    ap.add_argument("--owner")
    ap.add_argument("--diff", help="a git revision range; prints the owner of every file it touches")
    a = ap.parse_args()

    be, sectors, pkg_owner, router_rules, prefix_rules = load(a.config)
    owner = make_owner(be, pkg_owner, router_rules, prefix_rules)

    if a.owner:
        print(owner(a.owner) or "UNOWNED")
        return 0

    if a.diff:
        names = subprocess.run(["git", "-C", a.repo, "diff", "--name-only", a.diff], capture_output=True, text=True, check=True).stdout.split("\n")
        by = collections.defaultdict(list)
        for n in filter(None, names):
            by[owner(n) or "UNOWNED"].append(n)
        for sid in sorted(by):
            print(f"{sid}\t{len(by[sid])}")
            for n in by[sid]:
                print(f"\t{n}")
        return 0

    files = tracked(a.repo)
    by: dict[str, list[str]] = collections.defaultdict(list)
    unowned = []
    for f in files:
        o = owner(f)
        (by[o] if o else unowned).append(f)

    if a.sector:
        rows = sorted(((loc(a.repo, p), p) for p in by.get(a.sector, [])), reverse=True)
        lines = [f"# sector {a.sector}: {len(rows)} tracked files, {sum(n for n, _ in rows)} code lines", "# LOC<TAB>path (LOC is 0 for data and binary files)"]
        lines += [f"{n}\t{p}" for n, p in rows]
        text = "\n".join(lines) + "\n"
        if a.out:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            open(a.out, "w").write(text)
            print(f"{a.sector}: {len(rows)} files -> {a.out}")
        else:
            sys.stdout.write(text)
        return 0

    for s in sectors:
        print(f"{s['id']:24s} {len(by.get(s['id'], [])):6d} files")
    print(f"{'TOTAL':24s} {len(files):6d} files, unowned: {len(unowned)}")
    for u in unowned[:50]:
        print("UNOWNED", u)
    return 1 if (a.check and unowned) else 0


if __name__ == "__main__":
    raise SystemExit(main())
