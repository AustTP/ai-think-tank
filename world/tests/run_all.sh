#!/bin/bash
# Full regression gate: every Python test under world/tests plus the JavaScript
# coverage harness (which runs every world/tests/test_*.mjs and enforces the
# 90% line-coverage floor). Run from anywhere:
#   bash world/tests/run_all.sh
set -euo pipefail
cd "$(dirname "$0")/../.." || exit 1
python3 -m pytest world/tests -q -p no:cacheprovider
node world/tests/js_coverage.mjs
