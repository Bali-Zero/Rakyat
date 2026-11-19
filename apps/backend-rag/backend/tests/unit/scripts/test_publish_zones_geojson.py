"""Zones GeoJSON: deterministic build, validator rules, and the publish CLI."""

import hashlib
import json
import random

import orjson
import pytest

from backend.scripts import publish_zones_geojson as cli
from backend.services.prime.zones_geojson import (
    build_zones_geojson_body,
    validate_zones_geojson,
)

RING = [[115.1, -8.6], [115.2, -8.6], [115.2, -8.7], [115.1, -8.6]]


def row(i: int, **over) -> dict:
    base = {
        "id": f"id-{i:03d}",
        "geometry": json.dumps({"type": "Polygon", "coordinates": [RING]}),
        "zoning_type": "K-1: Perdagangan",
        "kodszn": "K-1",
        "max_floors": 4,
        "max_height_meters": 20,
        "master_kdb": 60,
    }
    return {**base, **over}


def feature(**props_over) -> dict:
    props = {
        "zone_code": "K-1",
        "zone_type": "K-1: x",
        "color": "#E8472A",
        "max_floors": 4,
        "max_height_meters": 20.0,
        "kdb": 60.0,
    }
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [RING]},
        "properties": {**props, **props_over},
    }


def collection(*features) -> bytes:
    return orjson.dumps({"type": "FeatureCollection", "features": list(features)})


def with_geometry(geometry: dict) -> dict:
    f = feature()
    f["geometry"] = geometry
    return f


def test_valid_body_passes():
    body, count = build_zones_geojson_body([row(1), row(2)])
    assert count == 2
    assert validate_zones_geojson(body, None) == []


def test_build_is_deterministic_whatever_the_row_order():
    rows = [row(i) for i in range(20)]
    expected = build_zones_geojson_body(rows)[0]
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert build_zones_geojson_body(shuffled)[0] == expected


def test_build_skips_empty_and_broken_geometry():
    empty = json.dumps({"type": "Polygon", "coordinates": []})
    body, count = build_zones_geojson_body(
        [row(1), row(2, geometry=None), row(3, geometry="x"), row(4, geometry=empty)]
    )
    assert count == 1
    assert body.count(b'"type":"Feature"') == 1


BAD = {
    "not json": b"{",
    "not a collection": b'{"type":"Feature"}',
    "no features": collection(),
    "feature type": collection({**feature(), "type": "Thing"}),
    "geometry type": collection(with_geometry({"type": "Point", "coordinates": [115.1, -8.6]})),
    "short ring": collection(
        with_geometry({"type": "Polygon", "coordinates": [RING[:3]]}),
    ),
    "open ring": collection(
        with_geometry({"type": "Polygon", "coordinates": [RING[:3] + [[115.3, -8.6]]]}),
    ),
    "outside lng": collection(
        with_geometry(
            {
                "type": "Polygon",
                "coordinates": [[[117, -8.6], [115.2, -8.6], [115.2, -8.7], [117, -8.6]]],
            }
        )
    ),
    "outside lat": collection(
        with_geometry(
            {
                "type": "Polygon",
                "coordinates": [[[115.1, -5], [115.2, -8.6], [115.2, -8.7], [115.1, -5]]],
            }
        )
    ),
    "three coordinates": collection(
        with_geometry(
            {"type": "Polygon", "coordinates": [[[115.1, -8.6, 0], *RING[1:3], [115.1, -8.6, 0]]]}
        )
    ),
    "extra property": collection(feature(extra=1)),
    "empty zone_code": collection(feature(zone_code="")),
    "zone_type not str": collection(feature(zone_type=3)),
    "bad color": collection(feature(color="red")),
    "short color": collection(feature(color="#FFF")),
    "float floors": collection(feature(max_floors=2.5)),
    "bool floors": collection(feature(max_floors=True)),
    "null height": collection(feature(max_height_meters=None)),
    "bool kdb": collection(feature(kdb=True)),
}


@pytest.mark.parametrize("body", BAD.values(), ids=BAD.keys())
def test_validator_rejects_each_rule(body):
    assert validate_zones_geojson(body, None)


def test_validator_accepts_the_edges_the_rules_allow():
    multi = with_geometry({"type": "MultiPolygon", "coordinates": [[RING], [RING]]})
    nullable = feature(max_floors=None, kdb=0.5, max_height_meters=3)
    assert validate_zones_geojson(collection(multi, nullable), None) == []


def test_shrink_guard_boundary():
    body = collection(*[feature() for _ in range(95)])
    assert validate_zones_geojson(body, 100) == []
    assert validate_zones_geojson(body, 101)
    assert validate_zones_geojson(body, None) == []


def test_errors_are_capped_with_a_total():
    errors = validate_zones_geojson(collection(*[feature(color="x") for _ in range(50)]), None)
    assert len(errors) == 21
    assert errors[-1] == "50 errors in total"


@pytest.fixture
def mouth(tmp_path):
    (tmp_path / "src/data").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def rows_file(tmp_path):
    def make(rows, name="rows.jsonl"):
        path = tmp_path / name
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return str(path)

    return make


def manifest(mouth):
    return json.loads((mouth / "src/data/prime-zones.json").read_text())


def publish(mouth, rows_path, *extra):
    return cli.main(["--rows", rows_path, "--mouth-dir", str(mouth), *extra])


def test_publish_writes_file_and_manifest(mouth, rows_file, capsys):
    assert publish(mouth, rows_file([row(1), row(2)])) == 0
    m = manifest(mouth)
    body = (mouth / "public/data/prime" / m["path"].rsplit("/", 1)[1]).read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    assert "sha256" not in m
    assert m["path"] == f"/data/prime/zones-{digest[:16]}.json"
    assert (m["features"], m["bytes"], m["previous"]) == (2, len(body), None)
    assert json.loads(capsys.readouterr().out)["status"] == "published"


def test_publish_keeps_previous_and_deletes_older(mouth, rows_file):
    data = mouth / "public/data/prime"
    paths = []
    for n in (1, 2, 3):
        assert publish(mouth, rows_file([row(i) for i in range(n)])) == 0
        paths.append(manifest(mouth)["path"].rsplit("/", 1)[1])
    assert manifest(mouth)["previous"] == f"/data/prime/{paths[1]}"
    assert sorted(p.name for p in data.glob("zones-*.json")) == sorted(paths[1:])
    assert not list(data.glob("*.tmp"))


def test_unchanged_sha_changes_nothing(mouth, rows_file, capsys):
    path = rows_file([row(1)])
    publish(mouth, path)
    before = (mouth / "src/data/prime-zones.json").read_bytes()
    capsys.readouterr()
    assert publish(mouth, path) == 0
    assert (mouth / "src/data/prime-zones.json").read_bytes() == before
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"


def test_invalid_input_exits_2_and_writes_nothing(mouth, rows_file):
    assert publish(mouth, rows_file([row(1, geometry=None)])) == 2
    assert not (mouth / "public").exists()
    assert not (mouth / "src/data/prime-zones.json").exists()


def test_shrink_keeps_the_last_valid_version(mouth, rows_file):
    publish(mouth, rows_file([row(i) for i in range(20)]))
    before = manifest(mouth)
    assert publish(mouth, rows_file([row(i) for i in range(5)], "small.jsonl")) == 2
    assert manifest(mouth) == before


def test_check_exit_codes_and_no_writes(mouth, rows_file):
    first = rows_file([row(1)])
    assert publish(mouth, first, "--check") == 10
    assert not (mouth / "public").exists()
    publish(mouth, first)
    assert publish(mouth, first, "--check") == 0
    assert publish(mouth, rows_file([row(1), row(2)], "b.jsonl"), "--check") == 10
    assert publish(mouth, rows_file([row(1, geometry=None)], "c.jsonl"), "--check") == 2


def test_print_sql_wraps_the_query(capsys):
    assert cli.main(["--print-sql"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT row_to_json(t)::text FROM (")
    assert "l.id" in out and out.rstrip().endswith(") t")
