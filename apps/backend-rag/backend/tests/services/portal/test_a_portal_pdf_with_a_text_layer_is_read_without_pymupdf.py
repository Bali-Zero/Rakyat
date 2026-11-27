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
        raise RuntimeError("pypdf down")

    monkeypatch.setattr(pypdf, "PdfReader", boom)
    res = await DocumentOCR._extract_from_pdf(_pdf(SENTENCE))
    assert res["success"] is True, res
    assert SENTENCE in res["text"]
    assert res["pages"] == 1


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
