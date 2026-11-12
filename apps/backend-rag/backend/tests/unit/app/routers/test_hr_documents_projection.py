"""R4 / acceptance 2 (STAFF_CARD.md C13): no card response carries a stored slot value.

A NIK-like synthetic value sits in every place the old card exposed raw (`docs.<slot>`, the legacy
`docs.contract`, `contract.file_id`, an unknown `docs` key, `doc_proof`). Every response that
contains a card must show only the closed `documents` map, and nothing may reach DEBUG logs."""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr as hr_router
from backend.app.routers import hr_people
from backend.app.setup.exception_handlers import register_exception_handlers
from backend.tests.unit.app.routers.test_hr_my_overview import CARD_ROW, ROWS

NIKISH = "3171234567890001"  # synthetic 16 digits
VERIFIED = "VerifiedDriveFile1234567890abcdef"  # a verified slot may show its id
OWNER = {"id": "tm-owner", "user_id": "tm-owner", "email": "zero@balizero.com", "role": "member"}
ID_IN = "11111111-1111-4111-8111-111111111111"
SIX = {"ktp", "kk", "npwp", "bpjs_kes", "bpjs_tk", "contract"}

PROFILE = {
    "unit": "Tax",
    "docs": {
        "ktp": NIKISH,  # legacy, unverified
        "kk": VERIFIED,
        "npwp": 12345,  # not a string
        "contract": NIKISH,  # the legacy duplicate
        "mystery": NIKISH,  # an unknown docs key
    },
    "contract": {"number": "C-1", "file_id": NIKISH},
    "doc_proof": {
        "kk": {"id": VERIFIED, "by": "zero@balizero.com", "at": "2026-11-13T00:00:00+00:00"},
        "ktp": {"id": "somethingelse", "by": "x", "at": "y"},  # does not match: unverified
    },
}


def _card_row(profile: dict[str, Any] = PROFILE) -> dict[str, Any]:
    return dict(CARD_ROW, profile=profile)


def _service() -> AsyncMock:
    s = AsyncMock()
    s.get_card_for_member = AsyncMock(side_effect=lambda i: dict(_card_row(), team_member_id=i))
    s.get_my_overview_rows = AsyncMock(return_value=ROWS)
    s.edit_card = AsyncMock(return_value=7)
    return s


def _client() -> tuple[TestClient, list[Any]]:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(hr_router.router)
    app.include_router(hr_people.router)
    app.dependency_overrides[get_current_user] = lambda: OWNER
    app.dependency_overrides[get_database_pool] = lambda: MagicMock()
    people = MagicMock()
    people.member_email = AsyncMock(return_value="dea@balizero.com")
    service = _service()
    patchers = [
        patch.object(hr_router, "_get_hr_service", return_value=service),
        patch.object(hr_people, "_get_hr_service", return_value=service),
        patch.object(hr_people, "_get_people_service", return_value=people),
    ]
    for p in patchers:
        p.start()
    return TestClient(app), patchers


@pytest.fixture
def client():
    c, patchers = _client()
    yield c
    for p in patchers:
        p.stop()


def _documents(card: dict[str, Any]) -> dict[str, Any]:
    assert set(card["documents"]) == SIX  # closed: exactly six keys
    assert "docs" not in card and "doc_proof" not in card
    assert "file_id" not in (card.get("contract") or {})
    return card["documents"]


def _check_card(card: dict[str, Any]) -> None:
    d = _documents(card)
    assert d["ktp"] == {"present": True, "file_id": None}  # legacy: present, never shown
    assert d["kk"] == {"present": True, "file_id": VERIFIED}  # verified: shown
    assert d["npwp"] == {"present": False, "file_id": None}  # non-string: absent, not echoed
    assert d["bpjs_kes"] == {"present": False, "file_id": None}
    assert d["contract"] == {"present": True, "file_id": None}  # contract.file_id unverified
    assert card["contract"] == {"number": "C-1"}  # the number stays


def test_every_card_response_carries_the_map_and_never_a_stored_value(client, caplog):
    with caplog.at_level(logging.DEBUG):
        me = client.get("/api/hr/me/card")
        overview = client.get("/api/hr/me/overview")
        person = client.get(f"/api/hr/people/{ID_IN}/overview")
        patched = client.patch(f"/api/hr/people/{ID_IN}/card", json={"card": {"bank_name": "B"}})
    for res in (me, overview, person, patched):
        assert res.status_code == 200, res.text
        assert NIKISH not in res.text and "mystery" not in res.text and "doc_proof" not in res.text
    _check_card(me.json())
    _check_card(overview.json()["card"])
    _check_card(person.json()["person"]["card"])
    _check_card(patched.json()["person"]["card"])
    assert NIKISH not in caplog.text


def test_the_legacy_employee_shape_never_carries_the_profile():
    """`/employees/{id}` (and the list) serve `_legacy_row`: the profile, so docs and proofs, is out."""
    from backend.app.services.hr.hr_service import HRService

    row = HRService._legacy_row({"id": 1, "profile": PROFILE, "bank_name": "B"})
    assert "profile" not in row and NIKISH not in str(row)


def test_the_openapi_contract_makes_documents_and_every_slot_required():
    """The runtime always returns the full map, so the contract says so: `documents` is required
    and non-nullable on the card and on the door's answer; the map has exactly six required
    closed keys, each `{present, file_id}` with both required and `file_id` nullable."""
    from backend.app.services.hr.hr_card_models import DocumentChangeResult, StaffCard

    for model in (StaffCard, DocumentChangeResult):
        schema = model.model_json_schema(mode="serialization")
        assert "documents" in schema["required"]
        assert "anyOf" not in schema["properties"]["documents"]  # not nullable
    defs = DocumentChangeResult.model_json_schema(mode="serialization")["$defs"]
    docs = defs["DocumentsMap"]
    assert set(docs["required"]) == SIX and set(docs["properties"]) == SIX
    assert docs["additionalProperties"] is False
    state = defs["DocumentState"]
    assert set(state["required"]) == {"present", "file_id"}
    assert state["additionalProperties"] is False
    assert {"type": "null"} in state["properties"]["file_id"]["anyOf"]
