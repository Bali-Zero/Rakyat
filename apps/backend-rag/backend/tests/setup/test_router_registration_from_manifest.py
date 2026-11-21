"""The include_* functions iterate the manifest (Imperator lot 19).

Fake modules and a fake manifest: no app boot, no env needed.
"""

from __future__ import annotations

import importlib
import types

import pytest
from fastapi import APIRouter, FastAPI

from backend.app.setup import router_manifest as rm
from backend.app.setup import router_registration as rr


def _fake_module(path: str) -> types.SimpleNamespace:
    router = APIRouter()
    router.add_api_route(f"/{path.rsplit('.', 1)[-1]}", lambda: None)
    return types.SimpleNamespace(router=router, other=router)


@pytest.fixture()
def wired(monkeypatch):
    imported: list[str] = []

    def fake_import(path: str):
        imported.append(path)
        return _fake_module(path)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.setattr(rm, "_get_api_v1_prefix", lambda: "/v1")
    off = lambda: False  # noqa: E731
    entries = (
        rm.RouterEntry(name="a", process_groups=rm._API),
        rm.RouterEntry(name="b", process_groups=rm._BOTH, prefix="__API_V1__"),
        rm.RouterEntry(name="c", process_groups=rm._RAG, prefix="/p"),
        rm.RouterEntry(name="d", process_groups=rm._API, condition=off),
        rm.RouterEntry(name="e", process_groups=rm._API, condition=off, import_when_disabled=True),
        rm.RouterEntry(name="f", process_groups=rm._API, attr="other"),
        rm.RouterEntry(name="g", process_groups=rm._API, import_path="x.y.z"),
    )
    monkeypatch.setattr(rm, "ROUTER_MANIFEST", entries)
    monkeypatch.setattr(
        rm,
        "_ORDERS",
        {"full": ["g", "c"], "api": ["f:other", "b", "a"], "rag": []},
    )
    return imported


def _bare_app() -> FastAPI:
    return FastAPI(openapi_url=None, docs_url=None, redoc_url=None)


def _walk(app: FastAPI) -> list[str]:
    from fastapi.routing import iter_route_contexts

    return [c.path for c in iter_route_contexts(app.routes)]


def test_listed_entries_come_first_then_manifest_order(wired):
    app = _bare_app()
    rr.include_light_routers(app)
    assert _walk(app) == ["/f", "/v1/b", "/a", "/z"]


def test_condition_false_skips_the_include_and_import_when_disabled_keeps_the_import(wired):
    rr.include_light_routers(_bare_app())
    assert "backend.app.routers.d" not in wired
    assert "backend.app.routers.e" in wired


def test_import_path_override_and_prefix_literal(wired):
    app = _bare_app()
    rr.include_routers(app)
    assert wired[0] == "x.y.z"
    assert "/p/c" in _walk(app)


def test_heavy_target_takes_only_rag_group(wired):
    app = _bare_app()
    rr.include_heavy_routers(app)
    assert _walk(app) == ["/v1/b", "/p/c"]


def test_real_order_lists_name_only_declared_routers_of_their_target():
    assert rm.validate_manifest() == []
