"""Publish-time content guards for public article text.

One entry point, ``check_public_article``, called at the two publish hook points
(``_publish_staging_item`` and ``publish_article_internal``). Each field is scanned
on its own; a hit carries the guard id, field, line, matched phrase and rule so the
409 can name exactly what to fix. Article content is public, so the phrase is shown.
Stdlib only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from backend.services.visa_check.e33_article_guard import (
    _find_second_home_predicated_5_10_offenders,
)

E33_SECOND_HOME_5_10_YEARS = "e33_second_home_5_10_years"

GUARD_IDS: tuple[str, ...] = (E33_SECOND_HOME_5_10_YEARS,)

_E33_RULE = (
    "Base E33 is a 5-year first grant, renewable up to a 10-year cumulative maximum "
    "(Permenkumham 22/2023 Pasal 113), never a bare 5-10 range"
)

BLOCK_MESSAGE = "Article blocked by content guard"


@dataclass(frozen=True)
class GuardHit:
    guard: str
    field: str
    line: int
    phrase: str
    rule: str

    def as_detail(self) -> dict[str, object]:
        return {"message": BLOCK_MESSAGE, **asdict(self)}


class ArticleGuardViolation(Exception):
    """Raised by publish paths when ``check_public_article`` returns hits."""

    def __init__(self, hits: list[GuardHit]) -> None:
        self.hits = hits
        first = hits[0]
        super().__init__(f"{BLOCK_MESSAGE}: {first.guard} in {first.field}:{first.line}")

    def detail(self) -> dict[str, object]:
        return self.hits[0].as_detail()

    def log_extra(self) -> dict[str, object]:
        first = self.hits[0]
        return {
            "guard": first.guard,
            "field": first.field,
            "line": first.line,
            "phrase": first.phrase,
        }


def check_public_article(fields: Mapping[str, object]) -> list[GuardHit]:
    """Scan each string field on its own; non-strings are ignored."""
    hits: list[GuardHit] = []
    for field, value in fields.items():
        if not isinstance(value, str) or not value:
            continue
        for start, phrase in _find_second_home_predicated_5_10_offenders(value):
            hits.append(
                GuardHit(
                    guard=E33_SECOND_HOME_5_10_YEARS,
                    field=field,
                    line=value.count("\n", 0, start) + 1,
                    phrase=phrase,
                    rule=_E33_RULE,
                )
            )
    return hits


def enforce_public_article(fields: Mapping[str, object]) -> None:
    hits = check_public_article(fields)
    if hits:
        raise ArticleGuardViolation(hits)
