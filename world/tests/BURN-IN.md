# Burn-in checklist

A supervised live-run checklist for the think tank, written 2026-09-24 after a
review found that ~20 commits of behavior (Cut 2, Cut 4, refinement, Knowledge
Social, the Bank, clarify router, distillation, dormancy, and the skill-review
fix itself) had never executed in a live think tank -- they were validated only by
unit tests with injected fakes and deterministic clocks.

This is not generic test advice. Every item marked **⚠️** is a replay of
something this think tank actually did, observed in `think_tank.db` at the last real
tick (2026-09-23 03:16 UTC), where the terminal state was:

- 65 tasks, **all 65 titled "Review pending skill files"**, all in `observatory`
  (56 `needs_review`, 7 `walking`, 2 `done`); task IDs had reached ~5,692
- 7 of 8 agents each simultaneously assigned that same task
- 5 of 8 agents named "Assistant to Ada"/"Assistant to leo" (auto-hire monoculture)
- 9 agents frozen in `_pendingOnboard`
- `owen` and `lyra` both frozen at exactly `(1182.0, 688.0)` with `pathActive: true`
- 795 `library/archive` artifacts with a **median size of 501 bytes** -- receipts
  (title/author/room/timestamp), not deliverables -- against one real product
- lifetime `task_assigned : task_completed` of 14,046 : 490

Run phases 0 and 1 first. Phase 2 is the one that matters most; every item in it
failed in the last live run.

---

## Phase 0 -- Before you start

- [ ] **Back up `think_tank.db`** and note the path. Automated backups exist
      (`509173d`) -- confirm one actually lands in `ai-think-tank-backups/` before
      trusting it.
- [ ] ⚠️ **Prove the suite can't touch the live DB.** Record `think_tank.db` mtime,
      run `tests/run_all.sh`, confirm mtime is unchanged. Today it *will* change:
      `test_composite_trust.py` imports `serve` without repointing `DB_PATH`, so
      `record_model_result('m', ...)` writes phantom model `"m"` into the live
      audit log (1,980 such rows already there). Until that's fixed, **never run
      the test suite during a burn-in.**
- [ ] **Start from a clean DB**, not the wedged one. Burning in on top of 65
      identical tasks and 9 frozen onboards tells you nothing.
- [ ] **Record the baseline** so deltas mean something: `action_log` row count,
      `sim.tick`, roster size, `library/` file count, spend so far.
- [ ] **Build the health check first** -- otherwise you reverse-engineer state
      from raw SQL every time. Read-only starting point:

```python
# read-only. python3 health.py
import sqlite3, json, time, collections
s = json.loads(sqlite3.connect('think_tank.db').execute(
    'select blob from kv_state where id=1').fetchone()[0])
t = s.get('tasks') or {}
inflight = [v for v in t.values() if v.get('status') not in ('done',)]
titles = collections.Counter(v.get('title') for v in inflight)
print('last tick   :', time.strftime('%m-%d %H:%M', time.gmtime(s['sim']['lastTickEpochS'])), '| tick', s['sim']['tick'])
print('statuses    :', collections.Counter(v.get('status') for v in t.values()))
print('queue depth :', len(s.get('workQueue') or []))
print('top title   :', titles.most_common(1),
      f'= {100*titles.most_common(1)[0][1]//max(1,len(inflight))}% of in-flight' if inflight else '')
print('roles       :', collections.Counter((v.get('role') or 'seed') for v in (s.get('agents') or {}).values()))
print('pendingOnb  :', len(s.get('_pendingOnboard') or {}))
```

## Phase 1 -- The headline claim: does it run closed?

- [ ] Start the server, **close the browser completely**, wait 10 minutes.
      `sim.tick` and `lastTickEpochS` must advance.
- [ ] Queue one task with no browser attached. Watch the full path:
      queue -> assign -> walk -> arrive -> work -> complete.
- [ ] Open the browser mid-task. Positions lerp to server truth and the
      renderer's autosave does not clobber engine state (`_merge_server_owned`).
- [ ] **Restart the server mid-task.** The task resumes or is reclaimed -- not
      lost, not double-assigned.
- [ ] ⚠️ `kill -9` mid-tick, restart. Check for a torn `kv_state` blob: it is a
      single JSON row, so a partial write is the worst realistic corruption.

## Phase 2 -- The wedge invariants (watch continuously)

Every one of these failed in the last live run.

- [ ] ⚠️ **No task title exceeds ~30% of in-flight tasks.** Last run: 65 of 65.
- [ ] ⚠️ **No two agents hold the same task title simultaneously** unless the work
      is genuinely parallel. Last run: 7 of 8 agents on one sweep task.
- [ ] ⚠️ **`needs_review` age is bounded.** Nothing sits past a defined timeout.
      Last run: 56 tasks parked there permanently.
- [ ] ⚠️ **Queue depth is not monotonic.** It must come down, not only up.
- [ ] ⚠️ **`task_assigned : task_completed` stays within ~3:1.** Lifetime: 28:1.
- [ ] ⚠️ **No two agents at identical coordinates with `pathActive: true`** across
      more than a couple of ticks (co-location deadlock).
- [ ] ⚠️ **`_pendingOnboard` drains.** No agent frozen mid-onboarding.
- [ ] **Task IDs grow at a sane rate.** Reaching ~5,692 in one session was itself
      the tell.

## Phase 3 -- Do the gates actually gate?

Test adversarially. A gate you have only seen pass has not been tested.

- [ ] **Submit deliberately broken code.** The Cut-4 pipeline (flake8 / mypy /
      bandit / `pytest --cov-fail-under=90`) must block approval.
- [ ] **Confirm one agent cannot self-approve** -- peer close needs two distinct
      clean votes.
- [ ] **Reject something and follow it.** It returns to the author, gets revised,
      and the revision loop terminates.
- [ ] ⚠️ **Grading must produce varied scores.** Every deliverable grading exactly
      `5.0` means the `_grading_decider_default` bug is still live: it hands Jev a
      single criterion keyed `'0-10'`, so `_jev_choice` returns the *string*
      `'0-10'`, `isinstance(choice, (int, float))` is always False, and the grade
      always falls back to trailing-mean-or-5.0 -- while still billing a Jev call.
- [ ] ⚠️ **Read one incident runbook.** If it contains the literal word `summary`,
      the twin bug is still live (`isinstance(choice, str)` is True for the
      single criterion `'summary'`, so the word itself is written as the runbook).
- [ ] **Stuck-gate watchdog fires** on something parked at review.
- [ ] **Self-heal reclaims an orphaned `walking` task** -- force one and confirm.

## Phase 4 -- Spend discipline

- [ ] ⚠️ **Idle think tank = zero model calls.** Empty queue for 30+ minutes;
      `decide` and `chat` counts must not move. This is the failure that once
      burned 37,034 `decide` calls in a single day.
- [ ] **Every `decide` carries a real `agent_id`.** Nulls mean unattributed spend
      is back.
- [ ] **`kv_spend` actually accrues** (zero rows today) and the Bank's
      used/left/forecast reconciles against OpenRouter's own dashboard.
- [ ] **No circuit breaks on slugs that aren't real models.** A `"m"` means test
      code reached live state.
- [ ] **Cadence ceremonies stay quiet with nothing to do**: refinement, social,
      distillation, skill-review.

## Phase 5 -- Does it produce real work?

The question the whole system exists to answer, and the one currently failing
hardest.

- [ ] ⚠️ **Open a completion artifact.** It must contain the deliverable, not a
      receipt. Current median: 501 bytes of title/author/timestamp.
- [ ] ⚠️ **Median artifact size climbs meaningfully** above that baseline.
- [ ] ⚠️ **Process-to-product ratio improves.** Today: 795 archive receipts + 131
      refinement + 56 social + 44 escalation records, against one real product
      (finger-drums, 4 files).
- [ ] **Follow one deliverable end to end** -- request -> story -> build -> peer
      gate -> grade -> release -> publish -- and confirm a real file exists at the
      end of it.
- [ ] **A product accumulates across sessions** instead of restarting from nothing.
- [ ] ⚠️ **Roster stays role-diverse.** Auto-hire produced "Assistant to Ada" x4.

## Phase 6 -- Player paths

- [ ] `/api/intent/ask` returns a real answer and actually uses its tools.
- [ ] Clarify router walks the real chain: on-call -> KB-first -> completing-agent
      fallback.
- [ ] Player quality-veto returns a closed story to its author.
- [ ] Publish-to-GitHub pushes to the repo named by `AI_THINK_TANK_PUBLISH_REPO`
      in `.env` -- **dry-run first**, it targets a real private repo.
- [ ] Dormancy sleeps the think tank and **wakes it on request** without losing state.

---

## Pass / fail

A **pass** requires, after 24 hours unattended:

1. no task title dominating the queue,
2. nothing stuck in `needs_review` past its timeout,
3. spend at zero while idle,
4. roster still role-diverse, and
5. **at least one artifact on disk containing real work product.**

A **fail** on any Phase 2 item means stop and fix before adding anything else --
those are the items that already took the think tank down once.

Recommended before starting: fix the review-gate timeout, the grading
`isinstance` bug, and test DB isolation. All three are cheap, and two of them
make this checklist unreadable while they are broken.
