"""Staff HR card: strict WRITE validators (import only) and tolerant READ models.

Contract: `STAFF_CARD.md` next to this module. The write models validate what the
import script feeds them; the read model and builder below cannot raise on stored
content (reads never validate). The CUSTOM validators here raise messages that never
include the input. Pydantic's own `str(ValidationError)` DOES render the input, so no caller
may render a `ValidationError`: the import worker reads `errors(include_input=False)` and only `loc[0]` (a deeper `loc`
can hold a caller-supplied dict key) and maps it to a closed reason code.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
)

from backend.app.utils.hr_utils import PTKP

_SLOT_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_SEPARATORS = re.compile(r"[ .\-]")


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


def _id_checker(max_len: int, shape: re.Pattern[str], strip: re.Pattern[str]) -> Any:
    """Validate on the normalised form (separators removed); store the trimmed value as given."""
    text = _text_checker(max_len)

    def check(value: str) -> str:
        value = text(value)
        if not shape.fullmatch(strip.sub("", value)):
            raise ValueError("value does not have the shape of this identifier")
        return value

    return check


_NO_BANK_DOTS = re.compile(r"[ \-]")

_NIK = _id_checker(32, re.compile(r"[0-9]{16}"), _SEPARATORS)
_NPWP = _id_checker(32, re.compile(r"[0-9]{15,16}"), _SEPARATORS)
_BPJS_KES = _id_checker(32, re.compile(r"[0-9]{13}"), _SEPARATORS)
_BPJS_KET = _id_checker(32, re.compile(r"[A-Za-z0-9]{11}"), _SEPARATORS)
_BANK_ACCOUNT = _id_checker(100, re.compile(r"[0-9]{6,20}"), _NO_BANK_DOTS)


def _slot_check(value: str) -> str:
    if not _SLOT_RE.fullmatch(value):
        raise ValueError("invalid document slot name")
    return value


def Text(max_len: int, multiline: bool = False) -> Any:
    return Annotated[str, AfterValidator(_text_checker(max_len, multiline))]


_Nik = Annotated[str, AfterValidator(_NIK)]
_Npwp = Annotated[str, AfterValidator(_NPWP)]
_BpjsKes = Annotated[str, AfterValidator(_BPJS_KES)]
_BpjsKet = Annotated[str, AfterValidator(_BPJS_KET)]
_BankAccount = Annotated[str, AfterValidator(_BANK_ACCOUNT)]
_Slot = Annotated[str, AfterValidator(_slot_check)]
_FileId = Annotated[str, AfterValidator(_text_checker(200))]


class StaffContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: Text(100) | None = None
    file_id: _FileId | None = None


class StaffProfilePatch(BaseModel):
    """The documented keys of hr_employees.profile; any other key is rejected.

    `contract` and `docs` may be absent but never an explicit null (no silent drop).
    """

    model_config = ConfigDict(extra="forbid")
    legal_name: Text(255) | None = None
    nik: _Nik | None = None
    birth_place: Text(100) | None = None
    birth_date: date | None = None
    gender: Text(20) | None = None
    address: Text(500, multiline=True) | None = None
    religion: Text(50) | None = None
    marital_status: Text(50) | None = None
    employer_entity: Text(255) | None = None
    unit: Text(100) | None = None
    position: Text(100) | None = None
    on_payroll: StrictBool | None = None
    contract: StaffContract = Field(default_factory=StaffContract)
    docs: dict[_Slot, _FileId] = Field(default_factory=dict)
    end_date: date | None = None


class StaffCardPatch(BaseModel):
    """What one import card may set. `profile` may be absent, never an explicit null."""

    model_config = ConfigDict(extra="forbid")
    hire_date: date | None = None
    npwp: _Npwp | None = None
    ptkp_status: str | None = None
    bpjs_kesehatan_number: _BpjsKes | None = None
    bpjs_ketenagakerjaan_number: _BpjsKet | None = None
    bank_name: Text(100) | None = None
    bank_account: _BankAccount | None = None
    bank_account_holder: Text(255) | None = None
    is_active: StrictBool | None = None
    profile: StaffProfilePatch = Field(default_factory=StaffProfilePatch)

    @field_validator("ptkp_status")
    @classmethod
    def validate_ptkp(cls, value: str | None) -> str | None:
        if value is not None and value not in PTKP:
            msg = "ptkp_status is not an allowed PTKP code"
            raise ValueError(msg)
        return value


# ─── read side: plain types, no validators; tolerant of whatever an older writer left ───


class StaffContractRead(BaseModel):
    """`contract.file_id` is NOT here: a stored slot value is untrusted free text (C13), and only
    the `documents` map of the card says whether a paper is on file and which file is verified."""

    number: str | None = None


DOCUMENT_SLOTS = ("ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract")


class DocumentState(BaseModel):
    """One paper: `present` is true iff the stored slot value is a non-empty string; `file_id` is
    that value only when it is VERIFIED (the door wrote it and `doc_proof` says so), else null."""

    model_config = ConfigDict(extra="forbid")

    present: bool
    file_id: str | None


class DocumentsMap(BaseModel):
    """Closed: exactly these six REQUIRED keys, never a stored `docs` key."""

    model_config = ConfigDict(extra="forbid")

    ktp: DocumentState
    kk: DocumentState
    npwp: DocumentState
    bpjs_kes: DocumentState
    bpjs_tk: DocumentState
    contract: DocumentState


class DocumentChangeResult(BaseModel):
    """The answer of the C13 door: which slot changed and the six-key map after the write
    (always present: an uncertain commit that cannot be reconciled is an error, never a null).
    Never a stored value."""

    slot: str
    documents: DocumentsMap


def _slot_value(profile: dict[str, Any], slot: str) -> Any:
    if slot == "contract":
        contract = profile.get("contract")
        return contract.get("file_id") if isinstance(contract, dict) else None
    docs = profile.get("docs")
    return docs.get(slot) if isinstance(docs, dict) else None


def project_documents(profile: Any) -> dict[str, dict[str, Any]]:
    """The ONE projection (STAFF_CARD.md C13): the six-key `documents` map of a stored profile.

    Pure: a dict in, a dict out, no I/O, so it cannot fail open. A slot is VERIFIED iff its value is
    a JSON string AND `doc_proof.<slot>` is an object whose `id` is a JSON string equal to it; only
    then is the value echoed as `file_id`. An unverified value is reported as `present` and never
    shown. A non-string or blank value is absent and is not echoed either."""
    p = profile if isinstance(profile, dict) else {}
    proofs = p.get("doc_proof") if isinstance(p.get("doc_proof"), dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for slot in DOCUMENT_SLOTS:
        value = _slot_value(p, slot)
        present = isinstance(value, str) and bool(value.strip())
        proof = proofs.get(slot)
        verified = (
            present
            and isinstance(proof, dict)
            and isinstance(proof.get("id"), str)
            and proof["id"] == value
        )
        out[slot] = {"present": bool(present), "file_id": value if verified else None}
    return out


# ASCII only, lowercase only (stored files are lowercase `.jpg`), slug starts alphanumeric so no
# leading dot. Matched with fullmatch: `$` would also accept a trailing newline.
_AVATAR_PATH = re.compile(r"/static/team/[a-z0-9][a-z0-9_-]*\.(?:jpe?g|png|webp)", re.ASCII)


def avatar_path(value: Any) -> str | None:
    """`team_members.avatar` as the portrait path the UI may render, else None.

    Only the local staff-photo shape (`/static/team/<slug>.jpg`) leaves the backend: a data URI,
    a remote URL or any other string is not a portrait and never reaches an HR response."""
    return value if isinstance(value, str) and _AVATAR_PATH.fullmatch(value) else None


class StaffCard(BaseModel):
    """The member's own card: every value in full except the HR free-text `notes`."""

    employee_id: int
    full_name: str | None = None
    avatar_url: str | None = None
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
    position: str | None = None
    on_payroll: bool = True
    contract: StaffContractRead | None = None
    documents: DocumentsMap
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
    "position",
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
    # Only a stored JSON `false` takes a person off payroll (STAFF_CARD.md C12); absent or any
    # other stored value reads as the default, on payroll.
    view["on_payroll"] = p.get("on_payroll") is not False
    view["birth_date"] = _date_or_none(p.get("birth_date"))
    view["end_date"] = _date_or_none(p.get("end_date"))
    contract = p.get("contract")
    view["contract"] = (
        {"number": _str_or_none(contract.get("number"))} if isinstance(contract, dict) else None
    )
    # R4/N6: raw `docs`, `contract.file_id` and `doc_proof` never leave here; the closed
    # `documents` map replaces them in EVERY response that carries a card.
    view["documents"] = project_documents(p)
    return view


def member_card(card: dict[str, Any]) -> StaffCard:
    """Explicit allow-list of what a card exposes; every value coerced, never raising."""
    values: dict[str, Any] = {
        "employee_id": _int_or_none(card.get("id")) or 0,
        "full_name": _str_or_none(card.get("full_name")),
        "avatar_url": avatar_path(card.get("avatar")),
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
