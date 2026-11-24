"""The error tracker finds addresses in free text in one forward pass, with the same result as before.

The address shape used to be a regular expression, `[\\w.+-]+@[\\w.-]+\\.[\\w.-]+`, which tried every
start of a long run of local-part characters and read the rest of the run from each: on a run with
no `@` the time grew with the square of its length, inside the hook that runs on every event. The
scan must give exactly the text that expression gave, and grow in proportion to its input.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from backend.app.setup.sentry_config import (
    PII_REDACTION_PLACEHOLDER,
    _before_send,
    _redact_addresses,
    _redact_string,
)

P = PII_REDACTION_PLACEHOLDER
# The expression of record, written here and not imported, so the module cannot change both sides.
OLD_RULE = re.compile(r"[\w.+-]+@[\w.-]+\.[\w.-]+")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no address here",
        "write to zz.client@example.test today",
        "zz+tag@example.test, other.zz@sub.example.test",
        "a@b.c+d@e.f",  # a match ends at "+": the next one starts there, "+d@e.f"
        "x@y",
        "a@.b",
        "a@b.",
        "a@b..c",
        "@example.test",
        "zz@@example.test",
        "é.zz@exämple.test",
        "zz@example.test.",
        "path/zz@example.test/next",
        "zz-@-.-",
        "a.@.a.a",
    ],
)
def test_pinned_texts_come_out_as_the_expression_gave_them(text: str) -> None:
    assert _redact_addresses(text) == OLD_RULE.sub(P, text)


def test_the_scan_and_the_expression_agree_on_a_seeded_corpus() -> None:
    rng = random.Random(20261123)
    alphabet = ["a", "Z", "1", "_", "é", ".", "+", "-", "@", "@", " ", ",", "/", "\n"]
    seen_matches = 0
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        expected = OLD_RULE.sub(P, text)
        assert _redact_addresses(text) == expected, text
        seen_matches += expected != text
    assert seen_matches > 500  # the corpus really holds addresses, not only near misses


def test_the_hook_still_hides_an_address_in_a_long_text() -> None:
    text = "ab." * 20_000 + " mail zz.client@example.test"
    event = _before_send({"exception": {"values": [{"type": "E", "value": text}]}}, {})
    assert event is not None
    value = event["exception"]["values"][0]["value"]
    assert "zz.client@example.test" not in value
    assert value.endswith(f" mail {P}")


def _seconds(text: str) -> float:
    # CPU time of this process, not wall time: a busy or throttled runner suspends the process
    # between slices, and wall time would count the suspension as work of the scan.
    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        _redact_addresses(text)
        best = min(best, time.process_time() - started)
    return best


@pytest.mark.parametrize(
    ("unit", "tail"),
    [
        ("ab.", "@"),
        ("a" * 50 + "@x ", ""),
        ("a@", ""),
        ("a@b.", ""),
        (".@", ""),
        ("a+", "@"),
    ],
    ids=[
        "long run, one @ at the end",
        "many addresses with no dot",
        "many glued @",
        "glued addresses",
        "dots and @",
        "plus signs",
    ],
)
def test_the_scan_grows_in_proportion_to_its_input(unit: str, tail: str) -> None:
    # Four times the input may cost about four times the time; the old expression cost sixteen.
    n = max(1, 40_000 // len(unit))
    small, large = _seconds(unit * n + tail), _seconds(unit * (4 * n) + tail)
    assert large < 7 * max(small, 1e-3), (small, large)


def test_the_text_pass_keeps_its_other_rules_after_the_addresses() -> None:
    out = _redact_string("zz@example.test called from client CL-000142")
    assert "zz@example.test" not in out
    assert "CL-000142" not in out
