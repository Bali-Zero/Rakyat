"""The ready pack escapes every stored text it writes into its HTML.

The KBLI list comes from the client's LKPM configuration and a validation message quotes a
KBLI code: both are stored text, like the company name the builder already escapes.
"""

from __future__ import annotations

from backend.app.models.lkpm import (
    EmploymentData,
    InvestmentRealization,
    LKPMReadyPack,
    LKPMValidationResult,
    ValidationAlert,
    ValidationSeverity,
)
from backend.services.compliance.lkpm_ready_pack import generate_ready_pack_html

_MARKUP = '<img src=x onerror="alert(1)">'
_ESCAPED = "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"


def _pack(**changes: object) -> LKPMReadyPack:
    fields: dict[str, object] = {
        "draft_id": 900001,
        "client_id": 900001,
        "company_name": "PT Contoh Satu",
        "quarter": "Q1",
        "year": 2026,
        "kbli_codes": ["62011", "62012"],
        "realized": InvestmentRealization(equipment_domestic=100_000_000),
        "realized_total": 100_000_000,
        "cumulative": InvestmentRealization(equipment_domestic=300_000_000),
        "cumulative_total": 300_000_000,
        "plan": InvestmentRealization(equipment_domestic=1_000_000_000),
        "realization_percentage": 30.0,
        "employment": EmploymentData(tki=10, tka=2),
    }
    fields.update(changes)
    return LKPMReadyPack(**fields)


def test_markup_in_a_kbli_code_is_written_as_text() -> None:
    html = generate_ready_pack_html(_pack(kbli_codes=["62011", _MARKUP]))

    assert _MARKUP not in html
    assert f"62011, {_ESCAPED}" in html


def test_markup_in_a_validation_message_is_written_as_text() -> None:
    summary = LKPMValidationResult(
        is_valid=False,
        alerts=[
            ValidationAlert(
                field="kbli",
                severity=ValidationSeverity.RED,
                message=f"KBLI {_MARKUP} has no plan",
            )
        ],
        red_count=1,
    )

    html = generate_ready_pack_html(_pack(validation_summary=summary))

    assert _MARKUP not in html
    assert f"KBLI {_ESCAPED} has no plan" in html


def test_ordinary_codes_and_messages_read_as_before() -> None:
    summary = LKPMValidationResult(
        is_valid=True,
        alerts=[
            ValidationAlert(
                field="total", severity=ValidationSeverity.GREEN, message="Totals agree"
            )
        ],
        green_count=1,
    )

    html = generate_ready_pack_html(_pack(validation_summary=summary))

    assert "<strong>KBLI:</strong> 62011, 62012</p>" in html
    assert "&#10004; Totals agree</div>" in html


def test_a_pack_with_no_code_shows_the_dash() -> None:
    assert "<strong>KBLI:</strong> —</p>" in generate_ready_pack_html(_pack(kbli_codes=[]))
