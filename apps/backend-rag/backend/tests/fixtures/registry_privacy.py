"""Shared privacy helpers for the registry write routes' tests (slice 4C).

Every helper takes its vocabulary as a parameter (``Vocabulary``) and reads no module global of the
caller, so that one runner cannot bind a route to the vocabulary of another. The rules are the
explicit ones of the specification (7.3), numbered R1 to R9:

R1 declared probes: each case declares the identifier strings it puts into a request and the strings
   that live only in the probe world's database; nothing is discovered by searching a request for an
   inventory. For a raw request every run of four or more word characters must be declared.
R2 collection: request string leaves (keys included) and integer leaves; surfaces are every response
   text, every log record of every logger but httpx/httpcore, and every audit row, complete.
R3 forms: raw, upper-cased with white space normalised, and the canonical form for each identifier type.
R4 filters: applied to the original leaves, never to a declared probe.
R5 audit rows: every column scanned except columns of a timestamp, real, double or numeric TYPE;
   containers are walked, never serialised; a string that parses as JSON is walked decoded too;
   a float inside a scanned column or container is a scalar like any other and is scanned.
   The walker's table is SPEC_B4C_addendum_walker.md (W1 to W9); a response text goes through it too.

Limits (what THIS instrument can see): a number written inside a longer text is scanned as that
text only, the walker does not look for number literals in prose; renderings of a number other than
integer text, repr and positional expansion (digit grouping, another base, fewer significant digits,
a locale's separators) are outside the rule; the six interpreter-measured attributes of a log record
are left out by name and the audit columns of an unscanned TYPE by type.
When a caller supplies its clock window, canonical UUID4 values and ISO datetimes inside that call
are masked in log text because their digits are drawn or measured during the call, not derived from
the body, for the same reason as the six interpreter-measured attributes.
R8 suppression guard: the vocabulary filter must not silently remove a request string from a set.
R9 teeth: pure tests (in the caller's module) that these functions can fail.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from backend.services.crm.related_identifiers import ID_TYPES, canonical_form

MIN_LENGTH = 4
RUN = re.compile(r"[A-Za-z0-9_]{4,}")
UUID4 = re.compile(
    r"(?<![0-9A-Za-z])"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-4[0-9A-Fa-f]{3}-"
    r"[89AaBb][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Za-z])"
)
ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
JSON_LITERALS = frozenset({"true", "false", "null"})

# Audit columns whose digits are measured by the server, not derived from a body: scanned never,
# chosen by TYPE read from the catalog (never by name).
UNSCANNED_AUDIT_TYPES = frozenset(
    {
        "timestamp without time zone",
        "timestamp with time zone",
        "real",
        "double precision",
        "numeric",
    }
)

# Numbers the interpreter measures about a log call (three integers and three floats, not derived
# from any body, their digits arbitrary): the only log attributes left out of the scan, by name,
# and said so in the module that uses them. A float under any other attribute is scanned.
INTERPRETER_ATTRIBUTES = frozenset(
    {"thread", "process", "lineno", "created", "msecs", "relativeCreated"}
)
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def is_http_client_logger(name: str) -> bool:
    """The exact HTTP-client namespaces: the logger itself or a child, never a bare prefix."""
    return any(name == root or name.startswith(root + ".") for root in HTTP_CLIENT_LOGGERS)


PROTOCOL_CLASSES = frozenset(
    {"request key", "closed value the model accepts", "refused value M10 sends"}
    | {"JSON literal", "fixed output"}
)


@dataclass(frozen=True)
class Vocabulary:
    """What a route legitimately says: closed words, answer words, the plan hash, stored probes."""

    protocol_words: frozenset[str]  # lower-case; matched exactly and case-sensitively (R4)
    answer_words: frozenset[str]  # the answer vocabulary, for responses and audit rows only (R4)
    plan: str
    unconditional: frozenset[str]  # strings living only in the probe world's database


# ── leaves, forms, tokens ─────────────────────────────────────────────────────


def leaves(node: Any, strings: set[str], ints: set[int]) -> None:
    """String leaves (dictionary keys included) and integer leaves (booleans excluded)."""
    if isinstance(node, dict):
        for key, value in node.items():
            leaves(key, strings, ints)
            leaves(value, strings, ints)
    elif isinstance(node, list):
        for value in node:
            leaves(value, strings, ints)
    elif isinstance(node, bool):
        return
    elif isinstance(node, int):
        ints.add(node)
    elif isinstance(node, str):
        strings.add(node)


def raw_text(request: Any) -> str:
    """The raw text of a request: bytes decoded as Latin-1, a dictionary as unescaped JSON."""
    if isinstance(request, (bytes, bytearray)):
        return bytes(request).decode("latin-1")
    return json.dumps(request, ensure_ascii=False)


def forms(leaf: str) -> set[str]:
    """R3: raw, upper-cased with white space normalised, and canonical for each identifier type,
    all case-folded, the empty form dropped."""
    out = {leaf.casefold(), " ".join(leaf.upper().split()).casefold()}
    for id_type in sorted(ID_TYPES):
        out.add(canonical_form(id_type, leaf).casefold())
    out.discard("")
    return out


def request_leaves(request: Any) -> tuple[set[str], set[int]]:
    """R2: for a dictionary every string leaf and integer leaf; for raw bytes the runs of R1."""
    strings: set[str] = set()
    ints: set[int] = set()
    if isinstance(request, (bytes, bytearray)):
        strings.update(RUN.findall(raw_text(request)))
    else:
        leaves(request, strings, ints)
    return strings, ints


def filtered_leaves(vocab: Vocabulary, request: Any, *, answer_words: bool) -> set[str]:
    """R4 applied to the ORIGINAL leaves: at least four characters, not a protocol word (exact,
    case-sensitive), not the plan hash; for responses and audit rows also not a substring of a word
    of the answer vocabulary. Integer leaves are taken as decimal strings."""
    strings, ints = request_leaves(request)
    kept = set()
    answer_folded = [w.casefold() for w in vocab.answer_words]
    for leaf in strings | {str(i) for i in ints}:
        if len(leaf) < MIN_LENGTH or leaf in vocab.protocol_words or leaf == vocab.plan:
            continue
        if answer_words and any(leaf.casefold() in word for word in answer_folded):
            continue
        kept.add(leaf)
    return kept


def probe_forms(probes: Iterable[str]) -> set[str]:
    """Every non-empty form of every probe. No filter applies to a declared or stored probe."""
    out: set[str] = set()
    for probe in probes:
        out |= forms(probe)
    return out


def case_sets(
    vocab: Vocabulary,
    requests: list[Any],
    identifier_probes: Iterable[str],
    stored_probes: Iterable[str],
) -> tuple[set[str], set[str]]:
    """(log forbidden set, response/audit forbidden set) of a case.

    The stored probes are forbidden on every surface of every case; a declared identifier probe is
    forbidden once a request of the case carries it; every filtered request leaf is forbidden in all
    its forms. The two sets differ only by the answer-vocabulary filter (R4).
    """
    stored = probe_forms([*vocab.unconditional, *stored_probes])
    log_set, answer_set = set(stored), set(stored)
    declared = list(identifier_probes)
    for request in requests:
        text = raw_text(request).casefold()
        carried = probe_forms(p for p in declared if p.casefold() in text)
        log_set |= carried
        answer_set |= carried
        for leaf in filtered_leaves(vocab, request, answer_words=False):
            log_set |= forms(leaf)
        for leaf in filtered_leaves(vocab, request, answer_words=True):
            answer_set |= forms(leaf)
    return log_set, answer_set


# ── first tests: R1, R4, R8 and the provenance of the vocabulary ──────────────


def undeclared_runs(vocab: Vocabulary, request: bytes, declared: Iterable[str]) -> list[str]:
    """R1, raw requests: every run of four or more word characters must be a declared probe, a
    protocol word, the plan hash or a JSON literal. Returns the undeclared runs."""
    declared_folded = {d.casefold() for d in declared}
    return [
        run
        for run in RUN.findall(raw_text(request))
        if run.casefold() not in declared_folded
        and run not in vocab.protocol_words
        and run != vocab.plan
        and run not in JSON_LITERALS
    ]


def missing_declared(requests: list[Any], declared: Iterable[str]) -> list[str]:
    """R1: each declared identifier probe must occur, case-folded, in the raw text of a request."""
    texts = [raw_text(r).casefold() for r in requests]
    return [p for p in declared if not any(p.casefold() in t for t in texts)]


def collisions(probe_forms_: Iterable[str], legitimate: Iterable[str]) -> list[tuple[str, str]]:
    """R4: a non-empty form of a probe must not equal, or be contained in, a legitimate value."""
    return [(f, v) for f in probe_forms_ if f for v in legitimate if f == v or f in v]


def suppressed(
    vocab: Vocabulary, request: Any, log_set: set[str], answer_set: set[str]
) -> list[tuple[str, str, str]]:
    """R8: every non-protocol string of four or more characters of the request (the plan hash
    exempt) must be present, in every non-empty form, in BOTH sets. Returns (leaf, form, set)."""
    strings, _ = request_leaves(request)
    out = []
    for leaf in sorted(strings):
        if len(leaf) < MIN_LENGTH or leaf in vocab.protocol_words or leaf == vocab.plan:
            continue
        for form in sorted(forms(leaf)):
            if form not in log_set:
                out.append((leaf, form, "log"))
            if form not in answer_set:
                out.append((leaf, form, "answer"))
    return out


def unclassified_protocol_words(vocab: Vocabulary, origin: dict[str, str]) -> list[str]:
    """Provenance of the protocol set: every word has a class of a fixed list, and none is extra."""
    bad = [w for w in sorted(vocab.protocol_words) if origin.get(w) not in PROTOCOL_CLASSES]
    return bad + [w for w in sorted(origin) if w not in vocab.protocol_words]


# ── surfaces ──────────────────────────────────────────────────────────────────


def _number_texts(value: float | Decimal) -> list[str]:
    """W3/W4: the repr (or str) and, when finite, the positional expansion without an exponent,
    written from the shortest repr: the exact binary value is not what a reader sees."""
    if isinstance(value, Decimal):
        out = [str(value)]
        if value.is_finite():
            out.append(format(value, "f"))
        return out
    out = [repr(value)]
    if math.isfinite(value):
        out.append(format(Decimal(repr(value)), "f"))
    return out


def walk(value: Any, out: list[str]) -> None:
    """Table W: None and booleans nothing; integers their decimals; floats and Decimals their repr
    and positional expansion; bytes as Latin-1 text; a string itself and, when the WHOLE string
    parses as JSON (any string is tried), the decoded value walked again; containers key by key and
    item by item, never serialised (so a token cannot be split by escaping)."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (float, Decimal)):
        out.extend(_number_texts(value))
        return
    if isinstance(value, int):
        out.append(str(value))
        return
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("latin-1")
    if isinstance(value, str):
        out.append(value)
        try:
            decoded = json.loads(value)
        except ValueError:
            return
        walk(decoded, out)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            walk(key, out)
            walk(item, out)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            walk(item, out)
        return
    out.append(str(value))


def record_texts(record: logging.LogRecord) -> list[str]:
    """R2: the formatted text and every ``record.__dict__`` value, containers walked."""
    out = [logging.Formatter().format(record)]
    for name, value in record.__dict__.items():
        if name not in INTERPRETER_ATTRIBUTES:
            walk(value, out)
    return out


def response_surface(text: str) -> str:
    """The case-folded surface of a response text: the text itself and, being JSON, the decoded
    value walked by table W (a number in an answer is rendered like any other)."""
    out: list[str] = []
    walk(text, out)
    return " ".join(out).casefold()


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _is_datetime_inside_clock_window(value: str, low: datetime, high: datetime) -> bool:
    if ISO_DATETIME.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        _utc_naive(low) - timedelta(seconds=5)
        <= _utc_naive(parsed)
        <= _utc_naive(high) + timedelta(seconds=5)
    )


def _mask_volatile_log_text(text: str, clock_window: tuple[datetime, datetime]) -> str:
    low, high = clock_window
    text = UUID4.sub("<volatile-uuid>", text)
    if _is_datetime_inside_clock_window(text, low, high):
        return "<volatile-datetime>"
    return text


def log_surface(
    records: Iterable[logging.LogRecord],
    excluded: Iterable[logging.LogRecord] = (),
    clock_window: tuple[datetime, datetime] | None = None,
) -> str:
    """Every record of every logger but the HTTP-client namespaces, except the record OBJECTS in
    ``excluded`` (compared by identity, never by what a message contains)."""
    skip = list(excluded)
    return " ".join(
        text
        for r in records
        if not is_http_client_logger(r.name) and not any(r is e for e in skip)
        for record_text in record_texts(r)
        for text in (
            (_mask_volatile_log_text(record_text, clock_window),)
            if clock_window is not None
            else (record_text,)
        )
    ).casefold()


def audit_texts(rows: Iterable[dict[str, Any]], column_types: dict[str, str]) -> str:
    """R5: every column of every row except columns of an unscanned TYPE, walked."""
    out: list[str] = []
    for row in rows:
        for name, value in dict(row).items():
            if column_types.get(name) in UNSCANNED_AUDIT_TYPES:
                continue
            walk(value, out)
    return " ".join(out).casefold()


def leaks(tokens: Iterable[str], surface: str) -> list[str]:
    """The forbidden tokens (case-folded) that occur as a substring of a case-folded surface."""
    return sorted(t for t in tokens if t in surface)


def created_at_outside(rows: Iterable[dict[str, Any]], low: datetime, high: datetime) -> list[Any]:
    """R5: rows whose ``created_at`` does not lie between the two clock readings around the call."""
    return [r["created_at"] for r in rows if not low <= r["created_at"] <= high]


async def audit_snapshot(conn) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Complete audit rows and the data type of every column, read from the catalog."""
    columns = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'api_audit_trail' "
        "ORDER BY ordinal_position"
    )
    types = {c["column_name"]: c["data_type"] for c in columns}
    rows = [dict(r) for r in await conn.fetch("SELECT * FROM api_audit_trail ORDER BY id")]
    if rows:
        assert set(rows[0]) == set(types)
    return rows, types


async def audit_created_at_is_server_generated(conn, log_api_call_source: str) -> None:
    """R5: the one excluded column is server-generated: catalog default ``now()`` and the audit
    INSERT names no ``created_at``."""
    default = await conn.fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'api_audit_trail' "
        "AND column_name = 'created_at'"
    )
    assert default == "now()", default
    insert = log_api_call_source.split("INSERT INTO api_audit_trail", 1)[1].split(")", 1)[0]
    assert "created_at" not in insert
