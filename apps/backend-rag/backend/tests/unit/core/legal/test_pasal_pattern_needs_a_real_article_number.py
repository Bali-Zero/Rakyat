"""Regression coverage for article headings versus line-wrapped prose."""

import time

from backend.core.legal.constants import PASAL_PATTERN
from backend.core.legal.structure_parser import LegalStructureParser


def test_pasal_ini_remains_in_the_preceding_article() -> None:
    text = "Pasal 4\nSetiap orang wajib.\nPasal ini berlaku sejak diundangkan.\nlain."

    articles = LegalStructureParser().parse(text)["pasal_list"]

    assert [article["number"] for article in articles] == ["4"]
    assert "Pasal ini berlaku sejak diundangkan." in articles[0]["text"]


def test_real_headings_and_boundaries_still_split_articles() -> None:
    headings = "Pasal I\nPertama.\nPasal II\nKedua.\nPasal 5A\nKetiga."
    assert [match.group(1) for match in PASAL_PATTERN.finditer(headings)] == ["I", "II", "5A"]

    penjelasan = "Pasal 4\nKetentuan.\nPENJELASAN\nUraian."
    assert PASAL_PATTERN.search(penjelasan).group(2).strip() == "Ketentuan."

    bab = "Pasal 4\nKetentuan.\nBAB II\nKETENTUAN LANJUTAN"
    assert PASAL_PATTERN.search(bab).group(2).strip() == "Ketentuan."


def test_pasal_pattern_handles_large_near_heading_inputs_linearly() -> None:
    cases = ("Pasal 1" + "\n" * 20_000, "Pasal ini\n" * 20_000)

    for text in cases:
        started = time.process_time()
        PASAL_PATTERN.findall(text)
        assert time.process_time() - started < 1.0


def test_ocr_headings_with_o_for_zero_stay_their_own_articles() -> None:
    # A scanned law's text layer reads 0 as O; the heading must still close the article above.
    text = (
        "Pasal 199\nKetentuan pertama.\nPasal 2OO\nKetentuan kedua.\nPasal lOO\nKetentuan ketiga."
    )

    articles = LegalStructureParser().parse(text)["pasal_list"]

    assert [article["number"] for article in articles] == ["199", "2O", "l"]
    assert "Ketentuan kedua." not in articles[0]["text"]
    assert "Ketentuan kedua." in articles[1]["text"]


def test_capitalised_prose_after_pasal_is_not_a_heading() -> None:
    text = "Pasal 4\nSetiap orang wajib.\nPasal Ini mulai berlaku.\nPasal lain tetap."

    articles = LegalStructureParser().parse(text)["pasal_list"]

    assert [article["number"] for article in articles] == ["4"]
