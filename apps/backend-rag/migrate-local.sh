#!/bin/bash
# Apply migrations to local DB

set -euo pipefail

cd "$(dirname "$0")"

# Load local env
if [[ ! -r .env.local ]]; then
    echo ".env.local is missing or unreadable" >&2
    exit 2
fi

set -a
. ./.env.local
set +a

if [[ -z "${DATABASE_URL:-}" ]] || ! python3 -c '
import os
import sys
from urllib.parse import parse_qs, urlparse

try:
    value = os.environ["DATABASE_URL"]
    parsed = urlparse(value)
    host = parsed.hostname
    socket_hosts = parse_qs(parsed.query).get("host", [])
    is_socket = (host is not None and host.startswith("/")) or any(
        item.startswith("/") for item in socket_hosts
    )
    is_local = host in {"localhost", "127.0.0.1", "::1"}
except (KeyError, ValueError):
    is_local = False
    is_socket = False

sys.exit(0 if is_local or is_socket else 1)
'; then
    echo "DATABASE_URL must target a local database" >&2
    exit 2
fi

# Activate venv
source .venv/bin/activate

# Run migrations
python3 -m backend.db.migrate apply-all
