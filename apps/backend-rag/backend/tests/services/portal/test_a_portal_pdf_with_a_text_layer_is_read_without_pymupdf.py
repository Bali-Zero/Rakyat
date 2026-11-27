"""A portal PDF with a text layer is read when PyMuPDF is absent (pypdf / pdfplumber)."""

import io

import pytest

from backend.services.portal import document_processing as dp
from backend.services.portal.document_processing import DocumentOCR

SENTENCE = "Passport expiry 2031-05-14"


def _pdf(text: str | None) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    if text:
        c.drawString(72, 700, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def _blank_pdf(page_count: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _no_pymupdf(monkeypatch):
    monkeypatch.setattr(dp, "PDF_VISION_AVAILABLE", False)
    monkeypatch.setattr(dp, "PYMUPDF_AVAILABLE", False)


async def test_digital_pdf_text_layer_is_read():
    res = await DocumentOCR._extract_from_pdf(_pdf(SENTENCE))
    assert res["success"] is True, res
    assert SENTENCE in res["text"]
    assert res["pages"] == 1
    assert "not available" not in (res["error"] or "")


async def test_pdfplumber_used_when_pypdf_fails(monkeypatch):
    import pypdf

    def boom(*a, **k):
        raise ValueError("pypdf down")

    monkeypatch.setattr(pypdf, "PdfReader", boom)
    res = await DocumentOCR._extract_from_pdf(_pdf(SENTENCE))
    assert res["success"] is True, res
    assert SENTENCE in res["text"]
    assert res["pages"] == 1


async def test_pdf_with_more_than_100_pages_stops_before_extraction_or_pdfplumber(monkeypatch):
    import pdfplumber
    import pypdf

    original_reader = pypdf.PdfReader
    extract_calls = 0
    pdfplumber_calls = 0

    class SpyPage:
        def __init__(self, page):
            self.page = page

        def extract_text(self):
            nonlocal extract_calls
            extract_calls += 1
            return self.page.extract_text()

    class SpyReader:
        def __init__(self, *args, **kwargs):
            self.reader = original_reader(*args, **kwargs)

        @property
        def pages(self):
            return [SpyPage(page) for page in self.reader.pages]

    def pdfplumber_open(*args, **kwargs):
        nonlocal pdfplumber_calls
        pdfplumber_calls += 1
        raise AssertionError("pdfplumber must not open an over-limit PDF")

    monkeypatch.setattr(pypdf, "PdfReader", SpyReader)
    monkeypatch.setattr(pdfplumber, "open", pdfplumber_open)
    res = await DocumentOCR._extract_from_pdf(_blank_pdf(101))
    assert res["success"] is False
    assert res["error"] == "PDF has too many pages to read (101 > 100)"
    assert extract_calls == 0
    assert pdfplumber_calls == 0


async def test_pypdf_decompression_limit_does_not_fall_back_to_pdfplumber(monkeypatch):
    import pdfplumber
    import pypdf

    pdfplumber_calls = 0

    def limit_reached(*args, **kwargs):
        raise pypdf.errors.LimitReachedError("synthetic limit")

    def pdfplumber_open(*args, **kwargs):
        nonlocal pdfplumber_calls
        pdfplumber_calls += 1
        raise AssertionError("pdfplumber must not open after a pypdf limit")

    monkeypatch.setattr(pypdf, "PdfReader", limit_reached)
    monkeypatch.setattr(pdfplumber, "open", pdfplumber_open)
    res = await DocumentOCR._extract_from_pdf(_pdf(SENTENCE))
    assert res["success"] is False
    assert res["error"] == "PDF exceeds a decompression limit"
    assert pdfplumber_calls == 0


async def test_pdfplumber_page_cap_applies_after_pypdf_failure(monkeypatch):
    import pypdf
    from pdfplumber.page import Page

    extract_calls = 0
    original_extract_text = Page.extract_text

    def pypdf_boom(*args, **kwargs):
        raise ValueError("synthetic pypdf failure")

    def spy_extract_text(self, *args, **kwargs):
        nonlocal extract_calls
        extract_calls += 1
        return original_extract_text(self, *args, **kwargs)

    monkeypatch.setattr(pypdf, "PdfReader", pypdf_boom)
    monkeypatch.setattr(Page, "extract_text", spy_extract_text)
    res = await DocumentOCR._extract_from_pdf(_blank_pdf(101))
    assert res["success"] is False
    assert res["error"] == "PDF has too many pages to read (101 > 100)"
    assert extract_calls == 0


async def test_pdf_with_100_pages_is_extracted(monkeypatch):
    import pypdf

    original_reader = pypdf.PdfReader
    extract_calls = 0

    class SpyPage:
        def __init__(self, page):
            self.page = page

        def extract_text(self):
            nonlocal extract_calls
            extract_calls += 1
            return self.page.extract_text()

    class SpyReader:
        def __init__(self, *args, **kwargs):
            self.reader = original_reader(*args, **kwargs)

        @property
        def pages(self):
            return [SpyPage(page) for page in self.reader.pages]

    monkeypatch.setattr(pypdf, "PdfReader", SpyReader)
    res = await DocumentOCR._extract_from_pdf(_blank_pdf(100))
    assert res["success"] is False
    assert "no text layer" in res["error"]
    assert extract_calls == 100


async def test_blank_pdf_reports_no_text_layer():
    res = await DocumentOCR._extract_from_pdf(_pdf(None))
    assert res["success"] is False
    assert "no text layer" in res["error"]


async def test_garbage_bytes_fail_without_exception():
    res = await DocumentOCR._extract_from_pdf(b"not a pdf at all")
    assert res["success"] is False
    assert "text-layer extraction failed" in res["error"]


async def test_pymupdf_branch_does_not_use_new_helper(monkeypatch):
    class FakePage:
        def get_text(self):
            return "fitz text"

    class FakeDoc(list):
        def close(self):
            pass

    class FakeFitz:
        @staticmethod
        def open(stream, filetype):
            return FakeDoc([FakePage()])

    def forbidden(*a, **k):
        raise AssertionError("text-layer helper must not run on the PyMuPDF path")

    monkeypatch.setattr(dp, "PYMUPDF_AVAILABLE", True)
    monkeypatch.setattr(dp, "fitz", FakeFitz, raising=False)
    monkeypatch.setattr(DocumentOCR, "_extract_pdf_text_layer_sync", forbidden, raising=False)
    res = await DocumentOCR._extract_from_pdf(_pdf(SENTENCE))
    assert res["success"] is True
    assert res["text"] == "fitz text"
