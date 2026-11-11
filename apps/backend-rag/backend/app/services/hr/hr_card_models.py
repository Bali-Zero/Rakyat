"""Staff HR card: strict WRITE models, tolerant READ models, response builders.

Contract: `STAFF_CARD.md` next to this module (S1 no-echo, S2 reads never validate,
S7 real data is accepted). Write models validate input only; the read models and
builders below cannot raise on stored content. Error messages never include a value.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from backend.app.utils.hr_utils import PTKP

_ID_RE = re.compile(r"[0-9 .\-]+")
_SLOT_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


def _text_checker(max_len: int, multiline: bool = False) -> Any:
    def check(value: str) -> str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("text is not valid unicode") from None
        if multiline:  # line breaks are real data in an address; nothing else is allowed
            value = value.replace("\r\n", "\n")
        if any(
            unicodedata.category(ch).startswith("C") and not (multiline and ch == "\n")
            for ch in value
        ):
            raise ValueError("control or non-printable characters are not allowed")
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        if len(value) > max_len:
            raise ValueError(f"value is longer than {max_len} characters")
        return value

    return check


def _id_checker(max_len: int) -> Any:
    text = _text_checker(max_len)

    def check(value: str) -> str:
        value = text(value)
        if not _ID_RE.fullmatch(value):
            raise ValueError("only digits, spaces, dots and dashes are allowed")
        return value

    return check


def _slot_check(value: str) -> str:
    if not _SLOT_RE.fullmatch(value):
        raise ValueError("invalid document slot name")
    return value


def Text(max_len: int, multiline: bool = False) -> Any:
    return Annotated[str, AfterValidator(_text_checker(max_len, multiline))]


def Ident(max_len: int) -> Any:
    return Annotated[str, AfterValidator(_id_checker(max_len))]


_Slot = Annotated[str, AfterValidator(_slot_check)]
_FileId = Annotated[str, AfterValidator(_text_checker(200))]


class StaffContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: Text(100) | None = None
    file_id: _FileId | None = None


class StaffProfilePatch(BaseModel):
    """The documented keys of hr_employees.profile; any other key is a 422."""

    model_config = ConfigDict(extra="forbid")
    legal_name: Text(255) | None = None
    nik: Ident(32) | None = None
    birth_place: Text(100) | None = None
    birth_date: date | None = None
    gender: Text(20) | None = None
    address: Text(500, multiline=True) | None = None
    religion: Text(50) | None = None
    marital_status: Text(50) | None = None
    employer_entity: Text(255) | None = None
    unit: Text(100) | None = None
    contract: StaffContract | None = None
    docs: dict[_Slot, _FileId] | None = None
    end_date: date | None = None


class StaffCardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hire_date: date | None = None
    npwp: Ident(32) | None = None
    ptkp_status: str | None = None
    bpjs_kesehatan_number: Ident(32) | None = None
    bpjs_ketenagakerjaan_number: Ident(32) | None = None
    bank_name: Text(100) | None = None
    bank_account: Ident(100) | None = None
    bank_account_holder: Text(255) | None = None
    is_active: bool | None = None
    profile: StaffProfilePatch | None = None

    @field_validator("ptkp_status")
    @classmethod
    def validate_ptkp(cls, value: str | None) -> str | None:
        if value is not None and value not in PTKP:
            msg = "ptkp_status is not an allowed PTKP code"
            raise ValueError(msg)
        return value


class StaffContractRead(BaseModel):
    """READ shape of the contract: plain types, no validators (S2)."""

    number: str | None = None
    file_id: str | None = None


class StaffCard(BaseModel):
    """The owner's own card: every value in full except the HR free-text `notes`.

    A READ model: plain types only, so FastAPI's response validation can never reject
    what is stored. Write validators live on the *Patch models above.
    """

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


class StaffCardAdmin(StaffCard):
    """HR-admin view: the member card plus the HR free-text `notes`."""

    notes: str | None = None


# ─── read side: tolerant of whatever an older writer left in the jsonb ───

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
    """Coerce the stored profile to what the response models accept; never raises."""
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


def _card_values(card: dict[str, Any]) -> dict[str, Any]:
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
    return values


def member_card(card: dict[str, Any]) -> StaffCard:
    return StaffCard(**_card_values(card))


def admin_card(card: dict[str, Any]) -> StaffCardAdmin:
    return StaffCardAdmin(**_card_values(card), notes=_str_or_none(card.get("notes")))


# ─── 422 bodies that carry no caller-supplied value (S1a) ───

_NON_BODY = {"query", "path", "header", "cookie"}
_SAFE_MESSAGE = "Invalid value."


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in getattr(annotation, "__args__", ()):
        found = _nested_model(arg)
        if found:
            return found
    return None


def safe_loc(loc: Any, model: type[BaseModel] | None, declared_params: frozenset[str]) -> list[Any]:
    """Keep list indexes and DECLARED field names; any other segment becomes "<key>".

    Extra keys and `profile.docs` slot names are caller-controlled, so a segment
    survives only when the model at that depth declares it.
    """
    out: list[Any] = []
    cur = model
    for i, seg in enumerate(loc):
        if isinstance(seg, int) and not isinstance(seg, bool):
            out.append(seg)
        elif i == 0 and seg == "body":
            out.append(seg)
        elif i == 0 and seg in _NON_BODY:
            out.append(seg)
            cur = None
        elif cur is not None and isinstance(seg, str) and seg in cur.model_fields:
            out.append(seg)
            cur = _nested_model(cur.model_fields[seg].annotation)
        elif cur is None and out and out[0] in _NON_BODY and seg in declared_params:
            out.append(seg)
        else:
            out.append("<key>")
            cur = None
    return out


def safe_errors(
    errors: list[Any], model: type[BaseModel] | None, declared_params: frozenset[str]
) -> list[dict[str, Any]]:
    return [
        {
            "type": e["type"] if isinstance(e.get("type"), str) else "value_error",
            "loc": safe_loc(e.get("loc", ()), model, declared_params),
            "msg": _SAFE_MESSAGE,
        }
        for e in errors
    ]
