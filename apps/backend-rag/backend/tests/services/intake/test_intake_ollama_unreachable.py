"""An unreachable Ollama is a transient infra failure, never an unreadable page.

The intake worker may point at an Ollama on another owner host (Mini over the
tailnet). A dead endpoint used to be swallowed page by page, which filed good
documents as ``unknown`` for human review and never retried them. These tests
pin the retry contract and the ``num_ctx`` travelling with the request.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from backend.services.intake import classify as cls
from backend.services.intake import inference_runtime, stages
from backend.services.intake.worker import TransientStageError


class _Page:
    def __init__(self, index: int, data: bytes = b"x") -> None:
        self.index = index
        self.png_bytes = data
        self.text = None


def _dead_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def dead_ollama(monkeypatch: pytest.MonkeyPatch) -> str:
    url = _dead_url()
    monkeypatch.setattr(cls, "OLLAMA_URL", url)
    monkeypatch.setattr(cls, "_client", None)
    inference_runtime.clear_ollama_inference_gates()
    return url


async def test_ocr_pages_raises_connect_error_when_endpoint_is_dead(dead_ollama: str) -> None:
    with pytest.raises(httpx.ConnectError, match="unreachable"):
        await cls.ocr_pages([_Page(0)])


async def test_ocr_pages_dead_endpoint_becomes_transient_stage_error(
    dead_ollama: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _classify_via_real_ocr(job: dict[str, Any], stage: str) -> dict[str, Any]:
        return {"ocr": await cls.ocr_pages([_Page(0)])}

    monkeypatch.setattr(cls, "build_stage_handler", lambda pool: _classify_via_real_ocr)
    handler = stages.build_real_stage_handler(object())  # type: ignore[arg-type]

    with pytest.raises(TransientStageError):
        await handler({"id": 1}, "classify")


async def test_ocr_pages_reachable_but_unreadable_page_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _http_500(model: str, b64: str, **kwargs: Any) -> tuple[str, str | None]:
        request = httpx.Request("POST", "http://127.0.0.1:1/api/generate")
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(500))

    monkeypatch.setattr(cls, "_ollama_vision", _http_500)

    out = await cls.ocr_pages([_Page(0)])

    assert out[0]["via"] == "empty"
    assert out[0]["text"] == ""


async def test_ocr_pages_second_attempt_rescues_a_page_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def _flaky(model: str, b64: str, **kwargs: Any) -> tuple[str, str | None]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("flap")
        return "NOMOR POKOK WAJIB PAJAK", ""

    monkeypatch.setattr(cls, "_ollama_vision", _flaky)

    out = await cls.ocr_pages([_Page(0)])

    assert out[0]["via"] == "fallback"
    assert "WAJIB PAJAK" in out[0]["text"]


def test_options_unchanged_when_num_ctx_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTAKE_OLLAMA_NUM_CTX", raising=False)
    assert inference_runtime.ollama_options({"temperature": 0}) == {"temperature": 0}


def test_options_carry_configured_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTAKE_OLLAMA_NUM_CTX", "32768")
    assert inference_runtime.ollama_options({"temperature": 0}) == {
        "temperature": 0,
        "num_ctx": 32768,
    }


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_options_ignore_invalid_num_ctx(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("INTAKE_OLLAMA_NUM_CTX", raw)
    assert inference_runtime.ollama_options({"temperature": 0}) == {"temperature": 0}


class _CapturingClient:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def post(self, url: str, json: dict[str, Any]) -> httpx.Response:
        self.payloads.append(json)
        return httpx.Response(200, json={"response": "OK"}, request=httpx.Request("POST", url))


@pytest.mark.parametrize("configured", [True, False])
async def test_vision_and_text_calls_send_num_ctx_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    client = _CapturingClient()
    monkeypatch.setattr(cls, "_get_client", lambda: client)
    inference_runtime.clear_ollama_inference_gates()
    if configured:
        monkeypatch.setenv("INTAKE_OLLAMA_NUM_CTX", "32768")
    else:
        monkeypatch.delenv("INTAKE_OLLAMA_NUM_CTX", raising=False)

    await cls._ollama_vision("qwen2.5vl:7b", "Zm9v")
    await cls._ollama_text_classify("qwen3.5:9b", "prompt")

    assert len(client.payloads) == 2
    for payload in client.payloads:
        assert ("num_ctx" in payload["options"]) is configured
        if configured:
            assert payload["options"]["num_ctx"] == 32768


@pytest.mark.parametrize("first_connects", [True, False])
async def test_ocr_pages_mixed_connect_and_http_failure_is_still_transient(
    monkeypatch: pytest.MonkeyPatch, first_connects: bool
) -> None:
    """A restarting Ollama refuses one call and answers 500 to the other."""
    calls = {"n": 0}

    async def _mixed(model: str, b64: str, **kwargs: Any) -> tuple[str, str | None]:
        calls["n"] += 1
        if (calls["n"] == 1) == first_connects:
            raise httpx.ConnectError("refused")
        request = httpx.Request("POST", "http://127.0.0.1:1/api/generate")
        raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(500))

    monkeypatch.setattr(cls, "_ollama_vision", _mixed)

    with pytest.raises(httpx.ConnectError):
        await cls.ocr_pages([_Page(0)])
