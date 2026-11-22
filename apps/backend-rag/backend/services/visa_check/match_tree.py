"""Rule-based visa recommender for the Visa Match wizard.

Input: nationality (ISO-3), purpose, duration_months, budget_band.
Output: MatchResult with a `ranking` list (best → worst) and backwards-compat
        properties (`recommended_visa`, `reason`, `alternatives`,
        `pre_arrival_steps`, `referral_mode`) read by the router.

Design:
- No hardcoded `VisaType.X` in branch logic. Each branch filters VISA_META
  by purpose tag, scores candidates, returns top-N.
- Scoring is deterministic and visible so users see *why* a rank was chosen.
- Catch-all referral terminates the tree.

Scoring rubric (all additive, clamped to [0.0, 1.0]):
  base:                +0.50
  budget_fit:          +0.25  (visa min-budget ≤ user's band ceiling)
  duration_fit:        +0.20  (visa max-stay ≥ requested months)
  overshoot:           −0.10  (visa covers > 4× requested stay — overkill)
  budget_near_floor:   −0.05  (ceiling < 1.5× visa min — thin headroom)
  high_budget_bonus:   +0.05  (ceiling ≥ 3× visa min — visa is right-sized)
  anti_tourism:        −0.15  (tourism-tagged visa used for non-tourism purpose)
  multi_entry:         −0.30  (multiple-entry visa unsuitable for continuous stay)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from backend.services.visa_check.catalogue import FitTag, VisaMeta, VisaType


def _get_voa_eligible_iso3() -> frozenset[str]:
    # Lazy import breaks the module-init cycle:
    # catalogue._build_meta() -> match_tree -> garuda_flow -> safe_clock -> catalogue.VISA_META
    from backend.services.garuda_flow.nationality_eligibility import (
        VOA_ELIGIBLE_NATIONALITIES,
    )

    return VOA_ELIGIBLE_NATIONALITIES


# ── Canonical Nationality Safety Tables (ISO-2 / ISO-3 / Name) ───────────
# Sourced from RulePack seq-33 (`hf.citizen`, `hf.calling-visa-nationality`,
# `hf.a1.not-bvk-nationality`, `hf.b1.not-voa-nationality`) and
# `garuda_flow.nationality_eligibility.VOA_ELIGIBLE_NATIONALITIES` (97 ISO-3).

CALLING_VISA_ISO3: frozenset[str] = frozenset(
    {
        "AFG",  # Afghanistan (AF)
        "ISR",  # Israel (IL)
        "PRK",  # North Korea / DPRK (KP)
        "LBR",  # Liberia (LR)
        "NGA",  # Nigeria (NG)
        "SOM",  # Somalia (SO)
    }
)

CALLING_VISA_LABELS: dict[str, str] = {
    "AFG": "Afghanistan (AFG)",
    "ISR": "Israel (ISR)",
    "PRK": "North Korea (PRK)",
    "LBR": "Liberia (LBR)",
    "NGA": "Nigeria (NGA)",
    "SOM": "Somalia (SOM)",
}

BVK_VISA_FREE_ISO3: frozenset[str] = frozenset(
    {
        "BRN",  # Brunei Darussalam (BN)
        "MYS",  # Malaysia (MY)
        "THA",  # Thailand (TH)
        "SGP",  # Singapore (SG)
        "PHL",  # Philippines (PH)
        "KHM",  # Cambodia (KH)
        "LAO",  # Laos (LA)
        "MMR",  # Myanmar (MM)
        "VNM",  # Vietnam (VN)
        "TLS",  # Timor-Leste (TL)
        "SUR",  # Suriname (SR)
        "COL",  # Colombia (CO)
        "HKG",  # Hong Kong SAR (HK)
        "TUR",  # Turkey (TR)
        "BRA",  # Brazil (BR)
        "PER",  # Peru (PE)
        "MAC",  # Macao SAR (MO)
        "KAZ",  # Kazakhstan (KZ)
        "BLR",  # Belarus (BY)
    }
)

_ISO2_TO_ISO3: dict[str, str] = {
    "AD": "AND", "AE": "ARE", "AF": "AFG", "AG": "ATG", "AI": "AIA", "AL": "ALB",
    "AM": "ARM", "AO": "AGO", "AR": "ARG", "AS": "ASM", "AT": "AUT", "AU": "AUS",
    "AW": "ABW", "AZ": "AZE", "BA": "BIH", "BB": "BRB", "BD": "BGD", "BE": "BEL",
    "BF": "BFA", "BG": "BGR", "BH": "BHR", "BI": "BDI", "BJ": "BEN", "BM": "BMU",
    "BN": "BRN", "BO": "BOL", "BR": "BRA", "BS": "BHS", "BT": "BTN", "BW": "BWA",
    "BY": "BLR", "BZ": "BLZ", "CA": "CAN", "CD": "COD", "CF": "CAF", "CG": "COG",
    "CH": "CHE", "CI": "CIV", "CL": "CHL", "CM": "CMR", "CN": "CHN", "CO": "COL",
    "CR": "CRI", "CU": "CUB", "CV": "CPV", "CW": "CUW", "CY": "CYP", "CZ": "CZE",
    "DE": "DEU", "DJ": "DJI", "DK": "DNK", "DM": "DMA", "DO": "DOM", "DZ": "DZA",
    "EC": "ECU", "EE": "EST", "EG": "EGY", "ER": "ERI", "ES": "ESP", "ET": "ETH",
    "FI": "FIN", "FJ": "FJI", "FM": "FSM", "FO": "FRO", "FR": "FRA", "GA": "GAB",
    "GB": "GBR", "GD": "GRD", "GE": "GEO", "GF": "GUF", "GG": "GGY", "GH": "GHA",
    "GI": "GIB", "GL": "GRL", "GM": "GMB", "GN": "GIN", "GP": "GLP", "GQ": "GNQ",
    "GR": "GRC", "GT": "GTM", "GU": "GUM", "GW": "GNB", "GY": "GUY", "HK": "HKG",
    "HN": "HND", "HR": "HRV", "HT": "HTI", "HU": "HUN", "ID": "IDN", "IE": "IRL",
    "IL": "ISR", "IM": "IMN", "IN": "IND", "IQ": "IRQ", "IR": "IRN", "IS": "ISL",
    "IT": "ITA", "JE": "JEY", "JM": "JAM", "JO": "JOR", "JP": "JPN", "KE": "KEN",
    "KG": "KGZ", "KH": "KHM", "KI": "KIR", "KM": "COM", "KN": "KNA", "KP": "PRK",
    "KR": "KOR", "KW": "KWT", "KY": "CYM", "KZ": "KAZ", "LA": "LAO", "LB": "LBN",
    "LC": "LCA", "LI": "LIE", "LK": "LKA", "LR": "LBR", "LS": "LSO", "LT": "LTU",
    "LU": "LUX", "LV": "LVA", "LY": "LBY", "MA": "MAR", "MC": "MCO", "MD": "MDA",
    "ME": "MNE", "MG": "MDG", "MH": "MHL", "MK": "MKD", "ML": "MLI", "MM": "MMR",
    "MN": "MNG", "MO": "MAC", "MQ": "MTQ", "MR": "MRT", "MT": "MLT", "MU": "MUS",
    "MV": "MDV", "MW": "MWI", "MX": "MEX", "MY": "MYS", "MZ": "MOZ", "NA": "NAM",
    "NC": "NCL", "NE": "NER", "NG": "NGA", "NI": "NIC", "NL": "NLD", "NO": "NOR",
    "NP": "NPL", "NR": "NRU", "NZ": "NZL", "OM": "OMN", "PA": "PAN", "PE": "PER",
    "PF": "PYF", "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL", "PR": "PRI",
    "PS": "PSE", "PT": "PRT", "PW": "PLW", "PY": "PRY", "QA": "QAT", "RE": "REU",
    "RO": "ROU", "RS": "SRB", "RU": "RUS", "RW": "RWA", "SA": "SAU", "SB": "SLB",
    "SC": "SYC", "SD": "SDN", "SE": "SWE", "SG": "SGP", "SI": "SVN", "SK": "SVK",
    "SL": "SLE", "SM": "SMR", "SN": "SEN", "SO": "SOM", "SR": "SUR", "SS": "SSD",
    "ST": "STP", "SV": "SLV", "SX": "SXM", "SY": "SYR", "SZ": "SWZ", "TD": "TCD",
    "TG": "TGO", "TH": "THA", "TJ": "TJK", "TL": "TLS", "TM": "TKM", "TN": "TUN",
    "TO": "TON", "TR": "TUR", "TT": "TTO", "TV": "TUV", "TW": "TWN", "TZ": "TZA",
    "UA": "UKR", "UG": "UGA", "UK": "GBR", "US": "USA", "UY": "URY", "UZ": "UZB",
    "VA": "VAT", "VC": "VCT", "VE": "VEN", "VI": "VIR", "VN": "VNM", "VU": "VUT",
    "WS": "WSM", "XK": "XKX", "YE": "YEM", "ZA": "ZAF", "ZM": "ZMB", "ZW": "ZWE",
}

_NAME_OR_DEMONYM_TO_ISO3: dict[str, str] = {
    "INDONESIA": "IDN", "INDONESIAN": "IDN", "WNI": "IDN", "WARGA NEGARA INDONESIA": "IDN",
    "DEMOCRATIC PEOPLE'S REPUBLIC OF KOREA": "PRK", "DEMOCRATIC PEOPLES REPUBLIC OF KOREA": "PRK",
    "REPUBLIC OF KOREA": "KOR",
    # Names whose country depends on the words in front: read only as written here.
    "PEOPLE'S REPUBLIC OF CHINA": "CHN", "PEOPLES REPUBLIC OF CHINA": "CHN", "PRC": "CHN",
    "REPUBLIC OF CHINA": "TWN", "ROC": "TWN",
    "REPUBLIC OF THE CONGO": "COG", "REPUBLIC OF CONGO": "COG",
    "DEMOCRATIC REPUBLIC OF THE CONGO": "COD", "DEMOCRATIC REPUBLIC OF CONGO": "COD",
    "DR CONGO": "COD", "DRC": "COD",
    "UNITED KINGDOM OF GREAT BRITAIN AND NORTHERN IRELAND": "GBR",
    "AFGHANISTAN": "AFG", "AFGHAN": "AFG",
    "ISRAEL": "ISR", "ISRAELI": "ISR",
    "NORTH KOREA": "PRK", "NORTH KOREAN": "PRK", "DPRK": "PRK",
    "LIBERIA": "LBR", "LIBERIAN": "LBR",
    "NIGERIA": "NGA", "NIGERIAN": "NGA",
    "SOMALIA": "SOM", "SOMALI": "SOM", "SOMALIAN": "SOM",
    "PAKISTAN": "PAK", "PAKISTANI": "PAK",
    "AUSTRALIA": "AUS", "AUSTRALIAN": "AUS",
    "UNITED STATES": "USA", "UNITED STATES OF AMERICA": "USA", "AMERICAN": "USA",
    "UNITED KINGDOM": "GBR", "GREAT BRITAIN": "GBR", "BRITISH": "GBR", "ENGLISH": "GBR",
    "GERMANY": "DEU", "GERMAN": "DEU",
    "FRANCE": "FRA", "FRENCH": "FRA",
    "ITALY": "ITA", "ITALIAN": "ITA",
    "SPAIN": "ESP", "SPANISH": "ESP",
    "NETHERLANDS": "NLD", "DUTCH": "NLD",
    "CANADA": "CAN", "CANADIAN": "CAN",
    "JAPAN": "JPN", "JAPANESE": "JPN",
    "SOUTH KOREA": "KOR", "SOUTH KOREAN": "KOR",
    "CHINA": "CHN", "CHINESE": "CHN",
    "INDIA": "IND", "INDIAN": "IND",
    "RUSSIA": "RUS", "RUSSIAN": "RUS",
    "SINGAPORE": "SGP", "SINGAPOREAN": "SGP",
    "MALAYSIA": "MYS", "MALAYSIAN": "MYS",
    "THAILAND": "THA", "THAI": "THA",
    "VIETNAM": "VNM", "VIETNAMESE": "VNM",
    "PHILIPPINES": "PHL", "FILIPINO": "PHL",
    "BRUNEI": "BRN", "CAMBODIA": "KHM", "CAMBODIAN": "KHM",
    "LAOS": "LAO", "LAOTIAN": "LAO", "MYANMAR": "MMR", "BURMESE": "MMR",
    "TIMOR-LESTE": "TLS", "TIMOR LESTE": "TLS",
    "BRAZIL": "BRA", "BRAZILIAN": "BRA",
    "SWITZERLAND": "CHE", "SWISS": "CHE",
    "SWEDEN": "SWE", "SWEDISH": "SWE",
    "NORWAY": "NOR", "NORWEGIAN": "NOR",
    "DENMARK": "DNK", "DANISH": "DNK",
    "FINLAND": "FIN", "FINNISH": "FIN",
    "POLAND": "POL", "POLISH": "POL",
    "PORTUGAL": "PRT", "PORTUGUESE": "PRT",
    "IRELAND": "IRL", "IRISH": "IRL",
    "AUSTRIA": "AUT", "AUSTRIAN": "AUT",
    "BELGIUM": "BEL", "BELGIAN": "BEL",
    "NEW ZEALAND": "NZL", "NEW ZEALANDER": "NZL",
    "SOUTH AFRICA": "ZAF", "SOUTH AFRICAN": "ZAF",
    "MEXICO": "MEX", "MEXICAN": "MEX",
    "ARGENTINA": "ARG", "ARGENTINE": "ARG",
    "CHILE": "CHL", "CHILEAN": "CHL",
    "COLOMBIA": "COL", "COLOMBIAN": "COL",
    "PERU": "PER", "PERUVIAN": "PER",
    "TURKEY": "TUR", "TURKISH": "TUR",
    "UAE": "ARE", "UNITED ARAB EMIRATES": "ARE", "EMIRATI": "ARE",
    "SAUDI ARABIA": "SAU", "SAUDI": "SAU",
    "QATAR": "QAT", "QATARI": "QAT",
    "UKRAINE": "UKR", "UKRAINIAN": "UKR",
    "BELARUS": "BLR", "BELARUSIAN": "BLR",
    "KAZAKHSTAN": "KAZ", "KAZAKH": "KAZ",
    "HONG KONG": "HKG", "MACAU": "MAC", "MACAO": "MAC", "TAIWAN": "TWN", "TAIWANESE": "TWN",
    "BANGLADESH": "BGD", "BANGLADESHI": "BGD",
    "SRI LANKA": "LKA", "SRI LANKAN": "LKA",
    "NEPAL": "NPL", "NEPALESE": "NPL",
    "IRAN": "IRN", "IRANIAN": "IRN",
    "IRAQ": "IRQ", "IRAQI": "IRQ",
    "CAMEROON": "CMR", "CAMEROONIAN": "CMR",
    "GUINEA": "GIN", "GUINEAN": "GIN",
    # Reviewed names and aliases. A name is read only when it is a row here:
    # nothing is derived from a text, so a formal name needs its own row.
    "REPUBLIC OF INDONESIA": "IDN",
    "REPUBLIK INDONESIA": "IDN",
    "NEGARA KESATUAN REPUBLIK INDONESIA": "IDN",
    "INA": "IDN",
    "ISLAMIC REPUBLIC OF AFGHANISTAN": "AFG",
    "ISLAMIC EMIRATE OF AFGHANISTAN": "AFG",
    "AFGANISTAN": "AFG",
    "STATE OF ISRAEL": "ISR",
    "REPUBLIC OF LIBERIA": "LBR",
    "FEDERAL REPUBLIC OF NIGERIA": "NGA",
    "FEDERAL REPUBLIC OF SOMALIA": "SOM",
    "DPR KOREA": "PRK",
    "KOREA DPR": "PRK",
    "KOREA UTARA": "PRK",
    "ARGENTINE REPUBLIC": "ARG",
    "REPUBLIC OF ARGENTINA": "ARG",
    "COMMONWEALTH OF AUSTRALIA": "AUS",
    "REPUBLIC OF AUSTRIA": "AUT",
    "KINGDOM OF BELGIUM": "BEL",
    "PEOPLE'S REPUBLIC OF BANGLADESH": "BGD",
    "PEOPLES REPUBLIC OF BANGLADESH": "BGD",
    "REPUBLIC OF BELARUS": "BLR",
    "FEDERATIVE REPUBLIC OF BRAZIL": "BRA",
    "BRUNEI DARUSSALAM": "BRN",
    "SWISS CONFEDERATION": "CHE",
    "REPUBLIC OF CHILE": "CHL",
    "REPUBLIC OF CAMEROON": "CMR",
    "REPUBLIC OF COLOMBIA": "COL",
    "FEDERAL REPUBLIC OF GERMANY": "DEU",
    "KINGDOM OF DENMARK": "DNK",
    "KINGDOM OF SPAIN": "ESP",
    "REPUBLIC OF FINLAND": "FIN",
    "FRENCH REPUBLIC": "FRA",
    "REPUBLIC OF FRANCE": "FRA",
    "REPUBLIC OF GUINEA": "GIN",
    "HONG KONG SPECIAL ADMINISTRATIVE REGION": "HKG",
    "HONG KONG SAR": "HKG",
    "REPUBLIC OF INDIA": "IND",
    "REPUBLIC OF IRELAND": "IRL",
    "ISLAMIC REPUBLIC OF IRAN": "IRN",
    "REPUBLIC OF IRAQ": "IRQ",
    "ITALIAN REPUBLIC": "ITA",
    "REPUBLIC OF ITALY": "ITA",
    "REPUBLIC OF KAZAKHSTAN": "KAZ",
    "KINGDOM OF CAMBODIA": "KHM",
    "LAO PEOPLE'S DEMOCRATIC REPUBLIC": "LAO",
    "LAO PEOPLES DEMOCRATIC REPUBLIC": "LAO",
    "LAO PDR": "LAO",
    "DEMOCRATIC SOCIALIST REPUBLIC OF SRI LANKA": "LKA",
    "MACAO SPECIAL ADMINISTRATIVE REGION": "MAC",
    "MACAU SPECIAL ADMINISTRATIVE REGION": "MAC",
    "MACAO SAR": "MAC",
    "MACAU SAR": "MAC",
    "UNITED MEXICAN STATES": "MEX",
    "REPUBLIC OF THE UNION OF MYANMAR": "MMR",
    "KINGDOM OF THE NETHERLANDS": "NLD",
    "KINGDOM OF NORWAY": "NOR",
    "FEDERAL DEMOCRATIC REPUBLIC OF NEPAL": "NPL",
    "ISLAMIC REPUBLIC OF PAKISTAN": "PAK",
    "REPUBLIC OF PERU": "PER",
    "REPUBLIC OF THE PHILIPPINES": "PHL",
    "REPUBLIC OF POLAND": "POL",
    "PORTUGUESE REPUBLIC": "PRT",
    "REPUBLIC OF PORTUGAL": "PRT",
    "STATE OF QATAR": "QAT",
    "RUSSIAN FEDERATION": "RUS",
    "KINGDOM OF SAUDI ARABIA": "SAU",
    "REPUBLIC OF SINGAPORE": "SGP",
    "KINGDOM OF SWEDEN": "SWE",
    "KINGDOM OF THAILAND": "THA",
    "DEMOCRATIC REPUBLIC OF TIMOR-LESTE": "TLS",
    "DEMOCRATIC REPUBLIC OF TIMOR LESTE": "TLS",
    "REPUBLIC OF TURKIYE": "TUR",
    "REPUBLIC OF TURKEY": "TUR",
    "TURKIYE": "TUR",
    "SOCIALIST REPUBLIC OF VIET NAM": "VNM",
    "SOCIALIST REPUBLIC OF VIETNAM": "VNM",
    "VIET NAM": "VNM",
    "REPUBLIC OF SOUTH AFRICA": "ZAF",
}


@dataclass(frozen=True)
class NationalityClassification:
    iso3_codes: tuple[str, ...]
    is_indonesian_citizen: bool
    is_calling_visa: bool
    calling_visa_code: str | None
    is_voa_eligible: bool
    is_bvk_eligible: bool
    # Some part of the text was not read as a country. The flags above then say
    # what was read, not that the rest is clear of the gates: a caller that offers
    # candidates must not offer them on a nationality it could not read in full.
    has_unread_part: bool = False


# What joins two nationalities in one answer. A comma is not in the list: "Guinea,
# French" names one place, and reading it as two nationalities would invent the second.
_NATIONALITY_JOINERS = re.compile(r"[/;&+]")
_NATIONALITY_AND = re.compile(r"\s+AND\s+")
# The three-letter codes this module has a row for. It is the module's coverage, not
# the whole ISO register: a code outside it is not read.
_KNOWN_ISO3: frozenset[str] = frozenset(_ISO2_TO_ISO3.values())


def _fold(text: str) -> str:
    """Upper case, compatibility forms and accents folded, apostrophes straightened.

    NFKD: a fullwidth letter or separator and a no-break space become the plain ones.
    Nothing is removed beyond combining marks, so a look-alike letter of another
    script or a zero-width character stays in the text and the text is not read.
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(plain.replace("\u2019", "'").replace("`", "'").upper().split())


def _code_to_iso3(cleaned: str) -> str | None:
    """Two letters of the ISO-2 table, or three letters of a code this module knows."""
    if len(cleaned) == 2 and cleaned.isalpha():
        return _ISO2_TO_ISO3.get(cleaned)
    if len(cleaned) == 3 and cleaned in _KNOWN_ISO3:
        return cleaned
    return None


def _entry_to_iso3(cleaned: str) -> str | None:
    """A reviewed name or alias of the table, or a code, exactly as written.

    One leading "THE" is dropped. Nothing else is taken off or put on a text: a
    name the table does not hold is not read.
    """
    candidates = [cleaned, cleaned[4:]] if cleaned.startswith("THE ") else [cleaned]
    for candidate in candidates:
        if candidate in _NAME_OR_DEMONYM_TO_ISO3:
            return _NAME_OR_DEMONYM_TO_ISO3[candidate]
        code = _code_to_iso3(candidate)
        if code is not None:
            return code
    return None


def _token_to_iso3(token: str) -> str | None:
    cleaned = _fold(token)
    return _entry_to_iso3(cleaned) if cleaned else None


def _read_piece(piece: str) -> tuple[list[str], bool]:
    """One answer with no joiner in it: the codes read, and whether it was not read.

    A comma is read in two shapes only: one name written with its own comma ("Korea,
    North"), when the name put back in order is a name of the table; and a list of
    codes or short aliases ("IT, US"). Any other text with a comma is not read, and
    then none of its parts is.
    """
    code = _entry_to_iso3(piece)
    if code is not None:
        return [code], False
    parts = [part for part in (raw.strip() for raw in piece.split(",")) if part]
    if not parts:
        return [], False
    if len(parts) == 1:
        code = _entry_to_iso3(parts[0])
        return ([code], False) if code is not None else ([], True)
    if len(parts) == 2:
        in_order = f"{parts[1]} {parts[0]}"
        candidates = [in_order, in_order[4:]] if in_order.startswith("THE ") else [in_order]
        for candidate in candidates:
            if candidate in _NAME_OR_DEMONYM_TO_ISO3:
                return [_NAME_OR_DEMONYM_TO_ISO3[candidate]], False
    codes = [_entry_to_iso3(part) if len(part) <= 3 else None for part in parts]
    if all(code is not None for code in codes):
        return [code for code in codes if code is not None], False
    return [], True


def _read_nationality(nationality: str) -> tuple[tuple[str, ...], bool]:
    """The ISO-3 codes read from the text, and whether some part of it was not read."""
    text = _fold(nationality)
    if not text:
        return (), False
    whole = _entry_to_iso3(text)
    if whole is not None:
        return (whole,), False
    codes: list[str] = []
    unread = False
    for chunk in _NATIONALITY_JOINERS.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        read, missed = _read_piece(chunk)
        if missed and _NATIONALITY_AND.search(chunk):
            # "Nigeria and USA": two answers joined by a word. Tried only when the
            # chunk is not read as it stands, so a name that holds an "and" of its
            # own is read whole when it is alone.
            read, missed = [], False
            for part in _NATIONALITY_AND.split(chunk):
                part_read, part_missed = _read_piece(part.strip())
                read += part_read
                missed = missed or part_missed
        codes += read
        unread = unread or missed
    return tuple(codes), unread


def classify_nationality(nationality: str) -> NationalityClassification:
    """Resolve codes and the reviewed names of the table, one or several.

    Several nationalities are joined by ``/`` ``;`` ``&`` ``+`` or "and"; codes may also
    be listed with commas. A part that is not read is reported in ``has_unread_part``,
    never dropped in silence.
    """
    resolved, has_unread_part = _read_nationality(nationality)
    is_wni = "IDN" in resolved
    calling_code = next((c for c in resolved if c in CALLING_VISA_ISO3), None)
    is_calling = (not is_wni) and (calling_code is not None)
    voa_set = _get_voa_eligible_iso3()
    is_voa = (
        (not is_wni)
        and (not is_calling)
        and any(c in voa_set for c in resolved)
    )
    is_bvk = (
        (not is_wni)
        and (not is_calling)
        and any(c in BVK_VISA_FREE_ISO3 for c in resolved)
    )
    return NationalityClassification(
        iso3_codes=resolved,
        is_indonesian_citizen=is_wni,
        is_calling_visa=is_calling,
        calling_visa_code=calling_code,
        is_voa_eligible=is_voa,
        is_bvk_eligible=is_bvk,
        has_unread_part=has_unread_part,
    )


# VISA_META is imported lazily inside _rank_for_purpose() to break the
# circular import: catalogue._build_meta() imports Purpose from this module,
# so importing VISA_META here at module-load time would create a cycle.
# The lazy import is safe because _rank_for_purpose() is only called after
# both modules have fully initialised.
def _get_visa_meta() -> dict[VisaType, VisaMeta]:
    from backend.services.visa_check.catalogue import VISA_META

    return VISA_META


class Purpose(str, Enum):
    WORK_REMOTE = "work_remote"
    INVESTOR = "investor"
    WORK_EMPLOYEE = "work_employee"
    FAMILY = "family"
    LONG_TOURISM = "long_tourism"
    RETIREMENT = "retirement"
    STUDENT = "student"
    OTHER = "other"


class WorkActivity(str, Enum):
    """Optional detail on HOW the applicant works. Only used to route E23-FREELANCE.

    E23-FREELANCE is the commercial "Impresariat" product: an E23 working KITAS
    (180-day tier) sponsored by an impresario company for sports/artistic work.
    A foreigner cannot invoice Indonesian clients as a freelancer (sponsor must be
    the employer: Permenkumham 22/2023 Pasal 34(1)(b); UU 13/2003 Pasal 42(4)).
    """

    SEMI_INDEPENDENT_ARTS_SPORTS = "semi_independent_arts_sports"
    FREELANCE_INDONESIAN_CLIENTS = "freelance_indonesian_clients"
    FREELANCE_FOREIGN_CLIENTS = "freelance_foreign_clients"


class BudgetBand(str, Enum):
    UNDER_50M = "under_50m"
    MID_50_500M = "50m_500m"
    OVER_500M = "over_500m"


_BUDGET_CEILING: dict[BudgetBand, int] = {
    BudgetBand.UNDER_50M: 50_000_000,
    BudgetBand.MID_50_500M: 500_000_000,
    BudgetBand.OVER_500M: 10_000_000_000,
}


@dataclass(frozen=True)
class RankedVisa:
    visa: VisaType
    score: float  # 0.0 – 1.0
    reason: str  # 1–2 sentences, user-facing
    fit_tags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MatchResult:
    ranking: list[RankedVisa]
    pre_arrival_steps: list[str]
    referral_mode: bool
    referral_reason: str = ""  # used when ranking is empty

    # ── Backwards-compat properties (router + existing tests) ────

    @property
    def recommended_visa(self) -> VisaType | None:
        return self.ranking[0].visa if self.ranking else None

    @property
    def reason(self) -> str:
        if self.ranking:
            return self.ranking[0].reason
        return self.referral_reason

    @property
    def alternatives(self) -> list[VisaType]:
        return [rv.visa for rv in self.ranking[1:]]


# ── Pre-arrival step libraries ───────────────────────────────────

_STEPS_TOURISM: list[str] = [
    "Passport valid ≥ 6 months from entry date",
    "Confirmed accommodation for first 7 nights",
    "Return or onward flight ticket",
    "Travel insurance covering Indonesia",
]

_STEPS_DIGITAL_NOMAD: list[str] = [
    "Proof of remote employment with a foreign company",
    "Passport valid ≥ 18 months",
    "Health insurance with Indonesia coverage",
    "CV + LinkedIn URL for immigration review",
]

_STEPS_IMPRESARIAT: list[str] = [
    "Sponsorship by Bali Zero as impresario company (acts as your employer for this role)",
    "RPTKA (temporary, max 6 months) and job title for your specific role (e.g. DJ, coach, dancer)",
    "Portfolio and/or signed contract for the engagement",
    "Passport valid ≥ 18 months",
    "Proof of accommodation in Indonesia",
]

# Purpose-level list: shown for D12 (pre-investment visit visa, "before committing to PT PMA").
# E28A has its own list in `_STEPS_EXCLUSIVE_BY_VISA`; none of its post-incorporation documents
# belong here.
_STEPS_INVESTOR: list[str] = [
    "PT PMA incorporated (or plan ready to incorporate on arrival)",
    "Investment plan document for the business you want to explore",
    "Proposed business address in Indonesia",
]

# Official E28A page (imigrasi.go.id, 2026-10-15): base documents + special requirements.
_STEPS_E28A: list[str] = [
    "Application letter for the visa",
    "Passport valid \u2265 6 months",
    "Bank statement for the last 3 months, in your name or your sponsor's, of at least USD 2,000 or equivalent",
    "Recent colour photo (taken within the last year)",
    "Curriculum vitae (CV)",
    "Travel itinerary",
    "Proof of your own shareholding of at least IDR 10 billion in the sponsoring company, registered at the Investment Ministry / BKPM",
    "Kemenkumham decision approving the establishment of the sponsoring company",
    "Company current-account statement for the last 2 months (a company under 2 months old may file it within 90 days after the ITAS is issued)",
]

_STEPS_EMPLOYEE: list[str] = [
    "RPTKA from sponsoring Indonesian employer",
    "Signed employment contract",
    "Education certificate (notarised + translated)",
    "Curriculum vitae",
    "Passport valid ≥ 18 months",
]

# Shared by both retirement routes. The route-specific money line is
# prepended per product by `_steps_for` — see `_STEPS_BY_VISA` below.
_STEPS_RETIREMENT: list[str] = [
    "Passport valid ≥ 18 months",
    "Proof of accommodation (rental or property in Indonesia)",
    "Domestic helper hire letter (optional but recommended)",
    "Health insurance valid in Indonesia",
]

_STEPS_FAMILY: list[str] = [
    "Sponsor's KTP & Kartu Keluarga (if Indonesian citizen) or KITAS/KITAP + passport copies",
    "Marriage or birth certificate (notarised + translated)",
    "Passport valid ≥ 18 months",
    "Proof of joint household (photos + rental/ownership docs)",
]

_STEPS_STUDENT: list[str] = [
    "Acceptance letter from an accredited Indonesian school or educational institution (E30B applies for university higher education)",
    "Sponsor letter from the accredited educational institution",
    "Passport valid ≥ the full study programme",
    "Health insurance valid in Indonesia",
]


_STEPS_BY_PURPOSE: dict[Purpose, list[str]] = {
    Purpose.LONG_TOURISM: _STEPS_TOURISM,
    Purpose.WORK_REMOTE: _STEPS_DIGITAL_NOMAD,
    Purpose.INVESTOR: _STEPS_INVESTOR,
    Purpose.WORK_EMPLOYEE: _STEPS_EMPLOYEE,
    Purpose.RETIREMENT: _STEPS_RETIREMENT,
    Purpose.FAMILY: _STEPS_FAMILY,
    Purpose.STUDENT: _STEPS_STUDENT,
}


#: Financial requirements that belong to ONE product, prepended to the
#: purpose-level list for the visa actually recommended.
#:
#: These cannot live in `_STEPS_BY_PURPOSE`. The result is rendered as an
#: ordered "Pre-arrival checklist" in the frontend and introduced in the funnel
#: email as "make sure you have the <visa> essentials" — i.e. as ONE visa's
#: mandatory list. Putting both routes' money lines in it would tell an E33F
#: client to place a USD 50,000 deposit they do not need, and would show an
#: E33E client "no deposit required" immediately above a deposit.
#:
#: The two senior routes (when #5357 landed, the visa catalogue of that time
#: named them with the generic retirement name; the price rows are now
#: "E33E Second Home Senior (5 Years)" and "E33F Second Home Senior (1 Year,
#: Offshore)" / "(1 Year, Altus/Onshore)") DO share the USD 3,000/month income
#: requirement; only E33E adds a deposit on top. Both figures map to `confirmed`
#: entries in research/secondhome/e33-fact-registry.json (e33e_requirements,
#: e33f_requirements) and to the E33E price-row note text ("USD 50,000 deposit
#: at a state-owned Indonesian bank").
_STEPS_BY_VISA: dict[VisaType, list[str]] = {
    VisaType.E33E: [
        "USD 50,000 deposit at a state-owned (BUMN) Indonesian bank, placed "
        "within 90 days of arrival",
        "Proof of pension or passive income \u2265 USD 3,000/month",
    ],
    VisaType.E33F: [
        "Proof of pension or passive income \u2265 USD 3,000/month "
        "\u2014 this route requires no deposit",
    ],
    # E33G requires annual foreign salary/income (not C1/C2/Impresariat).
    VisaType.E33G: [
        "Bank proof of salary or income of at least USD 60,000 a year",
    ],
}


#: Visas whose checklist REPLACES the purpose-level list (it would be wrong for them).
_STEPS_EXCLUSIVE_BY_VISA: dict[VisaType, list[str]] = {
    VisaType.E28A: _STEPS_E28A,
    VisaType.E23_FREELANCE: _STEPS_IMPRESARIAT,
}


def _steps_for(visa: VisaType | None, purpose: Purpose) -> list[str]:
    """Pre-arrival steps for the recommended VISA, not merely its purpose.

    One purpose can rank several products with different money requirements,
    and every caller presents this list as a single visa's mandatory
    checklist — so a purpose-level list cannot carry a product-level figure.
    """
    shared = _STEPS_BY_PURPOSE.get(purpose, [])
    if visa in _STEPS_EXCLUSIVE_BY_VISA:
        return list(_STEPS_EXCLUSIVE_BY_VISA[visa])
    if visa is None:
        return shared
    return _STEPS_BY_VISA.get(visa, []) + shared


# ── Scoring ──────────────────────────────────────────────────────


def _budget_fits(meta: VisaMeta, band: BudgetBand) -> bool:
    """True if the user's budget band ceiling strictly exceeds the visa's min_budget_idr.

    Strict `>` so that a visa with min_budget_idr == 50,000,000 (e.g. E33G)
    is NOT considered affordable at the under-50M band (ceiling 50M). This
    keeps the band semantics consistent with their labels:
    - UNDER_50M  = budget strictly less than 50M
    - MID_50_500M = budget in [50M, 500M)
    - OVER_500M  = budget > 500M (ceiling effectively unbounded)
    """
    if meta.min_budget_idr is None:
        return True
    return _BUDGET_CEILING[band] > meta.min_budget_idr


def _duration_fits(meta: VisaMeta, months: int) -> bool:
    """True if the visa's base + extensions cover the requested months."""
    total_days = meta.duration_days + meta.extensions[0] * meta.extensions[1]
    return total_days >= months * 30


def _score(meta: VisaMeta, purpose: Purpose, months: int, band: BudgetBand) -> float:
    """Deterministic score in [0.0, 1.0]. See module docstring for rubric."""
    score = 0.5
    budget_ceiling = _BUDGET_CEILING[band]

    # Budget fit bonus
    if _budget_fits(meta, band):
        score += 0.25

    # Duration fit bonus
    if _duration_fits(meta, months):
        score += 0.2

    # Duration overshoot penalty: visa covers > 4× requested stay (overkill)
    total_days = meta.duration_days + meta.extensions[0] * meta.extensions[1]
    if total_days > months * 30 * 4:
        score -= 0.1

    # Budget near-floor penalty: thin headroom between ceiling and min
    if meta.min_budget_idr is not None and budget_ceiling < meta.min_budget_idr * 1.5:
        score -= 0.05

    # High-budget preference bonus: visa is right-sized for this budget band
    if meta.min_budget_idr is not None and budget_ceiling >= meta.min_budget_idr * 3:
        score += 0.05

    # Anti-tourism penalty: tourism-tagged visa used for a non-tourism purpose.
    # C1/C2/C6 are valid short-term fallbacks but are semantically off-target
    # for users who identify as remote workers, investors, etc.
    if purpose != Purpose.LONG_TOURISM and Purpose.LONG_TOURISM in meta.purposes:
        score -= 0.15

    # Multi-entry penalty: D2 / multi-entry visas assume frequent short trips
    # and are semantically unsuitable for users planning continuous residence.
    if FitTag.MULTI_ENTRY in meta.fit_tags:
        score -= 0.30

    return max(0.0, min(1.0, round(score, 3)))


def _reason(meta: VisaMeta, purpose: Purpose, months: int, band: BudgetBand) -> str:
    """Short user-facing sentence explaining why this visa is ranked here."""
    del purpose, band  # reserved for future per-branch phrasing
    budget_note = ""
    if meta.min_budget_idr is not None and not meta.budget_gate_is_ranking_only:
        budget_note = f" (requires ≥ IDR {meta.min_budget_idr // 1_000_000}M)"
    total_days = meta.duration_days + meta.extensions[0] * meta.extensions[1]
    if meta.coverage_total_unstated:
        # Pack: 365 days per extension, no stated maximum total; page: extendable online by the sponsor.
        duration_note = f" Stay of {meta.duration_days} days (2 years), extendable online by the sponsor."
    elif _duration_fits(meta, months):
        duration_note = f" Covers {total_days} days total."
    else:
        duration_note = f" Max stay {total_days} days \u2014 less than your {months}-month plan."
    stop = "" if (not budget_note and meta.notes.endswith(".")) else "."
    return f"{meta.name_en} — {meta.notes}{budget_note}{stop}{duration_note}".strip()


# ── Branch dispatch ──────────────────────────────────────────────


_ARTS_EVENT_VISIT_VISAS: frozenset[VisaType] = frozenset(
    {
        VisaType.C7,
        VisaType.C7A,
        VisaType.C7B,
    }
)


def _rank_for_purpose(
    purpose: Purpose,
    months: int,
    band: BudgetBand,
    max_results: int,
    *,
    voa_eligible: bool = True,
    bvk_eligible: bool = False,
    activity: WorkActivity | None = None,
) -> list[RankedVisa]:
    candidates: list[RankedVisa] = []
    for visa_type, meta in _get_visa_meta().items():
        if purpose not in meta.purposes:
            continue
        if visa_type is VisaType.B1 and not voa_eligible:
            continue
        if visa_type.value == "A1" and not bvk_eligible:
            continue
        if (
            visa_type in _ARTS_EVENT_VISIT_VISAS
            and activity is not WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS
        ):
            # Event performance visit visas (C7/C7A/C7B) must never outrank E23
            # for salaried employees (e.g. software engineers).
            continue
        if meta.min_budget_idr is not None and not _budget_fits(meta, band):
            # Hard-exclude visas whose minimum budget the user cannot meet.
            continue
        score = _score(meta, purpose, months, band)
        candidates.append(
            RankedVisa(
                visa=visa_type,
                score=score,
                reason=_reason(meta, purpose, months, band),
                fit_tags=meta.fit_tags,
            )
        )
    # Sort by score desc, then by code for deterministic tie-break.
    candidates.sort(key=lambda rv: (-rv.score, rv.visa.value))
    return candidates[:max_results]


_MAX_RESULTS: dict[Purpose, int] = {
    Purpose.WORK_EMPLOYEE: 3,  # E23 + short-term (C18/C22A) alternatives
    Purpose.STUDENT: 2,
    Purpose.FAMILY: 2,
    Purpose.RETIREMENT: 2,
    Purpose.INVESTOR: 3,
    Purpose.WORK_REMOTE: 3,
    Purpose.LONG_TOURISM: 3,
}


#: Human-readable purpose names used inside referral explanations.
#: Every `Purpose` must appear here — `test_every_purpose_has_a_label` enforces
#: it, so a new purpose cannot ship with an explanation that names it `None`.
_PURPOSE_LABEL: dict[Purpose, str] = {
    Purpose.WORK_REMOTE: "remote work",
    Purpose.INVESTOR: "investment",
    Purpose.WORK_EMPLOYEE: "local employment",
    Purpose.FAMILY: "family",
    Purpose.LONG_TOURISM: "tourism",
    Purpose.RETIREMENT: "retirement",
    Purpose.STUDENT: "study",
    Purpose.OTHER: "the purpose you chose",
}


def _covered_purposes_sentence() -> str:
    """List the purposes this form can actually rank, as prose.

    Derived from `Purpose` rather than hardcoded, so adding a purpose updates
    the visitor-facing sentence instead of silently turning it into a lie.
    """
    labels = [_PURPOSE_LABEL[p] for p in Purpose if p is not Purpose.OTHER]
    return f"{', '.join(labels[:-1])} and {labels[-1]}"


def _explain_empty_ranking(purpose: Purpose, months: int, band: BudgetBand) -> str:
    """Name the specific constraint that eliminated every candidate.

    `_rank_for_purpose` can come back empty for two reasons that mean opposite
    things to the visitor: the catalogue carries no visa for this purpose at
    all, or it carries several but the budget filter excluded every one. A
    single "no visa matches cleanly" for both hides the one fact the visitor
    could act on, so each cause gets its own sentence and its own number.
    """
    tagged = [meta for meta in _get_visa_meta().values() if purpose in meta.purposes]

    if not tagged:
        return (
            f"Our catalogue carries no visa tagged for {_PURPOSE_LABEL[purpose]}, so "
            "there is nothing for us to rank — we would rather tell you that than hand "
            "you a poor match. Indonesia's visa index is wider than this form: tell us "
            "on WhatsApp what you will actually be doing here and we will name the route."
        )

    if not any(_budget_fits(meta, band) for meta in tagged):
        # `_budget_fits` returns True whenever `min_budget_idr is None`, so if
        # nothing fit then every tagged visa carries a minimum — `min()` below
        # can never run over an empty sequence.
        cheapest = min(meta.min_budget_idr for meta in tagged if meta.min_budget_idr is not None)
        return (
            f"Every {_PURPOSE_LABEL[purpose]} visa we rank carries a minimum-budget "
            "requirement above the band you selected — the cheapest one starts at "
            f"IDR {cheapest:,}. On WhatsApp we can work out whether a staged approach "
            "gets you there, or whether a different route fits what you have today."
        )

    return (
        f"We hold {len(tagged)} {_PURPOSE_LABEL[purpose]} route(s), and none of them "
        f"came through our ranking for a {months}-month stay at your budget band. That "
        "combination is unusual enough that it deserves a person rather than a form — "
        "15 minutes on WhatsApp."
    )


def recommend_visa(
    *,
    nationality: str,
    purpose: Purpose,
    duration_months: int,
    budget_band: BudgetBand,
    activity: WorkActivity | None = None,
) -> MatchResult:
    """Return a ranked list of visas + pre-arrival steps.

    `activity` (optional) refines WORK_REMOTE / WORK_EMPLOYEE: only
    SEMI_INDEPENDENT_ARTS_SPORTS surfaces the Impresariat product (E23-FREELANCE);
    a freelancer invoicing Indonesian clients is never matched to it.

    Rules:
    1. `OTHER` always refers to WhatsApp.
    2. Under-budget investors refer (the investor routes all require a minimum capital commitment).
    3. Tourism > 6 months refers (Indonesian tourism visas cap at ~180d).
    4. Everything else: filter VISA_META by purpose tag, score, top-N.

    Invariant on every referral (ruled by Zero, 2026-08-28): the reason must
    name the specific constraint that blocked us — quoting the stay length,
    the purpose, or a real IDR figure — and must tell the visitor where to go
    next. A generic "your case needs a conversation" is not an explanation.
    Enforced across the whole input space by
    `tests/services/visa_check/test_match_tree_referral_reasons.py`.
    """
    months = max(1, min(60, int(duration_months)))
    nat = classify_nationality(nationality)

    if nat.is_indonesian_citizen:
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                "Indonesian citizens (WNI) enter and reside in Indonesia on an "
                "Indonesian passport and do not require a foreigner visa for a "
                f"{months}-month {_PURPOSE_LABEL[purpose]} stay. If you hold dual "
                "nationality (such as a dual-passport child under UU 12/2006) or "
                "are checking options for a foreign family member, message a Bali "
                "Zero advisor on WhatsApp so we can review the exact status."
            ),
        )

    if nat.is_calling_visa:
        country_label = CALLING_VISA_LABELS.get(
            nat.calling_visa_code or "", nat.calling_visa_code or nationality.strip().upper()
        )
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                f"Passports from {country_label} require Directorate General of "
                "Immigration (Ditjen Imigrasi) Calling Visa clearance and are not "
                "eligible for Visa on Arrival (B1) or standard self-service e-Visas "
                f"for a {months}-month {_PURPOSE_LABEL[purpose]} stay. Message a "
                "Bali Zero advisor on WhatsApp so we can assess sponsor eligibility "
                "and the Calling Visa clearance process before you book travel."
            ),
        )

    if purpose == Purpose.OTHER:
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                f"This form ranks visas for {_covered_purposes_sentence()} — you told "
                "us none of those is your reason for coming. The visa follows the "
                "activity, so we will not reverse-engineer one from a duration and a "
                f"budget. We do have your {months}-month horizon and your budget band; "
                "what is missing is what you will actually be doing in Indonesia. "
                "That is one question on WhatsApp, and the official index names far "
                "more categories than a four-step form can ask about."
            ),
        )

    if purpose == Purpose.LONG_TOURISM and months > 6:
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                f"Indonesia's tourism visas max out at ~180 days. For a "
                f"{months}-month stay, we need a non-tourism route (investor, "
                "digital nomad, retirement) that matches what you actually "
                "plan to do here. Tell us on WhatsApp which of those fits and "
                "we will name the visa."
            ),
        )

    if purpose == Purpose.INVESTOR and budget_band == BudgetBand.UNDER_50M:
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                "Investor routes such as the Investor KITAS need a minimum "
                "capital commitment that a budget under IDR 50M does not meet. "
                "Let's talk through what kind of business you want to open on "
                "WhatsApp — there may be a staged approach."
            ),
        )

    impresariat_first: RankedVisa | None = None
    if activity is WorkActivity.SEMI_INDEPENDENT_ARTS_SPORTS and purpose in (
        Purpose.WORK_REMOTE,
        Purpose.WORK_EMPLOYEE,
    ):
        meta = _get_visa_meta()[VisaType.E23_FREELANCE]
        impresariat_first = RankedVisa(
            visa=VisaType.E23_FREELANCE,
            score=1.0,
            reason=_reason(meta, purpose, months, budget_band),
            fit_tags=meta.fit_tags,
        )

    if activity is WorkActivity.FREELANCE_INDONESIAN_CLIENTS:
        # Not a visa category: a foreigner may work in Indonesia only in an employment
        # relationship with the sponsor. visa_check has no PT PMA + E23/E28A product row to
        # offer here, so return no match with the explanation instead of inventing one.
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                "Indonesia has no freelancer visa for invoicing Indonesian clients: a foreigner "
                "may work only in an employment relationship, and the sponsor must be the "
                "employer (Permenkumham 22/2023 Pasal 34(1)(b); UU 13/2003 Pasal 42(4)). The "
                "lawful route is to work through your own PT PMA (E23 or the E28A investor "
                "KITAS). E33G applies only if you are employed by a company established outside "
                "Indonesia (employment agreement required) \u2014 not for freelancers invoicing "
                "clients. Tell us on WhatsApp which applies and we will name the route."
            ),
        )

    if activity is WorkActivity.FREELANCE_FOREIGN_CLIENTS:
        # No freelance route exists: E33G needs an employment agreement with a company
        # established outside Indonesia, and freelance work without it is not offered.
        # Do not fall back to tourism visas (C1/C2) or D2 as work routes.
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=(
                "Indonesia has no freelancer visa, even when all your clients are abroad. "
                "E33G applies only if you are employed by a company established outside "
                "Indonesia (employment agreement required); freelance work without that "
                "agreement is not offered as a route. Tourism visas do not allow work. "
                "Tell us on WhatsApp how you are engaged and we will name the route."
            ),
        )

    ranking = _rank_for_purpose(
        purpose,
        months,
        budget_band,
        max_results=_MAX_RESULTS.get(purpose, 3),
        voa_eligible=nat.is_voa_eligible,
        bvk_eligible=nat.is_bvk_eligible,
        activity=activity,
    )

    if impresariat_first is not None:
        # Semi-independent arts/sports work: Impresariat (Bali Zero as impresario sponsor)
        # leads; the usual ranking stays behind it as alternatives. Its checklist replaces
        # the purpose-level one (see _STEPS_EXCLUSIVE_BY_VISA).
        ranking = [impresariat_first, *ranking][: _MAX_RESULTS.get(purpose, 3)]

    if not ranking:
        return MatchResult(
            ranking=[],
            pre_arrival_steps=[],
            referral_mode=True,
            referral_reason=_explain_empty_ranking(purpose, months, budget_band),
        )

    return MatchResult(
        ranking=ranking,
        pre_arrival_steps=_steps_for(ranking[0].visa, purpose),
        referral_mode=False,
    )
