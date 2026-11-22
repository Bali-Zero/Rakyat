"""/visa-oracle/recommend never offers candidates on a nationality it could not read.

The route takes the nationality as free text. A spelling the classifier did not resolve
skipped both nationality gates and reached SUPPORTED_CANDIDATES. A Calling Visa country
or Indonesia written under one of its reviewed names now gets its gate, and a text that
is not read in full gets NEEDS_INPUT, after the two gates. Expectations are written by
hand.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_database_pool
from backend.app.routers.visa_oracle import _determine_recommend_state, router

SCORED = [{"visa_name": "Example visa", "price": "1.000.000 IDR", "score": 2.5}]

NEEDS_INPUT = ("NEEDS_INPUT", ["nationality_not_recognized"])
ADVISOR = ("HUMAN_REVIEW_REQUIRED", ["calling_visa_advisor_referral"])
INDONESIAN = ("NO_SUPPORTED_PATH", ["indonesian_citizen_no_visa_required"])
CANDIDATES = ("SUPPORTED_CANDIDATES", ["nationality_not_evaluated"])

NOT_READ = [
    "Narnia",
    "Nigerija",
    "Cor\u00e9e du Nord",
    "Niger",
    ",,",
    # a part is read, another is not
    "Italy and Narnia",
    "Korean / USA",
    "USA / It\u0430ly",
    "USA and United Kingdom of Great Britain and Northern Ireland",
    # round 3: a formal form that is not a row
    "Democratic People's Republic of Korean",
    "Republic of Chinese",
    "Federal Republic of China",
    # which Korea is not said
    "Korean",
    # a comma never joins two names
    "USA, Narnia",
    "Guinea, French",
    "Hong Kong, China",
    "Italy, USA",
    "Indonesia, Xyz",
    "Nigeria, Italian",
    "ID,XYZ",
    # three letters that are no country
    "WNA",
    "XYZ",
    "KSA",
    "ROK",
    "DPR",
    # no conjunction: the words in front of "Republic of" join nothing
    "Nigeria Republic of Italy",
    "Nigeria or Republic of Italy",
]

CALLING_VISA = [
    "Federal Republic of Nigeria",
    "Nig\u00e9ria",
    "Korea, North",
    "Korea Utara",
    "DPR Korea",
    "Democratic People's Republic of Korea",
    "State of Israel",
    "Islamic Emirate of Afghanistan",
    "Nigeria and USA",
    "NG,,US",
    "Nigeria and Republic of Italy",
    "Republic of Italy and Nigeria",
    "Nigeria and the Federal Republic of Germany",
    # the gate fires on the part that was read, before the unread answer
    "Nigeria and Narnia",
    "Nigeria / Xyz",
]

INDONESIAN_CITIZEN = [
    "Republic of Indonesia",
    "Republik Indonesia",
    "Warga Negara Indonesia",
    "WNI",
    "INA",
    "the Indonesia",
    "Indonesia and Republic of France",
    "Republic of France and Indonesia",
    "Indonesia / Narnia",
]

READ_IN_FULL = [
    "US",
    "USA",
    "Italian",
    "United States of America",
    "IT, FR",
    "The Netherlands",
    "Italy / USA",
    "Republic of China",
    "Republic of Korea",
    "T\u00fcrkiye",
]


def _state(nationality: str) -> tuple[str, list[str]]:
    return _determine_recommend_state(visas=SCORED, missing_facts=[], nationality=nationality)


@pytest.mark.parametrize("nationality", NOT_READ)
def test_a_nationality_that_is_not_read_in_full_gets_no_candidates(nationality: str) -> None:
    assert _state(nationality) == NEEDS_INPUT


@pytest.mark.parametrize("nationality", CALLING_VISA)
def test_a_calling_visa_country_written_as_text_goes_to_an_advisor(nationality: str) -> None:
    assert _state(nationality) == ADVISOR


@pytest.mark.parametrize("nationality", INDONESIAN_CITIZEN)
def test_an_indonesian_citizen_written_as_text_has_no_visa_path(nationality: str) -> None:
    assert _state(nationality) == INDONESIAN


@pytest.mark.parametrize("nationality", READ_IN_FULL)
def test_a_nationality_that_is_read_in_full_is_scored_as_before(nationality: str) -> None:
    assert _state(nationality) == CANDIDATES


@pytest.fixture
def client() -> TestClient:
    application = FastAPI()
    application.include_router(router, prefix="/api/v1")
    application.dependency_overrides[get_database_pool] = lambda: MagicMock()
    return TestClient(application, raise_server_exceptions=False)


def _recommend(client: TestClient, nationality: str) -> dict[str, Any]:
    with patch("backend.app.routers.visa_oracle._persist_session_create"):
        response = client.post(
            "/api/v1/visa-oracle/recommend",
            json={
                "nationality": nationality,
                "purpose": "visit",
                "duration": "short",
                "family": "no",
            },
        )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    ("nationality", "expected"),
    [
        ("Federal Republic of Nigeria", ADVISOR),
        ("Korea, North", ADVISOR),
        ("Democratic People's Republic of Korea", ADVISOR),
        ("Nigeria and Republic of Italy", ADVISOR),
        ("Republic of Indonesia", INDONESIAN),
        ("INA", INDONESIAN),
        ("Narnia", NEEDS_INPUT),
        ("Democratic People's Republic of Korean", NEEDS_INPUT),
        ("Guinea, French", NEEDS_INPUT),
        ("Korean / USA", NEEDS_INPUT),
        ("Italy and Narnia", NEEDS_INPUT),
        ("WNA", NEEDS_INPUT),
    ],
)
def test_the_route_offers_no_visa_on_those_nationalities(
    client: TestClient, nationality: str, expected: tuple[str, list[str]]
) -> None:
    answer = _recommend(client, nationality)

    assert (answer["state"], answer["review_reasons"]) == expected
    assert answer["visas"] == []


@pytest.mark.parametrize("nationality", ["US", "Italy / USA", "Republic of China"])
def test_the_route_still_answers_what_it_reads_as_before(
    client: TestClient, nationality: str
) -> None:
    answer = _recommend(client, nationality)

    assert "nationality_not_recognized" not in answer["review_reasons"]
    assert answer["state"] in {"SUPPORTED_CANDIDATES", "TEMPORARILY_UNAVAILABLE", "NEEDS_INPUT"}
    if answer["state"] == "SUPPORTED_CANDIDATES":
        assert answer["visas"]
