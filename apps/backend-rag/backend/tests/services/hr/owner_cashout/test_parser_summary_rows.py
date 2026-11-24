"""Summary rows (TOTAL / FEE footers) of a cash-out tab are not clients.

Every fixture here is invented: made-up client names and round made-up amounts.
"""

import pytest

from backend.services.hr.owner_cashout.parser import parse_bs_tab, parse_bz_tab

TITLE = ["NEW CASHOUT 01 JANUARY 2030"]
BZ_HEADER = [
    "NAME",
    "PROCESS",
    "PNBP",
    "URGENT",
    "RPTKA/IMTA",
    "TOTAL INCOME",
    "MARGIN BS",
    "MARGIN BZ",
    "NOTE",
]
BS_HEADER = ["NAME", "PROCESS", "PNBP", "URGENT", "RPTKA/IMTA", "MARGIN BS", "FINAL PRICE"]


def _bz_tab(*body: list[str]) -> list[list[str]]:
    return [TITLE, BZ_HEADER, *body]


def _bs_tab(*body: list[str]) -> list[list[str]]:
    return [TITLE, BS_HEADER, *body]


BZ_CLIENTS = [
    [
        "Contoh Satu",
        "KITAS",
        "Rp1,000,000",
        "",
        "",
        "Rp5,000,000",
        "Rp1,000,000",
        "Rp2,000,000",
        "",
    ],
    [
        "Contoh Dua",
        "PT PMA",
        "Rp2,000,000",
        "",
        "",
        "Rp8,000,000",
        "Rp1,500,000",
        "Rp3,000,000",
        "",
    ],
]
BS_CLIENTS = [
    ["Contoh Satu", "KITAS", "Rp1,000,000", "", "", "Rp1,000,000", "Rp6,000,000"],
    ["Contoh Dua", "PT PMA", "Rp2,000,000", "", "", "Rp1,500,000", "Rp9,000,000"],
]


class TestSummaryRowsAreNotClients:
    def test_bz_total_and_fee_footers_are_skipped(self):
        rows = _bz_tab(
            *BZ_CLIENTS,
            [""],
            ["TOTAL", "", "Rp3,000,000", "", "", "Rp13,000,000", "Rp2,500,000", "Rp5,000,000", ""],
            ["FEE BZ", "", "", "", "", "", "", "Rp5,000,000", ""],
        )
        result = parse_bz_tab(rows)

        assert [r.client_name for r in result] == ["Contoh Satu", "Contoh Dua"]
        assert sum(r.margin_bz_idr for r in result) == 5_000_000
        assert sum(r.margin_bs_idr for r in result) == 2_500_000
        assert sum(r.total_income_idr for r in result) == 13_000_000

    def test_bs_total_and_fee_footers_are_skipped(self):
        rows = _bs_tab(
            *BS_CLIENTS,
            ["GRAND TOTAL", "", "Rp3,000,000", "", "", "Rp2,500,000", "Rp15,000,000"],
            ["FEE BS", "", "", "", "", "Rp2,500,000", ""],
        )
        result = parse_bs_tab(rows)

        assert [r.client_name for r in result] == ["Contoh Satu", "Contoh Dua"]
        assert sum(r.margin_bs_idr for r in result) == 2_500_000
        assert sum(r.final_price_idr for r in result) == 15_000_000

    @pytest.mark.parametrize(
        "label",
        [
            "TOTAL",
            "total",
            "  Total  ",
            "GRAND TOTAL",
            "Grand  Total",
            "FEE BZ",
            "fee bs",
            "TOTAL:",
            "Total :",
            "FINAL TOTAL",
            "Final  Total",
            "TOTAL FINAL FEE",
            "total final fee:",
            "SUBTOTAL",
            "Total Fee",
        ],
    )
    def test_footer_label_made_only_of_total_and_fee_words_is_skipped(self, label):
        bz = parse_bz_tab(_bz_tab(BZ_CLIENTS[0], [label, "", "Rp1,000,000"]))
        bs = parse_bs_tab(_bs_tab(BS_CLIENTS[0], [label, "", "Rp1,000,000"]))

        assert [r.client_name for r in bz] == ["Contoh Satu"]
        assert [r.client_name for r in bs] == ["Contoh Satu"]

    def test_bz_final_footers_are_skipped_and_totals_equal_the_client_rows(self):
        rows = _bz_tab(
            *BZ_CLIENTS,
            [
                "FINAL TOTAL",
                "",
                "Rp3,000,000",
                "",
                "",
                "Rp13,000,000",
                "Rp2,500,000",
                "Rp5,000,000",
            ],
            ["TOTAL FINAL FEE", "", "", "", "", "", "", "Rp5,000,000", ""],
        )
        result = parse_bz_tab(rows)

        assert [r.client_name for r in result] == ["Contoh Satu", "Contoh Dua"]
        assert sum(r.pnbp_idr for r in result) == 3_000_000
        assert sum(r.margin_bz_idr for r in result) == 5_000_000
        assert sum(r.total_income_idr for r in result) == 13_000_000

    def test_bs_final_footers_are_skipped_and_totals_equal_the_client_rows(self):
        rows = _bs_tab(
            *BS_CLIENTS,
            ["FINAL TOTAL", "", "Rp3,000,000", "", "", "Rp2,500,000", "Rp15,000,000"],
            ["TOTAL FINAL FEE", "", "", "", "", "Rp2,500,000", ""],
        )
        result = parse_bs_tab(rows)

        assert [r.client_name for r in result] == ["Contoh Satu", "Contoh Dua"]
        assert sum(r.margin_bs_idr for r in result) == 2_500_000
        assert sum(r.final_price_idr for r in result) == 15_000_000

    def test_row_index_of_real_clients_is_unchanged_by_a_footer(self):
        rows = _bz_tab(*BZ_CLIENTS, ["TOTAL", "", "Rp3,000,000"])
        result = parse_bz_tab(rows)

        assert [r.row_index for r in result] == [3, 4]


class TestNamesThatOnlyContainTheWordAreClients:
    @pytest.mark.parametrize(
        "name",
        [
            "Totalindo Contoh",
            "Feebri Contoh",
            "Contoh Total",
            "Fee Contoh",
            "Subtotal Contoh",
            "Final Contoh",
            "Grand Contoh",
            "Total Final Contoh",
            "BZ Contoh",
        ],
    )
    def test_bz_name_containing_total_or_fee_is_still_a_client(self, name):
        row = [
            name,
            "KITAS",
            "Rp1,000,000",
            "",
            "",
            "Rp5,000,000",
            "Rp1,000,000",
            "Rp2,000,000",
            "",
        ]
        result = parse_bz_tab(_bz_tab(row))

        assert [r.client_name for r in result] == [name]
        assert result[0].margin_bz_idr == 2_000_000

    @pytest.mark.parametrize(
        "name",
        ["Totalindo Contoh", "Feebri Contoh", "Final Contoh", "Grand Contoh", "Fee Contoh"],
    )
    def test_bs_name_containing_total_or_fee_is_still_a_client(self, name):
        row = [name, "KITAS", "Rp1,000,000", "", "", "Rp1,000,000", "Rp6,000,000"]
        result = parse_bs_tab(_bs_tab(row))

        assert [r.client_name for r in result] == [name]
        assert result[0].final_price_idr == 6_000_000

    @pytest.mark.parametrize("name", ["Final", "Grand", "BZ", "BS"])
    def test_a_vocabulary_word_alone_without_total_or_fee_is_still_a_client(self, name):
        bz = parse_bz_tab(_bz_tab([name, "KITAS", "Rp1,000,000"]))
        bs = parse_bs_tab(_bs_tab([name, "KITAS", "Rp1,000,000"]))

        assert [r.client_name for r in bz] == [name]
        assert [r.client_name for r in bs] == [name]

    def test_ordinary_rows_are_unchanged(self):
        result = parse_bz_tab(_bz_tab(*BZ_CLIENTS))

        assert [r.client_name for r in result] == ["Contoh Satu", "Contoh Dua"]
        assert [r.row_index for r in result] == [3, 4]
        assert result[0].process == "KITAS"
        assert result[0].pnbp_idr == 1_000_000
        assert result[1].margin_bs_idr == 1_500_000
