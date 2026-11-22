"""The PDF check of a portal upload judges /JS and /AA as names, not as bytes.

Before parsing, the check looked for six names as raw bytes anywhere in the file. Two of
them are three bytes long: compressed image and font data hold them by chance, and a font
subset tag can begin with one, so ordinary documents were refused as "content does not match
its extension". The walk over the parsed objects forbids the same names as keys and values;
these tests show what it refuses on its own, and what the byte scan still does.
"""

from __future__ import annotations

import random
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

from backend.services.portal.upload_validation import (
    _PDF_ACTIVE_TOKENS,
    _pdf_object_graph_is_passive,
    content_matches_extension,
)


def _one_page() -> PdfWriter:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    return writer


def _bytes(writer: PdfWriter) -> bytes:
    payload = BytesIO()
    writer.write(payload)
    return payload.getvalue()


def _name(text: str) -> NameObject:
    return NameObject(text)


def _accepted(content: bytes) -> bool:
    return content_matches_extension(".pdf", content)


def _walk_alone_accepts(content: bytes) -> bool:
    return _pdf_object_graph_is_passive(PdfReader(BytesIO(content), strict=True).trailer)


# ── innocent documents ────────────────────────────────────────────────────────


def test_a_font_whose_subset_tag_begins_with_aa_is_accepted() -> None:
    writer = _one_page()
    font = DictionaryObject(
        {
            _name("/Type"): _name("/Font"),
            _name("/Subtype"): _name("/Type1"),
            _name("/BaseFont"): _name("/AAXYZQ+Helvetica"),
        }
    )
    writer.pages[0][_name("/Resources")] = DictionaryObject(
        {_name("/Font"): DictionaryObject({_name("/F1"): writer._add_object(font)})}
    )
    content = _bytes(writer)

    assert b"/AA" in content
    assert _accepted(content) is True


def _with_stream(data: bytes) -> bytes:
    writer = _one_page()
    stream = StreamObject()
    stream._data = data
    stream[_name("/Length")] = NumberObject(len(data))
    writer.pages[0][_name("/Contents")] = writer._add_object(stream)
    return _bytes(writer)


def test_a_stream_whose_data_holds_the_two_byte_runs_is_accepted() -> None:
    content = _with_stream(b"\x00\x01/JS\x02\x03/AA\x04 compressed data")

    assert b"/JS" in content and b"/AA" in content
    assert _accepted(content) is True


def test_a_large_stream_of_arbitrary_bytes_is_accepted_whatever_it_holds() -> None:
    """Two megabytes of fixed pseudo-random bytes that hold one of the runs, as scans do."""
    data = next(
        blob
        for seed in range(100)
        if b"/JS" in (blob := random.Random(seed).randbytes(2_000_000)) or b"/AA" in blob
    )
    for token in _PDF_ACTIVE_TOKENS:
        assert token not in data  # a long name is not there by chance

    assert _accepted(_with_stream(data)) is True


# ── documents that carry the names: refused by the walk over the objects ─────


def _aa_on_the_page() -> bytes:
    writer = _one_page()
    writer.pages[0][_name("/AA")] = DictionaryObject(
        {_name("/O"): DictionaryObject({_name("/S"): _name("/GoTo")})}
    )
    return _bytes(writer)


def _js_key() -> bytes:
    writer = _one_page()
    writer._root_object[_name("/Zz")] = DictionaryObject(
        {_name("/S"): _name("/Rendition"), _name("/JS"): TextStringObject("x")}
    )
    return _bytes(writer)


def _js_value_in_an_array() -> bytes:
    writer = _one_page()
    writer._root_object[_name("/Zz")] = ArrayObject([DictionaryObject({_name("/K"): _name("/JS")})])
    return _bytes(writer)


def _js_behind_a_reference() -> bytes:
    writer = _one_page()
    hidden = writer._add_object(DictionaryObject({_name("/JS"): TextStringObject("x")}))
    writer._root_object[_name("/Zz")] = ArrayObject([ArrayObject([hidden])])
    return _bytes(writer)


def _js_spelled_with_an_escape() -> bytes:
    """``/J#53`` is the name /JS to a PDF reader, and is not the bytes ``/JS``."""
    writer = _one_page()
    writer._root_object[_name("/Zz")] = DictionaryObject({_name("/Wwww"): TextStringObject("x")})
    content = _bytes(writer)
    assert content.count(b"/Wwww") == 1
    return content.replace(b"/Wwww", b"/J#53")  # same length: no offset moves


@pytest.mark.parametrize(
    "build",
    [_aa_on_the_page, _js_key, _js_value_in_an_array, _js_behind_a_reference],
    ids=["an AA key on the page", "a JS key", "JS as a value in an array", "JS behind a reference"],
)
def test_a_document_that_carries_one_of_the_two_names_is_refused(build) -> None:
    content = build()

    assert _walk_alone_accepts(content) is False
    assert _accepted(content) is False


def test_the_name_is_refused_in_a_spelling_the_bytes_do_not_show() -> None:
    content = _js_spelled_with_an_escape()

    assert b"/JS" not in content
    assert _accepted(content) is False


# ── what the byte scan still does, and what it no longer does ─────────────────


def _in_a_text(token: bytes) -> bytes:
    """A one-page document whose catalog holds a TEXT that reads like the name."""
    filler = "Q" * 24
    writer = _one_page()
    writer._root_object[_name("/Zz")] = TextStringObject(filler)
    content = _bytes(writer)
    assert content.count(filler.encode()) == 1
    return content.replace(filler.encode(), token.ljust(24))  # same length: no offset moves


@pytest.mark.parametrize("token", [b"/JavaScript", b"/Launch", b"/EmbeddedFile", b"/OpenAction"])
def test_a_long_name_is_still_refused_as_bytes_wherever_it_is_written(token: bytes) -> None:
    content = _in_a_text(token)

    assert _walk_alone_accepts(content) is True  # a text, not a name: no object carries it
    assert _accepted(content) is False


@pytest.mark.parametrize("token", [b"/JS", b"/AA"])
def test_a_text_that_reads_like_a_short_name_is_accepted(token: bytes) -> None:
    content = _in_a_text(token)

    assert token in content
    assert _accepted(content) is True


def test_an_object_nothing_refers_to_is_not_read_for_the_two_short_names() -> None:
    """The limit of this change. A reader does not reach such an object either."""
    writer = _one_page()
    writer._add_object(DictionaryObject({_name("/JS"): TextStringObject("x")}))
    content = _bytes(writer)

    assert b"/JS" in content
    assert _walk_alone_accepts(content) is True
    assert _accepted(content) is True
