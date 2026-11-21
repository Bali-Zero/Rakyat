"""The middleware stack no longer carries an NLP scanner, and boot needs no NLP packages."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_SCRIPT = """
import sys

# A None entry makes a package unimportable and unfindable: not installed.
for name in ("presidio_analyzer", "presidio_anonymizer", "spacy"):
    sys.modules[name] = None

from fastapi import FastAPI

from backend.app.setup.middleware_config import register_middleware

app = FastAPI()
register_middleware(app)
names = [m.cls.__name__ for m in app.user_middleware]
if not names:
    raise SystemExit("no middleware was registered")
if any("PII" in n for n in names):
    raise SystemExit(f"unexpected PII middleware: {names}")
"""


def test_register_middleware_works_without_presidio_or_spacy() -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_SCRIPT)],
        cwd=Path(__file__).resolve().parents[4],
        env={**os.environ, "PYTHONPATH": "."},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_scanner_module_is_gone() -> None:
    root = Path(__file__).resolve().parents[3]

    assert not (root / "middleware" / "pii_scanner.py").exists()
