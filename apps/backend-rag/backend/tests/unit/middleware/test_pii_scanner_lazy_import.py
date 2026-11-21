"""The PII scanner loads presidio and builds its analyzer on the first scan."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_INSTALL = """
import sys

from fastapi import FastAPI

from backend.app.setup.middleware_config import register_middleware

app = FastAPI()
register_middleware(app)
installed = "PIIScannerMiddleware" in [m.cls.__name__ for m in app.user_middleware]
"""


def _run(script: str) -> subprocess.CompletedProcess[str]:
    # A fresh interpreter: in this one presidio may already be imported by
    # another test. The timeout is a failure ceiling, not a performance claim.
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=Path(__file__).resolve().parents[4],
        env={**os.environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_installing_the_middleware_does_not_import_presidio() -> None:
    result = _run(
        _INSTALL
        + """
# Without this the test would pass where the scanner module failed to import
# and the middleware was left out.
if not installed:
    raise SystemExit("the PII scanner middleware was not installed")

loaded = [n for n in ("presidio_analyzer", "presidio_anonymizer", "spacy") if n in sys.modules]
if loaded:
    raise SystemExit(f"installing the middleware imported {loaded}")
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_without_presidio_the_middleware_is_left_out() -> None:
    result = _run(
        """
import sys

# A None entry makes the package unimportable and unfindable: not installed.
sys.modules["presidio_analyzer"] = None
"""
        + _INSTALL
        + """
if installed:
    raise SystemExit("the PII scanner middleware was installed without presidio")
"""
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_a_failed_build_caches_no_analyzer(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.middleware import pii_scanner

    def failing_build() -> list:
        raise RuntimeError("recognizer build failed")

    monkeypatch.setattr(pii_scanner, "_analyzer", None)
    monkeypatch.setattr(pii_scanner, "_indonesian_recognizers", failing_build)
    monkeypatch.setattr("presidio_analyzer.AnalyzerEngine", lambda: object())

    with pytest.raises(RuntimeError):
        pii_scanner._get_analyzer()

    # An engine cached without the Indonesian recognizers would answer every
    # later scan and miss KTP, NPWP, passport and phone numbers.
    assert pii_scanner._analyzer is None
