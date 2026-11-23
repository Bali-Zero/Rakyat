"""The funnel-event scrub finds addresses in one forward pass, with the same result as before.

The address shape used to be a regular expression, `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}`,
which tried every start of a long run of local-part characters and read the rest of the run from
each: on a run with no `@` the time grew with the square of its length, on the event loop, for a
payload anyone can post. The scan must give exactly the text that expression gave, and grow in
proportion to its input.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from backend.app.routers.analytics import _scrub_emails, _scrub_pii_string, scrub_pii

R = "[redacted]"
# The expression of record, written here and not imported, so the module cannot change both sides.
OLD_RULE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no address here",
        "write to zz.client@example.test today",
        "zz+tag@example.test, other%zz@sub.example.test",
        "a@b.co+d@e.fg",  # a match ends at "+": the next one starts there, "+d@e.fg"
        "a@b.com.x1",  # the address ends after the letters of the last usable dot
        "a@b.c",
        "a@.bc",
        "a@b.c1",
        "a@b..cd",
        "a@b.cd.",
        "a@b-.cd-e",
        "@example.test",
        "zz@@example.test",
        "zz_x@example.test",
        "é.zz@example.test",
        "zz@exämple.test",
        "a@b.cd@e.fg",
        "path/zz@example.test/next",
    ],
)
def test_pinned_texts_come_out_as_the_expression_gave_them(text: str) -> None:
    assert _scrub_emails(text) == OLD_RULE.sub(R, text)


def test_the_scan_and_the_expression_agree_on_a_seeded_corpus() -> None:
    rng = random.Random(20261123)
    # Pieces, not only characters: a domain ending needs two letters after its last usable dot.
    alphabet = [
        "a",
        "Z",
        "1",
        "_",
        "%",
        "é",
        ".",
        ".",
        "+",
        "-",
        "@",
        "@",
        " ",
        "/",
        "ab",
        "com",
        "Xy",
    ]
    changed = 0
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 40)))
        expected = OLD_RULE.sub(R, text)
        assert _scrub_emails(text) == expected, text
        changed += expected != text
    assert changed > 500  # the corpus really holds addresses, not only near misses


def test_the_payload_scrub_still_hides_addresses_and_phones() -> None:
    payload = {
        "note": "ab." * 20_000 + " mail zz.client@example.test",
        "phone": "+62 812 3456 7890",
    }
    out = scrub_pii(payload)
    assert out["note"].endswith(f" mail {R}")
    assert "zz.client@example.test" not in out["note"]
    assert out["phone"] == R
    assert _scrub_pii_string("call zz@example.test or +62 812 3456 7890") == f"call {R} or {R}"


def _seconds(text: str) -> float:
    # CPU time of this process, not wall time: a busy or throttled runner suspends the process
    # between slices, and wall time would count the suspension as work of the scan.
    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        _scrub_emails(text)
        best = min(best, time.process_time() - started)
    return best


@pytest.mark.parametrize(
    ("unit", "tail"),
    [
        ("a", "@"),
        ("ab.", "@"),
        ("a" * 50 + "@x ", ""),
        ("a@", ""),
        ("a@b.", ""),
        (".@", ""),
        ("a@b.c1", ""),
        ("a+", "@"),
    ],
    ids=[
        "long run, one @ at the end",
        "dotted run, one @ at the end",
        "many addresses with no dot",
        "many glued @",
        "glued addresses",
        "dots and @",
        "short endings",
        "plus signs",
    ],
)
def test_the_scan_grows_in_proportion_to_its_input(unit: str, tail: str) -> None:
    # Four times the input may cost about four times the time; the old expression cost sixteen.
    n = max(1, 40_000 // len(unit))
    small, large = _seconds(unit * n + tail), _seconds(unit * (4 * n) + tail)
    assert large < 7 * max(small, 1e-3), (small, large)
