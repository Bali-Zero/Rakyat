"""import_staff_cards: dry-run counts, strict flags, and no payload value ever printed."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.hr.hr_service import HRCardCode
from backend.scripts import import_staff_cards as imp

SCRIPT = Path(imp.__file__).resolve()
BACKEND_RAG = SCRIPT.parents[2]


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
        assert "<> 'client'" in sql and "LOWER($1)" in sql
        return [{"id": i, "email": e} for i, e in ROWS if e.lower() == email.lower()]

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
    for leaked in ("fake.", "Fake Bank", "u1", "tm-1", "@"):
        assert leaked not in out


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


def test_skip_not_found_is_strict_no_abbreviation():
    assert imp.parse_args(["--apply", "--skip-not-found"]).skip_not_found is True
    with pytest.raises(SystemExit) as exc:
        imp.parse_args(["--skip"])
    assert exc.value.code == 2


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
    assert "fake" not in imp.format_counts(counts, True)


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
async def test_amain_registers_the_production_codec_on_its_connection():
    conn = _conn()
    conn.close = AsyncMock()
    with (
        patch.object(imp.IdentityService, "get_db_connection", new=AsyncMock(return_value=conn)),
        patch.object(imp, "init_asyncpg_connection", new=AsyncMock()) as init,
    ):
        await imp.amain([], json.dumps(DOC))
    init.assert_awaited_once_with(conn)


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
    card["email"] = " Fake.Case@balizero.com "  # trimmed, then exact
    counts, _ = await imp.run(_conn(), {"cards": [card]}, apply=False)
    assert counts["resolved"] == 1


@pytest.mark.parametrize("key", ["fields", "profile"])
@pytest.mark.parametrize("bad", [None, [], False, 0, ""])
def test_a_present_non_object_fields_or_profile_is_rejected_not_normalised(key, bad):
    card, reason = imp.validate_card({"email": "fake.one@balizero.com", key: bad})
    assert card is None and reason == "bad_shape"


@pytest.mark.parametrize("sub", ["docs", "contract"])
@pytest.mark.parametrize("bad", [None, [], False, 0, ""])
def test_a_non_object_docs_or_contract_is_rejected(sub, bad):
    card, reason = imp.validate_card({"email": "fake.one@balizero.com", "profile": {sub: bad}})
    assert card is None and reason == "invalid_profile"


def test_absent_fields_and_profile_are_fine_and_an_unset_profile_dumps_empty():
    card, reason = imp.validate_card({"email": "fake.one@balizero.com"})
    assert reason is None and card["fields"] == {} and card["profile"] == {}


def test_identifier_shapes_are_enforced_by_the_import():
    _, reason = imp.validate_card({"email": "fake.one@balizero.com", "profile": {"nik": "123"}})
    assert reason == "invalid_profile"
    _, reason = imp.validate_card({"email": "fake.one@balizero.com", "fields": {"npwp": "1"}})
    assert reason == "invalid_fields"


SENTINEL = "ZQXSENTINEL7"


@pytest.mark.parametrize(
    "argv",
    [
        ["--apply", f"--npwp={SENTINEL}"],  # unknown flag with a value
        [f"--apply={SENTINEL}"],  # malformed value for a known flag
        [SENTINEL],  # stray positional
        [f"--skip-not-foun={SENTINEL}"],  # abbreviation attempt
    ],
)
def test_the_real_entry_point_fails_every_parse_error_with_a_fixed_text(argv):
    unreachable = {
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",  # any DB access would fail loudly
        "PATH": "/usr/bin:/bin",
        "JWT_SECRET_KEY": "j" * 40,  # settings need a 32+ char value to import; unused here
        "API_KEYS": "k" * 12,
    }
    run = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        input="",
        capture_output=True,
        text=True,
        cwd=BACKEND_RAG,
        env={**unreachable, "PYTHONPATH": str(BACKEND_RAG)},
        timeout=120,
    )
    assert run.returncode == 2
    assert SENTINEL not in run.stdout and SENTINEL not in run.stderr
    # (import-time warnings may precede it; the parser contributes exactly this one line)
    assert run.stderr.strip().splitlines()[-1] == (
        "import_staff_cards: invalid arguments (run with --help)"
    )
    assert "rejected" not in run.stdout and "failed" not in run.stdout  # nothing ran


def test_help_output_contains_no_argv(capsys):
    with pytest.raises(SystemExit) as exc:
        imp.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--apply" in out and "--skip-not-found" in out and SENTINEL not in out


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


# ─── the real entry point with a fake driver: no value reaches a stream, whatever fails ───

_FAKE_DRIVER = """
import os
mode = os.environ.get("HRIMPORT_FAKE_MODE")
if mode:
    import asyncpg
    sentinel = os.environ["HRIMPORT_FAKE_SENTINEL"]

    class Tx:
        async def start(self): pass
        async def commit(self): pass
        async def rollback(self): pass

    class Conn:
        async def set_type_codec(self, *a, **k):
            if mode == "codec":
                raise RuntimeError(sentinel)
        async def execute(self, *a): pass
        async def fetch(self, *a):
            if mode == "txn":
                raise ValueError(sentinel)
            return []
        async def fetchval(self, *a): return None
        def transaction(self): return Tx()
        async def close(self):
            if mode in ("close_rollback", "close_commit"):
                raise OSError(sentinel)

    async def connect(*a, **k):
        if mode == "connect":
            raise asyncpg.InvalidPasswordError(sentinel)
        return Conn()

    asyncpg.connect = connect
"""

_GHOST = {"cards": [{"email": "fake.ghost@balizero.com"}]}  # resolves to nobody


def _entry_point(tmp_path, mode, stdin, args=("--apply",)):
    (tmp_path / "sitecustomize.py").write_text(_FAKE_DRIVER)
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",
        "JWT_SECRET_KEY": "j" * 40,
        "API_KEYS": "k" * 12,
        "PYTHONPATH": f"{tmp_path}:{BACKEND_RAG}",
        "HRIMPORT_FAKE_MODE": mode,
        "HRIMPORT_FAKE_SENTINEL": SENTINEL,
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=BACKEND_RAG,
        env=env,
        timeout=120,
    )


def _assert_quiet(run, fixed):
    assert run.returncode != 0
    assert SENTINEL not in run.stdout and SENTINEL not in run.stderr
    assert "Traceback" not in run.stdout and "Traceback" not in run.stderr
    assert fixed in run.stderr + run.stdout


@pytest.mark.parametrize(
    ("mode", "stdin", "args", "fixed"),
    [
        ("connect", json.dumps(_GHOST), ("--apply",), "failed (connect_failed)"),
        ("codec", json.dumps(_GHOST), ("--apply",), "failed (unexpected)"),
        (
            "txn",
            json.dumps({"cards": [{"email": "fake.one@balizero.com"}]}),
            ("--apply",),
            "failed (unexpected)",
        ),
        ("close_rollback", json.dumps(_GHOST), ("--apply",), "failed (close_failed)"),
        (
            "close_commit",
            json.dumps(_GHOST),
            ("--apply", "--skip-not-found"),
            "failed (close_failed)",
        ),
        ("ok", '{"cards": [ ' + SENTINEL, ("--apply",), "failed (bad_input)"),
        ("ok", json.dumps([SENTINEL]), ("--apply",), "rejected:bad_document: 1"),
        ("ok", json.dumps(SENTINEL), ("--apply",), "rejected:bad_document: 1"),
    ],
)
def test_no_value_reaches_a_stream_on_any_failure_path(tmp_path, mode, stdin, args, fixed):
    _assert_quiet(_entry_point(tmp_path, mode, stdin, args), fixed)


def test_a_close_failure_after_a_commit_still_reports_the_committed_outcome(tmp_path):
    run = _entry_point(
        tmp_path, "close_commit", json.dumps(_GHOST), ("--apply", "--skip-not-found")
    )
    assert "committed:yes: 1" in run.stdout  # the data IS committed: say so
    assert "failed (close_failed)" in run.stderr and run.returncode == 1


def test_a_close_failure_after_a_rollback_reports_the_rollback(tmp_path):
    run = _entry_point(tmp_path, "close_rollback", json.dumps(_GHOST))
    assert "committed:no: 1" in run.stdout and "failed (close_failed)" in run.stderr


def test_a_clean_run_through_the_fake_driver_exits_zero(tmp_path):
    run = _entry_point(tmp_path, "ok", json.dumps({"cards": []}), ("--apply",))
    assert run.returncode == 0 and "committed:yes: 1" in run.stdout and "failed" not in run.stderr


def test_a_profile_nested_inside_fields_is_rejected_not_dropped():
    card = {"email": "fake.one@balizero.com", "fields": {"profile": {"unit": SENTINEL}}}
    assert imp.validate_card(card) == (None, "unknown_key")
