#!/usr/bin/env bash
# Telegram retired by owner order, 2026-11-07. Legacy callers get a quiet no-send.
set -euo pipefail
set +o xtrace
printf '%s\n' 'Telegram retired: no credentials read and no message sent.'
exit 0
