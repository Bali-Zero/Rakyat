"""Regression coverage for LAM episode payloads returned by Qdrant scroll."""

from __future__ import annotations

from typing import Any

import pytest

import backend.app.routers.lam_memory as lam_memory


class FakeEpisodeStore:
    def __init__(self, episodes: list[dict[str, Any]]) -> None:
        self.episodes = episodes

    async def scroll(
        self, *, limit: int, metadata_filter: dict[str, str] | None
    ) -> list[dict[str, Any]]:
        return self.episodes[:limit]


async def _set_episode_store(
    monkeypatch: pytest.MonkeyPatch, episodes: list[dict[str, Any]]
) -> None:
    store = FakeEpisodeStore(episodes)

    async def get_store() -> FakeEpisodeStore:
        return store

    monkeypatch.setattr(lam_memory, "_get_qdrant", get_store)


@pytest.mark.asyncio
async def test_list_recent_episodes_reads_fields_from_nested_stored_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_episode_store(
        monkeypatch,
        [
            {
                "id": "synthetic-nested-episode",
                "payload": {
                    "text": "Synthetic saved episode",
                    "metadata": {
                        "content": "Synthetic saved episode",
                        "agent": "synthetic-agent",
                        "tags": ["synthetic-tag"],
                        "outcome": "synthetic-outcome",
                        "timestamp": "2026-11-22T10:00:00Z",
                        "source": "synthetic-source",
                    },
                },
            }
        ],
    )

    result = await lam_memory.list_recent_episodes()

    assert result.episodes[0].content == "Synthetic saved episode"
    assert result.episodes[0].agent == "synthetic-agent"
    assert result.episodes[0].tags == ["synthetic-tag"]
    assert result.episodes[0].outcome == "synthetic-outcome"
    assert result.episodes[0].timestamp == "2026-11-22T10:00:00Z"


@pytest.mark.asyncio
async def test_list_recent_episodes_keeps_only_extra_nested_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_episode_store(
        monkeypatch,
        [
            {
                "id": "synthetic-nested-metadata",
                "payload": {
                    "text": "Synthetic saved episode",
                    "metadata": {
                        "content": "Synthetic saved episode",
                        "agent": "synthetic-agent",
                        "tags": [],
                        "outcome": "",
                        "timestamp": "2026-11-22T10:00:00Z",
                        "source": "synthetic-source",
                    },
                },
            }
        ],
    )

    result = await lam_memory.list_recent_episodes()

    assert result.episodes[0].metadata == {"source": "synthetic-source"}
    assert "metadata" not in result.episodes[0].metadata
    assert "text" not in result.episodes[0].metadata


@pytest.mark.asyncio
async def test_list_recent_episodes_keeps_flat_payloads_and_handles_empty_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_episode_store(
        monkeypatch,
        [
            {
                "id": "synthetic-flat-episode",
                "payload": {
                    "content": "Synthetic flat episode",
                    "agent": "synthetic-flat-agent",
                    "tags": ["flat-tag"],
                    "outcome": "flat-outcome",
                    "timestamp": "2026-11-22T11:00:00Z",
                    "source": "synthetic-flat-source",
                },
            }
        ],
    )

    result = await lam_memory.list_recent_episodes()

    assert result.episodes[0].content == "Synthetic flat episode"
    assert result.episodes[0].agent == "synthetic-flat-agent"
    assert result.episodes[0].tags == ["flat-tag"]
    assert result.episodes[0].outcome == "flat-outcome"
    assert result.episodes[0].timestamp == "2026-11-22T11:00:00Z"
    assert result.episodes[0].metadata == {"source": "synthetic-flat-source"}

    await _set_episode_store(monkeypatch, [])
    empty_result = await lam_memory.list_recent_episodes()

    assert empty_result.episodes == []
    assert empty_result.total == 0
