from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from backend.scripts.visa_engine import replace_activation_set as cli
from backend.tests.scripts.visa_engine.test_replace_activation_set import _replacement


def _bundles() -> list[cli.ReplacementBundle]:
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        replace(
            _replacement(instant, None, sequence=sequence),
            verified=SimpleNamespace(payload_sha256=bytes([sequence]) * 32),
        )
        for sequence in (11, 12, 13)
    ]


def test_validate_chain_advances_the_prior_bundle_sequence_and_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundles = _bundles()
    calls: list[dict[str, object]] = []

    def record_validation(_verified: object, **kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli, "validate_activation", record_validation)

    cli._validate_chain(
        bundles,
        current_sequence=10,
        current_payload_sha256=b"a" * 32,
        engine_version="test-engine",
    )

    assert calls == [
        {
            "current_sequence": 10,
            "current_payload_sha256": b"a" * 32,
            "environment": "TEST",
            "engine_version": "test-engine",
        },
        {
            "current_sequence": 11,
            "current_payload_sha256": bytes([11]) * 32,
            "environment": "TEST",
            "engine_version": "test-engine",
        },
        {
            "current_sequence": 12,
            "current_payload_sha256": bytes([12]) * 32,
            "environment": "TEST",
            "engine_version": "test-engine",
        },
    ]


def test_validate_chain_stops_when_the_second_bundle_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundles = _bundles()
    validated_sequences: list[int] = []

    def reject_second_bundle(_verified: object, **kwargs: object) -> None:
        sequence = int(kwargs["current_sequence"])
        validated_sequences.append(sequence)
        if sequence == 11:
            raise ValueError("synthetic invalid second bundle")

    monkeypatch.setattr(cli, "validate_activation", reject_second_bundle)

    with pytest.raises(ValueError, match="synthetic invalid second bundle"):
        cli._validate_chain(
            bundles,
            current_sequence=10,
            current_payload_sha256=b"a" * 32,
            engine_version="test-engine",
        )

    assert validated_sequences == [10, 11]


class _RecordingPool:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.events.append(f"close:{self.name}")


def _yes_args() -> object:
    return cli._parse_args(
        [
            "replacement.json",
            "--actor",
            "synthetic.operator",
            "--reason",
            "synthetic-test",
            "--current-sequence",
            "10",
            "--current-payload-sha256",
            "ab" * 32,
            "--yes",
        ]
    )


def _patch_yes_run(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> tuple[_RecordingPool, _RecordingPool, cli.ReplacementBundle]:
    bundle = _bundles()[0]
    pack_pool = _RecordingPool("pack", events)
    activation_pool = _RecordingPool("activation", events)

    monkeypatch.setenv(cli.PACK_WRITER_DATABASE_URL_ENV, "postgres://synthetic-pack")
    monkeypatch.setenv(cli.ACTIVATION_DATABASE_URL_ENV, "postgres://synthetic-activation")
    monkeypatch.setattr(cli.StaticTrustStore, "from_env", lambda: object())
    monkeypatch.setattr(cli, "_load_and_verify_bundles", lambda *_args, **_kwargs: [bundle])
    monkeypatch.setattr(cli, "_validate_chain", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_assert_pack_writer_capability", lambda _pool: _value("writer"))
    monkeypatch.setattr(
        cli, "_assert_activation_capability", lambda _pool, **_kwargs: _value("executor")
    )

    async def create_pool(url: str, **_kwargs: object) -> _RecordingPool:
        return pack_pool if url.endswith("pack") else activation_pool

    monkeypatch.setattr(cli.asyncpg, "create_pool", create_pool)
    return pack_pool, activation_pool, bundle


async def _value(value: Any) -> Any:
    return value


@pytest.mark.asyncio
async def test_run_yes_closes_both_pools_when_an_existing_pack_hash_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    pack_pool, activation_pool, _bundle = _patch_yes_run(monkeypatch, events)
    monkeypatch.setattr(cli, "_pack_row_payload_hash", lambda *_args: _value(b"x" * 32))

    class Repository:
        def __init__(self, _pool: object) -> None:
            pass

        async def replace_activation_set(self, **_kwargs: Any) -> list[str]:
            raise AssertionError("activation set must not be replaced")

    monkeypatch.setattr(cli, "VisaEngineRepository", Repository)

    with pytest.raises(SystemExit, match="exists with a different hash"):
        await cli.run(_yes_args())

    assert pack_pool.close_calls == 1
    assert activation_pool.close_calls == 1


@pytest.mark.asyncio
async def test_run_yes_closes_pack_pool_before_replacing_the_activation_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    pack_pool, activation_pool, bundle = _patch_yes_run(monkeypatch, events)
    monkeypatch.setattr(
        cli,
        "_pack_row_payload_hash",
        lambda *_args: _value(bundle.verified.payload_sha256),
    )

    class Repository:
        def __init__(self, pool: object) -> None:
            self.pool = pool

        async def replace_activation_set(self, **_kwargs: Any) -> list[str]:
            events.append("replace_activation_set")
            return ["synthetic-activation"]

    monkeypatch.setattr(cli, "VisaEngineRepository", Repository)

    assert await cli.run(_yes_args()) == 0
    assert events.index("close:pack") < events.index("replace_activation_set")
    assert pack_pool.close_calls == 1
    assert activation_pool.close_calls == 1
