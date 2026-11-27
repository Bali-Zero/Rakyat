"""Regression coverage for structural headings after wrapped prose."""

import time

import pytest

from backend.core.legal.cleaner import LegalCleaner
from backend.core.legal.structure_parser import LegalStructureParser


def test_guilt_keeps_pasal_after_bagian_on_its_own_line() -> None:
    text = """BAB II
PERIZINAN
Bagian Kesatu
Umum
Pasal 2
Setiap orang wajib memiliki izin.
Pasal 3
Izin diberikan oleh Menteri."""

    cleaned = LegalCleaner().clean(text)
    parsed = LegalStructureParser().parse(cleaned)

    assert "Umum\nPasal 2" in cleaned
    assert [pasal["number"] for pasal in parsed["batang_tubuh"][0]["pasal"]] == ["2", "3"]


def test_guilt_keeps_pasal_after_unpunctuated_line_on_its_own_line() -> None:
    text = """Pasal 4
Cukup jelas
Pasal 5
Ketentuan ini berlaku."""

    cleaned = LegalCleaner().clean(text)
    parsed = LegalStructureParser().parse(cleaned)

    assert "Cukup jelas\nPasal 5" in cleaned
    assert [pasal["number"] for pasal in parsed["pasal_list"]] == ["4", "5"]


def test_guilt_keeps_bagian_and_paragraf_headings_on_their_own_lines() -> None:
    cleaned = LegalCleaner().clean(
        "umum\nBAB II\ncukup\nBagian Kedua\t\nlengkap\nParagraf 2\njelas\nPENJELASAN"
    )

    assert "umum\nBAB II" in cleaned
    assert "cukup\nBagian Kedua " in cleaned
    assert "lengkap\nParagraf 2" in cleaned
    assert "jelas\nPENJELASAN" in cleaned


def test_innocence_joins_wrapped_prose_and_cross_references() -> None:
    cleaner = LegalCleaner()

    assert (
        cleaner.clean("wajib memiliki\nIzin Usaha yang berlaku.")
        == "wajib memiliki Izin Usaha yang berlaku."
    )
    assert (
        cleaner.clean("dimaksud dalam\nPasal 5 ayat (1) huruf b")
        == "dimaksud dalam Pasal 5 ayat (1) huruf b"
    )
    assert cleaner.clean("Pasal 1\nketentuan berlaku.") == "Pasal 1\nketentuan berlaku."


def test_innocence_cleans_adversarial_whitespace_in_linear_time() -> None:
    cleaner = LegalCleaner()

    for text in ("a\nA" * 20_000, "a" + "\n" * 20_000 + "Pasal 1"):
        start = time.process_time()
        cleaner.clean(text)
        assert time.process_time() - start < 1.0


def test_guilt_joins_wrapped_penjelasan_reference_into_prose() -> None:
    text = "ketentuan yang diubah dalam\nPenjelasan Umum angka 2 huruf a berlaku."
    cleaned = LegalCleaner().clean(text)

    assert cleaned == "ketentuan yang diubah dalam Penjelasan Umum angka 2 huruf a berlaku."


def test_guilt_joins_wrapped_bab_and_bagian_ini_into_prose() -> None:
    cleaner = LegalCleaner()

    assert (
        cleaner.clean("sebagaimana dimaksud dalam\nBab ini\nberlaku.")
        == "sebagaimana dimaksud dalam Bab ini\nberlaku."
    )
    assert (
        cleaner.clean("sebagaimana dimaksud dalam\nBagian ini\nberlaku.")
        == "sebagaimana dimaksud dalam Bagian ini\nberlaku."
    )
    assert cleaner.clean("diatur dalam\nBAB ini berlaku") == "diatur dalam BAB ini berlaku"


def test_innocence_real_headings_stay_on_their_own_lines() -> None:
    cleaner = LegalCleaner()

    for heading in (
        "PENJELASAN",
        "PENJELASAN UMUM",
        "PENJELASAN ATAS",
        "PENJELASAN ATAS UNDANG-UNDANG NOMOR 1 TAHUN 2000",
        "BAB IV",
        "BAB 4",
        "Bagian Kedua",
        "Bagian Kesepuluh",
        "Bagian Pertama",
        "Bagian 2",
        "Paragraf 1",
    ):
        cleaned = cleaner.clean(f"ketentuan umum\n{heading}\nisi")
        assert f"ketentuan umum\n{heading}" in cleaned, heading

    assert "tidak\nPENJELASAN\nATAS\nUNDANG" in cleaner.clean("tidak\nPENJELASAN\nATAS\nUNDANG")


def test_innocence_parser_counts_on_fixture_shapes() -> None:
    text = (
        "BAB I\nKETENTUAN\nPasal 1\nsebagaimana diubah dalam\n"
        "Penjelasan Umum angka 2 huruf a.\nPasal 2\nsebagaimana dimaksud dalam\n"
        "Bab ini berlaku.\nPasal 3\nIsi.\nPENJELASAN\nATAS\nUNDANG-UNDANG\nPasal 1\nCukup jelas"
    )
    cleaned = LegalCleaner().clean(text)
    parsed = LegalStructureParser().parse(cleaned)

    assert [p["number"] for p in parsed["batang_tubuh"][0]["pasal"]] == ["1", "2", "3"]
    assert cleaned.index("\nPENJELASAN\n") > cleaned.index("Pasal 3")


# Whitespace that `\s` matches and `WHITESPACE_FIXES[0]` (`[ \t]+`) does NOT collapse, so
# it reaches the heading lookahead as a long run: the whole cost of a quadratic heading.
RUN_CHARS = {
    "nbsp": "\xa0",
    "formfeed": "\f",
    "vtab": "\v",
    "cr": "\r",
    "emspace": " ",
    "space": " ",
}
RUN = 20_000
BUDGET_SECONDS = 1.0


def _timed_clean(text: str) -> float:
    start = time.perf_counter()
    LegalCleaner().clean(text)
    return time.perf_counter() - start


@pytest.mark.parametrize("ws", sorted(RUN_CHARS), ids=sorted(RUN_CHARS))
def test_guilt_signer_witness_penjelasan_umum_is_linear(ws: str) -> None:
    """`PENJELASAN UMUM` + a run of whitespace + a failing character, the shape that made
    the previous head's UMUM/ATAS tail `[^a-z\\n]*` overlap the shared `[^\\S\\n]*` tail."""
    payload = "a\nPENJELASAN UMUM" + RUN_CHARS[ws] * RUN + "x"
    if ws == "emspace":
        assert " " in payload
    took = _timed_clean(payload)
    assert took < BUDGET_SECONDS, f"{ws}: {took:.3f}s"


@pytest.mark.parametrize("ws", sorted(RUN_CHARS), ids=sorted(RUN_CHARS))
@pytest.mark.parametrize(
    "keyword",
    [
        "BAB",
        "Bagian",
        "BAGIAN",
        "Bagian Kedua",
        "Paragraf",
        "Pasal",
        "PENJELASAN",
        "PENJELASAN UMUM",
        "PENJELASAN ATAS",
        "PENJELASAN PASAL",
    ],
)
def test_guilt_every_heading_alternative_is_linear_on_a_failing_run(keyword: str, ws: str) -> None:
    for tail in ("x", "1x", "a"):
        took = _timed_clean(f"a\n{keyword}" + RUN_CHARS[ws] * RUN + tail)
        assert took < BUDGET_SECONDS, f"{keyword!r} {ws} {tail!r}: {took:.3f}s"


def test_innocence_umum_tail_still_matches_headings_with_trailing_spaces() -> None:
    cleaner = LegalCleaner()

    for heading in (
        "PENJELASAN UMUM\xa0\xa0",
        "PENJELASAN ATAS UNDANG-UNDANG NOMOR 1 TAHUN 2000\f",
        "PENJELASAN UMUM I. UMUM",
        "PENJELASAN PASAL 5A",
    ):
        cleaned = cleaner.clean(f"ketentuan umum\n{heading}\nisi")
        assert cleaned.startswith("ketentuan umum\nPENJELASAN"), heading
    # A lowercase letter anywhere on the line makes it prose, so it is joined.
    assert cleaner.clean("dalam\nPENJELASAN UMUM angka 2") == "dalam PENJELASAN UMUM angka 2"


def test_guilt_bagian_ordinal_is_a_closed_vocabulary() -> None:
    cleaner = LegalCleaner()

    assert (
        cleaner.clean("ketentuan yang diatur dalam\nBagian Keuangan\nberlaku.")
        == "ketentuan yang diatur dalam Bagian Keuangan\nberlaku."
    )
    assert (
        cleaner.clean("diatur dalam\nBagian Kedua Puluhan") == "diatur dalam Bagian Kedua Puluhan"
    )


def test_guilt_keeps_compact_bagian_ordinals_on_their_own_lines() -> None:
    cleaner = LegalCleaner()
    for heading in (
        "Bagian Keduabelas",
        "BAGIAN KEDUABELAS",
        "Bagian Kedua Belas",
        "Bagian Kedua Puluh",
        "Bagian Kedua Puluh Satu",
        "Bagian Ketigapuluh Dua",
        "BAGIAN KETIGA",
        "BAGIAN KEDUA BELAS",
        "Bagian Kesepuluh",
        "Bagian Kesebelas",
        "Bagian Kesatu",
        "Bagian Pertama",
        "Bagian 2",
        "Bagian 12",
    ):
        cleaned = cleaner.clean(f"ketentuan umum\n{heading}\nisi")
        assert f"ketentuan umum\n{heading}\nisi" == cleaned, heading


def test_innocence_joins_bagian_ordinal_prose() -> None:
    cleaner = LegalCleaner()

    assert (
        cleaner.clean("ketentuan yang diatur dalam\nBagian kedua ini\nberlaku.")
        == "ketentuan yang diatur dalam Bagian kedua ini\nberlaku."
    )
    assert (
        cleaner.clean("diatur dalam\nBagian Kedua Belas ayat")
        == "diatur dalam Bagian Kedua Belas ayat"
    )


@pytest.mark.parametrize(
    "ordinal",
    [
        "Keduabelas",
        "KEDUABELAS",
        "Kedua Belas",
        "Kedua Puluh",
        "Kedua Puluh Satu",
        "Ketigapuluh Dua",
        "Kesebelas",
        "Kesepuluh",
        "Kesatu",
        "Pertama",
        "12",
    ],
)
def test_guilt_bagian_ordinals_are_linear_on_failing_runs(ordinal: str) -> None:
    took = _timed_clean(f"a\nBagian {ordinal}" + "\xa0" * RUN + "x")
    assert took < BUDGET_SECONDS, f"{ordinal}: {took:.3f}s"
