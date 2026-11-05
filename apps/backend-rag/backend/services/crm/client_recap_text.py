"""Deterministic template, validator and rewrite providers for the kita AI recap.

Everything here reads ONLY the allow-listed payload produced by
``client_recap.recap_payload``; nothing in this module can see a name or an identifier.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MAX_CHARS = 450
# Bump whenever the template wording, the validator or the allow-list changes: it is hashed
# with the facts, so cached texts written by older logic are regenerated.
RECAP_SCHEMA_VERSION = "2026-11-04.v4"
_MONTHS = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]  # fmt: skip
_MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_NUM.update({m[:3].lower(): i + 1 for i, m in enumerate(_MONTHS)})


# ── formatting ──────────────────────────────────────────────────────────────


def _d(iso: str | None) -> date | None:
    try:
        return date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def fmt_date(iso: str | None) -> str | None:
    d = _d(iso)
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}" if d else None


def fmt_idr(n: int | None) -> str | None:
    if n is None:
        return None
    for unit, size in (("trillion", 10**12), ("billion", 10**9), ("million", 10**6)):
        if n >= size:
            return f"IDR {n / size:.1f} {unit}"
    return f"IDR {n:,}"


_WORD = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def num(n: int) -> str:
    return _WORD[n] if 0 <= n <= 10 else str(n)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def _strip_code(name: str, code: str | None) -> str:
    return re.sub(rf"^{re.escape(code)}\s*[-–—]\s*", "", name) if code else name


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _sentences(clauses: list[str]) -> list[str]:
    """First clause is a sentence on its own; the rest are folded into at most two more."""
    if not clauses:
        return []
    rest = clauses[1:]
    out = [clauses[0]]
    if len(rest) <= 2:
        out += rest
    else:
        out += [_join(rest[:2]), _join(rest[2:])]
    return [_cap(s) + "." for s in out]


def _fit(clauses: list[str]) -> str:
    while clauses:
        text = " ".join(_sentences(clauses))
        if len(text) <= MAX_CHARS or len(clauses) == 1:
            return text
        clauses = clauses[:-1]
    return ""


# ── groups present ──────────────────────────────────────────────────────────


def based_on(scope: str, payload: Mapping[str, Any]) -> list[str]:
    p = payload
    if scope == "immigration":
        pairs = [
            ("current permit", "current_permit"), ("ITK", "itk_expiry_date"),
            ("passport", "passport_expiry_date"), ("family", "family_members_count"),
            ("practices", "open_practices"), ("previous permits", "previous_permits_count"),
        ]  # fmt: skip
    else:
        pairs = [
            ("legal form", "legal_form"), ("establishment", "established_date"),
            ("capital", "authorized_capital_idr"), ("ownership", "shareholders_count"),
            ("KBLI", "kbli"), ("LKPM", "lkpm_latest"), ("amendments", "last_amendment_date"),
        ]  # fmt: skip
    out = [label for label, key in pairs if p.get(key) not in (None, [], {}, 0)]
    if scope == "company" and "capital" not in out and p.get("paid_up_capital_idr") is not None:
        out.append("capital")
    return out


# ── template ────────────────────────────────────────────────────────────────


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Makassar")).date()


def _imm_clauses(p: Mapping[str, Any], today: date) -> list[str]:
    clauses: list[str] = []
    cp = p.get("current_permit") or {}
    code, name = cp.get("code"), cp.get("official_name")
    expiry = _d(cp.get("expiry_date"))
    expired = cp.get("status") == "expired"
    verb = "expired on" if expired else "valid until"
    which = "latest" if expired else "current"
    label = None
    if name:
        label = f"{_strip_code(name, code)} ({code})" if code and code != name else name
    elif code:
        label = code
    if label:
        s = f"the client's {which} permit is {label}"
        if cp.get("issue_date"):
            s += f", issued on {fmt_date(cp['issue_date'])}"
            if expiry:
                s += f" and {verb} {fmt_date(cp['expiry_date'])}"
        elif expiry:
            s += f", {verb} {fmt_date(cp['expiry_date'])}"
        clauses.append(s)
    elif expiry:
        clauses.append(f"the client's {which} permit is {verb} {fmt_date(cp['expiry_date'])}")
    else:
        clauses.append("no current stay permit is recorded for the client")

    practices = p.get("open_practices") or []
    if practices:
        n = len(practices)
        types = list(dict.fromkeys(x["type"] for x in practices if x.get("type")))[:4]
        s = (
            "an immigration practice is open"
            if n == 1
            else f"{num(n)} immigration practices are open"
        )
        if types:
            lead = ": " if n == 1 or len(types) >= n else ", including "
            s += lead + _join(types)
        clauses.append(s)
    itk = _d(p.get("itk_expiry_date"))
    if itk and ("kitas" in str(name or "").lower() or str(code or "").startswith("E")):
        itk_expired = p["itk_status"] == "expired" if p.get("itk_status") else itk < today
        v = "expired on" if itk_expired else "runs until"
        clauses.append(f"the ITK stamp {v} {fmt_date(p['itk_expiry_date'])}")
    pe = _d(p.get("passport_expiry_date"))
    if pe and (expiry is None or pe <= expiry + timedelta(days=183)):
        pp_expired = p["passport_status"] == "expired" if p.get("passport_status") else pe < today
        v = "expired on" if pp_expired else "expires on"
        clauses.append(f"the passport {v} {fmt_date(p['passport_expiry_date'])}")
    fam_n = p.get("family_members_count") or 0
    if fam_n:
        s = f"{num(fam_n)} family {_plural(fam_n, 'member is', 'members are')} on file"
        k = len(
            [
                f
                for f in p.get("family_permits") or []
                if expiry and (fe := _d(f.get("permit_expiry_date"))) and fe < expiry
            ]
        )
        if k:
            s += (
                ", one of them with a permit that ends before the main permit"
                if k == 1
                else f", {num(k)} of them with permits that end before the main permit"
            )
        clauses.append(s)
    prev = p.get("previous_permits_count") or 0
    if prev:
        clauses.append(f"{num(prev)} earlier {_plural(prev, 'permit is', 'permits are')} on record")
    return clauses


def _company_clauses(p: Mapping[str, Any]) -> list[str]:
    clauses: list[str] = []
    form, est = p.get("legal_form"), p.get("established_date")
    if form and est:
        clauses.append(f"the company is a {form}, established on {fmt_date(est)}")
    elif form:
        clauses.append(f"the company is a {form}")
    elif est:
        clauses.append(f"the company was established on {fmt_date(est)}")
    auth, paid = fmt_idr(p.get("authorized_capital_idr")), fmt_idr(p.get("paid_up_capital_idr"))
    if auth and paid:
        clauses.append(f"authorized capital is {auth}, of which {paid} is paid up")
    elif auth:
        clauses.append(f"authorized capital is {auth}")
    elif paid:
        clauses.append(f"paid-up capital is {paid}")
    sh, di = p.get("shareholders_count"), p.get("directors_count")
    bits = []
    if sh:
        bits.append(f"{num(sh)} {_plural(sh, 'shareholder')}")
    if di:
        bits.append(f"{num(di)} {_plural(di, 'director')}")
    if bits:
        clauses.append("it has " + " and ".join(bits))
    kbli = p.get("kbli") or []
    if kbli:
        s = f"it is registered under {num(len(kbli))} KBLI {_plural(len(kbli), 'line')}"
        titles = [k["title"] for k in kbli if k.get("title")][:2]
        if titles:
            s += " (" + ", ".join(titles) + ")"
        old = sum(1 for k in kbli if k.get("in_2025") is False)
        if old:
            s += f", and {num(old)} {_plural(old, 'code')} still on KBLI 2020"
        clauses.append(s)
    lk = p.get("lkpm_latest") or {}
    if lk.get("year") and lk.get("quarter"):
        s = f"the latest LKPM on record is {lk['quarter']} {lk['year']}"
        if lk.get("status"):
            s += f" ({lk['status']})"
        clauses.append(s)
    if p.get("last_amendment_date"):
        clauses.append(f"the last amendment dates from {fmt_date(p['last_amendment_date'])}")
    return clauses


def template_recap(scope: str, payload: Mapping[str, Any], today: date | None = None) -> str:
    if not payload:
        return ""
    clauses = (
        _imm_clauses(payload, today or _today())
        if scope == "immigration"
        else _company_clauses(payload)
    )
    return _fit(clauses)


# ── validator ───────────────────────────────────────────────────────────────

_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_MDY_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")
_SLASH_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MY_RE = re.compile(r"\b([A-Za-z]{3,9})\s+(\d{4})\b")
_MONEY_RE = re.compile(
    r"\b(?:IDR|Rp\.?)\s*([\d][\d.,]*)(?:\s*(trillion|billion|million))?", re.IGNORECASE
)
_CODE_RE = re.compile(r"\b(?:[A-Z]\d{1,2}[A-Z]?|\d{4,5})\b")
_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?![\w])")
_RELATIVE_RE = re.compile(
    r"\b(in\s+(?:\d+|a|an|one|two|three|four|five|six|several|a few)\s+(?:day|week|month|year)s?"
    r"|soon|shortly|recently|tomorrow|yesterday|today|ago|upcoming"
    r"|(?:next|last|this|coming)\s+(?:day|week|month|year|quarter)"
    r"|(?:days?|weeks?|months?|years?)\s+(?:left|remaining|away))\b",
    re.IGNORECASE,
)
_SECOND_PERSON_RE = re.compile(r"\b(you|your|yours)\b", re.IGNORECASE)
_META_RE = re.compile(
    r"\b(as stated|provided|records? (?:indicates?|shows?)|facts?|firm|in our system|finalized"
    r"|as of the latest|associated with this (?:case|application)|without any additions"
    r"|applicants?|the data|the draft)\b",
    re.IGNORECASE,
)
_WORD_VALUE = {w: i for i, w in enumerate(_WORD) if i >= 2}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORD_VALUE) + r")\b")
_UNIT = {"trillion": 10**12, "billion": 10**9, "million": 10**6, None: 1, "": 1}


def _leaves(v: Any):
    if isinstance(v, Mapping):
        for x in v.values():
            yield from _leaves(x)
    elif isinstance(v, Sequence) and not isinstance(v, (str, bytes)):
        for x in v:
            yield from _leaves(x)
    else:
        yield v


def _payload_facts(payload: Mapping[str, Any]) -> tuple[set[date], set[str], set[int], list[int]]:
    dates: set[date] = set()
    codes: set[str] = set()
    ints: set[int] = {1, 2020, 2025}
    amounts: list[int] = []
    for key, val in payload.items():
        if key.endswith("_idr") and isinstance(val, int):
            amounts.append(val)
    for leaf in _leaves(payload):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, int):
            ints.add(leaf)
        elif isinstance(leaf, str):
            d = _d(leaf)
            if d:
                dates.add(d)
                ints.add(d.year)
            codes.update(_CODE_RE.findall(leaf.upper() if len(leaf) <= 16 else leaf))
    for v in payload.values():
        if isinstance(v, (list, tuple)):
            ints.add(len(v))
    kbli = payload.get("kbli") or []
    ints.add(sum(1 for k in kbli if k.get("in_2025") is False))
    ints.add(len(payload.get("previous_permit_codes") or []))
    permit_end = _d((payload.get("current_permit") or {}).get("expiry_date"))
    if permit_end:
        ints.add(
            sum(
                1
                for f in payload.get("family_permits") or []
                if (fe := _d(f.get("permit_expiry_date"))) and fe < permit_end
            )
        )
    return dates, codes, ints, amounts


def _text_dates(text: str) -> tuple[list[date | tuple[int, int] | None], str]:
    found: list[date | tuple[int, int] | None] = []

    def take(rx: re.Pattern[str], build) -> None:
        nonlocal text
        for m in list(rx.finditer(text)):
            found.append(build(m))
        text = rx.sub(" ", text)

    def mk(y: int, mo: int, d: int) -> date | None:
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    take(_ISO_RE, lambda m: mk(int(m[1]), int(m[2]), int(m[3])))
    take(_SLASH_RE, lambda m: mk(int(m[3]), int(m[2]), int(m[1])))
    take(_DMY_RE, lambda m: mk(int(m[3]), _MONTH_NUM.get(m[2].lower(), 0), int(m[1])))
    take(_MDY_RE, lambda m: mk(int(m[3]), _MONTH_NUM.get(m[1].lower(), 0), int(m[2])))
    for m in list(_MY_RE.finditer(text)):
        if m[1].lower() in _MONTH_NUM:
            found.append((int(m[2]), _MONTH_NUM[m[1].lower()]))
    text = _MY_RE.sub(lambda m: " " if m[1].lower() in _MONTH_NUM else m[0], text)
    return found, text


def _infer_scope(payload: Mapping[str, Any]) -> str:
    return (
        "company"
        if set(payload)
        & {"legal_form", "kbli", "lkpm_latest", "established_date", "shareholders_count"}
        else "immigration"
    )


_BANNED_RE = re.compile(
    r"\b(base text|official documentation|as described|as recorded|as stated|absolute|relative"
    r"|without further|specified|draft|instructions?|source data|exactly|unchanged|in effect"
    r"|no amendments?|status applies|out of which)\b",
    re.IGNORECASE,
)
_EXPIRED_RE = re.compile(r"\b(expired|lapsed|has expired|remains expired)\b", re.IGNORECASE)
_VALID_RE = re.compile(
    r"\b(valid|expires|expiring|expire|runs|until|through|ends)\b", re.IGNORECASE
)
_PERMIT_NOUN_RE = re.compile(r"\b(kitas|kitap|e-?visa|visa|permit|itk|itas|itap)\b", re.IGNORECASE)


def _tokenise_dates(text: str) -> tuple[str, dict[str, date | None]]:
    marks: dict[str, date | None] = {}

    def sub(rx: re.Pattern[str], build) -> None:
        nonlocal text

        def rep(m: re.Match[str]) -> str:
            key = f"DATE{len(marks)}X"
            try:
                marks[key] = build(m)
            except ValueError:
                marks[key] = None
            return key

        text = rx.sub(rep, text)

    sub(_ISO_RE, lambda m: date(int(m[1]), int(m[2]), int(m[3])))
    sub(_SLASH_RE, lambda m: date(int(m[3]), int(m[2]), int(m[1])))
    sub(_DMY_RE, lambda m: date(int(m[3]), _MONTH_NUM[m[2].lower()], int(m[1])))
    sub(_MDY_RE, lambda m: date(int(m[3]), _MONTH_NUM[m[1].lower()], int(m[2])))
    return text, marks


def _semantic_reasons(text: str, payload: Mapping[str, Any], scope: str, today: date) -> list[str]:
    reasons: list[str] = []
    low = text.lower()
    if scope == "immigration" and re.search(r"\bcompan(?:y|ies)\b", low):
        reasons.append("wrong_subject")
    if scope == "company" and re.search(r"\bclients?\b", low):
        reasons.append("wrong_subject")
    banned = _BANNED_RE.search(text)
    if banned:
        status = (payload.get("lkpm_latest") or {}).get("status")
        word = banned.group(0).lower()
        if not (word == "draft" and status == "draft" and re.search(r"\(draft\)", low)):
            reasons.append("leak_phrase")
    tokenised, marks = _tokenise_dates(text)
    cp = payload.get("current_permit") or {}
    permit_date = _d(cp.get("expiry_date"))
    passport_date = _d(payload.get("passport_expiry_date"))
    for sentence in re.split(r"(?<=[.!?])\s+", tokenised):
        sentence_dates = [marks[k] for k in re.findall(r"DATE\d+X", sentence)]
        if permit_date and permit_date in sentence_dates and permit_date != passport_date:
            if not _PERMIT_NOUN_RE.search(sentence):
                reasons.append("permit_date_misplaced")
        for clause in re.split(r"[,;]", sentence):
            words = clause.split()
            for i, w in enumerate(words):
                m = re.fullmatch(r"DATE\d+X\W*", w)
                d = marks.get(w.strip(".,;:()")) if m else None
                if d is None:
                    continue
                window = " ".join(words[max(0, i - 5) : i]).lower()
                if re.search(r"amendment|established|lkpm|incorporat", window) and re.search(
                    r"\b(until|runs|valid|expires?|ends|through|expired)\b", window
                ):
                    reasons.append("validity_word_on_non_validity_item")
                    continue
                if re.search(r"\b(issued|established|since|from|dates|amendment|filed)\b", window):
                    continue
                if _EXPIRED_RE.search(window):
                    if d >= today:
                        reasons.append("expired_in_future")
                elif _VALID_RE.search(window):
                    if d < today:
                        reasons.append("valid_in_past")
                clause_low = clause.lower()
                if passport_date and d == passport_date and d != permit_date:
                    if "passport" not in clause_low or _PERMIT_NOUN_RE.search(clause):
                        reasons.append("passport_date_misplaced")
                if (
                    permit_date
                    and d == permit_date
                    and d != passport_date
                    and "passport" in clause_low
                ):
                    reasons.append("permit_date_misplaced")
    return reasons


def _dropped_facts(template: str, text: str, payload: Mapping[str, Any]) -> list[str]:
    """Facts the deterministic template states that the rewrite must still state."""
    from collections import Counter

    out: list[str] = []
    t_found, t_rest = _text_dates(template)
    x_found, x_rest = _text_dates(text)
    if not {f for f in t_found if isinstance(f, date)} <= {
        f for f in x_found if isinstance(f, date)
    }:
        out.append("dropped_date")

    def amounts(s: str) -> list[float]:
        return [
            float(m[1].replace(",", "")) * _UNIT[(m[2] or "").lower() or None]
            for m in _MONEY_RE.finditer(s)
            if m[1].replace(",", "").replace(".", "", 1).isdigit()
        ]

    x_amounts = amounts(x_rest)
    if any(not any(abs(a - b) <= max(a * 0.006, 1) for b in x_amounts) for a in amounts(t_rest)):
        out.append("dropped_amount")
    t_clean, x_clean = _MONEY_RE.sub(" ", t_rest), _MONEY_RE.sub(" ", x_rest)
    if not set(_CODE_RE.findall(t_clean)) <= set(_CODE_RE.findall(x_clean)):
        out.append("dropped_code")
    name = (payload.get("current_permit") or {}).get("official_name")
    if name and name.lower() in template.lower() and name.lower() not in text.lower():
        out.append("dropped_name")

    def counts(s: str) -> Counter[int]:
        c: Counter[int] = Counter(_WORD_VALUE[w] for w in _WORD_RE.findall(s.lower()))
        for n in _NUM_RE.findall(_CODE_RE.sub(" ", s)):
            if n.isdigit() and 2 <= int(n) <= 10:
                c[int(n)] += 1
        return c

    tc, xc = counts(t_clean), counts(x_clean)
    if any(xc[k] < v for k, v in tc.items()):
        out.append("dropped_count")
    low = text.lower()
    for phrase, needle in (
        ("expired on", r"expired|lapsed"),
        ("valid until", r"valid"),
        ("expires on", r"expir|until|valid|ends"),
        ("runs until", r"until|valid|expir|runs|ends"),
    ):
        if phrase in template and not re.search(needle, low):
            out.append("dropped_status")
            break
    return out


def validate_recap(
    text: str | None,
    payload: Mapping[str, Any],
    scope: str | None = None,
    today: date | None = None,
    template: str | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not text or not text.strip():
        return False, ["empty"]
    text = text.strip()
    scope = scope or _infer_scope(payload)
    if len(text) > MAX_CHARS:
        reasons.append("too_long")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not 2 <= len(sentences) <= 3:
        reasons.append(f"sentence_count_{len(sentences)}")
    if re.search(r"[*#`]|^\s*[-•]\s", text, re.MULTILINE):
        reasons.append("markdown")
    if _RELATIVE_RE.search(text):
        reasons.append("relative_time")
    if _SECOND_PERSON_RE.search(text):
        reasons.append("second_person")
    if _META_RE.search(text):
        reasons.append("meta_phrase")
    reasons += _semantic_reasons(text, payload, scope, today or _today())
    dates, codes, ints, amounts = _payload_facts(payload)
    year_months = {(d.year, d.month) for d in dates}
    found, rest = _text_dates(text)
    for f in found:
        ok = (f in dates) if isinstance(f, date) else (f in year_months)
        if f is None or not ok:
            reasons.append("invented_date")
            break
    for m in _MONEY_RE.finditer(rest):
        try:
            value = float(m[1].replace(",", "")) * _UNIT[(m[2] or "").lower() or None]
        except ValueError:
            reasons.append("bad_amount")
            continue
        if not any(abs(value - a) <= max(a * 0.006, 1) for a in amounts):
            reasons.append("invented_amount")
    rest = _MONEY_RE.sub(" ", rest)
    for c in _CODE_RE.findall(rest):
        if c.isdigit() and len(c) == 4 and int(c) in ints:
            continue
        if c not in codes:
            reasons.append("invented_code")
            break
    for w in _WORD_RE.findall(text.lower()):
        if _WORD_VALUE[w] not in ints:
            reasons.append("invented_number")
            break
    rest_nocode = _CODE_RE.sub(" ", rest)
    for n in _NUM_RE.findall(rest_nocode):
        try:
            if int(n.replace(",", "")) not in ints:
                reasons.append("invented_number")
                break
        except ValueError:
            reasons.append("invented_number")
            break
    if template:
        reasons += _dropped_facts(template, text, payload)
    return (not reasons), sorted(set(reasons))


# ── rewrite providers ───────────────────────────────────────────────────────


class RecapProvider(Protocol):
    name: str
    model: str

    async def generate(self, prompt: str) -> str | None: ...


def rewrite_prompt(
    scope: str,
    template: str,
    payload: Mapping[str, Any],
    reasons: Sequence[str] = (),
) -> str:
    import json

    subject, other = (
        ("the client", "the company") if scope == "immigration" else ("the company", "the client")
    )
    prompt = (
        "Rewrite the base text below as 2 or 3 plain English sentences for internal staff notes. "
        f'The only subject is {subject}: refer to it as "{subject}" or with "it"/"its", and '
        f'never mention {other}. Never address the reader and never use "you" or "your". '
        "Keep every fact of the base text, add nothing, invent nothing, and write no comment "
        "about the text, the facts or these instructions. Keep each status word with its date "
        "exactly as in the base text (expired on, valid until, expires on, runs until), and keep "
        "every date attached to the same item it belongs to (permit, passport, ITK stamp, "
        'amendment). Write dates like 14 March 2027, never relative time such as "in 3 months" '
        'or "soon". No advice, no markdown, no bullet points, at most 450 characters. Output '
        "only the recap.\n\nFACTS (JSON):\n"
        + json.dumps(payload, sort_keys=True, ensure_ascii=True)
        + "\n\nBASE TEXT:\n"
        + template
    )
    if reasons:
        prompt += (
            "\n\nYour previous attempt was rejected for: "
            + ", ".join(reasons)
            + ". Fix exactly that and answer again."
        )
    return prompt


async def rewrite_with_retry(
    provider: RecapProvider,
    template: str,
    payload: Mapping[str, Any],
    scope: str | None = None,
) -> tuple[str | None, bool, bool, list[str]]:
    """(text, ok, first_try_ok, reasons). One retry with the validator's reasons appended."""
    scope = scope or _infer_scope(payload)
    text = await provider.generate(rewrite_prompt(scope, template, payload))
    ok, reasons = validate_recap(text, payload, scope, template=template)
    if ok:
        return text, True, True, []
    text2 = await provider.generate(rewrite_prompt(scope, template, payload, reasons))
    ok2, reasons2 = validate_recap(text2, payload, scope, template=template)
    return (text2 if ok2 else text), ok2, False, (reasons if ok2 else reasons2)


def _clean(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"<think>.*?</think>", " ", raw, flags=re.DOTALL)
    text = re.sub(r"[*_#`>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip().strip('"')
    return text or None


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str | None = None, timeout: float = 90.0) -> None:
        self.model = model or os.getenv("RECAP_OLLAMA_MODEL", "qwen3.5:9b")
        self.timeout = timeout

    async def generate(self, prompt: str) -> str | None:
        from backend.llm.ollama_client import ollama_generate

        raw = await ollama_generate(
            prompt, model=self.model, temperature=0.2, max_tokens=300, timeout=self.timeout
        )
        return _clean(raw)


def configured_provider() -> RecapProvider | None:
    choice = os.getenv("RECAP_REWRITE_PROVIDER", "").strip().lower()
    if choice == "ollama":
        return OllamaProvider()
    # No cloud provider: the recap facts are client data, and a cloud SDK logs
    # its own exception text. "gemini" (or any other value) means no rewrite.
    return None
