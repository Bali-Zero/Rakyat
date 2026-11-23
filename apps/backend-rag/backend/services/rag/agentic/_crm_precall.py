"""What the streaming CRM pre-call may hand the model.

The pre-call asks the CRM tool one question before the reasoning loop starts. Its answer
ends in exactly one of three outcomes:

- DATA: rows or counts of the shape that query type returns. The caller injects them as
  it always did and marks the answer as resting on a trusted tool.
- SCOPED_ZERO: a valid empty list. Nothing is injected as data and nothing is trusted;
  a fixed note tells the model that the lookup found nothing within the signed-in user's
  scope, and that CRM facts in the answer must come from a tool result of this turn.
- NOTHING: a refusal, an error object, anything malformed. The model is told nothing.

The note travels in the text of every message sent to the model during the turn (see
``with_crm_scope_note``): a provider fallback that receives only the current message
must still receive it.
"""

from __future__ import annotations

import json
from typing import Any

DATA = "data"
SCOPED_ZERO = "scoped_zero"
NOTHING = "nothing"

# The fields each list query returns, as the tool selects them. A row with any other set
# of keys is not a row of that query. Pinned to the tool's real output by a test.
ROW_FIELDS: dict[str, frozenset[str]] = {
    "search_clients": frozenset({"id", "full_name", "email", "nationality", "status", "phone"}),
    "recent_clients": frozenset(
        {"id", "full_name", "email", "nationality", "status", "created_at"}
    ),
    "expiring_documents": frozenset(
        {
            "client_id",
            "client_name",
            "entity_type",
            "entity_name",
            "document_type",
            "expiry_date",
            "days_until_expiry",
        }
    ),
    "practice_stats": frozenset({"service_type", "count", "on_process", "completed"}),
}
ROW_COUNTS: dict[str, tuple[str, ...]] = {"practice_stats": ("count", "on_process", "completed")}
CLIENT_COUNTS = frozenset({"active_clients", "leads", "inactive", "total"})
PRACTICE_COUNTS = frozenset(
    {
        "on_process",
        "completed",
        "sending_invoice",
        "waiting_documents",
        "inquiry",
        "cancelled",
        "total",
    }
)

CRM_NOTE_CONSTRAINT = (
    "Any CRM client, practice or count in your answer must come from a crm_query result "
    "of this turn. Do not take one from earlier turns or from retrieved documents."
)
ZERO_NOTES: dict[str, str] = {
    "practice_stats": "The CRM holds no practices within the signed-in user's scope.",
    # The pre-call asks with the tool's default window of 30 days.
    "expiring_documents": (
        "The CRM holds no passport, visa or document expiring in the next 30 days within "
        "the signed-in user's scope."
    ),
    # The pre-call hands the whole question to the tool as the search term, so an empty
    # answer says nothing about whether a client exists.
    "search_clients": (
        "The automatic CRM lookup matched no client for this question as typed; that is "
        "not a search result. To look a client up, call crm_query with the name: results "
        "are limited to the signed-in user's scope."
    ),
}


def _is_count(value: Any) -> bool:
    """A count is a whole number, zero or more. A JSON boolean is not one."""
    return type(value) is int and value >= 0


def _is_counts(value: Any, names: frozenset[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == names
        and all(_is_count(count) for count in value.values())
    )


def judge_crm_precall(query_type: str, result: Any) -> tuple[str, str]:
    """``(outcome, note)`` for what the CRM tool answered to a pre-call of ``query_type``.

    ``note`` is the scoped-zero note for SCOPED_ZERO and empty otherwise.
    """
    if not isinstance(result, str):
        return NOTHING, ""
    try:
        parsed = json.loads(result)
    except ValueError:
        return NOTHING, ""

    if query_type == "client_stats":
        if (
            isinstance(parsed, dict)
            and set(parsed) == {"clients", "practices"}
            and _is_counts(parsed["clients"], CLIENT_COUNTS)
            and _is_counts(parsed["practices"], PRACTICE_COUNTS)
        ):
            return DATA, ""
        return NOTHING, ""

    fields = ROW_FIELDS.get(query_type)
    if fields is None or not isinstance(parsed, list):
        return NOTHING, ""
    if not parsed:
        first_sentence = ZERO_NOTES.get(query_type)
        if first_sentence is None:
            return NOTHING, ""
        return SCOPED_ZERO, f"{first_sentence} {CRM_NOTE_CONSTRAINT}"
    counts = ROW_COUNTS.get(query_type, ())
    for row in parsed:
        if not isinstance(row, dict) or set(row) != fields:
            return NOTHING, ""
        if not all(_is_count(row[name]) for name in counts):
            return NOTHING, ""
    return DATA, ""


def with_crm_scope_note(state: Any, message: str) -> str:
    """``message`` with the turn's scoped-zero note appended, or ``message`` itself.

    Every message the streaming loop sends to the model goes through here. A turn with
    no note sends exactly what it would have sent.
    """
    note = getattr(state, "crm_scope_note", "")
    if not note:
        return message
    return f"{message}\n\n[CRM NOTE] {note}"
