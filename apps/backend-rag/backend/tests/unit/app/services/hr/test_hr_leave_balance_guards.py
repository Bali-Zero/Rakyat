"""Leave balances have one writer and one lock namespace in apps/ and the root scripts/.

The writer guard reads text. It sees INSERT INTO, UPDATE and DELETE FROM on the table, bare,
schema-qualified or double-quoted, in any case. It cannot see dynamic SQL (a statement built
from pieces) or an ORM write; the repository has no ORM writer for this table.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.services.hr import hr_service

APPS = Path(__file__).resolve().parents[7]
ROOT = APPS.parent
SCRIPTS = ROOT / "scripts"
HR_SERVICE = APPS / "backend-rag" / "backend" / "app" / "services" / "hr" / "hr_service.py"
SKIPPED_DIRS = {"node_modules", ".venv", "venv", ".next", "__pycache__", ".git", "dist", "build"}
SUFFIXES = {".py", ".sql", ".ts", ".tsx", ".js", ".mjs", ".sh"}
_TABLE = r'(?:"?\w+"?\s*\.\s*)?"?hr_leave_balances"?(?!\w)'
WRITE = re.compile(
    rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?{_TABLE}", re.IGNORECASE
)


def writes_leave_balances(text: str) -> bool:
    """True when the text holds a statement that writes hr_leave_balances."""
    return WRITE.search(text) is not None


def _source_files() -> list[Path]:
    files = []
    for top in (APPS, SCRIPTS):
        for path in top.rglob("*"):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            if SKIPPED_DIRS.intersection(path.relative_to(ROOT).parts):
                continue
            files.append(path)
    return files


def _is_test_or_migration(path: Path) -> bool:
    parts = path.relative_to(ROOT).parts
    return "tests" in parts or "migrations" in parts or "migrations_v2" in parts


def test_the_scan_sees_the_service_it_guards() -> None:
    files = _source_files()
    assert HR_SERVICE in files
    assert any(SCRIPTS in path.parents for path in files)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO hr_leave_balances (employee_id) VALUES (1)",
        "update hr_leave_balances SET used_days = 0",
        "DELETE FROM hr_leave_balances WHERE id = 1",
        "delete\n  from hr_leave_balances",
        "INSERT INTO public.hr_leave_balances (employee_id) VALUES (1)",
        'UPDATE "hr_leave_balances" SET used_days = 0',
        'DELETE FROM public."hr_leave_balances"',
        'UPDATE "public"."hr_leave_balances" SET used_days = 0',
    ],
)
def test_the_matcher_catches_every_shape_of_write(sql: str) -> None:
    assert writes_leave_balances(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM hr_leave_balances WHERE employee_id = 1",
        "SELECT 1 FROM public.hr_leave_balances lb JOIN hr_leave_types lt ON lt.id = lb.leave_type_id",
        'SELECT * FROM "hr_leave_balances"',
        "UPDATE hr_leave_balances_archive SET used_days = 0",
        "DELETE FROM hr_leave_types WHERE id = 1",
    ],
)
def test_the_matcher_lets_reads_and_other_tables_pass(sql: str) -> None:
    assert not writes_leave_balances(sql)


def test_only_the_hr_service_writes_leave_balances() -> None:
    writers = [
        str(path.relative_to(ROOT))
        for path in _source_files()
        if path != HR_SERVICE
        and not _is_test_or_migration(path)
        and writes_leave_balances(path.read_text(errors="ignore"))
    ]
    assert writers == []


def test_no_other_code_uses_the_leave_lock_namespace() -> None:
    namespace = getattr(hr_service, "HR_LEAVE_LOCK_NS", None)
    assert namespace is not None, "no leave lock namespace"
    number = re.compile(rf"\b{namespace}\b")
    users = [
        str(path.relative_to(APPS))
        for path in _source_files()
        if path != HR_SERVICE and number.search(path.read_text(errors="ignore"))
    ]
    assert users == []
