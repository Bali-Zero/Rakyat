"""Unit tests for Jev-style closed-set taxonomy classifier & writer parity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.intake import closed_taxonomy, writer


def test_canonical_codes_count_and_writer_map_parity() -> None:
    assert len(closed_taxonomy.CANONICAL_DOCUMENT_CODES) == 41
    assert len(set(closed_taxonomy.CANONICAL_DOCUMENT_CODES)) == 41
    assert "itas" in closed_taxonomy.CANONICAL_DOCUMENT_CODES
    assert writer.DOCUMENT_CATEGORY_MAP["itas"] == "immigration"
    # Legacy mappings stay intact for Drive compatibility tests
    assert writer.DOCUMENT_CATEGORY_MAP["bank_statement"] == "other"
    assert writer.DOCUMENT_CATEGORY_MAP["payment_receipt"] == "other"
    for code in closed_taxonomy.CANONICAL_DOCUMENT_CODES:
        assert code in writer.DOCUMENT_CATEGORY_MAP


@pytest.mark.parametrize(
    ("file_name", "ocr_text", "expected_code"),
    [
        ("NIB_company.pdf", "NOMOR INDUK BERUSAHA OSS RBA", "nib"),
        ("SK_Kemenkumham_PT.pdf", "Keputusan Menteri Hukum AHU-00123.AH.01.01", "sk_kemenkumham"),
        ("akta_pendirian_notaris.pdf", "AKTA PENDIRIAN PERSEROAN TERBATAS", "akta_pendirian"),
        ("e_itas_holder.pdf", "Izin Tinggal Terbatas Elektronik ITAS", "itas"),
        ("npwp_badan.pdf", "NPWP PT CONTOH MAJU ABADI DIREKTORAT JENDERAL PAJAK", "npwp_company"),
        ("npwp_pribadi.pdf", "NPWP WAJIB PAJAK ORANG PRIBADI", "npwp"),
        ("surat_domisili.pdf", "SURAT KETERANGAN DOMISILI PERUSAHAAN KELURAHAN", "surat_domisili"),
        ("lkpm_q4.pdf", "LAPORAN KEGIATAN PENANAMAN MODAL LKPM TRIWULAN", "lkpm"),
        ("passport_scan.jpg", "REPUBLIC OF PASSPORT PASPOR MACHINE READABLE", "passport"),
    ],
)
def test_deterministic_closed_set_classification(
    file_name: str, ocr_text: str, expected_code: str
) -> None:
    dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name=file_name,
        ocr_text=ocr_text,
    )
    assert not dec.abstained
    assert dec.code == expected_code
    assert dec.via == "deterministic"
    assert dec.document_category is not None


def test_deterministic_word_boundary_prevents_false_positives() -> None:
    # 'cv' or 'nib' inside longer English/Indonesian words must NOT false-trigger.
    dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="received_omnibus_draft.pdf",
        ocr_text="This memo reviews the omnibus schedule with zero formal IDs.",
    )
    assert dec.abstained
    assert dec.code == closed_taxonomy.ABSTAIN_CODE


def test_project_drive_destination_individual_and_company() -> None:
    ind_folder, ind_hint = closed_taxonomy.project_drive_destination(
        "itas", target_kind="individual"
    )
    assert ind_folder == "02_Immigration"
    assert ind_hint == "Actual Visa"

    comp_folder, comp_hint = closed_taxonomy.project_drive_destination(
        "nib", target_kind="company"
    )
    assert comp_folder == "01_Legal_and_Licensing"
    assert comp_hint == "NIB"

    tax_folder, tax_hint = closed_taxonomy.project_drive_destination(
        "lkpm", target_kind="company"
    )
    assert tax_folder == "02_Tax_Returns_and_Payments"
    assert tax_hint == "LKPM reports"


def test_local_host_guard_rejects_public_or_non_local_ollama_urls() -> None:
    assert closed_taxonomy._is_allowed_local_ollama_url("http://127.0.0.1:11434") is True
    assert closed_taxonomy._is_allowed_local_ollama_url("http://localhost:11434") is True
    assert closed_taxonomy._is_allowed_local_ollama_url("http://pro:11434") is True
    assert closed_taxonomy._is_allowed_local_ollama_url("http://100.64.0.1:11434") is True
    assert closed_taxonomy._is_allowed_local_ollama_url("http://100.111.132.7:11434") is True
    assert closed_taxonomy._is_allowed_local_ollama_url("https://something.ts.net") is False
    assert closed_taxonomy._is_allowed_local_ollama_url("http://host.docker.internal:11434") is False
    assert closed_taxonomy._is_allowed_local_ollama_url("http://192.168.1.50:11434") is False
    assert closed_taxonomy._is_allowed_local_ollama_url("http://10.0.0.5:11434") is False
    assert closed_taxonomy._is_allowed_local_ollama_url("https://api.openai.com/v1") is False
    assert closed_taxonomy._is_allowed_local_ollama_url("https://8.8.8.8:11434") is False


@pytest.mark.asyncio
async def test_local_llm_single_shot_uses_think_false_and_abstains_low_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    closed_taxonomy._ollama_client = None
    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.json.return_value = {
        "message": {
            "content": json.dumps(
                {"code": "virtual_office", "subject_kind": "company", "confidence": 0.88}
            )
        }
    }
    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp_ok)

    with patch(
        "backend.services.intake.closed_taxonomy.httpx.AsyncClient",
        return_value=mock_client,
    ) as client_ctor:
        dec = await closed_taxonomy.classify_taxonomy(
            file_name="doc_scan.pdf",
            ocr_text="Perjanjian Sewa Ruang Kantor Virtual Office Jl Raya",
            allow_local_llm=True,
            ollama_url="http://127.0.0.1:11434",
        )
        assert not dec.abstained
        assert dec.code == "virtual_office"
        assert dec.via == "local_llm"
        assert client_ctor.call_args.kwargs.get("trust_env") is False
        posted_json = mock_client.post.await_args.kwargs["json"]
        assert posted_json["think"] is False
        assert posted_json["format"] == closed_taxonomy.CLOSED_TAXONOMY_JSON_SCHEMA

        # Low-confidence output below floor -> safe abstain
        mock_resp_ok.json.return_value = {
            "message": {
                "content": json.dumps(
                    {"code": "nib", "subject_kind": "company", "confidence": 0.25}
                )
            }
        }
        low = await closed_taxonomy.classify_taxonomy_closed_llm(
            file_name="ambiguous.pdf",
            ocr_text="Blurry scan text without clear anchors",
            ollama_url="http://127.0.0.1:11434",
        )
        assert low.abstained
        assert low.code == closed_taxonomy.ABSTAIN_CODE
    closed_taxonomy._ollama_client = None


def test_punctuation_anchors_and_guardrails_prevent_false_positives() -> None:
    mrz_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="scan_01.jpg",
        ocr_text="P<IDNDOE<<JOHN<<<<<<<<<<<<<<<<<<<<",
    )
    assert not mrz_dec.abstained
    assert mrz_dec.code == "passport"

    ahu_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="doc_2025.pdf",
        ocr_text="Keputusan Menteri AHU-0012345.AH.01.01 Tahun",
    )
    assert not ahu_dec.abstained
    assert ahu_dec.code == "sk_kemenkumham"

    diploma_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="grad_cert.pdf",
        ocr_text="Bachelor's Degree Certificate in Computer Science",
    )
    assert not diploma_dec.abstained
    assert diploma_dec.code == "diploma"

    sponsor_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="doc_02.pdf",
        ocr_text="Surat Jaminan dan Tanggung Jawab Sponsor Visa untuk pemegang paspor kebangsaan Italia",
    )
    assert not sponsor_dec.abstained
    assert sponsor_dec.code == "sponsor_letter"

    stm_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="doc_03.pdf",
        ocr_text="Surat Tanda Melapor Kepolisian Resor Badung pemegang paspor nomor X123",
    )
    assert not stm_dec.abstained
    assert stm_dec.code == "stm"

    dom_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="doc_04.pdf",
        ocr_text="Surat Keterangan Domisili Tempat Tinggal pemegang paspor berlaku",
    )
    assert not dom_dec.abstained
    assert dom_dec.code == "domicile_letter"

    skdp_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="doc_04b.pdf",
        ocr_text="Surat Keterangan Domisili Perusahaan PT Contoh Maju",
    )
    assert not skdp_dec.abstained
    assert skdp_dec.code == "surat_domisili"

    cv_corp_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="company_doc.pdf",
        ocr_text="Akta Pendirian CV Maju Jaya Bersama berkedudukan di Badung",
    )
    assert cv_corp_dec.code != "cv"

    cv_person_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="CV_John_Resume.pdf",
        ocr_text="Curriculum Vitae work experience software engineer education",
    )
    assert not cv_person_dec.abstained
    assert cv_person_dec.code == "cv"

    tie_dec = closed_taxonomy.classify_taxonomy_deterministic(
        file_name="doc_ambig.pdf",
        ocr_text="NIB LKPM triwulan",
    )
    assert tie_dec.abstained
    assert tie_dec.code == closed_taxonomy.ABSTAIN_CODE

    assert closed_taxonomy.normalize_to_canonical_code("ktp") == "other"
    assert closed_taxonomy.normalize_to_canonical_code("family_card") == "other"
    assert closed_taxonomy.normalize_to_canonical_code("travel_ticket") == "other"
    assert closed_taxonomy.category_for_code("ktp") == "personal"
    assert closed_taxonomy.category_for_code("family_card") == "family"


def test_cli_help_runs_without_jwt_or_api_keys(tmp_path: Path) -> None:
    app_root = Path(__file__).resolve().parents[4]
    clean_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(app_root)}
    proc = subprocess.run(
        [sys.executable, str(app_root / "scripts" / "intake_batch_triage.py"), "--help"],
        cwd=str(tmp_path),
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
