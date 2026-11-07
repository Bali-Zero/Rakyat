#!/usr/bin/env bash
# test_cate_plan_oauth_baseline.sh — guilt + innocence for catE-sovereignty-lint
# #40f, the plan-OAuth-in-product predicate with its per-FILE baseline
# (RULED 2026-11-01: backend lanes never fall back to plan OAuth; RULED
# 2026-11-02 consequence (c)).
#
# Same contract as test_cate_paid_budget.sh: the step's own shell is extracted
# between its CATE40F markers, dedented, and run inside synthetic trees — no
# network, nothing outside a temp dir. The guilty literals are ASSEMBLED at
# runtime so they exist only in the fixtures the step reads.
#
# Run:  bash scripts/tests/test_cate_plan_oauth_baseline.sh

set -u

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORKFLOW="$REPO_ROOT/.github/workflows/catE-sovereignty-lint.yml"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n' "$1"; }

BLOCK="$(sed -n '/CATE40F_BEGIN/,/CATE40F_END/p' "$WORKFLOW" | sed 's/^          //')"
if [ -z "$BLOCK" ]; then
    echo "❌ could not extract the CATE40F block from $WORKFLOW"
    echo "   The markers are load-bearing: without them this test tests nothing."
    exit 1
fi
case "$BLOCK" in
    *BASELINE_FILE*) : ;;
    *) echo "❌ extracted block no longer reads a baseline file"; exit 1 ;;
esac
case "$BLOCK" in
    *"stale baseline entry"*) : ;;
    *) echo "❌ extracted block no longer ratchets stale entries — the list could grow silently"; exit 1 ;;
esac

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Assembled so the spellings live only in the fixtures.
READ_LINE='tok = os.environ.get("CLAUDE_CODE_OAUTH''_TOKEN", "")'
SET_LINE='env["CLAUDE_CODE_OAUTH''_TOKEN"] = tok'
KWARG_LINE='client = Anthropic(auth''_token=tok)'
POP_LINE='env.pop("CLAUDE_CODE_OAUTH''_TOKEN", None)'
MSG_LINE='message = "set CLAUDE_CODE_OAUTH''_TOKEN first"'

# new_tree <name> <baseline-body>
new_tree() {
    d="$WORK/$1"
    rm -rf "$d"
    mkdir -p "$d/.github/workflows" "$d/apps/backend-rag/backend" "$d/apps/backend-rag/scripts" "$d/scripts"
    printf '%s\n' "$2" > "$d/.github/workflows/catE-plan-oauth-product-baseline.txt"
    printf '%s\n' "$d"
}
run_step() { ( cd "$1" && bash -c "$BLOCK" 2>&1 ); }
rc_of()    { ( cd "$1" && bash -c "$BLOCK" >/dev/null 2>&1 ); echo $?; }
expect_rc() {
    _got="$(rc_of "$2")"
    if [ "$_got" = "$1" ]; then ok "$3"; else bad "$3 (expected rc=$1, got $_got)"; fi
}

echo "catE #40f plan-OAuth-in-product baseline — guilt + innocence"

# ── INNOCENCE 1: every hit is in a baselined file ────────────────────────────
T="$(new_tree innocence_baselined 'apps/backend-rag/backend/client.py')"
printf '%s\n%s\n' "$READ_LINE" "$SET_LINE" > "$T/apps/backend-rag/backend/client.py"
expect_rc 0 "$T" "reads and sets inside the one baselined file: green"

# ── INNOCENCE 2: empty baseline, no hits ────────────────────────────────────
T="$(new_tree innocence_empty '# drained')"
printf 'x = 1\n' > "$T/apps/backend-rag/backend/app.py"
expect_rc 0 "$T" "empty baseline and a clean perimeter: green"

# ── INNOCENCE 3: strips and messages are not reads ──────────────────────────
T="$(new_tree innocence_strip '# drained')"
printf '%s\n%s\n' "$POP_LINE" "$MSG_LINE" > "$T/apps/backend-rag/backend/app.py"
expect_rc 0 "$T" "pop + a message naming the variable: green (a strip is a defence)"

# ── INNOCENCE 4: the same read OUTSIDE the product perimeter is not this step's
T="$(new_tree innocence_outside '# drained')"
printf '%s\n' "$READ_LINE" > "$T/scripts/cron_seat.py"
expect_rc 0 "$T" "a read under scripts/ is outside the product perimeter: green here"

# ── INNOCENCE 5: tests/ and test_ fixtures stay legal ───────────────────────
T="$(new_tree innocence_tests '# drained')"
mkdir -p "$T/apps/backend-rag/backend/tests"
printf '%s\n' "$KWARG_LINE" > "$T/apps/backend-rag/backend/tests/fixture.py"
printf '%s\n' "$KWARG_LINE" > "$T/apps/backend-rag/backend/test_fixture.py"
expect_rc 0 "$T" "tests/ and test_ fixtures: green"

# ── GUILT 1: a hit in a product file that is NOT baselined ───────────────────
T="$(new_tree guilt_unlisted 'apps/backend-rag/backend/client.py')"
printf '%s\n' "$READ_LINE" > "$T/apps/backend-rag/backend/client.py"
printf '%s\n' "$KWARG_LINE" > "$T/apps/backend-rag/backend/newroute.py"
expect_rc 1 "$T" "auth_token= in an unlisted product file: red"

# ── GUILT 2: a stale baseline entry (listed, but clean) ──────────────────────
T="$(new_tree guilt_stale 'apps/backend-rag/backend/client.py')"
printf 'x = 1\n' > "$T/apps/backend-rag/backend/client.py"
expect_rc 1 "$T" "listed file with no hit: red (remove the line in the landing that drained it)"
OUT="$(run_step "$T")"
case "$OUT" in
    *"stale baseline entry"*) ok "the stale entry is named" ;;
    *) bad "stale entry not named — got: $(printf '%s' "$OUT" | head -3 | tr '\n' '|')" ;;
esac

# ── GUILT 3: a missing baseline file fails closed ────────────────────────────
T="$(new_tree guilt_missing '# placeholder')"
rm -f "$T/.github/workflows/catE-plan-oauth-product-baseline.txt"
printf 'x = 1\n' > "$T/apps/backend-rag/backend/app.py"
expect_rc 1 "$T" "missing baseline file: red (fails closed)"

# ── GROWTH (step 40f-growth, CATE40F_GROWTH markers): the list may only shrink
#    versus the base. Run in a throwaway git repository, because the block
#    compares HEAD's baseline with the one at the base commit.
GBLOCK="$(sed -n '/CATE40F_GROWTH_BEGIN/,/CATE40F_GROWTH_END/p' "$WORKFLOW" | sed 's/^          //')"
if [ -z "$GBLOCK" ]; then
    bad "could not extract the CATE40F_GROWTH block — the markers are load-bearing"
else
    G="$WORK/growth"; mkdir -p "$G/.github/workflows"
    ( cd "$G" && git init -q && git config user.email t@x && git config user.name t && git config commit.gpgsign false ) >/dev/null 2>&1
    gfile="$G/.github/workflows/catE-plan-oauth-product-baseline.txt"
    gcommit() { ( cd "$G" && git add -A && git commit -q -m "$1" ) >/dev/null 2>&1; }
    grun()    { ( cd "$G" && BASE_SHA="${1:-}" bash -c "$GBLOCK" >/dev/null 2>&1 ); echo $?; }
    # introduction: no baseline at the base → the one signed growth
    printf 'x\n' > "$G/seed"; gcommit seed
    printf 'apps/backend-rag/backend/a.py\napps/backend-rag/backend/b.py\n' > "$gfile"; gcommit introduce
    if [ "$(grun)" = "0" ]; then ok "growth: introducing the baseline (absent at HEAD~1) is signed, green"; else bad "growth: introduction should be green"; fi
    # shrink: green
    printf 'apps/backend-rag/backend/a.py\n' > "$gfile"; gcommit shrink
    if [ "$(grun)" = "0" ]; then ok "growth: a shrink versus HEAD~1 is green"; else bad "growth: shrink should be green"; fi
    # grow: red
    printf 'apps/backend-rag/backend/a.py\napps/backend-rag/backend/c.py\n' > "$gfile"; gcommit grow
    if [ "$(grun)" = "1" ]; then ok "growth: an added entry versus HEAD~1 is red"; else bad "growth: an added entry should be red"; fi
    # explicit base (PR run): compare with the 'shrink' commit two back → still growth → red
    SHRINK_SHA="$(cd "$G" && git rev-parse HEAD~1)"
    if [ "$(grun "$SHRINK_SHA")" = "1" ]; then ok "growth: BASE_SHA (PR base) is honoured"; else bad "growth: BASE_SHA should be honoured"; fi
    # unresolvable base: red, never a silent pass
    if [ "$(grun deadbeefdeadbeef)" = "1" ]; then ok "growth: an unresolvable base is red (never 'none')"; else bad "growth: unresolvable base should be red"; fi
    # comments/blank lines are not entries
    printf '# note\n\napps/backend-rag/backend/a.py\napps/backend-rag/backend/c.py\n' > "$gfile"; gcommit comments
    if [ "$(grun)" = "0" ]; then ok "growth: comments and blank lines are not entries"; else bad "growth: a comment counted as growth"; fi
    # a push of TWO commits (Sol gate, round 2): the first adds an entry, the
    # second touches something else — HEAD~1 would see no growth; the push
    # event's `before` must be the base.
    BEFORE_SHA="$(cd "$G" && git rev-parse HEAD)"
    printf '# note\n\napps/backend-rag/backend/a.py\napps/backend-rag/backend/c.py\napps/backend-rag/backend/d.py\n' > "$gfile"; gcommit add-d
    printf 'y\n' > "$G/seed"; gcommit unrelated
    grun_push() { ( cd "$G" && PUSH_BEFORE="$1" bash -c "$GBLOCK" >/dev/null 2>&1 ); echo $?; }
    if [ "$(grun_push "$BEFORE_SHA")" = "1" ]; then ok "growth: PUSH_BEFORE (push event) sees growth hidden behind a second commit"; else bad "growth: PUSH_BEFORE should red the two-commit push"; fi
    if [ "$(grun_push 0000000000000000000000000000000000000000)" = "0" ]; then ok "growth: an all-zero before (branch creation) falls back to HEAD~1"; else bad "growth: all-zero before should fall back"; fi
fi

# ── RATCHET: the committed baseline names only files that still hit ──────────
#    Measured, not assumed: run the real block in the real checkout.
RC="$( (cd "$REPO_ROOT" && bash -c "$BLOCK" >/dev/null 2>&1); echo $? )"
if [ "$RC" = "0" ]; then
    ok "the committed baseline matches the tree (no unlisted hit, no stale entry)"
else
    bad "the committed baseline does not match the tree (rc=$RC) — drained a file? remove its line in the same landing"
fi

echo
echo "  passed: $PASS   failed: $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
