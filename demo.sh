#!/usr/bin/env bash
# One-shot demo. Requires only python3 (>= 3.10). No installs, no network.
set -euo pipefail
cd "$(dirname "$0")"
echo "mcp-resource-heavy demo ($(python3 --version))"
exec python3 demo.py "$@"
