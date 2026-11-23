import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.services.oracle import nlm_notebook_registry as reg


def _write_state(path: Path, started_at: str) -> None:
    path.write_text(
        json.dumps(
            {
                "ingestion_verifications": {
                    "synthetic-notebook": {
                        "last": {
                            "started_at": started_at,
                            "status": "ok",
                            "uuid": "synthetic-canary",
                        }
                    }
                }
            }
        )
    )


def test_future_canary_is_stale(tmp_path):
    state = tmp_path / "state.json"
    started_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _write_state(state, started_at)

    verdict = reg.check_ingestion_freshness("synthetic-notebook", state_path=state)

    assert verdict["status"] == "stale"


def test_naive_canary_timestamp_is_stale(tmp_path):
    state = tmp_path / "state.json"
    started_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    _write_state(state, started_at)

    verdict = reg.check_ingestion_freshness("synthetic-notebook", state_path=state)

    assert verdict["status"] == "stale"


def test_aware_hour_old_canary_is_fresh(tmp_path):
    state = tmp_path / "state.json"
    started_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _write_state(state, started_at)

    verdict = reg.check_ingestion_freshness("synthetic-notebook", state_path=state)

    assert verdict["status"] == "fresh"


def test_aware_canary_one_minute_in_future_is_fresh(tmp_path):
    state = tmp_path / "state.json"
    started_at = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    _write_state(state, started_at)

    verdict = reg.check_ingestion_freshness("synthetic-notebook", state_path=state)

    assert verdict["status"] == "fresh"


def test_aware_canary_older_than_threshold_is_stale(tmp_path):
    state = tmp_path / "state.json"
    started_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    _write_state(state, started_at)

    verdict = reg.check_ingestion_freshness(
        "synthetic-notebook", max_stale_hours=24, state_path=state
    )

    assert verdict["status"] == "stale"
