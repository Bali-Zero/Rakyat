from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

smart_module = importlib.import_module("backend.services.oracle.smart_oracle")


def _patch_oracle_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    pdf_path: Path,
) -> None:
    def download_pdf(_: str) -> str:
        pdf_path.write_bytes(b"%PDF synthetic")
        return str(pdf_path)

    monkeypatch.setattr(smart_module, "download_pdf_from_drive", download_pdf)
    monkeypatch.setattr(smart_module, "get_oracle_client", lambda: None)


@pytest.mark.asyncio
async def test_smart_oracle_removes_downloaded_file_when_ai_retired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "downloaded.pdf"
    _patch_oracle_dependencies(monkeypatch, pdf_path)

    result = await smart_module.smart_oracle("Synthetic question", "synthetic.pdf")

    assert result == "AI service not available. Please check configuration."
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
async def test_a_file_that_cannot_be_removed_does_not_replace_the_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    pdf_path = tmp_path / "downloaded.pdf"
    _patch_oracle_dependencies(monkeypatch, pdf_path)
    monkeypatch.setattr(smart_module.os, "remove", _failing_remove(error))

    result = await smart_module.smart_oracle("Synthetic question", "synthetic.pdf")

    assert result == "AI service not available. Please check configuration."
