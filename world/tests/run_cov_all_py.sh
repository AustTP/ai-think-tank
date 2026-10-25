#!/bin/bash
# Measure line coverage across every Python source file (world/, scripts/,
# root health.py, and the sandbox checkout helper) by running the full pytest
# suite under coverage. The standalone soak harness is excluded: it is an
# operator-run failure-injection tool, not imported application code. Run from
# the repo root:
#   bash world/tests/run_cov_all_py.sh
set -e
cd "$(dirname "$0")/../.." || exit 1
rm -f .coverage
ROOT="$(pwd)"
export PYTHONPATH="$ROOT:$ROOT/world:$ROOT/scripts:$ROOT/world/sandbox:$ROOT/sandboxes/sb-p1${PYTHONPATH:+:$PYTHONPATH}"
python3 -m coverage run \
    --source="world,scripts,health,sandboxes/sb-p1/checkout.py" \
    --omit='*/tests/*,scripts/soak_tank.py' \
    -m pytest world/tests -q -p no:cacheprovider
python3 -m coverage report --fail-under=90
