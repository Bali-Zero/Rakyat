#!/usr/bin/env bash
# Fleet board: a plain shell loop, zero model tokens. Run it in tmux on the always-on coordinator Mac:
#   tmux new -s board 'bash fleet/kit/fleet_board.sh'
# Every INTERVAL seconds it reads each machine's status files (local ones directly, remote ones over ssh on the
# tailnet, read-only) and rewrites ~/.rakyat-fleet/BOARD.md. Machines are listed in ~/.rakyat-fleet/hosts.txt,
# one per line: "<name> <ssh target or 'local'>", for example "pro pro.tailnet-name" or "mini local".
# Status files carry counts and states only. Cloud seats are not polled: they publish private artifacts.
set -uo pipefail
ROOT="${FLEET_HOME:-$HOME/.rakyat-fleet}"; HOSTS="$ROOT/hosts.txt"; BOARD="$ROOT/BOARD.md"
INTERVAL="${INTERVAL:-300}"; ONCE="${ONCE:-}"
mkdir -p "$ROOT"
[ -f "$HOSTS" ] || { echo "board: write $HOSTS first (one line per machine: <name> <ssh target|local>)" >&2; exit 2; }
collect() {  # $1 name, $2 target -> prints "name<TAB>json" per status file
  local name="$1" target="$2" cmd='for f in ~/.rakyat-fleet/out/*/status.json; do [ -f "$f" ] && printf "%s\t" "$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f")" && tr -d "\n" < "$f" && echo; done'
  if [ "$target" = local ]; then bash -c "$cmd" 2>/dev/null
  else ssh -n -o ConnectTimeout=5 -o BatchMode=yes "$target" "$cmd" 2>/dev/null || echo "UNREACHABLE"; fi | sed "s/^/$name\t/"
}
while :; do
  tmp="$(mktemp)"
  while read -r name target _; do
    case "$name" in ''|'#'*) continue;; esac
    collect "$name" "$target" >> "$tmp"
  done < "$HOSTS"
  python3 - "$tmp" "$BOARD" <<'PY'
import json, sys, time
rows, now = [], time.time()
for line in open(sys.argv[1]):
    parts = line.rstrip("\n").split("\t", 2)
    if len(parts) == 2 and parts[1] == "UNREACHABLE":
        rows.append((parts[0], "-", "-", "UNREACHABLE", "-", "-", "-", "-", "-")); continue
    if len(parts) < 3: continue
    name, mtime, raw = parts
    try: d = json.loads(raw)
    except Exception: rows.append((name, "?", "-", "unreadable status.json", "-", "-", "-", "-", "-")); continue
    age = int((now - float(mtime or now)) // 60)
    rows.append((name, d.get("sector", "?"), d.get("mode", "-"), d.get("state", "-"), d.get("phase", "-"),
                 str(d.get("confirmed", "-")), str(d.get("branches", "-")), str(d.get("kit_id", "-"))[:12], f"{age} min"))
out = ["# Fleet board", "", time.strftime("Updated %Y-%m-%d %H:%M %Z"), "",
       "| machine | sector | mode | state | phase | confirmed | branches | kit | last update |", "|---|---|---|---|---|---:|---:|---|---|"]
out += ["| " + " | ".join(r) + " |" for r in sorted(rows)]
stale = [r for r in rows if r[3] == "running" and r[8].endswith("min") and int(r[8].split()[0]) > 90]
if stale: out += ["", "Running but silent for more than 90 minutes: " + ", ".join(f"{r[0]}/{r[1]}" for r in stale)]
open(sys.argv[2], "w").write("\n".join(out) + "\n")
PY
  rm -f "$tmp"; clear 2>/dev/null; cat "$BOARD"
  [ -n "$ONCE" ] && break
  sleep "$INTERVAL"
done
