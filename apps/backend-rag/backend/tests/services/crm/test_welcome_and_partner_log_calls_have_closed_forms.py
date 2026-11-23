"""Every log call of the welcome and partner senders, read from the source.

These five modules log only closed facts (``services/crm/welcome/log_facts.py``): our own
record ids, members of sets written in the source, and words chosen in the source. A test that
drives a sender proves the lines it happens to reach. This file proves the rest: it reads the
whole syntax tree of each module and holds four things.

1. The inventory. Every log call, as (function, call, message, arguments as the parser
   writes them back), equals the table written below, row for row. A log call that is added,
   moved to another function or changed fails here until a person changes the table.
2. The forms. A message is one string literal with only ``%s`` and ``%d``; no keyword
   argument; ``%d`` takes ``len(...)``; ``%s`` takes a call to one of the five functions,
   ``bool(...)``, a string literal, a local name bound only from literals and those calls, or
   the one clock expression. So a log record holds only exact ``str``, ``int`` and ``bool``.
3. The bindings. ``id_fact`` and ``uuid_fact`` are given a parameter that the function binds
   nowhere, or a row read whose whole producer chain is the one written below: every name of
   the chain bound exactly once, by that expression, and never written through.
4. The logger. Each module binds ``logger`` once, at module level, to
   ``logging.getLogger(__name__)``; nothing else logs, and the five functions are the ones
   imported from ``log_facts``.

What it does not prove is said, not tested: a pinned name can still be changed through
another name (an alias, a callee). The reading pins syntax and bindings, not immutability.

The reading proves itself on samples: each guilty sample is one of the real modules with one
edit, and must be refused for the reason named beside it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[3]
MODULES = {
    "welcome_email_service": "services/crm/welcome/welcome_email_service.py",
    "welcome_practice_service": "services/crm/welcome/welcome_practice_service.py",
    "welcome_whatsapp_service": "services/crm/welcome/welcome_whatsapp_service.py",
    "partners.emails": "services/crm/partners/emails.py",
    "partners.repository": "services/crm/partners/repository.py",
}

FACTS_MODULE = "backend.services.crm.welcome.log_facts"
FACT_ARITY = {"error_word": 1, "status_fact": 1, "id_fact": 1, "uuid_fact": 1, "choice_fact": 2}
CHOICE_SETS = frozenset({"LANGUAGES", "COMMISSION_STATUSES", "COMMISSION_ENTRY_TYPES"})
ALLOWED_LEVELS = frozenset({"debug", "info", "warning", "error"})
LOG_METHODS = ALLOWED_LEVELS | {"warn", "exception", "critical", "fatal", "log"}
CLOCK = 'send_at.strftime("%H:%M")'
CLOCK_BINDING = "datetime.now(timezone.utc) + timedelta(minutes=WELCOME_DELAY_MINUTES)"
NO_METHOD_CALLS = frozenset({"rows", "row", "c"})

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Chain:
    """Where a function may give ``id_fact`` or ``uuid_fact`` something read from a row."""

    reads: tuple[str, ...]
    producers: dict[str, tuple[str, str]]  # name -> ("assign" or "for", the expression as written)


def source_of(module: str) -> str:
    return (_BACKEND / MODULES[module]).read_text(encoding="utf-8")


def _same(node: ast.AST, written: str) -> bool:
    """The node is the expression ``written``, compared as syntax without positions."""
    return ast.dump(node) == ast.dump(ast.parse(written, mode="eval").body)


def _is_call(node: ast.AST, name: str, arity: int) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        and len(node.args) == arity
        and not node.keywords
        and not any(isinstance(argument, ast.Starred) for argument in node.args)
    )


def _is_fact_call(node: ast.AST) -> bool:
    for name, arity in FACT_ARITY.items():
        if _is_call(node, name, arity):
            if name != "choice_fact":
                return True
            choices = node.args[1]  # type: ignore[attr-defined]
            return isinstance(choices, ast.Name) and choices.id in CHOICE_SETS
    return False


def _is_closed_concat(node: ast.AST) -> bool:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_closed_concat(node.left) and _is_closed_concat(node.right)
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)) or _is_fact_call(node)


def _bindings(scope: ast.AST, name: str) -> list[ast.AST]:
    """Every place inside ``scope`` that binds, rebinds or unbinds ``name``, nested scopes too."""
    found: list[ast.AST] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Name):
            binds = node.id == name and isinstance(node.ctx, (ast.Store, ast.Del))
        elif isinstance(node, ast.arg):
            binds = node.arg == name
        elif isinstance(node, ast.alias):
            binds = (node.asname or node.name.split(".")[0]) == name
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            binds = node.name == name and node is not scope
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            binds = node.name == name
        elif isinstance(node, ast.MatchMapping):
            binds = node.rest == name
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            binds = name in node.names
        else:
            binds = False
        if binds:
            found.append(node)
    return found


def _root(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Subscript, ast.Attribute, ast.Starred)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class Reading:
    def __init__(self, source: str) -> None:
        self.tree = ast.parse(source)
        self.parent: dict[ast.AST, ast.AST] = {
            child: node for node in ast.walk(self.tree) for child in ast.iter_child_nodes(node)
        }

    def function_of(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, _FUNCTIONS):
                return node
        return None

    def where(self, node: ast.AST) -> str:
        """The functions and classes around ``node``, outermost first."""
        names: list[str] = []
        while node in self.parent:
            node = self.parent[node]
            if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
                names.append(node.name)
        return ".".join(reversed(names)) or "<module>"

    def functions(self) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
        found = {}
        for node in ast.walk(self.tree):
            if isinstance(node, _FUNCTIONS):
                outer = self.where(node)
                found[node.name if outer == "<module>" else f"{outer}.{node.name}"] = node
        return found

    def log_calls(self) -> list[ast.Call]:
        """Every call that looks like a log call, whatever it is called on."""
        calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOG_METHODS
        ]
        return sorted(calls, key=lambda call: (call.lineno, call.col_offset))


def inventory(source: str) -> list[tuple[str, str, str, tuple[str, ...]]]:
    reading = Reading(source)
    rows = []
    for call in reading.log_calls():
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            message = first.value
        else:
            message = f"<not a literal: {ast.unparse(first) if first else ''}>"
        written = [ast.unparse(argument) for argument in call.args[1:]]
        written += [f"{keyword.arg}={ast.unparse(keyword.value)}" for keyword in call.keywords]
        rows.append((reading.where(call), ast.unparse(call.func), message, tuple(written)))
    return rows


def violations(source: str, chains: dict[str, Chain] | None = None) -> list[str]:
    reading = Reading(source)
    chains = chains or {}
    found = _logger_violations(reading) + _name_violations(reading)
    for call in reading.log_calls():
        found += [f"line {call.lineno}: {reason}" for reason in _form_violations(reading, call)]
    return found + _binding_violations(reading, chains) + _chain_violations(reading, chains)


def _logger_violations(reading: Reading) -> list[str]:
    tree, found = reading.tree, []
    bound = _bindings(tree, "logging")
    imported = (
        len(bound) == 1
        and isinstance(bound[0], ast.alias)
        and bound[0].name == "logging"
        and bound[0].asname is None
        and isinstance(reading.parent[bound[0]], ast.Import)
        and reading.parent[reading.parent[bound[0]]] is tree
    )
    if not imported or any(
        isinstance(node, ast.ImportFrom) and node.module == "logging" for node in ast.walk(tree)
    ):
        found.append("logger: `logging` is not imported once, whole, at module level")
    bound = _bindings(tree, "logger")
    statement = reading.parent.get(bound[0]) if len(bound) == 1 else None
    if not (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and statement.targets[0] is bound[0]
        and reading.parent[statement] is tree
        and _same(statement.value, "logging.getLogger(__name__)")
    ):
        found.append("logger: `logger` is not bound once, at module level, to getLogger(__name__)")
    makers = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Attribute) and node.attr == "getLogger")
        or (isinstance(node, ast.Name) and node.id == "getLogger")
    ]
    if len(makers) != 1:
        found.append("logger: a second logger is made")
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        attribute = reading.parent.get(node)
        if node.id == "logging":
            if not (isinstance(attribute, ast.Attribute) and attribute.attr == "getLogger"):
                found.append(f"line {node.lineno}: logger: `logging` is used to log")
        elif node.id == "logger":
            call = reading.parent.get(attribute) if attribute is not None else None
            if not (
                isinstance(attribute, ast.Attribute)
                and attribute.attr in LOG_METHODS
                and isinstance(call, ast.Call)
                and call.func is attribute
            ):
                found.append(f"line {node.lineno}: logger: `logger` is renamed or handed on")
    return found


def _name_violations(reading: Reading) -> list[str]:
    tree, found = reading.tree, []
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for name in sorted((set(FACT_ARITY) | CHOICE_SETS) & used):
        bound = _bindings(tree, name)
        statement = reading.parent.get(bound[0]) if len(bound) == 1 else None
        if not (
            isinstance(bound[0], ast.alias)
            and bound[0].asname is None
            and isinstance(statement, ast.ImportFrom)
            and statement.module == FACTS_MODULE
            and statement.level == 0
            and reading.parent[statement] is tree
        ):
            found.append(f"names: `{name}` is not the one imported from log_facts")
    found += [f"names: `{name}` is rebound" for name in ("bool", "len") if _bindings(tree, name)]
    return found


def _clock_is_pinned(reading: Reading, function: ast.AST) -> bool:
    tree = reading.tree
    for name in ("datetime", "timezone", "timedelta"):
        bound = _bindings(tree, name)
        statement = reading.parent.get(bound[0]) if len(bound) == 1 else None
        if not (
            isinstance(bound[0], ast.alias)
            and bound[0].asname is None
            and isinstance(statement, ast.ImportFrom)
            and statement.module == "datetime"
            and reading.parent[statement] is tree
        ):
            return False
    bound = _bindings(tree, "WELCOME_DELAY_MINUTES")
    statement = reading.parent.get(bound[0]) if len(bound) == 1 else None
    if not (
        isinstance(statement, ast.Assign)
        and reading.parent[statement] is tree
        and isinstance(statement.value, ast.Constant)
        and type(statement.value.value) is int
    ):
        return False
    bound = _bindings(function, "send_at")
    statement = reading.parent.get(bound[0]) if len(bound) == 1 else None
    return (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and statement.targets[0] is bound[0]
        and _same(statement.value, CLOCK_BINDING)
    )


def _is_reason_name(reading: Reading, name: str, function: ast.AST) -> bool:
    bound = _bindings(function, name)
    for binding in bound:
        statement = reading.parent.get(binding)
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and statement.targets[0] is binding
            and _is_closed_concat(statement.value)
        ):
            return False
    return bool(bound)


def _is_closed_text(reading: Reading, node: ast.AST, function: ast.AST | None) -> bool:
    if _is_fact_call(node) or _is_call(node, "bool", 1):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if function is None:
        return False
    if isinstance(node, ast.Name):
        return _is_reason_name(reading, node.id, function)
    return _same(node, CLOCK) and _clock_is_pinned(reading, function)


def _form_violations(reading: Reading, call: ast.Call) -> list[str]:
    found = []
    receiver = call.func.value  # type: ignore[attr-defined]
    if not (isinstance(receiver, ast.Name) and receiver.id == "logger"):
        found.append(f"form: `{ast.unparse(call.func)}` is not a call on this module's logger")
    if call.func.attr not in ALLOWED_LEVELS:  # type: ignore[attr-defined]
        found.append(f"form: `{ast.unparse(call.func)}` is not an allowed level")
    if call.keywords:
        found.append("form: keyword argument " + ", ".join(k.arg or "**" for k in call.keywords))
    if any(isinstance(argument, ast.Starred) for argument in call.args):
        found.append("form: a starred argument")
    first = call.args[0] if call.args else None
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return [*found, "form: the message is not one string literal"]
    conversions = re.findall(r"%(.?)", first.value)
    if any(conversion not in ("s", "d") for conversion in conversions):
        found.append("form: a conversion other than %s and %d")
    arguments = call.args[1:]
    if len(conversions) != len(arguments):
        return [*found, "form: conversions and arguments do not pair"]
    function = reading.function_of(call)
    for conversion, argument in zip(conversions, arguments, strict=True):
        written = ast.unparse(argument)
        if conversion == "d" and not _is_call(argument, "len", 1):
            found.append(f"form: %d takes len(...) and nothing else, not `{written}`")
        if conversion == "s" and not _is_closed_text(reading, argument, function):
            found.append(f"form: %s does not take `{written}`")
    return found


def _is_untouched_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    bound = _bindings(function, name)
    own = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    return len(bound) == 1 and any(bound[0] is parameter for parameter in own)


def _binding_violations(reading: Reading, chains: dict[str, Chain]) -> list[str]:
    found = []
    for node in ast.walk(reading.tree):
        for name in ("id_fact", "uuid_fact"):
            if not _is_call(node, name, 1):
                continue
            given = node.args[0]  # type: ignore[attr-defined]
            function = reading.function_of(node)
            if function is not None:
                if isinstance(given, ast.Name) and _is_untouched_parameter(function, given.id):
                    continue
                chain = chains.get(reading.where(node))
                if chain is not None and any(_same(given, read) for read in chain.reads):
                    continue
            found.append(
                f"line {node.lineno}: binding: `{name}` is given `{ast.unparse(given)}`, neither "
                "a parameter the function leaves alone nor a pinned row read of this function"
            )
    return found


def _is_producer(statement: ast.AST | None, target: ast.AST, how: str, written: str) -> bool:
    if how == "assign":
        return (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and statement.targets[0] is target
            and _same(statement.value, written)
        )
    return (
        how == "for"
        and isinstance(statement, ast.For)
        and statement.target is target
        and _same(statement.iter, written)
    )


def _chain_violations(reading: Reading, chains: dict[str, Chain]) -> list[str]:
    found = []
    functions = reading.functions()
    for where, chain in chains.items():
        function = functions.get(where)
        if function is None:
            found.append(f"chain: there is no function `{where}`")
            continue
        for name, (how, written) in chain.producers.items():
            bound = _bindings(function, name)
            statement = reading.parent.get(bound[0]) if len(bound) == 1 else None
            if not _is_producer(statement, bound[0] if bound else function, how, written):
                found.append(f"chain: `{name}` in `{where}` is not bound once, as written")
        for node in ast.walk(function):
            if (
                isinstance(node, (ast.Subscript, ast.Attribute))
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and _root(node) in chain.producers
            ):
                found.append(f"line {node.lineno}: chain: `{_root(node)}` is written through")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in NO_METHOD_CALLS & set(chain.producers)
            ):
                found.append(
                    f"line {node.lineno}: chain: a method is called on `{node.func.value.id}`"
                )
    return found


# ── the row producers, as the modules write them ─────────────────────────────

QUEUE_READ = '''await conn.fetch(
            """
            SELECT id, client_id, scheduled_at
            FROM welcome_email_queue
            WHERE scheduled_at <= $1
            ORDER BY scheduled_at ASC
            LIMIT 50
            """,
            now,
        )'''

PARTNER_INSERT = (
    '''f"INSERT INTO partners ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id"'''
)

COMMISSION_INSERT = '''await self.conn.fetchrow(
            """
            INSERT INTO partner_commissions (
                partner_id, entry_type, referral_id, practice_id, related_commission_id,
                base_amount_idr, commission_type_snapshot, commission_value_snapshot,
                rule_source, assigned_to_snapshot,
                gross_amount_idr, withholding_category, withholding_rate,
                withholding_amount_idr, net_amount_idr,
                status, eligible_for_approval_at,
                manual_override_reason, clawback_reason, idempotency_key
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                    COALESCE($17, now()),$18,$19,$20)
            RETURNING id
            """,
            partner_id,
            entry_type,
            referral_id,
            practice_id,
            related_commission_id,
            base_amount_idr,
            commission_type_snapshot,
            commission_value_snapshot,
            rule_source,
            assigned_to_snapshot,
            gross_amount_idr,
            withholding_category,
            withholding_rate,
            withholding_amount_idr,
            net_amount_idr,
            status,
            eligible_for_approval_at,
            manual_override_reason,
            clawback_reason,
            idempotency_key,
        )'''

_COMMISSION_READ = Chain(
    reads=("c.partner_id",),
    producers={
        "repo": ("assign", "PartnersRepository(conn)"),
        "c": ("assign", "await repo.get_commission(commission_id)"),
    },
)

CHAINS: dict[str, dict[str, Chain]] = {
    "welcome_email_service": {
        "process_pending_welcome_emails": Chain(
            reads=("client_id",),
            producers={
                "rows": ("assign", QUEUE_READ),
                "row": ("for", "rows"),
                "client_id": ("assign", 'row["client_id"]'),
            },
        ),
    },
    "partners.emails": {
        "enqueue_commission_earned": _COMMISSION_READ,
        "send_commission_earned": _COMMISSION_READ,
    },
    "partners.repository": {
        "PartnersRepository.insert_partner": Chain(
            reads=('row["id"]',),
            producers={
                "sql": ("assign", PARTNER_INSERT),
                "row": ("assign", "await self.conn.fetchrow(sql, *vals)"),
            },
        ),
        "PartnersRepository.insert_commission": Chain(
            reads=('row["id"]',),
            producers={"row": ("assign", COMMISSION_INSERT)},
        ),
    },
}


# ── the inventory: every log call of the five modules ────────────────────────

TABLE: dict[str, list[tuple[str, str, str, tuple[str, ...]]]] = {
    "welcome_email_service": [
        (
            "schedule_client_welcome_email",
            "logger.debug",
            "WelcomeEmail: inactive, skipping client %s",
            ("id_fact(client_id)",),
        ),
        (
            "schedule_client_welcome_email",
            "logger.info",
            "WelcomeEmail: queued for client %s, will send at %s UTC",
            ("id_fact(client_id)", "send_at.strftime('%H:%M')"),
        ),
        (
            "schedule_client_welcome_email",
            "logger.error",
            "WelcomeEmail: failed to queue for client %s: %s error",
            ("id_fact(client_id)", "error_word(exc)"),
        ),
        (
            "process_pending_welcome_emails",
            "logger.error",
            "WelcomeEmail: send failed for client %s: %s error",
            ("id_fact(client_id)", "error_word(exc)"),
        ),
        (
            "_send_client_welcome_impl",
            "logger.warning",
            "WelcomeEmail: client %s not found",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeEmail: client %s has no email, skipping",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeEmail: already sent for client %s, skipping",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeEmail: brochure attached from URL (%d bytes)",
            ("len(br.content)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeEmail: brochure attached from local file",
            (),
        ),
        (
            "_send_client_welcome_impl",
            "logger.warning",
            "WelcomeEmail: could not attach brochure for client %s: %s error",
            ("id_fact(client_id)", "error_word(exc)"),
        ),
        (
            "_send_client_welcome_impl",
            "logger.warning",
            "WelcomeEmail: brochure unavailable for client %s",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeEmail: sent to client %s (lang=%s)",
            ("id_fact(client_id)", "choice_fact(lang, LANGUAGES)"),
        ),
        (
            "_send_client_welcome_impl",
            "logger.error",
            "WelcomeEmail: send failed for client %s: %s",
            ("id_fact(client_id)", "log_reason"),
        ),
    ],
    "welcome_practice_service": [
        (
            "send_practice_kickoff",
            "logger.error",
            "PracticeKickoff: unhandled error for practice %s (client %s): %s error",
            ("id_fact(practice_id)", "id_fact(client_id)", "error_word(exc)"),
        ),
        (
            "_send_practice_kickoff_impl",
            "logger.warning",
            "PracticeKickoff: client %s not found",
            ("id_fact(client_id)",),
        ),
        (
            "_record_welcome_run",
            "logger.info",
            "PracticeKickoff: crm_welcome_runs UPSERT for client=%s practice=%s success=%s whatsapp=%s email=%s",
            (
                "id_fact(client_id)",
                "id_fact(practice_id)",
                "bool(success)",
                "bool('whatsapp' in channels_sent)",
                "bool('email' in channels_sent)",
            ),
        ),
        (
            "_record_welcome_run",
            "logger.error",
            "PracticeKickoff: crm_welcome_runs UPSERT failed for client=%s practice=%s: %s error",
            ("id_fact(client_id)", "id_fact(practice_id)", "error_word(exc)"),
        ),
        (
            "_send_whatsapp",
            "logger.debug",
            "PracticeKickoff WA: inactive, skipping practice %s",
            ("id_fact(practice_id)",),
        ),
        (
            "_send_whatsapp",
            "logger.warning",
            "PracticeKickoff WA: WHATSAPP_PHONE_NUMBER_ID or API token not set",
            (),
        ),
        (
            "_send_whatsapp",
            "logger.info",
            "PracticeKickoff WA: client %s has no phone, skipping",
            ("id_fact(client_id)",),
        ),
        (
            "_send_whatsapp",
            "logger.info",
            "PracticeKickoff WA: already sent for practice %s, skipping",
            ("id_fact(practice_id)",),
        ),
        (
            "_send_whatsapp",
            "logger.info",
            "PracticeKickoff WA: sent to client %s practice %s (lang=%s)",
            ("id_fact(client_id)", "id_fact(practice_id)", "choice_fact(lang, LANGUAGES)"),
        ),
        (
            "_send_whatsapp",
            "logger.error",
            "PracticeKickoff WA: Meta API error for practice %s: status=%s",
            ("id_fact(practice_id)", "status_fact(resp.status_code)"),
        ),
        (
            "_send_email",
            "logger.debug",
            "PracticeKickoff Email: inactive, skipping practice %s",
            ("id_fact(practice_id)",),
        ),
        (
            "_send_email",
            "logger.info",
            "PracticeKickoff Email: client %s has no email, skipping",
            ("id_fact(client_id)",),
        ),
        (
            "_send_email",
            "logger.info",
            "PracticeKickoff Email: already sent for practice %s, skipping",
            ("id_fact(practice_id)",),
        ),
        (
            "_send_email",
            "logger.warning",
            "PracticeKickoff Email: could not read brochure for practice %s: %s error",
            ("id_fact(practice_id)", "error_word(exc)"),
        ),
        (
            "_send_email",
            "logger.info",
            "PracticeKickoff Email: sent to client %s practice %s (lang=%s)",
            ("id_fact(client_id)", "id_fact(practice_id)", "choice_fact(lang, LANGUAGES)"),
        ),
        (
            "_send_email",
            "logger.error",
            "PracticeKickoff Email: send failed for practice %s: status=%s",
            ("id_fact(practice_id)", "status_fact(resp.status_code)"),
        ),
    ],
    "welcome_whatsapp_service": [
        (
            "send_client_welcome",
            "logger.debug",
            "WelcomeWhatsApp: inactive (template approval pending), skipping client %s",
            ("id_fact(client_id)",),
        ),
        (
            "send_client_welcome",
            "logger.error",
            "WelcomeWhatsApp: unhandled error for client %s: %s error",
            ("id_fact(client_id)", "error_word(exc)"),
        ),
        (
            "_send_client_welcome_impl",
            "logger.warning",
            "WelcomeWhatsApp: WHATSAPP_PHONE_NUMBER_ID or API token not set, skipping",
            (),
        ),
        (
            "_send_client_welcome_impl",
            "logger.warning",
            "WelcomeWhatsApp: client %s not found",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeWhatsApp: client %s has no phone, skipping",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeWhatsApp: already sent for client %s, skipping",
            ("id_fact(client_id)",),
        ),
        (
            "_send_client_welcome_impl",
            "logger.info",
            "WelcomeWhatsApp: sent to client %s (lang=%s)",
            ("id_fact(client_id)", "choice_fact(lang, LANGUAGES)"),
        ),
        (
            "_send_client_welcome_impl",
            "logger.error",
            "WelcomeWhatsApp: Meta API error for client %s: status=%s",
            ("id_fact(client_id)", "status_fact(resp.status_code)"),
        ),
    ],
    "partners.emails": [
        (
            "_build_pricing_services",
            "logger.warning",
            "PricingService not loaded — welcome email services list will be empty",
            (),
        ),
        (
            "enqueue_welcome",
            "logger.warning",
            "enqueue_welcome: partner %s not found — skip",
            ("uuid_fact(partner_id)",),
        ),
        (
            "enqueue_welcome",
            "logger.info",
            "enqueue_welcome: welcome already sent for partner %s — skip (idempotent)",
            ("uuid_fact(partner_id)",),
        ),
        (
            "enqueue_welcome",
            "logger.info",
            "enqueue_welcome: enqueued for partner %s",
            ("uuid_fact(partner_id)",),
        ),
        (
            "enqueue_commission_earned",
            "logger.info",
            "enqueue_commission_earned: commission %s not found or not paid — skip",
            ("uuid_fact(commission_id)",),
        ),
        (
            "enqueue_commission_earned",
            "logger.info",
            "enqueue_commission_earned: already sent for commission %s — skip (idempotent)",
            ("uuid_fact(commission_id)",),
        ),
        (
            "enqueue_commission_earned",
            "logger.warning",
            "enqueue_commission_earned: partner %s not found — skip",
            ("uuid_fact(c.partner_id)",),
        ),
        (
            "enqueue_commission_earned",
            "logger.info",
            "enqueue_commission_earned: enqueued for commission %s",
            ("uuid_fact(commission_id)",),
        ),
        (
            "send_welcome",
            "logger.warning",
            "send_welcome: partner %s not found — skip",
            ("uuid_fact(partner_id)",),
        ),
        (
            "send_welcome",
            "logger.info",
            "send_welcome: already sent for partner %s — skip (idempotent)",
            ("uuid_fact(partner_id)",),
        ),
        (
            "send_welcome",
            "logger.info",
            "send_welcome: sent to partner %s",
            ("uuid_fact(partner_id)",),
        ),
        (
            "send_commission_earned",
            "logger.info",
            "send_commission_earned: commission %s not found or not paid — skip",
            ("uuid_fact(commission_id)",),
        ),
        (
            "send_commission_earned",
            "logger.info",
            "send_commission_earned: already sent for commission %s — skip (idempotent)",
            ("uuid_fact(commission_id)",),
        ),
        (
            "send_commission_earned",
            "logger.warning",
            "send_commission_earned: partner %s not found — skip",
            ("uuid_fact(c.partner_id)",),
        ),
        (
            "send_commission_earned",
            "logger.info",
            "send_commission_earned: sent for commission %s",
            ("uuid_fact(commission_id)",),
        ),
    ],
    "partners.repository": [
        (
            "PartnersRepository.insert_partner",
            "logger.debug",
            "insert_partner id=%s",
            ("uuid_fact(row['id'])",),
        ),
        (
            "PartnersRepository.insert_commission",
            "logger.debug",
            "insert_commission id=%s partner=%s type=%s status=%s",
            (
                "uuid_fact(row['id'])",
                "uuid_fact(partner_id)",
                "choice_fact(entry_type, COMMISSION_ENTRY_TYPES)",
                "choice_fact(status, COMMISSION_STATUSES)",
            ),
        ),
        (
            "PartnersRepository.update_commission_status",
            "logger.debug",
            "update_commission_status id=%s %s->%s",
            (
                "uuid_fact(commission_id)",
                "choice_fact(current.status, COMMISSION_STATUSES)",
                "choice_fact(new_status, COMMISSION_STATUSES)",
            ),
        ),
    ],
}


# ── the five modules ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("module", sorted(MODULES))
def test_the_log_calls_of_a_module_are_the_ones_in_the_table(module: str) -> None:
    assert inventory(source_of(module)) == TABLE[module]


@pytest.mark.parametrize("module", sorted(MODULES))
def test_every_log_call_of_a_module_has_a_closed_form_and_pinned_bindings(module: str) -> None:
    assert violations(source_of(module), CHAINS.get(module)) == []


def test_the_table_reads_the_whole_of_every_module() -> None:
    assert {module: len(rows) for module, rows in TABLE.items()} == {
        "welcome_email_service": 13,
        "welcome_practice_service": 16,
        "welcome_whatsapp_service": 8,
        "partners.emails": 15,
        "partners.repository": 3,
    }


_FRAME = """\
import logging
from datetime import datetime, timedelta, timezone

from backend.services.crm.welcome.log_facts import (
    COMMISSION_ENTRY_TYPES,
    COMMISSION_STATUSES,
    LANGUAGES,
    choice_fact,
    error_word,
    id_fact,
    status_fact,
    uuid_fact,
)

logger = logging.getLogger(__name__)
WELCOME_DELAY_MINUTES = 30


def frame():
    log_reason = "?"
    send_at = datetime.now(timezone.utc) + timedelta(minutes=WELCOME_DELAY_MINUTES)
    {call}
"""


@pytest.mark.parametrize(
    "row", [row for rows in TABLE.values() for row in rows], ids=lambda row: row[2][:60]
)
def test_a_row_of_the_table_is_itself_a_closed_form(row: tuple) -> None:
    """The table cannot hold a bad form: each row, alone in a bare function, is read too."""
    _, call, message, arguments = row
    reading = Reading(_FRAME.format(call=f"{call}({message!r}, {', '.join(arguments)})"))

    (written,) = reading.log_calls()

    assert _form_violations(reading, written) == []


# ── the reading proves itself ────────────────────────────────────────────────

EMAIL, KICKOFF, WHATSAPP = (
    "welcome_email_service",
    "welcome_practice_service",
    "welcome_whatsapp_service",
)
MAILS, ROWS = "partners.emails", "partners.repository"

_NOT_FOUND = 'logger.warning("WelcomeWhatsApp: client %s not found", id_fact(client_id))'
_META_LINE = '"WelcomeWhatsApp: Meta API error for client %s: status=%s",'
_META_STATUS = "            status_fact(resp.status_code),\n"
_UNHANDLED_WORD = "            error_word(exc),\n"
_BEFORE_THE_ANSWER = "    if resp.status_code == 200:\n"
_LOGGER = "logger = logging.getLogger(__name__)\n"
_QUEUE_LOOP = "    for row in rows:\n"
_QUEUE_READ_LINE = '        client_id = row["client_id"]\n'
_COMMISSION_WARNING = (
    "        logger.warning(\n"
    '            "enqueue_commission_earned: partner %s not found — skip", uuid_fact(c.partner_id)\n'
)
_STAND_IN_ROW = '{"id": 1, "client_id": 2025550123}'


def _edited(module: str, *edits: tuple[str, str]) -> str:
    source = source_of(module)
    for old, new in edits:
        assert source.count(old) == 1, f"the sample's anchor is not unique in {module}: {old!r}"
        source = source.replace(old, new)
    return source


GUILTY: list[tuple[str, str, tuple[tuple[str, str], ...], str]] = [
    # what the sample does | module | the edit | the reason it must be refused for
    (
        "outside text through an innocently named local",
        WHATSAPP,
        (
            (_BEFORE_THE_ANSWER, "    note = resp.text[:200]\n" + _BEFORE_THE_ANSWER),
            (_META_STATUS, "            note,\n"),
        ),
        "form: %s does not take `note`",
    ),
    (
        "an f-string message",
        WHATSAPP,
        ((_META_LINE, 'f"WelcomeWhatsApp: Meta API error for client %s: {resp.text}",'),),
        "form: the message is not one string literal",
    ),
    (
        "a message joined at run time",
        WHATSAPP,
        ((_META_LINE, '"WelcomeWhatsApp: Meta API error for client %s: " + resp.reason_phrase,'),),
        "form: the message is not one string literal",
    ),
    (
        "a message built by .format",
        WHATSAPP,
        ((_META_LINE, '"WelcomeWhatsApp: Meta API error for client %s: {}".format(resp.text),'),),
        "form: the message is not one string literal",
    ),
    (
        "a %r conversion",
        WHATSAPP,
        ((_META_LINE, _META_LINE.replace("status=%s", "status=%r")),),
        "form: a conversion other than %s and %d",
    ),
    (
        "extra=",
        WHATSAPP,
        ((_META_STATUS, _META_STATUS + '            extra={"body": resp.text},\n'),),
        "form: keyword argument extra",
    ),
    (
        "exc_info=True",
        WHATSAPP,
        ((_UNHANDLED_WORD, _UNHANDLED_WORD + "            exc_info=True,\n"),),
        "form: keyword argument exc_info",
    ),
    (
        "logger.exception",
        WHATSAPP,
        (
            (
                '        logger.error(\n            "WelcomeWhatsApp: unhandled error',
                '        logger.exception(\n            "WelcomeWhatsApp: unhandled error',
            ),
        ),
        "form: `logger.exception` is not an allowed level",
    ),
    (
        "str(exc)",
        WHATSAPP,
        ((_UNHANDLED_WORD, "            str(exc),\n"),),
        "form: %s does not take `str(exc)`",
    ),
    (
        "a bare int parameter",
        WHATSAPP,
        ((_NOT_FOUND, _NOT_FOUND.replace("id_fact(client_id)", "client_id")),),
        "form: %s does not take `client_id`",
    ),
    (
        "a bare UUID parameter",
        MAILS,
        (
            (
                'logger.info("send_welcome: sent to partner %s", uuid_fact(partner_id))',
                'logger.info("send_welcome: sent to partner %s", partner_id)',
            ),
        ),
        "form: %s does not take `partner_id`",
    ),
    (
        "a bare str parameter",
        KICKOFF,
        (
            (
                '            "PracticeKickoff WA: sent to client %s practice %s (lang=%s)",\n'
                "            id_fact(client_id),\n"
                "            id_fact(practice_id),\n"
                "            choice_fact(lang, LANGUAGES),\n",
                '            "PracticeKickoff WA: sent to client %s practice %s (lang=%s)",\n'
                "            id_fact(client_id),\n"
                "            id_fact(practice_id),\n"
                "            lang,\n",
            ),
        ),
        "form: %s does not take `lang`",
    ),
    (
        "a bare parameter annotated with a closed alias",
        ROWS,
        (
            (
                "            choice_fact(new_status, COMMISSION_STATUSES),\n",
                "            new_status,\n",
            ),
        ),
        "form: %s does not take `new_status`",
    ),
    (
        "a bare list[str] parameter",
        KICKOFF,
        (('            bool("whatsapp" in channels_sent),\n', "            channels_sent,\n"),),
        "form: %s does not take `channels_sent`",
    ),
    (
        "main's own form: an id under %d",
        WHATSAPP,
        ((_NOT_FOUND, 'logger.warning("WelcomeWhatsApp: client %d not found", client_id)'),),
        "form: %d takes len(...) and nothing else, not `client_id`",
    ),
    (
        "a status under %d",
        WHATSAPP,
        (
            (_META_LINE, _META_LINE.replace("status=%s", "status=%d")),
            (_META_STATUS, "            resp.status_code,\n"),
        ),
        "form: %d takes len(...) and nothing else, not `resp.status_code`",
    ),
    (
        "a fact call under %d",
        WHATSAPP,
        ((_NOT_FOUND, _NOT_FOUND.replace("client %s", "client %d")),),
        "form: %d takes len(...) and nothing else, not `id_fact(client_id)`",
    ),
    (
        "a choice out of a set that is not one of the three",
        WHATSAPP,
        (
            (
                "            choice_fact(lang, LANGUAGES),\n",
                "            choice_fact(lang, {lang}),\n",
            ),
        ),
        "form: %s does not take `choice_fact(lang, {lang})`",
    ),
    (
        "the reason name rebound from a response",
        EMAIL,
        (
            (
                '            log_reason = "status=" + status_fact(status_code)\n',
                '            log_reason = "status=" + status_fact(status_code)\n'
                "            log_reason = resp.text[:200]\n",
            ),
        ),
        "form: %s does not take `log_reason`",
    ),
    (
        "the reason name joined with a value no function closed",
        EMAIL,
        (('"status=" + status_fact(status_code)', '"status=" + str(status_code)'),),
        "form: %s does not take `log_reason`",
    ),
    (
        "an id parameter rebound from a response before id_fact",
        WHATSAPP,
        ((_BEFORE_THE_ANSWER, '    client_id = resp.json()["id"]\n' + _BEFORE_THE_ANSWER),),
        "binding: `id_fact` is given `client_id`",
    ),
    (
        "the queue's row read in another function",
        EMAIL,
        (
            (
                'logger.debug("WelcomeEmail: inactive, skipping client %s", id_fact(client_id))',
                'logger.debug("WelcomeEmail: inactive, skipping client %s", id_fact(row["id"]))',
            ),
        ),
        "binding: `id_fact` is given `row['id']`",
    ),
    (
        "the repository's row read in another function",
        ROWS,
        (
            (
                "            uuid_fact(commission_id),\n            choice_fact(current.status,",
                '            uuid_fact(row["id"]),\n            choice_fact(current.status,',
            ),
        ),
        "binding: `uuid_fact` is given `row['id']`",
    ),
    (
        "the commission's partner read in another function",
        MAILS,
        (
            (
                'logger.info("send_welcome: sent to partner %s", uuid_fact(partner_id))',
                'logger.info("send_welcome: sent to partner %s", uuid_fact(c.partner_id))',
            ),
        ),
        "binding: `uuid_fact` is given `c.partner_id`",
    ),
    (
        "a second `rows = ...` before the queue loop",
        EMAIL,
        ((_QUEUE_LOOP, f"    rows = [{_STAND_IN_ROW}]\n" + _QUEUE_LOOP),),
        "chain: `rows` in `process_pending_welcome_emails` is not bound once, as written",
    ),
    (
        "`rows` grown in place",
        EMAIL,
        ((_QUEUE_LOOP, f"    rows += [{_STAND_IN_ROW}]\n" + _QUEUE_LOOP),),
        "chain: `rows` in `process_pending_welcome_emails` is not bound once, as written",
    ),
    (
        "a queue fetch with another query",
        EMAIL,
        (
            (
                "            FROM welcome_email_queue\n",
                "            FROM welcome_email_queue_shadow\n",
            ),
        ),
        "chain: `rows` in `process_pending_welcome_emails` is not bound once, as written",
    ),
    (
        "`row` rebound inside the loop",
        EMAIL,
        ((_QUEUE_READ_LINE, f"        row = {_STAND_IN_ROW}\n" + _QUEUE_READ_LINE),),
        "chain: `row` in `process_pending_welcome_emails` is not bound once, as written",
    ),
    (
        "the queue's client read from another column",
        EMAIL,
        ((_QUEUE_READ_LINE, '        client_id = row["scheduled_at"]\n'),),
        "chain: `client_id` in `process_pending_welcome_emails` is not bound once, as written",
    ),
    (
        "a second `c = ...`",
        MAILS,
        ((_COMMISSION_WARNING, "        c = p\n" + _COMMISSION_WARNING),),
        "chain: `c` in `enqueue_commission_earned` is not bound once, as written",
    ),
    (
        "another repository behind the commission",
        MAILS,
        (
            (
                "    repo = PartnersRepository(conn)\n"
                "    c = await repo.get_commission(commission_id)\n"
                '    if c is None or c.status != "paid":\n'
                "        logger.info(\n"
                '            "enqueue_commission_earned:',
                "    repo = stand_in(conn)\n"
                "    c = await repo.get_commission(commission_id)\n"
                '    if c is None or c.status != "paid":\n'
                "        logger.info(\n"
                '            "enqueue_commission_earned:',
            ),
        ),
        "chain: `repo` in `enqueue_commission_earned` is not bound once, as written",
    ),
    (
        "another producer for the repository's `row`",
        ROWS,
        (("row = await self.conn.fetchrow(sql, *vals)", "row = await other.fetchrow(sql, *vals)"),),
        "chain: `row` in `PartnersRepository.insert_partner` is not bound once, as written",
    ),
    (
        "the partner insert's statement rebuilt before the fetch",
        ROWS,
        (
            (
                "        row = await self.conn.fetchrow(sql, *vals)\n",
                "        sql = sql + tail\n        row = await self.conn.fetchrow(sql, *vals)\n",
            ),
        ),
        "chain: `sql` in `PartnersRepository.insert_partner` is not bound once, as written",
    ),
    (
        "another statement behind the commission insert",
        ROWS,
        (("INSERT INTO partner_commissions (\n", "INSERT INTO partner_commissions_shadow (\n"),),
        "chain: `row` in `PartnersRepository.insert_commission` is not bound once, as written",
    ),
    (
        "`rows[:] = ...` before the queue loop",
        EMAIL,
        ((_QUEUE_LOOP, f"    rows[:] = [{_STAND_IN_ROW}]\n" + _QUEUE_LOOP),),
        "chain: `rows` is written through",
    ),
    (
        "`del rows[0]` before the queue loop",
        EMAIL,
        ((_QUEUE_LOOP, "    del rows[0]\n" + _QUEUE_LOOP),),
        "chain: `rows` is written through",
    ),
    (
        "`rows.insert(...)` before the queue loop",
        EMAIL,
        ((_QUEUE_LOOP, f"    rows.insert(0, {_STAND_IN_ROW})\n" + _QUEUE_LOOP),),
        "chain: a method is called on `rows`",
    ),
    (
        '`row["client_id"] = ...` before the read',
        EMAIL,
        ((_QUEUE_READ_LINE, '        row["client_id"] = 2025550123\n' + _QUEUE_READ_LINE),),
        "chain: `row` is written through",
    ),
    (
        "`row.update(...)` before the read",
        EMAIL,
        ((_QUEUE_READ_LINE, "        row.update(client_id=2025550123)\n" + _QUEUE_READ_LINE),),
        "chain: a method is called on `row`",
    ),
    (
        "`c.partner_id = ...` before the log",
        MAILS,
        ((_COMMISSION_WARNING, "        c.partner_id = p\n" + _COMMISSION_WARNING),),
        "chain: `c` is written through",
    ),
    (
        'strftime("%Z")',
        EMAIL,
        (('send_at.strftime("%H:%M"),', 'send_at.strftime("%Z"),'),),
        "form: %s does not take `send_at.strftime('%Z')`",
    ),
    (
        'strftime("%H:%M") on a clock built with a made-up zone',
        EMAIL,
        (("send_at = datetime.now(timezone.utc) +", "send_at = datetime.now(made_up_zone) +"),),
        "form: %s does not take `send_at.strftime('%H:%M')`",
    ),
    (
        "a second logger",
        WHATSAPP,
        ((_LOGGER, _LOGGER + 'audit = logging.getLogger("zz")\n'),),
        "logger: a second logger is made",
    ),
    (
        "the logger under another name",
        WHATSAPP,
        ((_LOGGER, _LOGGER + "log = logger\n"),),
        "logger: `logger` is renamed or handed on",
    ),
    (
        "a log call on another object",
        WHATSAPP,
        ((_NOT_FOUND, _NOT_FOUND.replace("logger.warning", "audit.warning")),),
        "form: `audit.warning` is not a call on this module's logger",
    ),
    (
        "a log call on the logging module",
        WHATSAPP,
        ((_NOT_FOUND, _NOT_FOUND.replace("logger.warning", "logging.warning")),),
        "logger: `logging` is used to log",
    ),
    (
        "getLogger imported by name",
        WHATSAPP,
        ((_LOGGER, "from logging import getLogger\n" + _LOGGER),),
        "logger: `logging` is not imported once, whole, at module level",
    ),
    (
        "a local function named like one of the five",
        WHATSAPP,
        ((_LOGGER, _LOGGER + "\n\ndef id_fact(value):\n    return value\n"),),
        "names: `id_fact` is not the one imported from log_facts",
    ),
    (
        "bool rebound",
        KICKOFF,
        ((_LOGGER, _LOGGER + "bool = str\n"),),
        "names: `bool` is rebound",
    ),
]


@pytest.mark.parametrize(
    ("module", "edits", "reason"),
    [sample[1:] for sample in GUILTY],
    ids=[sample[0] for sample in GUILTY],
)
def test_a_guilty_edit_of_a_real_module_is_refused_for_its_reason(
    module: str, edits: tuple[tuple[str, str], ...], reason: str
) -> None:
    found = violations(_edited(module, *edits), CHAINS.get(module))

    assert [violation for violation in found if reason in violation], found


CHANGES_OF_INVENTORY: list[tuple[str, str, tuple[str, str]]] = [
    ("a log call added", WHATSAPP, (_NOT_FOUND, _NOT_FOUND + '; logger.info("zz")')),
    ("a log call removed", WHATSAPP, (_NOT_FOUND, "pass")),
    (
        "a message reworded",
        WHATSAPP,
        ('"WelcomeWhatsApp: client %s not found"', '"WelcomeWhatsApp: no client %s"'),
    ),
    (
        "another id of the same closed form",
        KICKOFF,
        (
            'logger.warning("PracticeKickoff: client %s not found", id_fact(client_id))',
            'logger.warning("PracticeKickoff: client %s not found", id_fact(practice_id))',
        ),
    ),
    (
        "the calls of a function now in a function of another name",
        WHATSAPP,
        ("async def send_client_welcome(", "async def send_client_welcome_again("),
    ),
]


@pytest.mark.parametrize(
    ("module", "edit"),
    [sample[1:] for sample in CHANGES_OF_INVENTORY],
    ids=[sample[0] for sample in CHANGES_OF_INVENTORY],
)
def test_a_change_of_the_log_calls_fails_until_the_table_changes(
    module: str, edit: tuple[str, str]
) -> None:
    edited = _edited(module, edit)

    assert inventory(edited) != TABLE[module]
    # None of these edits breaks a form: only the table notices them.
    assert violations(edited, CHAINS.get(module)) == []


INNOCENT = """\
import logging
from datetime import datetime, timedelta, timezone

from backend.services.crm.welcome.log_facts import (
    LANGUAGES,
    choice_fact,
    error_word,
    id_fact,
    status_fact,
    uuid_fact,
)

logger = logging.getLogger(__name__)
WELCOME_DELAY_MINUTES = 30


async def every_allowed_form(client_id, partner_id, lang, resp, done, parts, conn, now):
    send_at = datetime.now(timezone.utc) + timedelta(minutes=WELCOME_DELAY_MINUTES)
    reason = "?"
    try:
        reason = "status=" + status_fact(resp.status_code)
    except Exception as exc:
        reason = error_word(exc) + " error"
    logger.debug("no argument at all")
    logger.info(
        "adjacent literals are one literal: client %s "
        "partner %s lang=%s",
        id_fact(client_id),
        uuid_fact(partner_id),
        choice_fact(lang, LANGUAGES),
    )
    logger.warning("%s %s %s (%d bytes)", bool(done), "a literal", reason, len(parts))
    logger.error("at %s UTC: %s error", send_at.strftime("%H:%M"), error_word(resp))
    rows = await conn.fetch("SELECT client_id FROM queue WHERE due <= $1", now)
    for row in rows:
        queued = row["client_id"]
        logger.info("queued %s", id_fact(queued))
"""
INNOCENT_CHAINS = {
    "every_allowed_form": Chain(
        reads=("queued",),
        producers={
            "rows": (
                "assign",
                'await conn.fetch("SELECT client_id FROM queue WHERE due <= $1", now)',
            ),
            "row": ("for", "rows"),
            "queued": ("assign", 'row["client_id"]'),
        },
    )
}


def test_one_sample_of_every_allowed_form_is_accepted() -> None:
    assert violations(INNOCENT, INNOCENT_CHAINS) == []
    assert [row[1:] for row in inventory(INNOCENT)] == [
        ("logger.debug", "no argument at all", ()),
        (
            "logger.info",
            "adjacent literals are one literal: client %s partner %s lang=%s",
            ("id_fact(client_id)", "uuid_fact(partner_id)", "choice_fact(lang, LANGUAGES)"),
        ),
        (
            "logger.warning",
            "%s %s %s (%d bytes)",
            ("bool(done)", "'a literal'", "reason", "len(parts)"),
        ),
        (
            "logger.error",
            "at %s UTC: %s error",
            ("send_at.strftime('%H:%M')", "error_word(resp)"),
        ),
        ("logger.info", "queued %s", ("id_fact(queued)",)),
    ]


def test_the_innocent_sample_without_its_chain_is_refused() -> None:
    """The row read is accepted because of the declared chain, and for no other reason."""
    found = violations(INNOCENT)

    assert len(found) == 1 and "binding: `id_fact` is given `queued`" in found[0]
