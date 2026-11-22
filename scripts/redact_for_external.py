#!/usr/bin/env python3
"""redact_for_external.py — strip values before any text leaves this machine.

Builder Contract §4 is an OUTPUT boundary: it does not care that the output is a
code review rather than a client report. Anything sent to a third-party endpoint
passes through here first.

The design rule that makes this safe for a *semantic* lint: redact VALUES, keep
STRUCTURE. A judge deciding whether a vendor client is being built with a paid
credential needs the call, the keyword argument and the fact that a literal is
there — it does not need the literal. So a constructor written with a live key
comes out as:

    VendorClient(api_key="<REDACTED:secret>")

which carries every bit of the signal and none of the credential. Over-redaction
is the failure mode to avoid here; a lint that redacts the function name cannot
judge anything.

NB: the examples above deliberately do NOT spell the banned Anthropic
constructor. catE-sovereignty-lint counts that spelling repo-wide against a
budget of three, and it cannot tell prose about the pattern from a use of it —
which is the very over-match this module's caller exists to adjudicate. Writing
the literal here would have spent a third of that budget on a docstring.

The name prefix is OPTIONAL in every rule: the first version required at
least one character before the sensitive word, so a variable literally
called TOKEN or SECRET was not redacted at all. Found by the
codex-gpt-5.6-sol council seat, 2026-09-20.

Stdlib only — this runs in CI.
"""

from __future__ import annotations

import re

# Value shapes that are credentials wherever they appear. Ordered longest-first
# so a specific prefix wins over the generic long-token rule.
_SECRET_LITERALS: list[re.Pattern[str]] = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bapikey_[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"),
    re.compile(r"\bfm2_[A-Za-z0-9_\-]{20,}"),
    # AWS access key id. Its absence was noted as ironic by a council seat,
    # since the unquoted-assignment scar that preceded it used an AWS key.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

# A name says its value is a credential when it ENDS in a credential word, or
# when that word is a whole underscore-separated segment followed only by
# suffixes that leave it the same kind of thing: an environment, a role among
# several copies, a version or an ordinal. Any other suffix describes something
# ABOUT a credential (its id, type, path, hash, usage count), and redacting
# those values blinds the reader of the code without protecting anything: a
# measurement over this repository found about fifty such lines for each real
# one. The vocabulary is extended here, in one place.
_CREDENTIAL_WORD = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|DSN)"
_CREDENTIAL_SUFFIX = (
    r"(?:PROD|PRODUCTION|STAGING|STAGE|STG|DEV|DEVELOPMENT|TEST|TESTING|LIVE|SANDBOX"
    r"|LOCAL|PREVIEW|BASE|MAIN|PRIMARY|SECONDARY|BACKUP|OLD|NEW|CURRENT|PREVIOUS|NEXT"
    r"|V?\d+|[A-Z])"
)
_CREDENTIAL_NAME = (
    r"(?:[A-Za-z_]?[A-Za-z0-9_]*" + _CREDENTIAL_WORD + r"\b"
    r"|(?:[A-Za-z_][A-Za-z0-9_]*_)?"
    + _CREDENTIAL_WORD
    + r"(?:_"
    + _CREDENTIAL_SUFFIX
    + r")+\b)"
)

# The shapes are matched by the patterns below rather than quoted here — see
# test_named_assignment_* for each as a real line.
_NAMED_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<name>\b"""
    + _CREDENTIAL_NAME
    + r""")
    (?P<sep>\s*[:=]\s*)
    (?P<quote>['"])
    (?P<value>[^'"\n]{4,})
    (?P=quote)
    """
)

# The unquoted form — `AWS_SECRET_ACCESS_KEY: wJalr...` in YAML, `TOKEN=abc` in
# a shell line. A hash remains part of the value until whitespace starts a
# comment. The quoted rule above silently did not cover it, so a live
# credential went to a third-party endpoint byte-for-byte. Found by an
# adversarial reviewer on 2026-09-20 with a real AWS key shape; it is the worst
# class of bug this module can have, because the module IS the §4 boundary.
_NAMED_ASSIGNMENT_BARE = re.compile(
    r"""(?ix)
    (?P<name>\b"""
    + _CREDENTIAL_NAME
    + r""")
    (?P<sep>\s*[:=]\s*)
    (?P<value>[^\s'"#][^\s]{3,})
    """
)

# A JSON or dict key puts a quote between the name and the colon, so the two
# assignment rules above — which need the name adjacent to `[:=]` — both miss
# a sensitive name QUOTED as a mapping key. An opaque value matches no literal
# shape either, so it was left verbatim. Third redaction hole found by review
# rather than by the author. Found by the kimi-code/k3 council seat,
# 2026-09-20. The shape is described, not written: this very comment spelled
# out is what reddened Detect Secrets on #6935. The literal lives in the guilt
# corpus — see test_redaction_catches_a_quoted_json_key.
_QUOTED_KEY_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<open>['"])
    (?P<name>"""
    + _CREDENTIAL_NAME
    + r""")
    (?P=open)
    (?P<sep>\s*:\s*)
    (?P<quote>['"])
    (?P<value>[^'"\n]{4,})
    (?P=quote)
    """
)

# An address is `\b<local>@<domain>`: a local part that starts at a word boundary
# and runs up to the "@", then a domain ending in a dot and letters. Written as
# one pattern, the search started at every word boundary of a long dotted line
# and scanned to its end each time, with no "@" in sight: quadratic. The same
# addresses are found here from the "@" signs instead, each local part walked
# once, so a line is read a fixed number of times whatever it holds.
_EMAIL_LOCAL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-"
)
_EMAIL_LOCAL_START = re.compile(r"\b[A-Za-z0-9._%+\-]")
_EMAIL_DOMAIN = re.compile(r"[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")


def _redact_emails(text: str) -> str:
    done = 0  # end of the previous address: the next one cannot start before it
    at = text.find("@")
    if at == -1:
        return text
    out: list[str] = []
    while at != -1:
        start = at
        while start > done and text[start - 1] in _EMAIL_LOCAL_CHARS:
            start -= 1
        first = _EMAIL_LOCAL_START.search(text, start, at) if start < at else None
        domain = _EMAIL_DOMAIN.match(text, at + 1) if first else None
        if first and domain:
            out.append(text[done : first.start()])
            out.append("<REDACTED:email>")
            done = domain.end()
            at = text.find("@", done)
        else:
            at = text.find("@", at + 1)
    out.append(text[done:])
    return "".join(out)


# A DSN carries user:password@host. Keep the scheme and the host; drop the middle.
#
# The scheme is read once per run of scheme characters, from the start of the
# run. The rule it replaces, a scheme starting at any word boundary before a
# lowercase letter, tried every such letter of a long dotted run and scanned to
# the end of the run each time. Every start inside one run leads to the same
# "://" and the same credentials, so one attempt decides the same thing: the two
# conditions below say that the run holds at least one of those starts (its
# first character, or a letter after one of its "+", "." or "-").
_DSN_CREDENTIALS = re.compile(
    r"(?<![a-z0-9+.\-])"
    r"(?:\b(?=[a-z])|(?=[a-z0-9+.\-]*?[+.\-][a-z]))"
    r"(?P<scheme>[a-z0-9+.\-]*://)[^\s/@:]+:[^\s/@]+@"
)


def redact(text: str) -> str:
    """Return `text` with credential VALUES replaced and structure intact."""
    out = text
    for pattern in _SECRET_LITERALS:
        out = pattern.sub("<REDACTED:secret>", out)
    out = _DSN_CREDENTIALS.sub(r"\g<scheme><REDACTED:dsn-credentials>@", out)
    out = _NAMED_ASSIGNMENT.sub(
        lambda m: f"{m['name']}{m['sep']}{m['quote']}<REDACTED:secret>{m['quote']}", out
    )
    out = _QUOTED_KEY_ASSIGNMENT.sub(
        lambda m: (
            f"{m['open']}{m['name']}{m['open']}{m['sep']}"
            f"{m['quote']}<REDACTED:secret>{m['quote']}"
        ),
        out,
    )
    out = _NAMED_ASSIGNMENT_BARE.sub(
        lambda m: f"{m['name']}{m['sep']}<REDACTED:secret>", out
    )
    out = _redact_emails(out)
    return out


def redact_lines(lines: list[str]) -> list[str]:
    return [redact(line) for line in lines]
