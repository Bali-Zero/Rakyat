"""Structured items in an error detail reach the client as JSON, not as Python text."""

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.app.setup.exception_handlers import register_exception_handlers, sanitize_detail

AMBIGUOUS_DETAIL = {
    "code": "invoice_company_ambiguous",
    "message": "Several companies match",
    "candidates": [{"id": 1, "company_name": "A"}, {"id": 2, "company_name": "B"}],
}


def _strict_json_roundtrip(value):
    """Serialize as JSONResponse does, so a value it would refuse fails here."""
    return json.loads(JSONResponse(content=value).body)


def test_dict_inside_a_list_stays_a_dict():
    detail = {"code": "x", "candidates": [{"id": 1, "company_name": "A"}]}
    assert sanitize_detail(detail) == detail


def test_http_409_sends_candidates_as_objects():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/ambiguous")
    async def ambiguous():
        raise HTTPException(status_code=409, detail=AMBIGUOUS_DETAIL)

    response = TestClient(app).get("/ambiguous")

    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["candidates"][0]["company_name"] == "A"
    assert body == AMBIGUOUS_DETAIL


def test_nested_dicts_and_lists_keep_their_shape():
    detail = {"outer": {"rows": [[1, {"name": "A"}], ("x", None)]}}
    assert sanitize_detail(detail) == {"outer": {"rows": [[1, {"name": "A"}], ["x", None]]}}


def test_only_the_unserializable_leaf_becomes_text():
    marker = object()
    result = sanitize_detail({"items": [{"id": 1, "obj": marker}]})
    assert result == {"items": [{"id": 1, "obj": str(marker)}]}


def test_non_finite_numbers_do_not_break_the_response():
    result = sanitize_detail({"items": [{"a": float("nan"), "b": float("inf")}]})
    assert _strict_json_roundtrip(result) == {"items": [{"a": "nan", "b": "inf"}]}


def test_lone_surrogate_does_not_break_the_response():
    result = sanitize_detail({"items": [{"name": "A\ud800"}]})
    assert _strict_json_roundtrip(result) == {"items": [{"name": "A\\ud800"}]}


def test_key_json_cannot_carry_becomes_text():
    result = sanitize_detail({"items": [{(1, 2): "A", 3: "B"}]})
    assert _strict_json_roundtrip(result) == {"items": [{"(1, 2)": "A", "3": "B"}]}


def test_cycle_ends_in_a_marker():
    row: dict = {"id": 1}
    row["self"] = row
    result = sanitize_detail({"items": [row]})
    assert _strict_json_roundtrip(result) == {"items": [{"id": 1, "self": "[truncated]"}]}


def test_very_deep_value_ends_in_a_marker():
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    result = sanitize_detail({"items": deep})
    assert "[truncated]" in json.dumps(result)
    _strict_json_roundtrip(result)


def test_list_as_the_whole_detail_stays_text():
    assert sanitize_detail([{"id": 1}]) == "[{'id': 1}]"
