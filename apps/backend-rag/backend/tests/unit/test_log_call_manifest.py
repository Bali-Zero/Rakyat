"""Visibility ratchet for the log calls of the five CRM/HR files (Rakyat PR #276).

Every log call in scope (direct ``logger.*`` calls and the ``log_success`` /
``log_database_operation`` helpers, which forward their kwargs into the record's
``context``) is reduced to a normalized record of its argument expressions and compared,
as a multiset per function, to the committed ``log_call_manifest.json``.

This is a VISIBILITY ratchet, not a provenance proof: it makes every change to a log
call's argument expressions show up in review. It does not see an upstream reassignment
that changes what a logged variable holds. Provenance stays a review duty.

To change a log call in these files, update the JSON in the same commit:
``python backend/tests/unit/test_log_call_manifest.py --write`` (from apps/backend-rag),
then add or keep a ``note`` on every entry that relies on rule (e).
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
MANIFEST_PATH = Path(__file__).with_name("log_call_manifest.json")

SCOPE_FILES = [
    "app/routers/crm_clients.py",
    "app/routers/crm_practices.py",
    "services/crm/client_core.py",
    "app/services/hr/hr_service.py",
    "app/routers/hr_my_late_incident.py",
]

LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
HELPERS = {"log_success", "log_database_operation"}
LOGGER_NAMES = {"logger", "logging"}
# Exception-variable names that are flagged even outside an `except ... as` binding.
FALLBACK_EXC_NAMES = {"e", "exc", "err", "ex", "error", "exception"}
# Attributes of an exception object that carry its text. A typed field of a custom
# exception (for example an integer row id) is not flagged here; it stays a review duty.
EXC_TEXT_ATTRS = {
    "args",
    "message",
    "msg",
    "detail",
    "reason",
    "orig",
    "strerror",
    "__cause__",
    "__context__",
}


def _is_log_call(node: ast.Call) -> str | None:
    """Return 'direct' or 'helper' when the call is in scope."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in LEVELS:
        base = func.value
        if isinstance(base, ast.Name) and base.id in LOGGER_NAMES:
            return "direct"
        if isinstance(base, ast.Attribute) and base.attr in LOGGER_NAMES:
            return "direct"
    if isinstance(func, ast.Name) and func.id in HELPERS:
        return "helper"
    return None


def _un(node: ast.AST) -> str:
    return ast.unparse(node)


def _exc_violations(expr: ast.AST, exc_names: set[str]) -> list[str]:
    """Flag an exception object, str(e), repr(e), e.args, f"{e}": every use of an
    exception name that is not the operand of type(...)."""
    found: list[str] = []
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(expr):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    for node in ast.walk(expr):
        if isinstance(node, ast.Name) and node.id in exc_names:
            parent = parents.get(id(node))
            if (
                isinstance(parent, ast.Call)
                and isinstance(parent.func, ast.Name)
                and parent.func.id == "type"
            ):
                continue
            if (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and parent.attr not in EXC_TEXT_ATTRS
            ):
                continue
            found.append(_un(parent) if parent is not None else node.id)
    return found


class _Collector(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.scope: list[str] = []
        self.handlers: list[str] = []
        self.records: list[dict] = []
        self.violations: list[str] = []

    def _enter(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.handlers.append(node.name)
        self.generic_visit(node)
        if node.name:
            self.handlers.pop()

    def visit_Call(self, node: ast.Call) -> None:
        kind = _is_log_call(node)
        if kind:
            self._record(node, kind)
        self.generic_visit(node)

    def _record(self, node: ast.Call, kind: str) -> None:
        function = ".".join(self.scope) or "<module>"
        target = _un(node.func)
        positional = list(node.args)
        if kind == "helper" and positional:
            positional = positional[1:]  # drop the logger argument
        rec: dict = {
            "file": self.file,
            "function": function,
            "target": target,
            "message": _un(positional[0]) if positional else None,
            "args": [_un(a) for a in positional[1:]],
            "fields": {},
            "exc_info": None,
        }
        exprs: list[ast.AST] = list(positional)
        for kw in node.keywords:
            if kw.arg is None:
                rec["fields"]["**"] = _un(kw.value)
                exprs.append(kw.value)
            elif kw.arg == "exc_info":
                rec["exc_info"] = _un(kw.value)
                exprs.append(kw.value)
            elif kw.arg in ("extra", "context") and isinstance(kw.value, ast.Dict):
                for key, value in zip(kw.value.keys, kw.value.values, strict=True):
                    name = _un(key) if key is not None else "**"
                    rec["fields"][f"{kw.arg}.{name}"] = _un(value)
                    if key is not None:
                        exprs.append(key)
                    exprs.append(value)
            else:
                rec["fields"][kw.arg] = _un(kw.value)
                exprs.append(kw.value)

        where = f"{self.file}::{function} {target}"
        if isinstance(node.func, ast.Attribute) and node.func.attr == "exception":
            self.violations.append(f"{where}: logger.exception")
        if rec["exc_info"] not in (None, "False"):
            self.violations.append(f"{where}: exc_info={rec['exc_info']}")
        names = set(self.handlers) | FALLBACK_EXC_NAMES
        for expr in exprs:
            for bad in _exc_violations(expr, names):
                self.violations.append(f"{where}: exception value in {bad}")
        self.records.append(rec)


def analyze(source: str, file: str = "snippet.py") -> tuple[list[dict], list[str]]:
    collector = _Collector(file)
    collector.visit(ast.parse(source))
    return collector.records, collector.violations


def _canon(record: dict) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


def _by_function(records: list[dict]) -> dict[tuple[str, str], Counter]:
    out: dict[tuple[str, str], Counter] = {}
    for rec in records:
        core = {k: v for k, v in rec.items() if k != "note"}
        out.setdefault((rec["file"], rec["function"]), Counter())[_canon(core)] += 1
    return out


def _describe(canon: str) -> str:
    rec = json.loads(canon)
    parts = [str(rec["message"])] + list(rec["args"])
    parts += [f"{k}={v}" for k, v in rec["fields"].items()]
    if rec["exc_info"] is not None:
        parts.append(f"exc_info={rec['exc_info']}")
    return f"{rec['target']}({', '.join(parts)})"


def diff_manifests(expected: list[dict], actual: list[dict]) -> list[str]:
    """Multiset diff per function, as normalized code expressions only."""
    exp, act = _by_function(expected), _by_function(actual)
    lines: list[str] = []
    for key in sorted(set(exp) | set(act)):
        removed = exp.get(key, Counter()) - act.get(key, Counter())
        added = act.get(key, Counter()) - exp.get(key, Counter())
        for canon, n in removed.items():
            lines.append(f"- {key[0]}::{key[1]} x{n}: {_describe(canon)}")
        for canon, n in added.items():
            lines.append(f"+ {key[0]}::{key[1]} x{n}: {_describe(canon)}")
    return lines


def build_manifest(sources: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    violations: list[str] = []
    for file in SCOPE_FILES:
        text = (sources or {}).get(file)
        if text is None:
            text = (BACKEND / file).read_text(encoding="utf-8")
        recs, viol = analyze(text, file)
        records += recs
        violations += viol
    return records, violations


def load_manifest() -> list[dict]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["entries"]


def write_manifest() -> None:
    old_notes = {}
    if MANIFEST_PATH.exists():
        for entry in load_manifest():
            if "note" in entry:
                core = {k: v for k, v in entry.items() if k != "note"}
                old_notes[_canon(core)] = entry["note"]
    records, _ = build_manifest()
    for rec in records:
        note = old_notes.get(_canon(rec))
        if note:
            rec["note"] = note
    records.sort(
        key=lambda r: (
            r["file"],
            r["function"],
            _canon({k: v for k, v in r.items() if k != "note"}),
        )
    )
    MANIFEST_PATH.write_text(
        json.dumps({"entries": records}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- tests


def test_manifest_matches_the_five_files():
    actual, _ = build_manifest()
    diff = diff_manifests(load_manifest(), actual)
    assert not diff, (
        "log calls in the five CRM/HR files changed; if intended, regenerate "
        "log_call_manifest.json and review the diff:\n" + "\n".join(diff)
    )


def test_manifest_counts_per_file():
    counts = Counter(entry["file"] for entry in load_manifest())
    assert dict(counts) == {
        "app/routers/crm_clients.py": 23,
        "app/routers/crm_practices.py": 33,
        "services/crm/client_core.py": 39,
        "app/services/hr/hr_service.py": 14,
        "app/routers/hr_my_late_incident.py": 1,
    }


def test_no_forbidden_shape_in_scope():
    _, violations = build_manifest()
    assert not violations, "\n".join(violations)


def test_every_rule_e_entry_names_its_validating_line():
    notes = [e for e in load_manifest() if "note" in e]
    assert notes, "rule (e) entries must carry a note naming the validating file:line"
    for entry in notes:
        assert ".py:" in entry["note"], entry["note"]


# ----- guilt snippets: each must be flagged -----

GUILT = {
    "truthy exc_info": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x", exc_info=True)\n',
    "exc_info with an exception": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x", exc_info=e)\n',
    "logger.exception": 'try:\n    pass\nexcept Exception:\n    logger.exception("x")\n',
    "exception object": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x %s", e)\n',
    "str(e)": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x %s", str(e))\n',
    "repr(e)": 'try:\n    pass\nexcept Exception as e:\n    logger.warning("x", extra={"r": repr(e)})\n',
    "e.args": 'try:\n    pass\nexcept Exception as e:\n    log_success(logger, "x", a=e.args)\n',
    "e.detail": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x %s", e.detail)\n',
    "key str(e)": 'try:\n    pass\nexcept Exception as e:\n    logger.info("x", extra={str(e): 1})\n',
    "key repr(e)": 'try:\n    pass\nexcept Exception as e:\n    logger.info("x", extra={repr(e): 1})\n',
    "key e.args": 'try:\n    pass\nexcept Exception as e:\n    logger.info("x", extra={e.args: 1})\n',
    "key f-string of the exception": (
        'try:\n    pass\nexcept Exception as e:\n    logger.info("x", extra={f"k{e}": 1})\n'
    ),
    "key is the exception object": (
        'try:\n    pass\nexcept Exception as e:\n    logger.info("x", extra={e: 1})\n'
    ),
    "key str(e) in a context dict": (
        'try:\n    pass\nexcept Exception as e:\n    logger.info("x", context={str(e): 1})\n'
    ),
    "f-string of the exception": 'try:\n    pass\nexcept Exception as boom:\n    logger.info(f"x {boom}")\n',
}

INNOCENCE = {
    "type name only": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x %s", type(e).__name__)\n',
    "exc_info False": 'try:\n    pass\nexcept Exception as e:\n    logger.error("x", exc_info=False)\n',
    "no exc_info": 'logger.info("x %s", 1)\n',
    "ids only": 'log_success(logger, "x", practice_id=1)\n',
    "literal keys": 'logger.info("x", extra={"practice_id": 1, "doc_id": 2})\n',
    "literal keys with type(e) value": (
        "try:\n    pass\nexcept Exception as e:\n"
        '    logger.info("x", extra={"error_type": type(e).__name__})\n'
    ),
    "typed field of a custom exception": (
        'try:\n    pass\nexcept Exception as dup:\n    logger.info("x %s", dup.existing_client_id)\n'
    ),
    "type name in extra": (
        "try:\n    pass\nexcept Exception as e:\n"
        '    logger.warning("x", extra={"error_type": type(e).__name__})\n'
    ),
}


@pytest.mark.parametrize("name", sorted(GUILT))
def test_shape_check_flags_guilt(name):
    _, violations = analyze(GUILT[name])
    assert violations, name


@pytest.mark.parametrize("name", sorted(INNOCENCE))
def test_shape_check_passes_innocence(name):
    _, violations = analyze(INNOCENCE[name])
    assert not violations, (name, violations)


def test_helper_kwargs_are_part_of_the_record():
    records, _ = analyze('log_success(logger, "m", practice_id=1, client_id=2)\n')
    assert records[0]["target"] == "log_success"
    assert records[0]["message"] == "'m'"
    assert records[0]["fields"] == {"practice_id": "1", "client_id": "2"}


def test_a_changed_expression_changes_the_manifest():
    base, _ = analyze('def f(user_email):\n    log_success(logger, "m", practice_id=1)\n')
    changed, _ = analyze(
        'def f(user_email):\n    log_success(logger, "m", practice_id=1, updated_by=user_email)\n'
    )
    diff = diff_manifests(base, changed)
    assert any("updated_by=user_email" in line for line in diff)
    assert not diff_manifests(base, base)


def test_a_call_moved_to_another_function_changes_the_manifest():
    a, _ = analyze('def f():\n    logger.info("m")\ndef g():\n    pass\n')
    b, _ = analyze('def f():\n    pass\ndef g():\n    logger.info("m")\n')
    assert diff_manifests(a, b)


def test_reinserting_updated_by_user_email_in_a_real_file_fails_the_manifest():
    path = "app/routers/crm_practices.py"
    source = (BACKEND / path).read_text(encoding="utf-8")
    anchor = '"Invoice regenerated successfully",\n            practice_id=practice_id,\n'
    assert anchor in source
    mutated = source.replace(anchor, anchor + "            updated_by=user_email,\n", 1)
    actual, _ = build_manifest({path: mutated})
    diff = diff_manifests(load_manifest(), actual)
    assert any("updated_by=user_email" in line for line in diff)


if __name__ == "__main__":
    if "--write" in sys.argv:
        write_manifest()
        print(f"wrote {MANIFEST_PATH}")
