#!/bin/bash
# Real automated regression tests, added after finding several real bugs
# manually that a test suite would have caught (empty-vs-null
# paths, an agent blocking her own escape, camera clamping, activity-log
# dedup). No framework/dependencies -- Node's built-in assert/vm, Python's
# built-in unittest. Run before trusting a change to tasks.js, agents.js,
# world.js, index.html's inline script, or serve.py's pure functions.
#
# NOTE: does NOT `set -e`. A suite this large should report EVERY failing test
# file, not abort at the first one (a first-failure abort hides regressions in
# the ~30 files after it). Failures are collected by the ERR trap below and
# summarized at the end; the script exits nonzero iff anything failed.
cd "$(dirname "$0")/.."
FAILURES=()
_trap_err() {
    FAILURES+=("$BASH_COMMAND")
}
trap '_trap_err' ERR
echo "== JS: pathfinding =="
node tests/test_pathfinding.mjs
echo
echo "== JS: UI helpers =="
node tests/test_ui_helpers.mjs
echo
echo "== JS: mailbox read/unread =="
node tests/test_mailbox.mjs
echo
echo "== JS: orphaned-script-file check =="
node tests/test_link_check.mjs
echo
echo "== JS: sandbox integrity checks (multi-page phantom refs, dangling selectors) =="
node tests/test_sandbox_integrity_checks.mjs
echo
echo "== JS: firing-review backoff =="
node tests/test_firing_backoff.mjs
echo
echo "== JS: researcher headcount cap =="
node tests/test_hire_cap.mjs
echo
echo "== JS: morale dropped-work decay =="
node tests/test_morale.mjs
echo
echo "== JS: agentic probe-request parsing =="
node tests/test_probe_request_parsing.mjs
echo
echo "== JS: idle think tank makes zero API calls =="
node tests/test_idle_quiet.mjs
echo
echo "== JS: smart render-fallback fetch =="
node tests/test_fetch_smart.mjs
echo
echo "== JS: room-capacity overflow =="
node tests/test_room_overflow.mjs
echo
echo "== JS: tasks pure routing predicates =="
node tests/test_tasks_routing.mjs
echo
echo "== JS: server-side sim bridge =="
node tests/test_sim_bridge.mjs
echo
echo "== JS: grading module (checklist meets/fails/insufficient) =="
node tests/test_grading.mjs
echo
echo "== JS: bounded graded revision loop (Phase G3) =="
node tests/test_revision_loop.mjs
echo
echo "== JS: working-guide memory (Phase G4) =="
node tests/test_working_guide.mjs
echo
echo "== Python/JS: findPath parity (sim.find_path === tasks.js findPath) =="
node tests/test_find_path_parity.mjs
echo
echo "== Python/JS: movement parity (step/slide/replan + geometry) =="
node tests/test_movement_parity.mjs
echo
echo "== Python: serve.py =="
python3 tests/test_serve.py
echo
echo "== Python: sim.py =="
python3 tests/test_sim.py
echo
echo "== Python: shadow/dry-run mode (Bot Ops -- work happens, world doesn't move) =="
python3 tests/test_shadow_mode.py
echo
echo "== Python: weekly diff-against-expectation review (Bot Ops -- ground truth, not self-report) =="
python3 tests/test_weekly_review.py
echo
echo "== Python: spend accrual (every real model call is accounted to the cap/Bank) =="
python3 tests/test_spend_accrual.py
echo
echo "== Python: teams data model (Phase A) =="
python3 tests/test_teams.py
echo
echo "== Python: onboard ceremony (Phase B) =="
python3 tests/test_onboard.py
echo
echo "== Python: capability keys + handles (Phase D) =="
python3 tests/test_capability_keys.py
echo
echo "== Python: sprints + scrum masters (Phase C) =="
python3 tests/test_sprints.py
echo
echo "== Python: products + wiki (Phase E) =="
python3 tests/test_products_wiki.py
echo
echo "== Python: peer-approval gate (Phase E addendum) =="
python3 tests/test_peer_approval.py
echo
echo "== Python: spike lane (Phase E2b) =="
python3 tests/test_spikes.py
echo
echo "== Python: spike content executor -- real search_web/browse_page tool loop =="
python3 tests/test_spike_content.py
echo
echo "== Python: hangout room (non-delegable, behind Town Hall) =="
python3 tests/test_hangout.py
echo
echo "== Python: per-team Backlog Refinement (scrum master creates stories) =="
python3 tests/test_refinement.py
echo
echo "== Python: WS-14 -- shared backlog + features + sprint retrospectives (room-free cards) =="
python3 tests/test_shared_backlog.py
echo
echo "== Python: Cut 2 -- grading + roadmap, coaching loop, incident runbooks =="
python3 tests/test_cut2_processes.py
echo
echo "== Python: Cut 3 -- on-call escalation (failed restore -> SM story/spike) =="
python3 tests/test_oncall_escalation.py
echo
echo "== Python: Cut 4 -- agent coding standards (PEP8/toolchain/>=90% cov, hard approval gate) =="
python3 tests/test_coding_standards.py
echo
echo "== Python: external-eval ports -- composite trust gate + half-open circuit probe =="
python3 tests/test_composite_trust.py
echo
echo "== Python: decision tape -- raw model-facing record of every Jev decision at the chokepoint =="
python3 tests/test_decision_tape.py
echo
echo "== Python: DB-backed Jev decisions-model setting -- live switch via /api/jev/model =="
python3 tests/test_jev_model.py
echo
echo "== Python: heterogeneous-judge escalation cross-check + drift circuit + review-grade calibration =="
python3 tests/test_judge_gate_calibration.py
echo
echo "== Python: clarify router -- player ask -> on-call -> KB-first -> completing agent =="
python3 tests/test_clarify_router.py
echo
echo "== Python: ask lane -- genuinely new one-off question -> agent tool loop =="
python3 tests/test_ask.py
echo
echo "== Python: Higgsfield image/video generation tools (estimate -> submit -> poll -> accrue) =="
python3 tests/test_higgsfield.py
echo
echo "== Python: human-in-the-loop -- player tasks (agents wait, player completes, deps release) =="
python3 tests/test_player_tasks.py
echo
echo "== Python: the spine + attention lanes -- player charter and build/reading/open/parking-lot =="
python3 tests/test_charter_lanes.py
echo
echo "== Python: phone check-in -- POST /api/device/checkin (location/battery/Focus/Wi-Fi) =="
python3 tests/test_device_checkin.py
echo
echo "== Python: player-vetted browse allowlist -- skips Jev for named domains only =="
python3 tests/test_browse_allowlist.py
echo
echo "== Python: sandbox egress proxy -- allowlist + per-host rate limit (pure logic) =="
python3 tests/test_sandbox_proxy.py
echo
echo "== Python: sandbox networking setup -- proxy create/recreate-on-drift =="
python3 tests/test_sandbox_networking.py
echo
echo "== Python: browser_act -- Jev-gated real-browser form interactions (endpoint, gate, runner, tool) =="
python3 tests/test_browser_act.py
echo
echo "== Python: the Bank -- model-spend ledger + director teller (used/left/forecast) =="
python3 tests/test_bank.py
echo
echo "== Python: idle on-duty wanderers parked off duty (vanish when idle) =="
python3 tests/test_idle_park.py
echo
echo "== Python: self-heal -- orphaned tasks reclaimed + skill-review sentinel =="
python3 tests/test_self_heal.py
echo
echo "== Python: hive-mind distillation -- archive findings merged into the think tank wiki =="
python3 tests/test_distill.py
echo
echo "== Python: sleep-not-die idle dormancy -- think tank pauses, stays bound, wakes on request =="
python3 tests/test_dormancy.py
echo
echo "== Python: round-robin on-call (Phase E2d) =="
python3 tests/test_oncall.py
echo
echo "== Python: fault-aware routing memory (insect-colony pheromone routing, 2026-09-26) =="
python3 tests/test_fault_aware_routing.py
echo
echo "== Python: stuck-at-review watchdog (Phase E3.1) =="
python3 tests/test_stuck_gate.py
echo
echo "== Python: runs-closed integration (Phase 5 proof) =="
python3 tests/test_runs_closed.py
echo
echo "== Python: full ship-path lifecycle (produce -> peer gate -> release -> publish) =="
python3 tests/test_full_lifecycle.py
echo
echo "== Python: adversarial (winter) village -- toggle, both-side delivery, drain/disable =="
python3 tests/test_adversarial_village.py
echo
echo "== Python: village isolation -- library/sandbox/agent-file scoping =="
python3 tests/test_village_isolation.py

# NOTE: test_visual_regression.py is NOT run here. Every test above is a pure-
# function test with no live server/DB/browser (hermetic, per test_bank.py's
# own docstring). The visual regression check needs a REAL running server and
# a real logged-in browser session, so it can't share that guarantee -- run it
# explicitly and separately when you want it:
#   AI_THINK_TANK_TEST_PASSWORD=... python3 tests/test_visual_regression.py
# (Borrowed from Hermes Town's verify:characters/verify:world,
# real automated checks for exactly the "undefined" nameplate / overlapping-
# sprite class of bug.)

# ---------------------------------------------------------------------------
# Summary: with `set -e` off, every test above ran to completion even if some
# failed. Surface all of them at once -- that is the whole point of not
# aborting early -- and exit nonzero iff any did.
echo
if [ ${#FAILURES[@]} -eq 0 ]; then
    echo "ALL TEST FILES PASSED"
    exit 0
else
    echo "!!!!! FAILED TEST FILES (${#FAILURES[@]}): !!!!!"
    printf '  - %s\n' "${FAILURES[@]}"
    exit 1
fi
