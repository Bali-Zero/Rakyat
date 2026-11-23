"""DOCX field instruction validation contracts."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from docx import Document

from backend.services.portal.upload_validation import content_matches_extension

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_STRICT_WORD_NAMESPACE = "http://purl.oclc.org/ooxml/wordprocessingml/main"


def _synthetic_docx_with_word_part(part_name: str, part_xml: str) -> bytes:
    document = Document()
    document.add_paragraph("Synthetic document")
    payload = BytesIO()
    document.save(payload)

    updated = BytesIO()
    with ZipFile(BytesIO(payload.getvalue())) as source, ZipFile(updated, "w") as destination:
        for info in source.infolist():
            destination.writestr(
                info,
                part_xml.encode("utf-8")
                if info.filename == part_name
                else source.read(info.filename),
            )
        if part_name not in source.namelist():
            destination.writestr(part_name, part_xml.encode("utf-8"))
    return updated.getvalue()


def _word_part(body: str, namespace: str = _WORD_NAMESPACE) -> str:
    return f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'


def _complex_field(*instruction_runs: str) -> str:
    runs = "".join(f"<w:r><w:instrText>{text}</w:instrText></w:r>" for text in instruction_runs)
    return (
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        f"{runs}"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>shown result</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )


@pytest.mark.parametrize(
    ("part_name", "body"),
    [
        ("word/document.xml", '<w:fldSimple w:instr="DDEAUTO &quot;synthetic&quot;"/>'),
        (
            "word/document.xml",
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText>INCLUDETEXT "http://example.invalid/x.docx"</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
        ),
        (
            "word/document.xml",
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            "<w:r><w:instrText>INCLUDE</w:instrText></w:r>"
            '<w:r><w:instrText>TEXT "synthetic"</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
        ),
        ("word/header1.xml", '<w:fldSimple w:instr="LINK &quot;synthetic&quot;"/>'),
        (
            "word/document.xml",
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            "<w:r><w:instrText>IF 1 = 1</w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            "<w:r><w:instrText>DDE synthetic</w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
        ),
        ("word/document.xml", '<w:fldSimple w:instr="ddeauto &quot;synthetic&quot;"/>'),
    ],
)
def test_content_matches_extension_refuses_external_word_fields(part_name: str, body: str) -> None:
    content = _synthetic_docx_with_word_part(part_name, _word_part(body))

    assert not content_matches_extension(".docx", content)


@pytest.mark.parametrize(
    "body",
    [
        '<w:fldSimple w:instr="PAGE"/><w:fldSimple w:instr="TOC"/>'
        '<w:fldSimple w:instr="DATE"/>'
        '<w:fldSimple w:instr="HYPERLINK &quot;https://example.invalid&quot;"/>',
        "<w:p><w:r><w:t>Plain synthetic document</w:t></w:r></w:p>",
    ],
)
def test_content_matches_extension_accepts_safe_word_fields(body: str) -> None:
    content = _synthetic_docx_with_word_part("word/document.xml", _word_part(body))

    assert content_matches_extension(".docx", content)


@pytest.mark.parametrize(
    "body",
    [
        _complex_field('INCLUDETEXT"http://example.invalid/x.docx"'),
        _complex_field('  INCLUDEPICTURE "http://example.invalid/logo.png" \\d'),
        _complex_field("DDE", 'AUTO "synthetic"'),
        '<w:fldSimple w:instr="LINK&quot;synthetic&quot;"/>',
    ],
)
def test_a_field_name_followed_by_a_quote_or_split_across_runs_is_refused(body: str) -> None:
    content = _synthetic_docx_with_word_part("word/document.xml", _word_part(body))

    assert not content_matches_extension(".docx", content)


@pytest.mark.parametrize(
    "field_name",
    [
        "DDE",
        "DDEAUTO",
        "INCLUDE",
        "INCLUDETEXT",
        "INCLUDEPICTURE",
        "LINK",
        "IMPORT",
        "DATABASE",
        "RD",
    ],
)
def test_every_field_that_runs_or_fetches_is_refused(field_name: str) -> None:
    body = f'<w:fldSimple w:instr="{field_name} &quot;synthetic&quot;"/>'
    content = _synthetic_docx_with_word_part("word/document.xml", _word_part(body))

    assert not content_matches_extension(".docx", content)


def test_an_external_field_whose_path_is_a_nested_field_is_refused():
    body = (
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText>INCLUDETEXT &quot;</w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText>FILENAME</w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>name</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "<w:r><w:instrText>&quot;</w:instrText></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    content = _synthetic_docx_with_word_part("word/document.xml", _word_part(body))

    assert not content_matches_extension(".docx", content)


def test_a_strict_namespace_field_is_refused():
    body = '<w:fldSimple w:instr="DDEAUTO &quot;synthetic&quot;"/>'
    content = _synthetic_docx_with_word_part(
        "word/document.xml", _word_part(body, namespace=_STRICT_WORD_NAMESPACE)
    )

    assert not content_matches_extension(".docx", content)


@pytest.mark.parametrize(
    "body",
    [
        _complex_field('HYPERLINK "https://example.invalid"'),
        _complex_field("PAGE"),
        _complex_field("= 1 + 1"),
    ],
)
def test_a_safe_complex_field_and_its_shown_result_pass(body: str) -> None:
    content = _synthetic_docx_with_word_part("word/document.xml", _word_part(body))

    assert content_matches_extension(".docx", content)


def test_a_header_part_with_a_safe_field_passes():
    content = _synthetic_docx_with_word_part(
        "word/header1.xml", _word_part('<w:fldSimple w:instr="PAGE"/>')
    )

    assert content_matches_extension(".docx", content)
