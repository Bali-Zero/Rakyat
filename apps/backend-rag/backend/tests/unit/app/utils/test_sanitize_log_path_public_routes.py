"""Every public route that carries a value in its path is logged without it.

`sanitize_log_path` feeds log lines, metric labels and rate-limit keys. A public
route (no credentials needed to reach it) whose path holds a token, a hash or a
record id would otherwise write that value wherever the path is recorded. The
mounted application is the reference: a new public route with a parameter fails
here until the sanitizer knows it, and a fixed route is never rewritten.
"""

from __future__ import annotations

import re

import pytest

from backend.app.auth.public_endpoints import PUBLIC_ENDPOINTS, find_entry
from backend.app.utils.logging_utils import _LITERAL_SIBLINGS, sanitize_log_path

SYNTHETIC = "synthval7f3a9c2e"
_PARAM = re.compile(r"\{[^/}]+\}")


@pytest.fixture(scope="module")
def mounted_routes() -> list[tuple[str, frozenset[str]]]:
    from fastapi import FastAPI

    from backend.app.setup.route_walk import iter_leaf_routes
    from backend.app.setup.router_registration import include_routers

    app = FastAPI()
    try:
        include_routers(app)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"include_routers failed in this env: {exc}")
    routes = []
    for route in iter_leaf_routes(app):
        path = getattr(route, "path", None)
        if path:
            routes.append((path, frozenset(getattr(route, "methods", None) or {"GET"})))
    if not routes:
        pytest.skip("no mounted routes collected in this env")
    return routes


def _public(path: str, methods: frozenset[str]) -> bool:
    return any(find_entry(path, method=m) is not None for m in methods)


def test_every_public_route_with_a_path_parameter_loses_its_value(mounted_routes):
    leaked = []
    covered = 0
    for template, methods in mounted_routes:
        if "{" not in template:
            continue
        concrete = _PARAM.sub(SYNTHETIC, template)
        if not _public(concrete, methods):
            continue
        covered += 1
        if SYNTHETIC in sanitize_log_path(concrete):
            leaked.append(template)
    assert covered >= 20, f"only {covered} public routes with a parameter were found"
    assert not leaked, "public routes whose path value reaches the log:\n  " + "\n  ".join(
        sorted(set(leaked))
    )


def test_every_registry_entry_with_a_parameter_loses_its_value():
    leaked = []
    for entry in PUBLIC_ENDPOINTS:
        if entry.match == "template":
            concrete = _PARAM.sub(SYNTHETIC, entry.prefix)
        elif entry.match == "prefix" and entry.prefix.endswith("/"):
            concrete = entry.prefix + SYNTHETIC
        else:
            continue
        if SYNTHETIC in sanitize_log_path(concrete):
            leaked.append(entry.prefix)
    assert not leaked, f"registry entries whose path value reaches the log: {leaked}"


def test_fixed_routes_are_logged_unchanged(mounted_routes):
    rewritten = [
        (path, sanitize_log_path(path))
        for path, _ in mounted_routes
        if "{" not in path and sanitize_log_path(path) != path
    ]
    assert not rewritten, f"fixed routes rewritten by the sanitizer: {rewritten}"


SENTINEL = "sentinelq7zk"


def _descendants(prefix: str, match: str):
    if "{" in prefix:
        concrete = _PARAM.sub(SENTINEL, prefix)
        yield concrete
        yield concrete + "/"
        yield concrete + "/extra"
        yield concrete.replace(SENTINEL, SENTINEL + "%2Fextra", 1)
        base = prefix[: prefix.index("{")]
    elif match == "prefix":
        base = prefix
    else:
        return
    root = base.rstrip("/")
    yield f"{root}/{SENTINEL}"
    yield f"{root}/{SENTINEL}/"
    yield f"{root}/{SENTINEL}/extra"
    yield f"{root}/extra/{SENTINEL}"
    yield f"{root}/{SENTINEL}%2Fextra.html"
    yield f"{root}/{SENTINEL}%2fextra"
    yield f"{root}//{SENTINEL}//extra"
    yield f"{root}/{SENTINEL}/extra".upper()
    if not base.endswith("/"):
        yield f"{root}{SENTINEL}"


def test_no_descendant_of_a_public_prefix_or_parameter_keeps_a_caller_value():
    leaked = []
    checked = 0
    for entry in PUBLIC_ENDPOINTS:
        for path in _descendants(entry.prefix, entry.match):
            checked += 1
            if SENTINEL in sanitize_log_path(path).lower():
                leaked.append(path)
    assert checked >= 300, f"only {checked} descendants were built"
    assert not leaked, "descendants recorded with their value:\n  " + "\n  ".join(leaked)


def test_every_named_fixed_route_is_mounted(mounted_routes):
    mounted = {path for path, _ in mounted_routes}
    assert not _LITERAL_SIBLINGS - mounted, sorted(_LITERAL_SIBLINGS - mounted)
