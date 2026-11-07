"""Offline benchmark of the production BM25 sparse path on the public Perpres set.

No Qdrant, embeddings, LLM or network (except an explicit --fetch). Production code is
measured unchanged; the result decides the legal tokenizer v2 work.
"""

import argparse
import asyncio
import json
import logging
import math
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.core.bm25_vectorizer import BM25Vectorizer
from backend.tests.benchmarks.legal_retrieval import perpres_set
from backend.tests.benchmarks.legal_retrieval.perpres_set import Benchmark

logger = logging.getLogger(__name__)

# backend/app/core/config.py: bm25_vocab_size=30000, bm25_k1=1.5, bm25_b=0.75 (used by
# backend/services/search/search_service.py to build the query-side vectorizer). Legal ingestion
# (backend/services/ingestion/legal_ingestion_service.py) uses a default BM25Vectorizer(), whose
# avg_doc_length stays 500.0 (never updated).
PROD_PARAMS: dict[str, float | int] = {"vocab_size": 30000, "k1": 1.5, "b": 0.75}

CONFIGS = ("v1", "v1_idf")

SparseVector = Mapping[str, Any]


class SparseIndex:
    """Inverted index over Qdrant-style sparse vectors, scored by dot product."""

    def __init__(self, doc_ids: Sequence[str], vectors: Sequence[SparseVector]) -> None:
        self.doc_ids = list(doc_ids)
        self.postings: dict[int, list[tuple[int, float]]] = defaultdict(list)
        for pos, vec in enumerate(vectors):
            for index, value in zip(vec["indices"], vec["values"], strict=True):
                self.postings[index].append((pos, value))

    @classmethod
    def from_texts(cls, docs: Mapping[str, str], vectorizer: BM25Vectorizer) -> "SparseIndex":
        ids = list(docs)
        return cls(ids, [vectorizer.generate_sparse_vector(docs[i]) for i in ids])

    def idf(self, index: int) -> float:
        """Qdrant IDF modifier, df counted per sparse index (hash bucket)."""
        n_docs, df = len(self.doc_ids), len(self.postings.get(index, ()))
        return math.log(1 + (n_docs - df + 0.5) / (df + 0.5))

    def search(self, query_vec: SparseVector, k: int, idf: bool) -> list[tuple[str, float]]:
        scores: dict[int, float] = defaultdict(float)
        for index, weight in zip(query_vec["indices"], query_vec["values"], strict=True):
            weight = weight * self.idf(index) if idf else weight
            for pos, value in self.postings.get(index, ()):
                scores[pos] += weight * value
        ranked = sorted(((p, s) for p, s in scores.items() if s > 0), key=lambda x: (-x[1], x[0]))
        return [(self.doc_ids[p], s) for p, s in ranked[:k]]


def recall_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    relevant = {d for d, g in relevance.items() if g > 0}
    return len(relevant.intersection(ranked[:k])) / len(relevant) if relevant else 0.0


def mrr_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    for rank, doc_id in enumerate(ranked[:k], start=1):
        if relevance.get(doc_id, 0) > 0:
            return 1 / rank
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevance: Mapping[str, int], k: int) -> float:
    def dcg(grades: Sequence[int]) -> float:
        return sum((2**g - 1) / math.log2(rank + 1) for rank, g in enumerate(grades, start=1))

    ideal = dcg(sorted((g for g in relevance.values() if g > 0), reverse=True)[:k])
    return dcg([relevance.get(d, 0) for d in ranked[:k]]) / ideal if ideal else 0.0


def evaluate(benchmark: Benchmark, configs: Sequence[str], k: int = 10) -> dict[str, Any]:
    """Score every query under each config; aggregate per query family."""
    vectorizer = BM25Vectorizer(**PROD_PARAMS)  # type: ignore[arg-type]
    index = SparseIndex.from_texts(benchmark.docs, vectorizer)
    counts: dict[str, int] = defaultdict(int)
    for query in benchmark.queries:
        counts[query.family] += 1
    results: dict[str, dict[str, dict[str, float]]] = {}
    for config in configs:
        if config not in CONFIGS:
            raise ValueError(f"unknown config {config!r}; expected one of {CONFIGS}")
        sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for query in benchmark.queries:
            qvec = vectorizer.generate_query_sparse_vector(query.text)
            ranked = index.search(qvec, k + 1, idf=config == "v1_idf")
            top = [doc_id for doc_id, _ in ranked[:k]]
            acc = sums[query.family]
            acc["recall_at_k"] += recall_at_k(top, query.relevance, k)
            acc["mrr_at_k"] += mrr_at_k(top, query.relevance, k)
            acc["ndcg_at_k"] += ndcg_at_k(top, query.relevance, k)
            acc["empty_query_rate"] += not qvec["indices"]
            acc["tied_cutoff_rate"] += len(ranked) > k and ranked[k - 1][1] == ranked[k][1]
        results[config] = {
            family: {
                "n": counts[family],
                **{m: round(v / counts[family], 4) for m, v in acc.items()},
            }
            for family, acc in sorted(sums.items())
        }
    return {
        "meta": {
            "zenodo_record": perpres_set.ZENODO_RECORD_ID,
            "md5": perpres_set.ZENODO_FILES,
            "n_docs": len(benchmark.docs),
            "n_queries": dict(sorted(counts.items())),
            "header": benchmark.header,
            "k": k,
            "prod_params": PROD_PARAMS,
            "configs": list(configs),
        },
        "results": results,
    }


def _markdown(report: Mapping[str, Any]) -> str:
    meta = report["meta"]
    lines = [
        f"header={meta['header']} k={meta['k']} docs={meta['n_docs']}",
        "",
        "| config | family | n | recall@k | mrr@k | ndcg@k | empty | tied |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for config, families in report["results"].items():
        for family, m in families.items():
            lines.append(
                f"| {config} | {family} | {m['n']} | {m['recall_at_k']} | {m['mrr_at_k']} "
                f"| {m['ndcg_at_k']} | {m['empty_query_rate']} | {m['tied_cutoff_rate']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=perpres_set.DEFAULT_CACHE_DIR)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--configs", default=",".join(CONFIGS))
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.fetch:
        asyncio.run(perpres_set.fetch_dataset(args.data_dir))
    benchmark = perpres_set.load_benchmark(args.data_dir, header=not args.no_header)
    report = evaluate(benchmark, args.configs.split(","), k=args.k)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    sys.stdout.write(_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
