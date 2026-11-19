"""Prime zoning polygons as one GeoJSON body: build, validate, and the query that feeds both."""

import json
import math
import re
from typing import Any, NamedTuple

import orjson

ZONE_COLORS_MAP: dict[str, str] = {
    "K-1": "#E8472A",
    "K-2": "#E8472A",
    "K-3": "#E8472A",
    "C-1": "#F0826E",
    "C-2": "#F0826E",
    "W": "#FFA5FF",
    "W-1": "#FFA5FF",
    "W-2": "#FF85F5",
    "R-2": "#FF7D00",
    "R-3": "#FF9D30",
    "R-4": "#FFB860",
    "P-1": "#C8C83C",
    "P-2": "#D4D44A",
    "P-3": "#C8C83C",
    "P-4": "#BEB82E",
    "KT": "#A855F7",
    "KPI": "#690000",
    "SPU-1": "#D4845A",
    "SPU-2": "#D4845A",
    "SPU-3": "#D4845A",
    "SPU-4": "#D4845A",
    "HL": "#224027",
    "KS-4": "#224027",
    "THR": "#224027",
    "TWA": "#224027",
    "EM": "#2D966E",
    "PS": "#05D7D7",
    "SS": "#05D7D7",
    "SP": "#05D7D7",
    "LS": "#F59E0B",
    "CB": "#B45309",
    "RTH-2": "#3BA062",
    "RTH-3": "#3BA062",
    "RTH-4": "#3BA062",
    "RTH-5": "#3BA062",
    "RTH-7": "#6B7280",
    "RTH-8": "#3BA062",
    "RTNH": "#9CA3AF",
    "BA": "#97DBF2",
    "BJ": "#9CA3AF",
    "TR": "#6B7280",
    "HK": "#9B00FF",
    "PL-3": "#6B7280",
    "PL-4": "#6B7280",
    "PTL": "#6B7280",
    "IK-1": "#507DD2",
}

DEFAULT_COLOR = "#6B7280"
EMPTY_BODY = b'{"type":"FeatureCollection","features":[]}'

ZONES_GEOJSON_QUERY = """
    SELECT
        l.id,
        ST_AsGeoJSON(ST_Simplify(l.boundary, 0.0001)) AS geometry,
        l.zoning_type,
        l.kodszn,
        m.max_floors,
        m.max_height_meters,
        m.kdb as master_kdb
    FROM bali_zoning_layers l
    LEFT JOIN master_building_codes m ON l.kodszn = m.kodszn AND l.district_name = m.district_name
    WHERE l.boundary IS NOT NULL
"""

LNG_RANGE = (114.0, 116.0)
LAT_RANGE = (-9.2, -7.8)
PROPERTY_KEYS = {"zone_code", "zone_type", "color", "max_floors", "max_height_meters", "kdb"}
SHRINK_FLOOR = 0.95
MAX_REPORTED_ERRORS = 20
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def truncate_geojson_coords(geom: dict, precision: int = 6) -> None:
    """Truncate GeoJSON coordinate arrays in-place to reduce payload size."""

    def _trunc(coords: Any) -> Any:
        if isinstance(coords, list):
            if coords and isinstance(coords[0], (int, float)):
                return [round(c, precision) for c in coords]
            return [_trunc(c) for c in coords]
        return coords

    if "coordinates" in geom:
        geom["coordinates"] = _trunc(geom["coordinates"])


GRID = 1_000_000  # coordinates are rounded to 6 decimals, so they are integers on this grid
DEGENERATE_FLOOR = 10
DEGENERATE_SHARE = 0.001
GROWTH_SHARE = 1.25
MAX_BYTES = 64_000_000


class ZonesBuild(NamedTuple):
    body: bytes
    features: int
    malformed: int
    degenerate_rings: int
    rows_with_geometry: int


# What each row's `geometry` column becomes: the spec of Rakyat #490 (PASS-SPEC 2026-11-19).
# The endpoint applies the same table. Phase 1 tests the rows in this order and the first
# that applies is the outcome; a row none applies to goes to phase 2.
#
# | row | geometry column                                                    | outcome  |
# |-----|--------------------------------------------------------------------|----------|
# | S1  | NULL or ""                                                         | skip     |
# | M1  | not JSON                                                           | MALFORMED|
# | M2  | JSON that is not an object                                         | MALFORMED|
# | M3  | type not exactly "Polygon" or "MultiPolygon"                       | MALFORMED|
# | M4  | coordinates missing or not an array (false, null, 0, "", {})       | MALFORMED|
# | S2  | coordinates == [] (EMPTY)                                          | skip     |
# | M5  | not a non-empty array of polygons, each a non-empty array of rings | MALFORMED|
# | M6  | a ring not >= 4 positions with first == last; a position not two  |          |
# |     | finite non-bool numbers                                            | MALFORMED|
# | M7  | a position outside lng [-180, 180] or lat [-90, 90]                | MALFORMED|
#
# Phase 2 rounds every coordinate to the grid, then drops each ring whose doubled area,
# computed EXACTLY with integers on the grid, is 0: an outer ring takes its polygon (D1),
# a hole goes alone (D2); both are counted. A feature left with no polygon is skipped
# (S3); anything else is published (OK).
#
# The publisher refuses a generation with any MALFORMED row, more dropped rings than
# max(DEGENERATE_FLOOR, DEGENERATE_SHARE x rows with geometry), a validator error, fewer
# than 95% of the previous features, or more than GROWTH_SHARE x the previous bytes (or
# MAX_BYTES in any case). Not checked, by design: self-intersection of a ring with a
# non-zero area, ring orientation, holes outside their shell, overlaps, repeated
# positions, tiny but non-zero areas, vertex counts (the size guard bounds the total).


def on_grid(c: float) -> bool:
    """True iff c is already a multiple of 1e-6 as rounding writes it: exact, no tolerance."""
    return round(c, 6) == c


def ring_doubled_area(ring: list) -> int:
    """Twice the signed area of a closed ring of on-grid positions, in grid units, exactly.

    Every coordinate is first turned into its integer on the grid (exact for |c| <= 180,
    the float product is within 3e-8 of the integer), so collinear rings give 0 whatever
    their length, which a float shoelace does not."""
    pts = [(round(x * GRID), round(y * GRID)) for x, y in ring]
    return sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1] for i in range(len(pts) - 1))


def _is_position(pos: Any) -> bool:
    return (
        isinstance(pos, list)
        and len(pos) == 2
        and all(_is_number(c) and _is_finite(c) for c in pos)
        and -180 <= pos[0] <= 180
        and -90 <= pos[1] <= 90
    )


def _is_ring(ring: Any) -> bool:
    return (
        isinstance(ring, list)
        and len(ring) >= 4
        and all(_is_position(p) for p in ring)
        and ring[0] == ring[-1]
    )


def _classify_geometry(geom: Any) -> tuple[str, int]:
    """Apply the table above to one parsed geometry (M2..M7, S2, then phase 2), rounding it
    and dropping its zero-area rings in place. Return (outcome, rings dropped); outcome is
    one of "ok", "empty", "degenerate", "malformed"."""
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return "malformed", 0
    coords = geom.get("coordinates")
    if not isinstance(coords, list):
        return "malformed", 0
    if not coords:
        return "empty", 0
    polygons = [coords] if geom["type"] == "Polygon" else coords
    if not all(isinstance(p, list) and p and all(_is_ring(r) for r in p) for p in polygons):
        return "malformed", 0
    truncate_geojson_coords(geom)
    polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    kept: list[list] = []
    dropped = 0
    for polygon in polygons:
        if ring_doubled_area(polygon[0]) == 0:
            dropped += len(polygon)
            continue
        holes = [r for r in polygon[1:] if ring_doubled_area(r) != 0]
        dropped += len(polygon) - 1 - len(holes)
        kept.append([polygon[0], *holes])
    if not kept:
        return "degenerate", dropped
    if dropped:
        geom["coordinates"] = kept[0] if geom["type"] == "Polygon" else kept
    return "ok", dropped


def build_zones_geojson_body(rows: list[Any]) -> tuple[bytes, int]:
    """Serialise the query rows to the FeatureCollection body, sorted by id; return (body, feature count)."""
    build = build_zones_geojson(rows)
    return build.body, build.features


def build_zones_geojson(rows: list[Any]) -> ZonesBuild:
    """Build the body and count what the table above skipped, dropped or found malformed."""
    features: list[bytes] = []
    malformed = degenerate_rings = with_geometry = 0
    for row in sorted(rows, key=lambda r: str(r["id"])):
        geom_str = row["geometry"]
        if geom_str is None or geom_str == "":
            continue
        with_geometry += 1
        try:
            geom = json.loads(geom_str)
        except (TypeError, ValueError):
            malformed += 1
            continue
        outcome, dropped = _classify_geometry(geom)
        degenerate_rings += dropped
        if outcome == "malformed":
            malformed += 1
            continue
        if outcome != "ok":
            continue

        zone_type: str = row["zoning_type"] or ""
        zone_code = row["kodszn"] or zone_type.split(":")[0].strip()
        max_height = float(row["max_height_meters"]) if row["max_height_meters"] else 15.0
        kdb = float(row["master_kdb"]) if row["master_kdb"] else 50.0

        features.append(
            orjson.dumps(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "zone_code": zone_code,
                        "zone_type": zone_type,
                        "color": ZONE_COLORS_MAP.get(zone_code, DEFAULT_COLOR),
                        "max_floors": row["max_floors"],
                        "max_height_meters": max_height,
                        "kdb": kdb,
                    },
                },
            ),
        )
    body = b'{"type":"FeatureCollection","features":[' + b",".join(features) + b"]}"
    return ZonesBuild(body, len(features), malformed, degenerate_rings, with_geometry)


def generation_errors(build: ZonesBuild) -> list[str]:
    """The publisher's refusals that only the build can see (the body cannot show a dropped row)."""
    errors = []
    if build.malformed:
        errors.append(
            f"{build.malformed} rows carry a geometry that is not a well-formed Polygon or MultiPolygon"
        )
    allowed = max(DEGENERATE_FLOOR, DEGENERATE_SHARE * build.rows_with_geometry)
    if build.degenerate_rings > allowed:
        errors.append(
            f"{build.degenerate_rings} zero-area rings dropped, more than the {allowed:.0f} allowed"
        )
    return errors


def growth_errors(body_bytes: int, previous_bytes: int | None) -> list[str]:
    """The visitor downloads and parses the whole body: a version much larger than the last
    one waits for a human, and the last valid version stays served."""
    errors = []
    if body_bytes > MAX_BYTES:
        errors.append(f"body is {body_bytes} bytes, over the {MAX_BYTES} cap")
    if previous_bytes and body_bytes > GROWTH_SHARE * previous_bytes:
        errors.append(
            f"body grew from {previous_bytes} to {body_bytes} bytes, over {GROWTH_SHARE:.0%} of the last version"
        )
    return errors


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite(value: Any) -> bool:
    """A JSON integer too large for a float (1e400 written as digits) is not finite either."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _position_error(pos: Any) -> str | None:
    if not (isinstance(pos, list) and len(pos) == 2 and all(_is_number(c) for c in pos)):
        return "position is not two numbers"
    if not all(_is_finite(c) for c in pos):
        return "position is not finite"
    lng, lat = pos
    if not (LNG_RANGE[0] <= lng <= LNG_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        return f"position {lng},{lat} outside the Bali bounding box"
    if not (on_grid(lng) and on_grid(lat)):
        return f"position {lng},{lat} is not on the 1e-6 grid"
    return None


def _ring_errors(ring: Any) -> list[str]:
    if not isinstance(ring, list) or len(ring) < 4:
        return ["ring has fewer than 4 positions"]
    errors = [e for e in (_position_error(p) for p in ring) if e]
    if not errors and ring[0] != ring[-1]:
        errors.append("ring is not closed")
    if not errors and ring_doubled_area(ring) == 0:
        errors.append("ring has zero area")
    return errors[:1]


def _geometry_errors(geom: Any) -> list[str]:
    if not isinstance(geom, dict):
        return ["geometry is not an object"]
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind == "Polygon":
        polygons = [coords]
    elif kind == "MultiPolygon":
        polygons = coords if isinstance(coords, list) else None
    else:
        return [f"geometry type {kind!r} is not Polygon or MultiPolygon"]
    if (
        not isinstance(polygons, list)
        or not polygons
        or not all(isinstance(p, list) and p for p in polygons)
    ):
        return ["geometry coordinates are malformed"]
    errors: list[str] = []
    for polygon in polygons:
        for ring in polygon:
            errors.extend(_ring_errors(ring))
    return errors[:1]


def _property_errors(props: Any) -> list[str]:
    if not isinstance(props, dict) or set(props) != PROPERTY_KEYS:
        return ["properties keys are not exactly the expected six"]
    errors = []
    if not (isinstance(props["zone_code"], str) and props["zone_code"]):
        errors.append("zone_code is not a non-empty string")
    if not isinstance(props["zone_type"], str):
        errors.append("zone_type is not a string")
    if not (isinstance(props["color"], str) and _COLOR_RE.match(props["color"])):
        errors.append("color is not #RRGGBB")
    floors = props["max_floors"]
    if floors is not None and not (isinstance(floors, int) and not isinstance(floors, bool)):
        errors.append("max_floors is not an int or null")
    for key in ("max_height_meters", "kdb"):
        if not _is_number(props[key]):
            errors.append(f"{key} is not a number")
    return errors


def validate_zones_geojson(body: bytes, previous_features: int | None) -> list[str]:
    """Return the problems found in a zones body; an empty list means it may be published."""
    try:
        doc = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        return [f"body is not JSON: {exc}"]
    if not (isinstance(doc, dict) and doc.get("type") == "FeatureCollection"):
        return ["top level is not a FeatureCollection"]
    features = doc.get("features")
    if not isinstance(features, list) or not features:
        return ["FeatureCollection has no features"]

    errors: list[str] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            errors.append(f"feature {index}: type is not Feature")
            continue
        problems = _geometry_errors(feature.get("geometry")) + _property_errors(
            feature.get("properties")
        )
        errors.extend(f"feature {index}: {p}" for p in problems)

    if previous_features is not None and len(features) < previous_features * SHRINK_FLOOR:
        errors.append(
            f"shrink guard: {len(features)} features is under 95% of the previous {previous_features}"
        )
    if len(errors) > MAX_REPORTED_ERRORS:
        errors = errors[:MAX_REPORTED_ERRORS] + [f"{len(errors)} errors in total"]
    return errors
