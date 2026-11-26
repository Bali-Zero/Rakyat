"""Cross-oracle synthesis must be answered by Claude, not by a concatenation.

The service used to await a method the Zantara client does not have, so every
call fell back to a concatenation of snippets. It now uses the console door
that headless product lanes use. Nothing here reaches a model or the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from backend.llm import claude_oauth_client
from backend.llm.claude_oauth_client import (
    ClaudeOAuthNotAvailable,
    ClaudeOAuthResponse,
)
from backend.services.oracle import cross_oracle_synthesis_service as service_module
from backend.services.oracle.cross_oracle_synthesis_service import (
    CrossOracleSynthesisService,
)

MODEL_TEXT = "SYNTHETIC-MODEL-ANSWER: integrated plan."

ORACLE_RESULTS: dict[str, Any] = {
    "kbli_2025_final": {
        "success": True,
        "results": [{"text": "Synthetic KBLI snippet"}],
    },
}


def _service() -> tuple[CrossOracleSynthesisService, MagicMock]:
    zantara = MagicMock(spec=[])  # any attribute access raises AttributeError
    service = CrossOracleSynthesisService(search_service=MagicMock(), zantara_ai_client=zantara)
    return service, zantara


def _patch_lane(
    monkeypatch: pytest.MonkeyPatch, *, result: Any = None, error: Exception | None = None
) -> AsyncMock:
    mock = create_autospec(claude_oauth_client.complete_async, spec_set=True)
    if error is not None:
        mock.side_effect = error
    else:
        mock.return_value = result
    monkeypatch.setattr(service_module, "complete_async", mock, raising=False)
    return mock


@pytest.mark.asyncio
async def test_synthesis_returns_the_model_text_from_the_claude_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane = _patch_lane(
        monkeypatch,
        result=ClaudeOAuthResponse(
            text=MODEL_TEXT, token_label="synthetic", elapsed_s=0.1, attempts=1
        ),
    )
    service, zantara = _service()

    out = await service.synthesize_with_zantara("open a cafe", "business_setup", ORACLE_RESULTS)

    assert out == MODEL_TEXT
    lane.assert_awaited_once()
    args, kwargs = lane.await_args
    assert "Synthetic KBLI snippet" in args[0]
    assert kwargs == {"endpoint": "cross_oracle_synthesis"}
    assert "model" not in kwargs
    assert zantara.method_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ClaudeOAuthNotAvailable("no seat"), RuntimeError("boom")])
async def test_lane_failure_falls_back_to_the_simple_synthesis(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    lane = _patch_lane(monkeypatch, error=error)
    service, zantara = _service()

    out = await service.synthesize_with_zantara("open a cafe", "business_setup", ORACLE_RESULTS)

    assert out == service._simple_synthesis("open a cafe", ORACLE_RESULTS)
    assert "Synthetic KBLI snippet" in out
    lane.assert_awaited_once()
    assert zantara.method_calls == []
