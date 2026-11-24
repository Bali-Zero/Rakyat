"""``activate_pack.run`` end to end: what it refuses, and in which order it writes.

The script stands between a signed bundle and the activation ledger. Its pure parts were
tested; ``run`` itself was called by no test, so a refactor could insert before verifying,
drop the immutability refusal, open pools in a dry run or skip the production separation, and
nothing would turn red.

The bundle here is REALLY signed, by an ephemeral in-fixture key that the trust store pins
through its environment variable, exactly as in a ceremony. Nothing reaches a database: the
pools are recording stand-ins and ``asyncpg.create_pool`` is the stand-in's factory, so a test
also proves WHEN a pool would have been opened. The dry run opens none.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from backend.scripts.visa_engine import activate_pack
from backend.services.visa_engine.bundle import TRUST_STORE_KEYS_ENV_VAR
from backend.services.visa_engine.errors import RulePackVerificationError
from backend.tests.services.visa_engine._builders import (
    ed25519_public_key_b64url,
    ephemeral_ed25519_keypair,
    minimal_valid_envelope,
    sha256_hex,
    sign_rule_pack_envelope,
)

WRITER_URL = "postgresql://zz-writer.invalid/zz"
ACTIVATION_URL = "postgresql://zz-activation.invalid/zz"
ACTOR = "operator.zz-test"
REASON = "zz-test-ceremony"


class _Ceremony:
    """A signed bundle on disk, its pinned key in the environment, and everything `run` touches."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        environment: str = "TEST",
        **payload: Any,
    ) -> None:
        private_key, public_key = ephemeral_ed25519_keypair()
        body = minimal_valid_envelope()["payload"]
        body.update(environment=environment, **payload)
        self.bundle = sign_rule_pack_envelope(body, private_key=private_key)
        self.pack_id = uuid.UUID(body["rule_pack_id"])
        self.payload_sha256 = bytes.fromhex(self.bundle["payload_sha256"])
        self.path = tmp_path / "pack.signed.json"
        self.write()
        monkeypatch.setenv(
            TRUST_STORE_KEYS_ENV_VAR,
            json.dumps(
                [
                    {
                        "kid": "test-key-1",
                        "public_key": ed25519_public_key_b64url(public_key),
                        "environment": environment,
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": None,
                        "revoked_at": None,
                    }
                ]
            ),
        )
        monkeypatch.setenv(activate_pack.PACK_WRITER_DATABASE_URL_ENV, WRITER_URL)
        monkeypatch.setenv(activate_pack.ACTIVATION_DATABASE_URL_ENV, ACTIVATION_URL)

        # What `run` does, in the order it does it.
        self.events: list[str] = []
        self.existing_hash: bytes | None = None
        self.separation_error: BaseException | None = None
        self.activations: list[dict[str, Any]] = []
        self.inserted: list[dict[str, Any]] = []
        ceremony = self

        class _Pool:
            def __init__(self, name: str) -> None:
                self.name = name

            async def close(self) -> None:
                ceremony.events.append(f"close {self.name}")

        async def create_pool(url: str, **_: Any) -> _Pool:
            name = {WRITER_URL: "writer", ACTIVATION_URL: "activation"}[url]
            ceremony.events.append(f"open {name}")
            return _Pool(name)

        async def existing(pool: _Pool, pack_id: uuid.UUID) -> bytes | None:
            assert pack_id == ceremony.pack_id
            ceremony.events.append(f"read the pack row with {pool.name}")
            return ceremony.existing_hash

        async def separation(writer: _Pool, activation: _Pool) -> None:
            ceremony.events.append(f"separation of {writer.name} and {activation.name}")
            if ceremony.separation_error is not None:
                raise ceremony.separation_error

        class _Repository:
            def __init__(self, pool: _Pool) -> None:
                self._pool = pool

            async def insert_rule_pack(self, **kwargs: Any) -> None:
                ceremony.events.append(f"insert with {self._pool.name}")
                ceremony.inserted.append(kwargs)

            async def activate_rule_pack(self, **kwargs: Any) -> uuid.UUID:
                ceremony.events.append(f"activate with {self._pool.name}")
                ceremony.activations.append(kwargs)
                return uuid.uuid4()

        monkeypatch.setattr(
            activate_pack, "asyncpg", SimpleNamespace(create_pool=create_pool, Range=asyncpg.Range)
        )
        monkeypatch.setattr(activate_pack, "_pack_row_payload_hash", existing)
        monkeypatch.setattr(activate_pack, "_assert_production_separation", separation)
        monkeypatch.setattr(activate_pack, "VisaEngineRepository", _Repository)

    def write(self) -> None:
        self.path.write_text(json.dumps(self.bundle), encoding="utf-8")

    def args(self, *extra: str) -> Any:
        """What the operator types, through the script's own parser and its defaults."""
        return activate_pack._parse_args(
            [str(self.path), "--actor", ACTOR, "--reason", REASON, *extra]
        )


@pytest.fixture
def ceremony(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Ceremony:
    return _Ceremony(tmp_path, monkeypatch)


# ── the dry run is the default and touches no database ───────────────────────


@pytest.mark.asyncio
async def test_without_yes_the_bundle_is_verified_and_no_pool_is_opened(
    ceremony: _Ceremony, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(activate_pack.PACK_WRITER_DATABASE_URL_ENV)
    monkeypatch.delenv(activate_pack.ACTIVATION_DATABASE_URL_ENV)

    with caplog.at_level("INFO", logger="visa_engine.activate_pack"):
        assert await activate_pack.run(ceremony.args()) == 0

    assert ceremony.events == []
    assert (
        f"dry_run=true would_insert_and_activate=true rule_pack_id={ceremony.pack_id}"
        in caplog.text
    )


def test_main_parses_the_command_line_and_runs_the_dry_run(ceremony: _Ceremony) -> None:
    assert activate_pack.main([str(ceremony.path), "--actor", ACTOR, "--reason", REASON]) == 0
    assert ceremony.events == []


# ── a pack that does not verify is never inserted ────────────────────────────


def _flip_a_signature_character(ceremony: _Ceremony) -> None:
    signature = ceremony.bundle["signature"]
    ceremony.bundle["signature"] = ("B" if signature[0] != "B" else "C") + signature[1:]


def _change_the_payload_after_signing(ceremony: _Ceremony) -> None:
    ceremony.bundle["payload"]["created_by"] = "someone-else"


def _sign_with_a_key_the_store_does_not_pin(ceremony: _Ceremony) -> None:
    other_key, _ = ephemeral_ed25519_keypair()
    ceremony.bundle = sign_rule_pack_envelope(ceremony.bundle["payload"], private_key=other_key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [
        _flip_a_signature_character,
        _change_the_payload_after_signing,
        _sign_with_a_key_the_store_does_not_pin,
    ],
    ids=["a flipped signature", "a payload changed after signing", "an unpinned key"],
)
async def test_a_bundle_that_does_not_verify_stops_before_any_pool(
    ceremony: _Ceremony, tamper: Any
) -> None:
    tamper(ceremony)
    ceremony.write()

    with pytest.raises(RulePackVerificationError):
        await activate_pack.run(ceremony.args("--yes"))

    assert ceremony.events == []


@pytest.mark.asyncio
async def test_without_a_trust_store_nothing_is_verified_and_nothing_is_opened(
    ceremony: _Ceremony, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TRUST_STORE_KEYS_ENV_VAR)

    with pytest.raises(RulePackVerificationError):
        await activate_pack.run(ceremony.args("--yes"))

    assert ceremony.events == []


# ── the anti-rollback gate the operator types ────────────────────────────────


@pytest.mark.asyncio
async def test_a_later_pack_is_refused_under_the_first_pack_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The defaults are "no pack is active yet". A pack that names a predecessor does not pass.
    ceremony = _Ceremony(tmp_path, monkeypatch, sequence=2, previous_payload_sha256=sha256_hex("1"))

    with pytest.raises(RulePackVerificationError):
        await activate_pack.run(ceremony.args("--yes"))

    assert ceremony.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "passes"),
    [
        (("--current-sequence", "1", "--current-payload-sha256", sha256_hex("1")), True),
        (("--current-sequence", "1", "--current-payload-sha256", sha256_hex("2")), False),
        (("--current-sequence", "2", "--current-payload-sha256", sha256_hex("1")), False),
    ],
    ids=["the predecessor it names", "another predecessor", "a sequence already reached"],
)
async def test_a_later_pack_passes_only_over_the_predecessor_it_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: tuple[str, ...], passes: bool
) -> None:
    ceremony = _Ceremony(tmp_path, monkeypatch, sequence=2, previous_payload_sha256=sha256_hex("1"))

    if passes:
        assert await activate_pack.run(ceremony.args(*current)) == 0
    else:
        with pytest.raises(RulePackVerificationError):
            await activate_pack.run(ceremony.args("--yes", *current))
    assert ceremony.events == []


# ── with --yes: both urls, then insert with one identity and activate with the other ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [activate_pack.PACK_WRITER_DATABASE_URL_ENV, activate_pack.ACTIVATION_DATABASE_URL_ENV],
    ids=["no pack-writer url", "no activation url"],
)
async def test_a_missing_database_url_exits_before_any_pool(
    ceremony: _Ceremony, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)

    with pytest.raises(SystemExit, match=f"{missing} is not set"):
        await activate_pack.run(ceremony.args("--yes"))

    assert ceremony.events == []


@pytest.mark.asyncio
async def test_a_new_pack_is_inserted_by_the_writer_and_activated_by_the_other_identity(
    ceremony: _Ceremony,
) -> None:
    assert await activate_pack.run(ceremony.args("--yes")) == 0

    assert ceremony.events == [
        "open writer",
        "open activation",
        "read the pack row with writer",
        "insert with writer",
        "activate with activation",
        "close activation",
        "close writer",
    ]
    (inserted,) = ceremony.inserted
    assert inserted["id"] == ceremony.pack_id
    assert inserted["payload_sha256"] == ceremony.payload_sha256
    assert ceremony.activations == [
        {"rule_pack_id": ceremony.pack_id, "activated_by": ACTOR, "activation_reason": REASON}
    ]


@pytest.mark.asyncio
async def test_a_pack_already_stored_with_the_same_payload_is_activated_without_an_insert(
    ceremony: _Ceremony,
) -> None:
    ceremony.existing_hash = ceremony.payload_sha256

    assert await activate_pack.run(ceremony.args("--yes")) == 0

    assert ceremony.inserted == []
    assert ceremony.events.count("activate with activation") == 1
    assert "insert with writer" not in ceremony.events


@pytest.mark.asyncio
async def test_a_pack_already_stored_with_another_payload_is_refused_and_nothing_is_written(
    ceremony: _Ceremony,
) -> None:
    ceremony.existing_hash = bytes.fromhex(sha256_hex("9"))

    with pytest.raises(SystemExit, match="packs are immutable"):
        await activate_pack.run(ceremony.args("--yes"))

    assert ceremony.inserted == [] and ceremony.activations == []
    assert ceremony.events == [
        "open writer",
        "open activation",
        "read the pack row with writer",
        "close activation",
        "close writer",
    ]


# ── production: the two identities are checked before anything is read or written ──


@pytest.mark.asyncio
async def test_a_production_pack_checks_the_separation_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ceremony = _Ceremony(tmp_path, monkeypatch, environment="PRODUCTION")

    assert await activate_pack.run(ceremony.args("--yes")) == 0

    assert ceremony.events[:4] == [
        "open writer",
        "open activation",
        "separation of writer and activation",
        "read the pack row with writer",
    ]
    assert ceremony.events[4:6] == ["insert with writer", "activate with activation"]


@pytest.mark.asyncio
async def test_a_production_pack_whose_separation_fails_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ceremony = _Ceremony(tmp_path, monkeypatch, environment="PRODUCTION")
    ceremony.separation_error = SystemExit("FAIL: one login for both capabilities")

    with pytest.raises(SystemExit, match="one login for both capabilities"):
        await activate_pack.run(ceremony.args("--yes"))

    assert ceremony.inserted == [] and ceremony.activations == []
    assert ceremony.events == [
        "open writer",
        "open activation",
        "separation of writer and activation",
        "close activation",
        "close writer",
    ]


@pytest.mark.asyncio
async def test_a_pack_of_another_environment_is_not_asked_for_the_separation(
    ceremony: _Ceremony,
) -> None:
    assert await activate_pack.run(ceremony.args("--yes")) == 0

    assert not [event for event in ceremony.events if event.startswith("separation")]
