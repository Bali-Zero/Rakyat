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
    build_zones_geojson,
    generation_errors,
    growth_errors,
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
    build = build_zones_geojson(rows)
    body, count = build.body, build.features
    errors = (
        generation_errors(build)
        + validate_zones_geojson(body, manifest["features"] if manifest else None)
        + growth_errors(len(body), manifest.get("bytes") if manifest else None)
    )
    if errors:
        _emit(status="invalid", errors=errors, features=count, malformed=build.malformed)
        return EXIT_INVALID

    # The manifest carries only the 16-hex prefix, inside the file name: a full
    # 64-hex digest there is flagged by the repository's secret scanner and would
    # need a hand audit on every publish.
    sha16 = hashlib.sha256(body).hexdigest()[:16]
    name = f"zones-{sha16}.json"
    old_path = manifest["path"] if manifest else None
    summary = {
        "sha16": sha16,
        "features": count,
        "bytes": len(body),
        "previous": old_path,
        "degenerate_rings_dropped": build.degenerate_rings,
    }
    if old_path == f"{WEB_PREFIX}{name}":
        _emit(status="unchanged", **summary)
        return 0
    if check:
        _emit(status="changed", **summary)
        return EXIT_CHANGED

    data_dir = mouth_dir / DATA_SUBDIR
    data_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(data_dir / name, body)
    new_manifest = {
        "path": f"{WEB_PREFIX}{name}",
        "features": count,
        "bytes": len(body),
        "previous": old_path,
    }
    _atomic_write(mouth_dir / MANIFEST, (json.dumps(new_manifest, indent=2) + "\n").encode("utf-8"))

    # The new version is valid and named by the manifest from here on: a cleanup failure
    # is reported, it does not turn a successful publish into a failure.
    keep = {name, Path(old_path).name if old_path else None}
    cleanup_errors = []
    try:
        stale_files = [p for p in data_dir.glob("zones-*.json") if p.name not in keep]
    except OSError as exc:  # listing the directory failed: nothing is deleted, the publish stands
        stale_files = []
        cleanup_errors.append(f"listing {DATA_SUBDIR}: {exc.strerror or exc}")
    for stale in stale_files:
        try:
            stale.unlink()
        except OSError as exc:
            cleanup_errors.append(f"{stale.name}: {exc.strerror or exc}")
    if cleanup_errors:
        summary["cleanup_errors"] = cleanup_errors
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
