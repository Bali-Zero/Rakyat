"""Wall-clock and verdict guards for the generic fold."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from backend.scripts.visa_engine.fold_pack_generic import assert_wall_clock_sanity, fold
from backend.scripts.visa_engine.fold_pack_seq25 import load_ledger
from backend.services.visa_engine.bundle import StaticTrustStore
from backend.tests.scripts.visa_engine import test_fold_pack as base
from backend.tests.scripts.visa_engine.test_fold_pack import (
    _LEDGER_DIR,
    META,
    OBSERVED_AT,
    _mutate,
    _portal_ids,
    _run,
)


@pytest.fixture(scope="module")
def anchor() -> dict[str, Any]:
    return base._read_json(base._PACKS / "rulepack-prod-024.source.json")


@pytest.fixture(scope="module")
def anchor_signed() -> dict[str, Any]:
    return base._read_json(base._PACKS / "rulepack-prod-024.signed.json")


@pytest.fixture
def trust(monkeypatch: pytest.MonkeyPatch) -> StaticTrustStore:
    monkeypatch.setenv(base._ENV, base.TRUST_JSON)
    return StaticTrustStore.from_env()


@pytest.fixture
def ledger_copy(tmp_path: Path) -> Path:
    target = tmp_path / "ledger"
    shutil.copytree(_LEDGER_DIR, target)
    return target


class TestFoldWallClockGuards:
    def test_guilt_a_judgement_after_observed_time_is_refused(
        self,
        anchor: dict[str, object],
        anchor_signed: dict[str, object],
        trust: StaticTrustStore,
        ledger_copy: Path,
    ) -> None:
        victim = _portal_ids(anchor)[0]
        _mutate(
            ledger_copy,
            "*-judgements.jsonl",
            victim,
            lambda row: row.update(judged_at="2026-10-07T13:45:01Z"),
        )

        with pytest.raises(SystemExit, match="is not between the first successful read .* and now"):
            _run(anchor, anchor_signed, load_ledger(ledger_copy), trust)

    def test_innocence_a_judgement_at_observed_time_is_accepted(
        self,
        anchor: dict[str, object],
        anchor_signed: dict[str, object],
        trust: StaticTrustStore,
        ledger_copy: Path,
    ) -> None:
        _mutate(
            ledger_copy,
            "*-judgements.jsonl",
            _portal_ids(anchor)[0],
            lambda row: row.update(judged_at="2026-10-07T13:45:00Z"),
        )

        out = fold(
            anchor,
            anchor_signed,
            load_ledger(ledger_copy),
            trust_store=trust,
            observed_at=OBSERVED_AT,
            **{**META, "created_at": "2026-10-07T13:45:00Z"},
        )

        assert out["created_at"] == "2026-10-07T13:45:00Z"

    @pytest.mark.parametrize("verdict", ["unsure", "bogus"])
    def test_guilt_unacceptable_judgement_verdict_is_refused(
        self,
        verdict: str,
        anchor: dict[str, object],
        anchor_signed: dict[str, object],
        trust: StaticTrustStore,
        ledger_copy: Path,
    ) -> None:
        victim = _portal_ids(anchor)[0]
        _mutate(
            ledger_copy,
            "*-judgements.jsonl",
            victim,
            lambda row: row.update(semantic_change=verdict),
        )

        expected = (
            "a reader is unsure — resolve before stamping"
            if verdict == "unsure"
            else "judgement verdict 'bogus' is not one of"
        )
        with pytest.raises(SystemExit, match=expected):
            _run(anchor, anchor_signed, load_ledger(ledger_copy), trust)

    def test_innocence_an_allowed_judgement_verdict_is_accepted(
        self,
        anchor: dict[str, object],
        anchor_signed: dict[str, object],
        trust: StaticTrustStore,
        ledger_copy: Path,
    ) -> None:
        _mutate(
            ledger_copy,
            "*-judgements.jsonl",
            _portal_ids(anchor)[0],
            lambda row: row.update(semantic_change="none"),
        )

        _run(anchor, anchor_signed, load_ledger(ledger_copy), trust)

    def test_guilt_created_at_after_observed_time_is_refused(
        self, anchor: dict[str, object], anchor_signed: dict[str, object], trust: StaticTrustStore
    ) -> None:
        with pytest.raises(SystemExit, match="created_at '2026-10-07T13:45:01Z' is in the future"):
            fold(
                anchor,
                anchor_signed,
                load_ledger(_LEDGER_DIR),
                trust_store=trust,
                observed_at=OBSERVED_AT,
                **{**META, "created_at": "2026-10-07T13:45:01Z"},
            )

    def test_innocence_created_at_at_observed_time_is_accepted(
        self, anchor: dict[str, object], anchor_signed: dict[str, object], trust: StaticTrustStore
    ) -> None:
        out = fold(
            anchor,
            anchor_signed,
            load_ledger(_LEDGER_DIR),
            trust_store=trust,
            observed_at=OBSERVED_AT,
            **{**META, "created_at": "2026-10-07T13:45:00Z"},
        )

        assert out["created_at"] == "2026-10-07T13:45:00Z"

    def test_guilt_verified_at_after_created_at_is_refused(self) -> None:
        created_at = OBSERVED_AT
        payload = {
            "created_at": "2026-10-07T13:45:00Z",
            "source_records": [
                {
                    "source_record_id": "00000000-0000-0000-0000-000000000001",
                    "verified_at": "2026-10-07T13:45:01Z",
                }
            ],
        }

        with pytest.raises(
            SystemExit, match="00000000: verified_at '2026-10-07T13:45:01Z' is after created_at"
        ):
            assert_wall_clock_sanity(payload, now=created_at)

    def test_innocence_verified_at_equal_to_created_at_is_accepted(self) -> None:
        payload = {
            "created_at": "2026-10-07T13:45:00Z",
            "source_records": [
                {
                    "source_record_id": "00000000-0000-0000-0000-000000000001",
                    "verified_at": "2026-10-07T13:45:00Z",
                }
            ],
        }

        assert_wall_clock_sanity(payload, now=OBSERVED_AT)
