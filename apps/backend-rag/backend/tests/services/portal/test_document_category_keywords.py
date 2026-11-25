"""Regression coverage for portal document category keyword precedence."""

import pytest

from backend.services.portal.portal_service import PortalService


@pytest.mark.parametrize(
    ("document_type", "file_name", "expected"),
    (
        ("akta_lahir", "akta lahir anak.pdf", "family"),
        ("akta_nikah", "akta nikah.pdf", "family"),
        ("deed_of_establishment", "x.pdf", "pma"),
        ("other", "Deed of Establishment PT.pdf", "pma"),
    ),
)
def test_classify_document_category_regressions(
    document_type: str, file_name: str, expected: str
) -> None:
    assert PortalService._classify_document_category(document_type, file_name) == expected


@pytest.mark.parametrize(
    ("document_type", "file_name", "expected"),
    (
        ("akta_pendirian", "akta pendirian.pdf", "pma"),
        ("passport", "passport.pdf", "personal"),
        ("spt", "spt tahunan.pdf", "tax"),
        ("other", "birth certificate.pdf", "family"),
        ("other", "invoice.pdf", "other"),
    ),
)
def test_classify_document_category_existing_keywords(
    document_type: str, file_name: str, expected: str
) -> None:
    assert PortalService._classify_document_category(document_type, file_name) == expected


@pytest.mark.parametrize(
    ("document_type", "file_name", "expected"),
    (
        ("other", "Akta Lahir Anak.pdf", "family"),
        ("other", "Akta Kelahiran Anak.pdf", "family"),
        ("other", "akta_kelahiran.pdf", "family"),
        ("other", "Buku Nikah.pdf", "family"),
        ("other", "Birth Certificate.pdf", "family"),
        ("deed_of_establishment", "Akta Pendirian PT.pdf", "pma"),
        ("other", "Akta Perubahan PT.pdf", "pma"),
    ),
)
def test_classify_document_category_on_the_name_the_upload_path_keeps(
    document_type: str, file_name: str, expected: str
) -> None:
    kept = PortalService._sanitize_filename(file_name)
    assert PortalService._classify_document_category(document_type, kept) == expected
