"""DAI-153: the HR writers set a staff member's WhatsApp number through the card edit (C11).

Guard: HR writer 200 + value stored; non-writer 403; a client row never written (404 at the
route, `role <> 'client'` in the UPDATE itself); an invalid number 422 without echo; the read is
masked and for HR writers only. Synthetic numbers only (`6289000...`), never a real one.
"""

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.dependencies import get_current_user, get_database_pool
from backend.app.routers import hr_people
from backend.app.services.hr.hr_card_edit import CardEditBody, mask_whatsapp, normalise_whatsapp
from backend.app.services.hr.hr_people_service import HRPeopleService
from backend.app.services.hr.hr_service import HRCardCode, HRCardError, HRService
from backend.app.setup.exception_handlers import register_exception_handlers
from backend.app.utils.hr_utils import HR_EXTRA_ADMIN_EMAILS, HR_SCOPED_VIEWERS, HR_WRITER_EMAILS
from backend.tests.unit.app.routers.test_hr_my_overview import CARD_ROW, ROWS

_WRITER_A, _WRITER_B = sorted(HR_WRITER_EMAILS)
OWNER = {"user_id": "tm-owner", "email": _WRITER_A, "role": "member"}
ACCOUNTANT = {"user_id": "tm-a", "email": _WRITER_B, "role": "member"}
HR_EXTRA = {"user_id": "tm-b", "email": sorted(HR_EXTRA_ADMIN_EMAILS)[0], "role": "member"}
SCOPED = {"user_id": "tm-c2", "email": sorted(HR_SCOPED_VIEWERS)[0], "role": "member"}
MEMBER = {"user_id": "tm-m", "email": "member-d@balizero.com", "role": "member"}
CLIENT_TOKEN = {"user_id": "tm-c", "email": _WRITER_A, "role": "client"}

STAFF_ID = "11111111-1111-4111-8111-111111111111"
CLIENT_ROW_ID = "33333333-3333-4333-8333-333333333333"  # a portal client row: not visible
URL = f"/api/hr/people/{STAFF_ID}/card"
TYPED = "+62 890-0000-0001"  # synthetic
STORED = "6289000000001"
STORED_OLD = "6289000001234"
MASKED_OLD = "62•••••1234"


def _hr_service(row_id: int | None = 7) -> AsyncMock:
    s = AsyncMock()
    s.edit_card = AsyncMock(return_value=row_id)
    s.get_card_for_member = AsyncMock(side_effect=lambda i: dict(CARD_ROW, team_member_id=i))
    s.get_my_overview_rows = AsyncMock(return_value=ROWS)
    return s


def _people() -> MagicMock:
    s = MagicMock()
    # The People predicate excludes client rows, so the lookup finds only the staff id.
    s.member_email = AsyncMock(
        side_effect=lambda i: "member-e@example.test" if i == STAFF_ID else None
    )
    s.member_whatsapp = AsyncMock(return_value=STORED_OLD)
    return s


@pytest.fixture
def make_client():
    patchers: list[Any] = []

    def _make(user: dict[str, Any]) -> tuple[TestClient, Any, Any]:
        hr, people = _hr_service(), _people()
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(hr_people.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_database_pool] = lambda: MagicMock()
        for name, val in (("_get_people_service", people), ("_get_hr_service", hr)):
            p = patch.object(hr_people, name, return_value=val)
            p.start()
            patchers.append(p)
        return TestClient(app), hr, people

    yield _make
    for p in patchers:
        p.stop()


# ─── route ───


@pytest.mark.parametrize("user", [OWNER, ACCOUNTANT])
def test_an_hr_writer_sets_the_number_normalised(make_client, user, caplog):
    client, hr, _ = make_client(user)
    with caplog.at_level(logging.DEBUG):
        res = client.patch(URL, json={"whatsapp": TYPED})
    assert res.status_code == 200
    hr.edit_card.assert_awaited_once_with(
        STAFF_ID, "member-e@example.test", {}, {}, [], whatsapp=STORED
    )
    assert res.json()["whatsapp_masked"] == MASKED_OLD
    assert STORED not in res.text and STORED_OLD not in res.text
    assert "whatsapp" in caplog.text  # the key name is logged ...
    assert STORED not in caplog.text and "890" not in caplog.text  # ... never the number


def test_the_number_rides_with_a_card_change(make_client):
    client, hr, _ = make_client(OWNER)
    res = client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}, "whatsapp": STORED})
    assert res.status_code == 200
    hr.edit_card.assert_awaited_once_with(
        STAFF_ID, "member-e@example.test", {"bank_name": "Bank Contoh"}, {}, [], whatsapp=STORED
    )


@pytest.mark.parametrize("user", [HR_EXTRA, SCOPED, MEMBER, CLIENT_TOKEN])
def test_everyone_else_is_403_and_nothing_is_written(make_client, user):
    client, hr, people = make_client(user)
    res = client.patch(URL, json={"whatsapp": STORED})
    assert res.status_code == 403
    hr.edit_card.assert_not_awaited()
    people.member_email.assert_not_awaited()


def test_a_client_row_is_404_and_never_written(make_client):
    client, hr, _ = make_client(OWNER)
    res = client.patch(f"/api/hr/people/{CLIENT_ROW_ID}/card", json={"whatsapp": STORED})
    assert res.status_code == 404
    hr.edit_card.assert_not_awaited()


@pytest.mark.parametrize(
    "bad",
    [
        "0890000000001",  # no 62
        "6289",  # too short
        "62890000000000000",  # too long (17)
        "62-890-abc-0001",  # letters
        "62٠890000001",  # a non-ASCII digit
        12345678901,  # not text
    ],
)
def test_an_invalid_number_is_422_without_echo(make_client, bad):
    client, hr, _ = make_client(OWNER)
    res = client.patch(URL, json={"whatsapp": bad})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert [d["field"] for d in detail] == ["whatsapp"]
    assert str(bad) not in res.text
    hr.edit_card.assert_not_awaited()


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_number_is_unchanged_not_a_delete(make_client, empty):
    client, hr, _ = make_client(OWNER)
    res = client.patch(URL, json={"card": {"bank_name": "Bank Contoh"}, "whatsapp": empty})
    assert res.status_code == 200
    assert "whatsapp" not in hr.edit_card.await_args.kwargs
    # alone it changes nothing: the usual 422
    assert client.patch(URL, json={"whatsapp": empty}).status_code == 422


def test_whatsapp_is_not_clearable(make_client):
    client, hr, _ = make_client(OWNER)
    assert client.patch(URL, json={"clear": ["whatsapp"]}).status_code == 422
    hr.edit_card.assert_not_awaited()


@pytest.mark.parametrize("user", [OWNER, ACCOUNTANT])
def test_the_overview_shows_the_writer_a_masked_number(make_client, user):
    client, _, _ = make_client(user)
    res = client.get(f"/api/hr/people/{STAFF_ID}/overview")
    assert res.status_code == 200
    assert res.json()["whatsapp_masked"] == MASKED_OLD
    assert STORED_OLD not in res.text


def test_the_overview_never_reads_the_number_for_a_non_writer(make_client):
    client, _, people = make_client(HR_EXTRA)  # an HR admin who reads People, not a writer
    res = client.get(f"/api/hr/people/{STAFF_ID}/overview")
    assert res.status_code == 200
    assert res.json()["whatsapp_masked"] is None
    people.member_whatsapp.assert_not_awaited()


# ─── body model ───


def test_normalise_whatsapp_shapes():
    assert normalise_whatsapp("+62 (890) 0000.0001") == STORED
    assert normalise_whatsapp("  ") is None
    assert normalise_whatsapp("6289000000") == "6289000000"  # 10 digits
    assert normalise_whatsapp("628900000000000") == "628900000000000"  # 15 digits
    for bad in ("628900000", "6289000000000000", "62 890 0000 000x"):
        with pytest.raises(ValueError) as exc:
            normalise_whatsapp(bad)
        assert bad not in str(exc.value)


def test_the_import_patch_cannot_carry_whatsapp():
    with pytest.raises(ValidationError):
        CardEditBody.model_validate({"card": {"whatsapp": STORED}})


def test_mask_whatsapp():
    assert mask_whatsapp(STORED_OLD) == MASKED_OLD
    assert mask_whatsapp("+62 890-0000-1234") == MASKED_OLD
    assert mask_whatsapp(None) is None
    assert mask_whatsapp("1234567") is None  # too short to mask safely
    assert mask_whatsapp(6289000001234) is None


# ─── service ───


def _pool(fetchval_values: list[Any]):
    conn = AsyncMock()
    conn.transaction = MagicMock(
        return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    )
    conn.fetchval = AsyncMock(side_effect=fetchval_values)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


@pytest.mark.asyncio
async def test_service_whatsapp_only_writes_team_members_for_staff_only():
    # lock -> visible, hr row id 7, UPDATE ... RETURNING id
    pool, conn = _pool([STAFF_ID, 7, STAFF_ID])
    assert await HRService(pool).edit_card(STAFF_ID, "a@b.c", {}, {}, [], whatsapp=STORED) == 7
    conn.fetchrow.assert_not_awaited()  # hr_employees untouched
    sql, member, value = conn.fetchval.await_args_list[-1].args
    assert sql.strip().startswith("UPDATE team_members SET whatsapp = $2")
    assert "<> 'client'" in sql and member == STAFF_ID and value == STORED


@pytest.mark.asyncio
async def test_service_with_a_card_change_merges_then_writes_the_number():
    pool, conn = _pool([STAFF_ID, STAFF_ID])
    conn.fetchrow = AsyncMock(return_value={"id": 7, "profile_type": "object"})
    got = await HRService(pool).edit_card(
        STAFF_ID, "a@b.c", {"bank_name": "B"}, {}, [], whatsapp=STORED
    )
    assert got == 7
    conn.fetchrow.assert_awaited_once()
    assert "UPDATE team_members SET whatsapp" in conn.fetchval.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_service_a_client_row_is_never_written():
    # visible at the lock, but the UPDATE's own role guard matches nothing: the whole edit fails
    pool, conn = _pool([STAFF_ID, 7, None])
    with pytest.raises(HRCardError) as exc:
        await HRService(pool).edit_card(CLIENT_ROW_ID, "a@b.c", {}, {}, [], whatsapp=STORED)
    assert exc.value.code is HRCardCode.DB_ERROR


@pytest.mark.asyncio
async def test_service_whatsapp_only_without_an_hr_row_writes_nothing():
    pool, conn = _pool([STAFF_ID, None])
    assert await HRService(pool).edit_card(STAFF_ID, "a@b.c", {}, {}, [], whatsapp=STORED) is None
    assert all("UPDATE" not in c.args[0] for c in conn.fetchval.await_args_list)


@pytest.mark.asyncio
async def test_people_service_reads_the_number_through_the_visibility_predicate():
    pool, conn = _pool([STORED_OLD])
    assert await HRPeopleService(pool).member_whatsapp(STAFF_ID) == STORED_OLD
    sql = conn.fetchval.await_args.args[0]
    assert "tm.whatsapp" in sql and "'client'" in sql
