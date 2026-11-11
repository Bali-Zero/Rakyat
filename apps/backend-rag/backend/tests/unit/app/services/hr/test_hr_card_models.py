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


@pytest.mark.parametrize("nik", ["3201234567890001", "3201 2345 6789 0001", "3201.2345-6789.0001"])
def test_nik_is_sixteen_digits_after_separators_and_is_stored_as_given(nik):
    assert _ok(profile={"nik": f" {nik} "}).profile.nik == nik


@pytest.mark.parametrize("nik", ["320123456789000", "32012345678900012", "32012345678900AB", ""])
def test_nik_of_any_other_shape_is_rejected(nik):
    _bad(profile={"nik": nik})


@pytest.mark.parametrize("npwp", ["01.234.567.8-901.234", "012345678901234", "0123456789012345"])
def test_npwp_is_fifteen_or_sixteen_digits(npwp):
    assert _ok(npwp=npwp).npwp == npwp


@pytest.mark.parametrize("npwp", ["12345678901234", "01234567890123456", "01.234.567.8-901.23X"])
def test_npwp_of_any_other_shape_is_rejected(npwp):
    _bad(npwp=npwp)


def test_bpjs_kesehatan_is_thirteen_digits():
    assert _ok(bpjs_kesehatan_number="0001234567890").bpjs_kesehatan_number
    _bad(bpjs_kesehatan_number="000123456789")
    _bad(bpjs_kesehatan_number="00012345678901")
    _bad(bpjs_kesehatan_number="000123456789A")


def test_bpjs_ketenagakerjaan_is_eleven_alphanumerics():
    assert _ok(bpjs_ketenagakerjaan_number="23-0001-2345 6").bpjs_ketenagakerjaan_number
    assert _ok(bpjs_ketenagakerjaan_number="AB123456789")
    _bad(bpjs_ketenagakerjaan_number="1234567890")
    _bad(bpjs_ketenagakerjaan_number="123456789012")
    _bad(bpjs_ketenagakerjaan_number="1234567890!")


def test_bank_account_is_six_to_twenty_digits_after_spaces_and_dashes():
    assert _ok(bank_account="1234-5678 90").bank_account == "1234-5678 90"
    assert _ok(bank_account="123456")
    _bad(bank_account="12345")
    _bad(bank_account="1" * 21)
    _bad(bank_account="1234.567890")  # dots are not a bank-account separator
    _bad(bank_account="12345678AB")


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


def test_validation_messages_never_carry_the_value():
    err = _bad(profile={"nik": "SECRETVALUE-123"})
    assert "SECRETVALUE" not in str(err.errors(include_input=False))


def test_the_read_model_never_raises_on_stored_content():
    card = member_card(
        {"id": 1, "is_active": 5, "profile": {"docs": ["x"], "contract": "c", "nik": 7}}
    )
    assert (
        card.docs == {} and card.contract is None and card.nik is None and card.is_active is False
    )
    assert member_card({"id": None, "profile": "x"}).employee_id == 0
