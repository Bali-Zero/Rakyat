"""Regression guards ensuring zantara_core_v4 never promotes 0%-PMA KBLI 55203 as open to PT PMA."""

import re

from backend.prompts import zantara_core_v4 as v4


def test_rule5_does_not_mark_55203_open_to_pma() -> None:
    text = v4.TOOL_USAGE_POLICY
    assert "55203" in text
    assert not re.search(r"55203[^.\n]*open to PMA", text, re.IGNORECASE)
    assert "0% PMA" in text or "CLOSED TO PT PMA" in text


def test_worked_examples_never_say_55203_open_to_pma() -> None:
    examples = v4.WORKED_EXAMPLES
    assert "55203" in examples
    assert not re.search(r"55203[^.\n]{0,80}open to PMA", examples, re.IGNORECASE)
    assert "CLOSED to PT PMA" in examples or "0% foreign ownership" in examples
