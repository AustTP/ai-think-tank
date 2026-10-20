#!/bin/bash
# Measure aggregate line coverage across EVERY tracked Python source
# file (world/*.py plus root health.py, sandboxes/sb-p1/checkout.py,
# scripts/ helpers and the sandbox browser driver) by running every
# tests/test_*.py under coverage. Run from the repo root:
#   bash world/tests/run_cov_all_py.sh
set -e
cd "$(dirname "$0")/../.." || exit 1
rm -f .coverage
ROOT="$(pwd)"
# PYTHONPATH (not the tests' own sys.path edits) so coverage can
# resolve every --source module name before the first test file
# starts: this coverage build collects nothing from modules that
# only become importable mid-run.
export PYTHONPATH="$ROOT:$ROOT/world:$ROOT/scripts:$ROOT/world/sandbox:$ROOT/sandboxes/sb-p1${PYTHONPATH:+:$PYTHONPATH}"
SOURCES="_gapmap,_village_check,audit_reachability,auth,bank,budget,browser_driver,cleanup_open_items,comprehension,config,containers,content,cron_scripts,egress,grant_store,evidence,export_training_interactions,fs,health,imessage_fetch,jev_grants,memory,notify,phone,planner,probe_model,reset_wrq8_gates,roster,sandbox_proxy,schedule,serve,signals,sim,sim_helpers,sprint,story_budget,sublow,theo_policy,training_capture,vpn_location,vpn_pool,web_helpers,checkout"
FILES=$(ls world/tests/test_*.py | sort)
for f in $FILES; do
    if [ "$(basename "$f")" = "test_visual_regression.py" ]; then
        continue
    fi
    python3 -m coverage run -a --source="$SOURCES" "$f" > /dev/null 2>&1
done
# Entry lines (if __name__ == '__main__') need a direct script
# run. All three fail fast on missing argv/DB before touching
# any real data, and stdin is /dev/null so the browser driver's
# stdin read cannot block on a terminal.
python3 -m coverage run -a --source=imessage_fetch \
    "$ROOT/scripts/imessage_fetch.py" < /dev/null > /dev/null 2>&1
python3 -m coverage run -a --source=export_training_interactions \
    "$ROOT/scripts/export_training_interactions.py" < /dev/null > /dev/null 2>&1
python3 -m coverage run -a --source=browser_driver \
    "$ROOT/world/sandbox/browser_driver.py" < /dev/null > /dev/null 2>&1
python3 -m coverage report --fail-under=90
