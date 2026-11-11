"""Write boundary of the intake client enricher: impossible dates never reach the card.

asyncpg encodes date.min / date.max as -infinity / infinity; a stored non-finite
passport expiry used to break client_expiry_alerts_view for every reader.
"""

from datetime import date

import pytest

from backend.services.intake.client_enricher import ENRICHMENT_MAP, _to_date


@pytest.mark.parametrize(
    "raw",
    ["0001-01-01", "9999-12-31", "01/01/0001", "1899-12-31", "2101-01-01", date.min, date.max],
)
def test_impossible_dates_are_dropped(raw):
    assert _to_date(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2030-06-15", date(2030, 6, 15)), ("15/06/2030", date(2030, 6, 15)), (date(1985, 3, 2), date(1985, 3, 2))],
)
def test_real_dates_pass(raw, expected):
    assert _to_date(raw) == expected


def test_passport_expiry_uses_the_guarded_coercion():
    coercions = {col: fn for _, col, fn in ENRICHMENT_MAP["passport"]}
    assert coercions["passport_expiry"] is _to_date
