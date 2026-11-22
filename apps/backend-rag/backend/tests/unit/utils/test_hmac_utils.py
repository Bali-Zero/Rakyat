"""Tests for HMAC request signing utilities."""

import json

from backend.utils.hmac_utils import sign_request, verify_signature


def test_hmac_cross_verification() -> None:
    payload = json.dumps({"notebook_id": "nb-immigration", "question": "KITAS requirements"})
    secret = "test-secret-key"

    signature = sign_request(payload, secret)
    assert verify_signature(payload, signature, secret) is True

    tampered = json.dumps({"notebook_id": "nb-immigration", "question": "different"})
    assert verify_signature(tampered, signature, secret) is False
    assert verify_signature(payload, signature, "wrong-secret") is False
