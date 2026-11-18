"""scripts/openapi_consumed.py: the consumed-subset derivation, closure and loud failures."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.openapi_consumed import (
    EXTRA_SCHEMAS,
    ConsumedTypesError,
    derive_consumed,
    prune_openapi,
)

DOC = {
    "openapi": "3.1.0",
    "info": {"title": "full", "version": "9"},
    "paths": {
        "/a": {
            "parameters": [{"$ref": "#/components/parameters/P"}],
            "get": {"operationId": "getA", "responses": {"200": {"$ref": "#/components/responses/R"}}},
            "post": {"operationId": "postA", "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Unrelated"}}}}},
        },
        "/b": {"get": {"operationId": "getB", "responses": {}}},
    },
    "components": {
        "parameters": {"P": {"name": "p", "in": "query", "schema": {"$ref": "#/components/schemas/PType"}}},
        "responses": {"R": {"description": "r", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Top"}}}}},
        "schemas": {
            "Top": {"allOf": [{"$ref": "#/components/schemas/Mid"}], "properties": {"u": {"anyOf": [{"$ref": "#/components/schemas/ViaAny"}]}}},
            "Mid": {"oneOf": [{"$ref": "#/components/schemas/Cat"}, {"$ref": "#/components/schemas/Dog"}],
                    "discriminator": {"propertyName": "k", "mapping": {"cat": "#/components/schemas/Cat", "bird": "Bird"}}},
            "Cat": {}, "Dog": {}, "Bird": {}, "ViaAny": {}, "PType": {}, "Unrelated": {}, "Other": {}, "Used": {},
        },
    },
}


def _mouth(tmp_path: Path, files: dict[str, str]) -> Path:
    src = tmp_path / "src"
    for rel, text in files.items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return src


def test_derives_every_literal_spelling_and_ignores_comments_and_other_modules(tmp_path):
    src = _mouth(tmp_path, {
        "lib/api/schema.d.ts": "export interface components {}\n",
        "a.ts": 'import type { components, operations } from "@/lib/api/schema";\n'
                'type A = components["schemas"]["Used"];\n'
                "type B = components['schemas']['Cat'];\n"
                '// components["schemas"]["InComment"]\n'
                'type O = operations["getA"];\n',
        "lib/api/b.ts": 'import type { components } from "./schema";\ntype C = keyof components["schemas"]["Dog"];\n',
        "other.ts": 'import type { components } from "./generated";\nconst x = components;\n',
    })
    got = derive_consumed(src)
    assert got["components:schemas"] == {"Used", "Cat", "Dog", *EXTRA_SCHEMAS}
    assert got["operations"] == {"getA"}


@pytest.mark.parametrize(
    "body",
    [
        'import type { components as c } from "@/lib/api/schema";\n',
        'import type * as api from "@/lib/api/schema";\n',
        'import type { components } from "@/lib/api/schema";\ntype S = components["schemas"];\n',
        'import type { components } from "@/lib/api/schema";\ntype S = components[K]["X"];\n',
        'import type { webhooks } from "@/lib/api/schema";\n',
        'export type { components } from "@/lib/api/schema";\n',
        'type T = import("@/lib/api/schema").components;\n',
    ],
)
def test_unreadable_use_of_the_schema_module_fails_loudly(tmp_path, body):
    src = _mouth(tmp_path, {"lib/api/schema.d.ts": "", "x.ts": body})
    with pytest.raises(ConsumedTypesError):
        derive_consumed(src)


def test_closure_walks_refs_through_every_container_and_keeps_nothing_else():
    out = prune_openapi(DOC, {"components:schemas": {"Used"}, "operations": {"getA"}})
    assert set(out["components"]["schemas"]) == {"Used", "Top", "Mid", "Cat", "Dog", "Bird", "ViaAny", "PType"}
    assert set(out["components"]["parameters"]) == {"P"}
    assert set(out["components"]["responses"]) == {"R"}
    assert set(out["paths"]) == {"/a"} and set(out["paths"]["/a"]) == {"parameters", "get"}
    assert "Unrelated" not in out["components"]["schemas"] and "Other" not in out["components"]["schemas"]


def test_a_change_outside_the_closure_does_not_move_the_subset_and_one_inside_does():
    from copy import deepcopy

    from scripts.generate_openapi import compute_schema_sha256

    consumed = {"components:schemas": {"Used"}, "operations": {"getA"}}
    base = compute_schema_sha256(prune_openapi(DOC, consumed))
    outside = deepcopy(DOC)
    outside["info"]["version"] = "10"
    outside["components"]["schemas"]["Other"] = {"x": 1}
    outside["paths"]["/b"]["get"]["summary"] = "changed"
    outside["paths"]["/a"]["post"]["summary"] = "changed"
    assert compute_schema_sha256(prune_openapi(outside, consumed)) == base
    inside = deepcopy(DOC)
    inside["components"]["schemas"]["PType"] = {"type": "string"}
    assert compute_schema_sha256(prune_openapi(inside, consumed)) != base


def test_a_name_the_backend_does_not_define_fails_loudly():
    with pytest.raises(ConsumedTypesError):
        prune_openapi(DOC, {"components:schemas": {"Missing"}})
    with pytest.raises(ConsumedTypesError):
        prune_openapi(DOC, {"operations": {"nope"}})
    with pytest.raises(ConsumedTypesError):
        prune_openapi(DOC, {"paths": {"/nope"}})


def test_path_seed_keeps_every_method_of_the_item():
    out = prune_openapi(DOC, {"paths": {"/a"}})
    assert {"get", "post"} <= set(out["paths"]["/a"]) and "Unrelated" in out["components"]["schemas"]
