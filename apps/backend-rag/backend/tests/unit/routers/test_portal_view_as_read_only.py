"""A staff view-as session reads the portal and writes nothing, whatever the route.

The rule lives where view-as is GRANTED: ``portal.get_current_client``, in its
impersonation branch, after the audit row. Every route that resolves its client
through it (as a dependency or awaited in its body, under any prefix, today or
later) is therefore covered with no per-route code.

These tests run the REAL resolver (no dependency override of it) on the REAL
``main_api.app``. The superuser identity reaches the resolver the way production
sets it: ``request.state.user`` (what ``HybridAuthMiddleware`` assigns). Here the
middleware is disabled by the ``disable_auth_middleware`` fixture and a thin ASGI
wrapper puts the user into ``scope["state"]``, which is what ``request.state``
reads.

Held, NOT closed by this round (rule 7: lkpm.py and crm_practices.py are
read-only): after this round, a staff view-as session can still submit and
approve an LKPM draft and upload a practice document; the portal is not yet
read-only. See ``NAMED_EXCEPTIONS``.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.routing import APIRoute, iter_route_contexts

from backend.app import main_api
from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import portal as portal_mod
from backend.app.routers.portal import (
    VIEW_AS_READ_ONLY_DETAIL,
    get_current_client,
    get_portal_service,
)

SUPERUSER_EMAIL = "viewer@example.com"
SAFE = frozenset({"GET", "HEAD", "OPTIONS"})
AUDIT_INSERT = "INSERT INTO security_audit_log"

# The mouth API client appends ``?as_client=`` to every path under these
# prefixes while impersonating (apps/mouth/src/lib/api/client.ts ~:409-424).
CLIENT_FACING_PREFIXES = ("/api/portal", "/api/v1/lkpm")

# Non-vacuity floor: every mutating route found on the head that resolves its
# client through get_current_client. The walk must find AT LEAST these.
EXPECTED_RESOLVER_ROUTES = frozenset(
    {
        ("POST", "/api/portal/company/{company_id}/select"),
        ("POST", "/api/portal/documents/upload"),
        ("DELETE", "/api/portal/documents/{document_id}"),
        ("POST", "/api/portal/documents/{document_id}/restore"),
        ("POST", "/api/portal/messages"),
        ("POST", "/api/portal/messages/{message_id}/read"),
        ("PATCH", "/api/portal/settings"),
        ("POST", "/api/portal/profile/reveal"),
        ("PATCH", "/api/portal/profile"),
        ("POST", "/api/portal/notifications/{notification_id}/read"),
        ("POST", "/api/portal/notifications/read-all"),
        ("PUT", "/api/portal/notifications/prefs"),
    }
)

_STAFF = "staff-authorized write: authorises the caller itself, never consults view-as"
_HELD = "HELD: admin-authorised write reachable in view-as (finding held, rule 7)"

# Mutating routes under the client-facing prefixes that do NOT resolve their
# client through get_current_client, each with the reason the code gives.
NAMED_EXCEPTIONS: dict[tuple[str, str], str] = {
    ("POST", "/api/portal/invite/send"): _STAFF,
    ("POST", "/api/portal/invite/resend/{client_id}"): _STAFF,
    ("POST", "/api/portal/invite/complete"): "public invitation-token onboarding, no session",
    ("POST", "/api/v1/lkpm/config"): _STAFF,
    ("POST", "/api/v1/lkpm/submit-data"): _HELD,
    ("POST", "/api/v1/lkpm/sync-jurnal/{client_id}"): _STAFF,
    ("POST", "/api/v1/lkpm/validate/{draft_id}"): _STAFF,
    ("POST", "/api/v1/lkpm/approve/{draft_id}"): _HELD,
    ("POST", "/api/v1/lkpm/mark-submitted/{draft_id}"): _STAFF,
    ("POST", "/api/v1/lkpm/upload-receipt/{draft_id}"): _STAFF,
    ("PUT", "/api/v1/lkpm/reports/{draft_id}/assign"): _STAFF,
    ("POST", "/api/v1/lkpm/ready-pack/{client_id}"): _STAFF,
}

ALLOWLISTED = ("POST", "/api/portal/messages/{message_id}/read")

BACKEND_DIR = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Walkers, written once (over the production mounts, never a hard-coded list)
# ---------------------------------------------------------------------------


def _calls(dependant) -> set:
    found = set()
    for dep in dependant.dependencies:
        found.add(dep.call)
        found |= _calls(dep)
    return found


def _api_routes(app):
    """Yield (method, effective_path, dependant) for every mutating APIRoute."""
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute):
            continue
        for method in sorted((ctx.methods or set()) - SAFE):
            yield method, ctx.path, ctx.dependant


def resolver_routes(app) -> set[tuple[str, str]]:
    """Mutating routes whose dependant tree contains get_current_client."""
    return {
        (method, path)
        for method, path, dependant in _api_routes(app)
        if get_current_client in _calls(dependant)
    }


def client_facing_writes(app) -> set[tuple[str, str]]:
    """Mutating routes under a client-facing prefix, WHATEVER their dependencies."""
    return {
        (method, path)
        for method, path, _dep in _api_routes(app)
        if path.startswith(CLIENT_FACING_PREFIXES)
    }


def census_problems(app, named=NAMED_EXCEPTIONS) -> list[str]:
    covered = resolver_routes(app)
    writes = client_facing_writes(app)
    problems = [
        f"neither covered nor named: {method} {path}"
        for method, path in sorted(writes - covered - set(named))
    ]
    problems += [
        f"stale exception: {method} {path}" for method, path in sorted(set(named) - writes)
    ]
    return problems


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "5", path)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class RecordingPool:
    """asyncpg pool double: serves the client row, records every statement."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        conn = MagicMock()

        async def _answer(sql, *args):
            self.statements.append(" ".join(sql.split()))
            if "FROM team_members" in sql:
                return {
                    "id": "login-1",
                    "email": "owner@example.com",
                    "full_name": "Own Client",
                    "linked_client_id": 42,
                    "portal_access": True,
                }
            if "FROM clients" in sql and "LOWER(email)" in sql:
                return {"id": 7, "email": SUPERUSER_EMAIL, "full_name": "Own Row"}
            if "FROM clients" in sql:
                return {"id": 42, "email": "owner@example.com", "full_name": "Own Client"}
            return None

        async def _execute(sql, *args):
            self.statements.append(" ".join(sql.split()))

        conn.fetchrow = AsyncMock(side_effect=_answer)
        conn.fetchval = AsyncMock(side_effect=_answer)
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock(side_effect=_execute)

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        self.acquire = MagicMock(side_effect=lambda: _Ctx())

    def writes(self) -> list[str]:
        return [
            s for s in self.statements if re.match(r"(INSERT|UPDATE|DELETE)\b", s, re.IGNORECASE)
        ]

    def writes_after_audit(self) -> list[str]:
        return [s for s in self.writes() if AUDIT_INSERT not in s]


class _AsUser:
    """ASGI wrapper: the identity HybridAuthMiddleware would put on request.state."""

    def __init__(self, app, user: dict) -> None:
        self.app = app
        self.user = user

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope["state"] = {**scope.get("state", {}), "user": self.user}
        await self.app(scope, receive, send)


SUPERUSER = {"id": "staff-1", "email": SUPERUSER_EMAIL, "role": "admin"}
CLIENT_LOGIN = {"id": "login-1", "email": "owner@example.com", "role": "client"}


@pytest.fixture
def stack(monkeypatch, disable_auth_middleware):
    """The real main_api.app with a recording pool; overrides restored after."""
    monkeypatch.setattr(portal_mod, "_superuser_emails", lambda: frozenset({SUPERUSER_EMAIL}))
    app = main_api.app
    pool = RecordingPool()
    service = AsyncMock()
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_database_pool] = lambda: pool
    app.dependency_overrides[get_portal_service] = lambda: service
    try:
        yield SimpleNamespace(app=app, pool=pool, service=service)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


async def _fire(app, user, method, path, *, view_as=True, json_body=True):
    transport = httpx.ASGITransport(app=_AsUser(app, user))
    url = _concrete(path) + ("?as_client=42" if view_as else "")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        kwargs = {"json": {}} if json_body else {}
        return await http.request(method, url, **kwargs)


@contextmanager
def mounted(app, *routers):
    """Inject routers into the production app; restore routes, schema cache."""
    saved_routes = list(app.router.routes)
    saved_schema = app.openapi_schema
    try:
        for r in routers:
            app.include_router(r)
        # The RAG proxy catch-all is registered LAST in production; an
        # injected route must sit before it to be reachable.
        added = app.router.routes[len(saved_routes) :]
        app.router.routes[:] = added + saved_routes
        yield
    finally:
        app.router.routes[:] = saved_routes
        app.openapi_schema = saved_schema
        assert list(app.router.routes) == saved_routes


def _router(prefix: str, method: str, path: str, *, dep) -> APIRouter:
    router = APIRouter(prefix=prefix)

    async def handler(_who=Depends(dep)):
        return {"ok": True}

    router.add_api_route(path, handler, methods=[method])
    return router


def _body_router(prefix: str, path: str) -> APIRouter:
    router = APIRouter(prefix=prefix)

    async def handler(request: Request, db_pool=Depends(get_database_pool)):
        await get_current_client(request, db_pool=db_pool)
        return {"ok": True}

    router.add_api_route(path, handler, methods=["POST"])
    return router


# ---------------------------------------------------------------------------
# 1. Every route that resolves through get_current_client refuses a write
# ---------------------------------------------------------------------------


def test_the_walk_finds_the_whole_known_write_surface():
    found = resolver_routes(main_api.app)
    assert EXPECTED_RESOLVER_ROUTES <= found, sorted(EXPECTED_RESOLVER_ROUTES - found)


async def test_every_resolver_route_refuses_a_write_under_view_as(stack):
    routes = sorted(resolver_routes(stack.app))
    assert routes
    for method, path in routes:
        before = len(stack.pool.statements)
        stack.service.reset_mock()
        response = await _fire(stack.app, SUPERUSER, method, path)
        if (method, path) == ALLOWLISTED:
            assert response.status_code == 200, (method, path, response.text)
            assert response.json() == {"success": True, "message": "Message marked as read"}
        else:
            assert response.status_code == 403, (method, path, response.status_code)
            assert response.json()["detail"] == VIEW_AS_READ_ONLY_DETAIL, (method, path)
        # The handler was never entered: no service call, and the only write
        # that reached the pool is the impersonation audit row.
        assert stack.service.method_calls == [], (method, path)
        assert stack.pool.writes_after_audit() == [], (method, path)
        assert len(stack.pool.statements) > before


async def test_a_refused_write_still_leaves_its_audit_row(stack):
    response = await _fire(stack.app, SUPERUSER, "PATCH", "/api/portal/profile")
    assert response.status_code == 403
    audit = [s for s in stack.pool.statements if AUDIT_INSERT in s]
    assert len(audit) == 1, stack.pool.statements
    # The attempt is audited before it is refused.
    assert stack.pool.statements.index(audit[0]) == len(stack.pool.statements) - 1


# ---------------------------------------------------------------------------
# 2. The census of client-facing writers (independent of the resolver)
# ---------------------------------------------------------------------------


def test_every_client_facing_write_is_covered_or_named():
    assert census_problems(main_api.app) == []


def test_the_named_exceptions_are_exactly_the_uncovered_writes():
    uncovered = client_facing_writes(main_api.app) - resolver_routes(main_api.app)
    assert uncovered == set(NAMED_EXCEPTIONS)


def test_the_two_held_lkpm_writes_are_named_as_held():
    held = {key for key, reason in NAMED_EXCEPTIONS.items() if reason.startswith("HELD")}
    assert held == {
        ("POST", "/api/v1/lkpm/submit-data"),
        ("POST", "/api/v1/lkpm/approve/{draft_id}"),
    }


def test_a_write_with_another_dependency_under_portal_fails_the_census():
    with mounted(main_api.app, _router("/api/portal", "POST", "/x-alt", dep=get_current_user)):
        assert ("POST", "/api/portal/x-alt") not in resolver_routes(main_api.app)
        problems = census_problems(main_api.app)
    assert problems == ["neither covered nor named: POST /api/portal/x-alt"]
    assert census_problems(main_api.app) == []


def test_a_write_with_another_dependency_under_lkpm_fails_the_census():
    with mounted(main_api.app, _router("/api/v1/lkpm", "POST", "/x-alt", dep=get_current_user)):
        problems = census_problems(main_api.app)
    assert problems == ["neither covered nor named: POST /api/v1/lkpm/x-alt"]


def test_a_stale_named_exception_fails_the_census():
    named = {**NAMED_EXCEPTIONS, ("POST", "/api/portal/gone"): "removed route"}
    assert census_problems(main_api.app, named) == ["stale exception: POST /api/portal/gone"]


def test_only_portal_and_lkpm_read_the_as_client_query_parameter():
    """Census of readers. lkpm.py reads it in a GET only (draft preview); its
    writes are the held finding named in NAMED_EXCEPTIONS."""
    pattern = re.compile(r"""["']as_client["']|(?<![?\w])as_client\s*[:=]""")
    readers = set()
    for path in BACKEND_DIR.rglob("*.py"):
        if "tests" in path.relative_to(BACKEND_DIR).parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("#") and pattern.search(line):
                readers.add(path.name)
    assert readers == {"portal.py", "lkpm.py"}


# ---------------------------------------------------------------------------
# 3. Guilt by mutation: routes injected into the production app
# ---------------------------------------------------------------------------


async def test_a_new_post_under_portal_is_found_and_refused(stack):
    with mounted(stack.app, _router("/api/portal", "POST", "/x-new", dep=get_current_client)):
        assert ("POST", "/api/portal/x-new") in resolver_routes(stack.app)
        refused = await _fire(stack.app, SUPERUSER, "POST", "/api/portal/x-new")
    assert refused.status_code == 403
    assert refused.json()["detail"] == VIEW_AS_READ_ONLY_DETAIL


async def test_a_new_post_under_another_prefix_is_found_and_refused(stack):
    with mounted(stack.app, _router("/api/x-test", "POST", "/new", dep=get_current_client)):
        assert ("POST", "/api/x-test/new") in resolver_routes(stack.app)
        refused = await _fire(stack.app, SUPERUSER, "POST", "/api/x-test/new")
    assert refused.status_code == 403


async def test_a_post_that_awaits_the_resolver_in_its_body_is_refused(stack):
    with mounted(stack.app, _body_router("/api/x-test", "/body")):
        # Not in the dependency walk: the rule itself is what refuses it.
        assert ("POST", "/api/x-test/body") not in resolver_routes(stack.app)
        refused = await _fire(stack.app, SUPERUSER, "POST", "/api/x-test/body")
    assert refused.status_code == 403
    assert refused.json()["detail"] == VIEW_AS_READ_ONLY_DETAIL


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
async def test_the_allowlist_is_not_matched_by_template_alone(stack, method):
    router = _router("/api/portal", method, "/messages/{message_id}/read", dep=get_current_client)
    with mounted(stack.app, router):
        refused = await _fire(stack.app, SUPERUSER, method, "/api/portal/messages/{id}/read")
    assert refused.status_code == 403


async def test_the_allowlist_is_not_matched_by_prefix(stack):
    router = _router("/api/portal", "POST", "/messages/{message_id}/unread", dep=get_current_client)
    with mounted(stack.app, router):
        refused = await _fire(stack.app, SUPERUSER, "POST", "/api/portal/messages/{id}/unread")
    assert refused.status_code == 403


async def test_the_allowlisted_write_answers_its_usual_shape_and_writes_nothing(stack):
    method, path = ALLOWLISTED
    response = await _fire(stack.app, SUPERUSER, method, path)
    assert response.status_code == 200
    assert response.json() == {"success": True, "message": "Message marked as read"}
    assert stack.service.method_calls == []
    assert stack.pool.writes_after_audit() == []


# ---------------------------------------------------------------------------
# 4. Innocence
# ---------------------------------------------------------------------------


async def test_a_get_under_view_as_is_served_and_only_audited(stack):
    with mounted(stack.app, _router("/api/x-test", "GET", "/read", dep=get_current_client)):
        response = await _fire(stack.app, SUPERUSER, "GET", "/api/x-test/read")
    assert response.status_code == 200
    assert stack.pool.writes_after_audit() == []
    assert len([s for s in stack.pool.statements if AUDIT_INSERT in s]) == 1


async def test_a_superuser_without_as_client_still_writes(stack):
    with mounted(stack.app, _router("/api/x-test", "POST", "/new", dep=get_current_client)):
        response = await _fire(stack.app, SUPERUSER, "POST", "/api/x-test/new", view_as=False)
    assert response.status_code == 200


async def test_a_client_login_still_writes_as_before(stack):
    with mounted(stack.app, _router("/api/x-test", "POST", "/new", dep=get_current_client)):
        response = await _fire(stack.app, CLIENT_LOGIN, "POST", "/api/x-test/new", view_as=False)
    assert response.status_code == 200


async def test_a_client_login_with_an_as_client_query_is_not_impersonating(stack):
    with mounted(stack.app, _router("/api/x-test", "POST", "/new", dep=get_current_client)):
        response = await _fire(stack.app, CLIENT_LOGIN, "POST", "/api/x-test/new", view_as=True)
    assert response.status_code == 200
    assert [s for s in stack.pool.statements if AUDIT_INSERT in s] == []


# ---------------------------------------------------------------------------
# 5. Direct calls: a request double with a missing or unhashable method fails closed
# ---------------------------------------------------------------------------


def _double(method, scope=None):
    return SimpleNamespace(
        state=SimpleNamespace(user=SUPERUSER),
        query_params={"as_client": "42"},
        url=SimpleNamespace(path="/api/portal/profile"),
        method=method,
        scope=scope if scope is not None else {},
    )


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr(portal_mod, "_superuser_emails", lambda: frozenset({SUPERUSER_EMAIL}))
    return RecordingPool()


@pytest.mark.parametrize("bad_method", [None, ["POST"], {"POST": 1}, 7])
async def test_a_non_string_method_under_view_as_is_refused_never_a_type_error(direct, bad_method):
    with pytest.raises(HTTPException) as refused:
        await get_current_client(_double(bad_method), db_pool=direct)
    assert refused.value.status_code == 403
    assert refused.value.detail == VIEW_AS_READ_ONLY_DETAIL


@pytest.mark.parametrize("bad_template", [None, ["x"], {"a": 1}])
async def test_a_non_string_template_under_view_as_is_refused(direct, bad_template):
    request = _double("POST", {"route": SimpleNamespace(path=bad_template)})
    with pytest.raises(HTTPException) as refused:
        await get_current_client(request, db_pool=direct)
    assert refused.value.status_code == 403


async def test_a_get_double_is_served_under_view_as(direct):
    result = await get_current_client(_double("GET"), db_pool=direct)
    assert result["impersonating"] is True
    assert result["client_id"] == 42


async def test_the_allowlisted_double_is_served_under_view_as(direct):
    method, path = ALLOWLISTED
    result = await get_current_client(
        _double(method, {"route": SimpleNamespace(path=path)}), db_pool=direct
    )
    assert result["impersonating"] is True


async def test_a_post_double_without_a_route_is_refused_after_the_audit_row(direct):
    with pytest.raises(HTTPException) as refused:
        await get_current_client(_double("POST"), db_pool=direct)
    assert refused.value.status_code == 403
    assert any(AUDIT_INSERT in s for s in direct.statements)
