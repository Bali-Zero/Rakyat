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

# Activate venv
source .venv/bin/activate

# The runner connects to `resolve_migration_dsn()` =
# `settings.migration_database_url or settings.database_url`, and the settings
# read each name from the environment first, then from ./.env (this directory).
# Importing the settings here would need the whole application environment, so
# the same precedence is replicated with the standard library and nothing about
# the URL is ever printed.
if ! python3 -c '
import os
import sys
from urllib.parse import parse_qs, urlparse


def dotenv_values(path):
    values = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"\x27":
                    value = value[1:-1]
                values[key.strip().lower()] = value
    except OSError:
        pass
    return values


def setting(name, dotenv):
    for key, value in os.environ.items():
        if key.lower() == name and value:
            return value
    return dotenv.get(name) or None


try:
    dotenv = dotenv_values(".env")
    value = setting("migration_database_url", dotenv) or setting("database_url", dotenv)
    if not value:
        raise ValueError
    parsed = urlparse(value)
    host = parsed.hostname
    socket_hosts = parse_qs(parsed.query).get("host", [])
    if host is None or host == "":
        is_local = any(item.startswith("/") for item in socket_hosts)
    else:
        is_local = host in {"localhost", "127.0.0.1", "::1"} or host.startswith("/")
except (KeyError, ValueError):
    is_local = False

sys.exit(0 if is_local else 1)
'; then
    echo "the migration DSN must target a local database" >&2
    exit 2
fi

# Run migrations
python3 -m backend.db.migrate apply-all
