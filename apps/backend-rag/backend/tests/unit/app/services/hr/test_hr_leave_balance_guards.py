"""Leave balances have one writer and one lock namespace in the whole of apps/."""

from __future__ import annotations

import re
from pathlib import Path

from backend.app.services.hr.hr_service import HR_LEAVE_LOCK_NS

APPS = Path(__file__).resolve().parents[7]
HR_SERVICE = APPS / "backend-rag" / "backend" / "app" / "services" / "hr" / "hr_service.py"
SKIPPED_DIRS = {"node_modules", ".venv", "venv", ".next", "__pycache__", ".git", "dist", "build"}
SUFFIXES = {".py", ".sql", ".ts", ".tsx", ".js", ".mjs", ".sh"}
WRITE = re.compile(r"\b(INSERT\s+INTO|UPDATE)\s+hr_leave_balances\b", re.IGNORECASE)


def _source_files() -> list[Path]:
    files = []
    for path in APPS.rglob("*"):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        if SKIPPED_DIRS.intersection(path.relative_to(APPS).parts):
            continue
        files.append(path)
    return files


def _is_test_or_migration(path: Path) -> bool:
    parts = path.relative_to(APPS).parts
    return "tests" in parts or "migrations" in parts or "migrations_v2" in parts


def test_the_scan_sees_the_service_it_guards() -> None:
    assert HR_SERVICE in _source_files()


def test_only_the_hr_service_writes_leave_balances() -> None:
    writers = [
        str(path.relative_to(APPS))
        for path in _source_files()
        if path != HR_SERVICE
        and not _is_test_or_migration(path)
        and WRITE.search(path.read_text(errors="ignore"))
    ]
    assert writers == []


def test_no_other_code_uses_the_leave_lock_namespace() -> None:
    number = re.compile(rf"\b{HR_LEAVE_LOCK_NS}\b")
    users = [
        str(path.relative_to(APPS))
        for path in _source_files()
        if path != HR_SERVICE and number.search(path.read_text(errors="ignore"))
    ]
    assert users == []
