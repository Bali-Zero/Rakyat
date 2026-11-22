"""Witness for one fresh, authenticated shadow observation on the regular query route."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ShadowScope:
    query_digest: str
    witness: dict = field(default_factory=dict)
    attempted: bool = False


_current: ContextVar[ShadowScope | None] = ContextVar("voyage_http_shadow", default=None)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextmanager
def canary_request(
    query: str, *, enabled: bool, current_user: object, session_id: str | None
) -> Iterator[dict[str, Any] | None]:
    authorized = (
        enabled
        and re.fullmatch(r"voyage-canary-[a-f0-9]{32}", session_id or "") is not None
        and isinstance(current_user, dict)
        and current_user.get("role") in ("Founder", "admin")
        and isinstance(query, str)
        and 0 < len(query) <= 8192
    )
    scope = ShadowScope(digest(query)) if authorized else None
    token = _current.set(scope)
    try:
        yield scope.witness if scope else None
    finally:
        _current.reset(token)


def accepts_query(query: str) -> bool:
    scope = _current.get()
    return (
        scope is not None
        and isinstance(query, str)
        and len(query) <= 8192
        and scope.query_digest == digest(query)
    )


def reserve_observation(query: str) -> bool:
    scope = _current.get()
    if scope is None or scope.attempted or not accepts_query(query):
        return False
    scope.attempted = True
    return True


def record_observation(query: str, result: dict | None) -> None:
    scope = _current.get()
    if scope is None or not accepts_query(query) or not result:
        return
    if scope.witness.get("status") == "ok":
        return
    # Only the observer calls this function after processing trusted results.
    # No tool text, user-controlled fields, source IDs, or prompts are copied.
    fields = (
        "mode",
        "status",
        "query_model",
        "rerank_model",
        "index_sha256",
        "collection",
        "served_legal",
        "shadow_count",
        "overlap",
        "top1_same",
    )
    scope.witness.clear()
    scope.witness.update((name, result[name]) for name in fields if name in result)
    scope.witness["query_sha256"] = scope.query_digest
