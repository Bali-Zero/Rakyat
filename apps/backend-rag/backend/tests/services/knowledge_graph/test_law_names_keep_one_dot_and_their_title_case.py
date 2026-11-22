"""Regression tests for law entity-name normalization."""

import pytest

from backend.services.knowledge_graph.advanced_quality import normalize_entity_name


@pytest.mark.parametrize(
    ("name", "entity_type", "expected"),
    [
        (
            "PP No. 5 Tahun 2021",
            "peraturan_pemerintah",
            "PP No. 5 Tahun 2021",
        ),
        (
            "UU No. 6 Tahun 2023 tentang Cipta Kerja",
            "undang_undang",
            "UU No. 6 Tahun 2023 tentang Cipta Kerja",
        ),
        ("UU 6/2023", "undang_undang", "UU No. 6 Tahun 2023"),
    ],
)
def test_law_names_have_one_dot_and_preserve_title_case(
    name: str, entity_type: str, expected: str
) -> None:
    assert normalize_entity_name(name, entity_type) == expected


@pytest.mark.parametrize(
    ("name", "entity_type"),
    [
        ("PP No. 5 Tahun 2021", "peraturan_pemerintah"),
        ("UU No. 6 Tahun 2023 tentang Cipta Kerja", "undang_undang"),
        ("UU 6/2023", "undang_undang"),
    ],
)
def test_law_name_normalization_is_idempotent(name: str, entity_type: str) -> None:
    normalized = normalize_entity_name(name, entity_type)

    assert normalize_entity_name(normalized, entity_type) == normalized


@pytest.mark.parametrize(
    ("name", "entity_type", "expected"),
    [
        ("  Acme PT  ", "company", "Acme PT"),
        ("pasal 7 ayat (2)", "pasal", "Pasal 7 Ayat 2"),
    ],
)
def test_non_law_entity_types_keep_existing_behavior(
    name: str, entity_type: str, expected: str
) -> None:
    assert normalize_entity_name(name, entity_type) == expected
