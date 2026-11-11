"""The tolerant staff card read model."""

from backend.app.services.hr.hr_card_models import member_card


def test_the_read_model_never_raises_on_stored_content():
    card = member_card(
        {"id": 1, "is_active": 5, "profile": {"docs": ["x"], "contract": "c", "nik": 7}}
    )
    assert (
        card.docs == {} and card.contract is None and card.nik is None and card.is_active is False
    )
    assert member_card({"id": None, "profile": "x"}).employee_id == 0
