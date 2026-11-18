"""DeepSeek stays reachable through the OpenRouter model table of llm.config.

Moved here from tests/unit/llm/providers/test_deepseek.py when the provider
package it sat next to was removed (Imperator lot 12): this assertion reads
llm.config, which stays.
"""

from backend.llm.config import OpenRouterModel


def test_openrouter_has_deepseek_model():
    assert "deepseek" in OpenRouterModel.DEEPSEEK_CHAT.lower()
