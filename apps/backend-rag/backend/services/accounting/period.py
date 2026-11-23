"""Calendar-month bounds for the optional accounting workspace filter."""

from calendar import monthrange
from datetime import date
from typing import Any


def month_bounds(month: date) -> tuple[date, date]:
    return month.replace(day=1), month.replace(day=monthrange(month.year, month.month)[1])


def month_condition(column: str, month: date | None, params: list[Any]) -> str:
    """Append date parameters; callers supply a fixed SQL column, never user input."""
    if month is None:
        return ""
    first = len(params) + 1
    params.extend(month_bounds(month))
    return f" AND {column} BETWEEN ${first}::date AND ${first + 1}::date"
