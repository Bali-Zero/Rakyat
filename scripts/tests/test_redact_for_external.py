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
