"""The public blog widget must not receive the internal KG workflow scaffold."""

from __future__ import annotations

import inspect

import pytest

from backend.services.rag.agentic import orchestrator_core as module


@pytest.mark.parametrize("channel", ["blog"])
def test_blog_channel_keeps_workflow_prose_out_of_answer(channel):
    assert module._workflow_prose_in_answer(channel) is False


@pytest.mark.parametrize("channel", [None, "webapp", "whatsapp", "website", "prime", ""])
def test_other_channels_keep_workflow_prose_in_answer(channel):
    assert module._workflow_prose_in_answer(channel) is True


def test_append_site_is_gated_by_the_helper():
    src = inspect.getsource(module.OrchestratorCore._process_query_core_unfinalized)
    site = src.split("# 12c. KG LangGraph workflow prose", 1)[1]
    site = site.split("# 13. R5 Phase 6", 1)[0]
    assert (
        "if langgraph_workflow and not result.abstain and _workflow_prose_in_answer(channel):"
        in site
    )
    assert 'result.answer = result.answer.rstrip() + "\\n\\n" + workflow_text' in site


def test_kg_fast_path_is_skipped_where_the_prose_is_withheld():
    """The fast-path answer is the workflow prose itself, so the blog must skip it."""
    src = inspect.getsource(module.OrchestratorCore._process_query_core_unfinalized)
    call = src.index("await self._try_kg_fast_path(")
    gate = src.index("if _workflow_prose_in_answer(channel)", call)
    assert gate - call < 400, "the fast-path call must be conditioned on the helper"
    assert src.count("await self._try_kg_fast_path(") == 1
