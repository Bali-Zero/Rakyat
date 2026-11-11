"""Staff HR card: tolerant READ models.

Contract: `STAFF_CARD.md` next to this module. The read model and builder below
cannot raise on stored content (reads never validate).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

# ─── read side: plain types, no validators; tolerant of whatever an older writer left ───


class StaffContractRead(BaseModel):
    number: str | None = None
    file_id: str | None = None


class StaffCard(BaseModel):
    """The member's own card: every value in full except the HR free-text `notes`."""

    employee_id: int
    full_name: str | None = None
    role: str | None = None
    department: str | None = None
    hire_date: date | None = None
    is_active: bool
    ptkp_status: str | None = None
    base_salary_idr: int | None = None
    npwp: str | None = None
    bpjs_kesehatan_number: str | None = None
    bpjs_ketenagakerjaan_number: str | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    bank_account_holder: str | None = None
    legal_name: str | None = None
    nik: str | None = None
    birth_place: str | None = None
    birth_date: date | None = None
    gender: str | None = None
    address: str | None = None
    religion: str | None = None
    marital_status: str | None = None
    employer_entity: str | None = None
    unit: str | None = None
    contract: StaffContractRead | None = None
    docs: dict[str, str] = Field(default_factory=dict)
    end_date: date | None = None


_PROFILE_STR_KEYS = (
    "legal_name",
    "nik",
    "birth_place",
    "gender",
    "address",
    "religion",
    "marital_status",
    "employer_entity",
    "unit",
)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _date_or_none(value: Any) -> date | None:
    if isinstance(value, datetime):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _profile_view(profile: Any) -> dict[str, Any]:
    """Coerce the stored profile to what the response model accepts; never raises."""
    p = profile if isinstance(profile, dict) else {}
    view: dict[str, Any] = {k: _str_or_none(p.get(k)) for k in _PROFILE_STR_KEYS}
    view["birth_date"] = _date_or_none(p.get("birth_date"))
    view["end_date"] = _date_or_none(p.get("end_date"))
    contract = p.get("contract")
    view["contract"] = (
        {
            "number": _str_or_none(contract.get("number")),
            "file_id": _str_or_none(contract.get("file_id")),
        }
        if isinstance(contract, dict)
        else None
    )
    docs = p.get("docs")
    view["docs"] = (
        {k: v for k, v in docs.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(docs, dict)
        else {}
    )
    return view


def member_card(card: dict[str, Any]) -> StaffCard:
    """Explicit allow-list of what a card exposes; every value coerced, never raising."""
    values: dict[str, Any] = {
        "employee_id": _int_or_none(card.get("id")) or 0,
        "full_name": _str_or_none(card.get("full_name")),
        "role": _str_or_none(card.get("role")),
        "department": _str_or_none(card.get("department")),
        "hire_date": _date_or_none(card.get("hire_date")),
        "is_active": card.get("is_active") is True,
        "ptkp_status": _str_or_none(card.get("ptkp_status")),
        "base_salary_idr": _int_or_none(card.get("base_salary_idr")),
        "npwp": _str_or_none(card.get("npwp")),
        "bpjs_kesehatan_number": _str_or_none(card.get("bpjs_kesehatan_number")),
        "bpjs_ketenagakerjaan_number": _str_or_none(card.get("bpjs_ketenagakerjaan_number")),
        "bank_name": _str_or_none(card.get("bank_name")),
        "bank_account": _str_or_none(card.get("bank_account")),
        "bank_account_holder": _str_or_none(card.get("bank_account_holder")),
    }
    values.update(_profile_view(card.get("profile")))
    return StaffCard(**values)
