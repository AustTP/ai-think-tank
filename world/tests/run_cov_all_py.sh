#!/bin/bash
# Measure aggregate line coverage across EVERY tracked Python source file
# (world/*.py plus root health.py and sandboxes/sb-p1/checkout.py) by running
# every tests/test_*.py under coverage. Run from the repo root:
#   bash world/tests/run_cov_all_py.sh
cd "$(dirname "$0")/../.." || exit 1
rm -f .coverage
ROOT="$(pwd)"
SOURCES="sim,serve,content,sandbox_proxy,sim_helpers,web_helpers,audit_reachability,probe_model,_village_check,_gapmap,$ROOT/health.py,$ROOT/sandboxes/sb-p1/checkout.py"
FILES=$(ls world/tests/test_*.py | sort)
for f in $FILES; do
    if [ "$(basename "$f")" = "test_visual_regression.py" ]; then
        continue
    fi
    python3 -m coverage run -a --source="$SOURCES" "$f" > /dev/null 2>&1
done
python3 -m coverage report