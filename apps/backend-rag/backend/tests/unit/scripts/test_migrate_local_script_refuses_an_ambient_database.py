"""Safety checks for the local migration wrapper."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[4] / "migrate-local.sh"


def _make_script_directory(tmp_path: Path) -> tuple[Path, Path]:
    workdir = tmp_path / "migration"
    workdir.mkdir()
    script = workdir / "migrate-local.sh"
    shutil.copy(SCRIPT, script)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    activate = workdir / ".venv" / "bin" / "activate"
    activate.parent.mkdir(parents=True)
    activate.write_text("")

    stub_directory = tmp_path / "bin"
    stub_directory.mkdir()
    record = tmp_path / "migration-call.json"
    stub = stub_directory / "python3"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1:] == ['-m', 'backend.db.migrate', 'apply-all']:\n"
        "    with open(os.environ['MIGRATION_RECORD'], 'w') as output:\n"
        "        json.dump({'args': sys.argv[1:], 'spaced': os.environ.get('SPACED_VALUE')}, output)\n"
        "    raise SystemExit(0)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return workdir, record


def _run_script(workdir: Path, record: Path, database_url: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "DATABASE_URL": database_url,
        "MARKER_VALUE": "do-not-print-me",
        "MIGRATION_RECORD": str(record),
        "PATH": f"{record.parent / 'bin'}:{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", "migrate-local.sh"],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_env_file_does_not_print_or_use_ambient_database(tmp_path: Path) -> None:
    workdir, record = _make_script_directory(tmp_path)
    ambient_url = "postgresql://db.example.test/prod"

    result = _run_script(workdir, record, ambient_url)

    assert result.returncode != 0
    assert not record.exists()
    assert "do-not-print-me" not in result.stdout + result.stderr
    assert ambient_url not in result.stdout + result.stderr


def test_non_local_database_is_refused_before_migration(tmp_path: Path) -> None:
    workdir, record = _make_script_directory(tmp_path)
    remote_host = "db.example.test"
    (workdir / ".env.local").write_text(f"DATABASE_URL=postgresql://{remote_host}/prod\n")

    result = _run_script(workdir, record, "postgresql://localhost/dev")

    assert result.returncode == 2
    assert not record.exists()
    assert remote_host not in result.stdout + result.stderr


def test_local_database_runs_migration_and_preserves_spaced_values(tmp_path: Path) -> None:
    workdir, record = _make_script_directory(tmp_path)
    (workdir / ".env.local").write_text(
        "# local connection\n"
        "DATABASE_URL=postgresql://user@localhost:5432/dev\n"
        "SPACED_VALUE='plain fake value'\n"
    )

    result = _run_script(workdir, record, "postgresql://db.example.test/prod")

    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text()) == {
        "args": ["-m", "backend.db.migrate", "apply-all"],
        "spaced": "plain fake value",
    }
