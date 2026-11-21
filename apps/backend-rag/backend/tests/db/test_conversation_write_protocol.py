"""Deterministic tests for the shared phase-1 conversation write protocol."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.db.repositories.conversation_repository import (
    ConversationCommitOutcomeUnknown,
    ConversationRepository,
    conversation_write_transaction,
    lock_conversation_identities,
    lock_conversation_session,
)
from backend.services.common import cache

_BACKEND = Path(__file__).resolve().parents[2]
_APP_ROOT = _BACKEND.parent


def test_source_snapshot_uses_its_own_root_pytest_guard() -> None:
    expected = _BACKEND.parents[2] / "scripts/pytest_guards/pytest_verbosity_guard.py"
    assert Path(sys.modules["pytest_verbosity_guard"].__file__).resolve() == expected.resolve()


_CANONICAL_CALLERS = (
    "app/routers/admin_conversation_cleanup.py",
    "app/routers/conversations.py",
)
_WRITE_METHODS = {
    "append_messages",
    "create_snapshot",
    "replace_latest_metadata",
    "merge_metadata_by_id",
    "delete_for_user",
    "delete_by_id_for_user",
    "apply_retention_window",
    "cleanup_old_conversations",
    "anonymize_user_data",
}
_SQL_IDENTIFIER = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"]|"")+")'
_SQL_RELATION = rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?{_SQL_IDENTIFIER}"
_CONVERSATIONS_RELATION = r'(?:(?:"public"|public)\s*\.\s*)?(?:"conversations"|conversations)'
_RELATION_TERMINATOR = r"(?![A-Za-z0-9_$])"
_CONVERSATION_MUTATION_PATTERNS = (
    re.compile(
        rf"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|COPY)"
        rf"\s+(?:ONLY\s+)?{_CONVERSATIONS_RELATION}(?:\s*\*)?"
        rf"{_RELATION_TERMINATOR}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bTRUNCATE(?:\s+TABLE)?\s+"
        rf"(?:(?:ONLY\s+)?{_SQL_RELATION}(?:\s*\*)?\s*,\s*)*"
        rf"(?:ONLY\s+)?{_CONVERSATIONS_RELATION}(?:\s*\*)?"
        rf"{_RELATION_TERMINATOR}",
        re.IGNORECASE,
    ),
)
_PRUNED_RUNTIME_DIRS = frozenset(
    {
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "cache",
        "caches",
        "fixtures",
        "site-packages",
        "tests",
        "venv",
    }
)


def _contains_conversation_mutation(source: str) -> bool:
    return any(pattern.search(source) is not None for pattern in _CONVERSATION_MUTATION_PATTERNS)


def _is_pruned_runtime_dir(name: str) -> bool:
    return (
        name in _PRUNED_RUNTIME_DIRS
        or name == "generated"
        or name.startswith("generated_")
        or name == "migrations"
        or name.startswith("migrations_")
    )


def _iter_runtime_python_files(root: Path) -> Iterator[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not _is_pruned_runtime_dir(name))
        current_path = Path(current_root)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield current_path / filename


def _find_runtime_conversation_mutations(
    root: Path,
    *,
    repository_path: Path | None = None,
) -> list[Path]:
    return [
        path
        for path in _iter_runtime_python_files(root)
        if path != repository_path and _contains_conversation_mutation(path.read_text())
    ]


def _stage1_protocol_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def takes_lock(statement):
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Await):
            call = statement.value.value
            return (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in {"lock_conversation_session", "lock_conversation_identities"}
            )
        if isinstance(statement, ast.If):
            return any(takes_lock(s) for s in statement.body) and any(
                takes_lock(s) for s in statement.orelse
            )
        return False

    violations = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executemany", "fetch", "fetchrow", "fetchval"}
            and node.args
            and _contains_conversation_mutation(ast.get_source_segment(source, node.args[0]) or "")
        ):
            continue
        current = node
        locked = False
        guarded = False
        while id(current) in parents:
            parent = parents[id(current)]
            for _field, value in ast.iter_fields(parent):
                if isinstance(value, list) and current in value:
                    locked |= any(
                        takes_lock(s)
                        for s in value[: value.index(current)]
                        if isinstance(s, ast.stmt)
                    )
            if isinstance(parent, ast.AsyncWith) and any(
                isinstance(i.context_expr, ast.Call)
                and isinstance(i.context_expr.func, ast.Name)
                and i.context_expr.func.id == "conversation_write_connection"
                for i in parent.items
            ):
                guarded = True
                break
            current = parent
        if not guarded or not locked:
            violations.append(f"line {node.lineno}: transaction={guarded}, preceding_lock={locked}")
    return violations


_INSERT_INTO_CONVERSATIONS = re.compile(
    rf"\bINSERT\s+INTO\s+{_CONVERSATIONS_RELATION}{_RELATION_TERMINATOR}", re.IGNORECASE
)


def _retired_writer_sites(source: str) -> list[str]:
    """Names the two writers retired from the web router: the `/save` INSERT and the title UPDATE."""
    tree = ast.parse(source)
    sites = [
        f"function {node.name}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"save_conversation", "_generate_and_update_title"}
    ]
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if _INSERT_INTO_CONVERSATIONS.search(node.value):
            sites.append(f"INSERT at line {node.lineno}")
        elif "generated_title" in node.value and _contains_conversation_mutation(node.value):
            sites.append(f"title UPDATE at line {node.lineno}")
    return sites


def test_retired_web_writers_cannot_return_to_the_router() -> None:
    router = _BACKEND / "app/routers/conversations.py"
    assert _retired_writer_sites(router.read_text()) == []


def test_retired_conversation_service_module_is_gone() -> None:
    assert not (_BACKEND / "services/misc/conversation_service.py").exists()
    assert importlib.util.find_spec("backend.services.misc.conversation_service") is None


@pytest.mark.parametrize(
    "source",
    [
        "async def save_conversation(request):\n    pass",
        "async def _generate_and_update_title(i, m, p):\n    pass",
        'Q = "INSERT INTO conversations (user_id) VALUES ($1)"',
        'Q = "INSERT INTO public.conversations(user_id) VALUES ($1)"',
        "Q = \"UPDATE conversations SET metadata = metadata || jsonb_build_object('generated_title', $1)\"",
    ],
)
def test_retired_writer_guard_detects_each_returning_form(source: str) -> None:
    assert _retired_writer_sites(source)


@pytest.mark.parametrize(
    "source",
    [
        'Q = "DELETE FROM conversations WHERE id = $1"',
        "Q = \"UPDATE conversations SET messages = '[]' WHERE id = $1\"",
        "Q = \"SELECT metadata->>'generated_title' FROM conversations\"",
        'Q = "INSERT INTO conversations_archive (id) VALUES ($1)"',
    ],
)
def test_retired_writer_guard_ignores_the_writers_that_remain(source: str) -> None:
    assert _retired_writer_sites(source) == []


def test_phase_one_runtime_mutations_cooperate_with_shared_protocol() -> None:
    expected = {
        _BACKEND / p for p in (*_CANONICAL_CALLERS, "db/repositories/conversation_repository.py")
    }
    actual = set(_find_runtime_conversation_mutations(_APP_ROOT))
    assert actual == expected
    for path in sorted(actual):
        assert not _stage1_protocol_violations(path.read_text()), str(path)


@pytest.mark.parametrize(
    "body",
    [
        'await conn.execute("UPDATE conversations SET messages = $1", [])',
        'async with conversation_write_connection(pool) as conn:\n        await conn.execute("DELETE FROM conversations")',
        'async with conversation_write_connection(pool) as conn:\n        if flag:\n            await lock_conversation_session(conn, session)\n        await conn.execute("DELETE FROM conversations")',
    ],
)
def test_phase_one_guard_rejects_missing_transaction_or_dominating_lock(body: str) -> None:
    assert _stage1_protocol_violations("async def writer():\n    " + body.replace("\n", "\n    "))


@pytest.mark.parametrize(
    "source",
    (
        "INSERT INTO ONLY public.conversations (messages) VALUES ($1)",
        'UPDATE ONLY "public"."conversations" SET messages = $1',
        "DELETE FROM ONLY conversations WHERE id = $1",
        "MERGE INTO ONLY public.conversations AS target USING staged AS source ON TRUE",
        "COPY public.conversations (messages) FROM STDIN",
        "TRUNCATE ONLY public.conversations",
        "TRUNCATE TABLE audit.events, ONLY public.conversations RESTART IDENTITY",
        'TRUNCATE TABLE ONLY "audit"."events", public.conversations, cache.entries CASCADE',
    ),
)
def test_conversation_mutation_syntax_guard_detects_supported_forms(source: str) -> None:
    assert _contains_conversation_mutation(source)


@pytest.mark.parametrize(
    "source",
    (
        "SELECT * FROM public.conversations",
        "UPDATE ONLY public.conversation_notes SET body = $1",
        "TRUNCATE TABLE public.audit_log, ONLY public.conversation_archive",
        "TRUNCATE TABLE public.audit_log; SELECT * FROM public.conversations",
        "DELETE FROM public.archived_conversations WHERE id = $1",
    ),
)
def test_conversation_mutation_syntax_guard_rejects_non_mutations(source: str) -> None:
    assert not _contains_conversation_mutation(source)


def test_runtime_syntax_guard_prunes_non_runtime_trees(tmp_path: Path) -> None:
    rogue = tmp_path / "runtime_writer.py"
    rogue.write_text("TRUNCATE TABLE audit.events, ONLY public.conversations")

    excluded_directories = (
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "cache",
        "caches",
        "fixtures",
        "generated",
        "generated_client",
        "migrations",
        "migrations_v2",
        "research/fixtures",
        "site-packages",
        "tests",
        "venv",
    )
    for directory in excluded_directories:
        excluded = tmp_path / directory / "nested" / "must_not_be_read.py"
        excluded.parent.mkdir(parents=True)
        excluded.write_bytes(b"\xffUPDATE ONLY public.conversations SET messages = $1")

    assert _find_runtime_conversation_mutations(tmp_path) == [rogue]


class _Transaction:
    def __init__(self, *, exit_error: Exception | None = None) -> None:
        self.exit_error = exit_error

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self.exit_error and exc_type is None:
            raise self.exit_error
        return False


@pytest.mark.asyncio
async def test_commit_failure_is_classified_as_outcome_unknown() -> None:
    conn = MagicMock()
    conn.transaction.return_value = _Transaction(exit_error=RuntimeError("lost ack"))

    with pytest.raises(ConversationCommitOutcomeUnknown):
        async with conversation_write_transaction(conn):
            pass


@pytest.mark.asyncio
async def test_body_failure_remains_a_confirmed_failure() -> None:
    conn = MagicMock()
    conn.transaction.return_value = _Transaction()

    with pytest.raises(ValueError, match="known body failure"):
        async with conversation_write_transaction(conn):
            raise ValueError("known body failure")


@pytest.mark.asyncio
async def test_release_failure_preserves_append_result_and_cache_invalidation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "SYNTHETIC_RELEASE_DETAIL"
    conn = MagicMock()
    conn.transaction.return_value = _Transaction()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": 41}])

    class _ReleaseFails:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                raise RuntimeError(marker)
            return False

    pool = MagicMock()
    pool.acquire.return_value = _ReleaseFails()
    invalidate = AsyncMock(return_value=0)
    monkeypatch.setattr(cache, "_invalidate_cache", invalidate)

    with caplog.at_level("ERROR"):
        result = await ConversationRepository(pool)._save_messages_locked(
            "synthetic-release-session",
            "synthetic-user",
            [{"role": "user", "content": "safe"}],
        )

    assert result == 41
    assert pool.acquire.call_count == 1
    assert [call.args[0] for call in invalidate.await_args_list] == [
        "zantara:conversations:*",
    ]
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text
    assert "synthetic-release-session" not in caplog.text


@pytest.mark.asyncio
async def test_release_cancellation_propagates_and_skips_cache_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MagicMock()
    conn.transaction.return_value = _Transaction()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"id": 42}])

    class _ReleaseCancelled:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, exc_type, exc, tb):
            if exc_type is None:
                raise asyncio.CancelledError
            return False

    pool = MagicMock()
    pool.acquire.return_value = _ReleaseCancelled()
    invalidate = AsyncMock(return_value=0)
    monkeypatch.setattr(cache, "_invalidate_cache", invalidate)

    with pytest.raises(asyncio.CancelledError):
        await ConversationRepository(pool)._save_messages_locked(
            "synthetic-cancel-session",
            "synthetic-user",
            [{"role": "user", "content": "safe"}],
        )

    assert pool.acquire.call_count == 1
    invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_bulk_rejects_unidentified_legacy_row_before_locking() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    with pytest.raises(ValueError, match="row_id"):
        await lock_conversation_identities(conn, [(None, None)])
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_session_requires_stable_row_identity() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()

    with pytest.raises(ValueError, match="row_id"):
        await lock_conversation_session(conn, None)

    await lock_conversation_session(conn, None, row_id=73)
    lock_call = conn.execute.await_args_list[-1]
    assert lock_call.args[2] == 73
