"""Tests for NotebookLM registry selection in the agentic pipeline."""

from backend.services.oracle.nlm_notebook_registry import resolve_notebook


def test_resolve_notebook_none_for_generic():
    """Generic query with no domain keywords returns None."""
    result = resolve_notebook("hello how are you")
    assert result is None


def test_resolve_notebook_immigration():
    """Immigration-domain query resolves to immigration notebook."""
    result = resolve_notebook("What documents for KITAS renewal?")
    assert result is not None
    assert result["domain"] == "immigration"
