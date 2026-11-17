import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_embeddings_import_needs_no_network_and_loads_the_encoder_on_first_use(
    tmp_path: Path,
) -> None:
    backend_root = Path(__file__).resolve().parents[4]
    script = textwrap.dedent(
        """
        import socket


        def _denied(*args, **kwargs):
            raise OSError("network denied by test")


        socket.socket.connect = _denied
        socket.create_connection = _denied
        socket.getaddrinfo = _denied

        import tiktoken

        attempts = []
        _real_get_encoding = tiktoken.get_encoding


        def _counting_get_encoding(name):
            attempts.append(name)
            return _real_get_encoding(name)


        tiktoken.get_encoding = _counting_get_encoding

        from backend.core import embeddings

        if attempts:
            raise SystemExit(f"encoder loaded at import: {attempts}")

        # First use: the cache dir is empty and the network is denied, so the
        # load fails and the char-count fallback has to answer.
        if embeddings._count_tokens("a" * 30) != 10:
            raise SystemExit("offline first use did not fall back to the char count")
        embeddings._count_tokens("a" * 30)
        if attempts != ["cl100k_base"]:
            raise SystemExit(f"expected one load attempt per process, got {attempts}")
        """
    )
    # An empty cache dir: a vocabulary cached by an earlier run on the same
    # machine would let the old import-time load succeed without the network.
    env = {**os.environ, "PYTHONPATH": ".", "TIKTOKEN_CACHE_DIR": str(tmp_path)}

    # The timeout is a failure ceiling, not a performance assertion.
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
