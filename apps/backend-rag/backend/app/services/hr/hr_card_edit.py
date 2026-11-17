"""HTTP write side of the staff card (STAFF_CARD.md C11): the body model and its safe errors.

The body wraps `StaffCardPatch` unchanged (no validator is loosened) and adds `clear`, a closed
list of keys to empty. Nothing here renders a submitted value: FastAPI's own 422 body echoes
`input`, so the route turns every validation error into `{field, code, message}` with a field name
from the closed set below and a message from a fixed table.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.services.hr.hr_card_models import StaffCardPatch, StaffProfilePatch

# What may be emptied. NOT hire_date (a record always has one), NOT is_active (a boolean you
# set) and NOT ptkp_status (the column is NOT NULL with a default). `contract_number` is
# profile.contract.number; the document slots and the contract file are the upload PR's.
ClearKey = Literal[
    "npwp",
    "bpjs_kesehatan_number",
    "bpjs_ketenagakerjaan_number",
    "bank_name",
    "bank_account",
    "bank_account_holder",
    "legal_name",
    "nik",
    "birth_place",
    "birth_date",
    "gender",
    "address",
    "religion",
    "marital_status",
    "employer_entity",
    "unit",
    "position",
    "on_payroll",
    "end_date",
    "contract_number",
]

_CARD_KEYS = frozenset(StaffCardPatch.model_fields) - {"profile"}


_PROFILE_KEYS = frozenset(StaffProfilePatch.model_fields) - {"contract", "docs"}
_FIELD_NAMES = _CARD_KEYS | _PROFILE_KEYS | {
    "contract_number",
    "clear",
    "docs",
    "contract",
    "whatsapp",
}
_LOC_WORDS = _FIELD_NAMES | {"card", "profile", "contract", "number", "file_id", "body"}


# The staff member's WhatsApp number (DAI-153): `team_members.whatsapp`, not an hr_employees
# column, so it sits beside `card` and never enters StaffCardPatch (the import cannot set it).
# Separators a person types are dropped; what is left must be 62 + digits, 10 to 15 in all.
_WA_SEPARATORS = re.compile(r"[ .\-()]")
_WA_SHAPE = re.compile(r"62[0-9]{8,13}", re.ASCII)
WHATSAPP_MESSAGE = "Must start with 62 and have 10 to 15 digits"


def normalise_whatsapp(value: str) -> str | None:
    """The stored form of a typed number: digits only, `62` first. None = empty (unchanged).

    Raises ValueError with a FIXED message: the value never reaches an error or a log."""
    text = _WA_SEPARATORS.sub("", value.strip())
    if text.startswith("+"):
        text = text[1:]
    if not text:
        return None
    if not _WA_SHAPE.fullmatch(text):
        raise ValueError(WHATSAPP_MESSAGE)
    return text


def mask_whatsapp(value: object) -> str | None:
    """`62•••••1234`: the only form of a stored number an HR response carries. None when there is
    no usable number (a non-string, or fewer than 8 digits, which would show too much)."""
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    if len(digits) < 8:
        return None
    return f"{digits[:2]}\u2022\u2022\u2022\u2022\u2022{digits[-4:]}"


class CardEditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card: StaffCardPatch = Field(default_factory=StaffCardPatch)
    clear: list[ClearKey] = Field(default_factory=list, max_length=64)
    # Empty or null = unchanged, never a delete (there is no `clear` key for it).
    whatsapp: str | None = Field(default=None, max_length=32)

    @field_validator("whatsapp")
    @classmethod
    def _whatsapp(cls, value: str | None) -> str | None:
        return None if value is None else normalise_whatsapp(value)

    def _set_keys(self) -> list[str]:
        patch = self.card
        keys = sorted(patch.model_fields_set - {"profile"})
        profile = patch.profile
        keys += sorted(profile.model_fields_set - {"contract", "docs"})
        if "number" in profile.contract.model_fields_set:
            keys.append("contract_number")
        if self.whatsapp is not None:
            keys.append("whatsapp")
        return keys

    @model_validator(mode="after")
    def _check(self) -> CardEditBody:
        patch = self.card
        for name in patch.model_fields_set - {"profile"}:
            if getattr(patch, name) is None:
                raise ValueError("a null sets nothing: list the key under clear to empty it")
        profile = patch.profile
        for name in profile.model_fields_set - {"contract", "docs"}:
            if getattr(profile, name) is None:
                raise ValueError("a null sets nothing: list the key under clear to empty it")
        if "docs" in profile.model_fields_set or "file_id" in profile.contract.model_fields_set:
            raise ValueError("documents are not edited here")
        if profile.contract.model_fields_set - {"number", "file_id"}:
            raise ValueError("unknown contract key")
        if "number" in profile.contract.model_fields_set and profile.contract.number is None:
            raise ValueError("a null sets nothing: list contract_number under clear")
        if len(set(self.clear)) != len(self.clear):
            raise ValueError("a key is listed twice under clear")
        set_keys = self._set_keys()
        if set(set_keys) & set(self.clear):
            raise ValueError("a key may not be both set and cleared")
        if not set_keys and not self.clear:
            raise ValueError("nothing to change")
        return self

    def to_write(self) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str]]:
        """(fields, profile patch, cleared keys, set key NAMES) for `HRService.edit_card`."""
        fields = self.card.model_dump(exclude={"profile"}, exclude_none=True)
        profile = self.card.profile.model_dump(mode="json", exclude_none=True, exclude_unset=True)
        profile.pop("docs", None)
        if not profile.get("contract"):
            profile.pop("contract", None)
        return fields, profile, sorted(self.clear), self._set_keys()


_MESSAGES = {
    "extra_forbidden": "Not an editable field",
    "missing": "Required",
    "string_type": "Must be text",
    "bool_type": "Must be true or false",
    "bool_parsing": "Must be true or false",
    "date_type": "Not a valid date",
    "date_parsing": "Not a valid date",
    "date_from_datetime_parsing": "Not a valid date",
    "literal_error": "Not an allowed value",
    "json_invalid": "The body is not valid JSON",
    "json_type": "The body is not valid JSON",
    "model_attributes_type": "The body must be an object",
    "dict_type": "Must be an object",
    "list_type": "Must be a list",
    "uuid_parsing": "Not a valid id",
    "too_long": "Too long",
}


def safe_validation_errors(errors: Iterable[Any]) -> list[dict[str, str]]:
    """Reduce pydantic/FastAPI errors to `{field, code, message}`; never `input`, `ctx` or a key
    the caller invented. One entry per field (a union reports once per branch)."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for err in errors:
        loc = [p for p in err.get("loc", ()) if isinstance(p, str | int)]
        words = []
        for part in loc:
            if isinstance(part, str) and part in _LOC_WORDS:
                words.append(part)
            elif isinstance(part, int):
                continue
            else:
                break  # a caller-invented key or a validator label: stop, never echo it
        field = ""
        if words[-2:] == ["contract", "number"]:
            field = "contract_number"
        elif words and words[-1] in _FIELD_NAMES:
            field = words[-1]
        code = str(err.get("type", "invalid"))
        if code == "value_error":
            raw = str(err.get("msg", ""))
            message = raw.removeprefix("Value error, ")
            code = "invalid"
        else:
            message = _MESSAGES.get(code, "Invalid value")
        if field and field in seen:
            continue
        seen.add(field)
        out.append({"field": field, "code": code, "message": message})
    return out
