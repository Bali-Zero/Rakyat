"""HR request bodies never reach Sentry (STAFF_CARD.md C11), proven with the real SDK.

A real `sentry_sdk` client, the repo's own hooks, `include_local_variables=False` as production
has it, and an in-memory transport. The FastAPI integration copies the request JSON onto the
event; a key-based scrub only knows the keys it was told, so `/api/hr/` events lose their body
by ROUTE (`_drop_hr_request_payload`) and the HR card keys are also exact-match keys.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr_people
from backend.app.setup.exception_handlers import register_exception_handlers
from backend.app.setup.sentry_config import (
    PII_REDACTION_PLACEHOLDER,
    _before_send,
    _before_send_transaction,
)

# Synthetic, distinctive values.
FIXTURE = {
    "legal_name": "Zetaquorum Legalname Fixture",
    "nik": "3171234567890123",
    "address": "Fixtureweg 7781 Nowhereville",
    "birth_place": "Quuxtown Fixture",
    "religion": "Fixturism Zeta",
    "bank_account": "5550011223",
    # not an HR card key in the exact-key list: only the ROUTE rule can protect these
    "unit": "Zetaquorum Unit Fixture",
    "employer_entity": "PT Zetaquorum Employer",
}
OWNER = {"user_id": "tm-owner", "email": "zero@balizero.com", "role": "member"}
ID_IN = "11111111-1111-4111-8111-111111111111"


class MemoryTransport(Transport):
    def __init__(self, options: Any = None) -> None:
        super().__init__(options)
        self.payloads: list[str] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            self.payloads.append(item.payload.get_bytes().decode("utf-8", "replace"))


@pytest.fixture
def transport():
    t = MemoryTransport()
    sentry_sdk.init(
        dsn="https://public@o0.ingest.sentry.io/1",
        transport=t,
        before_send=_before_send,
        before_send_transaction=_before_send_transaction,
        include_local_variables=False,
        send_default_pii=False,
        traces_sample_rate=1.0,
    )
    yield t
    sentry_sdk.get_client().close()
    sentry_sdk.init()  # leave a disabled client behind


def _app() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(hr_people.router)  # mounted as router_registration mounts it

    @app.post("/api/other/echo")
    async def other(payload: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - raises
        raise RuntimeError("other route failed")

    app.dependency_overrides[get_current_user] = lambda: OWNER
    app.dependency_overrides[get_database_pool] = lambda: MagicMock()
    return TestClient(app, raise_server_exceptions=False)


def test_a_failing_hr_lookup_sends_no_card_value_to_sentry(transport):
    people = MagicMock()
    people.member_email = AsyncMock(side_effect=RuntimeError("lookup failed"))
    body = {
        "card": {
            "bank_account": FIXTURE["bank_account"],
            "profile": {k: v for k, v in FIXTURE.items() if k != "bank_account"},
        },
        "clear": ["end_date"],
    }
    with patch.object(hr_people, "_get_people_service", return_value=people):
        res = _app().patch(f"/api/hr/people/{ID_IN}/card", json=body)
    assert res.status_code == 500
    sentry_sdk.flush()
    assert transport.payloads, "the lookup failure must produce an event, or this proves nothing"
    blob = json.dumps(transport.payloads)
    assert "hr people" in blob or "hr card" in blob  # the log event really was captured
    for value in FIXTURE.values():
        assert value not in blob


def test_a_non_hr_route_keeps_its_request_body_in_the_event(transport):
    body = {"topic": "visible-marker-xyz", "max_tokens": 42, "filename": "a.txt"}
    res = _app().post("/api/other/echo", json=body)
    assert res.status_code == 500
    sentry_sdk.flush()
    blob = json.dumps(transport.payloads)
    assert "visible-marker-xyz" in blob  # the route rule is not a blanket redaction


HR_EVENT = {
    "request": {
        "url": f"http://testserver/api/hr/people/{ID_IN}/card",
        "method": "PATCH",
        "query_string": "x=1",
        "data": {"anything": "personal-looking-value"},
    },
    "transaction": "/api/hr/people/{team_member_id}/card",
}


def test_hr_events_lose_body_and_query_string_whatever_the_keys():
    out = _before_send(json.loads(json.dumps(HR_EVENT)), {})
    assert out["request"]["data"] == PII_REDACTION_PLACEHOLDER
    assert out["request"]["query_string"] == PII_REDACTION_PLACEHOLDER
    assert out["request"]["method"] == "PATCH"


def test_the_transaction_path_applies_the_same_rule():
    event = {**json.loads(json.dumps(HR_EVENT)), "type": "transaction"}
    out = _before_send_transaction(event, {})
    assert out["request"]["data"] == PII_REDACTION_PLACEHOLDER


def test_an_event_known_only_by_its_transaction_name_is_covered():
    event = {"request": {"data": {"k": "v-personal"}}, "transaction": "/api/hr/me/card"}
    assert _before_send(event, {})["request"]["data"] == PII_REDACTION_PLACEHOLDER


def test_a_non_hr_event_keeps_its_body():
    event = {
        "request": {"url": "http://testserver/api/crm/clients", "data": {"topic": "kept"}},
        "transaction": "/api/crm/clients",
    }
    assert _before_send(event, {})["request"]["data"] == {"topic": "kept"}


def test_the_hr_card_keys_are_redacted_as_exact_keys_in_any_event():
    extra = {k: v for k, v in FIXTURE.items() if k not in ("unit", "employer_entity")}
    extra.update(bpjs_kesehatan_number="0001112223334", base_salary_idr=1, marital_status="x")
    out = _before_send({"extra": extra}, {})
    assert all(v == PII_REDACTION_PLACEHOLDER for v in out["extra"].values())


def test_the_exact_keys_do_not_over_match_debug_counters():
    keep = {
        "max_tokens": 5,
        "input_tokens": 7,
        "output_tokens": 9,
        "filename": "a.txt",
        "ip_address_count": 3,
        "nikon": "camera",
        "address_book_size": 4,
        "gender_ratio": 0.5,
    }
    assert _before_send({"extra": dict(keep)}, {})["extra"] == keep
