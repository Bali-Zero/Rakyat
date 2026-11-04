"""In-process, read-only view of the Law projection (``families/``), keyed by product code.

The rows are exactly the ones ``families.importer.build_rows`` turns into the generated
``visa_types`` migrations, so what the Visa Oracle displays and what the database holds come
from the same cards. Loaded once per process from the repository files: no database and no
network in the request path. Display-only; the signed rule pack still decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

from backend.services.visa_engine.families import importer

#: Days per stay-option unit. Only units the cards actually use are mapped; an option in
#: another unit leaves the stay policy to the pack.
_UNIT_DAYS = {"DAY": 1, "YEAR": 365}


@dataclass(frozen=True)
class LawEntry:
    code: str
    family: str
    official_name_id: str
    verification_status: str
    initial_stay_days: int | None
    stay_options: tuple[tuple[int, str], ...] | None
    extendable: bool | None
    max_total_days: int | None
    max_total_months: int | None
    card_sha256: str

    @property
    def verified(self) -> bool:
        return self.verification_status == "VERIFIED"

    def stay_option_days(self) -> tuple[int, ...] | None:
        """Each stay option in days, ascending, or ``None`` when the card lists none (or one
        in a unit this module does not map)."""

        if not self.stay_options or any(unit not in _UNIT_DAYS for _v, unit in self.stay_options):
            return None
        return tuple(sorted({value * _UNIT_DAYS[unit] for value, unit in self.stay_options}))

    def stay_days_range(self) -> tuple[int, int] | None:
        """(shortest, longest) stay in days the Law offers, or ``None`` when it is silent."""

        if self.stay_options:
            if any(unit not in _UNIT_DAYS for _value, unit in self.stay_options):
                return None
            days = [value * _UNIT_DAYS[unit] for value, unit in self.stay_options]
            return min(days), max(days)
        if self.initial_stay_days is not None:
            return self.initial_stay_days, self.initial_stay_days
        return None


def shipped_families() -> tuple[str, ...]:
    """Every family with a committed ``family_<f>.json``, sorted."""

    here = Path(importer.__file__).parent
    return tuple(sorted(p.stem.removeprefix("family_").upper() for p in here.glob("family_*.json")))


def entry_from_row(row: Mapping[str, Any]) -> LawEntry:
    options = row.get("stay_options")
    return LawEntry(
        code=row["code"],
        family=row["family"],
        official_name_id=row["official_name_id"],
        verification_status=row["verification_status"],
        initial_stay_days=row["initial_stay_days"],
        stay_options=(None if options is None else tuple((o["value"], o["unit"]) for o in options)),
        extendable=row["extendable"],
        max_total_days=row["max_total_days"],
        max_total_months=row["max_total_months"],
        card_sha256=row["card_sha256"],
    )


@cache
def law_projection() -> Mapping[str, LawEntry]:
    """Code -> Law entry for every shipped family; built once per process."""

    entries: dict[str, LawEntry] = {}
    for family in shipped_families():
        for row in importer.build_rows(family):
            if row["code"] in entries:
                raise ValueError(f"{row['code']}: present in more than one family")
            entries[row["code"]] = entry_from_row(row)
    return MappingProxyType(entries)


def verified_entry(code: str) -> LawEntry | None:
    """The Law entry for ``code`` when its card is VERIFIED; ``None`` otherwise."""

    entry = law_projection().get(code)
    return entry if entry is not None and entry.verified else None
