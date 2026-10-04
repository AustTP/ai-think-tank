#!/bin/bash
# Measure aggregate line coverage of sim.py + serve.py across every Python
# test file that touches them (the same 54-file set the original
# coverage.json combined). Run from the world/ dir:
#   bash tests/run_cov_sim_serve.sh
cd "$(dirname "$0")/.." || exit 1
rm -f .coverage
FILES=$(rg -l "import serve|import sim" tests/*.py | rg -v "_find_path_bridge|_movement_bridge" | sort)
for f in $FILES; do
    if [ "$f" = "tests/test_visual_regression.py" ]; then
        continue
    fi
    python3 -m coverage run -a --source=sim,serve "$f" > /dev/null 2>&1
done
python3 -m coverage report --include='*sim.py','*serve.py'