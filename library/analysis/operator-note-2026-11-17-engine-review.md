# Operator note: 10h review + interventions (2026-11-17 ~14:30 UTC)

Scope: Telegram digests + logs for Nov 17 02:36-12:46 UTC, plus root-cause work afterwards.

## What was wrong

1. Village work engine stopped at 22:12 Nov 16. No task_assigned / task_completed /
   agent movement for ~16h. Root cause chain:
   - OpenRouter credits pause latched Nov 15 13:05 UTC (balance $2.26 < watchdog floor).
     By design the tank then runs on the free pool.
   - The dispatch rate-gate (`_credits_pause_blocks_assignment`, sim_parts/160:1396)
     holds ALL assignment whenever the free pool has no servable low route.
   - The workQueue head was fully undeliverable: 13 `supervise` items whose
     `assignedTo` pin was dropped by `_requeue_stale_work` on re-queue. Supervise
     cards REQUIRE the pin (`_assign_due_item` returns `_ASSIGN_WAIT` forever when
     the named supervisor is missing), so they silently blocked every pass.
     The 5 remaining items were parked-lane; nothing could ever assign.
2. Free pool flaps every ~2h (gemini-free + key3 cascade, 900s breaker cooldowns,
   groq fallback; Jev lane hourly failovers to z-ai/glm-5.3). Same endpoints answer
   fine when probed directly with the tank's keys, so the flap is quota/burst-driven,
   not auth or outage.
3. Telegram inbound bridge failed 7x between 05:01-09:46 (4x ENETUNREACH, 3x
   read timeout), recovered on its own. No alarm exists for consecutive
   getUpdates failures. Last inbound message remains Nov 16 19:53.
4. Alert digests were all "Total alerts: 0" all night while all of the above
   happened: `_INTERNAL_SIGNAL_ACTIONS` (425_scheduled_items.py) does not include
   queued_card_replan_capped, free_pool_fallback, model_circuit_broken,
   jev_model_auto_failover, provider_pivot, work_item_abandoned, failed_work_dropped.

## What was done (operator interventions, 13:27-14:05 UTC)

- Restarted think-tank.service twice (routine; state saves + hourly checkpoints).
  Cleared latched in-memory free-pool state; failed-card sweep resumed; refinement
  convened (cora/wrq-783) for the first time since Nov 16 evening.
- Parked the 13 unpinned supervise queue items (lane=parking-lot,
  parkedReason=operator) via a stopped-state blob edit. They were 9 days stale
  (task-23xxx era) and structurally unassignable. This un-jammed the queue head.
- The parked supervise cards and the full findings are reproducible from
  action_log + this note. Pre-edit blob copy: /tmp/opencode/kv_state_pre_surgery.json.

## Still open (needs operator decision or a tank fix)

A. MONEY/POLICY: credits pause is still latched. Top up OpenRouter and the gate
   auto-lifts (watchdog resets circuits on restore). Alternative: keep
   free-pool-only, but note the 2h flaps will keep stalling assignment under the
   current gate design.
B. BUG (root cause of this incident): `_requeue_stale_work` drops `assignedTo`
   (and other pin fields) when re-queuing. For supervise/review cards the pin is
   mandatory, so re-planning them makes them permanently unassignable. Fix: carry
   the pin (and wake semantics) through re-queue, or park instead of re-queue
   when the card type requires a pin.
C. BUG: capped QUEUED cards never age out (only the 14d aged retirement at
   10/pass bounds them). 280 stranded cards emit ~168 log rows/hour forever, and
   after each restart the reconcile pass serially writes ~280 action_log inserts
   over WAN, stalling the tick for minutes (tick_froze_at_phase events at
   save_archive_reconcile, 100-309s holds, 9x overnight).
D. ALERTING: add the missing actions to the digest's internal-signal list and a
   consecutive-getUpdates-failure alarm.
E. TIER POLICY: the 2h full tier refresh re-picked ALL bands back to openrouter
   slugs at 14:10, overwriting the 12:09 operator-era pivots to straitly while the
   openrouter account is paused. Paid legs currently point at a provider that
   refuses calls.
F. The single live queue item ([PROG-1D] code card, ~17d stale, teamless) should
   be promoted or parked by the owning director.

## Addendum (15:45 UTC same day) — deeper root cause after unpausing

The pause was lifted WITHOUT topping up OpenRouter: the watchdog floor
(OPENROUTER_CREDITS_WATCHDOG_FLOOR_USD) was lowered 5.00 -> 0.50 in .env.
Operator intent: OpenRouter is the JEV lane's account (JEV is exempt from the
provider pivot); village band calls pivot to straitly below the $5 pivot
floor. Balance $2.26 > $0.50 so the pause latch cleared and circuits reset.
Straitly holds $82.05 (verified via its credits API), Cheaper Inference has a
$0.50/month dialing cap; both are wired dialing providers (DIALING_PROVIDERS =
openrouter, straitly, cheaperinference).

After the unpause, py-spy showed why assignment STILL did not resume: the sim
tick thread loses most of its wall-clock to DB round-trips on the WAN link
(Postgres is Aiven over Tailscale):
- 100% of 150 stack samples inside `_peer_review_tick` (per-agent action_log
  scans; table is 618k rows / 435 MB under the 21-day LOG_RETENTION_DAYS
  policy in .env; indexes exist, so this is RTT count, not scan cost).
- Earlier batches: wedged inside `tier_value_ratios()` at the pool checkout
  (`ThreadedConnectionPool.getconn` lock) — a stalled connect (connect_timeout
  covers TCP only, NOT the DNS lookup) holds the pool lock and every other
  DB call in the process queues behind it. Same class of stall as the
  documented 2026-11-02 freezes.
- `story_tracking.refresh` (completion recovery) hogs 37/54 samples of another
  window; `_reconcile_queued_cards` twin-scans 280 queued cards across 1054
  tasks per sweep pass.
Net effect: sim passes stretch to 15-35 minutes, dispatch (the assignment
pick loop) is a tiny slice of each pass, and the village board barely moves
even with the pause off. The assignment machinery itself is sound — it is
starved of thread time, not gated.

Candidate fixes (tank-dev, NOT applied by operator):
- Move the DB onto the VM (the tank was designed for local sqlite
  think_tank.db; THINK_TANK_DB_URL over a WAN link turns every O(n) pass into
  minutes) or pin the Aiven host in /etc/hosts to remove DNS stalls.
- Bound the per-pass DB work: peer-review per-agent scans, story_tracking
  refresh, and the reconcile twin-scan all run every pass; cadence or
  incremental cursors would cut thread loss.
- `psycopg2.connect` under the pool lock: resolve DNS BEFORE taking the pool
  lock (or cache the resolved IP) so a resolver stall cannot wedge the pool.
- Reconsider 21d retention on action_log vs the 50k cap in code (policy and
  cap disagree; table is 435 MB).

Verified healthy after changes: pause latch off (settings credits_pause_state
active:false), 141 agents hydrated (py-spy agents_len:141), refinement running
(ada/wrq-786 comprehension 14:58), paid calls pivoting to straitly
(provider_pivot rows 15:39-15:41), tier refresh/price-watch cycling, telegram
bridge + webhook active, saves churning (kv_state updated_at fresh).

Still open from the morning review: items B (re-queue drops assignedTo pin),
C (capped queued cards never age out + startup reconcile log burst), D (digest
internal-signal blind spot + getUpdates alarm), F ([PROG-1D] stale code card
disposition), plus the queue-head supervise cards parked this afternoon (see
above).

## Addendum 2 (16:55 UTC same day) — dispatch fix applied, JEV chain fixed, catalogs audited

### Dispatch: the hot fix (operator-applied, 16:09 restart)
Reordered the capped-check BEFORE `_open_twin_task` inside
`_reconcile_queued_cards` (sim_parts/160__stale_work_step.py ~line 143).
250 of 280 queued cards are replan-capped and can never re-issue, so their
twin-scan was pure waste; skipping it cuts ~89% of the sweep cost (py-spy
pre-fix: 73% of sim-thread samples in the reconcile twin-scan, dispatch <1%).
Post-restart evidence: the sweep logged the capped cards it skipped
(queued_card_replan_capped, ~0.9s/card) and moved on; sim thread observed
progressing through tick phases (state_begin -> story refresh), no freeze.
NOT yet observed: a task_assigned. The next several passes decide whether
dispatch actually resumes; if it still does not, the next suspects are the
idle gate `think_tank_has_work` (160:1036) and the pool-lock connect stalls
(two `psycopg2.connect`-under-lock sightings post-restart, bounded by
connect_timeout=10s TCP-only).

### JEV decision chain (operator-set, user-directed)
settings.jev_model was a persisted auto-failover artifact (`z-ai/glm-5.3`,
hourly jev_model_auto_failover rows since 22:12 Nov 16; the failover loop at
020_backup_think_tank_db.py:711 replaces the setting whenever the current
slug fails a decisions-endpoint probe AND no chain entry verifies — the old
@freepool:* slugs kept failing that probe, which is why it kept clobbering).
Now set to the failover chain `typesafe/jev-1.14,typesafe/jev-1.13`:
- jev-1.14 verified live on /api/alpha/decisions (typed decisions wire
  format; chat/completions correctly 400s for both jev slugs — that is why
  they never appear in the OpenRouter /v1/models listing and hence in
  model_provider_catalog / model_tiers. Not a sync bug; upstream listing gap).
- The auto-failover verifier (`_verify_decision_model_works_sync`,
  180_director_second_opinion.py:811) probes the decisions endpoint, so
  jev-1.14 verifies healthy and the chain survives the ~2h tier refresh.
- Chain semantics (160_local_file_tool.py:859-919): jev-1.14 -> jev-1.13 ->
  Laya/Colab standby (if the ssh forward is up) -> free-pool low route last.
  When credits pause or the __jev__ lane cap (JEV_SPEND_CAP_USD=0.50) is
  hit, paid slugs drop automatically and Laya/free pool keep decisions
  alive. jev-router is NOT used (routes to deepseek; operator said so).

### Catalog sync audit (answers "are models regularly pulled?")
Yes, per provider: `refresh_model_tiers` (190_ingest_hf_open_llm_fallback)
runs ~2h and syncs all three providers via PROVIDER_CATALOGS into
model_provider_catalog (fresh 15:25-15:28 today: openrouter 470, straitly
167, cheaperinference 351 rows) + union into model_catalog (853 rows).
Gaps found and one fixed:
- model_benchmark_scores (1013 rows, artificial_analysis) last refreshed
  ~16h ago; HF Open LLM fallback backfill was silently SKIPPED because
  pyarrow was not installed ("[model-tiers] pyarrow not installed"). FIXED:
  installed pyarrow 26.0.1 into the venv; the next refresh backfills.
- Tier refresh policy still overwrites operator-era pivots back to
  openrouter slugs (note item E, unchanged), but runtime per-call pivots to
  straitly are working (provider_pivot rows 15:39-15:41).

### Environment
- OPENROUTER_CREDITS_WATCHDOG_FLOOR_USD=0.50 (in .env with a comment).
- Service restarts today: 13:27, 14:03, 14:49, 14:55, 16:09. All routine.
- Tooling note: never `pkill -f` with a pattern that matches your own
  command line; background py-spy with `setsid ... & disown` and poll for
  the output file instead.

## Addendum 3 (17:45 UTC same day) — the real last blocker + fixes B and D applied

### The directedTo wedge (root cause of the continued 19h stall)
After the twin-scan fix the passes got fast but still assigned nothing.
Supply was fine (79 idle on-duty agents, pause gate clear). The single
unparked queue card, [PROG-1D] (task-35118), carried
`assignedTo: null, directedTo: true` -- a mint FLAG where an agent id
belongs. `_assign_due_item` (170__park_idle_wanderers.py:742) returns
_ASSIGN_WAIT forever for a truthy directedTo with no resolvable pin, and
_ASSIGN_WAIT never increments attempts, so the ONLY due card blocked every
pass silently. Minted by the program re-issue path
(166__programs.py:343: `assignedTo: directors.get(teamId)` -> None for a
teamless piece, paired with `directedTo: True`).
FIXES (17:32 restart):
- Mint sites: `directedTo`/`directRoute` flags now `bool(assignee)` so a
  null-assignee card round-robins instead of waiting forever
  (166__programs.py x2, 130__start_breakdown.py).
- Assignment defense: both WAIT branches in 170__park_idle_wanderers only
  honor directedTo/directRoute when they are non-empty STRINGS (agent ids);
  a boolean flag classifies but can never strand.
RESULT: first task_assigned since Nov 16 22:12 fired at 17:34:59 UTC
(task-35118 [PROG-1D] assigned). The 19.5h dispatch stall is over.

### Fix B applied: re-queues carry pins (160__stale_work_step.py)
`_requeue_stale_work` and `_requeue_failed_work` now carry
assignedTo/facilitatorId/_reassignedTo through re-issues via
`_carry_pin()` (module-level, validates the agent still exists in the
roster, never 'player'), plus reviewOf/incident/directedTo/lane so
pin-required cards keep their wait/wake semantics. Previously the
re-queue dropped every pin, which is what turned 13 supervise cards
permanently unassignable and jammed the queue head (the morning incident).
`_requeue_paused_work` already carried the full set (2026-11-05 fix) and
is untouched.

### Fix D applied: digest blind spot + Telegram poll alarm
- `_INTERNAL_SIGNAL_ACTIONS` (425_scheduled_items.py) now includes
  queued_card_replan_capped, failed_work_dropped, failed_work_sm_replan,
  aged_card_retired, free_pool_fallback, model_circuit_broken,
  jev_model_auto_failover, provider_pivot, work_item_abandoned,
  credits_pause_started, telegram_poll_failed, telegram_poll_recovered.
  High-frequency actions render as one counted section each (6 rows max),
  so no flooding.
- getUpdates failures previously went to stdout ONLY (061_telegram_bridge
  `_log_telegram_send` logs sendMessage/sendPhoto exclusively) -- the Nov 17
  05:01-09:46 outage left zero action_log rows. Added transition logging:
  telegram_poll_failed on the first failure of a streak and every 10th
  after, telegram_poll_recovered when a streak ends
  (`_telegram_poll_outcome`). One row per incident, not per poll.

### JEV model alias (operator tip)
`typesafe/jev-latest` does NOT exist on this OpenRouter account -- both
/api/alpha/decisions ("does not exist") and chat/completions ("not a valid
model ID") reject it, and /v1/models still lists only typesafe/jev-router.
Not added to the chain; the pinned 1.14 -> 1.13 chain stands. If a latest
alias appears later, it would slot after 1.13 as the auto-float fallback.

### Restarts today (all deliberate, service clean each time)
13:27, 14:03, 14:49, 14:55, 16:09, 17:15 (fixes B+D), 17:32 (directedTo fix).

### Still open (unchanged)
- E is now ADDRESSED (19:00 UTC): settings.tier_provider='straitly' pins the
  band picks to straitly's catalog on the next ~2h refresh (pin mechanism in
  refresh_model_tiers, 190; empty/delete the row to release). The pick only
  understood monthly SPEND caps before, not balance floors.
- C is now ADDRESSED (19:00 UTC): capped queued cards park at a 7-day line
  (`_CAPPED_CARD_RETIRE_MS` in _reconcile_queued_cards, action
  capped_card_retired, same 10/pass budget as the aged line). All 249
  stranded cards retire within ~25 passes; the ~15k rows/day capped-log
  churn ends. queued_card_replan_capped remains for young (<7d) capped
  cards only.
- F: [PROG-1D] is now ASSIGNED (was the ask) -- disposition resolves itself
  when the card completes or fails.
- Pool-lock connect stalls (DNS-before-lock) remain worth fixing but are
  bounded by connect_timeout and no longer gate dispatch.

## Addendum 4 (19:10 UTC) — JEV on Cheaper Inference (operator tip, live-verified)

The operator asked whether JEV exists beyond OpenRouter. Answer, probed
against both provider listings:
- Cheaper Inference: YES -- typed decision models `jev-1.13.0` and
  `jev-1.14.0` (unprefixed slugs; chat/completions correctly 400s with "use
  the decision generation API instead"). Different wire shape than
  OpenRouter: POST {model, input, questions:[{name,type,instructions,
  choices:[{value,label}]}]} to https://api.cheaperinference.com/v1/decisions;
  answers return as an ARRAY; cost in billing.billed_cost_usd. Probe cost
  $0.000009 (vs OpenRouter $0.0000134). Separate wallet: $10 prepaid,
  $0.50/mo dialing cap.
- Straitly: only `jev-router` (the routing model the operator excluded).
WIRING (17:52 restart): chain entry prefix `@cheaperinference:` --
- `_call_cheaper_decision_sync` (160_local_file_tool): Cheaper wire format,
  response normalized to the OpenRouter {answers:{name:...}, usage.cost}
  shape so _jev_choice/tape/__jev__ accrual/breaker work unchanged.
- Chain loop routes the prefix; the chat-completions fallback is skipped
  for these slugs (their chat endpoint 400s by contract).
- `_verify_decision_model_works_sync` probes the Cheaper transport, so the
  auto-failover cannot clobber the chain over a foreign slug.
- Pause semantics: an OPENROUTER-ACCOUNT pause keeps the Cheaper slug
  (separate wallet); a __jev__ LANE-cap hit still drops it (caps every
  provider together). Below the free floor it still drops (paid).
CHAIN (settings.jev_model): `typesafe/jev-1.14,typesafe/jev-1.13,
@cheaperinference:jev-1.14.0` -> Laya standby -> free pool. END-TO-END
PROVEN: with the cheaper slug temporarily first, real `groom` decisions
taped ok=1 through it (choice=reject, conf 0.7, $0.000021); order restored.
This gives decisions a third leg that survives the Nov 16 failure mode
(OpenRouter's jev slugs vanishing from availability).

## Addendum 5 (19:45 UTC) — price-driven JEV provider choice (operator direction)

"Let it determine based on price between Cheaper Inference and OpenRouter":
`_decision_model_chain` now stable-sorts the PAID slugs by measured per-call
cost (trailing 7d decision_tape AVG per slug, >=3 decided samples, 5-min
cache: `_jev_slug_prices` / `_price_order_decision_chain`, 160). Unknown-
priced slugs sort after known ones; Laya + free pool stay pinned after the
paid group. Tape averages (not catalog quotes) because both providers bill
the same workload shape -- the tape's model column records the answering
slug verbatim, so OpenRouter and @cheaperinference: entries compare
directly. Catalog per-million pricing is NOT comparable (and OpenRouter
doesn't list the typed slugs anyway).
Post-restart reality check: measured order is jev-1.14 ($11.8u/call, 5
samples) -> jev-1.13 ($29.4u/call, 480 samples) -> @cheaperinference:1.14.0
(1 sample, unknown -> last), then Laya/free pool. The my-probe guess that
Cheaper is cheaper was wrong at real workload size; the mechanism now
measures instead of guessing. Failover semantics unchanged: first healthy
slug serves, breaker skips cooldowns, pause/lane-cap filters run after the
sort. The settings row keeps the operator-written list; ordering is
runtime-only so the auto-failover cannot clobber it.
