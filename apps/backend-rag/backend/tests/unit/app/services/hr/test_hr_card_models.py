"""Staff card write validators (import only) and the tolerant read model."""

import pytest
from pydantic import ValidationError

from backend.app.services.hr.hr_card_models import StaffCardPatch, StaffProfilePatch, member_card


def _ok(**kw):
    return StaffCardPatch.model_validate(kw)


def _bad(**kw):
    with pytest.raises(ValidationError) as exc:
        StaffCardPatch.model_validate(kw)
    return exc.value


def _d(n: int, ch: str = "7") -> str:
    """A synthetic run of n identical digits (built, so no literal looks like a secret)."""
    return ch * n


def _spaced(n: int, sep: str) -> str:
    digits = _d(n)
    return sep.join(digits[i : i + 4] for i in range(0, n, 4))


@pytest.mark.parametrize("nik", [_d(16), _spaced(16, " "), _spaced(16, ".")])
def test_nik_is_sixteen_digits_after_separators_and_is_stored_as_given(nik):
    assert _ok(profile={"nik": f" {nik} "}).profile.nik == nik


@pytest.mark.parametrize("nik", [_d(15), _d(17), _d(14) + "AB", ""])
def test_nik_of_any_other_shape_is_rejected(nik):
    _bad(profile={"nik": nik})


@pytest.mark.parametrize("npwp", [_spaced(16, "."), _d(15), _d(16)])
def test_npwp_is_fifteen_or_sixteen_digits(npwp):
    assert _ok(npwp=npwp).npwp == npwp


@pytest.mark.parametrize("npwp", [_d(14), _d(17), _d(15) + "X"])
def test_npwp_of_any_other_shape_is_rejected(npwp):
    _bad(npwp=npwp)


def test_bpjs_kesehatan_is_thirteen_digits():
    assert _ok(bpjs_kesehatan_number=_d(13)).bpjs_kesehatan_number
    _bad(bpjs_kesehatan_number=_d(12))
    _bad(bpjs_kesehatan_number=_d(14))
    _bad(bpjs_kesehatan_number=_d(12) + "A")


def test_bpjs_ketenagakerjaan_is_eleven_alphanumerics():
    assert _ok(bpjs_ketenagakerjaan_number=_spaced(11, "-")).bpjs_ketenagakerjaan_number
    assert _ok(bpjs_ketenagakerjaan_number="AB" + _d(9))
    _bad(bpjs_ketenagakerjaan_number=_d(10))
    _bad(bpjs_ketenagakerjaan_number=_d(12))
    _bad(bpjs_ketenagakerjaan_number=_d(10) + "!")


def test_bank_account_is_six_to_twenty_digits_after_spaces_and_dashes():
    spaced = _spaced(10, " ")
    assert _ok(bank_account=spaced).bank_account == spaced
    assert _ok(bank_account=_d(6))
    _bad(bank_account=_d(5))
    _bad(bank_account=_d(21))
    _bad(bank_account=_spaced(10, "."))  # dots are not a bank-account separator
    _bad(bank_account=_d(8) + "AB")


@pytest.mark.parametrize("bad", [None, [], False, 0, "", "x"])
def test_profile_must_be_an_object_and_null_is_rejected(bad):
    _bad(profile=bad)


@pytest.mark.parametrize("bad", [None, [], False, 0, "", "x"])
def test_docs_and_contract_must_be_objects_and_null_is_rejected(bad):
    with pytest.raises(ValidationError):
        StaffProfilePatch.model_validate({"docs": bad})
    with pytest.raises(ValidationError):
        StaffProfilePatch.model_validate({"contract": bad})


def test_absent_profile_docs_and_contract_are_fine_and_not_dumped():
    patch = _ok(bank_name="Fake Bank")
    dumped = patch.profile.model_dump(mode="json", exclude_none=True, exclude_unset=True)
    assert dumped == {}
    assert patch.model_dump(exclude={"profile"}, exclude_none=True) == {"bank_name": "Fake Bank"}


def test_unknown_keys_and_odd_flags_are_rejected():
    _bad(salary=1)
    _bad(profile={"salary": 1})
    _bad(is_active="true")
    _bad(ptkp_status="ZZ/9")


def test_the_custom_validator_messages_carry_no_input():
    for kw in (
        {"profile": {"nik": "SECRETVALUE-123"}},
        {"npwp": "SECRETVALUE-123"},
        {"bank_account": "SECRETVALUE-123"},
        {"profile": {"docs": {"SECRETVALUE!": "f"}}},
        {"bank_name": "\x00SECRETVALUE"},
    ):
        err = _bad(**kw)
        errors = err.errors(include_input=False, include_context=False)
        assert "SECRETVALUE" not in str([e["msg"] for e in errors])
        assert all("SECRETVALUE" not in str(e["loc"][:1]) for e in errors)  # top level only:
        # a deeper `loc` can hold a caller-supplied dict key, so only loc[0] is ever read


def test_pydantic_renders_the_input_in_str_of_a_validation_error_so_nobody_may_render_it():
    err = _bad(profile={"nik": "SECRETVALUE-123"})
    assert "SECRETVALUE" in str(err)  # documents why the import worker never renders one


def test_the_read_model_never_raises_on_stored_content():
    card = member_card(
        {"id": 1, "is_active": 5, "profile": {"docs": ["x"], "contract": "c", "nik": 7}}
    )
    assert (
        card.docs == {} and card.contract is None and card.nik is None and card.is_active is False
    )
    assert member_card({"id": None, "profile": "x"}).employee_id == 0


@pytest.mark.parametrize("value", [True, False])
def test_on_payroll_accepts_a_strict_boolean(value):
    assert StaffProfilePatch(on_payroll=value).on_payroll is value


@pytest.mark.parametrize("bad", ["yes", "false", 1, 0, "true", []])
def test_on_payroll_rejects_anything_but_a_boolean(bad):
    with pytest.raises(ValidationError):
        StaffProfilePatch(on_payroll=bad)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({}, True),
        ({"on_payroll": True}, True),
        ({"on_payroll": False}, False),
        ({"on_payroll": "no"}, True),
        ("junk", True),
    ],
)
def test_the_read_side_defaults_to_on_payroll(profile, expected):
    card = {"id": 1, "is_active": True, "profile": profile}
    assert member_card(card).on_payroll is expected
