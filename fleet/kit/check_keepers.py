#!/usr/bin/env python3
"""Single-writer gate for a fleet machine (run it next to resolve_sectors.py --diff, before every commit and push).

Exit 1 if the change touches a keeper file the caller does not hold, or any frozen path. Published-interface files
are listed for an additive-only review. Fails closed: a missing or unreadable keepers.json, or a git error, exits 2.

  python3 check_keepers.py --role none|keeper_B|keeper_F --diff origin/main...HEAD
  python3 check_keepers.py --role none --staged          # what is staged for the next commit
"""
import argparse, fnmatch, json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=".")
ap.add_argument("--keepers", default=os.path.join(HERE, "keepers.json"))
ap.add_argument("--role", default="none", choices=["none", "keeper_B", "keeper_F", "any"], help="any: only frozen paths are refused (integration rehearsal of several branches)")
g = ap.add_mutually_exclusive_group(required=True)
g.add_argument("--diff", help="a git range, e.g. origin/main...HEAD")
g.add_argument("--staged", action="store_true", help="check the staged changes")
a = ap.parse_args()
try:
    K = json.load(open(a.keepers))
    for key in ("keeper_B", "keeper_F", "frozen", "published_interface"):
        assert isinstance(K[key], list)
except Exception as e:  # noqa: BLE001 - any problem with the list must stop the commit
    print(f"KEEPERS: cannot read {a.keepers} ({e}); refusing to pass (fail closed)")
    sys.exit(2)
cmd = ["git", "-C", a.repo, "diff", "--name-only", "--no-renames"] + (["--cached"] if a.staged else [a.diff])
try:
    files = [f for f in subprocess.check_output(cmd, text=True).split("\n") if f]
except subprocess.CalledProcessError as e:
    print(f"KEEPERS: git diff failed ({e}); refusing to pass (fail closed)")
    sys.exit(2)
m = lambda f, globs: any(fnmatch.fnmatchcase(f, x) for x in globs)
bad = []
for f in files:
    if m(f, K["frozen"]):
        bad.append(("frozen for the campaign (owner decision)", f))
    elif a.role == "any":
        continue
    elif m(f, K["keeper_B"]) and a.role != "keeper_B":
        bad.append(("single writer: Keeper-B", f))
    elif m(f, K["keeper_F"]) and a.role != "keeper_F":
        bad.append(("single writer: Keeper-F", f))
for why, f in bad:
    print(f"REFUSED ({why}): {f}  -> write a keeper request in your output instead")
for f in files:
    if m(f, K["published_interface"]):
        print(f"ADDITIVE-ONLY (published interface; run the importers' tests): {f}")
print(f"{len(files)} files checked, {len(bad)} refused")
sys.exit(1 if bad else 0)
