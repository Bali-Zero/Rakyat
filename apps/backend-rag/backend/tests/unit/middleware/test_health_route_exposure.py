"""The health router: probes are public and exact, everything else is behind the /metrics gate.

Fly's health check, the post-deploy check and the healer receptor read their
probe with no credentials, so each probe is a public entry of its own. The
router's other routes (Prometheus text, Qdrant and summary metrics, database,
Redis, collections, KG stats, detailed status) take the same gate as `/metrics`:
in production a caller needs an admin key, the Fly private network or the
normal API authentication.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.auth.public_endpoints import find_entry
from backend.app.routers import health as health_module
from backend.middleware.hybrid_auth import HybridAuthMiddleware

FLY_TOML = Path(__file__).resolve().parents[4] / "fly.toml"
PROBES = ("/health", "/health/", "/health/ready", "/health/live", "/health/garuda-outbox")


def _fly_check_paths() -> list[str]:
    config = tomllib.loads(FLY_TOML.read_text())
    found: list[str] = []

    def walk(node, under_checks: bool) -> None:
        if isinstance(node, dict):
            if under_checks and isinstance(node.get("path"), str):
                found.append(node["path"])
            for key, value in node.items():
                walk(value, under_checks or key in ("checks", "http_checks"))
        elif isinstance(node, list):
            for item in node:
                walk(item, under_checks)

    walk(config, False)
    return found


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ENVIRONMENT", "production")
    app = FastAPI()
    app.include_router(health_module.router)
    app.add_middleware(HybridAuthMiddleware)
    return TestClient(app, raise_server_exceptions=False)


def test_every_fly_check_path_is_public_and_exact():
    paths = _fly_check_paths()
    assert paths, f"no health-check path found in {FLY_TOML}"
    for path in paths:
        entry = find_entry(path, method="GET")
        assert entry is not None, f"fly.toml checks {path!r}, which is not public"
        assert entry.match == "exact", f"{path!r} is public through {entry.match} {entry.prefix!r}"
        assert entry.prefix == path


def test_prometheus_metrics_refuse_a_caller_without_credentials(client: TestClient):
    response = client.get("/health/metrics/prometheus")

    assert response.status_code in (401, 403)
    assert "zantara_" not in response.text


def test_every_non_probe_health_route_refuses_a_caller_without_credentials(client: TestClient):
    routes = sorted(
        {route.path for route in health_module.router.routes if "GET" in route.methods}
        - set(PROBES)
    )
    assert "/health/metrics/prometheus" in routes
    assert "/health/detailed" in routes
    open_routes = {path: client.get(path).status_code for path in routes}
    open_routes = {path: code for path, code in open_routes.items() if code not in (401, 403)}
    assert not open_routes, f"health routes answering without credentials: {open_routes}"


def test_probes_answer_without_credentials(client: TestClient):
    for path in PROBES:
        entry = find_entry(path, method="GET")
        assert entry is not None and entry.match == "exact", path
        assert client.get(path).status_code not in (401, 403), path
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/garuda-outbox").status_code == 200
