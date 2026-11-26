"""The chapters of a law are read once, not once for every article.

``_extract_pasal_list`` asked, for each Pasal, which BAB it belongs to, and each question
re-read the whole document for its BAB headings. The cost grew with the square of the law:
a second for five hundred articles, a minute for four thousand. The headings are now found
once and each article's chapter is looked up among them.

The answers must not move. They are compared here with the rule as it was written before:
the last BAB heading that starts strictly before the article.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from backend.core.legal import structure_parser
from backend.core.legal.constants import BAB_PATTERN, PASAL_PATTERN
from backend.core.legal.structure_parser import LegalStructureParser

ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]


def _chapter_as_before(text: str, position: int) -> str | None:
    """The previous rule, word for word: scan every heading, take the last one before."""
    for bab_match in reversed(list(BAB_PATTERN.finditer(text))):
        if bab_match.start() < position:
            return f"BAB {bab_match.group(1)} - {bab_match.group(2).strip()}"
    return None


def _law(seed: int) -> str:
    """A made-up law: articles before the first chapter, chapters of any size, some empty,
    headings indented or split over two lines, and a chapter heading as the last line."""
    rng = random.Random(seed)
    lines: list[str] = []
    article = 0
    for _ in range(rng.randint(0, 3)):
        article += 1
        lines += [f"Pasal {article}", f"Ketentuan pembuka nomor {article}.", ""]
    for chapter in range(rng.randint(0, 9)):
        title = f"KETENTUAN {rng.choice(['UMUM', 'KHUSUS', 'PERALIHAN', 'PENUTUP'])} {chapter}"
        if rng.random() < 0.5:
            lines += [f"BAB {ROMAN[chapter]}", title, ""]
        else:
            lines += [f"BAB {ROMAN[chapter]} {title}", ""]
        for _ in range(rng.randint(0, 6)):
            article += 1
            indent = " " * rng.choice([0, 0, 2, 4])
            lines += [f"{indent}Pasal {article}", f"(1) Isi ayat satu pasal {article}."]
            if rng.random() < 0.4:
                lines += [f"(2) Isi ayat dua pasal {article} yang menyebut BAB II di tengah."]
            lines += [""]
    if rng.random() < 0.3:
        lines += ["BAB XII", "PENUTUP"]
    return "\n".join(lines)


def _large_law(articles: int, per_chapter: int = 25) -> str:
    lines: list[str] = []
    for number in range(1, articles + 1):
        if (number - 1) % per_chapter == 0:
            lines += [f"BAB {(number - 1) // per_chapter + 1}", "KETENTUAN", ""]
        lines += [f"Pasal {number}", f"(1) Isi pasal {number}.", ""]
    return "\n".join(lines)


class _CountingPattern:
    """The BAB pattern, counting how many times the document is scanned with it."""

    def __init__(self) -> None:
        self.scans = 0

    def finditer(self, text: str) -> Any:
        self.scans += 1
        return BAB_PATTERN.finditer(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(BAB_PATTERN, name)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> _CountingPattern:
    pattern = _CountingPattern()
    monkeypatch.setattr(structure_parser, "BAB_PATTERN", pattern)
    return pattern


# ---------------------------------------------------------------- the cost


@pytest.mark.parametrize("articles", [1, 50, 400])
def test_the_articles_of_a_law_are_listed_with_one_scan_for_its_chapters(
    counting: _CountingPattern, articles: int
) -> None:
    listed = LegalStructureParser()._extract_pasal_list(_large_law(articles))

    assert len(listed) == articles
    assert counting.scans == 1


def test_parsing_a_law_scans_for_chapters_as_often_whatever_its_number_of_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = []
    for articles in (25, 400):
        pattern = _CountingPattern()
        monkeypatch.setattr(structure_parser, "BAB_PATTERN", pattern)
        parsed = LegalStructureParser().parse(_large_law(articles))
        assert len(parsed["pasal_list"]) == articles
        scans.append(pattern.scans)

    assert scans[0] == scans[1]


# ---------------------------------------------------------------- the answers


@pytest.mark.parametrize("seed", range(60))
def test_every_article_is_given_the_chapter_the_previous_rule_gave_it(seed: int) -> None:
    text = _law(seed)
    starts = [pasal_match.start() for pasal_match in PASAL_PATTERN.finditer(text)]

    listed = LegalStructureParser()._extract_pasal_list(text)

    assert [article["bab_context"] for article in listed] == [
        _chapter_as_before(text, start) for start in starts
    ]


def test_the_made_up_laws_cover_the_cases_that_matter() -> None:
    """The battery is not vacuous: no chapter at all, articles before the first chapter,
    several chapters, and an article in each kind of place."""
    contexts = [
        [article["bab_context"] for article in LegalStructureParser()._extract_pasal_list(_law(s))]
        for s in range(60)
    ]

    assert any(found and all(context is None for context in found) for found in contexts)
    assert any(found and found[0] is None and found[-1] is not None for found in contexts)
    assert any(len({context for context in found if context}) >= 4 for found in contexts)


@pytest.mark.parametrize("seed", range(20))
def test_the_lookup_by_position_answers_as_before_at_every_position(seed: int) -> None:
    """Also exactly ON a heading's first character, where "strictly before" decides."""
    text = _law(seed)
    parser = LegalStructureParser()
    heading_starts = [bab_match.start() for bab_match in BAB_PATTERN.finditer(text)]
    positions = {0, len(text), len(text) + 5, *heading_starts}
    positions |= {start + shift for start in heading_starts for shift in (-1, 1)}
    positions |= set(range(0, len(text), 37))

    for position in sorted(positions):
        assert parser._find_bab_context(text, position) == _chapter_as_before(text, position)


def test_a_law_of_four_thousand_articles_keeps_every_article_in_its_chapter() -> None:
    listed = LegalStructureParser()._extract_pasal_list(_large_law(4000))

    assert len(listed) == 4000
    assert listed[0]["bab_context"] == "BAB 1 - KETENTUAN"
    assert listed[25]["bab_context"] == "BAB 2 - KETENTUAN"
    assert listed[-1]["bab_context"] == "BAB 160 - KETENTUAN"
