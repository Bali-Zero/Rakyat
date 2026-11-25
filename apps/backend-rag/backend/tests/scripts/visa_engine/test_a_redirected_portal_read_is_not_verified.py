from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.scripts.visa_engine import fold_pack_generic as fold_generic
from backend.scripts.visa_engine import portal_read_receipt as portal_receipt
from backend.scripts.visa_engine.fold_pack_seq25 import Ledger

RECORD = {
    "source_record_id": "11111111-2222-3333-4444-555555555555",
    "source_key": "synthetic.portal.e31b",
    "canonical_url": "https://portal.example.test/visa/E31B",
    "title": "Synthetic visa (E31B) — Example portal",
}
REDIRECT_URL = "https://portal.example.test/catalogue"
FETCHED_AT = "2026-11-20T10:00:00Z"
NOW = datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc)


def _ledger(receipt: dict[str, object]) -> Ledger:
    return Ledger(
        receipts=[receipt],
        judgements=[
            {
                "source_record_id": RECORD["source_record_id"],
                "judged_at": "2026-11-20T10:30:00Z",
                "semantic_change": "none",
                "checked_sentence": "E31B",
            }
        ],
    )


def _receipt(**extra: object) -> dict[str, object]:
    return {
        "source_record_id": RECORD["source_record_id"],
        "canonical_url": RECORD["canonical_url"],
        "fetched_at": FETCHED_AT,
        "http_status": 200,
        "key_phrase_found": True,
        **extra,
    }


def test_guilt_a_redirected_read_with_the_key_phrase_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        portal_receipt,
        "fetch",
        lambda _url: (200, b"<h1>E31B</h1>", REDIRECT_URL),
    )

    receipt = portal_receipt.read_one(RECORD, reader="synthetic-reader", text_dir=tmp_path)

    assert receipt["key_phrase_found"] is False
    assert "redirect" in receipt["error"]


def test_innocence_a_read_at_its_canonical_url_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        portal_receipt,
        "fetch",
        lambda url: (200, b"<h1>E31B</h1>", url),
    )

    receipt = portal_receipt.read_one(RECORD, reader="synthetic-reader", text_dir=tmp_path)

    assert receipt["key_phrase_found"] is True
    assert "error" not in receipt


def test_guilt_a_receipt_redirected_away_from_its_record_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fold_generic, "_own_text", lambda _ledger, _receipt: "E31B")

    with pytest.raises(SystemExit, match="finished at"):
        fold_generic.attestation_instant(
            _ledger(_receipt(final_url=REDIRECT_URL)),
            [RECORD],
            previous_stamp="2026-11-19T10:00:00Z",
            now=NOW,
        )


def test_innocence_a_legacy_receipt_without_final_url_still_folds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fold_generic, "_own_text", lambda _ledger, _receipt: "E31B")

    assert (
        fold_generic.attestation_instant(
            _ledger(_receipt()),
            [RECORD],
            previous_stamp="2026-11-19T10:00:00Z",
            now=NOW,
        )
        == FETCHED_AT
    )
