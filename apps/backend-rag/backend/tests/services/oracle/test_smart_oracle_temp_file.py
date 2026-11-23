from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

smart_module = importlib.import_module("backend.services.oracle.smart_oracle")
_SYNTHETIC_KEY = "synthetic-key"  # pragma: allowlist secret


def _patch_oracle_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    pdf_path: Path,
    generate_content: Any,
) -> None:
    class FakeUploadFiles:
        def upload(self, *, file: str) -> SimpleNamespace:
            assert file == str(pdf_path)
            return SimpleNamespace(uri="gs://synthetic-upload", mime_type="application/pdf")

    class FakeGenAI:
        def Client(self, *, api_key: str | None) -> SimpleNamespace:
            assert api_key == _SYNTHETIC_KEY
            return SimpleNamespace(files=FakeUploadFiles())

    class FakeClient:
        is_available = True
        _auth_method = "api_key"

        async def generate_content(self, **kwargs: Any) -> Any:
            return await generate_content(**kwargs)

    def download_pdf(_: str) -> str:
        pdf_path.write_bytes(b"%PDF synthetic")
        return str(pdf_path)

    monkeypatch.setattr(smart_module, "download_pdf_from_drive", download_pdf)
    monkeypatch.setattr(smart_module, "get_oracle_client", lambda: FakeClient())
    monkeypatch.setattr(smart_module, "genai", FakeGenAI())
    monkeypatch.setattr(
        smart_module, "settings", SimpleNamespace(google_api_key=_SYNTHETIC_KEY)
    )  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_smart_oracle_removes_downloaded_file_when_model_call_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "downloaded.pdf"

    async def failing_generate_content(**_: Any) -> None:
        raise RuntimeError("synthetic model failure")

    _patch_oracle_dependencies(monkeypatch, pdf_path, failing_generate_content)

    result = await smart_module.smart_oracle("Synthetic question", "synthetic.pdf")

    assert result == "Error processing the document with AI."
    assert not pdf_path.exists()


@pytest.mark.asyncio
async def test_smart_oracle_keeps_successful_answer_and_removes_downloaded_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "downloaded.pdf"

    async def successful_generate_content(**_: Any) -> dict[str, str]:
        return {"text": "Synthetic answer"}

    _patch_oracle_dependencies(monkeypatch, pdf_path, successful_generate_content)

    result = await smart_module.smart_oracle("Synthetic question", "synthetic.pdf")

    assert result == "Synthetic answer"
    assert not pdf_path.exists()


def _failing_remove(error: OSError) -> Any:
    def remove(path: str) -> None:
        raise error

    return remove


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [PermissionError("synthetic permission"), FileNotFoundError("synthetic missing")],
)
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("answer", "Synthetic answer"), ("model failure", "Error processing the document with AI.")],
)
async def test_a_file_that_cannot_be_removed_does_not_replace_the_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    outcome: str,
    expected: str,
) -> None:
    pdf_path = tmp_path / "downloaded.pdf"

    async def generate_content(**_: Any) -> dict[str, str]:
        if outcome == "model failure":
            raise RuntimeError("synthetic model failure")
        return {"text": "Synthetic answer"}

    _patch_oracle_dependencies(monkeypatch, pdf_path, generate_content)
    monkeypatch.setattr(smart_module.os, "remove", _failing_remove(error))

    result = await smart_module.smart_oracle("Synthetic question", "synthetic.pdf")

    assert result == expected
