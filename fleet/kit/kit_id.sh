#!/usr/bin/env bash
# Print the KIT_ID: a short hash of every file in this kit (path and content). Every launch prompt names the KIT_ID;
# a machine whose kit prints a different one stops and reports it.   bash fleet/kit/kit_id.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
find . -type f ! -name '*.pyc' ! -path '*/__pycache__/*' -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12
