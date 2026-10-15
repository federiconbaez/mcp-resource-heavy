#!/usr/bin/env bash
# Regenerate everything under evidence/ from a clean run. Aborts on the first failure (set -e).
# Usage: tools/build_evidence.sh [--skip-mutation]
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p evidence
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
{ echo "# generated $(ts) | $(python3 --version) | $(uname -sr)"; } > evidence/run_info.txt
echo "[1/6] unit tests";            python3 -m unittest discover -v > evidence/test_output.txt 2>&1
echo "[2/6] conformance";           python3 -m mcpres conformance --json evidence/conformance_report.json > evidence/conformance_output.txt 2>&1
echo "[3/6] stdlib-only check";     python3 tools/check_stdlib_only.py > evidence/stdlib_check.txt 2>&1
echo "[4/6] demo";                  ./demo.sh > evidence/demo_output.txt 2>&1
if [ "${1:-}" != "--skip-mutation" ]; then
  echo "[5/6] mutation sanity";     python3 tools/mutation_sanity.py > evidence/mutation_report.txt 2>&1
fi
echo "[6/6] manifest";              python3 tools/make_manifest.py
echo "evidence written:"; ls -l evidence
