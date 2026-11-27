"""Tool-call text parsing never writes argument values to the logs.

The ReAct loops read a tool call out of the model's text (``ACTION: tool(args)``, a
``{"tool_name": ...}`` object) and out of native function-call parts. Those arguments are
model-controlled and can carry client data, so the parsers log the tool name and the
parameter NAMES, never the values. ``parse_tool_calls_from_response(..., silent=True)`` is
the probe form (what the loops WOULD parse, with no log record at all) and must agree with
the default form on every shape.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from backend.services.rag.agentic._reasoning_loop_helpers import parse_tool_calls_from_response
from backend.services.rag.agentic.tool_executor import (
    parse_native_function_call,
    parse_tool_call_regex,
)

SENTINEL = "SYNTHETIC_CLIENT_SENTINEL"

SHAPES = {
    "action_text": f'ACTION: team_my_clients(client_name="{SENTINEL}")',
    "json_object": f'{{"tool_name": "team_my_clients", "tool_input": {{"client_name": "{SENTINEL}"}}}}',
    "json_unicode_escape": (
        f'{{"tool_\\u006eame": "team_my_clients", "tool_input": {{"client_name": "{SENTINEL}"}}}}'
    ),
    "nested_fences": (
        f'```json\n```json\n{{"tool_name": "team_my_clients", '
        f'"tool_input": {{"client_name": "{SENTINEL}"}}}}\n```\n```'
    ),
    "malformed_json": f'{{"tool_name": "team_my_clients", "tool_input": {{"client_name": "{SENTINEL}"',
}


def _text_only(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, candidates=[], function_calls=None)


def test_regex_parser_returns_the_values_but_logs_only_names(caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        call = parse_tool_call_regex(SHAPES["action_text"])

    assert call is not None
    assert call.arguments == {"client_name": SENTINEL}
    assert SENTINEL not in caplog.text
    assert "team_my_clients" in caplog.text
    assert "client_name" in caplog.text  # the parameter NAME is kept


def test_native_parser_returns_the_values_but_logs_only_names(caplog) -> None:
    part = SimpleNamespace(
        function_call=SimpleNamespace(name="team_my_clients", args={"client_name": SENTINEL})
    )

    with caplog.at_level(logging.DEBUG):
        call = parse_native_function_call(part)

    assert call is not None
    assert call.arguments == {"client_name": SENTINEL}
    assert SENTINEL not in caplog.text
    assert "team_my_clients" in caplog.text


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_default_form_never_logs_a_value(shape: str, caplog) -> None:
    text = SHAPES[shape]

    with caplog.at_level(logging.DEBUG):
        parse_tool_calls_from_response(_text_only(text), text)

    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_silent_probe_writes_no_log_record_at_all(shape: str, caplog) -> None:
    text = SHAPES[shape]

    with caplog.at_level(logging.DEBUG):
        parse_tool_calls_from_response(_text_only(text), text, silent=True)

    assert caplog.records == []


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_silent_probe_agrees_with_the_default_form(shape: str) -> None:
    text = SHAPES[shape]

    loud_calls, loud_mode = parse_tool_calls_from_response(_text_only(text), text)
    quiet_calls, quiet_mode = parse_tool_calls_from_response(_text_only(text), text, silent=True)

    assert quiet_mode == loud_mode
    assert [(c.tool_name, c.arguments) for c in quiet_calls] == [
        (c.tool_name, c.arguments) for c in loud_calls
    ]


def test_the_probe_still_reads_each_text_mode() -> None:
    action = parse_tool_calls_from_response(
        _text_only(SHAPES["action_text"]), SHAPES["action_text"], silent=True
    )
    degraded = parse_tool_calls_from_response(
        _text_only(SHAPES["nested_fences"]), SHAPES["nested_fences"], silent=True
    )
    nothing = parse_tool_calls_from_response(
        _text_only(SHAPES["malformed_json"]), SHAPES["malformed_json"], silent=True
    )

    assert (action[1], [c.tool_name for c in action[0]]) == ("regex", ["team_my_clients"])
    assert (degraded[1], [c.tool_name for c in degraded[0]]) == (
        "regex_degraded",
        ["team_my_clients"],
    )
    assert nothing == ([], "none")
