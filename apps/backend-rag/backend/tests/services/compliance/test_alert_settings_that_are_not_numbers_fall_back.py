from __future__ import annotations

import pytest

from backend.services.compliance.alert_feedback import AlertFeedback
from backend.services.compliance.alert_metrics import compute_metrics


class MetricsSettingsConnection:
    def __init__(self, threshold: str | None) -> None:
        self.threshold = threshold

    async def fetchrow(self, query: str, *args: object) -> dict[str, int]:
        return {"acted": 2, "dismissed": 1, "expired": 1}

    async def fetchval(self, query: str, *args: object) -> str | None:
        return self.threshold


class FeedbackSettingsConnection:
    def __init__(self, window: str | None) -> None:
        self.window = window

    async def fetchval(self, query: str, key: str) -> str | None:
        if key == "compliance_alert_autotune_enabled":
            return "true"
        return self.window


@pytest.mark.asyncio
async def test_metrics_malformed_threshold_falls_back_to_absent_setting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    malformed = await compute_metrics(
        MetricsSettingsConnection("7d"),  # type: ignore[arg-type]
        window_days=90,
        category="synthetic_category",
    )
    absent = await compute_metrics(
        MetricsSettingsConnection(None),  # type: ignore[arg-type]
        window_days=90,
        category="synthetic_category",
    )

    assert malformed.threshold_current == absent.threshold_current is None
    assert malformed.sample_size == 4
    assert "compliance_alert_threshold_urgent_synthetic_category" in caplog.text
    assert "not a whole number" in caplog.text
    assert "7d" not in caplog.text


@pytest.mark.asyncio
async def test_retrain_malformed_window_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    windows: list[int] = []

    async def capture_window(connection: object, *, window_days: int) -> dict[str, object]:
        windows.append(window_days)
        return {}

    monkeypatch.setattr(
        "backend.services.compliance.alert_feedback.compute_metrics_all",
        capture_window,
    )

    result = await AlertFeedback(
        connection=FeedbackSettingsConnection("7d")  # type: ignore[arg-type]
    ).retrain()

    assert result == {"changed": [], "reason": "no_change"}
    assert windows == [90]
    assert "compliance_alert_autotune_window_days" in caplog.text
    assert "not a whole number" in caplog.text
    assert "7d" not in caplog.text


@pytest.mark.asyncio
async def test_numeric_and_absent_settings_keep_existing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numeric = await compute_metrics(
        MetricsSettingsConnection("30"),  # type: ignore[arg-type]
        window_days=90,
        category="synthetic_category",
    )
    absent = await compute_metrics(
        MetricsSettingsConnection(None),  # type: ignore[arg-type]
        window_days=90,
        category="synthetic_category",
    )
    windows: list[int] = []

    async def capture_window(connection: object, *, window_days: int) -> dict[str, object]:
        windows.append(window_days)
        return {}

    monkeypatch.setattr(
        "backend.services.compliance.alert_feedback.compute_metrics_all",
        capture_window,
    )

    await AlertFeedback(connection=FeedbackSettingsConnection("30")).retrain()  # type: ignore[arg-type]
    await AlertFeedback(connection=FeedbackSettingsConnection(None)).retrain()  # type: ignore[arg-type]

    assert numeric.threshold_current == 30
    assert absent.threshold_current is None
    assert windows == [30, 90]
