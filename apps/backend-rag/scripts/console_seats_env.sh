#!/usr/bin/env bash
# console_seats_env.sh — map the Console seat pair files into the slot variables.
#
# SOURCE it from a headless wrapper (never execute it):
#     [ -f "$APP_DIR/scripts/console_seats_env.sh" ] && . "$APP_DIR/scripts/console_seats_env.sh"
#
# RULED 2026-10-26 / 2026-11-01 (docs/rules/RULINGS.md): a headless lane reaches a
# Claude model only on a Console seat — the key of a linked organization pinned to
# its workspace — and this file is the ONE place on a machine where the pair files
# become the slot variables the lane reads (backend.llm.claude_oauth_client:
# ANTHROPIC_CONSOLE_KEY_<n> + ANTHROPIC_CONSOLE_WORKSPACE_<n>, n in 1..3). It lives
# in the lane (infra/anthropic-door/headless_lanes.txt lists this directory) so a
# wrapper outside the lane never touches a credential itself.
#
# Inputs (all optional):
#   NUZANTARA_CONSOLE_SECRETS_DIR  directory of the 0600 pair files
#                                  (default: ~/nuzantara/.secrets)
#   NUZANTARA_CONSOLE_SEATS        org names in slot order
#                                  (default: "antozero kaiser antosiano")
# Files read: <dir>/anthropic-console-<org>.key and .workspace (whitespace trimmed).
# A pair that is missing, empty or unreadable leaves its slot unset and the next
# org takes the next slot. Output: the slot variables, plus
# NUZANTARA_CONSOLE_SEAT_COUNT for the caller's log line.
#
# NOTHING is printed, and an inherited `set -x` is suspended for the duration:
# under xtrace the shell itself would print every `export NAME=<value>` (gate
# round 3, 2026-11-03, reproduced through the wrapper's own source line). The
# caller's xtrace state is restored on the way out. Safe under `set -eu`: every
# read sits in an `if` condition, so a failing read skips the slot instead of
# exiting the caller or exporting an empty value.

case $- in
    *x*) _console_xtrace=1; set +x ;;
    *) _console_xtrace= ;;
esac 2>/dev/null

_console_secrets_dir="${NUZANTARA_CONSOLE_SECRETS_DIR:-$HOME/nuzantara/.secrets}"
_console_slot=0
for _console_org in ${NUZANTARA_CONSOLE_SEATS:-antozero kaiser antosiano}; do
    _console_key_file="$_console_secrets_dir/anthropic-console-$_console_org.key"
    _console_ws_file="$_console_secrets_dir/anthropic-console-$_console_org.workspace"
    if [ -r "$_console_key_file" ] && [ -r "$_console_ws_file" ] \
        && _console_key="$(tr -d '[:space:]' < "$_console_key_file" 2>/dev/null)" \
        && _console_ws="$(tr -d '[:space:]' < "$_console_ws_file" 2>/dev/null)" \
        && [ -n "$_console_key" ] && [ -n "$_console_ws" ]; then
        _console_slot=$((_console_slot + 1))
        export "ANTHROPIC_CONSOLE_KEY_$_console_slot=$_console_key"
        export "ANTHROPIC_CONSOLE_WORKSPACE_$_console_slot=$_console_ws"
    fi
done
export NUZANTARA_CONSOLE_SEAT_COUNT="$_console_slot"

if [ -n "$_console_xtrace" ]; then
    unset _console_secrets_dir _console_slot _console_org _console_key_file _console_ws_file \
        _console_key _console_ws _console_xtrace
    set -x
else
    unset _console_secrets_dir _console_slot _console_org _console_key_file _console_ws_file \
        _console_key _console_ws _console_xtrace
fi
