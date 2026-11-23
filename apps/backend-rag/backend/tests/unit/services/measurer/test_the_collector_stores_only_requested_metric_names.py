"""The collector stores only the metric names it asked for.

A metric name taken from a Graph response is written as
post_metrics_history.metric_name, so an unrequested name (a token, say) must
never reach the sensor's result or the database.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from backend.services.measurer.ig_graph_sensor import IGGraphSensor
from backend.services.measurer.m13_feedback_loop import M13CollectionHorizon, M13FeedbackLoop

_ODD_NAME = "EAAG" + "Ab1" * 20


class _Acq:
    def __init__(self, conn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *a):
        return None


class _Pool:
    def __init__(self, conn):
        self._c = conn

    def acquire(self):
        return _Acq(self._c)


def _insights(*entries: tuple) -> dict:
    return {"data": [{"name": n, "values": [{"value": v}]} for n, v in entries]}


@pytest.mark.asyncio
async def test_the_sensor_keeps_only_the_names_it_asked_for() -> None:
    sensor = IGGraphSensor(token="tok", ig_user_id="123", http_client=None)
    response = _insights(("likes", 7), (_ODD_NAME, 1), ("saved", 3), (None, 9), ("reach", 50))

    with patch.object(sensor, "_get", AsyncMock(return_value=response)):
        parsed = await sensor._fetch_insights("m1", "IMAGE")

    assert set(parsed) == {"likes", "comments", "saves", "reach", "impressions", "video_views"}
    assert (parsed["likes"], parsed["saves"], parsed["reach"]) == (7, 3, 50)


@pytest.mark.asyncio
async def test_an_unknown_metric_name_is_never_written_or_logged(caplog) -> None:
    conn = AsyncMock()
    loop = M13FeedbackLoop(db_pool=_Pool(conn))

    with caplog.at_level(logging.DEBUG):
        await loop.collect_post_metrics(
            post_id=uuid4(),
            horizon=M13CollectionHorizon.T_24H,
            metrics={"likes": 5, _ODD_NAME: 1, "reach": 40, "click_through": 2},
            source="ig_graph",
        )

    written = [c.args[3] for c in conn.execute.await_args_list]
    assert written == ["likes", "reach", "click_through"]
    assert _ODD_NAME not in caplog.text
    assert "skipped 1 unknown metric name" in caplog.text
