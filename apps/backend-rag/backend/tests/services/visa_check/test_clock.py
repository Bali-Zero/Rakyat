"""Unit tests for Visa Clock timeline builder."""

from __future__ import annotations

from datetime import date, timedelta

from backend.services.visa_check.catalogue import VisaType
from backend.services.visa_check.clock import clock_timeline


class TestTimeline:
    def test_produces_5_checkpoints_when_stay_exceeds_60_days(self):
        tl = clock_timeline(visa_type=VisaType.E33G, entry_date=date(2026, 1, 1))
        assert len(tl.checkpoints) == 5
        assert [c.label for c in tl.checkpoints] == ["D-60", "D-30", "D-14", "D-7", "D-1"]

    def test_filters_checkpoints_falling_before_entry_date(self):
        # B1 (30d inclusive -> expiry = entry + 29d): D-60 and D-30 fall before arrival.
        b1 = clock_timeline(visa_type=VisaType.B1, entry_date=date(2026, 10, 18))
        assert [c.label for c in b1.checkpoints] == ["D-14", "D-7", "D-1"]
        assert all(c.at >= b1.entry_date for c in b1.checkpoints)

        # C1 (60d inclusive -> expiry = entry + 59d): D-60 falls 1 day before arrival.
        c1 = clock_timeline(visa_type=VisaType.C1, entry_date=date(2026, 10, 21))
        assert [c.label for c in c1.checkpoints] == ["D-30", "D-14", "D-7", "D-1"]
        assert all(c.at >= c1.entry_date for c in c1.checkpoints)

    def test_b1_expiry_counts_arrival_as_day_1_inclusive(self):
        """Regression P13: B1 (30d) entered 2026-10-18 expires 2026-11-16, not 2026-11-17."""
        tl = clock_timeline(visa_type=VisaType.B1, entry_date=date(2026, 10, 18))
        assert tl.expiry_date == date(2026, 11, 16)

    def test_c1_expiry_is_60_days_inclusive_of_arrival(self):
        """Regression P01: C1 (60d) entered 2026-10-21 expires 2026-12-19, not 2026-12-20."""
        entry = date(2026, 10, 21)
        tl = clock_timeline(visa_type=VisaType.C1, entry_date=entry)
        assert tl.expiry_date == date(2026, 12, 19)
        assert tl.expiry_date == entry + timedelta(days=59)

    def test_e33g_expiry_is_365_days_inclusive_of_arrival(self):
        entry = date(2026, 1, 1)
        tl = clock_timeline(visa_type=VisaType.E33G, entry_date=entry)
        assert tl.expiry_date == entry + timedelta(days=364)

    def test_checkpoint_dates_descend_to_expiry(self):
        tl = clock_timeline(visa_type=VisaType.E28A, entry_date=date(2026, 1, 1))
        # D-60 is 60 days before expiry; D-1 is 1 day before; expiry not included.
        assert tl.checkpoints[0].at == tl.expiry_date - timedelta(days=60)
        assert tl.checkpoints[-1].at == tl.expiry_date - timedelta(days=1)

    def test_c7a_is_not_extendable(self):
        tl = clock_timeline(visa_type=VisaType.C7A, entry_date=date(2026, 1, 1))
        assert tl.extensions_possible == 0
        assert tl.extension_days == 0

    def test_c1_extensions_match_reference(self):
        """Visa C-series reference memory: C1 = 60 + 2×60 = 180 days max."""
        tl = clock_timeline(visa_type=VisaType.C1, entry_date=date(2026, 1, 1))
        assert tl.extensions_possible == 2
        assert tl.extension_days == 60
