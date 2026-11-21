"""WA DLP — identifier shapes the redactor must catch (letters glued to
digits, parenthesised local phone, common bank-account labels) and the
ordinary text it must keep leaving alone. All identifiers are synthetic."""

from __future__ import annotations

import time

import pytest

from backend.services.rag.agentic.wa_dlp import redact_package_fields


def _out(text: str) -> str:
    result = redact_package_fields([], [{"collection": "t", "text": text, "score": 1.0}])
    return result.chunks[0]["text"]


GUILT = [
    ("NIK3201990011223344 tolong", "3201990011223344"),
    ("id3201990011223344", "3201990011223344"),
    ("3201990011223344x", "3201990011223344"),
    ("081298765432abc", "081298765432"),  # pragma: allowlist secret - synthetic phone shape, not a key
    ("tel (0812) 9876 5432", "9876 5432"),
    ("tel (0812)98765432", "98765432"),
    ("wa:+6281298765432x", "6281298765432"),
    ("rek BCA 7712345678", "7712345678"),
    ("rek. 7712345678", "7712345678"),
    ("a/c 7712345678", "7712345678"),
    ("acct 7712345678", "7712345678"),
]


@pytest.mark.parametrize(("text", "secret"), GUILT)
def test_proven_shape_is_redacted(text: str, secret: str) -> None:
    assert secret not in _out(text)


INNOCENT = [
    "harga Rp 12.500.000 sudah termasuk pajak",
    "tanggal 20 Agustus 2026 atau 20/08/2026",
    "periode 2024-2026 berlaku",
    "KBLI 56101 dan 55130",
    "invoice INV-2026-000123 jatuh tempo besok",
    "diskon 15% untuk paket tahunan",
    "order 12 pcs (3 box) dikirim hari ini",
    "tolong rek ulang jadwalnya, saya cek rekening kami di website",
    "open an account for the new company, ticket 4417",
    "call (021) 555 1234 office hours",
    "Peraturan BKPM 5/2025 dan UU 6/2011",
    "visa C1 atau E33 untuk 180 hari",
]


@pytest.mark.parametrize("text", INNOCENT)
def test_ordinary_text_is_untouched(text: str) -> None:
    assert _out(text) == text


def test_long_text_without_at_sign_is_fast() -> None:
    text = "kata.biasa-tanpa.tanda" * 1000
    text = text[:20000]
    start = time.perf_counter()
    assert _out(text) == text
    assert time.perf_counter() - start < 0.5


def test_email_is_still_redacted() -> None:
    assert "budi.test@example.test" not in _out("kirim ke budi.test@example.test ya")
