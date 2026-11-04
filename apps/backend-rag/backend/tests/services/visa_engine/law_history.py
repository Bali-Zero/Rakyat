"""History helpers for families whose shipped migration was superseded by a later bundle.

Applied migrations are immutable (the production ledger pins their checksums), so a test of
an applied family migration compares the database against the rows THAT FILE carries, decoded
from its importer-generated literals, not against today's cards. Owner-ruling additions to a
card are reversible here, so the pre-ruling content can still be checked against the original
extraction pins.
"""

from __future__ import annotations

import json
from typing import Any

from backend.services.visa_engine.families import importer

#: Card fields added by owner rulings, kept apart from the page extraction.
RULING_KEYS = ("owner_rulings", "eligible_nationalities", "regulation_verbatim")


def pre_ruling_card(card: dict) -> dict:
    """The card as extracted, before owner-ruling additions and resolved TO_VERIFY items."""

    restored = sorted(
        (
            item
            for ruling in card.get("owner_rulings", [])
            for item in ruling.get("resolves_to_verify", [])
        ),
        key=lambda item: item["index"],
    )
    original = {key: value for key, value in card.items() if key not in RULING_KEYS}
    if restored:
        to_verify = list(original["to_verify"])
        for item in restored:
            to_verify.insert(item["index"], item["item"])
        original["to_verify"] = to_verify
    return original


def decode_literal(literal: str) -> Any:
    """Inverse of ``importer.literal`` for the forms the importer emits."""

    if literal == "NULL":
        return None
    if literal in ("TRUE", "FALSE"):
        return literal == "TRUE"
    if literal.startswith("$"):
        tag = literal[: literal.index("$", 1) + 1]
        return json.loads(literal[len(tag) : literal.rindex(tag)])
    if literal.startswith("'"):
        return literal[1 : literal.rindex("'")].replace("''", "'")
    return int(literal)


def applied_rows(filename: str) -> list[dict]:
    """Rows of an applied importer-generated migration, decoded, sorted by code."""

    text = (importer.MIGRATIONS_DIR / filename).read_text()
    rows = importer.literals_in_migration(text)
    return [
        {column: decode_literal(value) for column, value in rows[code].items()}
        for code in sorted(rows)
    ]


def rows_as_of(family: str, number: int) -> list[dict]:
    """The family's rows as projected by its newest migration numbered ``<= number``."""

    history = [
        name
        for name in importer.FAMILY_MIGRATIONS[family.upper()]
        if int(name.split("_", 1)[0]) <= number
    ]
    if not history:
        raise ValueError(f"{family}: no migration up to {number}")
    return [row for row in applied_rows(history[-1]) if row["family"] == family.upper()]
