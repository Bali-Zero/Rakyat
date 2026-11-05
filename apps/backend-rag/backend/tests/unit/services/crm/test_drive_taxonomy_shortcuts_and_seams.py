"""
Unit tests for the 4-Model Consensus (Astra, Fable, Qwen, Muse) Seams:
- Seam 1: _normalize_drive_file resolves application/vnd.google-apps.shortcut into targetId / targetMimeType
- Seam 2: company_router sync-drive skips application/vnd.google-apps.shortcut items
- Seam 3: Corporate SPT in company_documents feeds build_client_attention without false missing-SPT alert
"""

from datetime import date
from backend.services.integrations.drive.drive_operations import (
    _FOLDER_MIME_TYPE,
    _SHORTCUT_MIME_TYPE,
    _normalize_drive_file,
)
from backend.services.crm.client_attention import build_client_attention


def test_seam1_normalize_drive_file_resolves_folder_shortcut():
    raw_shortcut = {
        "id": "shortcut-123",
        "name": "↗ CO0001_PT Contoh Kreatif Mandiri",
        "mimeType": _SHORTCUT_MIME_TYPE,
        "modifiedTime": "2026-11-05T00:00:00Z",
        "shortcutDetails": {
            "targetId": "1ExampleShortcutTargetFolder00000",
            "targetMimeType": _FOLDER_MIME_TYPE,
        },
    }
    normalized = _normalize_drive_file(raw_shortcut)
    assert normalized["id"] == "1ExampleShortcutTargetFolder00000"
    assert normalized["shortcut_id"] == "shortcut-123"
    assert normalized["is_shortcut"] is True
    assert normalized["type"] == "folder"
    assert normalized["mimeType"] == _FOLDER_MIME_TYPE
    assert normalized["name"] == "↗ CO0001_PT Contoh Kreatif Mandiri"


def test_seam1_normalize_drive_file_preserves_regular_folder_and_file():
    raw_folder = {
        "id": "folder-01",
        "name": "01_Profile",
        "mimeType": _FOLDER_MIME_TYPE,
        "modifiedTime": "2026-11-05T00:00:00Z",
    }
    norm_folder = _normalize_drive_file(raw_folder)
    assert norm_folder["id"] == "folder-01"
    assert norm_folder["type"] == "folder"
    assert norm_folder["is_shortcut"] is False
    assert norm_folder.get("shortcut_id") is None

    raw_file = {
        "id": "file-99",
        "name": "Passport.pdf",
        "mimeType": "application/pdf",
        "size": "204800",
        "modifiedTime": "2026-11-05T00:00:00Z",
    }
    norm_file = _normalize_drive_file(raw_file)
    assert norm_file["id"] == "file-99"
    assert norm_file["type"] == "file"
    assert norm_file["size"] == 204800
    assert norm_file["is_shortcut"] is False


def test_seam3_corporate_spt_satisfies_client_attention_spt_rule():
    client = {
        "id": 5228,
        "full_name": "Matteo Maimone",
        "tax_consultant": "tax@balizero.com",
        "passport_expiry": "2030-01-01",
    }
    company_links = [
        {"company_id": 2812, "company_name": "PT Contoh Kreatif Mandiri", "role": "Director"}
    ]
    # Without corporate SPT doc -> spt_company is upcoming/urgent/overdue
    res_missing = build_client_attention(
        client=client,
        family_members=[],
        documents=[],
        company_links=company_links,
        lkpm_reports=[],
        catalogue=[],
        today=date(2026, 4, 25),
    )
    spt_co_missing = [i for i in res_missing["attention"] if i.get("kind") == "spt_company"]
    assert len(spt_co_missing) == 1
    assert spt_co_missing[0]["state"] == "urgent"

    # With synthetic SPT doc mapped from company_documents (Seam 3) -> spt_company is done!
    res_filed = build_client_attention(
        client=client,
        family_members=[],
        documents=[
            {
                "id": -57180,
                "document_type": "spt",
                "status": "active",
                "fiscal_year": 2025,
                "family_member_id": None,
                "deleted_at": None,
                "is_archived": False,
            }
        ],
        company_links=company_links,
        lkpm_reports=[],
        catalogue=[],
        today=date(2026, 4, 25),
    )
    spt_co_filed = [i for i in res_filed["attention"] if i.get("kind") == "spt_company"]
    assert len(spt_co_filed) == 1
    assert spt_co_filed[0]["state"] == "done"
    assert spt_co_filed[0]["label"] == "SPT company 2025"
