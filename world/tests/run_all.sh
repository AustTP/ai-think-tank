#!/bin/bash
# Full regression gate: every Python test under world/tests plus the JavaScript
# coverage harness (which runs every world/tests/test_*.mjs and enforces the
# 90% line-coverage floor). Run from anywhere:
#   bash world/tests/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1
python3 scripts/check_module_boundaries.py
python3 scripts/check_business_processes.py
python3 evals/run_evals.py --dry-run
# CI (GitHub runner) kills this step ~84s in; verbose output names the exact
# test running at the kill so the cause is diagnosable. Local runs stay quiet.
if [ "${CI:-}" = "true" ]; then
    python3 -m pytest world/tests -v --tb=no -p no:cacheprovider
else
    python3 -m pytest world/tests -q -p no:cacheprovider
fi
node world/tests/js_coverage.mjs
