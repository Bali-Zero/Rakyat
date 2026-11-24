"""A nationality written as text is read by exact names, or reported as not read.

On main a part of the answer that was not read was dropped in silence, and both
nationality gates (Indonesian citizen, Calling Visa country) stayed open. The reader
now takes a name only when it is a row of its table, a code only when the module knows
it, and reports everything else as unread. Nothing is derived from a text.

Two kinds of test, kept apart on purpose:
- FIXED expectations, written by hand: a wrong row of the table fails them;
- MECHANICAL sweeps over the whole table: they catch a mechanism that reads a text as
  another text, whatever the rows are.
"""

from __future__ import annotations

import itertools
import re
import string

import pytest

from backend.services.visa_check.match_tree import (
    _ISO2_TO_ISO3,
    _NAME_OR_DEMONYM_TO_ISO3,
    BudgetBand,
    Purpose,
    classify_nationality,
    recommend_visa,
)

# ───────────────────────────── FIXED expectations ─────────────────────────────

CALLING_VISA_NAMES = [
    ("Afghanistan", "AFG"),
    ("afghan", "AFG"),
    ("AF", "AFG"),
    ("AFG", "AFG"),
    ("Islamic Republic of Afghanistan", "AFG"),
    ("Islamic Emirate of Afghanistan", "AFG"),
    ("Afganistan", "AFG"),
    ("Israel", "ISR"),
    ("Israeli", "ISR"),
    ("IL", "ISR"),
    ("State of Israel", "ISR"),
    ("North Korea", "PRK"),
    ("North Korean", "PRK"),
    ("KP", "PRK"),
    ("PRK", "PRK"),
    ("DPRK", "PRK"),
    ("DPR Korea", "PRK"),
    ("Korea DPR", "PRK"),
    ("Korea Utara", "PRK"),
    ("Korea, North", "PRK"),
    ("Democratic People's Republic of Korea", "PRK"),
    ("Democratic People’s Republic of Korea", "PRK"),
    ("Democratic Peoples Republic of Korea", "PRK"),
    ("the Democratic People's Republic of Korea", "PRK"),
    ("Korea, Democratic People's Republic of", "PRK"),
    ("Liberia", "LBR"),
    ("Liberian", "LBR"),
    ("LR", "LBR"),
    ("Republic of Liberia", "LBR"),
    ("Nigeria", "NGA"),
    ("nigerian", "NGA"),
    ("NG", "NGA"),
    ("Nigéria", "NGA"),
    ("Federal Republic of Nigeria", "NGA"),
    ("the Federal Republic of Nigeria", "NGA"),
    ("Somalia", "SOM"),
    ("Somali", "SOM"),
    ("SO", "SOM"),
    ("Federal Republic of Somalia", "SOM"),
]

CALLING_VISA_BESIDE_ANOTHER = [
    ("Nigeria and USA", "NGA"),
    ("USA & Somalia", "SOM"),
    ("Australia; Liberia", "LBR"),
    ("Nigeria and Republic of Italy", "NGA"),
    ("Republic of Italy and Nigeria", "NGA"),
    ("Nigeria and the Federal Republic of Germany", "NGA"),
    ("Somalia & Kingdom of Spain", "SOM"),
    ("Korea, North / USA", "PRK"),
    ("Korea, North and USA", "PRK"),
    ("NG,,US", "NGA"),
    ("Nigeria / Xyz", "NGA"),
]

INDONESIAN_NAMES = [
    "Indonesia",
    "indonesian",
    "ID",
    "IDN",
    "WNI",
    "INA",
    "Republic of Indonesia",
    "Republik Indonesia",
    "Negara Kesatuan Republik Indonesia",
    "Warga Negara Indonesia",
    "the Indonesia",
    "indonésia",
    "Indonesia and Australia",
    "Indonesia and Republic of France",
    "Republic of France and Indonesia",
    "Indonesia / Xyz",
]

# The rows this change adds, one by one, each against the code written here.
REVIEWED_ROWS = [
    ("REPUBLIC OF INDONESIA", "IDN"),
    ("REPUBLIK INDONESIA", "IDN"),
    ("NEGARA KESATUAN REPUBLIK INDONESIA", "IDN"),
    ("INA", "IDN"),
    ("ISLAMIC REPUBLIC OF AFGHANISTAN", "AFG"),
    ("ISLAMIC EMIRATE OF AFGHANISTAN", "AFG"),
    ("AFGANISTAN", "AFG"),
    ("STATE OF ISRAEL", "ISR"),
    ("REPUBLIC OF LIBERIA", "LBR"),
    ("FEDERAL REPUBLIC OF NIGERIA", "NGA"),
    ("FEDERAL REPUBLIC OF SOMALIA", "SOM"),
    ("DPR KOREA", "PRK"),
    ("KOREA DPR", "PRK"),
    ("KOREA UTARA", "PRK"),
    ("ARGENTINE REPUBLIC", "ARG"),
    ("REPUBLIC OF ARGENTINA", "ARG"),
    ("COMMONWEALTH OF AUSTRALIA", "AUS"),
    ("REPUBLIC OF AUSTRIA", "AUT"),
    ("KINGDOM OF BELGIUM", "BEL"),
    ("PEOPLE'S REPUBLIC OF BANGLADESH", "BGD"),
    ("PEOPLES REPUBLIC OF BANGLADESH", "BGD"),
    ("REPUBLIC OF BELARUS", "BLR"),
    ("FEDERATIVE REPUBLIC OF BRAZIL", "BRA"),
    ("BRUNEI DARUSSALAM", "BRN"),
    ("SWISS CONFEDERATION", "CHE"),
    ("REPUBLIC OF CHILE", "CHL"),
    ("REPUBLIC OF CAMEROON", "CMR"),
    ("REPUBLIC OF COLOMBIA", "COL"),
    ("FEDERAL REPUBLIC OF GERMANY", "DEU"),
    ("KINGDOM OF DENMARK", "DNK"),
    ("KINGDOM OF SPAIN", "ESP"),
    ("REPUBLIC OF FINLAND", "FIN"),
    ("FRENCH REPUBLIC", "FRA"),
    ("REPUBLIC OF FRANCE", "FRA"),
    ("REPUBLIC OF GUINEA", "GIN"),
    ("HONG KONG SPECIAL ADMINISTRATIVE REGION", "HKG"),
    ("HONG KONG SAR", "HKG"),
    ("REPUBLIC OF INDIA", "IND"),
    ("REPUBLIC OF IRELAND", "IRL"),
    ("ISLAMIC REPUBLIC OF IRAN", "IRN"),
    ("REPUBLIC OF IRAQ", "IRQ"),
    ("ITALIAN REPUBLIC", "ITA"),
    ("REPUBLIC OF ITALY", "ITA"),
    ("REPUBLIC OF KAZAKHSTAN", "KAZ"),
    ("KINGDOM OF CAMBODIA", "KHM"),
    ("LAO PEOPLE'S DEMOCRATIC REPUBLIC", "LAO"),
    ("LAO PEOPLES DEMOCRATIC REPUBLIC", "LAO"),
    ("LAO PDR", "LAO"),
    ("DEMOCRATIC SOCIALIST REPUBLIC OF SRI LANKA", "LKA"),
    ("MACAO SPECIAL ADMINISTRATIVE REGION", "MAC"),
    ("MACAU SPECIAL ADMINISTRATIVE REGION", "MAC"),
    ("MACAO SAR", "MAC"),
    ("MACAU SAR", "MAC"),
    ("UNITED MEXICAN STATES", "MEX"),
    ("REPUBLIC OF THE UNION OF MYANMAR", "MMR"),
    ("KINGDOM OF THE NETHERLANDS", "NLD"),
    ("KINGDOM OF NORWAY", "NOR"),
    ("FEDERAL DEMOCRATIC REPUBLIC OF NEPAL", "NPL"),
    ("ISLAMIC REPUBLIC OF PAKISTAN", "PAK"),
    ("REPUBLIC OF PERU", "PER"),
    ("REPUBLIC OF THE PHILIPPINES", "PHL"),
    ("REPUBLIC OF POLAND", "POL"),
    ("PORTUGUESE REPUBLIC", "PRT"),
    ("REPUBLIC OF PORTUGAL", "PRT"),
    ("STATE OF QATAR", "QAT"),
    ("RUSSIAN FEDERATION", "RUS"),
    ("KINGDOM OF SAUDI ARABIA", "SAU"),
    ("REPUBLIC OF SINGAPORE", "SGP"),
    ("KINGDOM OF SWEDEN", "SWE"),
    ("KINGDOM OF THAILAND", "THA"),
    ("DEMOCRATIC REPUBLIC OF TIMOR-LESTE", "TLS"),
    ("DEMOCRATIC REPUBLIC OF TIMOR LESTE", "TLS"),
    ("REPUBLIC OF TURKIYE", "TUR"),
    ("REPUBLIC OF TURKEY", "TUR"),
    ("TURKIYE", "TUR"),
    ("SOCIALIST REPUBLIC OF VIET NAM", "VNM"),
    ("SOCIALIST REPUBLIC OF VIETNAM", "VNM"),
    ("VIET NAM", "VNM"),
    ("REPUBLIC OF SOUTH AFRICA", "ZAF"),
]

# Names whose country depends on the words in front, and the short aliases.
QUALIFIED_NAMES = [
    ("Republic of Korea", "KOR"),
    ("Korea, Republic of", "KOR"),
    ("South Korea", "KOR"),
    ("South Korean", "KOR"),
    ("Korea, South", "KOR"),
    ("People's Republic of China", "CHN"),
    ("Peoples Republic of China", "CHN"),
    ("the People’s Republic of China", "CHN"),
    ("China, People's Republic of", "CHN"),
    ("PRC", "CHN"),
    ("China", "CHN"),
    ("Chinese", "CHN"),
    ("Republic of China", "TWN"),
    ("China, Republic of", "TWN"),
    ("ROC", "TWN"),
    ("Taiwan", "TWN"),
    ("Republic of the Congo", "COG"),
    ("Republic of Congo", "COG"),
    ("Democratic Republic of the Congo", "COD"),
    ("Democratic Republic of Congo", "COD"),
    ("Congo, Democratic Republic of the", "COD"),
    ("DR Congo", "COD"),
    ("DRC", "COD"),
    ("United Kingdom of Great Britain and Northern Ireland", "GBR"),
    ("Republic of Ireland", "IRL"),
    ("Islamic Republic of Iran", "IRN"),
    ("Türkiye", "TUR"),
    ("Republic of Türkiye", "TUR"),
    ("Netherlands, The", "NLD"),
    ("UAE", "ARE"),
]

NOT_READ = [
    # round 3: a formal form that is not a row is not read, whatever word it ends with
    "Democratic People's Republic of Korean",
    "Republic of Chinese",
    "Federal Republic of China",
    "Democratic Republic of China",
    "People's Republic of Korea",
    "Democratic Republic of Korea",
    "Kingdom of Korea",
    "Federal Republic of the Congo",
    # which Korea is not said
    "Korean",
    "Korea",
    # one place written with a comma, or two names with a comma
    "Guinea, French",
    "Hong Kong, China",
    "Italy, USA",
    "Indonesia, Xyz",
    "Nigeria, Italian",
    "Korea, Republic of, USA",
    "ID,XYZ",
    "Nigeria,,USA",
    # names that hold "and" and are not rows
    "Bosnia and Herzegovina",
    "Trinidad and Tobago",
    "Antigua and Barbuda",
    "Saint Kitts and Nevis",
    "Sao Tome and Principe",
    # a longer name is never read as the shorter one inside it
    "Guinea-Bissau",
    "Equatorial Guinea",
    "Papua New Guinea",
    "Niger",
    "Dominica",
    "Dominican Republic",
    "South Sudan",
    "American Samoa",
    "Virgin Islands",
    "British Virgin Islands",
    "Korea (North)",
    "Chinese-American",
    # three letters the module has no row and no code for
    "WNA",
    "XYZ",
    "KSA",
    "ROK",
    "GER",
    "DPR",
    # a letter of another script, a zero-width character
    "Itаly",
    "Ita​ly",
    "Narnia",
]


@pytest.mark.parametrize(("text", "code"), CALLING_VISA_NAMES)
def test_a_calling_visa_country_is_read_under_each_of_its_names(text: str, code: str) -> None:
    read = classify_nationality(text)

    assert read.iso3_codes == (code,)
    assert read.has_unread_part is False
    assert read.is_calling_visa is True
    assert read.calling_visa_code == code
    assert read.is_voa_eligible is False


@pytest.mark.parametrize(("text", "code"), CALLING_VISA_BESIDE_ANOTHER)
def test_a_calling_visa_country_beside_another_answer_keeps_its_gate(text: str, code: str) -> None:
    read = classify_nationality(text)

    assert read.is_calling_visa is True
    assert read.calling_visa_code == code
    assert read.is_voa_eligible is False


@pytest.mark.parametrize(("text", "code"), CALLING_VISA_NAMES + CALLING_VISA_BESIDE_ANOTHER[:7])
def test_the_match_refers_a_calling_visa_country_written_as_text(text: str, code: str) -> None:
    result = recommend_visa(
        nationality=text,
        purpose=Purpose.LONG_TOURISM,
        duration_months=1,
        budget_band=BudgetBand.OVER_500M,
    )

    assert result.referral_mode is True
    assert result.ranking == []
    assert "Calling Visa" in result.reason


@pytest.mark.parametrize("text", INDONESIAN_NAMES)
def test_an_indonesian_citizen_is_read_under_each_of_its_names(text: str) -> None:
    read = classify_nationality(text)

    assert read.is_indonesian_citizen is True
    assert read.is_calling_visa is False
    assert "IDN" in read.iso3_codes


@pytest.mark.parametrize(("name", "code"), REVIEWED_ROWS)
def test_each_reviewed_row_reads_as_the_code_written_here(name: str, code: str) -> None:
    for text in (name, name.lower(), f"the {name.title()}"):
        read = classify_nationality(text)

        assert read.iso3_codes == (code,), text
        assert read.has_unread_part is False, text


@pytest.mark.parametrize(("text", "code"), QUALIFIED_NAMES)
def test_a_name_that_depends_on_its_words_is_read_as_the_country_it_names(
    text: str, code: str
) -> None:
    read = classify_nationality(text)

    assert read.iso3_codes == (code,)
    assert read.has_unread_part is False


@pytest.mark.parametrize("text", NOT_READ)
def test_a_text_that_is_not_an_entry_reads_nothing_and_is_reported(text: str) -> None:
    read = classify_nationality(text)

    assert read.iso3_codes == ()
    assert read.has_unread_part is True
    assert read.is_voa_eligible is False
    assert read.is_bvk_eligible is False
    assert read.is_calling_visa is False
    assert read.is_indonesian_citizen is False


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("Korean / USA", ("USA",)),
        ("USA / Itаly", ("USA",)),
        ("Italy and Narnia", ("ITA",)),
        ("Italy / / xyz", ("ITA",)),
        ("xyz / / Italy", ("ITA",)),
        ("Italy and", ()),
        ("USA and United Kingdom of Great Britain and Northern Ireland", ("USA",)),
        ("Nigeria Republic of Italy", ()),
        ("Nigeria or Republic of Italy", ()),
        ("Guinea, French / Italy", ("ITA",)),
    ],
)
def test_the_part_that_is_read_is_kept_and_the_rest_is_reported(
    text: str, codes: tuple[str, ...]
) -> None:
    read = classify_nationality(text)

    assert read.iso3_codes == codes
    assert read.has_unread_part is True


@pytest.mark.parametrize(
    ("text", "codes"),
    [
        ("US", ("USA",)),
        ("usa", ("USA",)),
        ("ID,US", ("IDN", "USA")),
        ("IT, US", ("ITA", "USA")),
        ("UAE, UK", ("ARE", "GBR")),
        ("NG,,US", ("NGA", "USA")),
        ("id / us", ("IDN", "USA")),
        ("Italy / USA", ("ITA", "USA")),
        ("Italy and USA", ("ITA", "USA")),
        ("Italy; USA", ("ITA", "USA")),
        ("Italy & USA", ("ITA", "USA")),
        ("Italy + USA", ("ITA", "USA")),
        ("Hong Kong / China", ("HKG", "CHN")),
        ("Republic of China and USA", ("TWN", "USA")),
        ("USA and Republic of China", ("USA", "TWN")),
        ("Nigeria and Republic of Italy", ("NGA", "ITA")),
        ("USA / United Kingdom of Great Britain and Northern Ireland", ("USA", "GBR")),
        ("Italy /", ("ITA",)),
        ("/ Italy", ("ITA",)),
        ("United States of America", ("USA",)),
        ("The Netherlands", ("NLD",)),
        ("Italian", ("ITA",)),
        ("AND", ("AND",)),  # Andorra's code is not the word "and"
        ("IN", ("IND",)),
        ("India", ("IND",)),
        ("NE", ("NER",)),
        ("Guinea", ("GIN",)),
        # compatibility forms: fullwidth letters and joiners, a no-break space
        ("Ｉｔａｌｙ", ("ITA",)),
        ("Italy／USA", ("ITA", "USA")),
        ("Italy / USA", ("ITA", "USA")),
        ("South Korea", ("KOR",)),
    ],
)
def test_an_answer_made_of_entries_is_read_in_full(text: str, codes: tuple[str, ...]) -> None:
    read = classify_nationality(text)

    assert read.iso3_codes == codes
    assert read.has_unread_part is False


@pytest.mark.parametrize("text", ["", "   ", " / , ; ", ",,", "/"])
def test_a_text_with_nothing_in_it_reads_nothing(text: str) -> None:
    read = classify_nationality(text)

    assert read.iso3_codes == ()
    assert read.has_unread_part is False


def test_a_comma_between_two_names_invents_no_nationality_and_no_eligibility() -> None:
    """ "Guinea, French" is one place: France's Visa on Arrival is not handed to it."""
    assert classify_nationality("France").is_voa_eligible is True
    assert classify_nationality("Guinea").is_voa_eligible is False

    read = classify_nationality("Guinea, French")

    assert read.iso3_codes == ()
    assert read.is_voa_eligible is False


# ───────────────────────────── MECHANICAL sweeps ──────────────────────────────

ROWS = dict(_NAME_OR_DEMONYM_TO_ISO3)
FORMS_AROUND_A_NAME = [
    "Republic of {}",
    "Republic of the {}",
    "Democratic Republic of {}",
    "Democratic Republic of the {}",
    "Democratic People's Republic of {}",
    "Federal Republic of {}",
    "Islamic Republic of {}",
    "People's Republic of {}",
    "Kingdom of {}",
    "State of {}",
    "Commonwealth of {}",
    "{} Republic",
    "North {}",
    "South {}",
    "New {}",
    "United {}",
]
# Rows that hold, as whole words, a row of ANOTHER country. Each was reviewed by hand;
# a new row of this shape fails the test below until it is added here with its code.
ROWS_THAT_HOLD_ANOTHER_COUNTRY = {
    "DEMOCRATIC PEOPLE'S REPUBLIC OF KOREA": "PRK",
    "DEMOCRATIC PEOPLES REPUBLIC OF KOREA": "PRK",
    "DEMOCRATIC REPUBLIC OF CONGO": "COD",
    "DEMOCRATIC REPUBLIC OF THE CONGO": "COD",
    "PEOPLE'S REPUBLIC OF CHINA": "CHN",
    "PEOPLES REPUBLIC OF CHINA": "CHN",
    "REPUBLIC OF CHINA": "TWN",
    "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND": "GBR",
}


def test_nothing_is_derived_from_a_name_with_words_around_it() -> None:
    """Every row, in every form: the row of that exact text, or nothing. Never the
    country of the name inside."""
    for name in ROWS:
        for form in FORMS_AROUND_A_NAME:
            text = form.format(name)
            read = classify_nationality(text)
            row = ROWS.get(text.upper())
            if row is not None:
                assert read.iso3_codes == (row,), text
                assert read.has_unread_part is False, text
            else:
                assert read.iso3_codes == (), text
                assert read.has_unread_part is True, text


def test_a_leading_the_is_dropped_for_every_row() -> None:
    for name, code in ROWS.items():
        read = classify_nationality(f"the {name}")

        assert read.iso3_codes == (code,), name
        assert read.has_unread_part is False, name


def test_every_row_that_holds_another_country_was_reviewed() -> None:
    holders = {}
    for name, code in ROWS.items():
        for other, other_code in ROWS.items():
            inside = re.search(rf"(?<![A-Z]){re.escape(other)}(?![A-Z])", name)
            if other != name and other_code != code and inside:
                holders[name] = code

    assert holders == ROWS_THAT_HOLD_ANOTHER_COUNTRY


def test_two_rows_with_a_joiner_read_as_those_two_countries_and_nothing_else() -> None:
    names = [name for name in ROWS if " AND " not in name]
    for first, second in itertools.product(names, repeat=2):
        expected = (ROWS[first], ROWS[second])
        for joiner in (" / ", ";", " & ", "+", " and "):
            read = classify_nationality(f"{first}{joiner}{second}")
            assert read.iso3_codes == expected, (first, joiner, second)
            assert read.has_unread_part is False, (first, joiner, second)


def test_two_rows_with_a_comma_are_one_name_in_order_two_short_entries_or_nothing() -> None:
    for first, second in itertools.product(ROWS, repeat=2):
        read = classify_nationality(f"{first}, {second}")
        in_order = f"{second} {first}"
        in_order = in_order[4:] if in_order.startswith("THE ") else in_order
        if in_order in ROWS:
            expected: tuple[tuple[str, ...], bool] = ((ROWS[in_order],), False)
        elif len(first) <= 3 and len(second) <= 3:
            expected = ((ROWS[first], ROWS[second]), False)
        else:
            expected = ((), True)
        assert (read.iso3_codes, read.has_unread_part) == expected, (first, second)


def test_three_letters_are_read_only_as_a_row_or_as_a_code_the_module_knows() -> None:
    known = set(_ISO2_TO_ISO3.values())
    for letters in itertools.product(string.ascii_uppercase, repeat=3):
        text = "".join(letters)
        read = classify_nationality(text)
        if text in ROWS:
            assert read.iso3_codes == (ROWS[text],), text
        elif text in known:
            assert read.iso3_codes == (text,), text
        else:
            assert read.iso3_codes == (), text
            assert read.has_unread_part is True, text


def test_no_three_letter_row_is_the_code_of_another_country() -> None:
    known = set(_ISO2_TO_ISO3.values())
    short_rows = {name: code for name, code in ROWS.items() if len(name) == 3}

    assert short_rows == {
        "DRC": "COD",
        "INA": "IDN",
        "PRC": "CHN",
        "ROC": "TWN",
        "UAE": "ARE",
        "WNI": "IDN",
    }
    assert not [name for name, code in short_rows.items() if name in known and name != code]
