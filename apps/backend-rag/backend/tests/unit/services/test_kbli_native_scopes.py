"""Coverage is affirmative only for the pinned native rows; the legacy array is archival."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from backend.services import kbli_native_scopes as scopes

CANONICAL = Path(__file__).resolve().parents[6] / "data/source_documents/KBLI_2025_FINAL_CLEAN.json"
ATTESTED_COUNTS = {
    "55106": 6,
    "55201": 3,
    "55203": 3,
    "56101": 6,
    "56303": 4,
    "55209": 3,
    "56102": 6,
    "68111": 4,
    "68210": 8,
    "68292": 4,
    "70201": 3,
    "70209": 8,
    "85510": 8,
    "93116": 3,
    "96220": 3,
    "96230": 4,
}


def rows_digest(rows):
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def fake_row(index, label):
    return {
        "scope_index": index,
        "scope_uraian": label,
        "skala_usaha": ["Kecil"],
        "kategori_risiko": "Menengah Tinggi",
        "perizinan": [],
        "persyaratan": [f"Require {label}"],
        "kewajiban": [f"Obligation {label}"],
        "kewenangan": [f"Authority {label}"],
    }


def write_source(tmp_path, monkeypatch, records, row_pins):
    dataset = {"metadata": {"version": scopes._DATASET_VERSION}, "data": records}
    raw = json.dumps(dataset).encode()
    path = tmp_path / "canonical.json"
    path.write_bytes(raw)
    monkeypatch.setattr(scopes, "_DATASET_SHA256", hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(scopes, "_DATASET_SIZE", len(raw))
    monkeypatch.setattr(scopes, "_NATIVE_ROWS_SHA256", row_pins)
    return path


def synthetic_source(tmp_path, monkeypatch, rows, *, code="55106", row_pin=None, **fields):
    record = {
        "kode_kbli_2025": code,
        "_l1_source": "OSS_RBA_2025_id_version_fff4053d",
        "_l2_source": "OSS_RBA_resiko_2025",
        "per_skala": rows,
        "per_skala_legacy": [fake_row(9, "Legacy")],
        **fields,
    }
    return write_source(tmp_path, monkeypatch, [record], {code: row_pin or rows_digest(rows)})


@pytest.mark.parametrize("code,count", ATTESTED_COUNTS.items())
def test_default_reader_matches_attested_source_per_row(code, count):
    raw = CANONICAL.read_bytes()
    frozen = next(record for record in json.loads(raw)["data"] if record["kode_kbli_2025"] == code)
    coverage = scopes.read_native_scope_coverage(code)
    assert coverage.scope_rows_status == "available"
    assert coverage.scope_rows_source.dataset_sha256 == hashlib.sha256(raw).hexdigest()
    assert coverage.scope_rows_source.rows_sha256 == rows_digest(frozen["per_skala"])
    assert coverage.scope_rows_source.source == frozen["_l2_source"]
    assert coverage.scope_rows_source.kbli_vintage == "2025"
    assert len(coverage.scope_rows) == count
    for ordinal, (actual, expected) in enumerate(
        zip(coverage.scope_rows, frozen["per_skala"], strict=True)
    ):
        assert (actual.row_ordinal, actual.scope_index, actual.scope_uraian) == (
            ordinal,
            expected["scope_index"],
            expected["scope_uraian"],
        )
        assert (actual.scale, actual.risk_level) == (
            expected["skala_usaha"],
            expected["kategori_risiko"],
        )
        assert (actual.requirements, actual.obligations, actual.authorities) == (
            expected["persyaratan"],
            expected["kewajiban"],
            expected["kewenangan"],
        )
    if code == "55106":
        assert (
            rows_digest(frozen["per_skala_legacy"])
            == "bba78cb3807de891914a98362b52b3f504cf69c6fa7b91231abf1c220a234135"
        )


def test_same_tier_scopes_stay_separate_and_caller_cannot_mutate_cache(tmp_path, monkeypatch):
    path = synthetic_source(tmp_path, monkeypatch, [fake_row(0, "A"), fake_row(1, "B")])
    first = scopes.read_native_scope_coverage("55106", path)
    first.scope_rows[0].requirements.clear()
    fresh = scopes.read_native_scope_coverage("55106", path)
    assert [(r.scope_uraian, r.requirements, r.obligations) for r in fresh.scope_rows] == [
        ("A", ["Require A"], ["Obligation A"]),
        ("B", ["Require B"], ["Obligation B"]),
    ]


def test_residential_rental_gap_is_not_an_empty_attestation():
    assert not scopes.is_registered_native_scope("68112")
    assert scopes.read_native_scope_coverage("68112") == scopes.NativeScopeCoverage()


@pytest.mark.parametrize(
    "reason",
    [
        "empty",
        "wrong-marker",
        "pending",
        "bad-vintage",
        "row-pin",
        "invalid-second-scope",
        "unsupported-scale",
    ],
)
def test_gap_never_uses_legacy_or_yields_partial_rows(tmp_path, monkeypatch, reason):
    rows = [fake_row(0, "A"), fake_row(1, "B")]
    fields = {}
    if reason == "empty":
        rows = []
    elif reason == "wrong-marker":
        fields["_l2_source"] = "legacy"
    elif reason == "pending":
        fields["_l2_status"] = "absent_pending_corroboration"
    elif reason == "bad-vintage":
        fields["_l1_source"] = "OSS_RBA_2020"
    elif reason == "row-pin":
        fields["row_pin"] = rows_digest([])
    elif reason == "invalid-second-scope":
        rows[1]["scope_uraian"] = ""
    else:
        rows[1]["skala_usaha"] = ["Invalid"]
    path = synthetic_source(tmp_path, monkeypatch, rows, **fields)
    result = scopes.read_native_scope_coverage("55106", path)
    assert result == scopes.NativeScopeCoverage()


@pytest.mark.parametrize(
    "change",
    [
        {"jangka_waktu": 123},
        {"kategori_risiko": "Unsupported", "perizinan": ["Synthetic explicit label"]},
        {"kewajiban": None},
        {"persyaratan": [42]},
        {"scope_index": True},
    ],
)
def test_malformed_attested_row_refuses_the_whole_projection(tmp_path, monkeypatch, change):
    rows = [fake_row(0, "Good"), {**fake_row(1, "Malformed"), **change}]
    path = synthetic_source(tmp_path, monkeypatch, rows)
    assert scopes.read_native_scope_coverage("55106", path) == scopes.NativeScopeCoverage()


def test_transient_read_error_is_retried_and_warm_requests_do_not_reparse(tmp_path, monkeypatch):
    path = synthetic_source(tmp_path, monkeypatch, [fake_row(0, "A")])
    opens = []
    original_open = Path.open

    def open_source(self, *args, **kwargs):
        opens.append(self)
        if len(opens) == 1:
            raise OSError("synthetic transient failure")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_source)
    assert scopes.read_native_scope_coverage("55106", path).scope_rows is None
    assert scopes.read_native_scope_coverage("55106", path).scope_rows_status == "available"
    assert scopes.read_native_scope_coverage("55106", path).scope_rows_status == "available"
    assert opens == [path, path]


def test_cache_detects_same_size_replacement_and_recovers(tmp_path, monkeypatch):
    path = synthetic_source(tmp_path, monkeypatch, [fake_row(0, "A")])
    raw = path.read_bytes()
    before = path.stat()
    assert scopes.read_native_scope_coverage("55106", path).scope_rows_status == "available"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(raw.replace(b"Require A", b"Require B"))
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    replacement.replace(path)
    assert scopes.read_native_scope_coverage("55106", path) == scopes.NativeScopeCoverage()
    path.unlink()
    assert scopes.read_native_scope_coverage("55106", path) == scopes.NativeScopeCoverage()
    path.write_bytes(raw)
    assert scopes.read_native_scope_coverage("55106", path).scope_rows_status == "available"


def test_other_codes_have_no_attestation(tmp_path, monkeypatch):
    path = synthetic_source(tmp_path, monkeypatch, [fake_row(0, "A")])
    assert scopes.read_native_scope_coverage("56304", path) == scopes.NativeScopeCoverage()


def test_registered_codes_share_one_dataset_read_without_cache_thrashing(tmp_path, monkeypatch):
    path = tmp_path / "canonical.json"
    path.write_bytes(CANONICAL.read_bytes())
    opens = []
    original_open = Path.open

    def open_source(self, *args, **kwargs):
        opens.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_source)
    for code in list(ATTESTED_COUNTS) * 3:
        assert scopes.read_native_scope_coverage(code, path).scope_rows_status == "available"
    assert opens == [path]


@pytest.mark.parametrize("code", ATTESTED_COUNTS)
def test_native_terms_preserve_order_markup_and_duplicates(tmp_path, monkeypatch, code):
    row = fake_row(0, "All")
    row.update(
        persyaratan=["<b>First</b>", "Second", "<b>First</b>"],
        kewajiban=["Duty B", "Duty A", "Duty B"],
        kewenangan=["Authority B", "Authority A", "Authority B"],
    )
    path = synthetic_source(tmp_path, monkeypatch, [row], code=code)
    called_codes = []
    original_derive = scopes.derive_licence_groups

    def derive(actual_code, rows):
        called_codes.append(actual_code)
        return original_derive(actual_code, rows)

    monkeypatch.setattr(scopes, "derive_licence_groups", derive)
    result = scopes.read_native_scope_coverage(code, path)
    assert result.scope_rows_status == "available"
    assert called_codes == [code]
    actual = result.scope_rows[0]
    assert actual.requirements == row["persyaratan"]
    assert actual.obligations == row["kewajiban"]
    assert actual.authorities == row["kewenangan"]
    actual.obligations.clear()
    assert (
        scopes.read_native_scope_coverage(code, path).scope_rows[0].obligations == row["kewajiban"]
    )


@pytest.mark.parametrize(
    "failure", ["pin", "missing", "duplicate", "partial", "malformed", "marker"]
)
def test_one_code_failure_never_poisons_another(tmp_path, monkeypatch, failure):
    records = [
        {
            "kode_kbli_2025": code,
            "_l1_source": "OSS_RBA_2025_id_version_fff4053d",
            "_l2_source": "OSS_RBA_resiko_2025",
            "per_skala": [fake_row(0, code), fake_row(1, "Second " + code)],
        }
        for code in ["55201", "55203"]
    ]
    pins = {record["kode_kbli_2025"]: rows_digest(record["per_skala"]) for record in records}
    if failure == "pin":
        pins["55201"] = pins["55203"]
    elif failure == "missing":
        records.pop(0)
    elif failure == "duplicate":
        records.append(records[0])
    elif failure == "partial":
        records[0]["per_skala"].pop()
    elif failure == "malformed":
        records[0]["per_skala"][0]["kewajiban"] = None
        pins["55201"] = rows_digest(records[0]["per_skala"])
    else:
        records[0]["_l2_source"] = "legacy"
    path = write_source(tmp_path, monkeypatch, records, pins)
    for code in ["55201", "55203"] * 2:
        result = scopes.read_native_scope_coverage(code, path)
        assert result.scope_rows_status == ("unavailable" if code == "55201" else "available")
        if code == "55203":
            assert result.scope_rows[0].scope_uraian == code


def test_restaurant_scope_preserves_risk_changes_between_scales():
    result = scopes.read_native_scope_coverage("56101")
    assert result.scope_rows_status == "available"
    restaurant = [row for row in result.scope_rows if row.scope_index == 0]
    assert [(row.scale, row.risk_level) for row in restaurant] == [
        (["Mikro"], "Menengah Rendah"),
        (["Kecil"], "Menengah Tinggi"),
        (["Menengah"], "Menengah Tinggi"),
        (["Besar"], "Menengah Tinggi"),
    ]


def test_food_stall_keeps_three_scopes_with_two_scales_each():
    result = scopes.read_native_scope_coverage("56102")
    assert result.scope_rows_status == "available"
    assert len({row.scope_uraian for row in result.scope_rows}) == 3
    assert [(row.scope_index, row.scale) for row in result.scope_rows] == [
        (0, ["Mikro"]),
        (0, ["Kecil"]),
        (1, ["Mikro"]),
        (1, ["Kecil"]),
        (2, ["Mikro"]),
        (2, ["Kecil"]),
    ]
