#!/usr/bin/env bash
# Integration rehearsal, run by the editor before a PUBLISH window: are the candidate branches safe TOGETHER on
# the current origin/main? Nothing is pushed.
#   fleet/kit/integration-rehearsal.sh <branch>... [-- <pytest test ids: the guilt tests of those branches>]
# It builds a throwaway worktree on origin/main, merges every branch into it (stopping at the first conflict),
# then runs the deterministic checks: no frozen path, no duplicate migration number, ruff F on backend/app,
# the OpenAPI pin test, tsc, and the named guilt tests, all database work through kit/t. Each check also runs on
# origin/main first: a check already red there is reported as RED, and only a new red fails the rehearsal (exit 1).
# The backend virtualenv and node_modules are borrowed from the worktree you run it from (SRC_WT, default: here).
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_WT="${SRC_WT:-$(git rev-parse --show-toplevel)}"
branches=(); tests=()
while [ $# -gt 0 ]; do if [ "$1" = "--" ]; then shift; tests=("$@"); break; fi; branches+=("$1"); shift; done
[ ${#branches[@]} -gt 0 ] || { echo "usage: integration-rehearsal.sh <branch>... [-- <tests>]" >&2; exit 2; }
git fetch -q origin main || { echo "cannot fetch origin/main" >&2; exit 2; }
WT="$(mktemp -d "${TMPDIR:-/tmp}/rehearsal.XXXXXX")"; rmdir "$WT"
git worktree add -q --detach "$WT" origin/main || exit 2
trap 'cd /; git -C "$SRC_WT" worktree remove --force "$WT" >/dev/null 2>&1' EXIT
cd "$WT"
git config user.name >/dev/null || git config user.name "fleet rehearsal"; git config user.email >/dev/null || git config user.email "rehearsal@localhost"
fail=0; tally=()
[ -e "$SRC_WT/apps/backend-rag/.venv" ] && ln -sfn "$SRC_WT/apps/backend-rag/.venv" apps/backend-rag/.venv
[ -e "$SRC_WT/node_modules" ] && ln -sfn "$SRC_WT/node_modules" node_modules
PY=apps/backend-rag/.venv/bin/python
RUFF=""; if [ -x "$PY" ] && "$PY" -m ruff --version >/dev/null 2>&1; then RUFF="$PY -m ruff"; elif command -v ruff >/dev/null; then RUFF=ruff; fi
# name|command; each check runs on origin/main first and again on the merged tree: only a NEW red fails the rehearsal.
checks=("migration numbers unique|test -z \"\$(ls apps/backend-rag/backend/db/migrations_v2/ | grep -oE '^[0-9]+' | sort | uniq -d)\"")
[ -n "$RUFF" ] && checks+=("ruff F|$RUFF check --select F401,F821,F822,F823 apps/backend-rag/backend/app") || tally+=("SKIP  ruff F (ruff not installed)")
[ -x "$PY" ] && checks+=("OpenAPI pin|$KIT/t 'cd apps/backend-rag && PYTHONPATH=.:../crm-cell .venv/bin/python -m pytest backend/tests/unit/scripts/test_generate_openapi_schema_pin.py -p no:cacheprovider --no-header -x'") || tally+=("SKIP  OpenAPI pin (no backend virtualenv in $SRC_WT)")
[ -x node_modules/.bin/tsc ] && checks+=("tsc|cd apps/mouth && ../../node_modules/.bin/tsc --noEmit") || tally+=("SKIP  tsc (no node_modules in $SRC_WT)")
if [ ${#tests[@]} -gt 0 ]; then
  [ -x "$PY" ] && checks+=("guilt tests (${#tests[@]})|$KIT/t 'cd apps/backend-rag && PYTHONPATH=.:../crm-cell .venv/bin/python -m pytest ${tests[*]} -p no:cacheprovider --no-header'") || tally+=("SKIP  guilt tests (no backend virtualenv)")
fi
basev=()   # bash 3.2 (macOS) has no associative arrays: basev[i] belongs to checks[i]
for i in "${!checks[@]}"; do c="${checks[$i]}"; (bash -c "${c#*|}" >/dev/null 2>&1) && basev[$i]=green || basev[$i]=red; done
merged=1
for b in "${branches[@]}"; do
  if git merge -q --no-ff --no-edit "$b" >/dev/null 2>&1; then tally+=("PASS  merge $b")
  else tally+=("FAIL  merge $b: conflict with origin/main or with an earlier branch in the list"); fail=1; merged=0; git merge --abort >/dev/null 2>&1; break; fi
done
if [ $merged = 1 ]; then
  python3 "$KIT/check_keepers.py" --repo . --role any --diff origin/main...HEAD >/tmp/rehearsal.keepers.$$ 2>&1; rc=$?
  case $rc in 0) tally+=("PASS  no frozen path");; 1) tally+=("FAIL  frozen path touched: $(grep REFUSED /tmp/rehearsal.keepers.$$ | head -3 | tr '\n' ' ')"); fail=1;;
              *) tally+=("FAIL  keeper gate could not run: $(tail -1 /tmp/rehearsal.keepers.$$)"); fail=1;; esac
  rm -f /tmp/rehearsal.keepers.$$
  for i in "${!checks[@]}"; do
    c="${checks[$i]}"; n="${c%%|*}"
    if (bash -c "${c#*|}" >/dev/null 2>&1); then tally+=("PASS  $n")
    elif [ "${basev[$i]}" = red ]; then tally+=("RED   $n (already red on origin/main: not caused by these branches)")
    else tally+=("FAIL  $n (green on origin/main, red with these branches)"); fail=1; fi
  done
fi
echo "Integration rehearsal on origin/main $(git rev-parse --short origin/main) with ${#branches[@]} branch(es):"
printf '  %s\n' "${tally[@]}"
[ $fail = 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL (do not open these pull requests together)"
exit $fail
