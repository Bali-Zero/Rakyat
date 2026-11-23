"""DOCX field instruction validation contracts."""

from io import BytesIO
from zipfile import ZipFile

import pytest
from docx import Document

from backend.services.portal.upload_validation import content_matches_extension

_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_STRICT_WORD_NAMESPACE = "http://purl.oclc.org/ooxml/wordprocessingml/main"
_MC_NAMESPACE = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_HEADER_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"


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
    return (
        f'<w:document xmlns:w="{namespace}" xmlns:mc="{_MC_NAMESPACE}" xmlns:x="urn:example">'
        f"<w:body>{body}</w:body></w:document>"
    )


def _run(inner: str) -> str:
    return f"<w:r>{inner}</w:r>"


def _char(kind: str) -> str:
    return _run(f'<w:fldChar w:fldCharType="{kind}"/>')


def _instr(text: str, tag: str = "instrText") -> str:
    return _run(f"<w:{tag}>{text}</w:{tag}>")


def _docx_with_declared_part(part_name: str, content_type: str, part_xml: str) -> bytes:
    base = _synthetic_docx_with_word_part("word/document.xml", _word_part("<w:p/>"))
    updated = BytesIO()
    with ZipFile(BytesIO(base)) as source, ZipFile(updated, "w") as destination:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "[Content_Types].xml":
                override = f'<Override PartName="/{part_name}" ContentType="{content_type}"/>'
                payload = payload.replace(b"</Types>", override.encode() + b"</Types>")
            destination.writestr(info, payload)
        destination.writestr(part_name, part_xml.encode("utf-8"))
    return updated.getvalue()


def _refused(body: str) -> bool:
    content = _synthetic_docx_with_word_part("word/document.xml", _word_part(body))
    return not content_matches_extension(".docx", content)


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


@pytest.mark.parametrize(
    "part_name", ["WORD/header1.XML", "headers/header1.xml", "customXml/item9.xml"]
)
def test_a_field_in_any_parsed_part_is_refused(part_name: str) -> None:
    body = '<w:p><w:fldSimple w:instr="DDEAUTO &quot;synthetic&quot;"/></w:p>'
    content = _synthetic_docx_with_word_part(part_name, _word_part(body))

    assert not content_matches_extension(".docx", content)


def test_a_part_declared_as_xml_under_another_extension_is_refused():
    content = _docx_with_declared_part(
        "word/header9.bin", _HEADER_TYPE, _word_part('<w:p><w:fldSimple w:instr="PAGE"/></w:p>')
    )

    assert not content_matches_extension(".docx", content)


@pytest.mark.parametrize(
    "body",
    [
        '<w:altChunk w:id="chunk"/>',
        '<w:p><w:subDoc w:id="sub"/></w:p>',
        "<w:frameset/>",
        "<w:p><w:frame/></w:p>",
        _run('<w:fldChar w:fldCharType="begin"><w:fldData>AA==</w:fldData></w:fldChar>')
        + _instr("ADDIN synthetic")
        + _char("end"),
    ],
)
def test_imported_content_sub_documents_frames_and_field_data_are_refused(body: str) -> None:
    assert _refused(body)


@pytest.mark.parametrize(
    "body",
    [
        _char("begin")
        + _char("begin")
        + _instr('QUOTE "DDEAUTO"')
        + _char("end")
        + _instr(' "calc"')
        + _char("end"),
        _char("begin")
        + _instr("INCLUD")
        + _char("begin")
        + _instr('QUOTE "ETEXT"')
        + _char("end")
        + _instr(' "https://example.invalid/x.docx"')
        + _char("end"),
        _char("begin") + '<w:fldSimple w:instr="QUOTE DDEAUTO"/>' + _instr(" x") + _char("end"),
    ],
)
def test_a_field_that_builds_its_name_from_a_nested_field_is_refused(body: str) -> None:
    assert _refused(body)


@pytest.mark.parametrize(
    "body",
    [
        _char("begin")
        + "<w:del>"
        + _instr("SAFE ", "delInstrText")
        + "</w:del>"
        + _instr('DDEAUTO "synthetic"')
        + _char("end"),
        _char("begin")
        + "<w:ins>"
        + _instr("SAFE ")
        + "</w:ins>"
        + _instr('DDEAUTO "synthetic"')
        + _char("end"),
        _char("begin")
        + "<w:del>"
        + _instr("SAFE ")
        + "</w:del>"
        + _instr('DDEAUTO "synthetic"')
        + _char("end"),
        _char("begin")
        + '<mc:AlternateContent><mc:Choice Requires="x">'
        + _instr("SAFE ")
        + "</mc:Choice><mc:Fallback>"
        + _instr('DDEAUTO "synthetic"')
        + "</mc:Fallback></mc:AlternateContent>"
        + _char("end"),
        _char("begin")
        + _run("<x:wrap><w:instrText>SAFE </w:instrText></x:wrap>")
        + _instr('DDEAUTO "synthetic"')
        + _char("end"),
        "<w:p>"
        + _char("begin")
        + "</w:p><w:tbl><w:tr><w:tc><w:p>"
        + _instr('DDEAUTO "synthetic"')
        + _char("end")
        + "</w:p></w:tc></w:tr></w:tbl>",
    ],
)
def test_a_field_whose_pieces_word_may_keep_or_drop_apart_is_refused(body: str) -> None:
    assert _refused(body)


@pytest.mark.parametrize(
    "instruction",
    [
        '"INCLUDETEXT" "https://example.invalid/x.docx"',
        '\\* INCLUDETEXT "https://example.invalid/x.docx"',
        'INCLUD\u200bETEXT "https://example.invalid/x.docx"',
        'LIN\u00adK "synthetic"',
        "1 INCLUDETEXT",
    ],
)
def test_a_field_whose_name_is_not_plain_letters_is_refused(instruction: str) -> None:
    assert _refused(_char("begin") + _instr(instruction) + _char("end"))
    assert _refused(f'<w:fldSimple w:instr="{instruction.replace(chr(34), "&quot;")}"/>')


@pytest.mark.parametrize(
    "body",
    [
        _char("begin") + _instr('DDEAUTO "synthetic"'),
        _char("begin") + _instr("PAGE"),
        _run('<w:fldChar w:fldCharType="Begin"/>') + _instr("PAGE") + _char("end"),
        '<w:fldSimple instr="DDEAUTO &quot;synthetic&quot;"/>',
    ],
)
def test_an_open_field_an_unknown_field_character_or_a_bare_instr_is_refused(body: str) -> None:
    assert _refused(body)


@pytest.mark.parametrize(
    "body",
    [
        _complex_field("PAGE \\* MERGEFORMAT"),
        _complex_field('DATE \\@ "d MMMM yyyy"'),
        _char("begin") + _char("end"),
        _char("begin")
        + _instr("IF ")
        + _char("begin")
        + _instr("PAGE")
        + _char("separate")
        + _run("<w:t>1</w:t>")
        + _char("end")
        + _instr(' = 1 "one" "other"')
        + _char("separate")
        + _char("end"),
        _char("begin")
        + _instr("= ")
        + _char("begin")
        + _instr("PAGE")
        + _char("end")
        + _instr(" + 1")
        + _char("end"),
        "<w:p>"
        + _char("begin")
        + _instr("TOC \\o")
        + "</w:p><w:p>"
        + _char("separate")
        + _run("<w:t>Contents</w:t>")
        + "</w:p><w:p>"
        + _char("end")
        + "</w:p>",
        "<w:ins>"
        + _char("begin")
        + "</w:ins><w:ins>"
        + _instr("PAGE")
        + "</w:ins><w:ins>"
        + _char("separate")
        + "</w:ins><w:ins>"
        + _char("end")
        + "</w:ins>",
        "<w:del>" + _char("begin") + _instr("PAGE", "delInstrText") + _char("end") + "</w:del>",
        '<mc:AlternateContent><mc:Choice Requires="x">'
        + _complex_field("PAGE")
        + "</mc:Choice><mc:Fallback>"
        + _complex_field("PAGE")
        + "</mc:Fallback>"
        + "</mc:AlternateContent>",
        '<w:hyperlink w:anchor="top">' + _complex_field("PAGE") + "</w:hyperlink>",
        _char("begin")
        + _instr('TOC \\o "1-3" \\h')
        + '<w:hyperlink w:anchor="_Toc1">'
        + _char("separate")
        + _run("<w:t>Introduction</w:t>")
        + _char("end")
        + "</w:hyperlink>",
    ],
)
def test_ordinary_fields_tracked_whole_or_in_a_text_box_pass(body: str) -> None:
    assert not _refused(body)


@pytest.mark.parametrize(
    "body",
    [
        _char("begin")
        + '<mc:AlternateContent><mc:Choice Requires="x">'
        + _char("end")
        + "</mc:Choice><mc:Fallback/></mc:AlternateContent>"
        + _instr('DDEAUTO "synthetic"')
        + _char("end"),
        "<w:ins>"
        + _char("begin")
        + "</w:ins><w:del>"
        + _instr("SAFE ", "delInstrText")
        + "</w:del><w:ins>"
        + _instr('DDEAUTO "synthetic"')
        + _char("end")
        + "</w:ins>",
        "<w:del>"
        + _char("begin")
        + _instr('DDEAUTO "synthetic"', "delInstrText")
        + _char("end")
        + "</w:del>",
    ],
)
def test_a_field_closed_in_a_branch_mixed_revisions_or_deleted_whole_is_still_read(
    body: str,
) -> None:
    assert _refused(body)
