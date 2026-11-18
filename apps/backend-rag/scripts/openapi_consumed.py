"""The part of the backend OpenAPI document that `apps/mouth` actually consumes.

`apps/mouth/src/lib/api/schema.d.ts` is rendered from, and pinned to, only this
subset: the schemas / operations / paths mouth names, plus their full `$ref`
closure. A backend change outside the closure leaves the rendered file and the
pin byte-identical; a change inside it still moves both.

The consumed set is DERIVED from mouth's sources, never hand-listed. Every file
under `apps/mouth/src` that imports the schema module is parsed strictly: it may
import only `components` / `operations` / `paths`, and every use of those names
must be a literal index (`components["schemas"]["X"]`, `operations["id"]`,
`paths["/p"]`). Anything else (alias, spread, computed key, namespace import,
re-export) raises `ConsumedTypesError`, and so does a name the backend document
does not define: a new use of the contract goes RED here, never to a silent
`any`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SCHEMA_MODULE = ("lib", "api", "schema")
_SRC_SUFFIXES = {".ts", ".tsx", ".mts", ".cts"}
_SKIP_DIRS = {"node_modules", ".next"}
_ROOT_NAMES = ("components", "operations", "paths")

# Names read as TEXT from schema.d.ts by a mouth test, not through a type
# import (`extractPublicLeadSourceBlock` in `src/app/sitemap.test.ts`).
EXTRA_SCHEMAS = ("PublicLeadSource",)

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
_STR = r"""(?:"([^"\\]*)"|'([^'\\]*)')"""


class ConsumedTypesError(RuntimeError):
    """mouth uses the generated contract in a way this derivation cannot read."""


def _strip_comments(src: str) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            out.append(src[i : j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _is_schema_module(spec: str, importer: Path, src_root: Path) -> bool:
    if spec.startswith("@/"):
        target = src_root / spec[2:]
    elif spec.startswith("."):
        target = importer.parent / spec
    else:
        return False
    parts = target.resolve().parts
    stem = re.sub(r"(\.d)?(\.(ts|js))?$", "", parts[-1]) if parts else ""
    return (*parts[:-1], stem)[-3:] == SCHEMA_MODULE


def _scan_file(text: str, importer: Path, src_root: Path, found: dict[str, set[str]]):
    text = _strip_comments(text)
    specs = re.finditer(
        rf"(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+){_STR}", text
    )
    local: set[str] = set()
    covered: list[tuple[int, int]] = []
    for m in specs:
        spec = m.group(1) or m.group(2)
        if not _is_schema_module(spec, importer, src_root):
            continue
        start = max(text.rfind("import", 0, m.start()), text.rfind("export", 0, m.start()))
        stmt = text[start : m.end()]
        clause = re.match(r"import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*[\"']", stmt)
        if not clause:
            raise ConsumedTypesError(
                f"{importer}: unsupported import form of the schema module: {stmt!r}"
            )
        for item in clause.group(1).split(","):
            name = item.strip().removeprefix("type ").strip()
            if not name:
                continue
            if name not in _ROOT_NAMES:
                raise ConsumedTypesError(
                    f"{importer}: imports {name!r} from the schema module; only "
                    f"{_ROOT_NAMES} are readable"
                )
            local.add(name)
        covered.append((start, m.end()))
    if not local:
        return
    body = text
    for a, b in reversed(covered):
        body = body[:a] + " " * (b - a) + body[b:]
    literal = {
        "components": re.compile(rf"\bcomponents\s*\[\s*{_STR}\s*\]\s*\[\s*{_STR}\s*\]"),
        "operations": re.compile(rf"\boperations\s*\[\s*{_STR}\s*\]"),
        "paths": re.compile(rf"\bpaths\s*\[\s*{_STR}\s*\]"),
    }
    for name in sorted(local):
        total = len(re.findall(rf"(?<![\w.$/@-]){name}\b(?![/-]|\s*:)", body))
        hits = list(literal[name].finditer(body))
        if total != len(hits):
            raise ConsumedTypesError(
                f"{importer}: {total - len(hits)} use(s) of {name!r} are not a "
                "literal index; write components[\"schemas\"][\"X\"] / "
                'operations["id"] / paths["/p"]'
            )
        for m in hits:
            g = [x for x in m.groups() if x is not None]
            if name == "components":
                found.setdefault(f"components:{g[0]}", set()).add(g[1])
            else:
                found.setdefault(name, set()).add(g[0])


def derive_consumed(mouth_src: Path) -> dict[str, set[str]]:
    """Map `components:<kind>` / `operations` / `paths` -> consumed names."""
    found: dict[str, set[str]] = {}
    src_root = mouth_src.resolve()
    for f in sorted(src_root.rglob("*")):
        if f.suffix not in _SRC_SUFFIXES or _SKIP_DIRS & set(f.parts):
            continue
        if f.name.endswith(".d.ts") and not f.name.endswith(".test.d.ts"):
            continue
        _scan_file(f.read_text(encoding="utf-8"), f, src_root, found)
    found.setdefault("components:schemas", set()).update(EXTRA_SCHEMAS)
    return found


def _refs(node: Any):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        mapping = node.get("mapping")
        if isinstance(mapping, dict) and "propertyName" in node:
            for v in mapping.values():
                if isinstance(v, str):
                    yield v if v.startswith("#/") else f"#/components/schemas/{v}"
        for v in node.values():
            yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def prune_openapi(schema: dict[str, Any], consumed: dict[str, set[str]]) -> dict[str, Any]:
    """Keep the consumed operations / paths / components and their `$ref` closure."""
    paths = schema.get("paths", {})
    comps = schema.get("components", {})
    keep_ops: dict[str, set[str]] = {}  # path -> methods kept
    for p in consumed.get("paths", ()):
        if p not in paths:
            raise ConsumedTypesError(f"mouth uses paths[{p!r}], absent from the OpenAPI document")
        keep_ops[p] = {m for m in paths[p] if m in _HTTP_METHODS}
    wanted_ops = set(consumed.get("operations", ()))
    for p, item in paths.items():
        for m, op in item.items():
            if m in _HTTP_METHODS and op.get("operationId") in wanted_ops:
                keep_ops.setdefault(p, set()).add(m)
                wanted_ops.discard(op["operationId"])
    if wanted_ops:
        raise ConsumedTypesError(f"mouth uses operations {sorted(wanted_ops)}, absent from the OpenAPI document")

    out_paths = {
        p: {k: v for k, v in paths[p].items() if k not in _HTTP_METHODS or k in ms}
        for p, ms in keep_ops.items()
    }
    seeds = [out_paths]
    todo: list[tuple[str, str]] = []
    for key, names in consumed.items():
        if key.startswith("components:"):
            todo += [(key.split(":", 1)[1], n) for n in sorted(names)]
    kept: dict[str, dict[str, Any]] = {}

    def visit(kind: str, name: str) -> None:
        if name in kept.get(kind, {}):
            return
        node = comps.get(kind, {}).get(name)
        if node is None:
            raise ConsumedTypesError(f"components/{kind}/{name} is used but absent from the OpenAPI document")
        kept.setdefault(kind, {})[name] = node
        for ref in _refs(node):
            follow(ref)

    def follow(ref: str) -> None:
        m = re.fullmatch(r"#/components/([^/]+)/(.+)", ref)
        if not m:
            raise ConsumedTypesError(f"$ref {ref!r} points outside #/components; the subset cannot carry it")
        visit(m.group(1), m.group(2).replace("~1", "/").replace("~0", "~"))

    for ref in _refs(seeds):
        follow(ref)
    for kind, name in todo:
        visit(kind, name)
    return {
        "openapi": schema["openapi"],
        "info": {"title": "consumed OpenAPI subset", "version": "0"},
        "paths": out_paths,
        "components": {k: dict(sorted(v.items())) for k, v in sorted(kept.items())},
    }
