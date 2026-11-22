"""Regression tests for indented operative-section headings."""

from backend.core.legal.structure_parser import LegalStructureParser


def _indented_document(article_count: int) -> str:
    articles = "\n\n".join(
        f" Pasal {number}\nKetentuan sintetis untuk artikel {number}."
        for number in range(1, article_count + 1)
    )
    return f"""Menimbang:
Pertimbangan sintetis.

Mengingat:
Dasar sintetis.

 MEMUTUSKAN:
Menetapkan: PERATURAN SINTETIS

{articles}"""


def test_indented_headings_keep_all_thirty_leading_articles():
    result = LegalStructureParser().parse(_indented_document(30))

    assert [pasal["number"] for pasal in result["pasal_list"]] == [
        str(number) for number in range(1, 31)
    ]


def test_short_indented_document_keeps_all_articles():
    result = LegalStructureParser().parse(_indented_document(3))

    assert [pasal["number"] for pasal in result["pasal_list"]] == ["1", "2", "3"]


def test_column_zero_document_keeps_preamble_out_of_articles():
    text = """Menimbang:
Pertimbangan sintetis.

MEMUTUSKAN:
Menetapkan: PERATURAN SINTETIS

Pasal 1
Ketentuan sintetis pertama.

Pasal 2
Ketentuan sintetis kedua."""

    result = LegalStructureParser().parse(text)

    assert "Menimbang" in result["konsiderans"]
    assert [pasal["number"] for pasal in result["pasal_list"]] == ["1", "2"]
