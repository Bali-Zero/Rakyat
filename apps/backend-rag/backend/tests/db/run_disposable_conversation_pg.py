"""Run selected tests in a new synthetic-only PG17 cluster on Pro.

Adapted from the repository's evidence/2026-09/*garuda-voa*/run_disposable_pg.py.
No caller-supplied database URL is accepted. No existing server is used.
The allowlisted environment excludes runtime credentials; fsync stays enabled.
Run with the existing backend venv, cwd apps/backend-rag in the test worktree.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode


def main() -> int:
    if socket.gethostname().split(".")[0].lower() != "nuzantara":
        raise SystemExit("Heavy database verification is Pro-only")
    cwd = Path.cwd().resolve()
    if ".worktrees" not in cwd.parts or not (cwd / "backend").is_dir():
        raise SystemExit("Run from apps/backend-rag inside an isolated worktree")
    binaries = Path("/opt/homebrew/opt/postgresql@17/bin")
    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"}
    }
    # A short, owned socket directory avoids macOS Unix-socket path limits.
    # The cluster never listens on TCP, even on localhost.
    with tempfile.TemporaryDirectory(prefix="udb-w3-", dir="/tmp") as temporary:
        root = Path(temporary)
        port = 5432
        cluster = root / "data"
        subprocess.run(
            [
                str(binaries / "initdb"),
                "-D",
                str(cluster),
                "-U",
                "universal_db_test_owner",
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        (cluster / "postgresql.conf").write_text(
            f"listen_addresses = ''\nport = {port}\nunix_socket_directories = '{root}'\n"
        )
        control = [str(binaries / "pg_ctl"), "-D", str(cluster)]
        started = False
        try:
            subprocess.run(
                control + ["-l", str(root / "postgres.log"), "-w", "start"],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            started = True
            pgargs = ["-h", str(root), "-p", str(port), "-U", "universal_db_test_owner"]
            subprocess.run(
                [str(binaries / "createdb"), *pgargs, "universal_db_test"],
                check=True,
                env=env,
                capture_output=True,
            )
            system_id = subprocess.check_output(
                [
                    str(binaries / "psql"),
                    *pgargs,
                    "-d",
                    "universal_db_test",
                    "-XAt",
                    "-c",
                    "SELECT system_identifier FROM pg_control_system()",
                ],
                env=env,
                text=True,
            ).strip()
            query = urlencode({"host": str(root), "port": port})
            dsn = f"postgresql://universal_db_test_owner@/universal_db_test?{query}"
            env.update(
                TEST_DATABASE_URL=dsn,
                DATABASE_URL=dsn,
                INTAKE_TEST_DSN=dsn,
                MIGRATION_DATABASE_URL="",
                ENVIRONMENT="test",
                PYTHONPATH=".:../../packages/research-os-core:../../packages/shared-schemas/src",
                NUZ_UNIVERSAL_PG_SYSTEM_ID=system_id,
                NUZ_UNIVERSAL_PG_BIN=str(binaries),
                NUZ_UNIVERSAL_PG_DSN=dsn,
                NUZ_UNIVERSAL_PG_ARTIFACT_DIR=str(root),
                PGPASSFILE=os.devnull,
                PGSERVICEFILE=os.devnull,
                # Reuse the Pro's existing tokenizer cache. tiktoken verifies
                # the cached bytes; verification runs with TCP denied.
                TIKTOKEN_CACHE_DIR="/tmp/data-gym-cache",
                API_KEYS="synthetic-test-only",
                JWT_SECRET_KEY="synthetic-key-used-only-inside-disposable-tests",
                TELEGRAM_BOT_TOKEN="123456:synthetic",
                TG_DRY_RUN="1",
                OPENAI_API_KEY="synthetic-not-a-key",
                GOOGLE_API_KEY="synthetic-not-a-key",
                QDRANT_URL="http://127.0.0.1:1",
                REDIS_URL="redis://127.0.0.1:1/0",
            )
            print(
                json.dumps(
                    {
                        "engine": "PostgreSQL17",
                        "isolation": "fresh_cluster",
                        "transport": "owned_unix_socket_only",
                        "database": "universal_db_test",
                        "system_id": system_id,
                        "fsync": "on",
                    }
                ),
                flush=True,
            )
            return subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-o",
                    "addopts=",
                    "-p",
                    "no:cacheprovider",
                    "-p",
                    "no:xdist",
                    "--tb=short",
                    "-q",
                    *sys.argv[1:],
                ],
                env=env,
            ).returncode
        finally:
            if started:
                subprocess.run(
                    control + ["-m", "immediate", "-w", "stop"],
                    check=True,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                print("DISPOSABLE_PG_STOPPED", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
