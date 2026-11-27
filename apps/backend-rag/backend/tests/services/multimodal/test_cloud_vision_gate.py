"""Unit tests for the PII-sovereignty cloud-vision gate.

SYMBIOSIS Law 2 / UU PDP Art. 56: OCR of client documents must NOT fall back to
cloud Gemini Vision at all (permanently disabled, owner order 2026-11-27). These
tests lock in: unconditional deny, no re-enable flag, and that the alert helper
never raises on the degradation path.
"""

from unittest.mock import patch

from backend.services.multimodal import cloud_vision_gate


class TestCloudVisionAllowed:
    def test_config_source_has_no_cloud_vision_override(self) -> None:
        """The override flag is gone from config.py: cloud Gemini OCR cannot be enabled."""
        import pathlib

        cfg = pathlib.Path(__file__).resolve()
        # backend/tests/services/multimodal/ -> backend/app/core/config.py
        backend_root = cfg.parents[3]
        config_src = (backend_root / "app" / "core" / "config.py").read_text()
        assert "ocr_allow_cloud_vision" not in config_src

    def test_denied_with_default_settings(self) -> None:
        assert cloud_vision_gate.cloud_vision_allowed() is False

    def test_denied_even_when_a_settings_object_says_true(self) -> None:
        """A stale settings object (or env var) cannot re-open the cloud path."""

        class _S:
            ocr_allow_cloud_vision = True

        with patch("backend.app.core.config.settings", _S()):
            assert cloud_vision_gate.cloud_vision_allowed() is False

    def test_denied_under_a_magicmock_settings_object(self) -> None:
        from unittest.mock import MagicMock

        with patch("backend.app.core.config.settings", MagicMock()):
            assert cloud_vision_gate.cloud_vision_allowed() is False

    def test_denied_when_config_access_raises(self) -> None:
        class _Boom:
            @property
            def ocr_allow_cloud_vision(self):  # type: ignore[no-untyped-def]
                raise RuntimeError("config blew up")

        with patch("backend.app.core.config.settings", _Boom()):
            assert cloud_vision_gate.cloud_vision_allowed() is False


class TestNoteCloudOcrBlocked:
    def test_logs_warning(self, caplog) -> None:  # type: ignore[no-untyped-def]
        import logging

        with caplog.at_level(logging.WARNING):
            cloud_vision_gate.note_cloud_ocr_blocked("unit.test.context")
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "PII-SOVEREIGNTY" in joined
        assert "unit.test.context" in joined

    def test_never_raises_when_alerter_unimportable(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """If `scripts.sentinel_lib.alerter` can't be imported, the helper must
        still complete — the import is inside a try/except by design."""
        import logging
        import sys

        # scripts/ IS on PYTHONPATH in this test env (matches the production
        # runtime the module's own docstring describes) — the import does NOT
        # fail on its own here. Force it deterministically instead: setting a
        # dotted module's sys.modules entry to None makes Python's import
        # system raise ImportError for that exact submodule regardless of
        # whether it's genuinely importable, per the import system's own
        # documented contract (verified: `from scripts.sentinel_lib.alerter
        # import send_alert` raises ImportError under this patch).
        with patch.dict(sys.modules, {"scripts.sentinel_lib.alerter": None}):
            with caplog.at_level(logging.DEBUG, logger="backend.multimodal.cloud_vision_gate"):
                cloud_vision_gate.note_cloud_ocr_blocked("ctx-unimportable")
        # Proves the call actually reached and completed the except branch
        # (swallowing the ImportError), not merely that no exception happened
        # to propagate up through the test.
        debug_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Telegram alert unavailable" in msg for msg in debug_messages)

    def test_never_raises_when_send_alert_explodes(self, caplog) -> None:  # type: ignore[no-untyped-def]
        """When send_alert IS importable but raises, the failure is swallowed."""
        import logging
        import sys
        import types

        fake_alerter = types.ModuleType("scripts.sentinel_lib.alerter")

        def _boom(*_a, **_k):  # noqa: ANN002, ANN003
            raise RuntimeError("telegram down")

        fake_alerter.send_alert = _boom  # type: ignore[attr-defined]
        fake_pkg = types.ModuleType("scripts.sentinel_lib")
        with patch.dict(
            sys.modules,
            {
                "scripts.sentinel_lib": fake_pkg,
                "scripts.sentinel_lib.alerter": fake_alerter,
            },
        ):
            # Must complete without raising despite send_alert exploding.
            with caplog.at_level(logging.DEBUG, logger="backend.multimodal.cloud_vision_gate"):
                cloud_vision_gate.note_cloud_ocr_blocked("ctx-explode")
        # Proves the RuntimeError from send_alert was actually caught and
        # swallowed by the except block, not just that nothing propagated.
        debug_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
        assert any(
            "Telegram alert unavailable" in msg and "telegram down" in msg
            for msg in debug_messages
        )

    def test_returns_none(self) -> None:
        assert cloud_vision_gate.note_cloud_ocr_blocked("ctx") is None
