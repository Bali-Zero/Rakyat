"""Public Perpres retrieval set (Zenodo 21450375, CC-BY-4.0): fetch, verify, parse, render."""

import asyncio
import csv
import hashlib
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ZENODO_RECORD_ID = "21450375"
ZENODO_FILES: dict[str, str] = {
    "qrels_final.csv": "e17999fa2dcc615cf08d8dd541e151fc",
    "ds_literal_final.csv": "f1d4da77917eaafc8bbedc949351cd5e",
    "ds_semantic_final.csv": "1f09d2112ac164fbec8bf8263f3646d7",
    "dataset_korpus_per_ayat.csv": "908208aff530eb96c122196d5ebcc597",
}
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nuzantara-bench" / f"zenodo-{ZENODO_RECORD_ID}"
_URL = "https://zenodo.org/api/records/" + ZENODO_RECORD_ID + "/files/{name}/content"

_REGULATION = re.compile(r"^nomor\s+(\d+)\s+tahun\s+(\d+)$")
_PASAL = re.compile(
    r"^pasal\s+(\w+)\s+-\s+(?:ayat\s+\((\w+)\)|teks)(?:\s+\(part\s+\d+\))?$", re.IGNORECASE
)


class DatasetIntegrityError(Exception):
    """A dataset file is missing or its md5 differs from the pinned value."""


@dataclass(frozen=True)
class Segment:
    row_id: str
    regulation: str
    segmen: str
    sub_segmen: str
    text: str


@dataclass(frozen=True)
class Query:
    query_id: str
    family: str
    text: str
    relevance: Mapping[str, int]


@dataclass(frozen=True)
class Benchmark:
    docs: Mapping[str, str]
    queries: tuple[Query, ...]
    header: bool


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def verify_dataset(cache_dir: Path) -> None:
    """Raise DatasetIntegrityError unless every pinned file is present with the pinned md5."""
    for name, expected in ZENODO_FILES.items():
        path = cache_dir / name
        if not path.is_file():
            raise DatasetIntegrityError(f"{name}: missing in {cache_dir}")
        if _md5(path) != expected:
            raise DatasetIntegrityError(f"{name}: md5 mismatch")


async def fetch_dataset(cache_dir: Path) -> None:
    """Download the missing or mismatching files, verifying md5 before they replace the cache."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        for name, expected in ZENODO_FILES.items():
            target = cache_dir / name
            if target.is_file() and _md5(target) == expected:
                continue
            response = await client.get(_URL.format(name=name))
            response.raise_for_status()
            tmp = target.with_suffix(".part")
            tmp.write_bytes(response.content)
            if _md5(tmp) != expected:
                tmp.unlink()
                raise DatasetIntegrityError(f"{name}: md5 mismatch after download")
            tmp.replace(target)
            logger.info("fetched %s", name)


def parse_regulation(nomor: str) -> tuple[str, str] | None:
    """`nomor 103 tahun 2020` -> ("103", "2020")."""
    match = _REGULATION.match(nomor.strip())
    return (match.group(1), match.group(2)) if match else None


def parse_pasal(sub_segmen: str) -> tuple[str | None, str | None]:
    """`pasal 1 - ayat (2)` -> ("1", "2"); `pasal 8 - teks` -> ("8", None)."""
    match = _PASAL.match(sub_segmen.strip())
    return (match.group(1), match.group(2)) if match else (None, None)


def render_chunk(segment: Segment, titles: Mapping[str, str], header: bool = True) -> str:
    """Render a segment the way production legal chunks look.

    Mirrors backend/core/legal/chunker.py: `_build_context` (parts joined by " - " inside
    `[CONTEXT: ...]`, Pasal as the last part), `_create_chunk` (`f"{context}\\n\\n{content}"`)
    and `_split_by_ayat` (ayat body `f"Pasal {n} Ayat ({a})\\n{text}"`).
    """
    pasal, ayat = parse_pasal(segment.sub_segmen)
    body = f"Pasal {pasal} Ayat ({ayat})\n{segment.text}" if ayat else segment.text
    if not header:
        return body
    number, year = parse_regulation(segment.regulation) or ("UNKNOWN", "UNKNOWN")
    parts = [
        "PERPRES",
        f"NO {number}",
        f"TAHUN {year}",
        f"TENTANG {titles.get(segment.regulation, 'UNKNOWN')}",
    ]
    if pasal:
        parts.append(f"Pasal {pasal}")
    return f"[CONTEXT: {' - '.join(parts)}]\n\n{body}"


def citation_queries(segments: Sequence[Segment]) -> list[Query]:
    """Derive deterministic pasal-citation queries (formal and short) from the corpus."""
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for seg in segments:
        regulation = parse_regulation(seg.regulation)
        pasal, _ = parse_pasal(seg.sub_segmen)
        if seg.segmen == "batang_tubuh" and regulation and pasal:
            groups[(regulation[0], regulation[1], pasal)].append(seg.row_id)
    queries: list[Query] = []
    for (number, year, pasal), row_ids in groups.items():
        relevance = dict.fromkeys(row_ids, 2)
        key = f"{number}-{year}:{pasal}"
        queries.append(
            Query(
                f"citation_formal:{key}",
                "citation_formal",
                f"Pasal {pasal} Peraturan Presiden Nomor {number} Tahun {year}",
                relevance,
            )
        )
        queries.append(
            Query(
                f"citation_short:{key}",
                "citation_short",
                f"pasal {pasal} perpres {number}/{year}",
                relevance,
            )
        )
    return sorted(queries, key=lambda q: (q.family, q.query_id))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def load_benchmark(cache_dir: Path, header: bool = True) -> Benchmark:
    """Verify the cache and build docs plus published and derived queries."""
    verify_dataset(cache_dir)
    segments = [
        Segment(r["row_id"], r["nomor_peraturan"], r["segmen"], r["sub_segmen"], r["value"])
        for r in _read_csv(cache_dir / "dataset_korpus_per_ayat.csv")
    ]
    titles = {s.regulation: s.text for s in segments if s.segmen == "judul"}
    docs = {s.row_id: render_chunk(s, titles, header) for s in segments}
    published: dict[str, Query] = {}
    for row in _read_csv(cache_dir / "qrels_final.csv"):
        grade = int(row["label"])
        query = published.setdefault(
            row["query_id"],
            Query(row["query_id"], row["query_type"].lower(), row["query_text"], {}),
        )
        if grade > 0:
            query.relevance[row["doc_id"]] = grade  # type: ignore[index]
    queries = (*published.values(), *citation_queries(segments))
    return Benchmark(docs=docs, queries=queries, header=header)


if __name__ == "__main__":
    asyncio.run(fetch_dataset(DEFAULT_CACHE_DIR))
