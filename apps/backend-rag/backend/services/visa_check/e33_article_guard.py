"""E33 Second Home "5-10 years" nearest-predicate rule, shared by CI and publish.

The rule below moved VERBATIM from ``tests/services/visa_check/test_e33_forbidden_claims.py``;
that test re-imports these same objects, so the corpus scan in CI and the publish-time
content guard (``backend.services.content_guards``) can never disagree. Stdlib only.
"""

from __future__ import annotations

import re

# --- Second-Home-predicated "5-10 years" over apps/mouth/src/content ---
#
# S13-510: base E33 is a 5-year first grant with a cumulative cap under
# Permenkumham 22/2023 Pasal 113 (first grant >=5y -> 10y cumulative) — two
# facts, never one bare "5-10 years" range. But the Golden Visa genuinely IS
# a 5-10 year product, and "5-10 Days" appears for unrelated document-prep
# timelines. A bare substring/whole-sentence scan over-matches the Golden
# Visa content (guard family #3, cicatrix-superscar.md) — instead this
# classifies each match by its NEAREST visa-name predicate (within a ±300
# char window, either direction, mirroring the sentence-scoped census that
# found this defect), never by "does the term appear anywhere in the
# sentence/file".

_SECOND_HOME_DURATION_VOCABULARY: dict[str, dict[str, tuple[str, ...]]] = {
    # language: conjunction separators, year units
    "en": {"conjunctions": ("or",), "units": ("year", "years")},
    "it": {"conjunctions": ("o",), "units": ("anno", "anni")},
    "id": {"conjunctions": ("atau",), "units": ("tahun",)},
    "fr": {"conjunctions": ("ou", "à"), "units": ("an", "ans")},
    "ru": {"conjunctions": ("или",), "units": ("год", "года", "лет")},
}
_SYMBOLIC_ALTERNATION_SEPARATORS = ("-", "–", "/")


def _duration_alternation_pattern(
    vocabulary: dict[str, tuple[str, ...]],
) -> str:
    """Match an alternation between 5 and 10 of one language's year unit."""
    conjunctions = "|".join(re.escape(item) for item in vocabulary["conjunctions"])
    units = "|".join(
        re.escape(item) for item in sorted(vocabulary["units"], key=len, reverse=True)
    )
    symbols = "|".join(re.escape(item) for item in _SYMBOLIC_ALTERNATION_SEPARATORS)
    separator = rf"(?:{symbols}|\b(?:{conjunctions})\b)"
    optional_first_unit = rf"(?:(?:\s*-\s*|\s+)(?:{units}))?"
    required_second_unit = rf"(?:\s*-\s*|\s+)(?:{units})\b"
    return rf"\b5{optional_first_unit}\s*{separator}\s*10{required_second_unit}"


_SECOND_HOME_DURATION_RE = re.compile(
    "|".join(
        _duration_alternation_pattern(vocabulary)
        for vocabulary in _SECOND_HOME_DURATION_VOCABULARY.values()
    ),
    re.IGNORECASE,
)
_GOLDEN_VISA_RE = re.compile(
    r"Golden\s+Visa|Visa\s+Emas|Золотая\s+Виза|золотую?\s+визу?\w*",
    re.IGNORECASE,
)
_SECOND_HOME_VISA_RE = re.compile(
    r"Second\s+Home(?:\s+Visa)?|Visa\s+Rumah\s+Kedua|Visa\s+Second\s+Home"
    r"|Rumah\s+Kedua|Виза\s+второго\s+дома"
    r"|второго\s+дома",
    re.IGNORECASE,
)
_PROXIMITY_WINDOW = 300


def _nearest_marker_distance(
    text: str, start: int, end: int, pattern: re.Pattern[str]
) -> int | None:
    """Char-distance from [start, end) to the nearest match of `pattern`
    within `_PROXIMITY_WINDOW` chars either side. None if no match in range."""
    lo = max(0, start - _PROXIMITY_WINDOW)
    hi = min(len(text), end + _PROXIMITY_WINDOW)
    segment = text[lo:hi]
    best: int | None = None
    for marker in pattern.finditer(segment):
        m_start, m_end = marker.start() + lo, marker.end() + lo
        if m_end <= start:
            distance = start - m_end
        elif m_start >= end:
            distance = m_start - end
        else:
            distance = 0
        if best is None or distance < best:
            best = distance
    return best


def _find_second_home_predicated_5_10_offenders(text: str) -> list[tuple[int, str]]:
    """Nearest-predicate scan: flag a '5-10 year(s)/anni/tahun/...' range
    ONLY when the nearest visa-name predicate is Second Home, not Golden
    Visa. A range with no Second-Home predicate within the window at all
    (e.g. blacklist-ban durations, KITAP renewal, unrelated '5-10 days')
    is never flagged."""
    offenders: list[tuple[int, str]] = []
    for match in _SECOND_HOME_DURATION_RE.finditer(text):
        start, end = match.span()
        second_home_dist = _nearest_marker_distance(text, start, end, _SECOND_HOME_VISA_RE)
        if second_home_dist is None:
            continue
        golden_dist = _nearest_marker_distance(text, start, end, _GOLDEN_VISA_RE)
        if golden_dist is not None and golden_dist < second_home_dist:
            continue
        offenders.append((start, match.group(0)))
    return offenders
