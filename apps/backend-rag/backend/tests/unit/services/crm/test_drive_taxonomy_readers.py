"""Reader compatibility for canonical and historical Drive category names."""

import pytest

from backend.services.crm.drive_poll_service import _infer_document_type


@pytest.mark.parametrize(
    ("folder", "expected"),
    [
        ("01_Profile", "profile_document"),
        ("02_Immigration", "immigration_document"),
        ("03_Company_and_Tax", "other"),
        ("04_Archive", "misc_document"),
        ("01_Legal_and_Licensing", "company_document"),
        ("02_Tax_Returns_and_Payments", "tax_document"),
        ("03_Finance_and_Banking", "other"),
        ("99_Archive", "misc_document"),
        ("00_Profile", "profile_document"),
        ("01_Immigration", "immigration_document"),
        ("02_Company", "company_document"),
        ("03_Tax", "tax_document"),
        ("04_Family", "family_document"),
        ("99_Misc", "misc_document"),
    ],
)
def test_canonical_and_legacy_reader_fallback(folder, expected):
    assert _infer_document_type("SYNTHETIC_SCAN.pdf", folder) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("SYNTHETIC_AKTA.pdf", "akta"),
        ("SYNTHETIC_SPT.pdf", "spt"),
        ("SYNTHETIC_PASSPORT.pdf", "passport"),
    ],
)
def test_combined_folder_keeps_filename_evidence(filename, expected):
    assert _infer_document_type(filename, "03_Company_and_Tax") == expected


def test_known_nested_category_remains_readable():
    assert _infer_document_type("SYNTHETIC_SCAN.pdf", "03_Company_and_Tax/AKTA") == "akta"
