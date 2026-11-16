"""related_subjects_apply: plan row -> register request mapping and the run loop (synthetic rows)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import related_subjects_apply as apply_mod

SHA = "b" * 64
NAME = "Zz Probe Subject"
PASSPORT = "ZZ0000001"


def person(**over):
    row = {
        "subject_id": 11,
        "kind": "person",
        "tier": "T1",
        "proposable": True,
        "identifiers": [
            {"type": "passport", "value": PASSPORT, "files": 3, "independent_files": 2, "strengths": ["mrz"]}
        ],
        "mrz": {
            PASSPORT: {
                "dob": "1990-01-02", "expiry": "2031-05-06", "given": "ZZ", "surname": "PROBE",
                "nationality": "TESTLAND", "sex": "F", "tok": "x",
            }
        },
        "queue_ids": [1, 2],
        "flags": [],
    }  # fmt: skip
    row.update(over)
    return row


def company(**over):
    row = {
        "subject_id": 12,
        "kind": "company",
        "tier": "T2",
        "proposable": True,
        "identifiers": [
            {"type": "npwp", "value": "12.345.678.9-012.345", "files": 2},
            {"type": "ahu", "value": "AHU-12345.AH.01.01.TAHUN 2020"},
        ],
        "queue_ids": [],
        "flags": [],
    }
    row.update(over)
    return row


def test_person_row_maps_to_one_request_with_mrz_details() -> None:
    req, why = apply_mod.row_to_request(person(), SHA, dry_run=True)
    assert why is None
    assert req == {
        "kind": "person",
        "identifiers": [
            {
                "type": "passport",
                "value": PASSPORT,
                "evidence": {"files": 3, "independent_files": 2, "strengths": ["mrz"]},
            }
        ],
        "evidence": {"plan_sha256": SHA, "subject_ref": "11", "tier": "T1"},
        "dry_run": True,
        "display_name": "ZZ PROBE",
        "date_of_birth": "1990-01-02",
        "nationality": "TESTLAND",
        "sex": "F",
    }


def test_company_row_has_no_person_details() -> None:
    req, _ = apply_mod.row_to_request(company(), SHA, dry_run=False)
    assert req is not None and req["dry_run"] is False
    assert [i["type"] for i in req["identifiers"]] == ["ahu", "npwp"]
    assert not {"display_name", "date_of_birth", "nationality", "sex"} & set(req)


def test_non_key_identifiers_are_dropped_and_a_subject_left_bare_is_not_sent() -> None:
    row = person(
        identifiers=[
            {"type": "npwp", "value": "0123456789012345"},
            {"type": "passport", "value": "ZZ12"},
            {"type": "visa", "value": "ABC12345"},
        ],
        mrz=None,
    )
    assert apply_mod.row_to_request(row, SHA, dry_run=True) == (None, "no_key")
    bare = company(identifiers=[{"type": "ahu", "value": "AHU-12345.AH.01.01"}])
    assert apply_mod.row_to_request(bare, SHA, dry_run=True) == (None, "no_key")


def test_a_person_npwp_and_a_yearless_ahu_are_dropped_beside_real_keys() -> None:
    row = person(
        identifiers=[
            {"type": "passport", "value": PASSPORT},
            {"type": "npwp", "value": "0123456789012345"},
        ]
    )
    req, _ = apply_mod.row_to_request(row, SHA, dry_run=True)
    assert [i["type"] for i in req["identifiers"]] == ["passport"]
    mixed = company(
        identifiers=[
            {"type": "nib", "value": "1234567890123"},
            {"type": "ahu", "value": "AHU-1.AH.01.01"},
            {"type": "passport", "value": PASSPORT},
        ]
    )
    req, _ = apply_mod.row_to_request(mixed, SHA, dry_run=True)
    assert [i["type"] for i in req["identifiers"]] == ["nib"]


def test_not_proposable_unknown_kind_and_oversized_rows_are_not_sent() -> None:
    assert (
        apply_mod.row_to_request(person(proposable=False), SHA, dry_run=True)[1] == "not_proposable"
    )
    assert apply_mod.row_to_request(person(kind="robot"), SHA, dry_run=True)[1] == "unknown_kind"
    many = person(identifiers=[{"type": "passport", "value": f"ZZ00{n:05d}"} for n in range(21)])
    assert apply_mod.row_to_request(many, SHA, dry_run=True)[1] == "too_many_keys"
    assert apply_mod.row_to_request(person(tier="a b"), SHA, dry_run=True)[1] == "invalid_reference"


def test_equal_keys_in_two_spellings_are_sent_once() -> None:
    row = person(
        identifiers=[
            {"type": "passport", "value": "zz 000-0001"},
            {"type": "passport", "value": PASSPORT},
        ]
    )
    req, _ = apply_mod.row_to_request(row, SHA, dry_run=True)
    assert len(req["identifiers"]) == 1


def test_mrz_is_found_through_the_canonical_passport_and_disagreeing_birth_dates_are_left_out() -> (
    None
):
    row = person(
        identifiers=[
            {"type": "passport", "value": "ZZ0000001"},
            {"type": "passport", "value": "ZZ0000002"},
        ],
        mrz={
            "zz-0000001": {"dob": "1990-01-02", "given": "ZZ", "surname": "PROBE"},
            "ZZ0000002": {"dob": "1991-01-02"},
        },
    )
    req, _ = apply_mod.row_to_request(row, SHA, dry_run=True)
    assert req["display_name"] == "ZZ PROBE" and "date_of_birth" not in req
    bad = person(mrz={PASSPORT: {"dob": "not-a-date"}})
    req, _ = apply_mod.row_to_request(bad, SHA, dry_run=True)
    assert "date_of_birth" not in req


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Client:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.answer(json)


def _plan(tmp_path: Path, rows) -> Path:
    path = tmp_path / "plan.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WA_MIRROR_CRM_WRITE_KEY", "synthetic-key-0000")
    monkeypatch.setattr(apply_mod.time, "sleep", lambda s: None)


def _rows():
    return [
        person(subject_id=1),
        person(
            subject_id=2,
            identifiers=[{"type": "passport", "value": "ZZ0000002"}],
            mrz={"ZZ0000002": {"given": "ZZ", "surname": "PROBE"}},
        ),
        company(subject_id=3),
        person(subject_id=4, identifiers=[{"type": "npwp", "value": "0123456789012345"}], mrz=None),
        person(subject_id=5, proposable=False),
    ]


def test_dry_run_sends_dry_run_requests_prints_the_sha_and_no_personal_value(tmp_path, capsys):
    plan = _plan(tmp_path, _rows())
    client = _Client(lambda req: _Resp(200, {"outcome": "would_create", "dry_run": True}))
    code = apply_mod.main(["--plan", str(plan)], client=client)
    out = capsys.readouterr().out
    assert code == 0
    assert f"plan_sha256={apply_mod.plan_sha256(plan)}" in out
    assert (
        "would_create=3" in out
        and "skipped:no_key=1" in out
        and "subjects_left_with_no_key=1" in out
    )
    assert len(client.calls) == 3 and all(c["json"]["dry_run"] is True for c in client.calls)
    for secret in (
        NAME,
        PASSPORT,
        "ZZ PROBE",
        "ZZ0000002",
        "0123456789012345",
        "AHU-12345",
        "PROBE",
    ):
        assert secret not in out
    assert not Path(str(plan) + ".apply-ledger.jsonl").exists()


def test_requests_carry_only_the_crm_write_key_header(tmp_path):
    plan = _plan(tmp_path, [person()])
    client = _Client(lambda req: _Resp(200, {"outcome": "would_create"}))
    apply_mod.main(["--plan", str(plan)], client=client)
    call = client.calls[0]
    assert call["headers"] == {"X-CRM-Write-Key": "synthetic-key-0000"}
    assert call["url"].endswith("/api/crm/related-subjects/register")


def test_apply_needs_the_sha_the_dry_run_printed(tmp_path, capsys):
    plan = _plan(tmp_path, [person()])
    client = _Client(lambda req: _Resp(200, {"outcome": "created"}))
    assert apply_mod.main(["--plan", str(plan), "--apply"], client=client) == 2
    assert (
        apply_mod.main(["--plan", str(plan), "--apply", "--plan-sha256", "0" * 64], client=client)
        == 2
    )
    assert client.calls == []
    assert "refused" in capsys.readouterr().out


def test_apply_writes_and_the_rerun_skips_what_ended_well(tmp_path, capsys):
    plan = _plan(tmp_path, _rows())
    sha = apply_mod.plan_sha256(plan)

    def answer(req):
        ref = req["evidence"]["subject_ref"]
        if ref == "2":
            return _Resp(200, {"outcome": "conflict", "reason": "partial_overlap"})
        return _Resp(200, {"outcome": "created"})

    client = _Client(answer)
    args = ["--plan", str(plan), "--apply", "--plan-sha256", sha]
    assert apply_mod.main(args, client=client) == 0
    assert len(client.calls) == 3 and all(c["json"]["dry_run"] is False for c in client.calls)
    ledger = Path(str(plan) + ".apply-ledger.jsonl").read_text()
    entries = [json.loads(line) for line in ledger.splitlines()]
    assert {e["subject_ref"]: (e["outcome"], e["reason"]) for e in entries} == {
        "1": ("created", None),
        "2": ("conflict", "partial_overlap"),
        "3": ("created", None),
    }
    assert all(set(e) == {"plan_sha256", "subject_ref", "outcome", "reason"} for e in entries)
    assert PASSPORT not in ledger and "PROBE" not in ledger
    capsys.readouterr()
    again = _Client(answer)
    apply_mod.main(args, client=again)
    assert [c["json"]["evidence"]["subject_ref"] for c in again.calls] == ["2"]
    assert "skipped:ledger_done=2" in capsys.readouterr().out


def test_missing_key_refuses_and_a_first_call_401_stops_the_run(tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, _rows())
    client = _Client(lambda req: _Resp(401, {"detail": "unauthorized"}))
    assert apply_mod.main(["--plan", str(plan)], client=client) == 1
    assert len(client.calls) == 1
    assert "http_401=1" in capsys.readouterr().out
    monkeypatch.delenv("WA_MIRROR_CRM_WRITE_KEY")
    assert apply_mod.main(["--plan", str(plan)], client=client) == 2
