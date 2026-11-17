"""related_subject_documents_apply (Addendum A v1.3): request mapping, preflight, the prepared
outgoing request, `classify`, and the run loop through a mock transport. Values are synthetic and
generated at run time."""

from __future__ import annotations

import hashlib
import inspect
import itertools
import json
import re
from pathlib import Path

import httpx
import pytest

from scripts import related_subject_documents_apply as apply_mod
from scripts import related_subjects_apply as slice_one

PLAN_SHA = hashlib.sha256(b"plan-unit").hexdigest()
PASSPORT = "ZZ0000001"
SENT = "ZzSentinelValue"
WIDE = "\U0001d400"  # four bytes as UTF-8, twelve as an ASCII escape pair
URL = "https://registry.test/api/crm/related-subjects/documents/register"
TRANSPORT_DEFAULTS = {
    "host",
    "content-length",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
}
ALLOWED_NAMES = {"x-crm-write-key", "content-type"} | TRANSPORT_DEFAULTS


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


def row(ref="1", docs=None, **over):
    data = {
        "subject_ref": ref,
        "kind": "person",
        "identifiers": [{"type": "passport", "value": PASSPORT}],
        "documents": docs if docs is not None else [document("a", files=("wordone",))],
    }
    data.update(over)
    return data


def bad_row(ref="2"):
    """A row that preflight refuses (no key)."""
    return row(ref=ref, identifiers=[{"type": "visa", "value": "ABC12345"}])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WA_MIRROR_CRM_WRITE_KEY", "synthetic-key-0000")
    monkeypatch.setattr(apply_mod.time, "sleep", lambda s: None)


class Raw:
    def __init__(self, text):
        self.text = text


class Recorder:
    """An httpx client over a mock transport; records every PREPARED outgoing request.

    ``answer(request_json, call_number)`` returns ``(status, body[, headers])`` or an exception
    instance to raise from the transport.
    """

    def __init__(self, answer, client_class=httpx.Client):
        self.requests: list[httpx.Request] = []
        self.answer = answer
        self.calls = 0
        self.client = client_class(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.calls += 1
        result = self.answer(json.loads(request.content), self.calls)
        if isinstance(result, BaseException):
            raise result
        status, body, *extra = result
        headers = extra[0] if extra else None
        if isinstance(body, Raw):
            return httpx.Response(status, content=body.text.encode(), headers=headers)
        return httpx.Response(status, json=body, headers=headers)

    @property
    def refs(self):
        return [json.loads(r.content)["evidence"]["subject_ref"] for r in self.requests]


def good(req, n=0):
    outcome = "would_link" if req["dry_run"] else "linked"
    return 200, {"outcome": outcome, "dry_run": req["dry_run"]}


def ok_label(apply_):
    return "linked" if apply_ else "would_link"


# ── the output schema (A.4): every line is one of these ──────────────────────

LABELS = (
    "subject_not_registered|conflict|would_link|linked|already_linked|"
    "http_401|http_422|http_503|http_0|http_other|malformed"
)
SKIPS = (
    "unknown_kind|no_key|too_many_keys|invalid_reference|no_documents|too_many_documents|"
    "invalid_document|too_many_file_ids|body_too_large"
)
LINE_PATTERNS = [
    re.compile(p)
    for p in (
        r"plan_sha256=[0-9a-f]{64}",
        r"mode=(apply|dry-run) sent=[0-9]+",
        r"plan unique (documents|sightings|links)=[0-9]+",
        rf"({LABELS})=[0-9]+",
        rf"skipped:({SKIPS})=[0-9]+",
        r"refused: --apply needs the plan's sha256 as printed by the dry run",
        r"refused: the write key is not set",
        r"refused: the plan cannot be read as a plan",
        r"refused: invalid command line",
        r"stopped: the first call was refused as unauthorized",
        r"stopped: the first call found the registry unavailable",
        r"stopped: a prepared request was refused before sending",
        r"failed: unexpected producer failure",
    )
]


def assert_schema(out: str, err: str = ""):
    for line in out.splitlines():
        assert any(p.fullmatch(line) for p in LINE_PATTERNS), "unexpected output line"
    assert err == ""


class Result:
    def __init__(self, code, out, err, rec, plan, before):
        self.code, self.out, self.err, self.rec, self.plan = code, out, err, rec, plan
        self.before = before

    def counters(self):
        found = {}
        for line in self.out.splitlines():
            name, sep, number = line.rpartition("=")
            if sep and name and number.isdigit() and not line.startswith("plan_sha256"):
                found[name] = int(number)
        return found


def write_plan(tmp_path: Path, rows) -> Path:
    path = tmp_path / "plan.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def run(tmp_path, capsys, rows, answer=good, *extra, apply_=False, client=None, plan=None):
    plan = plan or write_plan(tmp_path, rows)
    before = sorted(p.name for p in tmp_path.iterdir())
    rec = Recorder(answer) if client is None else client
    args = ["--plan", str(plan), "--pace", "0", *extra]
    if apply_:
        args += ["--apply", "--plan-sha256", hashlib.sha256(plan.read_bytes()).hexdigest()]
    code = apply_mod.main(args, client=rec.client)
    captured = capsys.readouterr()
    assert_schema(captured.out, captured.err)
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # the producer writes no file
    return Result(code, captured.out, captured.err, rec, plan, before)


# ── request mapping and preflight (kept, updated for references) ─────────────


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
        "evidence": {"plan_sha256": PLAN_SHA, "subject_ref": "1"},
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
    expected, _ = slice_one._identifier_items(mixed)
    assert request["subject"]["identifiers"] == [
        {"type": i["type"], "value": i["value"]} for i in expected
    ]
    assert [i["type"] for i in request["subject"]["identifiers"]] == ["passport"]
    assert apply_mod.row_to_request(row(kind="robot"), PLAN_SHA, dry_run=True)[1] == "unknown_kind"
    assert apply_mod.row_to_request(bad_row(), PLAN_SHA, dry_run=True)[1] == "no_key"
    many = row(identifiers=[{"type": "passport", "value": f"ZZ00{n:05d}"} for n in range(21)])
    assert apply_mod.row_to_request(many, PLAN_SHA, dry_run=True)[1] == "too_many_keys"


def test_preflight_never_sends_what_the_route_would_refuse_and_names_the_reason():
    forty = [document(f"d{n}") for n in range(40)]
    assert apply_mod.preflight(row(docs=forty), PLAN_SHA, dry_run=True)[2] is None
    over = apply_mod.preflight(row(docs=forty + [document("x")]), PLAN_SHA, dry_run=True)
    assert over[2] == "too_many_documents"
    eight = document("e", files=tuple(f"word{n}" for n in range(8)))
    assert apply_mod.preflight(row(docs=[eight]), PLAN_SHA, dry_run=True)[2] is None
    nine = document("n", files=tuple(f"word{n}" for n in range(9)))
    assert apply_mod.preflight(row(docs=[nine]), PLAN_SHA, dry_run=True)[2] == "too_many_file_ids"
    assert apply_mod.preflight(row(docs=[]), PLAN_SHA, dry_run=True)[2] == "no_documents"
    broken = {k: v for k, v in document("b").items() if k != "sha256"}
    assert apply_mod.preflight(row(docs=[broken]), PLAN_SHA, dry_run=True)[2] == "invalid_document"


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
    return row(ref="999999999", identifiers=identifiers, docs=docs)


def test_the_body_is_measured_as_the_exact_bytes_sent_compact_utf8_without_ascii_escaping():
    request, body, skip = apply_mod.preflight(_widest_row(), PLAN_SHA, dry_run=False)
    assert skip is None and body is not None
    assert len(request["subject"]["identifiers"]) == 20 and len(request["documents"]) == 40
    assert len(body) <= apply_mod.MAX_BODY_BYTES
    assert len(json.dumps(request).encode()) > apply_mod.MAX_BODY_BYTES
    assert body == json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def test_the_widest_request_is_sent_as_those_bytes_and_not_as_a_second_serialization(
    tmp_path, capsys
):
    res = run(tmp_path, capsys, [_widest_row()])
    sha = hashlib.sha256(res.plan.read_bytes()).hexdigest()
    (request,) = res.rec.requests
    assert request.content == apply_mod.preflight(_widest_row(), sha, dry_run=True)[1]
    assert res.code == 0


def test_the_prepared_request_carries_nothing_but_the_key_the_content_type_and_client_defaults(
    tmp_path, capsys
):
    res = run(
        tmp_path,
        capsys,
        [row(ref="9", docs=[document("probe", files=("wordprobe",))])],
        good,
        "--base-url",
        "https://registry.test",
    )
    (request,) = res.rec.requests
    assert request.method == "POST"
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "registry.test",
        "/api/crm/related-subjects/documents/register",
    )
    assert request.url.query == b"" and request.url.fragment == ""
    names = {k.lower() for k in request.headers.keys()}
    assert {"x-crm-write-key", "content-type"} <= names and names <= ALLOWED_NAMES
    assert request.headers["content-type"] == "application/json"
    derived = [
        dh("probe"),
        fid("wordprobe"),
        PASSPORT,
        hashlib.sha256(res.plan.read_bytes()).hexdigest(),
    ]
    for name, value in request.headers.items():
        assert not any(token in value for token in derived), name
    assert not any(token in str(request.url) for token in derived)


# ── cookie and prepared-request guards (kept) ────────────────────────────────


def _assert_clean(requests):
    assert requests
    for request in requests:
        names = {k.lower() for k in request.headers.keys()}
        assert "cookie" not in names
        assert {"x-crm-write-key", "content-type"} <= names and names <= ALLOWED_NAMES


def test_a_cookie_set_by_an_answer_is_not_sent_with_the_next_subject(tmp_path, capsys):
    cookie = {"Set-Cookie": f"probe={fid('wordcookie')}; Path=/"}
    res = run(
        tmp_path,
        capsys,
        [row(ref="1"), row(ref="2", docs=[document("b", files=("wordtwo",))])],
        lambda req, n: (*good(req), cookie),
    )
    assert res.code == 0 and len(res.rec.requests) == 2
    _assert_clean(res.rec.requests)


def test_a_cookie_set_by_a_503_is_not_sent_with_the_retry(tmp_path, capsys):
    cookie = {"Set-Cookie": f"probe={fid('wordcookie')}; Path=/"}

    def answer(req, n):
        return (503, {}, cookie) if n == 1 else (*good(req), cookie)

    res = run(tmp_path, capsys, [row(ref="1"), row(ref="2")], answer)
    assert res.rec.refs == ["1", "1", "2"]
    _assert_clean(res.rec.requests)


class Tamper(httpx.Client):
    """Adds a header to the prepared request at the build numbers in ``on``."""

    on: set[int] = set()
    builds = 0

    def build_request(self, *a, **kw):
        request = super().build_request(*a, **kw)
        type(self).builds += 1
        if type(self).builds in type(self).on:
            request.headers["X-Plan-Ref"] = SENT
        return request


def tamper(on):
    class T(Tamper):
        pass

    T.on, T.builds = set(on), 0
    return T


def test_check_prepared_refuses_a_cookie_and_an_unknown_header():
    ok = {"X-CRM-Write-Key": "k", "Content-Type": "application/json"}
    slice_one.check_prepared(httpx.Request("POST", "https://x.test/", headers=ok))
    for extra in ({"Cookie": "a=b"}, {"X-Plan-Ref": "1"}):
        with pytest.raises(slice_one.UnsafeRequest):
            slice_one.check_prepared(
                httpx.Request("POST", "https://x.test/", headers={**ok, **extra})
            )


# ── 1. classify as a pure function, by a table written here ──────────────────


class Int200(int):
    pass


NON_200_STATUSES = [
    (True, "http_other"), (False, "http_other"), (None, "http_other"), ("200", "http_other"),
    (200.0, "http_other"), (401.0, "http_other"), (0.0, "http_other"),
    (Int200(200), "http_other"), (-1, "http_other"), (0, "http_0"), (99, "http_other"),
    (100, "http_other"), (199, "http_other"), (201, "http_other"), (204, "http_other"),
    (301, "http_other"), (400, "http_other"), (401, "http_401"), (404, "http_other"),
    (422, "http_422"), (500, "http_other"), (503, "http_503"), (599, "http_other"),
    (600, "http_other"), (10**30, "http_other"),
]  # fmt: skip
FIVE = ["subject_not_registered", "conflict", "would_link", "linked", "already_linked"]


@pytest.mark.parametrize("sent_dry", [True, False])
@pytest.mark.parametrize(
    ("status", "label"), NON_200_STATUSES, ids=[repr(s) for s, _ in NON_200_STATUSES]
)
def test_classify_every_status_other_than_200(status, label, sent_dry):
    well_formed = {"outcome": "linked", "dry_run": sent_dry}
    assert apply_mod.classify(status, well_formed, sent_dry) == label
    assert apply_mod.classify(status, {}, sent_dry) == label
    assert apply_mod.classify(status, None, sent_dry) == label


OUTCOME_VALUES = [None, 5, [], {}, "", "LINKED", "other", *FIVE, "missing"]
DRY_VALUES = ["missing", None, 0, 1, "true", True, False]


def expected_for_200(outcome, dry, sent_dry):
    if not isinstance(outcome, str) or outcome not in FIVE:
        return "malformed"
    if dry is True or dry is False:
        if dry != sent_dry:
            return "malformed"
    else:
        return "malformed"
    if outcome == "would_link" and not sent_dry:
        return "malformed"
    if outcome == "linked" and sent_dry:
        return "malformed"
    return outcome


@pytest.mark.parametrize("sent_dry", [True, False])
@pytest.mark.parametrize("dry", DRY_VALUES, ids=repr)
@pytest.mark.parametrize("outcome", OUTCOME_VALUES, ids=repr)
def test_classify_status_200_crossed_with_outcome_dry_run_and_mode(outcome, dry, sent_dry):
    body = {}
    if outcome != "missing":
        body["outcome"] = outcome
    if dry != "missing":
        body["dry_run"] = dry
    expected = expected_for_200(outcome if outcome != "missing" else None, dry, sent_dry)
    assert apply_mod.classify(200, body, sent_dry) == expected


def test_classify_gives_the_prescribed_label_for_bodies_with_several_defects_and_raises_nothing():
    for body in (
        {"outcome": 5, "dry_run": "x", "counts": [1], "reason": None},
        {"outcome": ["linked"], "dry_run": [True]},
        [],
        None,
        7,
        "text",
    ):
        assert apply_mod.classify(200, body, True) == "malformed"
    assert apply_mod.classify(True, [], True) == "http_other"
    assert len(set(apply_mod.OUTCOMES) | apply_mod.FAILING) == 11


# ── 2. forbidden reads ───────────────────────────────────────────────────────


class Recording(dict):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reads: list = []

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.reads.append(key)
        return super().__contains__(key)

    def __iter__(self):
        self.reads.append("*iter")
        return super().__iter__()

    def keys(self):
        self.reads.append("*keys")
        return super().keys()

    def items(self):
        self.reads.append("*items")
        return super().items()

    def values(self):
        self.reads.append("*values")
        return super().values()


EXTRAS = {
    "counts": {"documents": 3201234567890123, "sightings": True, "links": -1},
    "reason": "leaky-reason",
    "detail": SENT,
    "subject_id": 3201234567890123,
    SENT: [SENT],
}


@pytest.mark.parametrize("status", [401, 422, 503, 0, 500, 201])
def test_a_non_200_status_reads_no_key(status):
    body = Recording({"outcome": "linked", "dry_run": False, **EXTRAS})
    apply_mod.classify(status, body, False)
    assert body.reads == []


@pytest.mark.parametrize("sent_dry", [True, False])
def test_status_200_reads_outcome_and_dry_run_and_no_other(sent_dry):
    body = Recording({"outcome": ok_label(not sent_dry), "dry_run": sent_dry, **EXTRAS})
    assert apply_mod.classify(200, body, sent_dry) == ok_label(not sent_dry)
    assert sorted(body.reads) == ["dry_run", "outcome"]
    early = Recording({"outcome": "other", **EXTRAS})
    apply_mod.classify(200, early, sent_dry)
    assert set(early.reads) <= {"outcome", "dry_run"}


WELL_FORMED = [
    (outcome, apply_)
    for outcome in FIVE
    for apply_ in (False, True)
    if not (outcome == "would_link" and apply_) and not (outcome == "linked" and not apply_)
]


@pytest.mark.parametrize(("outcome", "apply_"), WELL_FORMED)
def test_extra_keys_in_a_well_formed_answer_change_neither_the_label_nor_a_byte_of_output(
    tmp_path, capsys, outcome, apply_
):
    def plain(req, n):
        return 200, {"outcome": outcome, "dry_run": req["dry_run"]}

    def extended(req, n):
        return 200, {"outcome": outcome, "dry_run": req["dry_run"], **EXTRAS}, {"X-Leak": SENT}

    first = run(tmp_path, capsys, [row()], plain, apply_=apply_)
    second = run(tmp_path, capsys, [row()], extended, apply_=apply_)
    assert (first.code, first.out, first.err) == (second.code, second.out, second.err)
    assert SENT not in second.out and "3201234567890123" not in second.out


# ── 3. wire level, through `send` with an injected transport ─────────────────

BODY = apply_mod.encode(apply_mod.row_to_request(row(), PLAN_SHA, dry_run=True)[0])


@pytest.mark.parametrize(
    "body",
    [[], None, 7, "text", Raw("not json at all"), {}],
    ids=["list", "null", "number", "string", "text", "empty-object"],
)
def test_a_200_answer_that_is_not_an_object_with_the_fields_is_malformed_without_a_retry(body):
    rec = Recorder(lambda req, n: (200, body))
    status, answer = slice_one.send(rec.client, URL, "k", BODY)
    assert (status, answer) == (200, {}) and rec.calls == 1
    assert apply_mod.classify(status, answer, True) == "malformed"


BOOM = httpx.ConnectError("boom")
SEQUENCES = {
    "503-200": ([503, 200], 200, True),
    "exc-200": ([BOOM, 200], 200, True),
    "503-exc": ([503, BOOM], 0, False),
    "exc-503": ([BOOM, 503], 503, False),
    "exc-exc": ([BOOM, BOOM], 0, False),
    "503-503": ([503, 503], 503, False),
}


def _scripted(steps):
    def answer(req, n):
        step = steps[n - 1]
        if isinstance(step, BaseException):
            return step
        return (
            (200, {"outcome": "would_link", "dry_run": True})
            if step == 200
            else (
                step,
                {},
                {"Set-Cookie": f"probe={fid('wordcookie')}; Path=/"},
            )
        )

    return answer


@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_send_makes_at_most_two_attempts_and_returns_the_final_pair(name):
    steps, final, has_body = SEQUENCES[name]
    rec = Recorder(_scripted(steps))
    status, answer = slice_one.send(rec.client, URL, "k", BODY)
    assert status == final and rec.calls == 2
    assert (answer != {}) == has_body
    assert [r.content for r in rec.requests] == [BODY, BODY]
    _assert_clean(rec.requests)


@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_a_run_counts_one_sent_and_one_label_per_invocation_whatever_the_retry(
    tmp_path, capsys, name
):
    steps, final, _ = SEQUENCES[name]
    res = run(tmp_path, capsys, [row()], _scripted(steps))
    label = apply_mod.classify(
        final, {"outcome": "would_link", "dry_run": True} if final == 200 else {}, True
    )
    assert res.rec.calls == 2 and "mode=dry-run sent=1" in res.out
    assert res.counters()[label] == 1
    assert sum(v for k, v in res.counters().items() if k in LABELS.split("|")) == 1
    _assert_clean(res.rec.requests)


def test_a_failure_while_preparing_or_processing_follows_the_exception_path():
    class PrepareFails(httpx.Client):
        calls = 0

        def build_request(self, *a, **kw):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError(SENT)
            return super().build_request(*a, **kw)

    rec = Recorder(lambda req, n: good(req), client_class=PrepareFails)
    assert slice_one.send(rec.client, URL, "k", BODY)[0] == 200 and rec.calls == 1

    class BadResponse:
        status_code = 200

        def json(self):
            raise RuntimeError(SENT)

    class ProcessFails(httpx.Client):
        sends = 0

        def send(self, request, **kw):
            type(self).sends += 1
            if type(self).sends == 1:
                return BadResponse()
            return super().send(request, **kw)

    rec = Recorder(lambda req, n: good(req), client_class=ProcessFails)
    assert slice_one.send(rec.client, URL, "k", BODY)[0] == 200 and rec.calls == 1

    class AlwaysFails(httpx.Client):
        def build_request(self, *a, **kw):
            raise RuntimeError(SENT)

    rec = Recorder(lambda req, n: good(req), client_class=AlwaysFails)
    assert slice_one.send(rec.client, URL, "k", BODY) == (0, {}) and rec.calls == 0


# ── 4. runs, both modes, for each failing label: good, bad, good ─────────────

BAD_ANSWERS = {
    "http_401": lambda req: (401, {}),
    "http_422": lambda req: (422, {}),
    "http_503": lambda req: (503, {}),
    "http_0": lambda req: BOOM,
    "http_other": lambda req: (500, {}),
    "malformed": lambda req: (200, []),
}


@pytest.mark.parametrize("apply_", [False, True])
@pytest.mark.parametrize("label", sorted(BAD_ANSWERS))
def test_a_failing_label_in_the_middle_fails_the_run_and_the_later_row_is_still_sent(
    tmp_path, capsys, label, apply_
):
    def answer(req, n):
        if req["evidence"]["subject_ref"] == "2":
            return BAD_ANSWERS[label](req)
        return good(req)

    res = run(tmp_path, capsys, [row("1"), row("2"), row("3")], answer, apply_=apply_)
    retried = label in ("http_503", "http_0")
    assert res.rec.refs == ["1", "2", "2", "3"] if retried else ["1", "2", "3"]
    counters = res.counters()
    assert counters[ok_label(apply_)] == 2 and counters[label] == 1
    assert "sent=3" in res.out
    assert res.code == 1
    assert {k for k in counters if k in LABELS.split("|")} == {ok_label(apply_), label}


@pytest.mark.parametrize("apply_", [False, True])
def test_answers_of_the_registry_that_are_not_failures_exit_zero(tmp_path, capsys, apply_):
    def answer(req, n):
        ref = req["evidence"]["subject_ref"]
        if ref == "2":
            return 200, {"outcome": "subject_not_registered", "dry_run": req["dry_run"]}
        if ref == "3":
            return 200, {"outcome": "conflict", "dry_run": req["dry_run"]}
        if ref == "4":
            return 200, {"outcome": "already_linked", "dry_run": req["dry_run"]}
        return good(req)

    res = run(tmp_path, capsys, [row("1"), row("2"), row("3"), row("4")], answer, apply_=apply_)
    assert res.code == 0
    counters = res.counters()
    assert (
        counters["subject_not_registered"],
        counters["conflict"],
        counters["already_linked"],
    ) == (1, 1, 1)
    assert counters[ok_label(apply_)] == 1


def test_an_earlier_failure_is_not_cleared_by_a_later_good_row(tmp_path, capsys):
    res = run(
        tmp_path,
        capsys,
        [row("1"), row("2"), row("3"), row("4")],
        lambda req, n: (500, {}) if req["evidence"]["subject_ref"] == "1" else good(req),
    )
    assert res.code == 1 and res.counters()["http_other"] == 1 and res.counters()["would_link"] == 3


# ── 5. the plan ──────────────────────────────────────────────────────────────

REFERENCES = [
    (7, "7"), ("7", "7"), ("007", "007"), (0, "0"), (999999999, "999999999"),
    (1000000000, None), ("1234567890", None), (True, None), (7.0, None),
    ("٣٤٥", None), ("7 ", None), ("7a", None), ("", None), (-1, None), (None, None),
]  # fmt: skip


@pytest.mark.parametrize(("value", "accepted"), REFERENCES, ids=[repr(v) for v, _ in REFERENCES])
def test_references_are_accepted_or_refused_as_written_and_the_evidence_carries_the_string(
    tmp_path, capsys, value, accepted
):
    res = run(tmp_path, capsys, [row(ref=value)])
    if accepted is None:
        assert res.code == 1 and res.rec.requests == []
        assert res.counters()["skipped:invalid_reference"] == 1
    else:
        assert res.code == 0 and res.rec.refs == [accepted]
        assert res.counters()["would_link"] == 1


def test_two_rows_with_the_same_reference_are_two_independent_rows(tmp_path, capsys):
    res = run(tmp_path, capsys, [row("5"), row("5")])
    assert res.rec.refs == ["5", "5"] and res.counters()["would_link"] == 2


def test_an_empty_plan_exits_zero_and_sends_nothing(tmp_path, capsys):
    for content in ("", "\n\n  \n"):
        (tmp_path / "plan.jsonl").write_text(content)
        res = run(tmp_path, capsys, [], plan=tmp_path / "plan.jsonl")
        assert res.code == 0 and res.rec.requests == [] and "mode=dry-run sent=0" in res.out


def test_a_run_whose_only_defect_is_one_skipped_row_exits_one(tmp_path, capsys):
    res = run(tmp_path, capsys, [row("1"), bad_row("2")])
    assert res.code == 1
    assert res.counters() == {
        "mode=dry-run sent": 1,
        "would_link": 1,
        "skipped:no_key": 1,
        "plan unique documents": 1,
        "plan unique sightings": 1,
        "plan unique links": 1,
    }


@pytest.mark.parametrize(
    "content",
    [b"not json\n", b'["a list"]\n', b"7\n", b"\xff\xfe\xfa\n", b'{"a": 1}\n{broken\n'],
    ids=["not-json", "list", "number", "not-utf8", "broken-second-line"],
)
def test_a_plan_that_cannot_be_read_as_rows_is_refused_before_any_call(tmp_path, capsys, content):
    plan = tmp_path / "plan.jsonl"
    plan.write_bytes(content)
    res = run(tmp_path, capsys, [], plan=plan)
    assert res.code == 2 and res.rec.requests == []
    assert "plan_sha256" not in res.out and "cannot be read as a plan" in res.out


def test_the_plan_is_opened_exactly_once_and_a_replaced_file_changes_nothing(
    tmp_path, capsys, monkeypatch
):
    plan = write_plan(tmp_path, [row("1"), row("2")])
    original = hashlib.sha256(plan.read_bytes()).hexdigest()
    opened = []
    real_open = Path.open
    monkeypatch.setattr(
        Path, "open", lambda self, *a, **k: opened.append(1) or real_open(self, *a, **k)
    )
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("plan read as text"))

    def answer(req, n):
        if n == 1:
            real_write(plan, [row("9", docs=[document("other")])])
        return good(req)

    def real_write(path, rows):
        with __import__("builtins").open(path, "w") as handle:
            handle.write("".join(json.dumps(r) + "\n" for r in rows))

    rec = Recorder(answer)
    code = apply_mod.main(["--plan", str(plan), "--pace", "0"], client=rec.client)
    out = capsys.readouterr().out
    assert code == 0 and opened == [1]
    assert f"plan_sha256={original}" in out
    assert rec.refs == ["1", "2"]
    assert {json.loads(r.content)["evidence"]["plan_sha256"] for r in rec.requests} == {original}


# ── 6. limit ─────────────────────────────────────────────────────────────────


def test_only_returned_invocations_count_toward_the_limit(tmp_path, capsys):
    rows = [bad_row("1"), bad_row("2"), row("3"), row("4"), row("5")]
    res = run(tmp_path, capsys, rows, good, "--limit", "2")
    assert res.rec.refs == ["3", "4"] and "mode=dry-run sent=2" in res.out
    assert res.counters()["skipped:no_key"] == 2 and res.code == 1


def test_a_refused_row_after_the_limit_adds_no_counter_and_does_not_fail_the_run(tmp_path, capsys):
    res = run(tmp_path, capsys, [row("1"), row("2"), bad_row("3")], good, "--limit", "2")
    assert res.code == 0 and "skipped" not in res.out and res.rec.refs == ["1", "2"]


# ── 7. stops ─────────────────────────────────────────────────────────────────

UNAUTH = "stopped: the first call was refused as unauthorized"
UNAVAIL = "stopped: the first call found the registry unavailable"


def test_a_first_returned_401_stops_the_loop_even_after_skipped_rows(tmp_path, capsys):
    res = run(tmp_path, capsys, [bad_row("1"), row("2"), row("3")], lambda req, n: (401, {}))
    assert UNAUTH in res.out and res.rec.refs == ["2"] and res.code == 1
    assert res.counters()["http_401"] == 1 and res.counters()["skipped:no_key"] == 1


def test_a_first_returned_503_after_its_retry_stops_the_loop(tmp_path, capsys):
    res = run(tmp_path, capsys, [bad_row("1"), row("2"), row("3")], lambda req, n: (503, {}))
    assert UNAVAIL in res.out and res.rec.refs == ["2", "2"] and res.code == 1
    assert res.counters()["http_503"] == 1


def test_a_first_503_then_200_is_not_a_stop(tmp_path, capsys):
    res = run(
        tmp_path, capsys, [row("1"), row("2")], lambda req, n: (503, {}) if n == 1 else good(req)
    )
    assert "stopped" not in res.out and res.rec.refs == ["1", "1", "2"] and res.code == 0


def test_a_first_call_whose_retry_ends_in_401_stops(tmp_path, capsys):
    res = run(
        tmp_path, capsys, [row("1"), row("2")], lambda req, n: (503, {}) if n == 1 else (401, {})
    )
    assert UNAUTH in res.out and res.rec.refs == ["1", "1"] and res.code == 1


@pytest.mark.parametrize("status", [401, 503])
def test_the_same_label_on_a_later_call_does_not_stop_the_loop(tmp_path, capsys, status):
    def answer(req, n):
        return (status, {}) if req["evidence"]["subject_ref"] == "2" else good(req)

    res = run(tmp_path, capsys, [row("1"), row("2"), row("3")], answer)
    assert "stopped" not in res.out and res.rec.refs[-1] == "3" and res.code == 1


@pytest.mark.parametrize("builds", [{2}, {3}], ids=["first-attempt-of-a-later-row", "retry"])
def test_a_refused_prepared_request_ends_the_run_with_a_fixed_message_and_no_summary(
    tmp_path, capsys, builds
):
    first_503 = builds == {3}

    def answer(req, n):
        return (503, {}) if first_503 and n == 2 else good(req)

    rec = Recorder(answer, client_class=tamper(builds))
    res = run(tmp_path, capsys, [row("1"), row("2"), row("3")], client=rec)
    assert res.code == 1 and "a prepared request was refused before sending" in res.out
    assert "mode=" not in res.out and "plan unique" not in res.out
    assert SENT not in res.out + res.err


# ── 8. failures outside `send` ───────────────────────────────────────────────


def test_an_unhashable_document_field_is_a_fixed_diagnostic_and_exit_one(tmp_path, capsys):
    res = run(tmp_path, capsys, [row(docs=[document("a", sha256=[SENT])])])
    assert res.code == 1 and "failed: unexpected producer failure" in res.out
    assert SENT not in res.out + res.err and res.rec.requests == []


def test_a_serialization_failure_is_a_fixed_diagnostic_and_exit_one(tmp_path, capsys, monkeypatch):
    def boom(request):
        raise TypeError(SENT)

    monkeypatch.setattr(apply_mod, "encode", boom)
    res = run(tmp_path, capsys, [row()])
    assert res.code == 1 and "failed: unexpected producer failure" in res.out
    assert SENT not in res.out + res.err and "mode=" not in res.out


@pytest.mark.parametrize(
    "extra",
    [["--bogus", SENT], ["--limit", SENT], ["--pace", SENT], [SENT], ["--limit", "-3"]],
    ids=["unknown-option", "limit", "pace", "positional", "negative-limit"],
)
def test_an_invalid_command_line_is_refused_and_echoes_nothing(tmp_path, capsys, extra):
    plan = write_plan(tmp_path, [row()])
    rec = Recorder(good)
    code = apply_mod.main(["--plan", str(plan), *extra], client=rec.client)
    captured = capsys.readouterr()
    assert code == 2 and rec.requests == []
    assert SENT not in captured.out + captured.err and "-3" not in captured.out + captured.err
    assert captured.out.strip() == "refused: invalid command line" and captured.err == ""
    assert apply_mod.main([], client=rec.client) == 2
    capsys.readouterr()


def test_apply_needs_the_sha_and_the_key_is_needed(tmp_path, capsys, monkeypatch):
    plan = write_plan(tmp_path, [row()])
    rec = Recorder(good)
    assert apply_mod.main(["--plan", str(plan), "--apply"], client=rec.client) == 2
    assert (
        apply_mod.main(
            ["--plan", str(plan), "--apply", "--plan-sha256", "0" * 64], client=rec.client
        )
        == 2
    )
    monkeypatch.delenv("WA_MIRROR_CRM_WRITE_KEY")
    assert apply_mod.main(["--plan", str(plan)], client=rec.client) == 2
    out = capsys.readouterr().out
    assert "refused" in out and rec.requests == []
    assert_schema(out)


# ── 9. output schema with sentinels everywhere ───────────────────────────────


def test_sentinels_in_answers_headers_plan_fields_and_references_never_reach_the_output(
    tmp_path, capsys
):
    rows = [
        {**row("1"), SENT: SENT, "extra": [SENT]},
        row(ref=SENT),
        {**row("3"), "kind": SENT},
        row("4", docs=[document("a", files=("wordone",), mime_type=SENT)]),
    ]

    def answer(req, n):
        return 200, {"outcome": "would_link", "dry_run": True, "detail": SENT}, {"X-Leak": SENT}

    res = run(tmp_path, capsys, rows, answer)
    assert SENT not in res.out + res.err
    assert (
        res.counters()["skipped:invalid_reference"] == 1
        and res.counters()["skipped:unknown_kind"] == 1
    )


# ── 10. totals ───────────────────────────────────────────────────────────────


def _shared_rows():
    a, b, c = (
        document("ta", files=("wordf1", "wordf2")),
        document("tb", files=("wordf3",)),
        document("tc", files=("wordf3",)),
    )
    a_again = document("ta", files=("wordf1",))
    return [
        row("1", docs=[a, b]),
        row("2", identifiers=[{"type": "passport", "value": "ZZ0000002"}], docs=[a_again, c]),
    ]


def test_the_totals_count_distinct_documents_sightings_and_links(tmp_path, capsys):
    res = run(tmp_path, capsys, _shared_rows())
    assert res.code == 0
    assert (
        res.counters()["plan unique documents"],
        res.counters()["plan unique sightings"],
        res.counters()["plan unique links"],
    ) == (3, 4, 4)
    for extra in (["--limit", "1"],):
        again = run(tmp_path, capsys, _shared_rows(), good, *extra)
        assert {k: v for k, v in again.counters().items() if k.startswith("plan unique")} == {
            k: v for k, v in res.counters().items() if k.startswith("plan unique")
        }
    stopped = run(tmp_path, capsys, _shared_rows(), lambda req, n: (401, {}))
    assert (
        stopped.counters()["plan unique documents"],
        stopped.counters()["plan unique links"],
    ) == (3, 4)


def _boundary_row():
    base = row("1", docs=[document("pad")])
    target = apply_mod.MAX_BODY_BYTES
    size = len(apply_mod.encode(apply_mod.row_to_request(base, "0" * 64, dry_run=True)[0]))
    base["documents"][0]["doc_type"] = "z" * (len(base["documents"][0]["doc_type"]) + target - size)
    return base


def test_a_row_of_exactly_the_cap_in_a_dry_run_is_one_byte_over_in_apply(tmp_path, capsys):
    boundary = _boundary_row()
    sha = "0" * 64
    assert len(apply_mod.preflight(boundary, sha, dry_run=True)[1]) == 65536
    assert len(apply_mod.encode(apply_mod.row_to_request(boundary, sha, dry_run=False)[0])) == 65537
    dry = run(tmp_path, capsys, [boundary])
    assert (
        dry.code == 0
        and len(dry.rec.requests) == 1
        and dry.counters()["plan unique documents"] == 1
    )
    live = run(tmp_path, capsys, [boundary], good, apply_=True)
    assert (
        live.code == 1
        and live.rec.requests == []
        and live.counters()["skipped:body_too_large"] == 1
    )
    assert live.counters()["plan unique documents"] == 0


# ── 11. slice 1 untouched in behaviour ───────────────────────────────────────


def test_read_ledger_has_its_slice_one_signature_and_behaviour(tmp_path):
    assert list(inspect.signature(slice_one.read_ledger).parameters) == ["path", "sha256"]
    ledger = tmp_path / "ledger.jsonl"
    for ref, outcome in (("A", "created"), ("B", "already_registered"), ("C", "linked")):
        slice_one.append_ledger(
            ledger, {"plan_sha256": PLAN_SHA, "subject_ref": ref, "outcome": outcome}
        )
    assert slice_one.read_ledger(ledger, PLAN_SHA) == {"A", "B"}
    assert not hasattr(apply_mod, "read_ledger") and not hasattr(apply_mod, "_counts")
    assert "ledger" not in " ".join(inspect.signature(apply_mod.main).parameters)
    assert itertools  # keep the import used by parametrization helpers
