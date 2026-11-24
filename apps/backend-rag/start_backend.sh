#!/bin/bash
# Start the local two-process backend topology.

set -euo pipefail

cd "$(dirname "$0")" || exit 1

rag_port="${RAG_PORT:-8081}"
api_port="${PORT:-8080}"

cleanup() {
  kill "${api_pid:-}" "${rag_pid:-}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting RAG worker on http://127.0.0.1:${rag_port}"
PYTHONPATH=. uvicorn backend.app.main_rag:app \
  --host 127.0.0.1 --port "$rag_port" --reload &
rag_pid=$!

echo "Starting API worker on http://127.0.0.1:${api_port}"
RAG_WORKER_URL="http://127.0.0.1:${rag_port}" PYTHONPATH=. \
  uvicorn backend.app.main_api:app --host 0.0.0.0 --port "$api_port" --reload &
api_pid=$!

wait "$api_pid"
