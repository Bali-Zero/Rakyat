"""The import worker: validation, resolution, ordering, accounting and the result records.

The process boundary around it (the launcher) is tested in test_import_staff_cards.py.
"""

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.hr.hr_service import HRCardCode
from backend.scripts import _import_staff_cards_worker as imp
from backend.scripts import import_staff_cards as launcher


@pytest.fixture(autouse=True)
def _lock_succeeds():
    """The row lock is a database statement; the DB-backed tests exercise the real one."""
    with patch.object(imp.HRService, "lock_staff_member", new=AsyncMock(return_value=True)) as lock:
        yield lock


# Synthetic data only.
DOC = {
    "cards": [
        {
            "email": "fake.one@balizero.com",
            "fields": {"bank_name": "Fake Bank"},
            "profile": {"unit": "u1"},
        },
        {
            "email": "fake.two@balizero.com",
            "fields": {"hire_date": "2025-03-01"},
            "profile": {},
            "is_active": False,
        },
        {"email": "fake.ghost@balizero.com", "fields": {}, "profile": {}},
        {"email": "fake.bad@balizero.com", "fields": {"ptkp_status": "ZZ/9"}, "profile": {}},
        {"email": "fake.bad2@balizero.com", "fields": {}, "profile": {"salary": 1}},
        {"email": "fake.dup@balizero.com", "fields": {"bank_name": "Fake Bank"}, "profile": {}},
        {"fields": {}},
    ]
}
# (id, stored email) of the STAFF rows; tm-3/tm-4 differ only by case.
ROWS = [
    ("tm-1", "fake.one@balizero.com"),
    ("tm-2", "fake.two@balizero.com"),
    ("tm-3", "fake.dup@balizero.com"),
    ("tm-4", "Fake.Dup@balizero.com"),
    ("tm-5", "Fake.Case@balizero.com"),
]
HAS_ROW = {"tm-1"}


def _conn() -> AsyncMock:
    conn = AsyncMock()

    async def fetch(sql, email):
        assert "<> 'client'" in sql and "[[:space:]]" in sql and "lower(" in sql

        def norm(text):
            return "".join(text.split()).lower()

        return [{"id": i, "email": e} for i, e in ROWS if norm(e) == norm(email)]

    async def fetchval(sql, member_id):
        return 1 if member_id in HAS_ROW else None

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    tx = MagicMock()
    tx.start, tx.commit, tx.rollback = AsyncMock(), AsyncMock(), AsyncMock()
    conn.transaction = MagicMock(return_value=tx)
    conn.tx = tx
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
    assert counts["rejected:ambiguous_email"] == 1
    merge.assert_not_awaited()
    conn.transaction.assert_not_called()


@pytest.mark.asyncio
async def test_apply_with_a_rejected_card_writes_nothing():
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock()):
        counts, ok = await imp.run(conn, DOC, apply=True)
    assert ok is False
    conn.tx.commit.assert_not_awaited()
    conn.tx.rollback.assert_awaited_once()
    assert counts["committed:no"] == 1


@pytest.mark.asyncio
async def test_apply_clean_document_merges_in_one_transaction():
    doc = {"cards": DOC["cards"][:2]}
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock()) as merge:
        counts, ok = await imp.run(conn, doc, apply=True)
    assert ok is True and counts["resolved"] == 2
    assert merge.await_count == 2
    conn.transaction.assert_called_once()
    conn.tx.commit.assert_awaited_once()
    assert counts["applied"] == 2 and counts["committed:yes"] == 1
    first = merge.await_args_list[0].args  # (conn, team_member_id, fields, profile)
    assert first[1:] == ("tm-1", {"bank_name": "Fake Bank"}, {"unit": "u1"})


def test_validate_card_keeps_hire_date_a_real_date():
    card, reason = imp.validate_card(
        {
            "email": "fake.two@balizero.com",
            "fields": {"hire_date": "2025-03-01"},
            "profile": {"end_date": "2026-01-01"},
        }
    )
    assert reason is None
    assert card["fields"]["hire_date"] == date(2025, 3, 1)
    assert card["profile"]["end_date"] == "2026-01-01"  # jsonb gets text


@pytest.mark.asyncio
async def test_apply_stops_on_a_not_found_email_unless_skip_is_given():
    doc = {"cards": [DOC["cards"][0], DOC["cards"][2]]}  # one resolved, one ghost
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock()):
        counts, ok = await imp.run(conn, doc, apply=True)
        assert ok is False and counts["not_found"] == 1 and counts["blocked:not_found"] == 1
        assert counts["committed:no"] == 1
        counts, ok = await imp.run(_conn(), doc, apply=True, skip_not_found=True)
        assert ok is True and counts["applied"] == 1 and counts["not_found"] == 1


@pytest.mark.asyncio
async def test_dry_run_reports_that_apply_would_stop_on_not_found():
    counts, ok = await imp.run(_conn(), {"cards": [DOC["cards"][2]]}, apply=False)
    assert ok is True and counts["blocked:not_found"] == 1


@pytest.mark.asyncio
async def test_a_merge_that_returns_no_row_is_rejected_not_applied_and_rolls_back():
    doc = {"cards": DOC["cards"][:2]}
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock(side_effect=["r1", None])):
        counts, ok = await imp.run(conn, doc, apply=True)
    assert ok is False
    assert counts["rejected:not_applied"] == 1 and counts["applied"] == 1
    conn.tx.commit.assert_not_awaited()
    conn.tx.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_card_error_is_accounted_by_its_code_only():
    doc = {"cards": DOC["cards"][:1]}
    conn = _conn()
    boom = AsyncMock(side_effect=imp.HRCardError(HRCardCode.DB_ERROR))
    with patch.object(imp.HRService, "merge_card_on", new=boom):
        counts, ok = await imp.run(conn, doc, apply=True)
    assert ok is False and counts["rejected:db_error"] == 1
    sent = _records_for(counts, True)
    assert "fake" not in sent


@pytest.mark.asyncio
async def test_every_card_is_accounted_for_exactly_once():
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock(return_value=1)):
        counts, _ = await imp.run(conn, DOC, apply=True, skip_not_found=True)
    accounted = (
        counts["applied"]
        + counts["not_found"]
        + sum(n for k, n in counts.items() if k.startswith("rejected:"))
    )
    assert accounted == len(DOC["cards"])


@pytest.mark.asyncio
async def test_an_email_matching_two_staff_rows_is_ambiguous_and_never_resolved():
    doc = {"cards": [DOC["cards"][5]]}  # fake.dup@: tm-3 (exact) and tm-4 (case twin)
    conn = _conn()
    merge = AsyncMock()
    with patch.object(imp.HRService, "merge_card_on", new=merge):
        counts, ok = await imp.run(conn, doc, apply=True)
    assert ok is False and counts["rejected:ambiguous_email"] == 1 and counts["resolved"] == 0
    merge.assert_not_awaited()
    conn.tx.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_single_case_variant_of_the_stored_email_is_not_an_exact_match():
    card = {"email": "fake.case@balizero.com", "fields": {}, "profile": {}}
    counts, _ = await imp.run(_conn(), {"cards": [card]}, apply=False)
    assert counts["not_found"] == 1 and counts["resolved"] == 0
    card["email"] = "Fake.Case@balizero.com"  # exactly as stored
    counts, _ = await imp.run(_conn(), {"cards": [card]}, apply=False)
    assert counts["resolved"] == 1


@pytest.mark.parametrize(
    "email",
    [
        " fake.one@balizero.com", "fake.one@balizero.com ", "\tfake.one@balizero.com",
        "fake.one@balizero.com\n", "fake. one@balizero.com", "fake.one@@balizero.com",
        "fake.one", "@balizero.com", "fake.one@", "fake.one@balizero.com\u200b",
        "fake.one@balizero.com\x00", "fake.one@balizero\u00a0.com", "a@b c",
        "fake.one@balizero.com\u202e", "x" * 320 + "@a.b", "a@b\ufeff",
    ],
)  # fmt: skip
def test_a_non_canonical_email_is_rejected_before_any_lookup(email):
    card, reason = imp.validate_card({"email": email, "fields": {}, "profile": {}})
    assert card is None and reason == "email_noncanonical"


def test_a_canonical_email_is_used_byte_for_byte():
    card, reason = imp.validate_card({"email": "Fake.One+tag@Balizero.com"})
    assert reason is None and card["email"] == "Fake.One+tag@Balizero.com"


@pytest.mark.asyncio
async def test_a_non_canonical_card_never_reaches_the_database():
    conn = _conn()
    doc = {"cards": [{"email": " fake.one@balizero.com "}]}
    counts, ok = await imp.run(conn, doc, apply=True)
    assert counts["rejected:email_noncanonical"] == 1 and ok is False
    conn.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_lock_recheck_receives_the_card_email_unchanged():
    seen = []

    async def spy(self, conn, member_id, email):
        seen.append(email)
        return True

    with (
        patch.object(imp.HRService, "lock_staff_member", new=spy),
        patch.object(imp.HRService, "merge_card_on", new=AsyncMock(return_value=1)),
    ):
        await imp.run(_conn(), {"cards": [{"email": "fake.one@balizero.com"}]}, apply=True)
    assert seen == ["fake.one@balizero.com"]


@pytest.mark.asyncio
async def test_a_failing_lookup_still_accounts_for_every_card():
    conn = _conn()
    real_fetch = conn.fetch.side_effect
    calls = {"n": 0}

    async def fetch(sql, email):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return await real_fetch(sql, email)

    conn.fetch = AsyncMock(side_effect=fetch)
    doc = {
        "cards": [
            {"email": "fake.one@balizero.com"},
            {"email": "fake.two@balizero.com", "fields": {"hire_date": "2025-03-01"}},
            {"email": "fake.ghost@balizero.com"},
        ]
    }
    rep = _Rep()
    with pytest.raises(ValueError):
        await imp.run(conn, doc, apply=True, on_decided=rep.decided)
    ((_, committed, counts),) = rep.events
    assert committed is False
    final = counts.get("not_found", 0) + counts.get("applied", 0)
    final += sum(n for k, n in counts.items() if k.startswith("rejected:"))
    assert final + counts["unclassified"] == len(doc["cards"]) and counts["unclassified"] == 3
    conn2 = _conn()
    calls["n"] = 0
    conn2.fetch = AsyncMock(side_effect=fetch)
    rep = _Rep()
    with pytest.raises(ValueError):
        await imp.run(conn2, doc, apply=False, on_decided=rep.decided)
    assert rep.events == []  # a dry-run has no transaction: no decision is claimed on error


def test_validate_card_reads_pydantic_errors_without_the_input(monkeypatch):
    """str(ValidationError) renders the input; the worker reads errors(include_input=False)."""
    calls = []
    original = imp.ValidationError.errors

    def spy(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(imp.ValidationError, "errors", spy)
    card = {"email": "fake.one@balizero.com", "profile": {"nik": SENTINEL}}
    assert imp.validate_card(card) == (None, "invalid_profile")
    assert calls and all(kw.get("include_input") is False for kw in calls)
    src = open(imp.__file__).read()
    assert "str(exc)" not in src and "repr(exc)" not in src and "exc.errors()" not in src


@pytest.mark.parametrize("key", ["fields", "profile"])
@pytest.mark.parametrize("bad", [None, [], False, 0, ""])
def test_a_present_non_object_fields_or_profile_is_rejected_not_normalised(key, bad):
    card, reason = imp.validate_card({"email": "fake.one@balizero.com", key: bad})
    assert card is None and reason == "bad_shape"


@pytest.mark.parametrize("bad", [None, [], False, 0, ""])
def test_a_non_object_contract_is_rejected(bad):
    card, reason = imp.validate_card(
        {"email": "fake.one@balizero.com", "profile": {"contract": bad}}
    )
    assert card is None and reason == "invalid_profile"


@pytest.mark.parametrize(
    "profile",
    [
        {"docs": {"ktp": "x"}},
        {"docs": {}},
        {"docs": None},
        {"docs": ["x"]},
        {"doc_proof": {"ktp": {"id": "x"}}},
        {"doc_proof": {}},
        {"contract": {"file_id": "x"}},
        {"contract": {"number": "C-1", "file_id": "x"}},
        {"unit": "Tax", "docs": {"contract": "x"}},
    ],
)
def test_the_door_owned_keys_are_refused_whole_with_their_own_outcome(profile):
    card, reason = imp.validate_card({"email": "fake.one@balizero.com", "profile": profile})
    assert card is None and reason == "doc_keys_door_only"


def test_contract_number_alone_is_still_importable():
    card, reason = imp.validate_card(
        {"email": "fake.one@balizero.com", "profile": {"contract": {"number": "C-1"}}}
    )
    assert reason is None and card["profile"] == {"contract": {"number": "C-1"}}


def test_absent_fields_and_profile_are_fine_and_an_unset_profile_dumps_empty():
    card, reason = imp.validate_card({"email": "fake.one@balizero.com"})
    assert reason is None and card["fields"] == {} and card["profile"] == {}


def test_identifier_shapes_are_enforced_by_the_import():
    _, reason = imp.validate_card({"email": "fake.one@balizero.com", "profile": {"nik": "123"}})
    assert reason == "invalid_profile"
    _, reason = imp.validate_card({"email": "fake.one@balizero.com", "fields": {"npwp": "1"}})
    assert reason == "invalid_fields"


SENTINEL = "ZQXSENTINEL7"


def test_an_unknown_key_on_the_outer_card_is_rejected_not_dropped():
    card = {"email": "fake.one@balizero.com", "fields": {}, "profile": {}, "extra": SENTINEL}
    assert imp.validate_card(card) == (None, "unknown_key")
    assert imp.validate_card({"email": "fake.one@balizero.com", "is_active": True})[1] is None


@pytest.mark.asyncio
async def test_unknown_outer_key_counts_and_blocks_the_batch():
    doc = {"cards": [{"email": "fake.one@balizero.com", "notes": "x"}]}
    counts, ok = await imp.run(_conn(), doc, apply=True)
    assert counts["rejected:unknown_key"] == 1 and ok is False and counts["committed:no"] == 1


@pytest.mark.asyncio
async def test_cards_are_written_in_resolved_member_id_order_not_document_order():
    doc = {
        "cards": [
            {"email": "fake.two@balizero.com", "fields": {"hire_date": "2025-03-01"}},  # tm-2
            {"email": "fake.one@balizero.com", "fields": {"bank_name": "B1"}},  # tm-1
            {"email": "fake.one@balizero.com", "fields": {"bank_name": "B2"}},  # tm-1 again
        ]
    }
    merge = AsyncMock(return_value=1)
    with patch.object(imp.HRService, "merge_card_on", new=merge):
        counts, ok = await imp.run(_conn(), doc, apply=True)
    assert ok is True and counts["applied"] == 3
    order = [(c.args[1], c.args[2]) for c in merge.await_args_list]
    assert order == [
        ("tm-1", {"bank_name": "B1"}),
        ("tm-1", {"bank_name": "B2"}),  # same member: document order
        ("tm-2", {"hire_date": date(2025, 3, 1)}),
    ]


@pytest.mark.asyncio
async def test_a_member_that_no_longer_matches_is_rejected_and_not_merged(_lock_succeeds):
    _lock_succeeds.return_value = False
    merge = AsyncMock()
    conn = _conn()
    with patch.object(imp.HRService, "merge_card_on", new=merge):
        counts, ok = await imp.run(conn, {"cards": DOC["cards"][:1]}, apply=True)
    assert ok is False and counts["rejected:member_changed"] == 1
    merge.assert_not_awaited()
    conn.tx.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_sets_a_transaction_local_lock_timeout_and_a_lock_timeout_is_a_code():
    conn = _conn()
    boom = AsyncMock(side_effect=imp.HRCardError(HRCardCode.LOCK_TIMEOUT))
    with patch.object(imp.HRService, "merge_card_on", new=boom):
        counts, ok = await imp.run(conn, {"cards": DOC["cards"][:1]}, apply=True)
    assert ok is False and counts["rejected:lock_timeout"] == 1
    assert "SET LOCAL lock_timeout" in conn.execute.await_args_list[0].args[0]
    dry = _conn()
    await imp.run(dry, {"cards": DOC["cards"][:1]}, apply=False)
    dry.execute.assert_not_awaited()


def test_a_profile_nested_inside_fields_is_rejected_not_dropped():
    card = {"email": "fake.one@balizero.com", "fields": {"profile": {"unit": SENTINEL}}}
    assert imp.validate_card(card) == (None, "unknown_key")


# ─── the result records and the moment of decision ───


class _Rep:
    """Records what the worker would write on the result pipe."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def decided(self, committed, counts) -> None:
        self.events.append(("decided", committed, dict(counts)))

    def failed(self, code) -> None:
        self.events.append(("failed", code))

    def closed(self, ok) -> None:
        self.events.append(("closed", ok))

    def names(self) -> list[str]:
        return [e[0] for e in self.events]


def _records_for(counts, committed: bool) -> str:
    import os

    read_end, write_end = os.pipe()
    try:
        imp.Reporter(write_end).decided(committed, counts)
        os.close(write_end)
        return os.read(read_end, 65536).decode()
    finally:
        os.close(read_end)


def test_the_records_the_worker_writes_are_exactly_what_the_launcher_accepts():
    import os

    read_end, write_end = os.pipe()
    rep = imp.Reporter(write_end)
    rep.decided(True, {"applied": 2, "committed:yes": 1, "rejected:db_error": 0})
    rep.failed("unexpected")
    rep.closed(True)
    os.close(write_end)
    st = launcher.State()
    st.feed(os.read(read_end, 65536))
    os.close(read_end)
    assert st.protocol_error is False
    assert st.decided == (True, {"applied": 2}) and st.failed == "unexpected" and st.closed is True


def test_every_outcome_and_code_the_worker_can_produce_is_in_the_launcher_closed_sets():
    reasons = {
        "bad_shape", "unknown_key", "email_missing", "invalid_ptkp", "invalid_profile",
        "invalid_fields", "ambiguous_email", "member_changed", "not_applied", "bad_document",
    }  # fmt: skip
    reasons |= {"email_noncanonical", "doc_keys_door_only"}
    produced = {f"rejected:{r}" for r in reasons} | {f"rejected:{c.value}" for c in HRCardCode}
    produced |= {"resolved", "not_found", "applied", "would_update", "would_insert"}
    produced |= {"blocked:not_found", "unclassified"}
    assert produced == set(launcher.OUTCOME_KEYS)
    assert {c.value for c in HRCardCode} <= set(launcher.WORKER_CODES)


@pytest.mark.asyncio
async def test_decided_is_reported_after_a_commit_and_after_a_rollback_and_for_a_dry_run():
    merge = AsyncMock(return_value=1)
    with patch.object(imp.HRService, "merge_card_on", new=merge):
        rep = _Rep()
        await imp.run(_conn(), {"cards": DOC["cards"][:2]}, True, on_decided=rep.decided)
        assert [e[:2] for e in rep.events] == [("decided", True)]
        rep = _Rep()
        await imp.run(_conn(), DOC, True, on_decided=rep.decided)  # rejected cards: rollback
        assert [e[:2] for e in rep.events] == [("decided", False)]
        rep = _Rep()
        await imp.run(_conn(), DOC, False, on_decided=rep.decided)
        assert [e[:2] for e in rep.events] == [("decided", False)]  # a dry-run never writes


@pytest.mark.asyncio
async def test_a_commit_that_raises_is_never_reported_as_decided():
    conn = _conn()
    conn.tx.commit = AsyncMock(side_effect=OSError("boom"))
    rep = _Rep()
    with patch.object(imp.HRService, "merge_card_on", new=AsyncMock(return_value=1)):
        with pytest.raises(OSError):
            await imp.run(conn, {"cards": DOC["cards"][:1]}, True, on_decided=rep.decided)
    assert rep.events == []
    conn.tx.rollback.assert_not_awaited()  # it may have committed: no rollback is claimed


@pytest.mark.asyncio
async def test_an_error_inside_the_transaction_is_decided_false_only_once_the_rollback_returned():
    conn = _conn()
    rep = _Rep()
    boom = AsyncMock(side_effect=ValueError("boom"))
    with patch.object(imp.HRService, "merge_card_on", new=boom):
        with pytest.raises(ValueError):
            await imp.run(conn, {"cards": DOC["cards"][:1]}, True, on_decided=rep.decided)
    assert [e[:2] for e in rep.events] == [("decided", False)]
    conn.tx.rollback.assert_awaited_once()
    conn = _conn()
    conn.tx.rollback = AsyncMock(side_effect=OSError("boom"))
    rep = _Rep()
    with patch.object(imp.HRService, "merge_card_on", new=boom):
        with pytest.raises(OSError):
            await imp.run(conn, {"cards": DOC["cards"][:1]}, True, on_decided=rep.decided)
    assert rep.events == []  # the rollback itself failed: unknown, not "no"


def _work(conn_or_exc, raw: bytes, apply: bool = True, init=None):
    import asyncio

    rep = _Rep()
    connect = (
        AsyncMock(side_effect=conn_or_exc)
        if isinstance(conn_or_exc, BaseException)
        else AsyncMock(return_value=conn_or_exc)
    )
    with (
        patch.object(imp.IdentityService, "get_db_connection", new=connect),
        patch.object(imp, "init_asyncpg_connection", new=init or AsyncMock()) as codec,
    ):
        ok = asyncio.run(imp.work(apply, False, raw, rep))
    return rep, ok, codec


def test_work_reports_a_bad_document_before_touching_the_database():
    rep, ok, _ = _work(OSError("x"), b'{"cards": [ broken')
    assert ok is False and rep.events == [
        ("decided", False, {}),
        ("failed", "bad_input"),
        ("closed", True),
    ]
    assert imp.IdentityService.get_db_connection is not None


def test_work_reports_connect_failed_as_decided_no_with_nothing_to_close():
    rep, ok, _ = _work(RuntimeError(SENTINEL), b'{"cards": []}')
    assert ok is False
    assert rep.events == [("decided", False, {}), ("failed", "connect_failed"), ("closed", True)]
    assert SENTINEL not in repr(rep.events)


def test_work_registers_the_production_codec_on_its_connection():
    conn = _conn()
    conn.close = AsyncMock()
    rep, ok, codec = _work(conn, json.dumps({"cards": []}).encode())
    codec.assert_awaited_once_with(conn)
    assert ok is True and rep.names() == ["decided", "closed"]
    conn.close.assert_awaited_once()


def test_work_a_codec_failure_is_decided_no_then_closed():
    conn = _conn()
    conn.close = AsyncMock()
    rep, ok, _ = _work(conn, b'{"cards": []}', init=AsyncMock(side_effect=RuntimeError(SENTINEL)))
    assert ok is False and rep.events[0][:2] == ("decided", False)
    assert rep.events[1:] == [("failed", "unexpected"), ("closed", True)]


def test_work_a_close_failure_after_a_commit_is_closed_false_after_decided_yes():
    conn = _conn()
    conn.close = AsyncMock(side_effect=OSError(SENTINEL))
    rep, ok, _ = _work(conn, b'{"cards": []}')
    assert ok is False
    assert [e[:2] for e in rep.events] == [("decided", True), ("closed", False)]


def test_work_a_system_exit_inside_the_worker_is_a_failure_record_not_an_exit():
    conn = _conn()
    conn.close = AsyncMock(side_effect=SystemExit(SENTINEL))
    rep, ok, _ = _work(conn, b'{"cards": []}')
    assert ok is False and rep.events[-1] == ("closed", False)
    rep, ok, _ = _work(SystemExit(0), b'{"cards": []}')
    assert rep.events == [("decided", False, {}), ("failed", "connect_failed"), ("closed", True)]


def test_the_worker_refuses_to_run_without_a_result_pipe():
    import os

    assert imp.main([]) == 2
    assert imp.main(["--apply"]) == 2
    assert imp.main(["--result-fd=abc"]) == 2
    assert imp.main(["--result-fd=999"]) == 2  # not an open descriptor
    read_end, write_end = os.pipe()
    try:
        assert imp.main(["--result-fd=1", "--bogus"]) == 2
    finally:
        os.close(read_end)
        os.close(write_end)
