"""Guilt + innocence corpus for scripts/redact_for_external.py.

This module is the boundary text passes before it leaves for a third-party
model. The repository is public and scanned, so no value below is a literal:
every sample is ASSEMBLED at run time from harmless pieces (a prefix joined to
a run of one repeated letter), and the assertions are about what the output no
longer contains and what it still shows.

Rule of the module under test: redact the VALUE, keep the STRUCTURE.
"""

from __future__ import annotations

import random
import re
import sys
from itertools import pairwise
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import redact_for_external as module
from redact_for_external import redact

PLACEHOLDER = "<REDACTED:secret>"

# Two distinct markers, so a value that is cut in two by the redactor still
# fails the assertion when only one half survives.
HEAD = "Mq" + "x" * 8
TAIL = "Nr" + "y" * 8
CRED = HEAD + TAIL  # a 20-character run, assembled

# Name pieces assembled so that no line of this file reads as an assignment.
KEYISH = "API" + "_" + "KEY"
TOKENISH = "UPSTREAM" + "_" + "TOKEN"
PASSISH = "DB" + "_" + "PASSWORD"


def _run(chars: str, n: int = 10) -> str:
    return chars * n


# ───────────────────────────────────────── shapes the code already handled


def _existing_shape_samples() -> dict[str, tuple[str, str]]:
    """label -> (line, marker that must not survive)."""
    body = _run("k", 24)
    return {
        "sk-prefix": (f"x = {'sk' + '-' + body}", body),
        "ant-prefix": (f"x = {'ant' + '-' + body}", body),
        "apikey-prefix": (f"x = {'apikey' + '_' + body}", body),
        "ghp-prefix": (f"x = {'ghp' + '_' + _run('k', 24)}", _run("k", 24)),
        "github-pat-prefix": (
            f"x = {'github' + '_pat_' + _run('k', 24)}",
            _run("k", 24),
        ),
        "google-prefix": (f"x = {'AI' + 'za' + _run('k', 24)}", _run("k", 24)),
        "slack-prefix": (f"x = {'xo' + 'xb-' + _run('7', 14)}", _run("7", 14)),
        "jwt-shape": (
            f"x = {'ey' + 'J' + _run('a', 12)}.{_run('b', 12)}.{_run('c', 8)}",
            _run("b", 12),
        ),
        "fm2-prefix": (f"x = {'fm2' + '_' + _run('k', 24)}", _run("k", 24)),
        "aws-key-id": (f"x = {'AK' + 'IA' + _run('Q', 16)}", _run("Q", 16)),
        "dsn-credentials": (
            f"{'postgres' + '://'}admin:{HEAD}@db.internal:5432/app",
            HEAD,
        ),
        "named-quoted": (f'{TOKENISH} = "{HEAD}"', HEAD),
        "named-quoted-single": (f"{TOKENISH} = '{HEAD}'", HEAD),
        "named-bare-yaml": (f"{KEYISH}: {HEAD}", HEAD),
        "named-bare-shell": (f"export {TOKENISH}={HEAD}", HEAD),
        "quoted-key": ('{"' + KEYISH.lower() + '": "' + HEAD + '"}', HEAD),
        "email": (f"contact {'nobody' + '@' + 'example.test'}", "nobody"),
    }


@pytest.mark.parametrize("label", sorted(_existing_shape_samples()))
def test_every_shape_the_redactor_already_handled_still_goes(label):
    line, marker = _existing_shape_samples()[label]
    out = redact(line)
    assert marker not in out
    assert "<REDACTED:" in out


def _family_samples() -> dict[str, tuple[str, str]]:
    body = "k" * 24
    return {
        "github-oauth": ("gh" + "o" + "_" + body, body),
        "github-user": ("gh" + "u" + "_" + body, body),
        "github-server": ("gh" + "s" + "_" + body, body),
        "github-refresh": ("gh" + "r" + "_" + body, body),
        "stripe-live": ("sk" + "_live_" + body, body),
        "google-oauth": ("ya" + "29." + body, body),
        "meta-access": ("EA" + "A" + body, body),
    }


@pytest.mark.parametrize("label", sorted(_family_samples()))
def test_a_well_known_token_family_is_redacted_wherever_it_appears(label):
    token, body = _family_samples()[label]
    for line in (f"x = {token}", f"call(a, b, {token}, c)", f"see {token}."):
        out = redact(line)
        assert body not in out, label
        assert PLACEHOLDER in out


def test_the_text_around_a_redacted_family_token_stays():
    token, _ = _family_samples()["github-oauth"]
    assert redact(f"call(a, {token}, c)") == f"call(a, {PLACEHOLDER}, c)"


@pytest.mark.parametrize(
    "line",
    [
        "gho_short",
        "ghost_writer = 1",
        "ghr_x",
        "sk_live_ok",
        "sk_test_mode = True",
        "ya29.5",
        "version = ya29.5.3",
        "EAAB12",
        "EAAgle is not a token",
        "see the ghost_in_the_shell docs",
    ],
)
def test_a_word_that_only_starts_like_a_token_prefix_stays(line):
    assert redact(line) == line


def _token_shaped_key_samples() -> dict[str, str]:
    """A credential name that starts like a token, in the shapes main reads."""
    samples = {}
    for label, (token, _) in _family_samples().items():
        samples[f"{label}-bare"] = f"{token}_TOKEN = {CRED}"
        samples[f"{label}-python"] = f'{token}_TOKEN = "{CRED}"'
        if "." not in token:  # a quoted mapping key holds no dot
            samples[f"{label}-json"] = '{"' + token + '_TOKEN": "' + CRED + '"}'
            samples[f"{label}-json-camel"] = '{"' + token + 'Token": "' + CRED + '"}'
    return samples


@pytest.mark.parametrize("label", sorted(_token_shaped_key_samples()))
def test_a_key_that_starts_like_a_token_keeps_its_name_and_loses_its_value(label):
    line = _token_shaped_key_samples()[label]
    assert redact(line) == line.replace(CRED, PLACEHOLDER)


def test_a_token_glued_to_a_credential_name_is_cut_where_the_name_starts():
    token = "ya" + "29." + "k" * 24 + "-"
    out = redact(f"{token}{KEYISH}: {CRED}")
    assert out == f"{PLACEHOLDER}{KEYISH}: {PLACEHOLDER}"


def _main_redacts_samples() -> dict[str, tuple[str, str]]:
    """label -> (line, marker the rules before the families already hide)."""
    jwt = "ey" + "J" + "a" * 12 + "." + "EA" + "A" + "b" * 20 + "." + "c" * 8
    return {
        "jwt-whose-middle-starts-like-a-meta-token": (f"x = {jwt}", "c" * 8),
        "address-whose-domain-starts-like-a-google-token": (
            "contact nobody" + "@" + "ya" + "29." + "k" * 24 + ".test",
            "nobody",
        ),
        "address-whose-local-part-is-a-github-token": (
            "mail " + "gh" + "o_" + "k" * 24 + "@" + "example.test",
            "example.test",
        ),
        "credential-name-that-is-a-github-token": (
            "gh" + "o_" + "k" * 24 + "_TOKEN" + f" = {HEAD}",
            HEAD,
        ),
    }


@pytest.mark.parametrize("label", sorted(_main_redacts_samples()))
def test_a_family_never_uncovers_what_the_rules_before_it_hide(label):
    line, marker = _main_redacts_samples()[label]
    assert marker not in redact(line), label


_SENTINEL = re.compile(r"V\d{5}q{6}")


def _sentinel_lines(seed: int, count: int) -> list[str]:
    """Random lines of names, separators, quotes, escapes, tokens and addresses.

    Every value written into a line is a unique sentinel, so a sentinel that one
    output hides and another shows is a value uncovered by the difference.
    """
    tokens = [token for token, _ in _family_samples().values()]
    jwt = "ey" + "J" + "a" * 12 + "." + "EA" + "A" + "b" * 20 + "." + "c" * 8
    pieces = [
        KEYISH,
        TOKENISH,
        PASSISH,
        "gh" + "o_" + "a" * 20 + "_TOKEN",
        "ya" + "29." + "k" * 24 + "-",
        "config",
        "Authorization",
        "Bearer ",
        "=",
        ": ",
        " = ",
        '"',
        "'",
        '"""',
        "\\",
        "@",
        "example.test",
        "\n",
        " ",
        ", ",
        " # ",
        "{",
        "}",
        jwt,
        *tokens,
    ]
    rng = random.Random(seed)
    lines = []
    serial = 0
    for _ in range(count):
        parts = []
        for _ in range(rng.randint(1, 10)):
            if rng.random() < 0.3:
                serial += 1
                parts.append(f"V{serial:05d}" + "q" * 6)
            else:
                parts.append(rng.choice(pieces))
        lines.append("".join(parts))
    return lines


def test_the_families_only_ever_hide_more_than_the_rules_before_them(monkeypatch):
    lines = _sentinel_lines(seed=20261123, count=4000)
    with monkeypatch.context() as patched:
        # With no late family the module is the rules before them, unchanged.
        patched.setattr(module, "_LATE_TOKEN_FAMILIES", [])
        before = [redact(line) for line in lines]
    hidden_before = 0
    for line, earlier in zip(lines, before):
        later = redact(line)
        for sentinel in _SENTINEL.findall(line):
            if sentinel not in earlier:
                hidden_before += 1
                assert sentinel not in later, (line, earlier, later)
    assert hidden_before > 100  # the lines reach the rules before the families


def test_late_family_redaction_stays_linear():
    import time

    text = ("gh" + "o_" + "a" * 20 + "_TOKEN=") * 16_000
    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 20.0


# ───────────────────────────────── construction: main first, then a stage that hides more

# A marker assembled at run time, so no line of this file carries it whole.
_PEM = "-----BEGIN " + "PRIVATE KEY-----"

# Inputs and the exact output of the rules as they stand on main (before the stage
# and the late families), captured by running main's redact() on each input and
# written here as literals. The table does not call the module under test.
MAIN_TABLE: list[tuple[str, str]] = [
    (
        'LONGEXAMPLEAPIKEY="opaque-value-123"',  # pragma: allowlist secret
        'LONGEXAMPLEAPIKEY="<REDACTED:secret>"',
    ),
    (
        "Authorization: Bearer UPSTREAM_TOKEN=opaque-value-123",  # pragma: allowlist secret
        "Authorization: Bearer UPSTREAM_TOKEN=<REDACTED:secret>",
    ),
    (
        '{"gho_aaaaaaaaaaaaaaaaaaaaaaaa_TOKEN": "opaque-value-123"}',  # pragma: allowlist secret
        '{"gho_aaaaaaaaaaaaaaaaaaaaaaaa_TOKEN": "<REDACTED:secret>"}',
    ),
    (
        'API_KEY = """aaaa\\"""opaque-value-123"""',  # pragma: allowlist secret
        'API_KEY = """aaaa\\"""opaque-value-123"""',  # pragma: allowlist secret
    ),
    (
        "export TOKEN='abcd\\' # keep this comment",
        "export TOKEN='<REDACTED:secret>' # keep this comment",
    ),
    (
        "export TOKEN='abcd\\' API_KEY=' hidden-value-123'",  # pragma: allowlist secret
        "export TOKEN='<REDACTED:secret>' API_KEY='<REDACTED:secret>'",
    ),
    (
        "# "
        + _PEM
        + '\nLONGEXAMPLEAPIKEY="opaque-value-123"',  # pragma: allowlist secret
        "# " + _PEM + '\nLONGEXAMPLEAPIKEY="<REDACTED:secret>"',
    ),
    (
        "Authorization: Bearer a2345678@example.test",
        "Authorization: Bearer <REDACTED:email>",
    ),
    ("Authorization: Bearer YWJjZGVmKEY=", "Authorization: Bearer YWJjZGVmKEY="),
    (
        "Authorization: Bearer abcd1234;MODE=production",
        "Authorization: Bearer abcd1234;MODE=production",
    ),
    (
        "Authorization: Bearer abcd1234.API_KEY=hidden-value-123",  # pragma: allowlist secret
        "Authorization: Bearer abcd1234.API_KEY=<REDACTED:secret>",
    ),
    ("Authorization: Bearer AbcdEfgh", "Authorization: Bearer AbcdEfgh"),
    (
        "Authorization: Bearer abcdefgh12345678",
        "Authorization: Bearer abcdefgh12345678",
    ),
    (
        '-H "Authorization: Basic abcd1234efgh"',
        '-H "Authorization: Basic abcd1234efgh"',
    ),
    (
        '{"Authorization": "Bearer abcd1234efgh"}',
        '{"Authorization": "Bearer abcd1234efgh"}',
    ),
    ('authorization="Bearer abcd1234efgh"', 'authorization="Bearer abcd1234efgh"'),
    (
        'headers["Authorization"] = "Bearer abcdefgh12345678"',
        'headers["Authorization"] = "Bearer abcdefgh12345678"',
    ),
    (
        "h['authorization']='Basic abcd1234efgh'",
        "h['authorization']='Basic abcd1234efgh'",
    ),
    (
        '"Authorization": f"Bearer abcd1234efgh"',
        '"Authorization": f"Bearer abcd1234efgh"',
    ),
    ("Authorization: Bearer {token}", "Authorization: Bearer {token}"),
    ("Authorization: Bearer ${TOKEN}", "Authorization: Bearer ${TOKEN}"),
    ("Authorization: Bearer $TOKEN", "Authorization: Bearer $TOKEN"),
    ("Authorization: Bearer <token>", "Authorization: Bearer <token>"),
    ("Authorization: Bearer %s", "Authorization: Bearer %s"),
    ("Authorization header missing", "Authorization header missing"),
    ("Authorization: Bearer short12", "Authorization: Bearer short12"),
    (
        "Authorization: Bearer abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnop",
    ),
    ("Authorization: Bearer ABCDEFGH", "Authorization: Bearer ABCDEFGH"),
    ("Authorization: Bearer Abcdefgh", "Authorization: Bearer Abcdefgh"),
    (
        "Authorization: Bearer abcdefgh;MODE=production",
        "Authorization: Bearer abcdefgh;MODE=production",
    ),
    ("Authorization: Digest abcd1234efgh", "Authorization: Digest abcd1234efgh"),
    (
        "postgres://admin:hunter22@db.internal:5432/app",  # pragma: allowlist secret
        "postgres://<REDACTED:dsn-credentials>@db.internal:5432/app",  # pragma: allowlist secret
    ),
    ("contact nobody@example.test now", "contact <REDACTED:email> now"),
    ("UPSTREAM_TOKEN: abcd1234efgh", "UPSTREAM_TOKEN: <REDACTED:secret>"),
    (
        '{"api_key": "opaque-value-123"}',  # pragma: allowlist secret
        '{"api_key": "<REDACTED:secret>"}',
    ),
    (
        "UPSTREAM_TOKEN='opaque-value-123' and DB_PASSWORD=abcd1234efgh",  # pragma: allowlist secret
        "UPSTREAM_TOKEN='<REDACTED:secret>' and DB_PASSWORD=<REDACTED:secret>",
    ),
    (
        "Authorization: Bearer <REDACTED:secret> and a@example.test",
        "Authorization: Bearer <REDACTED:secret> and <REDACTED:email>",
    ),
    (
        "Authorization: Bearer abcd1234 Authorization: Bearer efgh5678 x",
        "Authorization: Bearer abcd1234 Authorization: Bearer efgh5678 x",
    ),
]

MAIN_PLACEHOLDERS = (
    "<REDACTED:secret>",
    "<REDACTED:email>",
    "<REDACTED:dsn-credentials>",
)
_PLACEHOLDER_RE = re.compile(r"<REDACTED:[^>]*>")


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(any(item == n for item in it) for n in needles)


def test_the_placeholders_main_emits_are_the_reserved_list():
    import inspect

    emitted = set()
    for func in (module._redact_main, module._redact_emails):
        emitted.update(re.findall(r"<REDACTED:[a-z\-]+>", inspect.getsource(func)))
    assert emitted == set(MAIN_PLACEHOLDERS)
    assert module._RESERVED_PLACEHOLDERS == MAIN_PLACEHOLDERS
    for _, output in MAIN_TABLE:
        assert set(_PLACEHOLDER_RE.findall(output)) <= set(MAIN_PLACEHOLDERS)


@pytest.mark.parametrize("index", range(len(MAIN_TABLE)))
def test_main_rules_equal_the_literal_outputs_of_main(index):
    line, expected = MAIN_TABLE[index]
    assert module._redact_main(line) == expected


@pytest.mark.parametrize("index", range(len(MAIN_TABLE)))
def test_with_the_stage_and_late_families_off_redact_is_main(index, monkeypatch):
    line, expected = MAIN_TABLE[index]
    monkeypatch.setattr(module, "_stage", lambda text: text)
    monkeypatch.setattr(module, "_redact_late_token_families", lambda text: text)
    assert redact(line) == expected


@pytest.mark.parametrize("index", range(len(MAIN_TABLE)))
def test_every_placeholder_of_main_survives_the_stage_in_order(index):
    line, expected = MAIN_TABLE[index]
    kept = _PLACEHOLDER_RE.findall(redact(line))
    assert _is_subsequence(_PLACEHOLDER_RE.findall(expected), kept)


# T3: main's literal output, and the stage adds nothing.
_UNCHANGED_BY_THE_STAGE = [
    'LONGEXAMPLEAPIKEY="opaque-value-123"',  # pragma: allowlist secret
    "Authorization: Bearer UPSTREAM_TOKEN=opaque-value-123",  # pragma: allowlist secret
    '{"gho_aaaaaaaaaaaaaaaaaaaaaaaa_TOKEN": "opaque-value-123"}',  # pragma: allowlist secret
    'API_KEY = """aaaa\\"""opaque-value-123"""',  # pragma: allowlist secret
    "export TOKEN='abcd\\' # keep this comment",
    "export TOKEN='abcd\\' API_KEY=' hidden-value-123'",  # pragma: allowlist secret
    "# " + _PEM + '\nLONGEXAMPLEAPIKEY="opaque-value-123"',  # pragma: allowlist secret
]


@pytest.mark.parametrize("index", range(len(_UNCHANGED_BY_THE_STAGE)))
def test_inputs_main_already_answers_come_out_exactly_as_main_gives_them(index):
    line = _UNCHANGED_BY_THE_STAGE[index]
    expected = dict(MAIN_TABLE)[line]
    assert redact(line) == expected


def test_main_hides_the_address_before_the_stage_reads_the_header():
    assert (
        redact("Authorization: Bearer a2345678@example.test")
        == "Authorization: Bearer <REDACTED:email>"
    )


_COVERAGE = [
    ("Authorization: Bearer abcd1234efgh", "Authorization: Bearer <REDACTED:secret>"),
    (
        '-H "Authorization: Basic abcd1234efgh"',
        '-H "Authorization: Basic <REDACTED:secret>"',
    ),
    (
        '{"Authorization": "Bearer abcd1234efgh"}',
        '{"Authorization": "Bearer <REDACTED:secret>"}',
    ),
    (
        'authorization="Bearer abcd1234efgh"',
        'authorization="Bearer <REDACTED:secret>"',
    ),
    (
        'headers["Authorization"] = "Bearer abcdefgh12345678"',
        'headers["Authorization"] = "Bearer <REDACTED:secret>"',
    ),
    (
        "h['authorization']='Basic abcd1234efgh'",
        "h['authorization']='Basic <REDACTED:secret>'",
    ),
    (
        '"Authorization": f"Bearer abcd1234efgh"',
        '"Authorization": f"Bearer <REDACTED:secret>"',
    ),
    (
        "Authorization: Bearer abcd1234;MODE=production",
        "Authorization: Bearer <REDACTED:secret>;MODE=production",
    ),
    (
        "Authorization: Bearer abcd1234.API_KEY=hidden-value-123",  # pragma: allowlist secret
        "Authorization: Bearer <REDACTED:secret>API_KEY=<REDACTED:secret>",
    ),
    ("Authorization: Bearer AbcdEfgh", "Authorization: Bearer <REDACTED:secret>"),
]


@pytest.mark.parametrize("index", range(len(_COVERAGE)))
def test_an_authorization_credential_is_hidden_and_its_frame_stays(index):
    line, expected = _COVERAGE[index]
    assert redact(line) == expected


_INNOCENT = [
    "Authorization: Bearer {token}",
    "Authorization: Bearer ${TOKEN}",
    "Authorization: Bearer $TOKEN",
    "Authorization: Bearer <token>",
    "Authorization: Bearer %s",
    "Authorization header missing",
    "Authorization: Bearer short12",
    "Authorization: Bearer abcdefghijklmnop",
    "Authorization: Bearer ABCDEFGH",
    "Authorization: Bearer Abcdefgh",
    "Authorization: Bearer abcdefgh;MODE=production",
    "Authorization: Digest abcd1234efgh",
]


@pytest.mark.parametrize("index", range(len(_INNOCENT)))
def test_a_template_a_short_word_or_another_scheme_is_left_alone(index):
    assert redact(_INNOCENT[index]) == _INNOCENT[index]


def test_a_credential_run_that_reads_as_a_name_site_stays_as_main_leaves_it():
    line = "Authorization: Bearer YWJjZGVmKEY="
    assert redact(line) == dict(MAIN_TABLE)[line] == line


def _reserved_by_hand(text: str) -> list[tuple[int, int]]:
    """Reserved intervals again, from the placeholders and the name regex alone."""
    found = []
    for holder in MAIN_PLACEHOLDERS:
        at = text.find(holder)
        while at != -1:
            found.append((at, at + len(holder)))
            at = text.find(holder, at + 1)
    found.extend(m.span() for m in module._RESERVED_NAME_SITE.finditer(text))
    return found


def _edit_corpus() -> list[str]:
    pieces = [
        "Authorization",
        '"Authorization"',
        "Bearer ",
        "Basic ",
        ": ",
        "=",
        "]",
        "abcd1234",
        "AbcdEfgh",
        "short",
        "." + KEYISH + "=",
        KEYISH + ": ",
        '"' + TOKENISH + '": ',
        "<REDACTED:secret>",
        "<REDACTED:email>",
        "<REDACTED:dsn-credentials>",
        "a2345678@example.test",
        ";",
        '"',
        " ",
        "\n",
    ]
    rng = random.Random(20261124)
    corpus = [line for line, _ in MAIN_TABLE]
    frames = [
        "Authorization: Bearer ",
        'h["Authorization"] = "Basic ',
        "authorization=Bearer ",
    ]
    for _ in range(3000):
        body = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 14)))
        corpus.append(rng.choice(["", rng.choice(frames)]) + body)
    return corpus


def test_the_edit_list_is_sorted_disjoint_outside_every_reserved_interval():
    edited = 0
    for original in _edit_corpus():
        text = module._redact_main(original)
        edits = module._authorization_edits(text)
        reserved = _reserved_by_hand(text)
        previous_end = 0
        for start, end in edits:
            assert previous_end <= start < end <= len(text), (text, edits)
            assert text[start:end].strip(), (text, edits)
            for r_start, r_end in reserved:
                assert end <= r_start or r_end <= start, (text, edits, reserved)
            previous_end = end
        merged = module._reserved_intervals(text)
        assert merged == sorted(merged)
        assert all(a[1] < b[0] for a, b in pairwise(merged))
        edited += bool(edits)
    assert edited > 100  # the corpus reaches the stage


def test_the_stage_never_touches_a_reserved_interval():
    for original in _edit_corpus():
        text = module._redact_main(original)
        out = module._stage(text)
        if out == text:
            continue
        for holder in MAIN_PLACEHOLDERS:
            assert out.count(holder) >= text.count(holder)
        for start, end in module._reserved_intervals(text):
            assert text[start:end] in out, (text, out)


def _growth_families():
    return {
        "short-credentials": lambda n: "Authorization: Bearer short\n" * n,
        "one-long-run": lambda n: "Authorization: Bearer " + "ab12." * n,
        "placeholders-after-the-scheme": (
            lambda n: "Authorization: Bearer " + "<REDACTED:secret>" * n
        ),
        "words-without-a-scheme": lambda n: "Authorization " * n,
        "one-edit-each": lambda n: "Authorization: Bearer abcd1234 " * n,
    }


def _best_cpu_time(text: str, rounds: int = 5) -> float:
    import time

    best = float("inf")
    for _ in range(rounds):
        started = time.process_time()
        module._stage(text)
        best = min(best, time.process_time() - started)
    return best


@pytest.mark.parametrize("family", sorted(_growth_families()))
def test_the_stage_grows_linearly_with_its_input(family):
    build = _growth_families()[family]
    n = 64
    while _best_cpu_time(build(n), rounds=1) < 0.005:
        n *= 2
        assert n < 2**24
    t1 = _best_cpu_time(build(n))
    t4 = _best_cpu_time(build(4 * n))
    t16 = _best_cpu_time(build(16 * n))
    # Linear growth gives about 4 for each step, quadratic about 16.
    assert t4 / t1 < 5.5, (family, n, t1, t4)
    assert t16 / t4 < 5.5, (family, n, t4, t16)
