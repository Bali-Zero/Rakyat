"""related_subject_documents_apply: plan row -> documents request, preflight, the prepared
outgoing request and the run loop. Values are synthetic and generated at run time."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from scripts import related_subject_documents_apply as apply_mod
from scripts import related_subjects_apply as slice_one

PLAN_SHA = hashlib.sha256(b"plan-unit").hexdigest()
PASSPORT = "ZZ0000001"
WIDE = "\U0001d400"  # four bytes as UTF-8, twelve as an ASCII escape pair


def dh(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def fid(word: str, size: int = 33) -> str:
    return (word * 100)[:size]


def document(label: str, files=(), **over):
    data = {
        "sha256": dh(label),
        "byte_size": 4317905,
        "mime_type": "application/zzprobe",
        "doc_type": "zzdoctype",
        "first_seen_on": "2019-01-02",
        "first_channel": "drive" if files else "whatsapp",
        "drive": [{"file_id": fid(w), "seen_on": "2019-01-03"} for w in files],
    }
    data.update(over)
    return data


def row(ref="REF-1", docs=None, **over):
    data = {
        "subject_ref": ref,
        "kind": "person",
        "identifiers": [{"type": "passport", "value": PASSPORT}],
        "documents": docs if docs is not None else [document("a", files=("wordone",))],
    }
    data.update(over)
    return data


def _plan(tmp_path: Path, rows) -> Path:
    path = tmp_path / "plan.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WA_MIRROR_CRM_WRITE_KEY", "synthetic-key-0000")
    monkeypatch.setattr(apply_mod.time, "sleep", lambda s: None)


class Recorder:
    """An httpx client over a mock transport: it records every PREPARED outgoing request."""

    def __init__(self, answer):
        self.requests: list[httpx.Request] = []
        self.answer = answer
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answer(json.loads(request.content))
        return httpx.Response(status, json=body)


def _counts(documents=0, sightings=0, links=0, differences=0):
    return {
        "documents": documents,
        "sightings": sightings,
        "links": links,
        "descriptive_differences": differences,
    }


def would_link(req):
    return 200, {
        "outcome": "would_link",
        "subject_id": 7,
        "counts": _counts(1, 1, 1),
        "dry_run": True,
    }


# ── mapping and preflight ────────────────────────────────────────────────────


def test_a_row_maps_to_one_request_with_keys_only_and_the_evidence_of_the_plan():
    request, why = apply_mod.row_to_request(
        row(
            identifiers=[{"type": "passport", "value": PASSPORT, "files": 3, "strengths": ["mrz"]}]
        ),
        PLAN_SHA,
        dry_run=True,
    )
    assert why is None
    assert request == {
        "subject": {"kind": "person", "identifiers": [{"type": "passport", "value": PASSPORT}]},
        "documents": [document("a", files=("wordone",))],
        "evidence": {"plan_sha256": PLAN_SHA, "subject_ref": "REF-1"},
        "dry_run": True,
    }


def test_identifiers_are_mapped_by_the_slice_one_rules():
    mixed = row(
        identifiers=[
            {"type": "passport", "value": "zz 000-0001"},
            {"type": "passport", "value": PASSPORT},
            {"type": "npwp", "value": "0123456789012345"},
            {"type": "visa", "value": "ABC12345"},
        ]
    )
    request, _ = apply_mod.row_to_request(mixed, PLAN_SHA, dry_run=False)
    assert [i["type"] for i in request["subject"]["identifiers"]] == ["passport"]
    expected, _ = slice_one._identifier_items(mixed)
    assert request["subject"]["identifiers"] == [
        {"type": i["type"], "value": i["value"]} for i in expected
    ]
    assert apply_mod.row_to_request(row(kind="robot"), PLAN_SHA, dry_run=True)[1] == "unknown_kind"
    bare = row(identifiers=[{"type": "visa", "value": "ABC12345"}])
    assert apply_mod.row_to_request(bare, PLAN_SHA, dry_run=True)[1] == "no_key"
    many = row(identifiers=[{"type": "passport", "value": f"ZZ00{n:05d}"} for n in range(21)])
    assert apply_mod.row_to_request(many, PLAN_SHA, dry_run=True)[1] == "too_many_keys"
    assert (
        apply_mod.row_to_request(row(ref="a b"), PLAN_SHA, dry_run=True)[1] == "invalid_reference"
    )


def test_preflight_never_sends_what_the_route_would_refuse_and_counts_the_reason():
    forty = [document(f"d{n}") for n in range(40)]
    assert apply_mod.preflight(row(docs=forty), PLAN_SHA, dry_run=True)[2] is None
    assert (
        apply_mod.preflight(row(docs=forty + [document("x")]), PLAN_SHA, dry_run=True)[2]
        == "too_many_documents"
    )
    eight = document("e", files=tuple(f"word{n}" for n in range(8)))
    assert apply_mod.preflight(row(docs=[eight]), PLAN_SHA, dry_run=True)[2] is None
    nine = document("n", files=tuple(f"word{n}" for n in range(9)))
    assert apply_mod.preflight(row(docs=[nine]), PLAN_SHA, dry_run=True)[2] == "too_many_file_ids"
    assert apply_mod.preflight(row(docs=[]), PLAN_SHA, dry_run=True)[2] == "no_documents"
    broken = {k: v for k, v in document("b").items() if k != "sha256"}
    assert apply_mod.preflight(row(docs=[broken]), PLAN_SHA, dry_run=True)[2] == "invalid_document"


def test_a_body_over_the_cap_is_never_sent(monkeypatch):
    monkeypatch.setattr(apply_mod, "MAX_BODY_BYTES", 300)
    request, body, skip = apply_mod.preflight(row(), PLAN_SHA, dry_run=True)
    assert (request, body, skip) == (None, None, "body_too_large")


def _widest_row():
    identifiers = [{"type": "passport", "value": f"ZZ{n:06d}" + WIDE * 54} for n in range(20)]
    docs = [
        document(
            f"max{n}",
            mime_type="x" * 61 + "/" + "y" * 91,
            doc_type="z" * 40,
            drive=[{"file_id": fid(f"w{k}", 80), "seen_on": "2019-01-03"} for k in range(8)],
        )
        for n in range(40)
    ]
    return row(ref="R" * 64, identifiers=identifiers, docs=docs)


def test_the_body_is_measured_as_the_exact_bytes_sent_compact_utf8_without_ascii_escaping():
    request, body, skip = apply_mod.preflight(_widest_row(), PLAN_SHA, dry_run=False)
    assert skip is None and body is not None
    assert len(request["subject"]["identifiers"]) == 20 and len(request["documents"]) == 40
    assert len(body) <= apply_mod.MAX_BODY_BYTES
    assert (
        len(json.dumps(request).encode()) > apply_mod.MAX_BODY_BYTES
    )  # the escaped form would not fit
    assert body == json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert json.loads(body) == request


def test_the_widest_request_is_sent_as_those_bytes_and_not_as_a_second_serialization(tmp_path):
    seen = Recorder(would_link)
    plan = _plan(tmp_path, [_widest_row()])
    assert apply_mod.main(["--plan", str(plan)], client=seen.client) == 0
    (request,) = seen.requests
    _expected, body, _ = apply_mod.preflight(
        _widest_row(), apply_mod.plan_sha256(plan), dry_run=True
    )
    assert request.content == body
    assert len(request.content) <= apply_mod.MAX_BODY_BYTES


# ── the prepared outgoing request ────────────────────────────────────────────

TRANSPORT_DEFAULTS = {
    "host",
    "content-length",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
}


def test_the_prepared_request_carries_nothing_but_the_key_the_content_type_and_client_defaults(
    tmp_path,
):
    seen = Recorder(would_link)
    rows = [row(ref="REF-9", docs=[document("probe", files=("wordprobe",))])]
    plan = _plan(tmp_path, rows)
    apply_mod.main(["--plan", str(plan), "--base-url", "https://registry.test"], client=seen.client)
    (request,) = seen.requests
    assert request.method == "POST"
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "registry.test",
        "/api/crm/related-subjects/documents/register",
    )
    assert request.url.query == b"" and request.url.fragment == ""
    names = {k.lower() for k in request.headers.keys()}
    assert {"x-crm-write-key", "content-type"} <= names
    assert names - {"x-crm-write-key", "content-type"} <= TRANSPORT_DEFAULTS
    assert not {"cookie", "set-cookie", "authorization", "x-session-id", "x-user-email"} & names
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-crm-write-key"] == "synthetic-key-0000"
    derived = [dh("probe"), fid("wordprobe"), "REF-9", PASSPORT, apply_mod.plan_sha256(plan)]
    for name, value in request.headers.items():
        assert not any(token in value for token in derived), name
    assert not any(token in str(request.url) for token in derived)
    assert json.loads(request.content)["documents"][0]["sha256"] == dh("probe")


# ── the run loop ─────────────────────────────────────────────────────────────


def _rows():
    return [
        row(ref="REF-1"),
        row(ref="REF-2", docs=[document("b", files=("wordtwo",))]),
        row(ref="REF-3", identifiers=[{"type": "visa", "value": "ABC12345"}]),
        row(ref="REF-4", docs=[document(f"d{n}") for n in range(41)]),
    ]


def test_a_dry_run_prints_counts_reasons_sums_totals_and_no_value(tmp_path, capsys):
    rows = _rows()
    plan = _plan(tmp_path, rows)

    def answer(req):
        if req["evidence"]["subject_ref"] == "REF-2":
            return 200, {
                "outcome": "subject_not_registered",
                "reason": "none_owned",
                "dry_run": True,
            }
        return would_link(req)

    seen = Recorder(answer)
    assert apply_mod.main(["--plan", str(plan)], client=seen.client) == 0
    out = capsys.readouterr().out
    assert f"plan_sha256={apply_mod.plan_sha256(plan)}" in out
    assert "would_link=1" in out and "subject_not_registered=1" in out
    assert "reason subject_not_registered:none_owned=1" in out
    assert "skipped:no_key=1" in out and "skipped:too_many_documents=1" in out
    assert "sum would_link:documents=1" in out and "sum would_link:links=1" in out
    assert (
        "plan unique documents=2" in out
        and "plan unique sightings=2" in out
        and "plan unique links=2" in out
    )
    assert apply_mod.DRY_RUN_NOTE in out and "equals the number of pairs, not of hashes" in out
    assert len(seen.requests) == 2 and all(
        json.loads(r.content)["dry_run"] is True for r in seen.requests
    )
    for secret in (dh("a"), dh("b"), fid("wordone"), fid("wordtwo"), PASSPORT, "REF-1"):
        assert secret not in out


def test_the_plan_unique_totals_count_a_shared_hash_once_and_a_pair_per_subject(tmp_path):
    shared = document("shared", files=("wordshare",))
    rows = [
        row(ref="REF-1", docs=[shared]),
        row(ref="REF-2", identifiers=[{"type": "passport", "value": "ZZ0000002"}], docs=[shared]),
    ]
    assert apply_mod.unique_totals(rows) == {"documents": 1, "sightings": 1, "links": 2}


def test_apply_needs_the_sha_the_dry_run_printed(tmp_path, capsys):
    plan = _plan(tmp_path, [row()])
    seen = Recorder(would_link)
    assert apply_mod.main(["--plan", str(plan), "--apply"], client=seen.client) == 2
    assert (
        apply_mod.main(
            ["--plan", str(plan), "--apply", "--plan-sha256", "0" * 64], client=seen.client
        )
        == 2
    )
    assert "refused" in capsys.readouterr().out and seen.requests == []


def test_apply_writes_and_the_rerun_skips_only_what_ended_well(tmp_path, capsys):
    plan = _plan(tmp_path, _rows()[:2])
    sha = apply_mod.plan_sha256(plan)

    def answer(req):
        ref = req["evidence"]["subject_ref"]
        if ref == "REF-1":
            return 200, {
                "outcome": "linked",
                "subject_id": 7,
                "counts": _counts(1, 1, 1),
                "dry_run": False,
            }
        return 200, {"outcome": "subject_not_registered", "reason": "none_owned", "dry_run": False}

    args = ["--plan", str(plan), "--apply", "--plan-sha256", sha]
    first = Recorder(answer)
    assert apply_mod.main(args, client=first.client) == 0
    assert all(json.loads(r.content)["dry_run"] is False for r in first.requests)
    ledger = [
        json.loads(line)
        for line in (tmp_path / "plan.jsonl.docs-apply-ledger.jsonl").read_text().splitlines()
    ]
    assert {(e["subject_ref"], e["outcome"]) for e in ledger} == {
        ("REF-1", "linked"),
        ("REF-2", "subject_not_registered"),
    }
    assert apply_mod.read_ledger(tmp_path / "plan.jsonl.docs-apply-ledger.jsonl", sha) == {"REF-1"}
    capsys.readouterr()
    again = Recorder(answer)
    apply_mod.main(args, client=again.client)
    assert [json.loads(r.content)["evidence"]["subject_ref"] for r in again.requests] == ["REF-2"]
    assert "skipped:ledger_done=1" in capsys.readouterr().out


def test_terminal_outcomes_here_are_linked_and_already_linked_and_slice_one_is_unchanged(tmp_path):
    assert apply_mod.TERMINAL == {"linked", "already_linked"}
    ledger = tmp_path / "ledger.jsonl"
    for ref, outcome in (
        ("A", "linked"),
        ("B", "already_linked"),
        ("C", "would_link"),
        ("D", "created"),
    ):
        apply_mod.append_ledger(
            ledger, {"plan_sha256": PLAN_SHA, "subject_ref": ref, "outcome": outcome}
        )
    assert apply_mod.read_ledger(ledger, PLAN_SHA) == {"A", "B"}
    assert slice_one.read_ledger(ledger, PLAN_SHA) == {"D"}  # the slice-1 default is kept
    assert slice_one.read_ledger(ledger, PLAN_SHA, {"linked"}) == {"A"}


def test_a_missing_key_refuses_and_a_first_call_401_stops_the_run(tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, _rows()[:2])
    seen = Recorder(lambda req: (401, {"detail": "unauthorized"}))
    assert apply_mod.main(["--plan", str(plan)], client=seen.client) == 1
    assert len(seen.requests) == 1 and "stopped: first call answered 401" in capsys.readouterr().out
    monkeypatch.delenv("WA_MIRROR_CRM_WRITE_KEY")
    assert apply_mod.main(["--plan", str(plan)], client=seen.client) == 2


@pytest.mark.parametrize(
    "answer",
    [
        (200, {"outcome": "linked-by-Zz-Leaky-Value", "reason": "leaky-reason-text"}),
        (
            200,
            {
                "outcome": "would_link",
                "reason": "leaky-reason-text",
                "counts": {"documents": "leaky"},
            },
        ),
        (503, {"detail": "database says leaky-text"}),
        (418, {"detail": "leaky-text"}),
        (200, {"counts": {"documents": True, "sightings": -1}}),
    ],
)
def test_unrecognised_answer_text_never_reaches_the_ledger_or_stdout(tmp_path, capsys, answer):
    plan = _plan(tmp_path, [row()])
    sha = apply_mod.plan_sha256(plan)
    seen = Recorder(lambda req: answer)
    apply_mod.main(
        ["--plan", str(plan), "--apply", "--plan-sha256", sha, "--limit", "1"], client=seen.client
    )
    shown = capsys.readouterr().out + (tmp_path / "plan.jsonl.docs-apply-ledger.jsonl").read_text()
    assert "leaky" not in shown.lower() and "Zz-Leaky" not in shown
    assert "sum would_link:documents" not in shown and "sum unknown:documents" not in shown


def test_known_codes_pass_through_unchanged(tmp_path, capsys):
    plan = _plan(tmp_path, [row()])
    seen = Recorder(
        lambda req: (200, {"outcome": "conflict", "reason": "document_differs", "dry_run": True})
    )
    apply_mod.main(["--plan", str(plan)], client=seen.client)
    assert "reason conflict:document_differs=1" in capsys.readouterr().out
    busy = Recorder(lambda req: (503, {"detail": "registry_busy"}))
    apply_mod.main(["--plan", str(plan)], client=busy.client)
    assert "reason http_503:registry_busy=1" in capsys.readouterr().out
