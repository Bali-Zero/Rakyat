"""Service cards feed the practice checklist (V1, spec BLOCK-1-VISA sections 3 and 4).

The seeding cases go through the router (create_practice / update_practice) with a fake
asyncpg connection, so they exercise the call sites and fail on an assertion, not on a
missing import, where the card path does not exist yet.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.core.config import settings

HERE = Path(__file__).parent
FIXTURE = HERE / "fixtures" / "visa_cards_seed.json"
MIGRATION = Path(__file__).resolve().parents[4] / "db" / "migrations_v2" / "396_service_cards.sql"
# sha256 of the reviewed seed list, not a credential.
SEED_SHA256 = (
    "6bfc6cfb4be5a93720c0c54b330aa1af4fb61606744f4b398a793300a9f0bba2"  # pragma: allowlist secret
)
SETTING = "service_cards_seeding_codes"
_MISSING = object()

LEGACY_DOCS = ["Legacy Only Doc"]
CARD_DOCS = [
    {
        "document_type": "passport",
        "label": "Passport bio page",
        "client_text": "Your passport photo page.",
        "condition_text": None,
        "active": True,
        "sort_order": 1,
    },
    {
        "document_type": "sponsor_letter",
        "label": "Sponsor letter",
        "client_text": "Letter from your sponsor.",
        "condition_text": "only when sponsored by a company",
        "active": True,
        "sort_order": 2,
    },
    {
        "document_type": "old_form",
        "label": "Old form",
        "client_text": "A retired form.",
        "condition_text": None,
        "active": False,
        "sort_order": 3,
    },
    {
        "document_type": "photo",
        "label": "Passport photo",
        "client_text": "A recent photo, red background.",
        "condition_text": None,
        "active": True,
        "sort_order": 4,
    },
]


@contextmanager
def armed(value: str):
    """Set the setting for one test, whether or not the field is declared yet."""
    previous = settings.__dict__.get(SETTING, _MISSING)
    object.__setattr__(settings, SETTING, value)
    try:
        yield
    finally:
        if previous is _MISSING:
            settings.__dict__.pop(SETTING, None)
        else:
            object.__setattr__(settings, SETTING, previous)


def _pool(conn_fetchrow_list, card_row, docs):
    """A pool whose connection answers card queries by SQL text and the rest in order."""
    conn = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    queue = list(conn_fetchrow_list)

    async def fetchrow(sql, *args):
        if "service_cards" in sql:
            return card_row
        return queue.pop(0)

    async def fetch(sql, *args):
        return docs if "service_card_documents" in sql else []

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.execute = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acq)
    return pool, conn


def _patches():
    return (
        patch("backend.app.routers.crm_practices.verify_client_access", new=AsyncMock()),
        patch("backend.app.routers.crm_practices.invalidate_cache", new=AsyncMock()),
        patch("backend.app.routers.crm_practices.log_database_operation"),
        patch("backend.app.routers.crm_practices.log_success"),
    )


async def _create(card_row, docs=None, legacy=None):
    from backend.app.routers.crm_practices import PracticeCreate, create_practice

    type_row = {
        "id": 14,
        "base_price": Decimal("1000000"),
        "category": "visa",
        "name": "C1 Tourism",
        "required_documents": LEGACY_DOCS if legacy is None else legacy,
    }
    inserted_row = {
        "id": 601,
        "uuid": "00000000-0000-0000-0000-000000000601",
        "client_id": 42,
        "practice_type_id": 14,
        "status": "inquiry",
        "priority": "normal",
        "quoted_price": Decimal("1000000"),
        "actual_price": None,
        "payment_status": "unpaid",
        "assigned_to": "staff-member",
        "start_date": None,
        "completion_date": None,
        "expiry_date": None,
        "created_at": datetime(2026, 11, 28, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 11, 28, tzinfo=timezone.utc),
        "client_visible": True,
    }
    pool, conn = _pool(
        [type_row, {"assigned_to": "staff-member"}, inserted_row],
        card_row,
        CARD_DOCS if docs is None else docs,
    )
    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        await create_practice(
            request=MagicMock(),
            practice=PracticeCreate(client_id=42, practice_type_code="visa_c1_tourism"),
            background_tasks=MagicMock(),
            db_pool=pool,
            current_user={"email": "staff-member", "role": "admin"},
        )
    return conn


async def _update(card_row, docs=None):
    from backend.app.routers.crm_practices import PracticeUpdate, update_practice

    old_row = {
        "status": "inquiry",
        "client_id": 42,
        "client_visible": True,
        "created_by": "admin-member",
        "assigned_to": "staff-member",
        "practice_type_code": "open_inquiry",
    }
    new_type_row = {
        "id": 14,
        "base_price": Decimal("1000000"),
        "required_documents": LEGACY_DOCS,
    }
    updated_row = {
        "id": 602,
        "status": "waiting_documents",
        "client_visible": True,
        "practice_type_id": 14,
        "client_id": 42,
        "practice_type_code": "visa_c1_tourism",
        "practice_type_name": "C1 Tourism",
        "completion_date": None,
    }
    pool, conn = _pool(
        [old_row, new_type_row, updated_row], card_row, CARD_DOCS if docs is None else docs
    )
    conn.fetchval = AsyncMock(side_effect=[602, False])
    p1, p2, p3, p4 = _patches()
    with p1, p2, p3, p4:
        await update_practice(
            practice_id=602,
            updates=PracticeUpdate(practice_type_code="visa_c1_tourism"),
            request=MagicMock(),
            db_pool=pool,
            current_user={"email": "staff-member", "role": "admin"},
        )
    return conn


def _seed_calls(conn):
    return [
        call
        for call in conn.execute.await_args_list
        if "INSERT INTO practice_required_documents" in call.args[0]
    ]


def _all_args(call):
    out = []
    for arg in call.args[1:]:
        out.extend(arg if isinstance(arg, (list, tuple)) else [arg])
    return out


ACTIVE_CARD = {"code": "visa_c1_tourism", "active": True}
INACTIVE_CARD = {"code": "visa_c1_tourism", "active": False}


# --------------------------------------------------------------------------- card seeding


@pytest.mark.asyncio
async def test_listed_active_card_seeds_one_slot_per_always_asked_document() -> None:
    with armed("visa_c1_tourism"):
        conn = await _create(ACTIVE_CARD)
    calls = _seed_calls(conn)
    assert len(calls) == 1
    assert calls[0].args[2] == ["passport", "photo"]
    sql, practice_id, doc_types, labels, descriptions, author = calls[0].args
    assert practice_id == 601
    assert labels == ["Passport bio page", "Passport photo"]
    assert descriptions == ["Your passport photo page.", "A recent photo, red background."]
    assert "team_member_notes" not in sql
    assert "ON CONFLICT (practice_id, document_type) DO NOTHING" in sql
    assert author == "staff-member"
    assert "legacy_only_doc" not in _all_args(calls[0])


@pytest.mark.asyncio
async def test_only_when_document_is_not_seeded() -> None:
    with armed("visa_c1_tourism"):
        conn = await _create(ACTIVE_CARD)
    calls = _seed_calls(conn)
    assert calls[0].args[2] == ["passport", "photo"]
    assert "sponsor_letter" not in _all_args(calls[0])


@pytest.mark.asyncio
async def test_inactive_document_is_not_seeded() -> None:
    with armed("visa_c1_tourism"):
        conn = await _create(ACTIVE_CARD)
    calls = _seed_calls(conn)
    assert calls[0].args[2] == ["passport", "photo"]
    assert "old_form" not in _all_args(calls[0])


@pytest.mark.asyncio
async def test_card_with_only_when_documents_seeds_nothing_and_does_not_fall_back() -> None:
    only_when = [CARD_DOCS[1]]
    with armed("visa_c1_tourism"):
        conn = await _create(ACTIVE_CARD, docs=only_when)
    assert _seed_calls(conn) == []


@pytest.mark.asyncio
async def test_change_of_service_seeds_the_new_cards_documents_and_removes_none() -> None:
    with armed("visa_c1_tourism"):
        conn = await _update(ACTIVE_CARD)
    calls = _seed_calls(conn)
    assert len(calls) == 1
    assert calls[0].args[2] == ["passport", "photo"]
    assert not [c for c in conn.execute.await_args_list if "DELETE" in c.args[0].upper()]


@pytest.mark.asyncio
async def test_star_seeds_from_an_active_card() -> None:
    with armed("*"):
        conn = await _create(ACTIVE_CARD)
    calls = _seed_calls(conn)
    assert len(calls) == 1
    assert calls[0].args[2] == ["passport", "photo"]


@pytest.mark.asyncio
async def test_code_in_a_comma_list_with_spaces_is_listed() -> None:
    with armed("other_code, visa_c1_tourism ,another"):
        conn = await _create(ACTIVE_CARD)
    assert _seed_calls(conn)[0].args[2] == ["passport", "photo"]


# --------------------------------------------------------------------------- legacy unchanged


def _assert_legacy(conn) -> None:
    calls = _seed_calls(conn)
    assert len(calls) == 1
    assert calls[0].args[2] == ["legacy_only_doc"]
    assert calls[0].args[3] == ["Legacy Only Doc"]


@pytest.mark.asyncio
async def test_setting_empty_is_legacy_only_and_never_reads_a_card() -> None:
    with armed(""):
        conn = await _create(ACTIVE_CARD)
    _assert_legacy(conn)
    assert not [c for c in conn.fetchrow.await_args_list if "service_cards" in c.args[0]]
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_code_not_listed_is_legacy() -> None:
    with armed("some_other_code"):
        conn = await _create(ACTIVE_CARD)
    _assert_legacy(conn)


@pytest.mark.asyncio
async def test_inactive_card_is_legacy() -> None:
    with armed("visa_c1_tourism"):
        conn = await _create(INACTIVE_CARD)
    _assert_legacy(conn)


@pytest.mark.asyncio
async def test_star_with_inactive_card_is_legacy() -> None:
    with armed("*"):
        conn = await _create(INACTIVE_CARD)
    _assert_legacy(conn)


@pytest.mark.asyncio
async def test_no_card_is_legacy() -> None:
    with armed("visa_c1_tourism"):
        conn = await _create(None)
    _assert_legacy(conn)


@pytest.mark.asyncio
async def test_seeding_twice_issues_identical_inserts_and_never_overwrites_or_deletes() -> None:
    from backend.app.routers import crm_practices

    seed_required_documents_from_card = getattr(
        crm_practices, "seed_required_documents_from_card", None
    )
    assert seed_required_documents_from_card is not None, "the router does not seed from cards"

    _pool_unused, conn = _pool([], ACTIVE_CARD, CARD_DOCS)
    with armed("visa_c1_tourism"):
        first = await seed_required_documents_from_card(conn, 601, 14, "staff-member")
        second = await seed_required_documents_from_card(conn, 601, 14, "staff-member")
    assert first == second == 2
    calls = _seed_calls(conn)
    assert len(calls) == 2
    assert calls[0].args == calls[1].args
    assert "ON CONFLICT (practice_id, document_type) DO NOTHING" in calls[0].args[0]
    assert "DO UPDATE" not in calls[0].args[0].upper()
    assert not [
        c
        for c in conn.execute.await_args_list
        if re.search(r"\b(DELETE|UPDATE)\b", c.args[0].upper())
        and "practice_required_documents" in c.args[0]
    ]


def test_setting_is_declared_and_off_by_default() -> None:
    from backend.app.core.config import Settings

    field = Settings.model_fields.get(SETTING)
    assert field is not None and field.default == ""


# --------------------------------------------------------------------------- fixture and migration parity


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_the_reviewed_list() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == SEED_SHA256
    cards = _fixture()["cards"]
    assert len(cards) == 61
    assert sum(1 for c in cards if c["active"]) == 17
    assert sum(len(c["documents"]) for c in cards) == 220


_TOKEN = re.compile(r"'(?:[^']|'')*'|NULL|true|false|\d+")


def _parse_row(line: str) -> list:
    body = line.strip().rstrip(",")
    assert body.startswith("(") and body.endswith(")"), line
    tokens = _TOKEN.findall(body[1:-1])
    values: list = []
    for tok in tokens:
        if tok == "NULL":
            values.append(None)
        elif tok in ("true", "false"):
            values.append(tok == "true")
        elif tok[0] == "'":
            values.append(tok[1:-1].replace("''", "'"))
        else:
            values.append(int(tok))
    return values


def _seed_rows() -> tuple[list[list], list[list]]:
    text = MIGRATION.read_text(encoding="utf-8").split("-- === ROLLBACK ===")[0]
    card_rows: list[list] = []
    doc_rows: list[list] = []
    for line in text.splitlines():
        if not line.startswith("    ('"):
            continue
        row = _parse_row(line)
        (card_rows if len(row) == 5 else doc_rows).append(row)
    return card_rows, doc_rows


def test_migration_seed_equals_the_reviewed_list() -> None:
    card_rows, doc_rows = _seed_rows()
    cards = _fixture()["cards"]
    assert card_rows == [
        [c["code"], c["block"], c["visa_type_code"], c["stage"], c["active"]] for c in cards
    ]
    expected = [
        [
            c["code"],
            d["document_type"],
            d["label"],
            d["client_text"],
            d["provider"],
            d["condition_text"],
            d["reuse"],
            d["source"],
            position,
        ]
        for c in cards
        for position, d in enumerate(c["documents"], start=1)
    ]
    assert doc_rows == expected


def test_seeded_values_fit_the_columns() -> None:
    for c in _fixture()["cards"]:
        for d in c["documents"]:
            assert len(d["document_type"]) <= 50, d["document_type"]
            assert len(d["label"]) <= 100, d["label"]


def test_migration_has_rollback_guarded_grant_and_no_visa_types_fk() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    forward, rollback = text.split("-- === ROLLBACK ===")
    assert "visa_types(" not in forward
    assert "pg_roles" in forward and forward.index("pg_roles") < forward.index("GRANT")
    assert rollback.index("DROP TABLE service_card_documents") < rollback.index(
        "DROP TABLE service_cards"
    )
    assert "ON CONFLICT" not in forward
    assert "JOIN practice_types pt ON pt.code = v.code" in forward
    assert "v.sort_order, true, 'migration_396'" in forward
    assert (
        "practice_type_id integer PRIMARY KEY REFERENCES practice_types(id) ON DELETE CASCADE"
        in forward
    )
