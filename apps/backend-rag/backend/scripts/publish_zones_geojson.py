"""Publish the Prime zoning polygons as a versioned static file for the mouth CDN.

Needs no database credentials: rows arrive as JSON lines produced by
`psql -tA -c "$(python -m backend.scripts.publish_zones_geojson --print-sql)"`.
Exit codes: 0 unchanged (or published), 10 --check found a valid change, 2 invalid input.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from backend.services.prime.zones_geojson import (
    ZONES_GEOJSON_QUERY,
    build_zones_geojson_body,
    validate_zones_geojson,
)

EXIT_CHANGED = 10
EXIT_INVALID = 2
DATA_SUBDIR = Path("public/data/prime")
MANIFEST = Path("src/data/prime-zones.json")
WEB_PREFIX = "/data/prime/"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_rows(source: str) -> list[dict]:
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _read_manifest(mouth_dir: Path) -> dict | None:
    path = mouth_dir / MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _emit(**summary: object) -> None:
    sys.stdout.write(json.dumps(summary, sort_keys=True) + "\n")


def run(rows: list[dict], mouth_dir: Path, check: bool) -> int:
    manifest = _read_manifest(mouth_dir)
    body, count = build_zones_geojson_body(rows)
    errors = validate_zones_geojson(body, manifest["features"] if manifest else None)
    if errors:
        _emit(status="invalid", errors=errors, features=count)
        return EXIT_INVALID

    digest = hashlib.sha256(body).hexdigest()
    sha16 = digest[:16]
    old_path = manifest["path"] if manifest else None
    summary = {"sha16": sha16, "features": count, "bytes": len(body), "previous": old_path}
    if manifest and manifest["sha256"] == digest:
        _emit(status="unchanged", **summary)
        return 0
    if check:
        _emit(status="changed", **summary)
        return EXIT_CHANGED

    data_dir = mouth_dir / DATA_SUBDIR
    data_dir.mkdir(parents=True, exist_ok=True)
    name = f"zones-{sha16}.json"
    _atomic_write(data_dir / name, body)
    new_manifest = {
        "path": f"{WEB_PREFIX}{name}",
        "sha256": digest,
        "features": count,
        "bytes": len(body),
        "previous": old_path,
    }
    _atomic_write(mouth_dir / MANIFEST, (json.dumps(new_manifest, indent=2) + "\n").encode("utf-8"))

    keep = {name, Path(old_path).name if old_path else None}
    for stale in data_dir.glob("zones-*.json"):
        if stale.name not in keep:
            stale.unlink()
    _emit(status="published", **summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print-sql", action="store_true")
    parser.add_argument("--rows", help="JSON lines file, or - for stdin")
    parser.add_argument("--mouth-dir", type=Path, default=Path("../mouth"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.print_sql:
        sys.stdout.write(f"SELECT row_to_json(t)::text FROM ({ZONES_GEOJSON_QUERY.strip()}) t\n")
        return 0
    if not args.rows:
        parser.error("--rows is required unless --print-sql is given")
    return run(_read_rows(args.rows), args.mouth_dir, args.check)


if __name__ == "__main__":
    sys.exit(main())
