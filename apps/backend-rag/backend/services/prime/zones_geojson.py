"""Prime zoning polygons as one GeoJSON body: build, validate, and the query that feeds both."""

import json
import math
import re
from typing import Any

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


def build_zones_geojson_body(rows: list[Any]) -> tuple[bytes, int]:
    """Serialise the query rows to the FeatureCollection body, sorted by id; return (body, feature count)."""
    body, count, _malformed = build_zones_geojson(rows)
    return body, count


def build_zones_geojson(rows: list[Any]) -> tuple[bytes, int, int]:
    """Like build_zones_geojson_body, plus the number of rows whose geometry could not be read.

    Two skips are expected and not counted: a NULL geometry (ST_Simplify collapsed the
    zone) and an EMPTY one (no coordinates). Anything else that is not a GeoJSON object
    with coordinates is malformed: the endpoint skips it, the publisher refuses to publish.
    """
    features: list[bytes] = []
    malformed = 0
    for row in sorted(rows, key=lambda r: str(r["id"])):
        geom_str = row["geometry"]
        if not geom_str:
            continue
        try:
            geom = json.loads(geom_str)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(geom, dict) or "coordinates" not in geom:
            malformed += 1
            continue
        if not geom["coordinates"]:
            continue
        try:
            truncate_geojson_coords(geom)
        except Exception:
            malformed += 1
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
    return body, len(features), malformed


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _position_error(pos: Any) -> str | None:
    if not (isinstance(pos, list) and len(pos) == 2 and all(_is_number(c) for c in pos)):
        return "position is not two numbers"
    if not all(math.isfinite(c) for c in pos):
        return "position is not finite"
    lng, lat = pos
    if not (LNG_RANGE[0] <= lng <= LNG_RANGE[1] and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        return f"position {lng},{lat} outside the Bali bounding box"
    return None


def _ring_errors(ring: Any) -> list[str]:
    if not isinstance(ring, list) or len(ring) < 4:
        return ["ring has fewer than 4 positions"]
    errors = [e for e in (_position_error(p) for p in ring) if e]
    if not errors and ring[0] != ring[-1]:
        errors.append("ring is not closed")
    # Collapsed to a point or a segment. Three distinct collinear positions are
    # accepted: production serves three such zero-area rings today.
    if not errors and len({tuple(p) for p in ring}) < 3:
        errors.append("ring has fewer than 3 distinct positions")
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
