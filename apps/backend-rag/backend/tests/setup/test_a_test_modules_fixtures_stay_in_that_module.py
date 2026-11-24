"""A test module's fixtures stay in that module: the GARUDA JWT fixtures reach no later test.

`pytest_plugins` registers the named module as a session-wide plugin, so its fixtures, autouse
ones included, apply to every test collected after it in the same process. Naming a test module
there made a GARUDA test's autouse JWT-key patch run under every later test of a CI shard, where
the auth router, imported at collection with the real key, then signed tokens the patched key
could not verify.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]
BACKEND_RAG = TESTS_ROOT.parents[1]
_GARUDA_FILE = "backend/tests/services/garuda_portal/test_an_empty_assignee_is_refused.py"
_AUTH_FILE = "backend/tests/unit/app/deps/test_auth_hardened.py"


def test_the_garuda_jwt_fixture_does_not_reach_a_test_collected_after_it() -> None:
    """Real path: pytest's own fixture closure for the auth tests, collected after the GARUDA file."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
            "-o",
            "addopts=",
            "--fixtures-per-test",
            "-v",
            _GARUDA_FILE,
            _AUTH_FILE,
        ],
        cwd=BACKEND_RAG,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = result.stdout
    first_auth_test = f"({_AUTH_FILE}:"
    assert first_auth_test in out, out[-2000:]
    garuda_section, auth_section = out.split(first_auth_test, 1)
    assert "test_new_token_has_all_s03_claims" in auth_section
    assert "_jwt_secret --" in garuda_section
    assert "_jwt_secret" not in auth_section
    assert "_garuda_public_enabled" not in auth_section
