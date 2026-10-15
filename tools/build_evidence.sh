#!/usr/bin/env bash
# Regenerate everything under evidence/ from a clean run. Aborts on the first failure (set -e).
# Usage: tools/build_evidence.sh [--skip-mutation] [--skip-model]
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p evidence
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
SKIP_MUT=0; SKIP_MODEL=0
for a in "$@"; do case "$a" in --skip-mutation) SKIP_MUT=1;; --skip-model) SKIP_MODEL=1;; esac; done
{ echo "# generated $(ts) | $(python3 --version) | $(uname -sr)"; echo "# git HEAD: $(git rev-parse --short HEAD 2>/dev/null || echo none)"; } > evidence/run_info.txt
echo "[1/10] unit tests";           python3 -m unittest discover -v > evidence/test_output.txt 2>&1
echo "[2/10] conformance";          python3 -m mcpres conformance --json evidence/conformance_report.json > evidence/conformance_output.txt 2>&1
echo "[3/10] stdlib-only check";    python3 tools/check_stdlib_only.py > evidence/stdlib_check.txt 2>&1
echo "[4/10] demo";                 ./demo.sh > evidence/demo_output.txt 2>&1
if [ "$SKIP_MODEL" = 0 ]; then
  echo "[5/10] subscription model check (--depth 6)";  python3 tools/model_check.py --depth 6 > evidence/model_check.txt 2>&1
fi
if [ -d ../mcp-base ] && [ "$(basename "$PWD")" != "mcp-base" ]; then
  echo "[6/10] adaptation report";  python3 tools/adaptation_report.py . ../mcp-base > evidence/adaptation_report.txt
fi
if [ "$SKIP_MUT" = 0 ]; then
  echo "[7/10] mutation sanity";    python3 tools/mutation_sanity.py > evidence/mutation_report.txt 2>&1
fi
echo "[8/10] ORIGINALITY block";    python3 tools/gen_originality.py
echo "[9/10] doc references";       python3 tools/check_doc_refs.py > evidence/doc_refs.txt 2>&1
echo "[10/10] manifest";            python3 tools/make_manifest.py
echo "evidence written:"; ls -l evidence
