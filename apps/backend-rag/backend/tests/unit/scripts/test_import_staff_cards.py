"""import_staff_cards: dry-run counts, strict flags, and no payload value ever printed."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.scripts import import_staff_cards as imp

# Synthetic data only.
DOC = {
    "cards": [
        {"email": "fake.one@balizero.com", "fields": {"npwp": "N-1"}, "profile": {"unit": "u1"}},
        {
            "email": "fake.two@balizero.com",
            "fields": {"hire_date": "2025-03-01"},
            "profile": {},
            "is_active": False,
        },
        {"email": "fake.ghost@balizero.com", "fields": {}, "profile": {}},
        {"email": "fake.bad@balizero.com", "fields": {"ptkp_status": "ZZ/9"}, "profile": {}},
        {"email": "fake.bad2@balizero.com", "fields": {}, "profile": {"salary": 1}},
        {"fields": {}},
    ]
}
MEMBERS = {"fake.one@balizero.com": "tm-1", "fake.two@balizero.com": "tm-2"}
HAS_ROW = {"tm-1"}


def _conn() -> AsyncMock:
    conn = AsyncMock()

    async def fetchrow(sql, email):
        mid = MEMBERS.get(email.lower())
        return {"id": mid} if mid else None

    async def fetchval(sql, member_id):
        return 1 if member_id in HAS_ROW else None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    return conn


@pytest.mark.asyncio
async def test_dry_run_counts_and_no_write():
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock()) as merge:
        counts, ok = await imp.run(conn, DOC, apply=False)
    assert ok is True
    assert counts["resolved"] == 2
    assert counts["not_found"] == 1
    assert counts["would_update"] == 1
    assert counts["would_insert"] == 1
    assert counts["rejected:invalid_ptkp"] == 1
    assert counts["rejected:invalid_profile"] == 1
    assert counts["rejected:email_missing"] == 1
    merge.assert_not_awaited()
    conn.transaction.assert_not_called()


@pytest.mark.asyncio
async def test_apply_with_a_rejected_card_writes_nothing():
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock()) as merge:
        _, ok = await imp.run(conn, DOC, apply=True)
    assert ok is False
    merge.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_clean_document_merges_in_one_transaction():
    doc = {"cards": DOC["cards"][:2]}
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock()) as merge:
        counts, ok = await imp.run(conn, doc, apply=True)
    assert ok is True and counts["resolved"] == 2
    assert merge.await_count == 2
    conn.transaction.assert_called_once()
    first = merge.await_args_list[0].args  # (conn, team_member_id, fields, profile)
    assert first[1:] == ("tm-1", {"npwp": "N-1"}, {"unit": "u1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [["--apply-all"], ["--aply"], ["extra"], ["--apply", "--force"]])
async def test_unknown_flag_exits_2_without_touching_the_db(argv):
    with patch.object(imp.IdentityService, "get_db_connection", new=AsyncMock()) as connect:
        with pytest.raises(SystemExit) as exc:
            await imp.amain(argv, json.dumps(DOC))
    assert exc.value.code == 2
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_carries_counts_only(capsys):
    conn = _conn()
    conn.close = AsyncMock()
    with patch.object(imp.IdentityService, "get_db_connection", new=AsyncMock(return_value=conn)):
        code = await imp.amain([], json.dumps(DOC))
    out = capsys.readouterr().out
    assert code == 0
    assert "mode: dry-run" in out and "would_insert: 1" in out
    for leaked in ("fake.", "N-1", "u1", "tm-1", "@"):
        assert leaked not in out
