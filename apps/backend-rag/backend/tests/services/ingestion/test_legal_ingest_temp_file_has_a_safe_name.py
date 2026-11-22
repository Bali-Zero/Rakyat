"""Regression tests for legal-ingest temporary files and Drive queries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_http_client(content: bytes = b"%PDF-synthetic") -> AsyncMock:
    response = MagicMock()
    response.content = content
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_download_with_slash_number_writes_directly_in_temp_directory(tmp_path: Path):
    from backend.services.ingestion.legal_full_ingestion_worker import _download_pdf

    temp_dir = tmp_path / "legal-job"
    temp_dir.mkdir()
    client = _mock_http_client()
    with (
        patch(
            "backend.services.ingestion.legal_full_ingestion_worker.httpx.AsyncClient",
            return_value=client,
        ),
        patch("tempfile.mkdtemp", return_value=str(temp_dir)),
    ):
        path = await _download_pdf(
            "https://example.test/regulation.pdf", "SE", "PER-12/PJ/2024", "2024"
        )

    assert path.exists()
    assert path.parent == temp_dir
    assert "/" not in path.name


@pytest.mark.asyncio
async def test_download_with_dot_dot_number_cannot_leave_temp_directory(tmp_path: Path):
    from backend.services.ingestion.legal_full_ingestion_worker import _download_pdf

    temp_dir = tmp_path / "legal-job"
    temp_dir.mkdir()
    client = _mock_http_client()
    with (
        patch(
            "backend.services.ingestion.legal_full_ingestion_worker.httpx.AsyncClient",
            return_value=client,
        ),
        patch("tempfile.mkdtemp", return_value=str(temp_dir)),
    ):
        path = await _download_pdf(
            "https://example.test/regulation.pdf", "SE", "../../escape", "2024"
        )

    assert path.parent == temp_dir
    assert path.resolve().is_relative_to(temp_dir.resolve())


@pytest.mark.asyncio
async def test_download_removes_temp_directory_when_write_fails(tmp_path: Path):
    from backend.services.ingestion.legal_full_ingestion_worker import _download_pdf

    temp_dir = tmp_path / "legal-job"
    temp_dir.mkdir()
    client = _mock_http_client()
    with (
        patch(
            "backend.services.ingestion.legal_full_ingestion_worker.httpx.AsyncClient",
            return_value=client,
        ),
        patch("tempfile.mkdtemp", return_value=str(temp_dir)),
        patch("pathlib.Path.write_bytes", side_effect=OSError("synthetic write failure")),
        pytest.raises(OSError),
    ):
        await _download_pdf("https://example.test/regulation.pdf", "SE", "12", "2024")

    assert not temp_dir.exists()


@pytest.mark.asyncio
async def test_job_cleanup_removes_the_download_temporary_directory(tmp_path: Path):
    import backend.services.ingestion.legal_full_ingestion_worker as worker

    temp_dir = tmp_path / "legal-job"
    temp_dir.mkdir()
    pdf_path = temp_dir / "SE_12_2024.pdf"
    pdf_path.write_bytes(b"%PDF-synthetic")
    job = {
        "id": "synthetic-job",
        "tipo": "SE",
        "nomor": "12",
        "anno": "2024",
        "source_url": "https://example.test/regulation.pdf",
        "nb_target": "NB-3",
        "titolo": "Synthetic regulation",
        "status": "pending",
    }
    conn = AsyncMock()

    @asynccontextmanager
    async def transaction():
        yield None

    @asynccontextmanager
    async def acquire():
        yield conn

    conn.transaction = MagicMock(return_value=transaction())
    pool = MagicMock()
    pool.acquire = acquire
    failing_service = MagicMock()
    failing_service.ingest_legal_document = AsyncMock(side_effect=OSError("synthetic failure"))

    with (
        patch.object(worker, "_claim_job", new=AsyncMock(return_value=job)),
        patch.object(worker, "_download_pdf", new=AsyncMock(return_value=pdf_path)),
        patch(
            "backend.services.ingestion.legal_ingestion_service.LegalIngestionService",
            return_value=failing_service,
        ),
    ):
        await worker._process_one_job(pool, MagicMock())

    assert not temp_dir.exists()


@pytest.mark.parametrize(
    ("function_name", "args"),
    [
        ("_drive_get_or_create_folder", ("Director's Office", "root\\folder")),
        ("_drive_find_file", ("Director's file.pdf", "folder\\id")),
    ],
)
def test_drive_search_escapes_quotes_and_backslashes(function_name: str, args: tuple[str, str]):
    import backend.services.ingestion.legal_full_ingestion_worker as worker

    service = MagicMock()
    service.files.return_value.list.return_value.execute.return_value = {"files": [{"id": "found"}]}

    getattr(worker, function_name)(service, *args)

    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "\\'" in query
    assert "\\\\" in query


@pytest.mark.asyncio
async def test_plain_number_keeps_existing_drive_name(tmp_path: Path):
    from backend.services.ingestion.legal_full_ingestion_worker import _download_pdf

    temp_dir = tmp_path / "legal-job"
    temp_dir.mkdir()
    client = _mock_http_client()
    with (
        patch(
            "backend.services.ingestion.legal_full_ingestion_worker.httpx.AsyncClient",
            return_value=client,
        ),
        patch("tempfile.mkdtemp", return_value=str(temp_dir)),
    ):
        path = await _download_pdf("https://example.test/regulation.pdf", "SE", "12", "2024")

    assert path.name == "SE_12_2024.pdf"
