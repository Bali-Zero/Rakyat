"""The retained CI entrypoint is inert even with stale caller credentials."""

import os
import subprocess
from pathlib import Path


def test_ci_entrypoint_never_invokes_curl_or_exposes_credentials(tmp_path):
    repo = Path(__file__).resolve().parents[5]
    curl = tmp_path / "curl"
    marker = tmp_path / "called"
    curl.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n')
    curl.chmod(0o700)
    env = dict(
        os.environ,
        PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
        TELEGRAM_BOT_TOKEN="stale-placeholder",
        TELEGRAM_OWNER_CHAT_ID="synthetic-owner",
    )
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts/ci/telegram_notify.sh"), "--stdin"],
        input="synthetic body",
        text=True,
        capture_output=True,
        env=env,
        timeout=3,
    )
    assert result.returncode == 0
    assert "retired" in result.stdout.lower()
    assert "no message sent" in result.stdout.lower()
    assert not marker.exists()
    assert "stale-placeholder" not in result.stdout + result.stderr
    assert "synthetic-owner" not in result.stdout + result.stderr
