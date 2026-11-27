"""No decorator argument may read the clock.

Why this guard exists (2026-07-27): `backend/tests/routers/test_intake_review.py`
put `datetime.now(timezone.utc) + timedelta(minutes=5)` directly in a
`@pytest.mark.parametrize` list, as the "live lease" case for the intake claim
guard. A parametrize argument is evaluated ONCE, at collection — and pytest
collects the entire tree before running a single test. The lease was therefore
five minutes old the moment it was born, and stayed pinned to that instant while
the rest of the suite ran.

In CI the suite reached that file inside the window, so it was green. On a loaded
machine the full backend suite needs far longer, the "live" lease is expired by
the time the case runs, the guard correctly answers 409 — and three of the five
params fail asserting 403/400. The test's verdict depended on how long the suite
took to reach it, which is not a property of the code under test. Every local
push from that machine was blocked by a diff that touched none of it.

The cure is to keep absolute time inside the test body and parametrize an OFFSET.
This guard arms that rule for the whole tree. It found exactly one offender —
the one it was written for — and the tree is clean at zero after the fix, so it
starts life green rather than as a backlog.

Scope note: the ban is on decorator arguments only. Reading the clock inside a
test, a fixture, or a helper is normal and must keep passing.

Extended 2026-07-27 to cover the clock's twin: random/UUID values in the same
position. Found while probing pytest-xdist for `backend/tests/` — the same
mechanism (evaluated once at collection) breaks a DIFFERENT thing for a
random source than for a clock. A frozen timestamp ages; a frozen UUID is
merely wrong-but-stable within a single process, so it never bit the
single-process suite. It bites `-n auto`: each xdist worker is a separate
process, so a `uuid.uuid4()` read at collection mints a different value per
worker, giving each worker a different generated test ID for the same
logical test — pytest-xdist refuses the entire run with "Different tests
were collected between gwN and gwM" before executing a single test. See
`_NONDETERMINISTIC_NAMES` below for the guard's own docstring on this half.

What "in a decorator" covers (2026-11-28): the decorator expression itself; a
lambda called on the spot and the default values of any lambda or def, which
run when they are built; the module-level functions it calls by name and the
functions those define and call, at any depth; and the module-level names it
reads, with what their assignment computed at import. What it does NOT see:
a helper imported from another module, an attribute or method call
(`obj.helper()`), `functools.partial`, a helper passed by reference or through
an alias, a bare `@helper` decorator, class attributes, and module-level
constants read in test BODIES (frozen at collection too, but outside a
decorator, so outside this guard).
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

# `apps/backend-rag` — parents: compliance -> tests -> backend -> apps/backend-rag
_BACKEND_RAG_ROOT = Path(__file__).resolve().parents[3]

_TEST_ROOTS = (
    _BACKEND_RAG_ROOT / "backend" / "tests",
    _BACKEND_RAG_ROOT / "tests",
)

# `datetime.now()` / `datetime.utcnow()` / `date.today()` / `time.time()` /
# `time.monotonic()` / `time.perf_counter()`. Matched on the attribute name so a
# renamed import (`import datetime as dt`) is still caught.
_CLOCK_ATTRS = frozenset({"now", "utcnow", "today", "time", "monotonic", "perf_counter"})

# The clock's twin (2026-07-27, found while probing pytest-xdist): a decorator
# argument that mints a RANDOM value is non-deterministic in the same way a
# clock read is, and it breaks something the clock case did not. pytest-xdist
# requires every worker to collect a byte-identical test list; each worker is a
# separate Python process, so a `uuid.uuid4()` in a parametrize list produces a
# different value — and therefore a different generated test ID — per worker.
# xdist then refuses the whole run with "Different tests were collected between
# gw2 and gw3", before executing a single test. Measured live: three sites in
# backend/tests/routers/test_intake_review.py, 22 differing collection lines
# across two plain processes.
#
# Matched on BOTH a bare name (`uuid4()` after `from uuid import uuid4`) and an
# attribute (`uuid.uuid4()`), unlike the clock set above which only matches the
# attribute form — these names are distinctive enough that a bare call is not
# plausibly a local helper, whereas a bare `time()` could be.
_NONDETERMINISTIC_NAMES = frozenset(
    {
        "uuid1",
        "uuid4",
        "random",
        "randint",
        "randrange",
        "choice",
        "shuffle",
        "sample",
        "getrandbits",
        "token_hex",
        "token_bytes",
        "token_urlsafe",
    }
)


def _module_scope(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, ast.AST]]:
    """Module-level functions, and module-level names bound by a plain assignment,
    each by name. A decorator that calls such a function runs its body at
    collection; a decorator that names such a value reads what the module
    computed at import, which is collection too."""
    helpers: dict[str, ast.AST] = {}
    values: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            helpers[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    values[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            values[node.target.id] = node.value
    return helpers, values


def _built_now(function: ast.AST) -> list[ast.AST]:
    """What Python evaluates when a lambda or a def is BUILT: its default values,
    and a def's decorators. Its body runs only when it is called."""
    args = function.args
    built = [*args.defaults, *(default for default in args.kw_defaults if default is not None)]
    return built + list(getattr(function, "decorator_list", []))


def _eager_nodes(roots: list[ast.AST]) -> tuple[list[ast.AST], dict[str, ast.AST]]:
    """The nodes that run when their roots are evaluated, and the functions they
    define, by name. The body of a lambda or a nested def runs later, when a test
    calls it, so it is left out, except for a lambda called on the spot."""
    nodes: list[ast.AST] = []
    defined: dict[str, ast.AST] = {}
    stack = list(roots)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Lambda):
            stack.extend(_built_now(node))
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined[node.name] = node
            stack.extend(_built_now(node))
            continue
        nodes.append(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Lambda):
            stack.append(node.func.body)
        stack.extend(ast.iter_child_nodes(node))
    return nodes, defined


def _local_names(function: ast.AST, nodes: list[ast.AST]) -> frozenset[str]:
    """Names local to a function: its parameters and every name it assigns."""
    args = function.args
    params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    params += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
    stored = {
        node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    return frozenset({param.arg for param in params} | stored)


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _reads_the_clock(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr in _CLOCK_ATTRS


def _reads_a_random_value(call: ast.Call) -> bool:
    return _called_name(call) in _NONDETERMINISTIC_NAMES


def _function_reads(function: ast.AST, module, reads, seen: set) -> bool:
    """Whether calling `function` reads a source `reads` matches, at any depth."""
    if id(function) in seen:
        return False
    seen.add(id(function))
    nodes, defined = _eager_nodes(list(function.body))
    local = _local_names(function, nodes) | defined.keys()
    return any(_node_reads(node, module, reads, seen, defined, local) for node in nodes)


def _node_reads(node: ast.AST, module, reads, seen: set, defined, local) -> bool:
    """Whether evaluating one eager node reads a source `reads` matches: the call
    itself, a function it calls by name (defined in the same function, or at
    module level), or a module-level value it names."""
    helpers, values = module
    if isinstance(node, ast.Call):
        if reads(node):
            return True
        if not isinstance(node.func, ast.Name):
            return False
        name = node.func.id
        target = defined.get(name) or (None if name in local else helpers.get(name))
        return target is not None and _function_reads(target, module, reads, seen)
    if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
        return False
    if node.id in local or node.id not in values or ("value", node.id) in seen:
        return False
    seen.add(("value", node.id))
    nodes, defined_here = _eager_nodes([values[node.id]])
    return any(_node_reads(sub, module, reads, seen, defined_here, frozenset()) for sub in nodes)


def _decorator_reads(source: str, reads) -> list[int]:
    """Line numbers of the calls and names in any decorator expression that read
    a source `reads` matches when the decorator is evaluated, i.e. at collection."""
    tree = ast.parse(source)
    module = _module_scope(tree)
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            nodes, defined = _eager_nodes([decorator])
            for sub in nodes:
                if _node_reads(sub, module, reads, set(), defined, frozenset()):
                    found.append(sub.lineno)
    return sorted(set(found))


def _nondeterministic_calls_in_decorators(source: str) -> list[int]:
    """Line numbers of random-value reads inside any decorator expression."""
    return _decorator_reads(source, _reads_a_random_value)


def _clock_calls_in_decorators(source: str) -> list[int]:
    """Line numbers of clock reads inside any decorator expression."""
    return _decorator_reads(source, _reads_the_clock)


def _iter_test_sources() -> list[Path]:
    files: list[Path] = []
    for root in _TEST_ROOTS:
        if not root.is_dir():
            continue
        files.extend(
            p
            for p in root.rglob("*.py")
            if "__pycache__" not in p.parts and "_obsolete" not in p.parts
        )
    return files


def test_guard_flags_a_frozen_timestamp_in_parametrize() -> None:
    """GUILT: the exact shape that blocked every push must be detected."""
    guilty = textwrap.dedent(
        """
        from datetime import datetime, timedelta, timezone

        import pytest

        @pytest.mark.parametrize(
            "lease_expires_at",
            [datetime.now(timezone.utc) + timedelta(minutes=5)],
        )
        def test_live_lease(lease_expires_at):
            assert lease_expires_at
        """
    )
    assert _clock_calls_in_decorators(guilty) == [8]


def test_guard_flags_a_parametrize_helper_that_reads_the_clock() -> None:
    """GUILT: a module helper does not make collection-time clock reads safe."""
    guilty = textwrap.dedent(
        """
        from datetime import datetime, timezone

        import pytest

        def live_deadline():
            return datetime.now(timezone.utc)

        @pytest.mark.parametrize("deadline", live_deadline())
        def test_deadline(deadline):
            assert deadline
        """
    )
    assert _clock_calls_in_decorators(guilty) == [9]


def test_guard_flags_a_parametrize_helper_that_reads_a_random_value() -> None:
    guilty = textwrap.dedent(
        """
        import uuid

        import pytest

        def tokens():
            return [uuid.uuid4()]

        @pytest.mark.parametrize("token", tokens())
        def test_token(token):
            assert token
        """
    )
    assert _nondeterministic_calls_in_decorators(guilty) == [9]


def test_guard_follows_a_helper_that_reads_the_clock_through_another_helper() -> None:
    """GUILT: the clock read two helpers down still happens at collection."""
    guilty = textwrap.dedent(
        """
        from datetime import datetime, timezone

        import pytest

        def soon():
            return datetime.now(timezone.utc)

        def deadlines():
            return [soon().date().isoformat()]

        @pytest.mark.parametrize("deadline", deadlines())
        def test_deadline(deadline):
            assert deadline
        """
    )
    assert _clock_calls_in_decorators(guilty) == [12]


def test_guard_ends_on_helpers_that_call_each_other() -> None:
    """INNOCENCE: mutually recursive helpers that read no clock are not flagged."""
    innocent = textwrap.dedent(
        """
        import pytest

        def even(n):
            return n == 0 or odd(n - 1)

        def odd(n):
            return n != 0 and even(n - 1)

        @pytest.mark.parametrize("flag", [even(4)])
        def test_flag(flag):
            assert flag
        """
    )
    assert _clock_calls_in_decorators(innocent) == []
    assert _nondeterministic_calls_in_decorators(innocent) == []


def test_guard_allows_a_static_parametrize_helper_and_a_body_clock_helper() -> None:
    innocent = textwrap.dedent(
        """
        from datetime import datetime, timezone

        import pytest

        def deadline_shapes():
            return ["naive", "aware"]

        def live_deadline():
            return datetime.now(timezone.utc)

        @pytest.mark.parametrize("shape", deadline_shapes())
        def test_deadline(shape):
            deadline = live_deadline()
            assert shape and deadline
        """
    )
    assert _clock_calls_in_decorators(innocent) == []
    assert _nondeterministic_calls_in_decorators(innocent) == []


def test_guard_allows_a_helper_whose_clock_read_is_deferred_to_the_test() -> None:
    """INNOCENCE: a helper that returns factories reads no clock at collection."""
    innocent = textwrap.dedent(
        """
        import uuid
        from datetime import datetime, timezone

        import pytest

        def factories():
            def later():
                return uuid.uuid4()
            return [lambda: datetime.now(timezone.utc), later]

        @pytest.mark.parametrize("make", factories())
        def test_deadline(make):
            assert make()
        """
    )
    assert _clock_calls_in_decorators(innocent) == []
    assert _nondeterministic_calls_in_decorators(innocent) == []


def test_guard_allows_a_lambda_in_the_decorator_that_reads_the_clock_later() -> None:
    """INNOCENCE: a lambda in the parametrize list runs inside the test body."""
    innocent = textwrap.dedent(
        """
        import uuid
        from datetime import datetime, timezone

        import pytest

        def live():
            return datetime.now(timezone.utc)

        @pytest.mark.parametrize(
            "make", [lambda: datetime.now(timezone.utc), lambda: live(), lambda: uuid.uuid4()]
        )
        def test_deadline(make):
            assert make()
        """
    )
    assert _clock_calls_in_decorators(innocent) == []
    assert _nondeterministic_calls_in_decorators(innocent) == []


def test_guard_flags_a_lambda_called_on_the_spot_or_a_lambda_default() -> None:
    """GUILT (F1): a lambda called in the decorator, or in a helper the decorator
    calls, runs at collection; so does the default value of a lambda."""
    guilty = textwrap.dedent(
        """
        import uuid
        from datetime import datetime, timezone

        import pytest

        def tokens():
            return [(lambda: uuid.uuid4())()]

        @pytest.mark.parametrize("now", [(lambda: datetime.now(timezone.utc))()])
        @pytest.mark.parametrize("make", [lambda at=datetime.now(timezone.utc): at])
        @pytest.mark.parametrize("token", tokens())
        def test_deadline(now, make, token):
            assert now and make() and token
        """
    )
    assert _clock_calls_in_decorators(guilty) == [10, 11]
    assert _nondeterministic_calls_in_decorators(guilty) == [12]


def test_guard_flags_a_nested_def_a_helper_calls_by_name() -> None:
    """GUILT (F2): a def nested in a helper and called there runs at collection."""
    guilty = textwrap.dedent(
        """
        from datetime import datetime, timezone

        import pytest

        def cases():
            def one():
                return datetime.now(timezone.utc)
            return [one()]

        @pytest.mark.parametrize("deadline", cases())
        def test_deadline(deadline):
            assert deadline
        """
    )
    assert _clock_calls_in_decorators(guilty) == [11]


def test_guard_allows_a_nested_def_a_helper_only_returns() -> None:
    """INNOCENCE (F2): a nested def handed to the test runs in the test body."""
    innocent = textwrap.dedent(
        """
        from datetime import datetime, timezone

        import pytest

        def cases():
            def one():
                return datetime.now(timezone.utc)
            return [one]

        @pytest.mark.parametrize("make", cases())
        def test_deadline(make):
            assert make()
        """
    )
    assert _clock_calls_in_decorators(innocent) == []


def test_guard_flags_a_module_constant_read_by_a_decorator() -> None:
    """GUILT (F3): a module constant is computed at import, i.e. at collection,
    whether the decorator names it, a helper returns it, or another constant does."""
    guilty = textwrap.dedent(
        """
        import uuid
        from datetime import datetime, timezone

        import pytest

        NOW = datetime.now(timezone.utc)
        CASES = [NOW]
        TOKENS: list = [uuid.uuid4()]

        def cases():
            return CASES

        @pytest.mark.parametrize("deadline", CASES)
        @pytest.mark.parametrize("again", cases())
        @pytest.mark.parametrize("token", TOKENS)
        def test_deadline(deadline, again, token):
            assert deadline and again and token
        """
    )
    assert _clock_calls_in_decorators(guilty) == [14, 15]
    assert _nondeterministic_calls_in_decorators(guilty) == [16]


def test_guard_allows_a_static_constant_a_local_shadow_and_a_constant_used_in_a_body() -> None:
    """INNOCENCE (F3): a constant that reads nothing; a helper whose local name
    shadows a clock constant; a clock constant read only in a test body."""
    innocent = textwrap.dedent(
        """
        from datetime import datetime, timezone

        import pytest

        SHAPES = ["naive", "aware"]
        CASES = [datetime.now(timezone.utc)]

        def cases():
            CASES = ["fixed"]
            return CASES

        @pytest.mark.parametrize("shape", SHAPES)
        @pytest.mark.parametrize("case", cases())
        def test_deadline(shape, case):
            assert shape and case and CASES
        """
    )
    assert _clock_calls_in_decorators(innocent) == []


def test_guard_does_not_flag_the_clock_inside_a_test_or_fixture() -> None:
    """INNOCENCE: reading the clock at CALL time is the correct pattern.

    This is the post-fix shape of the very test that motivated the guard —
    parametrize carries an offset, the absolute instant is built in the body —
    plus a fixture that stamps a time. Neither may be flagged.
    """
    innocent = textwrap.dedent(
        """
        from datetime import datetime, timedelta, timezone

        import pytest

        @pytest.fixture
        def started_at():
            return datetime.now(timezone.utc)

        @pytest.mark.parametrize("lease_offset", [timedelta(minutes=5), None])
        def test_live_lease(lease_offset):
            expires = (
                None if lease_offset is None
                else datetime.now(timezone.utc) + lease_offset
            )
            assert expires is None or expires > datetime.now(timezone.utc)
        """
    )
    assert _clock_calls_in_decorators(innocent) == []


def test_no_test_decorator_reads_the_clock() -> None:
    """The real sweep over both collected test trees."""
    sources = _iter_test_sources()
    # A guard that walks zero files is blind, not green.
    assert len(sources) > 100, (
        f"expected to scan the whole backend test tree, found {len(sources)} "
        f"files under {[str(r) for r in _TEST_ROOTS]} — the roots are wrong"
    )

    offenders: list[str] = []
    for path in sources:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        try:
            lines = _clock_calls_in_decorators(source)
        except SyntaxError:  # pragma: no cover - not this guard's job to police
            continue
        for line in lines:
            offenders.append(f"{path.relative_to(_BACKEND_RAG_ROOT)}:{line}")

    assert not offenders, (
        "a decorator argument is evaluated once at COLLECTION, so a timestamp "
        "frozen there ages while the rest of the suite runs and the case starts "
        "failing for how long the suite took (see this file's docstring). "
        "Parametrize an offset and build the absolute time inside the test:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_flags_a_random_uuid_in_parametrize() -> None:
    """GUILT: the exact shape that broke pytest-xdist collection.

    Live case, `backend/tests/routers/test_intake_review.py`: a bare
    `f"...{uuid.uuid4()}"` inside a parametrize list. Minted once per Python
    process at collection time — a different value (and therefore a
    different generated test ID) in every xdist worker.
    """
    guilty = textwrap.dedent(
        """
        import uuid

        import pytest

        @pytest.mark.parametrize(
            "release_url",
            [f"/api/intake/review/42/release?claim_token={uuid.uuid4()}"],
        )
        def test_release(release_url):
            assert release_url
        """
    )
    assert _nondeterministic_calls_in_decorators(guilty) == [8]


def test_guard_flags_a_bare_import_of_the_random_call() -> None:
    """GUILT (bare-name form): `from uuid import uuid4` then a naked call."""
    guilty = textwrap.dedent(
        """
        from uuid import uuid4

        import pytest

        @pytest.mark.parametrize("token", [str(uuid4())])
        def test_token(token):
            assert token
        """
    )
    assert _nondeterministic_calls_in_decorators(guilty) == [6]


def test_guard_does_not_flag_random_values_inside_a_test_or_fixture() -> None:
    """INNOCENCE: minting a fresh UUID per test run is the correct pattern.

    This is the post-fix shape of the live offender — parametrize carries a
    FIXED constant (still a syntactically valid, deliberately-wrong token,
    so semantics are preserved), the fresh one is built inside the test body
    where it is re-evaluated for every worker/run and never poisons
    collection.
    """
    innocent = textwrap.dedent(
        """
        import uuid

        import pytest

        @pytest.fixture
        def claim_token():
            return uuid.uuid4()

        @pytest.mark.parametrize(
            "release_url",
            ["/api/intake/review/42/release?claim_token=00000000-0000-0000-0000-000000000000"],
        )
        def test_release(release_url, claim_token):
            assert release_url
            assert claim_token
        """
    )
    assert _nondeterministic_calls_in_decorators(innocent) == []


def test_no_test_decorator_reads_a_random_value() -> None:
    """The real sweep over both collected test trees, random-value twin."""
    sources = _iter_test_sources()
    assert len(sources) > 100, (
        f"expected to scan the whole backend test tree, found {len(sources)} "
        f"files under {[str(r) for r in _TEST_ROOTS]} — the roots are wrong"
    )

    offenders: list[str] = []
    for path in sources:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
            continue
        try:
            lines = _nondeterministic_calls_in_decorators(source)
        except SyntaxError:  # pragma: no cover - not this guard's job to police
            continue
        for line in lines:
            offenders.append(f"{path.relative_to(_BACKEND_RAG_ROOT)}:{line}")

    assert not offenders, (
        "a decorator argument is evaluated once at COLLECTION — a random/UUID "
        "value minted there is a different value (and therefore a different "
        "generated test ID) in every process, which is exactly what makes "
        "pytest-xdist refuse the run with 'Different tests were collected "
        "between gwN and gwM'. Use a fixed constant in parametrize and mint "
        "the real random value inside the test body:\n  " + "\n  ".join(offenders)
    )
