"""Zones GeoJSON: deterministic build, validator rules, and the publish CLI."""

import hashlib
import json
import random

import orjson
import pytest

from backend.scripts import publish_zones_geojson as cli
from backend.services.prime.zones_geojson import (
    GRID,
    build_zones_geojson,
    build_zones_geojson_body,
    growth_errors,
    on_grid,
    ring_doubled_area,
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


COLLAPSED = [[115.1, -8.6]] * 4
SEGMENT = [[115.1, -8.6], [115.2, -8.6], [115.1, -8.6], [115.1, -8.6]]
COLLINEAR = [[115.1, -8.6], [115.2, -8.6], [115.3, -8.6], [115.1, -8.6]]
HOLE = [[115.14, -8.62], [115.16, -8.62], [115.16, -8.64], [115.14, -8.62]]
# Two triangles of equal area traversed in opposite directions: signed areas cancel exactly.
FIGURE_EIGHT = [
    [115.1, -8.6],
    [115.2, -8.7],
    [115.2, -8.6],
    [115.1, -8.7],
    [115.1, -8.6],
]
SLIVER = [[115.1, -8.6], [115.100001, -8.6], [115.1, -8.599999], [115.1, -8.6]]
_R3 = [[(114100000 + 100 * i) / 1000000, (-9100000 + 100 * i) / 1000000] for i in range(1000)]
R3_RING = [*_R3, _R3[0]]


def poly(*rings) -> str:
    return json.dumps({"type": "Polygon", "coordinates": list(rings)})


def multi(*polygons) -> str:
    return json.dumps({"type": "MultiPolygon", "coordinates": list(polygons)})


def raw(text: str) -> str:
    return text


# One row of the table in zones_geojson.py per entry:
# geometry column -> (features, malformed, degenerate rings dropped).
TABLE = {
    "NULL": (None, (0, 0, 0)),
    "empty string": ("", (0, 0, 0)),
    "not JSON": (raw("x"), (0, 1, 0)),
    "JSON array": (raw("[1, 2]"), (0, 1, 0)),
    "JSON string": (raw('"Polygon"'), (0, 1, 0)),
    "Point": (json.dumps({"type": "Point", "coordinates": [115.1, -8.6]}), (0, 1, 0)),
    "Point with empty coordinates (M3 before S2)": (
        json.dumps({"type": "Point", "coordinates": []}),
        (0, 1, 0),
    ),
    "longitude out of range (M7)": (poly([[200, -8.6], *RING[1:3], [200, -8.6]]), (0, 1, 0)),
    "latitude out of range (M7)": (poly([[115.1, -91], *RING[1:3], [115.1, -91]]), (0, 1, 0)),
    "figure-eight with exactly cancelling lobes": (poly(FIGURE_EIGHT), (0, 0, 1)),
    "sliver of doubled area 1 is published": (poly(SLIVER), (1, 0, 0)),
    "gate r3: 1,000-point collinear ring": (poly(R3_RING), (0, 0, 1)),
    "GeometryCollection": (json.dumps({"type": "GeometryCollection", "geometries": []}), (0, 1, 0)),
    "coordinates missing": (json.dumps({"type": "Polygon"}), (0, 1, 0)),
    "coordinates false": (json.dumps({"type": "Polygon", "coordinates": False}), (0, 1, 0)),
    "coordinates null": (json.dumps({"type": "Polygon", "coordinates": None}), (0, 1, 0)),
    "coordinates 0": (json.dumps({"type": "Polygon", "coordinates": 0}), (0, 1, 0)),
    'coordinates ""': (json.dumps({"type": "Polygon", "coordinates": ""}), (0, 1, 0)),
    "coordinates {}": (json.dumps({"type": "Polygon", "coordinates": {}}), (0, 1, 0)),
    "EMPTY Polygon": (poly(), (0, 0, 0)),
    "EMPTY MultiPolygon": (multi(), (0, 0, 0)),
    "Polygon with an empty ring": (poly([]), (0, 1, 0)),
    "MultiPolygon with an empty polygon": (multi([RING], []), (0, 1, 0)),
    "ring of 3 positions": (poly(RING[:3]), (0, 1, 0)),
    "open ring": (poly(RING[:3] + [[115.3, -8.6]]), (0, 1, 0)),
    "3-number position": (poly([[115.1, -8.6, 0], *RING[1:3], [115.1, -8.6, 0]]), (0, 1, 0)),
    "bool position": (poly([[True, False], *RING[1:3], [True, False]]), (0, 1, 0)),
    "string position": (poly([["a", "b"], *RING[1:3], ["a", "b"]]), (0, 1, 0)),
    "NaN position": (raw(poly(RING).replace("115.1", "NaN")), (0, 1, 0)),
    "integer too large for a float": (raw(poly(RING).replace("115.1", "1" + "0" * 400)), (0, 1, 0)),
    "Polygon whose only ring has zero area": (poly(COLLINEAR), (0, 0, 1)),
    "Polygon collapsed to a point": (poly(COLLAPSED), (0, 0, 1)),
    "zero-area hole": (poly(RING, COLLINEAR), (1, 0, 1)),
    "MultiPolygon with one zero-area part": (multi([RING], [SEGMENT]), (1, 0, 1)),
    "MultiPolygon all zero-area": (multi([COLLINEAR], [COLLAPSED]), (0, 0, 2)),
    "good Polygon with a hole": (poly(RING, HOLE), (1, 0, 0)),
    "good MultiPolygon": (multi([RING], [RING, HOLE]), (1, 0, 0)),
}


@pytest.mark.parametrize("geometry,expected", TABLE.values(), ids=TABLE.keys())
def test_build_applies_the_geometry_table(geometry, expected):
    build = build_zones_geojson([row(1, geometry=geometry)])
    assert (build.features, build.malformed, build.degenerate_rings) == expected
    if build.features:
        assert validate_zones_geojson(build.body, None) == []


def test_dropped_rings_leave_the_rest_of_the_geometry_untouched():
    build = build_zones_geojson([row(1, geometry=multi([RING], [SEGMENT], [RING, COLLINEAR]))])
    geometry = orjson.loads(build.body)["features"][0]["geometry"]
    assert geometry == {"type": "MultiPolygon", "coordinates": [[RING], [RING]]}


BAD = {
    "ring collapsed to a point": collection(
        with_geometry({"type": "Polygon", "coordinates": [COLLAPSED]})
    ),
    "ring collapsed to a segment": collection(
        with_geometry({"type": "Polygon", "coordinates": [SEGMENT]})
    ),
    "zero-area collinear ring": collection(
        with_geometry({"type": "Polygon", "coordinates": [COLLINEAR]})
    ),
    "zero-area hole": collection(
        with_geometry({"type": "Polygon", "coordinates": [RING, COLLINEAR]})
    ),
    "empty multipolygon": collection(with_geometry({"type": "MultiPolygon", "coordinates": []})),
    "empty polygon in a multipolygon": collection(
        with_geometry({"type": "MultiPolygon", "coordinates": [[RING], []]})
    ),
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
    multi = with_geometry({"type": "MultiPolygon", "coordinates": [[RING], [RING, HOLE]]})
    nullable = feature(max_floors=None, kdb=0.5, max_height_meters=3)
    # The smallest area the rule allows: a right triangle of legs 1e-5 and 2e-5 degrees.
    tiny = [[115.1, -8.6], [115.10001, -8.6], [115.10001, -8.60002], [115.1, -8.6]]
    small = with_geometry({"type": "Polygon", "coordinates": [tiny]})
    assert validate_zones_geojson(collection(multi, nullable, small), None) == []


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
    for n in (10, 11, 12):  # under the 125% growth guard
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


REFUSED = {
    "one unreadable row": [row(i) for i in range(99)] + [row(99, geometry="{")],
    "one row with coordinates false": [row(i) for i in range(99)]
    + [row(99, geometry=json.dumps({"type": "Polygon", "coordinates": False}))],
    "every polygon zero-area": [row(i, geometry=poly(COLLINEAR)) for i in range(100)],
    "more zero-area rings than allowed": [row(i) for i in range(89)]
    + [row(i, geometry=poly(RING, COLLINEAR)) for i in range(89, 100)],
}


@pytest.mark.parametrize("rows", REFUSED.values(), ids=REFUSED.keys())
def test_a_bad_generation_keeps_the_last_version(mouth, rows_file, rows):
    publish(mouth, rows_file([row(i) for i in range(100)]))
    before = manifest(mouth)
    files = sorted(p.name for p in (mouth / "public/data/prime").iterdir())
    assert publish(mouth, rows_file(rows, "bad.jsonl")) == 2
    assert manifest(mouth) == before
    assert sorted(p.name for p in (mouth / "public/data/prime").iterdir()) == files


def test_a_few_zero_area_rings_are_dropped_and_the_rest_published(mouth, rows_file, capsys):
    rows = [row(i) for i in range(90)] + [
        row(i, geometry=poly(RING, COLLINEAR)) for i in range(90, 100)
    ]
    assert publish(mouth, rows_file(rows)) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["status"], summary["features"], summary["degenerate_rings_dropped"]) == (
        "published",
        100,
        10,
    )


def test_shrink_keeps_the_last_valid_version(mouth, rows_file):
    publish(mouth, rows_file([row(i) for i in range(20)]))
    before = manifest(mouth)
    assert publish(mouth, rows_file([row(i) for i in range(5)], "small.jsonl")) == 2
    assert manifest(mouth) == before


def test_check_exit_codes_and_no_writes(mouth, rows_file):
    first = rows_file([row(i) for i in range(10)])
    assert publish(mouth, first, "--check") == 10
    assert not (mouth / "public").exists()
    publish(mouth, first)
    assert publish(mouth, first, "--check") == 0
    assert publish(mouth, rows_file([row(i) for i in range(11)], "b.jsonl"), "--check") == 10
    assert publish(mouth, rows_file([row(1, geometry=None)], "c.jsonl"), "--check") == 2


def test_print_sql_wraps_the_query(capsys):
    assert cli.main(["--print-sql"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SELECT row_to_json(t)::text FROM (")
    assert "l.id" in out and out.rstrip().endswith(") t")


# ---- spec section 8: exact arithmetic, precedence, growth guard, cleanup ----


@pytest.mark.parametrize(
    "k",
    [
        -180_000_000,
        -90_000_000,
        0,
        90_000_000,
        180_000_000,
        114_000_000,
        116_000_000,
        -9_200_000,
        -7_800_000,
    ],
)
def test_integer_recovery_at_the_boundaries(k):
    c = round(k / GRID, 6)
    assert on_grid(c) and round(c * GRID) == k


def test_integer_recovery_on_seeded_samples():
    rnd = random.Random(490)
    for _ in range(100_000):
        k = rnd.randint(-180_000_000, 180_000_000)
        c = round(k / GRID, 6)
        assert on_grid(c) and round(c * GRID) == k


def test_grid_predicate_rejects_off_grid_values():
    assert not on_grid(115.1234567) and not on_grid(115.0000005)


def _grid(k: int) -> float:
    return round(k / GRID, 6)


def test_random_collinear_rings_are_all_degenerate():
    rnd = random.Random(1)
    for _ in range(300):
        x0, y0 = rnd.randint(114_500_000, 115_500_000), rnd.randint(-8_900_000, -8_100_000)
        dx, dy = 0, 0
        while (dx, dy) == (0, 0):
            dx, dy = rnd.randint(-50, 50), rnd.randint(-50, 50)
        ts = [rnd.randint(0, 2000) for _ in range(rnd.randint(3, 1999))]
        ring = [[_grid(x0 + dx * s), _grid(y0 + dy * s)] for s in ts]
        ring.append(ring[0])
        build = build_zones_geojson([row(1, geometry=poly(ring))])
        assert (build.features, build.malformed, build.degenerate_rings) == (0, 0, 1)


def test_random_non_degenerate_rings_are_all_published():
    rnd = random.Random(2)
    done = 0
    while done < 300:
        pts = [
            [rnd.randint(114_500_000, 115_500_000), rnd.randint(-8_900_000, -8_100_000)]
            for _ in range(rnd.choice((3, 4)))
        ]
        ring = [[_grid(x), _grid(y)] for x, y in pts] + [[_grid(pts[0][0]), _grid(pts[0][1])]]
        if ring_doubled_area(ring) == 0:
            continue
        build = build_zones_geojson([row(1, geometry=poly(ring))])
        assert (build.features, build.malformed, build.degenerate_rings) == (1, 0, 0)
        assert validate_zones_geojson(build.body, None) == []
        done += 1


def test_validator_rejects_an_off_grid_position():
    off = [[115.1234567, -8.6], [115.2, -8.6], [115.2, -8.7], [115.1234567, -8.6]]
    body = collection(with_geometry({"type": "Polygon", "coordinates": [off]}))
    assert any("grid" in e for e in validate_zones_geojson(body, None))


def test_growth_guard_boundaries():
    assert growth_errors(125, 100) == []
    assert growth_errors(126, 100)
    assert growth_errors(64_000_000, None) == []
    assert growth_errors(64_000_001, None)
    assert growth_errors(64_000_001, 60_000_000)


def test_a_padded_ring_over_the_growth_margin_is_refused(mouth, rows_file):
    publish(mouth, rows_file([row(i) for i in range(10)]))
    before = manifest(mouth)
    padded = [*RING[:3], *([RING[0]] * 2000), RING[0]]
    rows = [row(i) for i in range(9)] + [row(9, geometry=poly(padded))]
    assert publish(mouth, rows_file(rows, "padded.jsonl")) == 2
    assert manifest(mouth) == before


def test_a_cleanup_failure_after_the_manifest_is_reported_not_fatal(
    mouth, rows_file, capsys, monkeypatch
):
    for n in (10, 11):
        publish(mouth, rows_file([row(i) for i in range(n)], f"r{n}.jsonl"))
    capsys.readouterr()

    def refuse(self, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli.Path, "unlink", refuse)
    assert publish(mouth, rows_file([row(i) for i in range(12)], "r12.jsonl")) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "published" and len(summary["cleanup_errors"]) == 1
    assert manifest(mouth)["features"] == 12


def test_a_listing_failure_during_cleanup_is_reported_not_fatal(
    mouth, rows_file, capsys, monkeypatch
):
    publish(mouth, rows_file([row(i) for i in range(10)]))
    capsys.readouterr()

    def broken_glob(self, pattern):
        raise OSError(5, "Input/output error")
        yield  # pragma: no cover - makes this a generator like Path.glob

    monkeypatch.setattr(cli.Path, "glob", broken_glob)
    assert publish(mouth, rows_file([row(i) for i in range(11)], "r11.jsonl")) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "published"
    assert summary["cleanup_errors"] == ["listing public/data/prime: Input/output error"]
    assert manifest(mouth)["features"] == 11
