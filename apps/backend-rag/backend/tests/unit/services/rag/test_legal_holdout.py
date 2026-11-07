"""Offline checks for the frozen legal retrieval robustness set."""

import copy
import hashlib
from collections import Counter

import pytest

from backend.tests.benchmarks.legal_retrieval import holdout_set as holdout
from backend.tests.benchmarks.legal_retrieval import perpres_set as corpus


@pytest.fixture
def synthetic_source():
    segments, cases, published = [], [], []

    def add(case_id, family, number, year, pasal, ayat=None, pair_id=None):
        regulation = f"nomor {number} tahun {year}"
        sub = f"pasal {pasal} - ayat ({ayat})" if ayat else f"pasal {pasal} - teks"
        query = (
            f"Tolong temukan Pasal {pasal} dalam Peraturan Presiden Nomor {number} Tahun {year}."
        )
        if ayat:
            query = (
                f"Tunjukkan hanya ayat ({ayat}) dari Pasal {pasal}, "
                f"Peraturan Presiden Nomor {number} Tahun {year}."
            )
        segments.append(
            corpus.Segment(case_id, regulation, "batang_tubuh", sub, "pengadaan barang")
        )
        cases.append(
            {
                "case_id": case_id,
                "family": family,
                "query": query,
                "source_regulation": regulation,
                "source_pasal": pasal,
                "source_ayat": ayat,
                "relevance": {case_id: 2},
                "derivation": "structured_corpus_reference",
                "source_query_id": None,
                "is_new_formulation": True,
                "pair_id": pair_id,
            }
        )

    for i in range(24):
        add(f"citation-{i}", "citation_variant", 100 + i, 2020, "1a" if i < 4 else "1")
    for i in range(12):
        add(f"ayat-{i}", "ayat_exact", 200 + i, 2020, "2", "1")
        segments.append(
            corpus.Segment(
                f"other-ayat-{i}",
                f"nomor {200 + i} tahun 2020",
                "batang_tubuh",
                "pasal 2 - ayat (2)",
                "bagian lain",
            )
        )
    for i in range(6):
        for year in (2020, 2021):
            add(f"contrast-{i}-{year}", "year_contrast", 300 + i, year, "3", pair_id=str(i))
    for family in ("literal", "semantic"):
        for i in range(6):
            case_id = f"{family}-{i}"
            add(case_id, f"published_{family}", 400 + i, 2020, family)
            case = cases[-1]
            case.update(
                source_query_id=case_id, is_new_formulation=False, derivation="published_qrels"
            )
            published.append(corpus.Query(case_id, family, case["query"], case["relevance"]))
    source = corpus.Benchmark(
        {s.row_id: s.text for s in segments},
        tuple(published + corpus.citation_queries(segments)),
        True,
    )
    manifest = {"schema_version": 1, "source_md5": corpus.ZENODO_FILES, "cases": cases}
    return manifest, source, segments


def test_frozen_bytes_and_declared_coverage():
    manifest = holdout.read_manifest()
    assert hashlib.sha256(holdout.DEFAULT_FIXTURE.read_bytes()).hexdigest() == holdout.FROZEN_SHA256
    assert Counter(c["family"] for c in manifest["cases"]) == holdout.FAMILY_COUNTS
    assert sum(c["is_new_formulation"] for c in manifest["cases"]) == 48


def test_fixture_digest_rejects_corruption(tmp_path):
    changed = tmp_path / "changed.json"
    changed.write_bytes(holdout.DEFAULT_FIXTURE.read_bytes() + b" ")
    with pytest.raises(holdout.HoldoutIntegrityError, match="SHA-256"):
        holdout.read_manifest(changed)


def test_valid_source_is_deterministic_and_ayat_is_precise(synthetic_source):
    manifest, source, segments = synthetic_source
    queries = holdout.validate_manifest(manifest, source, segments)
    assert queries == holdout.validate_manifest(manifest, source, segments)
    assert len(queries) == 60
    assert queries[24].relevance == {"ayat-0": 2}


@pytest.mark.parametrize(
    "mutation",
    [
        "number",
        "year",
        "pasal",
        "ayat",
        "metadata_labels",
        "contradictory",
        "missing_year",
    ],
)
def test_query_reference_must_agree_with_metadata(synthetic_source, mutation):
    manifest, source, segments = synthetic_source
    manifest = copy.deepcopy(manifest)
    case = manifest["cases"][24 if mutation == "ayat" else 0]
    if mutation == "number":
        case["query"] = case["query"].replace("Nomor 100", "Nomor 999")
    elif mutation == "year":
        case["query"] = case["query"].replace("Tahun 2020", "Tahun 2021")
    elif mutation == "pasal":
        case["query"] = case["query"].replace("Pasal 1a", "Pasal 999")
    elif mutation == "ayat":
        case["query"] = case["query"].replace("ayat (1)", "ayat (2)")
    elif mutation == "metadata_labels":
        for field in ("source_regulation", "source_pasal", "source_ayat", "relevance"):
            case[field] = manifest["cases"][1][field]
    elif mutation == "contradictory":
        case["query"] += " Perpres 999/1999."
    elif mutation == "missing_year":
        case["query"] = case["query"].replace(" Tahun 2020", "")
    with pytest.raises(holdout.HoldoutIntegrityError, match="query reference"):
        holdout.validate_manifest(manifest, source, segments)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("count", "distribution"),
        ("duplicate", "duplicate"),
        ("missing_doc", "missing source document"),
        ("missing_ref", "reference mismatch"),
        ("grade", "invalid relevance grade"),
        ("published", "published judgment mismatch"),
        ("existing_query", "already published"),
        ("pair", "different|differ only"),
        ("same_relevance", "reference mismatch"),
        ("source", "source identity"),
    ],
)
def test_invalid_case_contracts_fail(synthetic_source, mutation, match):
    manifest, source, segments = synthetic_source
    manifest = copy.deepcopy(manifest)
    first = manifest["cases"][0]
    if mutation == "count":
        manifest["cases"].pop()
    elif mutation == "duplicate":
        first["query"] = manifest["cases"][1]["query"]
    elif mutation == "missing_doc":
        first["relevance"] = {"absent": 2}
    elif mutation == "missing_ref":
        first["source_pasal"] = "999"
    elif mutation == "grade":
        first["relevance"] = {"citation-0": True}
    elif mutation == "published":
        manifest["cases"][-1]["query"] += " changed"
    elif mutation == "existing_query":
        first["query"] = source.queries[-1].text
    elif mutation == "pair":
        manifest["cases"][37]["pair_id"], manifest["cases"][39]["pair_id"] = "1", "0"
    elif mutation == "same_relevance":
        manifest["cases"][37]["relevance"] = manifest["cases"][36]["relevance"]
    elif mutation == "source":
        manifest["source_md5"] = {}
    with pytest.raises(holdout.HoldoutIntegrityError, match=match):
        holdout.validate_manifest(manifest, source, segments)


def test_evaluation_uses_existing_evaluator(synthetic_source):
    manifest, source, segments = synthetic_source
    queries = holdout.validate_manifest(manifest, source, segments)
    benchmark = corpus.Benchmark(source.docs, queries, True)
    result = holdout.evaluate_holdout(benchmark, ["v1"], k=3)
    assert result == holdout.evaluate_holdout(benchmark, ["v1"], k=3)
    assert result["results"] == holdout.sparse_bench.evaluate(benchmark, ["v1"], k=3)["results"]
    assert set(result["per_case"]["v1"]) == {q.query_id for q in queries}
    for query in queries:
        single = corpus.Benchmark(source.docs, (query,), True)
        expected = holdout.sparse_bench.evaluate(single, ["v1"], k=3)["results"]["v1"][query.family]
        assert result["per_case"]["v1"][query.query_id] == expected
    with pytest.raises(ValueError, match="configs"):
        holdout.evaluate_holdout(benchmark, ["unavailable-config"])
    with pytest.raises(ValueError, match="k must"):
        holdout.evaluate_holdout(benchmark, ["v1"], k=0)


def test_cli_output_and_explicit_missing_config(synthetic_source, monkeypatch, tmp_path):
    manifest, source, segments = synthetic_source
    benchmark = corpus.Benchmark(
        source.docs, holdout.validate_manifest(manifest, source, segments), False
    )
    monkeypatch.setattr(holdout, "load_holdout", lambda *args, **kwargs: benchmark)
    monkeypatch.setattr(holdout, "code_identity", lambda: {"head": "synthetic-head"})
    output = tmp_path / "result.json"
    assert holdout.main(["--configs", "v1", "--no-header", "--out", str(output)]) == 0
    assert '"holdout_sha256"' in output.read_text()
    with pytest.raises(SystemExit) as exc:
        holdout.main(["--configs", "unavailable-config"])
    assert exc.value.code == 2


@pytest.mark.skipif(
    not corpus.DEFAULT_CACHE_DIR.is_dir(), reason="verified public cache unavailable"
)
def test_frozen_cases_match_cached_public_corpus():
    benchmark = holdout.load_holdout(corpus.DEFAULT_CACHE_DIR)
    no_header = holdout.load_holdout(corpus.DEFAULT_CACHE_DIR, header=False)
    assert len(benchmark.docs) == 1509
    assert len(benchmark.queries) == 60
    assert benchmark.queries == no_header.queries
    assert benchmark.docs != no_header.docs
