"""GET /api/prime/zones-geojson: the body it serves and what it keeps in memory."""

from unittest.mock import AsyncMock, patch

import orjson
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import prime

ROWS = [
    {
        "id": 1,
        "geometry": (
            '{"type":"Polygon","coordinates":'
            "[[[115.1234567891,-8.6543219876],[115.2,-8.7],[115.25,-8.6],[115.1234567891,-8.6543219876]]]}"
        ),
        "zoning_type": "K-1: Perdagangan dan Jasa",
        "kodszn": "K-1",
        "max_floors": 4,
        "max_height_meters": 20,
        "master_kdb": 60,
    },
    # No geometry and unparseable geometry: both rows are skipped.
    {
        "id": 2,
        "geometry": None,
        "zoning_type": "R-2",
        "kodszn": "R-2",
        "max_floors": None,
        "max_height_meters": None,
        "master_kdb": None,
    },
    {
        "id": 3,
        "geometry": "not json",
        "zoning_type": "R-2",
        "kodszn": "R-2",
        "max_floors": None,
        "max_height_meters": None,
        "master_kdb": None,
    },
    # Unknown zone code, no building data: default colour, height and KDB.
    {
        "id": 4,
        "geometry": (
            '{"type":"MultiPolygon","coordinates":[[[[115.3,-8.5],[115.31,-8.51],[115.32,-8.5],[115.3,-8.5]]]]}'
        ),
        "zoning_type": "ZZ-9: Zona Uji",
        "kodszn": None,
        "max_floors": None,
        "max_height_meters": None,
        "master_kdb": None,
    },
]

# The FeatureCollection the endpoint answers for ROWS, as the parsed structure
# it used to return and keep.
EXPECTED = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [115.123457, -8.654322],
                        [115.2, -8.7],
                        [115.25, -8.6],
                        [115.123457, -8.654322],
                    ]
                ],
            },
            "properties": {
                "zone_code": "K-1",
                "zone_type": "K-1: Perdagangan dan Jasa",
                "color": "#E8472A",
                "max_floors": 4,
                "max_height_meters": 20.0,
                "kdb": 60.0,
            },
        },
        {
            "type": "Feature",
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[[[115.3, -8.5], [115.31, -8.51], [115.32, -8.5], [115.3, -8.5]]]],
            },
            "properties": {
                "zone_code": "ZZ-9",
                "zone_type": "ZZ-9: Zona Uji",
                "color": "#6B7280",
                "max_floors": None,
                "max_height_meters": 15.0,
                "kdb": 50.0,
            },
        },
    ],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(prime, "_ZONES_GEOJSON_CACHE", None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit-test.invalid/none")
    app = FastAPI()
    app.include_router(prime.router)
    return TestClient(app)


def _connection(rows):
    conn = AsyncMock()
    conn.fetch.return_value = rows
    return conn


def test_body_is_byte_identical_to_the_compact_json_of_the_parsed_collection(client):
    with patch.object(prime.asyncpg, "connect", AsyncMock(return_value=_connection(ROWS))):
        response = client.get("/api/prime/zones-geojson")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == orjson.dumps(EXPECTED)


def test_cache_holds_the_serialized_body_and_the_second_request_reuses_it(client):
    connect = AsyncMock(return_value=_connection(ROWS))
    with patch.object(prime.asyncpg, "connect", connect):
        first = client.get("/api/prime/zones-geojson")
        second = client.get("/api/prime/zones-geojson")

    # The parsed FeatureCollection of the real table is several times the size
    # of its JSON and used to be re-encoded on every request.
    assert isinstance(prime._ZONES_GEOJSON_CACHE, bytes)
    assert prime._ZONES_GEOJSON_CACHE == first.content
    assert second.content == first.content
    assert connect.await_count == 1


def test_a_failed_load_answers_an_empty_collection_and_caches_nothing(client):
    with patch.object(prime.asyncpg, "connect", AsyncMock(side_effect=OSError("db down"))):
        response = client.get("/api/prime/zones-geojson")

    assert response.status_code == 200
    assert response.content == b'{"type":"FeatureCollection","features":[]}'
    assert prime._ZONES_GEOJSON_CACHE is None


def test_no_database_url_answers_an_empty_collection_without_connecting(client, monkeypatch):
    monkeypatch.delenv("DATABASE_URL")
    connect = AsyncMock()
    with patch.object(prime.asyncpg, "connect", connect):
        response = client.get("/api/prime/zones-geojson")

    assert response.content == b'{"type":"FeatureCollection","features":[]}'
    assert connect.await_count == 0
    assert prime._ZONES_GEOJSON_CACHE is None
