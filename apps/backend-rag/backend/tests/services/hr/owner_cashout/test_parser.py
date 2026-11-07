"""Tests for owner cashout sheet parser."""

import json
from pathlib import Path

from backend.services.hr.owner_cashout.parser import (
    parse_bs_tab,
    parse_bz_tab,
    parse_idr,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[list[str]]:
    data = json.loads((FIXTURES / name).read_text())
    return data["rows"]


class TestParseIdr:
    def test_standard_format(self):
        assert parse_idr("Rp1,000,000") == 1_000_000

    def test_large_amount(self):
        assert parse_idr("Rp10,500,000") == 10_500_000

    def test_empty_string(self):
        assert parse_idr("") == 0

    def test_none(self):
        assert parse_idr(None) == 0

    def test_whitespace_only(self):
        assert parse_idr("   ") == 0

    def test_dash_placeholder(self):
        assert parse_idr("-") == 0
        assert parse_idr("—") == 0

    def test_plain_number_no_prefix(self):
        assert parse_idr("500000") == 500_000

    def test_with_dot_separator(self):
        assert parse_idr("Rp1.000.000") == 1_000_000

    def test_invalid_returns_zero(self):
        assert parse_idr("not a number") == 0


class TestParseBzTab:
    def test_skips_title_and_header(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        # Title (row 1), header (row 2) must be skipped
        assert all(r.client_name not in ("NEW CASHOUT 22 AUGUST 2025", "NAME") for r in result)

    def test_skips_empty_rows(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        assert all(r.client_name for r in result)

    def test_extracts_all_clients(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        assert len(result) == 6
        names = {r.client_name for r in result}
        assert "TIXAMUFI REMOWA" in names
        assert "RUK HIBET CASTEL" in names
        assert "JUTIM WOMUMAW KOSTRO" in names
        assert "RUTIHAF JEKA XIDEFUVO" in names

    def test_parses_amounts_for_first_row(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        tixamufi = next(r for r in result if r.client_name == "TIXAMUFI REMOWA")
        assert tixamufi.process == "BRIDGING VISA"
        assert tixamufi.pnbp_idr == 1_000_000
        assert tixamufi.urgent_idr == 0
        assert tixamufi.rptka_imta_idr == 0
        assert tixamufi.total_income_idr == 5_000_000
        assert tixamufi.margin_bs_idr == 3_000_000
        assert tixamufi.margin_bz_idr == 1_000_000
        assert tixamufi.final_price_idr == 0  # BZ doesn't populate this
        assert tixamufi.entity == "BZ"

    def test_extracts_note_when_present(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        lize = next(r for r in result if r.client_name == "LIZE RALPH TAJOBIVAW")
        assert lize.note == "DISCOUNT 200K"

    def test_urgent_amount_parsed(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        urgent = next(r for r in result if "RUTIHAF" in r.client_name)
        assert urgent.urgent_idr == 1_000_000

    def test_row_index_preserves_sheet_position(self):
        rows = load_fixture("bz_22_aug_sample.json")
        result = parse_bz_tab(rows)
        tixamufi = next(r for r in result if r.client_name == "TIXAMUFI REMOWA")
        assert tixamufi.row_index == 3  # 1-indexed, row 3 in sheet

    def test_empty_rows_list(self):
        assert parse_bz_tab([]) == []

    def test_only_header_no_data(self):
        rows = [
            ["NEW CASHOUT"],
            ["NAME", "PROCESS"],
        ]
        assert parse_bz_tab(rows) == []


class TestParseBsTab:
    def test_extracts_clients(self):
        rows = load_fixture("bs_22_aug_sample.json")
        result = parse_bs_tab(rows)
        assert len(result) == 3
        names = [r.client_name for r in result]
        assert "TIXAMUFI REMOWA" in names
        assert "RUK HIBET CASTEL" in names

    def test_parses_bs_schema_amounts(self):
        rows = load_fixture("bs_22_aug_sample.json")
        result = parse_bs_tab(rows)
        tixamufi = next(r for r in result if r.client_name == "TIXAMUFI REMOWA")
        assert tixamufi.entity == "BS"
        assert tixamufi.process == "BRIDGING VISA"
        assert tixamufi.pnbp_idr == 1_000_000
        assert tixamufi.margin_bs_idr == 3_000_000
        assert tixamufi.final_price_idr == 4_000_000
        assert tixamufi.total_income_idr == 0  # BS doesn't populate this
        assert tixamufi.margin_bz_idr == 0  # BS doesn't populate this
        assert tixamufi.note is None

    def test_skips_empty_rows(self):
        rows = load_fixture("bs_22_aug_sample.json")
        result = parse_bs_tab(rows)
        assert all(r.client_name for r in result)

    def test_row_index_preserved(self):
        rows = load_fixture("bs_22_aug_sample.json")
        result = parse_bs_tab(rows)
        tixamufi = next(r for r in result if r.client_name == "TIXAMUFI REMOWA")
        assert tixamufi.row_index == 3

    def test_empty_input(self):
        assert parse_bs_tab([]) == []
