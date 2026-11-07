"""Tests for the legal sparse-retrieval benchmark (offline, no network)."""

import hashlib
import math
from pathlib import Path

import pytest

from backend.core.bm25_vectorizer import BM25Vectorizer
from backend.tests.benchmarks.legal_retrieval import perpres_set
from backend.tests.benchmarks.legal_retrieval.perpres_set import (
    Benchmark,
    DatasetIntegrityError,
    Query,
    Segment,
    citation_queries,
    parse_pasal,
    parse_regulation,
    render_chunk,
    verify_dataset,
)
from backend.tests.benchmarks.legal_retrieval.sparse_bench import (
    SparseIndex,
    evaluate,
    mrr_at_k,
    ndcg_at_k,
    recall_at_k,
)

REG = "nomor 81 tahun 2025"
TITLES = {REG: "pengadaan barang"}


def _seg(row_id: str, segmen: str, sub: str, text: str, reg: str = REG) -> Segment:
    return Segment(row_id, reg, segmen, sub, text)


def test_metrics_perfect_none_and_graded() -> None:
    rel = {"a": 2, "b": 1}
    assert recall_at_k(["a", "b", "x"], rel, 3) == 1.0
    assert mrr_at_k(["a", "b", "x"], rel, 3) == 1.0
    assert ndcg_at_k(["a", "b", "x"], rel, 3) == pytest.approx(1.0)
    assert recall_at_k(["x", "y", "a"], rel, 2) == 0.0
    assert mrr_at_k(["x", "y", "a"], rel, 2) == 0.0
    assert ndcg_at_k(["x", "y", "a"], rel, 2) == 0.0
    assert mrr_at_k(["x", "b"], rel, 5) == 0.5
    dcg = 1 / math.log2(2) + 3 / math.log2(3)
    ideal = 3 / math.log2(2) + 1 / math.log2(3)
    assert ndcg_at_k(["b", "a"], rel, 2) == pytest.approx(dcg / ideal)


def test_parse_regulation() -> None:
    assert parse_regulation("nomor 103 tahun 2020") == ("103", "2020")
    assert parse_regulation("nomor 103") is None
    assert parse_regulation("") is None


def test_parse_pasal() -> None:
    assert parse_pasal("pasal 1 - ayat (2)") == ("1", "2")
    assert parse_pasal("pasal 8 - teks") == ("8", None)
    assert parse_pasal("pasal 5a - teks") == ("5a", None)
    assert parse_pasal("pasal i - ayat (2)") == ("i", "2")
    assert parse_pasal("pasal 3 - teks (Part 2)") == ("3", None)
    assert parse_pasal("a") == (None, None)
    assert parse_pasal("-") == (None, None)


def test_citation_queries() -> None:
    segs = [
        _seg("r1", "batang_tubuh", "pasal 8 - ayat (1)", "isi satu"),
        _seg("r2", "batang_tubuh", "pasal 8 - ayat (2)", "isi dua"),
        _seg("r3", "batang_tubuh", "pasal 9 - teks", "isi tiga"),
        _seg("r4", "menimbang", "a", "bahwa"),
        _seg("r5", "batang_tubuh", "-", "tanpa pasal"),
    ]
    queries = citation_queries(segs)
    assert [q.query_id for q in queries] == [
        "citation_formal:81-2025:8",
        "citation_formal:81-2025:9",
        "citation_short:81-2025:8",
        "citation_short:81-2025:9",
    ]
    formal, short = queries[0], queries[2]
    assert formal.family == "citation_formal"
    assert formal.text == "Pasal 8 Peraturan Presiden Nomor 81 Tahun 2025"
    assert short.text == "pasal 8 perpres 81/2025"
    assert dict(formal.relevance) == {"r1": 2, "r2": 2}
    assert dict(queries[1].relevance) == {"r3": 2}
    assert citation_queries(segs) == queries


def test_render_chunk_with_and_without_header() -> None:
    ayat = _seg("r1", "batang_tubuh", "pasal 8 - ayat (2)", "isi dua")
    teks = _seg("r2", "batang_tubuh", "pasal 8 - teks", "isi pasal")
    head = "[CONTEXT: PERPRES - NO 81 - TAHUN 2025 - TENTANG pengadaan barang - Pasal 8]"
    assert render_chunk(ayat, TITLES) == f"{head}\n\nPasal 8 Ayat (2)\nisi dua"
    assert render_chunk(teks, TITLES) == f"{head}\n\nisi pasal"
    assert render_chunk(ayat, TITLES, header=False) == "Pasal 8 Ayat (2)\nisi dua"
    assert render_chunk(teks, TITLES, header=False) == "isi pasal"
    menimbang = _seg("r3", "menimbang", "a", "bahwa x")
    assert render_chunk(menimbang, TITLES).startswith(
        "[CONTEXT: PERPRES - NO 81 - TAHUN 2025 - TENTANG pengadaan barang]\n\n"
    )


def test_sparse_index_scores_idf_ties_and_zero_docs() -> None:
    index = SparseIndex(
        ["d1", "d2", "d3", "d4"],
        [
            {"indices": [1, 2], "values": [1.0, 2.0]},
            {"indices": [1], "values": [1.0]},
            {"indices": [2], "values": [5.0]},
            {"indices": [3], "values": [9.0]},
        ],
    )
    query = {"indices": [1], "values": [2.0]}
    assert index.search(query, 10, idf=False) == [("d1", 2.0), ("d2", 2.0)]
    expected = 2.0 * math.log(1 + (4 - 2 + 0.5) / (2 + 0.5))
    hits = index.search(query, 1, idf=True)
    assert hits == [("d1", pytest.approx(expected))]
    assert index.search({"indices": [], "values": []}, 5, idf=False) == []
    assert index.search({"indices": [3], "values": [1.0]}, 5, idf=False) == [("d4", 9.0)]


def test_prod_tokenizer_drops_citation_anchors() -> None:
    """Characterizes the UNCHANGED prod tokenizer: the bug the next PR fixes.

    Stopwords and the short-number filter erase the pasal number and the regulation
    number, so a citation query collapses to a bare year (or "perpres" plus the year).
    """
    vec = BM25Vectorizer()
    assert vec.tokenize("Pasal 8 Peraturan Presiden Nomor 81 Tahun 2025") == ["2025"]
    assert vec.tokenize("pasal 8 perpres 81/2025") == ["perpres", "2025"]


def _write_dataset(tmp_path: Path, payload: bytes) -> dict[str, str]:
    md5 = hashlib.md5(payload).hexdigest()
    for name in perpres_set.ZENODO_FILES:
        (tmp_path / name).write_bytes(payload)
    return dict.fromkeys(perpres_set.ZENODO_FILES, md5)


def test_integrity_wrong_bytes_and_correct_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(DatasetIntegrityError, match="qrels_final.csv"):
        verify_dataset(tmp_path)
    monkeypatch.setattr(perpres_set, "ZENODO_FILES", _write_dataset(tmp_path, b"abc"))
    verify_dataset(tmp_path)
    (tmp_path / "qrels_final.csv").write_bytes(b"tampered")
    with pytest.raises(DatasetIntegrityError, match="qrels_final.csv"):
        verify_dataset(tmp_path)


def test_evaluate_tiny_benchmark() -> None:
    bench = Benchmark(
        docs={
            "d1": "pengadaan barang pemerintah kontrak",
            "d2": "tarif pajak penghasilan badan usaha",
            "d3": "jaminan pembiayaan pembangunan nasional",
        },
        queries=(
            Query("q1", "literal", "pengadaan barang kontrak", {"d1": 2}),
            Query("q2", "semantic", "pajak penghasilan", {"d2": 2, "d3": 1}),
            Query("q3", "literal", "pasal", {"d1": 2}),
        ),
        header=True,
    )
    report = evaluate(bench, ["v1", "v1_idf"], k=2)
    assert set(report["results"]) == {"v1", "v1_idf"}
    assert report["meta"]["n_queries"] == {"literal": 2, "semantic": 1}
    for config in ("v1", "v1_idf"):
        assert set(report["results"][config]) == {"literal", "semantic"}
        for row in report["results"][config].values():
            assert {"n", "recall_at_k", "mrr_at_k", "ndcg_at_k"} <= set(row)
            for key in ("recall_at_k", "mrr_at_k", "ndcg_at_k"):
                assert 0.0 <= row[key] <= 1.0
    assert report["results"]["v1"]["literal"]["empty_query_rate"] == 0.5
