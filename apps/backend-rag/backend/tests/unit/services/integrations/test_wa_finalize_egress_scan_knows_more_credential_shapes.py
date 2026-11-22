"""Credential shapes recognized by the WhatsApp egress scan."""

from __future__ import annotations

import pytest

from backend.services.integrations.wa_finalize import scan_text_for_secret_egress


@pytest.mark.parametrize(
    ("pattern_name", "token"),
    [
        ("aws_access_key_id", "AK" + "IA" + "A" * 16),
        ("aws_access_key_id", "AS" + "IA" + "A" * 16),
        ("github_token", "gh" + "p_" + "a" * 30),
        ("github_token", "github" + "_pat_" + "a" * 30),
        ("slack_token", "xox" + "b-" + "a" * 20),
        ("google_api_key", "AI" + "za" + "a" * 35),
        ("google_api_key", "AI" + "za" + "a" * 34 + "-"),
    ],
)
def test_scan_catches_bare_common_vendor_credentials(pattern_name: str, token: str) -> None:
    assert scan_text_for_secret_egress(f"The value is {token}.") == pattern_name


@pytest.mark.parametrize(
    "text",
    [
        "The AKIA product identifier is intentionally short.",
        "The github_pat_ route documents a URL path, not a value.",
        "We use xoxb-brief as an ordinary short product code.",
        "The AIza prefix appears in an ordinary short identifier.",
        "CAPITAL LETTERS ARE ORDINARY ENGLISH PROSE.",
    ],
)
def test_scan_does_not_mistake_prefixes_or_prose_for_credentials(text: str) -> None:
    assert scan_text_for_secret_egress(text) is None


@pytest.mark.parametrize(
    ("pattern_name", "text"),
    [
        ("private_key_block", "-----BEGIN " + "TEST " + "PRIVATE KEY-----"),
        ("ssh_pubkey", "ssh" + "-ed25519 " + "A" * 20),
        ("openai_style_key", "sk" + "-" + "A" * 20),
        ("jwt", "ey" + "J" + "a" * 10 + "." + "a" * 10 + "." + "a" * 10),
        ("token_assignment", "api" + "_key = " + "a" * 16),
        ("bearer_token", "Bearer " + "a" * 15 + "1"),
    ],
)
def test_scan_still_catches_existing_credential_shapes(pattern_name: str, text: str) -> None:
    assert scan_text_for_secret_egress(text) == pattern_name
