"""A blank owner e-mail on case creation means no owner, never an empty one."""

import pytest

import backend.app.routers.e33_cases as e33_cases_module


class TestCaseCreateBodyOwnerEmail:
    @pytest.mark.unit
    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_blank_owner_email_becomes_none(self, blank: str) -> None:
        body = e33_cases_module.CaseCreateBody(client_id=1, basis="deposit", owner_email=blank)
        assert body.owner_email is None

    @pytest.mark.unit
    def test_real_owner_email_is_kept(self) -> None:
        body = e33_cases_module.CaseCreateBody(
            client_id=1, basis="deposit", owner_email="team@balizero.com"
        )
        assert body.owner_email == "team@balizero.com"
