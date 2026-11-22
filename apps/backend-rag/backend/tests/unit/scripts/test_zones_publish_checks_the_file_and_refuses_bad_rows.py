"""Regression coverage for zones GeoJSON publish preconditions."""

import hashlib
import json

import pytest

from backend.scripts import publish_zones_geojson as cli
from backend.services.prime.zones_geojson import build_zones_geojson

RING = [[115.1, -8.6], [115.2, -8.6], [115.2, -8.7], [115.1, -8.6]]


def row() -> dict:
    return {
        "id": "zone-synthetic-001",
        "geometry": json.dumps({"type": "Polygon", "coordinates": [RING]}),
        "zoning_type": "K-1: Synthetic",
        "kodszn": "K-1",
        "max_floors": 4,
        "max_height_meters": 20,
        "master_kdb": 60,
    }


def expected(rows: list[dict]) -> tuple[bytes, str]:
    body = build_zones_geojson(rows).body
    name = f"zones-{hashlib.sha256(body).hexdigest()[:16]}.json"
    return body, name


def seed_manifest(mouth, name: str, body: bytes) -> None:
    (mouth / "src/data").mkdir(parents=True)
    (mouth / "src/data/prime-zones.json").write_text(
        json.dumps({"path": f"/data/prime/{name}", "features": 1, "bytes": len(body)})
    )


def test_matching_manifest_with_missing_data_file_publishes_it(tmp_path, capsys):
    rows = [row()]
    body, name = expected(rows)
    seed_manifest(tmp_path, name, body)

    assert cli.run(rows, tmp_path, check=False) == 0

    assert (tmp_path / "public/data/prime" / name).read_bytes() == body
    assert json.loads(capsys.readouterr().out)["status"] == "published"


def test_matching_manifest_with_wrong_data_file_rewrites_it(tmp_path):
    rows = [row()]
    body, name = expected(rows)
    seed_manifest(tmp_path, name, body)
    data_file = tmp_path / "public/data/prime" / name
    data_file.parent.mkdir(parents=True)
    data_file.write_bytes(b"synthetic wrong content")

    assert cli.run(rows, tmp_path, check=False) == 0

    assert data_file.read_bytes() == body


@pytest.mark.parametrize("bad_line", ["not-json", "[]"])
def test_malformed_json_line_returns_invalid_and_writes_nothing(tmp_path, capsys, bad_line):
    rows = tmp_path / "rows.jsonl"
    rows.write_text(json.dumps(row()) + f"\n{bad_line}\n")
    mouth = tmp_path / "mouth"
    mouth.mkdir()

    assert cli.main(["--rows", str(rows), "--mouth-dir", str(mouth)]) == cli.EXIT_INVALID

    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "invalid"
    assert "line 2" in summary["errors"][0]
    assert bad_line not in summary["errors"][0]
    assert not (mouth / "public").exists()
    assert not (mouth / "src/data/prime-zones.json").exists()


def test_valid_input_publishes_and_complete_version_is_unchanged(tmp_path, capsys):
    rows = [row()]
    body, name = expected(rows)
    mouth = tmp_path / "mouth"
    (mouth / "src/data").mkdir(parents=True)

    assert cli.run(rows, mouth, check=False) == 0
    data_file = mouth / "public/data/prime" / name
    manifest_file = mouth / "src/data/prime-zones.json"
    before = (data_file.read_bytes(), data_file.stat().st_mtime_ns, manifest_file.read_bytes())
    capsys.readouterr()

    assert cli.run(rows, mouth, check=False) == 0

    assert (
        data_file.read_bytes(),
        data_file.stat().st_mtime_ns,
        manifest_file.read_bytes(),
    ) == before
    assert json.loads(capsys.readouterr().out)["status"] == "unchanged"


def test_rewriting_a_missing_data_file_keeps_the_previous_version(tmp_path):
    rows = [row()]
    body, name = expected(rows)
    previous_name = "zones-0000000000000000.json"
    (tmp_path / "src/data").mkdir(parents=True)
    manifest_file = tmp_path / "src/data/prime-zones.json"
    manifest_file.write_text(
        json.dumps(
            {
                "path": f"/data/prime/{name}",
                "features": 1,
                "bytes": len(body),
                "previous": f"/data/prime/{previous_name}",
            }
        )
    )
    data_dir = tmp_path / "public/data/prime"
    data_dir.mkdir(parents=True)
    (data_dir / previous_name).write_bytes(b"synthetic previous version")

    assert cli.run(rows, tmp_path, check=False) == 0

    assert (data_dir / name).read_bytes() == body
    assert (data_dir / previous_name).exists()
    assert json.loads(manifest_file.read_text())["previous"] == f"/data/prime/{previous_name}"
