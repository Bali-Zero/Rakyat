"""Version-aware KBLI decisions remain intact through the three investment routes."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from backend.app.routers import dashboard, prime_v2
from backend.services.kbli_eye import KBLIEye
from backend.services.prime.prime_nexus_service import PrimeNexusService

DASHBOARD = "/api/dashboard/map/analyze-investment"
PRIME = "/api/prime/v2/analyze"
VALIDATE = "/api/dashboard/map/validate-property"


@pytest.fixture(scope="module")
def eye():
    return KBLIEye(
        str(
            Path(__file__).resolve().parents[6] / "data/source_documents/KBLI_2025_FINAL_CLEAN.json"
        )
    )


@pytest.fixture
def app(monkeypatch, eye, request):
    zone_code = getattr(request, "param", "K-1")
    app = FastAPI()
    app.include_router(dashboard.router)
    app.include_router(prime_v2.router)
    monkeypatch.setattr(dashboard, "_get_kbli_eye", lambda: eye)
    # Isolate external zoning and intel while running the real scoring and KBLI resolver.
    zone = {
        "zone": {"code": zone_code, "name": "Synthetic zone"},
        "location": {},
        "zone_intensity_requirements": [],
    }
    batara = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": {"territorials": {"geom": [zone]}}})
    )
    monkeypatch.setattr(
        dashboard,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(transport=batara, **kw)),
    )
    store = {}
    cache = AsyncMock()
    cache.get.side_effect = store.get
    cache.set.side_effect = lambda key, value, ttl: store.update({key: value})
    service = PrimeNexusService(cache_service=cache)
    service._kbli_eye = eye
    service.resolve = AsyncMock(
        return_value={
            "status": "found",
            "source": "batara_live",
            "zone_code": zone_code,
            "zone": {"zone_code": zone_code},
        }
    )
    service._search_intel = AsyncMock(return_value=[])
    app.state.prime_nexus_service = service
    return app


def payload(route, code, vintage=None):
    req = {"kbli_code": code}
    if route == PRIME:
        req.update(lat=-8.6, lng=115.2)
    if route == DASHBOARD:
        req.update(lat=-8.6, lon=115.2)
    if vintage is not None:
        req["vintage"] = vintage
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [VALIDATE, DASHBOARD, PRIME])
async def test_real_routes_keep_ambiguity_and_exact_current_result(app, route):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Same activity digits and coordinates, different editions. Prime must not serve the first cached answer twice.
        current = await client.post(route, json=payload(route, "56102"))
        legacy = await client.post(route, json=payload(route, "56102", "2020"))
    assert current.status_code == legacy.status_code == 200
    cur, old = current.json(), legacy.json()
    if route != VALIDATE:
        assert old["kbli"]["max_foreign_ownership"] is None
        assert old["verdict"] is None
        cur, old = cur["kbli"], old["kbli"]
    assert cur["resolution"]["vintage"] == "2025"
    assert old["state"] == "ERROR"
    assert old["reason_code"] == "AMBIGUOUS_KBLI_TRANSITION"
    assert old["candidates"] == ["56101", "56400"]
    assert old["input_code"] == "56102"
    assert old["vintage"] == "2020"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [VALIDATE, DASHBOARD, PRIME])
async def test_real_routes_show_inferred_historical_resolution(app, route):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(route, json=payload(route, "80100"))
    assert response.status_code == 200
    body = response.json() if route == VALIDATE else response.json()["kbli"]
    assert body.get("kbli_2025", body.get("code")) == "80110"
    assert body["resolution"]["input_code"] == "80100"
    assert body["resolution"]["vintage"] == "2020"
    assert body["resolution"]["inferred"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [DASHBOARD, PRIME])
async def test_real_routes_do_not_invent_cap_for_missing_code(app, route):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(route, json=payload(route, "00000", "2025"))
    assert response.status_code == 200
    body = response.json()
    assert body["kbli"]["reason_code"] == "CODE_NOT_FOUND"
    assert body["kbli"]["max_foreign_ownership"] is None
    assert body["verdict"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["LS"], indirect=True)
@pytest.mark.parametrize("route", [DASHBOARD, PRIME])
async def test_independent_zoning_prohibition_survives_kbli_ambiguity(app, route):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(route, json=payload(route, "56109", "2020"))
    assert response.status_code == 200
    projected = response.json()
    assert projected["kbli"]["candidates"] == ["56101", "56102", "56400"]
    assert projected["kbli"]["max_foreign_ownership"] is None
    assert projected["verdict"]["can_invest"] is False
    assert projected["verdict"]["risk_level"] == "HIGH"
    assert projected["verdict"]["hard_blocks"] == ["Zona LS non edificabile"]


@pytest.mark.asyncio
async def test_validate_property_uses_normal_dataset_discovery(monkeypatch):
    app = FastAPI()
    app.include_router(dashboard.router)
    monkeypatch.setattr(dashboard, "_kbli_eye", None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(VALIDATE, json=payload(VALIDATE, "55130", "2020"))
    assert response.status_code == 200
    body = response.json()
    assert body["kbli_2025"] == "55201"
    assert body["resolution"]["basis"] == "UNIQUE_BPS_2020"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["none", ""])
async def test_prime_cache_distinguishes_absent_code_from_invalid_string(app, code):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        without_code = await client.post(PRIME, json=payload(PRIME, None))
        invalid = await client.post(PRIME, json=payload(PRIME, code))
    assert without_code.status_code == invalid.status_code == 200
    assert without_code.json()["kbli"] is None
    assert without_code.json()["verdict"] is not None
    body = invalid.json()
    assert body["cache_hit"] is False
    assert body["kbli"]["reason_code"] == "CODE_NOT_FOUND"
    assert body["kbli"]["max_foreign_ownership"] is None
    assert body["verdict"] is None
