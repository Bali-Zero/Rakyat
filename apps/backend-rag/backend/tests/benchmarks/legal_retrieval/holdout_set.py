"""Frozen reference-retrieval robustness cases on the verified public Perpres corpus.

The 48 new formulations and 12 published controls reuse an already-seen corpus.
They evaluate retrieval of references, not legal validity or generated answers.
Run this file with a candidate backend on PYTHONPATH to test that candidate
without copying or changing its source. There is no download or model call.
"""

import argparse
import csv
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.core import bm25_vectorizer
from backend.tests.benchmarks.legal_retrieval import perpres_set, sparse_bench
from backend.tests.benchmarks.legal_retrieval.perpres_set import Benchmark, Query, Segment

DEFAULT_FIXTURE = Path(__file__).with_name("holdout_v1.json")
FROZEN_SHA256 = "108d42088fbf36fc0d5f75a9938c016bf212e79b7c773e9d4bd2c0a2375d93ef"
FAMILY_COUNTS = {
    "citation_variant": 24,
    "ayat_exact": 12,
    "year_contrast": 12,
    "published_literal": 6,
    "published_semantic": 6,
}
# Full matches deliberately cover only the frozen reference-formulation grammar.
# Extra, missing or contradictory references cannot be silently ignored.
REFERENCE_PATTERNS = tuple(
    re.compile(
        pattern.format(
            number=r"(?P<number>\d+)",
            year=r"(?P<year>\d{4})",
            pasal=r"(?P<pasal>\d+[a-z]*|[ivxlcdm]+)",
            ayat=r"(?P<ayat>\d+[a-z]*|[ivxlcdm]+)",
        ),
        re.IGNORECASE,
    )
    for pattern in (
        r"Tolong temukan Pasal {pasal} dalam Peraturan Presiden Nomor {number} Tahun {year}\.",
        r"Perpres {number}/{year}: saya mencari bagian Pasal {pasal}\.",
        r"Untuk tahun {year}, ambil Pasal {pasal} dari Perpres nomor {number}\.",
        r"PASAL {pasal} — PERATURAN PRESIDEN NO\. {number} TAHUN {year}",
        r"Dokumen Perpres nomor {number}, tahun {year}; buka pasal {pasal}\.",
        r"Bisakah Anda menunjukkan pasal {pasal} pada Perpres {number} Tahun {year}\?",
        r"Rujukan yang saya perlukan adalah Peraturan Presiden {number}/{year}, Pasal {pasal}\.",
        r"Cari kutipan pasal {pasal} di peraturan presiden nomor {number} tahun {year}\.",
        r"Tunjukkan hanya ayat \({ayat}\) dari Pasal {pasal}, Peraturan Presiden Nomor {number} Tahun {year}\.",
        r"Saya memilih Perpres Nomor {number} Tahun {year}\. Temukan isi Pasal {pasal} pada versi tahun tersebut\.",
    )
)


class HoldoutIntegrityError(ValueError):
    """The frozen cases or their derivation do not match the verified sources."""


def normalize_query(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def query_reference(text: str) -> tuple[str, str, str | None]:
    """Extract the complete reference from one supported formulation."""
    for pattern in REFERENCE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            fields = match.groupdict()
            return (
                f"nomor {fields['number']} tahun {fields['year']}",
                fields["pasal"].casefold(),
                fields["ayat"].casefold() if fields.get("ayat") else None,
            )
    raise HoldoutIntegrityError("query reference does not match the supported grammar")


def read_manifest(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN_SHA256:
        raise HoldoutIntegrityError("holdout SHA-256 mismatch")
    return json.loads(raw)


def read_segments(cache_dir: Path) -> list[Segment]:
    with (cache_dir / "dataset_korpus_per_ayat.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        return [
            Segment(r["row_id"], r["nomor_peraturan"], r["segmen"], r["sub_segmen"], r["value"])
            for r in csv.DictReader(handle, delimiter=";")
        ]


def validate_manifest(
    manifest: dict[str, Any], source: Benchmark, segments: list[Segment]
) -> tuple[Query, ...]:
    """Re-derive every relevance judgment from corpus identity or published qrels."""
    if (
        manifest.get("schema_version") != 1
        or manifest.get("source_md5") != perpres_set.ZENODO_FILES
    ):
        raise HoldoutIntegrityError("manifest source identity mismatch")
    cases = manifest["cases"]
    if Counter(c["family"] for c in cases) != FAMILY_COUNTS:
        raise HoldoutIntegrityError("expected the frozen 60-case family distribution")
    ids = [c["case_id"] for c in cases]
    texts = [normalize_query(c["query"]) for c in cases]
    if len(set(ids)) != len(ids) or len(set(texts)) != len(texts) or "" in texts:
        raise HoldoutIntegrityError("empty or duplicate case id/query")
    original_texts = {normalize_query(q.text) for q in source.queries}
    published = {q.query_id: q for q in source.queries if q.family in ("literal", "semantic")}
    references: dict[tuple[str, str, str | None], dict[str, int]] = defaultdict(dict)
    for segment in segments:
        pasal, ayat = perpres_set.parse_pasal(segment.sub_segmen)
        if segment.segmen == "batang_tubuh" and pasal:
            references[(segment.regulation, pasal, None)][segment.row_id] = 2
            if ayat:
                references[(segment.regulation, pasal, ayat)][segment.row_id] = 2
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    suffix_count = 0
    queries = []
    for case in cases:
        family = case["family"]
        relevance = case["relevance"]
        if not relevance or not set(relevance) <= source.docs.keys():
            raise HoldoutIntegrityError(f"{case['case_id']}: missing source document")
        if any(type(grade) is not int or grade <= 0 for grade in relevance.values()):
            raise HoldoutIntegrityError(f"{case['case_id']}: invalid relevance grade")
        if family.startswith("published_"):
            original = published.get(case["source_query_id"])
            if (
                original is None
                or family != f"published_{original.family}"
                or case["query"] != original.text
                or relevance != original.relevance
                or case["is_new_formulation"] is not False
                or case["derivation"] != "published_qrels"
            ):
                raise HoldoutIntegrityError(f"{case['case_id']}: published judgment mismatch")
        else:
            key = (case["source_regulation"], case["source_pasal"], case["source_ayat"])
            if (
                relevance != references.get(key)
                or case["is_new_formulation"] is not True
                or case["derivation"] != "structured_corpus_reference"
                or case["source_query_id"] is not None
            ):
                raise HoldoutIntegrityError(f"{case['case_id']}: corpus reference mismatch")
            if normalize_query(case["query"]) in original_texts:
                raise HoldoutIntegrityError(f"{case['case_id']}: formulation already published")
            if query_reference(case["query"]) != key:
                raise HoldoutIntegrityError(f"{case['case_id']}: query reference mismatch")
            if (family == "ayat_exact") != (case["source_ayat"] is not None):
                raise HoldoutIntegrityError(f"{case['case_id']}: ayat scope mismatch")
            suffix_count += family == "citation_variant" and bool(
                re.fullmatch(r"\d+[a-zA-Z]+", case["source_pasal"])
            )
        if family == "year_contrast":
            if not case["pair_id"]:
                raise HoldoutIntegrityError("contrast case needs a pair id")
            pairs[case["pair_id"]].append(case)
        elif case["pair_id"] is not None:
            raise HoldoutIntegrityError("pair id outside contrast family")
        queries.append(Query(case["case_id"], family, case["query"], relevance))
    if suffix_count < 4 or len(pairs) != 6:
        raise HoldoutIntegrityError("missing suffix articles or year pairs")
    for pair in pairs.values():
        if len(pair) != 2:
            raise HoldoutIntegrityError("contrast pair must have two cases")
        left, right = pair
        left_reg = perpres_set.parse_regulation(left["source_regulation"])
        right_reg = perpres_set.parse_regulation(right["source_regulation"])
        if (
            left_reg is None
            or right_reg is None
            or left_reg[0] != right_reg[0]
            or left_reg[1] == right_reg[1]
            or left["source_pasal"] != right["source_pasal"]
            or set(left["relevance"]) & set(right["relevance"])
        ):
            raise HoldoutIntegrityError("contrast references must differ only in year")
    return tuple(queries)


def load_holdout(
    cache_dir: Path, header: bool = True, fixture: Path = DEFAULT_FIXTURE
) -> Benchmark:
    manifest = read_manifest(fixture)
    source = perpres_set.load_benchmark(cache_dir, header=header)
    queries = validate_manifest(manifest, source, read_segments(cache_dir))
    return Benchmark(source.docs, queries, header)


def evaluate_holdout(benchmark: Benchmark, configs: list[str], k: int = 10) -> dict[str, Any]:
    """Use the same evaluator for family aggregates and single-case results."""
    if k < 1 or k >= len(benchmark.docs):
        raise ValueError("k must be positive and smaller than the corpus")
    if not configs or len(set(configs)) != len(configs) or set(configs) - set(sparse_bench.CONFIGS):
        raise ValueError(f"configs must be unique members of {sparse_bench.CONFIGS}")
    report = sparse_bench.evaluate(benchmark, configs, k=k)
    per_case = replace(
        benchmark, queries=tuple(replace(q, family=q.query_id) for q in benchmark.queries)
    )
    report["per_case"] = sparse_bench.evaluate(per_case, configs, k=k)["results"]
    return report


def code_identity() -> dict[str, Any]:
    paths = [
        Path(module.__file__).resolve() for module in (perpres_set, sparse_bench, bm25_vectorizer)
    ]
    result = subprocess.run(
        ["git", "-C", str(paths[0].parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        "head": result.stdout.strip(),
        "files_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
        },
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=perpres_set.DEFAULT_CACHE_DIR)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--configs", default="v1")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        benchmark = load_holdout(args.data_dir, header=not args.no_header, fixture=args.fixture)
        report = evaluate_holdout(benchmark, args.configs.split(","), args.k)
        report["meta"].update(
            {
                "holdout_sha256": FROZEN_SHA256,
                "new_formulations": 48,
                "published_controls": 12,
                "data_dir": str(args.data_dir.resolve()),
                "code": code_identity(),
                "scope": "Sparse reference retrieval on an already-seen corpus; no legal-answer evaluation.",
            }
        )
    except (
        OSError,
        ValueError,
        perpres_set.DatasetIntegrityError,
        subprocess.CalledProcessError,
    ) as exc:
        parser.error(str(exc))
    output = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
