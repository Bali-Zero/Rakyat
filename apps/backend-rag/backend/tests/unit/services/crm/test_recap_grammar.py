"""RECAP_SPEC.md "Grammars": every parser against the spec's type matrix (synthetic values only)."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.services.crm import recap_grammar as g

UTC = timezone.utc
NOT_A_VALUE = [True, False, 5.0, 0.5, {}, {"a": 1}, [], [1], None, object()]


# ── date ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 11, 5), date(2026, 11, 5)),
        ("2026-11-05", date(2026, 11, 5)),
        ("2024-02-29", date(2024, 2, 29)),
        # aware datetimes are reduced to their WITA (UTC+8) date, across midnight both ways
        (datetime(2026, 11, 5, 15, 59, tzinfo=UTC), date(2026, 11, 5)),
        (datetime(2026, 11, 5, 16, 0, tzinfo=UTC), date(2026, 11, 6)),
        (datetime(2026, 11, 5, 23, 59, tzinfo=ZoneInfo("Asia/Makassar")), date(2026, 11, 5)),
        (datetime(2026, 11, 5, 0, 1, tzinfo=timezone(timedelta(hours=-5))), date(2026, 11, 5)),
    ],
)
def test_date_accepts_exactly_three_forms(value, expected):
    assert g.parse_date(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 11, 5, 12, 0),  # naive
        datetime(2026, 11, 5),  # naive, midnight
        "2026-02-30",
        "2025-02-29",
        "2026-13-01",
        "0000-01-01",
        "2026-11-05T00:00:00",
        "2026-11-05 ",
        " 2026-11-05",
        "2026-11-05\n",
        "20261105",
        "2026-1-5",
        "2026/11/05",
        "05-11-2026",
        "２０２６-１１-０５",  # full-width digits
        "٢٠٢٦-١١-٠٥",  # Arabic-Indic digits
        "",
        *NOT_A_VALUE,
    ],
    ids=lambda v: repr(v)[:24],
)
def test_date_rejects_everything_else(value):
    assert g.parse_date(value) is None


# ── non-negative integer ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (7, 7),
        (10_000_000_000, 10_000_000_000),
        ("0", 0),
        ("1000", 1000),
        ("999", 999),
        ("1.000", 1000),
        ("12.345.678", 12_345_678),
        ("1,000,000", 1_000_000),
        ("999,999", 999_999),
        ("10,000,000,000", 10_000_000_000),
    ],
)
def test_int_accepts(value, expected):
    assert g.parse_nonneg_int(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        -1,
        "-5",
        "−5",  # U+2212 minus
        "+5",
        "(5)",
        "-0",
        "−0",
        "5.0",
        "5,5",
        "0.5",
        "1e3",
        "01",
        "00",
        "007",
        "1.0000",
        "1.00",
        "1000.000",
        "1,00,000",  # Indian grouping
        "1.000,000",  # mixed separators
        "1,000.000",
        "1.000.00",
        ".000",
        "1.",
        "1,",
        "1 000",
        " 1000",
        "1000 ",
        "1000\n",
        "1 000 000",
        "IDR 1000",
        "Rp1000",
        "1000 IDR",
        "１２３",  # full-width
        "٣",  # Arabic-Indic
        "①",
        "",
        " ",
        *NOT_A_VALUE,
    ],
    ids=lambda v: repr(v)[:24],
)
def test_int_rejects(value):
    assert g.parse_nonneg_int(value) is None


# ── KBLI code ───────────────────────────────────────────────────────────────


def test_kbli_accepts_exactly_five_ascii_digits():
    assert g.parse_kbli_code("62010") == "62010"
    assert g.parse_kbli_code("00000") == "00000"


@pytest.mark.parametrize(
    "value",
    [
        "6201",
        "620100",
        " 62010",
        "62010 ",
        "62010\n",
        "62 010",
        "6201A",
        "６２０１０",
        62010,
        "",
        *NOT_A_VALUE,
    ],
    ids=lambda v: repr(v)[:24],
)
def test_kbli_rejects(value):
    assert g.parse_kbli_code(value) is None


# ── code list ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "container",
    [None, {}, {"kbli_codes": None}, {"kbli_codes": []}, {"kbli_codes": "[]"}, {"other": 1}],
)
def test_code_list_empty(container):
    assert g.parse_code_list(container, "kbli_codes") == (g.CODE_LIST_EMPTY, [])


@pytest.mark.parametrize(
    "value",
    [["62010", "70209"], '["62010", "70209"]', ["not a code"]],
)
def test_code_list_accepted(value):
    state, items = g.parse_code_list({"kbli_codes": value}, "kbli_codes")
    assert state == g.CODE_LIST_ACCEPTED and len(items) > 0
    assert all(isinstance(i, str) for i in items)


@pytest.mark.parametrize(
    "container",
    [
        {"kbli_codes": {"a": "62010"}},
        {"kbli_codes": [["62010"]]},
        {"kbli_codes": ["62010", 70209]},
        {"kbli_codes": [None]},
        {"kbli_codes": "62010"},
        {"kbli_codes": "[62010"},
        {"kbli_codes": ""},
        {"kbli_codes": '{"a": "62010"}'},
        {"kbli_codes": '[["62010"]]'},
        {"kbli_codes": "[62010]"},
        {"kbli_codes": 62010},
        {"kbli_codes": True},
        {"kbli_codes": 0},
        {"kbli_codes": 5.0},
        ["62010"],  # custom_fields itself is not an object
        "kbli_codes",
    ],
    ids=lambda v: repr(v)[:30],
)
def test_code_list_malformed(container):
    assert g.parse_code_list(container, "kbli_codes") == (g.CODE_LIST_MALFORMED, [])


def test_code_list_json_string_decodes_only_once():
    twice = '"[\\"62010\\"]"'
    assert g.parse_code_list({"k": twice}, "k")[0] == g.CODE_LIST_MALFORMED


# ── LKPM period ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("year", "quarter", "expected"),
    [
        (2026, "Q1", (2026, 1)),
        (2026, "Q4", (2026, 4)),
        (2000, "Q2", (2000, 2)),
        (2100, "Q3", (2100, 3)),
        (2026, 1, (2026, 1)),
        (2026, 4, (2026, 4)),
    ],
)
def test_lkpm_period_accepts(year, quarter, expected):
    assert g.parse_lkpm_period(year, quarter) == expected


@pytest.mark.parametrize(
    "quarter",
    [
        "Q5",
        "Q0",
        "q1",
        "Q1 ",
        " Q1",
        "Q1\n",
        "Q",
        "1",
        "Q１",
        0,
        5,
        -1,
        1.0,
        True,
        None,
        [],
        {},
        "",
    ],
)
def test_lkpm_quarter_rejects(quarter):
    assert g.parse_lkpm_period(2026, quarter) is None


@pytest.mark.parametrize("year", [1999, 2101, "2026", 2026.0, True, False, None, -2026, [2026], {}])
def test_lkpm_year_rejects(year):
    assert g.parse_lkpm_period(year, "Q1") is None


# ── closed-set matching ─────────────────────────────────────────────────────


def test_closed_sets_trim_and_lower_case_but_never_match_substrings():
    assert g.classify("  On_Process ", g.PRACTICE_STATUSES) == g.OPEN
    assert g.classify("COMPLETED", g.PRACTICE_STATUSES) == g.CLOSED
    assert g.classify("on_process_late", g.PRACTICE_STATUSES) == g.UNKNOWN
    assert g.classify("not a shareholder", g.LINK_ROLES) == g.UNKNOWN
    assert g.classify("Pemegang Saham", g.LINK_ROLES) == g.SHAREHOLDER
    assert g.classify("owner", g.LINK_ROLES) == g.UNKNOWN
    for v in (None, "", "   ", 5, True, ["kitas"]):
        assert g.classify(v, g.PRACTICE_CATEGORIES) == g.UNKNOWN


def test_every_closed_set_member_is_recognised_and_sets_are_disjoint():
    for sets in (g.PRACTICE_CATEGORIES, g.PRACTICE_STATUSES, g.LINK_ROLES):
        seen: set[str] = set()
        for name, members in sets.items():
            for member in members:
                assert g.classify(member, sets) == name
                assert g.classify(member.upper(), sets) == name
            assert not (seen & members)
            seen |= members


def test_legal_form_is_stated_as_recorded_trimmed_and_only_from_the_set():
    assert g.legal_form("  PT PMA ") == "PT PMA"
    assert g.legal_form("pt pma") == "pt pma"
    for bad in ("Company", "PT PMA Holding", "PT  PMA", "", None, 5, "PT PMA;"):
        assert g.legal_form(bad) is None
    for member in g.LEGAL_FORMS:
        assert g.legal_form(member) == member


def test_lkpm_status_is_label_only():
    assert g.lkpm_status(" Approved ") == "approved"
    for bad in ("final", "", None, 3, "approved!"):
        assert g.lkpm_status(bad) is None


def test_decode_jsonb():
    assert g.decode_jsonb(None) == (True, None)
    assert g.decode_jsonb({"a": 1}) == (True, {"a": 1})
    assert g.decode_jsonb('{"a": 1}') == (True, {"a": 1})
    assert g.decode_jsonb("{not json") == (False, None)
    assert g.decode_jsonb([1]) == (True, [1])
