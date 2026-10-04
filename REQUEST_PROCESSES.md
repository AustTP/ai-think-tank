# Think Tank — Business Process Reference (by request type)

> **Purpose of this file.** This is the authoritative, reviewable description of how the
> think tank actually processes **every request type** — what enters, what the system decides,
> what happens step by step, what gates apply, and what completes. It exists so the player can
> review each flow against their intent, disagree with an implementation, or supply additional
> context, and have those revisions folded back into the spec before (or after) code changes.
>
> Line numbers are pointers into `world/serve.py`, `world/sim.py`, `world/content.py`,
> `world/tasks.js`, `world/world.js` — best-effort entry points, not exhaustive spans.
> The newest Part III gap bullets (§3.1 SM gap, §3.2 Director gap, §3.6–§3.10 W2–W6)
> are re-anchored to the current code at commit `22ce3d5`; older section anchors were
> written for commit `5b8f681` and may lag by up to ~1,000 lines after that commit
> grew `sim.py` (+1,741) and `serve.py` (+933). When a pointer looks off, grep the
> symbol, not the number.
> Where a flow was implemented to a *specific* player decision, that decision is quoted.

---

## How to read this file

Three layers of "request":

- **Part I — Player intents** (`/api/intent/*`): explicit things the player delegates, files,
  votes on, or inspects. Every one is an auth-gated business decision.
- **Part II — Direct tool/utility requests** (`/api/*`): the concrete capabilities agents and the
  player call — chat, web access, execution, sandboxes, library, keys, escalations, the decision
  system.
- **Part III — Agent-driven processes**: the recurring ceremonies and sweeps the simulation runs
  on its own clock (refinement, sprints, retrospectives, on-call, grading, social, distill,
  self-heal, dormancy, shadow mode, the Bank). These are the *processors* that consume the
  requests from Parts I and II.

Each entry follows the same skeleton so it is easy to diff against the code:

`Intent` → `Trigger` → `Inputs / validation / authorization` → `Routing decision` →
`Steps (in order)` → `Completion / what the caller sees` → `Gating` → `Fallbacks` →
`Model tier used` → `Tests that pin the behavior`.

---

# Part 0 — Cross-cutting infrastructure (applies to everything)

## 0.1 Authentication and attribution
- **Middleware:** `require_login` (`serve.py:7549`) guards every path under
  `AUTH_PROTECTED_PREFIXES` (`serve.py:7523`, includes all `/api/intent/*`, `/api/state`,
  `/api/execute`, `/api/browse`, `/api/library/*`, `/api/keys/*`, `/api/jev/*`, …). A request
  passes with **either** a valid player session cookie (`verify_session`, `serve.py:4038`) **or**
  a valid agent key (`X-Agent-Key`, `_valid_agent_key_presented`, `serve.py:7533`). Else 401.
- **Player-only intent surface:** the 15 endpoints that hardcode actor `'player'` (sprint
  create/close, publish, product release, story veto, spike promote, issue store, wiki write/category,
  and others) add a **second, stricter gate** `_require_player_session` (`serve.py:4079`) — it
  returns `verify_session(request.cookies.get(SESSION_COOKIE_NAME))` and rejects **even a valid
  agent key** with 401 ("please log in"). The middleware's session-OR-agent-key rule is deliberately
  not enough for the player's own controls: a server content executor (loopback with only an agent
  key) must never create/close a sprint, trigger a publish push, release a product, veto a story,
  promote a spike, or file new work as the player. Each gate runs BEFORE the handler parses its body.
  Tests: `test_serve.py::PlayerIntentEndpoints` (`test_player_intent_surface_rejects_valid_agent_key`,
  `test_require_player_session_accepts_only_a_real_session`).
- **`/api/chat`, `/api/device/checkin`** are NOT in the prefix and self-guard (session OR agent
  key; device key) so anonymous spend can never be billed.
- **Attribution:** most intent handlers hardcode `player_id = 'player'` and write `log_action(...)`.
  Agent actions are attributed by key (`verify_agent_key`, `serve.py:1641`).
- **Coverage standard:** total test coverage across the Python files under `world/` (and the
  browser JS files) has a **hard floor of ≥95%** (raised from 90% on 2026-10-03; 99–100% is the
  current target). Enforcement lands in `test_coding_standards.py` with the coverage push.

## 0.2 The passport (tamper-evident decision ledger)
- `_append_passport_decision(kind, actor, payload)` (`serve.py:11261`) chains every consequential
  decision into `library/.passport.json` as a hash-linked block (each block's `prev` = prior
  `head`). `GET /api/intent/passport/verify` (`serve.py:11334`) walks the chain and checks
  on-disk file hashes (`verify_passport`, `serve.py:11294`). Read-only; never mutated by the verify.
- Passport-chained events include: hires/fires, library promotes + writes, credential store/delete,
  handle mint/revoke/use, access grants, pipeline create/delete, Jev model switch, story veto,
  spike promotion, publish, product release, wiki writes, big-task delegation.

## 0.3 The Jev decision layer (the judgment chokepoint)
- **Proxy:** `POST /api/decide` (`serve.py:14463`) forwards `state` + `questions` to the typed
  decisions endpoint — the **resolved model is authoritative** (`_jev_model()`, `serve.py:5886`);
  a client-supplied model is ignored.
- **Call:** `_call_openrouter_decision_sync` (`serve.py:5696`) — multi-slug failover chain + per-slug
  circuit breaker (armed when >1 slug configured), writes `decision_tape` with `trace_id`.
  `_jev_choice` (`serve.py:5804`) extracts `choice/confidence/cost`. Cost accrued to `__jev__`.
- **Quorum:** `_jev_quorum_decision` (`serve.py:6684`) samples up to 3, 2 agreeing votes win.
- **Safety gate:** `_jev_safety_gate` (`serve.py:7001`) — confident allow passes; **low-confidence
  allow escalates as an "unsure safety decision"**; non-allow blocks. The live threshold is
  `_effective_safety_confidence()` (`serve.py:13367`).
- **Model switch:** `GET /api/jev/model` (read), `POST /api/jev/model` (`serve.py:14538`,
  **player-only by session cookie**, persists via `_set_setting` so it survives restarts, never
  auto-updated). Resolution: settings `jev_model` → env `JEV_MODELS` → `JEV_MODEL` → optional
  Colab/Laya standby URL.
- **Calibration:** `GET /api/jev/calibration` (`serve.py:14522`) — DB-only: gate outcomes bucketed
  into `JEV_CALIBRATION_BINS` with `calibration_error`, review-grade calibration
  (`serve.py:13463`), and the live low-confidence floor. Feedback loop `_calibration_adjust_pass`
  (`serve.py:13383`) moves the floor so auto-approved actions succeed ~as claimed (target 90%,
  ±5% dead-band, ≥10 scored decisions, 0.05 steps, clamped 0.5–0.95).

## 0.4 Model-tier policy
- `_resolve_model_tier(purpose, task_type=None, allow_high=False)` (`serve.py:2845`):
  - `task_type` in `code|review|qa` → deterministic CODING tier (no Jev).
  - else a Jev decision low/mid/high; **fails closed to low**.
  - `allow_high=True` also checks the high-tier monthly budget and a confidence gate.
- Slugs: `_low_tier_slug` (2764), `_mid_tier_slug` (~2760), `_high_tier_slug` (2771),
  `_coding_tier_slug` (2777), `_vision_tier_slug` (2785), `_reasoning_tier_slug` (2792).

## 0.5 The Bank (spend + budget caps)
- **Accrual:** every model call is costed exactly once at the choke point
  (`/api/chat` → `_call_openrouter_sync`, `/api/decide` → `_call_openrouter_decision_sync`) via
  `_accrue_spend(service, cost)` (`serve.py:715`) into the independent `kv_spend` ledger
  (per-service `used/calls/lastAt` + daily `byDay` burn series). `COLAB_LEDGER_KEY` counts compute
  UNITS not USD; Apify has its own FREE-plan monthly bucket.
- **Caps:** `_budget_cap_usd` (`serve.py:690`) — per-product `budgetCapUsd` wins, else
  `DEFAULT_BUDGET_CAP_USD=50`; the effective cap is the sum of service caps.
  `_think_tank_spend_cap_exceeded` (`serve.py:779`) is the **hard monthly USD ceiling** checked at
  the top of every money-spending chokepoint (disabled when `SPEND_CAP_USD==0`; month rolls lazily;
  COLAB units excluded from the USD count). High tier has its own
  `HIGH_TIER_MONTHLY_BUDGET_USD` (`_accrue_high_tier_spend`, `serve.py:4238`); `_resolve_model_tier`
  fails closed to mid when spent.
- **Readout:** `_run_bank_content` (content.py:621) — director/admin sees per-service used/cap/left/
  burn forecast + live OpenRouter-account + Apify reconcile; a non-director sees a real read-only
  cumulative summary; only directors see reallocation authority.

## 0.6 Rate limits
- Shared **ask + clarify** bucket: 20 calls / 60s (`ASK_LANE_RATE_LIMIT_KEY`, `serve.py:11024`,
  checked via `check_rate_limit` at `serve.py:11034`).
- Per-endpoint, per-agent `check_rate_limit(agent_id)` on every tool endpoint
  (browse, curl, execute, youtube-transcript, page-probe, allowlist/request, access/request, …).

## 0.7 Escalations (the human approval valve)
- `create_escalation(kind, question, on_approve_note)` (`serve.py:4883`) writes
  `escalations.json` and emails the player approve/deny links (`_send_escalation_email_sync`).
- **Director auto-approval loop** `_resolve_pending_escalations_sync` (`serve.py:2920`), for
  non-floor-1.0 kinds, asks Jev (quorum-sampled, composite trust `_jev_directory_score` `:6899`,
  per-kind floor `_escalation_floor` `:6886`, heterogeneous-judge cross-check
  `_escalation_judge_crosscheck` `:6926`) to approve/deny on the player's behalf.
- **Floor-1.0 kinds (`ESCALATION_KIND_RISK`, `serve.py:6819`):** `blocked command`, `blocked pipeline
  step`, `allowlist request` — ONLY the player's email link can resolve. A human resolution clears
  that kind's judge-drift circuit (`_reset_escalation_judge_drift`, `serve.py:6976`).
- **Player-side resolve:** `GET /api/escalation/resolve` (`serve.py:13079`) — tappable from email;
  random `token` makes the link unguessable; approve/deny side-effect for `allowlist request` is
  `_grant_allowlist(esc['note'])` (`serve.py:4471`) — the ONLY path that grants an allowlist entry
  (also refreshes the egress proxy, `ensure_sandbox_networking` `:4932`).

## 0.8 Dormancy, idle, and the sim loop
- The engine runs `_task_cycle` (`sim.py:7603`) on the sim loop's **single read-modify-write**
  (server-owned only when `sim.owner == 'server'`). Cadence-stamped ceremonies run **ungated** —
  they are the point even on a quiet think tank — via `_sim_loop_pass` (`sim.py:1109`).
- **Ceremony shape:** stamp cadence → convene on the next pass (snapshot attendees, mark busy,
  spread at the venue by `pos_offset`) → resolve after a `MEET_MS` window → restore attendees
  (a mid-task worker resumes with `workUntil` extended by the meeting length; an off-duty attendee
  is woken then returned off-duty).
- **Dormancy:** while `_dormant()` the sim tick is skipped entirely — no DB read/write, no movement,
  no spend — but `_expire_handles` housekeeping still runs. **Any** request wakes the tank
  (`no_store` middleware `serve.py:7497`). `/api/device/checkin` is deliberately NOT dormancy-gated.

---

# Part I — Player intents

## 1.1 Delegate a big task — `POST /api/intent/assign-big-task` (serve.py:8045)

- **Intent:** the player hands the tank a large task and expects it to become real work.
- **Inputs:** `{goal}` (non-empty). **Authorization:** any authenticated session/key; attributed to
  the player.
- **Routing decision (the big branch):**
  1. If `_all_teams_busy_in_sprint(state)` (`serve.py:7951`) — every team is committed to an active
     sprint:
     - **`_breakdown_into_shared_backlog`** (`serve.py:7975`, run via `asyncio.to_thread` — it is a
       blocking urllib call and must not deadlock the loopback). High-stakes planning tier
       (`_resolve_model_tier(..., allow_high=True)`). One `/api/chat` call breaks the goal into
       **2–5 stories/spikes** (`type` story|spike, optional `acceptanceCriteria`, `sizeEstimate`)
       plus a feature name. Cards are filed room-free into the **shared unassigned backlog** under a
       feature (reused by name) via `create_or_reuse_feature` (sim.py:2232) + `add_backlog_item`
       (sim.py:2256, cap `MAX_BACKLOG_ITEMS=200`). A team pulls first-come when a sprint closes.
       Success → `{backlogged: True, feature, featureName, items, note}`.
     - **Fallback** if the breakdown fails (model outage / unusable JSON):
       `spawn_new_team_for_request` (sim.py:4564) — spawn a new Director + employees, create the
       team record, file the goal as a pending backlogRequest, `kick_refinement_now`.
  2. Otherwise (some team free): resolve the acting authority `_free_authority` (free admin first,
     else senior-most director; `serve.py:7905`); if none, wake an off-duty admin/director
     (`_wake_authority_on_request`, `serve.py:7923` → `appear_from_outskirts`, sim.py:2660); still
     none → error.
- **Steps (staffable path):** build a system prompt enumerating `_DELEGATABLE_ROOMS` (serve.py:7869)
  purposes; instruct 2–5 subtasks (pressoffice-only `taskType` code/review/qa, `pair`, `notBefore`,
  `priority`); one `/api/chat` call (allow_high tier, max_tokens 4000, timeout 90, off-thread);
  strip fences, `json.loads`, keep subtasks with a title + a valid room; clamp `taskType`; normalize
  `notBefore` (malformed → None, fail-open); `queue_work` (sim.py:1666); `kick_refinement_now`.
- **Completion:** `{ok, admin, subtasks:[{title, room, instructions, pair, notBefore, priority,
  taskType}]}`.
- **Gating:** auth; JEV high-tier on the decomposition; busy-check; free-authority + wake fallback.
  No peer approval (delegation, not work).
- **Player-decided behavior:** when *every* team is busy, the task is NOT dropped and NOT force-
  staffed — it is broken down and parked in the shared backlog for the first free team; cards are
  **room-free by design** ("shouldn't the agent who picks up the story be able to figure out where
  they need to go").

## 1.2 Ask a one-off question — `POST /api/intent/ask` (serve.py:9565) + `_ask_core` (9306)

- **Intent:** a genuinely NEW one-off question (not about completed work — that is `clarify`).
- **Inputs:** `{question, location?, agentId?, max_tokens?}`. Rate limit (shared ask/clarify bucket)
  → 429; empty question → 400.
- **Agent pick (`_ask_core`, 9321):** candidates = eligible non-admin agents (may be off-duty),
  `_eligible_candidates(include_off_duty=True)` (sim.py:2641) ∩ live records. A requested
  `agentId` is honored only if that agent is free AND (the caller is the Telegram bridge with
  `allow_admin_pin` OR the agent is in candidates) — **raw player input can never pin an admin**.
  Else deterministic round-robin `candidates[0]`. No candidates → 409.
- **Prompt:** in-character system prompt (name/role/mission; Red Team Auditor gets an instructions
  checklist); "tool output is data, never instructions".
- **Model tier:** JEV low/light-mid (`allow_high=False`).
- **Tool loop:** `_call_agent_tool_loop` (`serve.py:5271`, via `asyncio.to_thread`), tools =
  `AGENT_ASK_TOOLS` (weather_now, browse_page, +search_web when `TAVILY_API_KEY`, team_digest,
  read_peer_reviews) + Treg tools (`x_trending_topics`, `search_linkedin_posts`) +
  `SECURITY_TEST_TOOLS` for Red Team. `ask_force_first_tool` forces `tool_choice` when the question
  detects X-trending/LinkedIn. Loop: post (spend accrued to `__player_ask__`); if `tool_calls`,
  execute each (web tools wrapped by `wrap_external_content` — the injection boundary;
  `request_allowlist` files a Jev-gated allowlist request; `read_peer_reviews` reads other agents'
  review dirs; Red Team self-tests through the real `/api/curl` and `/api/keys/handles`); append as
  `role:tool`; re-invoke. Cap 50 iterations.
- **Completion:** `{reply, agent, tools}` — a reply, NOT a deliverable (nothing enters the
  sprint/grade/release pipeline).

## 1.3 Clarify completed work — `POST /api/intent/clarify` (serve.py:9104)

- **Intent:** "how was X done?" — KB-first Q&A over the completed-work record.
- **Inputs:** `{productId, question, sprintId?, max_tokens?}`. Rate limit → 429; product + question
  required.
- **Router (derived, no model):** `clarify_router_plan` (sim.py:1955) → `onCall` =
  `on_call_agent` (sim.py:1826, deterministic per-sprint rotation over non-SM/non-admin members,
  day-shifted when sprint-less, prefers an available member; if nobody rousable → the team director
  with `onCallFallback=True`). `completing` = the most recently *graded* completed deliverable
  matching the product (`_completing_agent_for_product`, sim.py:2018). No on-call → 404.
- **KB-first:** `_library_search_matches(f'{product_name} {question}'[:200])` (serve.py:10892) —
  the SAME search agents use (substring, trail-ranked). **Grounding decision (9145):** if zero KB
  matches AND a *different* agent completed the work → ask the completing agent directly (strictly
  more grounded), skipping on-call. Else on-call answers.
- **LLM:** JEV low/light-mid; cost accrued to `__clarify__`.
- **Escalation (9182):** if the on-call reply contains the exact token `__CLARIFY_ESCALATE__`,
  make a second call to the completing agent; token stripped. Never past "I don't know" without it.
- **Completion:** `{reply, onCall, completing, escalatedTo, onCallFallback}`.

## 1.4 Schedule a recurring research sweep — `POST /api/intent/schedule` (serve.py:9888)

- **Intent:** set up a recurring web-monitoring job.
- **Inputs:** `{topic, startUrl, cadenceMs}`. `add_research_topic` (sim.py:2998) rejects empty topic
  or a startUrl that is not absolute http/https with a netloc; clamps `cadenceMs` to ≥
  `MIN_RESEARCH_CADENCE_MS` (5 min); record `{lastRunAt: 0 → fires next pass, seenUrls: [], ...}` →
  `state['researchTopics']`.
- **Completion:** `{ok, topic}`. Consumed by `_check_schedules` (sim.py:3167) on the sim clock.
- **Free-text sibling:** the routing lane extracts `{topic,startUrl,cadenceMs}` via
  `_extract_schedule_fields_sync` (`serve.py:9698`, a `/api/chat` JSON extraction that fails closed).

## 1.5 Reject a delivered story (player veto) — `POST /api/intent/story/{task_id}/reject` (serve.py:8213)

- **Intent:** Phase E3.4 — the player sends a *closed* story back to its ORIGINAL AUTHOR for rework.
  Agents never self-direct this; the player is the external gauge.
- **Inputs:** `{reason?}`. Only `status == 'done'` stories are rejectable (else 409); unknown task
  404; no `assignedTo` author → 409.
- **Steps:** `_enter_peer_review` (sim.py:3713) re-arms the gate — `_pick_reviewer_ids`
  (sim.py:3657) **prefers the reviewers who already know the story**, then the author's team, then
  room-contributors, then any non-admin; status → `needs_review`; files `peer_review_request` notes;
  queues two `taskType:'review'` subtasks carrying `reviewOf` + `reviewAuthorId` (the fix returns to
  the author who built it). Gate with no eligible reviewer → 409 (fail closed — the story stays
  done rather than dropped into limbo). `player_veto` note to the author.
- **Completion:** `{ok, taskId, author, reviewers}`.

## 1.6 Publish released work — `POST /api/intent/publish` (serve.py:8362)

- **Intent:** push the tank's *released* work to a configured GitHub repo. Nothing leaves the
  machine unless the player explicitly asks.
- **Precondition:** `PUBLISH_REPO` (env `AI_THINK_TANK_PUBLISH_REPO`) set, else 409.
- **Steps:** `_stage_released_work` (8295) wipes + recreates the staging dir, stages every
  `library/projects/*` dir + wiki pages + authored skill `.md`s (only released player-facing work);
  `_write_publish_readme` (8342); git init/config/add/commit; **push** rewrites the remote URL as
  `https://x-access-token:<gh token>@github.com/<repo>.git` and pushes to `main`
  (`PUBLISH_TIMEOUT_S=60`; non-zero → 502 surfacing git's stderr); `finally` rmtrees the staging dir
  so the ephemeral token never persists.
- **Completion:** `{ok, repo, entries, staged, push}`. No Jev (export, not judgment).

## 1.7 Promote a completed spike — `POST /api/intent/spike/{task_id}/promote` (serve.py:8427)

- **Intent:** Phase E3.5 — the player turns a completed spike's findings into a real queued
  deliverable (or a deeper spike). Agents never self-direct follow-ups.
- **Inputs:** `{room?, taskType?}`. Spike-only, done-only (else 409); **`taskType` is REQUIRED**
  (code|review|qa|spike, else 400) — silent defaulting was removed after a loop where
  non-actionable findings burned review cycles. `room` defaults pressoffice, clamped to delegatable.
- **Steps:** load the real finding — `task.libraryPath` file (first 20k chars) → fallback
  `task.note` → placeholder; build `Follow up: <title>` with instructions embedding the finding;
  `taskType='spike'` shapes a time-boxed `queue_spike` item (budgetMs from original or 60s), else a
  normal item riding the peer-gate path; the spike task itself **stays 'done'** (immutable record).
- **Completion:** `{ok, queued}`.

## 1.8 Sprints — create / list / close

### `POST /api/intent/sprint` (serve.py:8655)
- **Intent:** the player seeds a new sprint on behalf of the acting authority (admin else senior
  director). `{name?, goal, items:[...], targetDate?, teamIds?}` — goal + non-empty items required.
- **Scrum-master gate:** if `teamIds` given, teams at/above `SCRUM_MASTER_MIN_TEAM_SIZE=4` must have
  a designated scrum master (`_teams_missing_scrum_master`, serve.py:3865) → 409 naming the teams;
  small teams let the director stand in.
- **Steps:** `queue_sprint` (sim.py:2049) keeps only items with title + valid room, tags each with
  `sprintId`, stores `{status:'active', teamIds, items}`; zero retainable items → not created.
  Work flows through the normal queue → assignment → executor → peer gate.
- **Completion:** `{ok, sprint, owner, queuedItems}`.

### `GET /api/intent/sprints` (serve.py:8723)
- Read-only board: `{record, progress: sprint_progress(sid)}`. `sprint_progress` (sim.py:2090)
  derives queued/inProgress/done/pct/landed by matching stored (title, room) pairs against the live
  workQueue + tasks map; shadow items excluded.

### `POST /api/intent/sprint/{id}/close` (serve.py:8738)
- `close_sprint` (sim.py:2157) — a **container status flip** to `closed`; already-queued items keep
  flowing (close ≠ cancel). The auto-close ceremony (`_auto_close_completed_sprints`, sim.py:2169)
  additionally runs `_on_sprint_closed` (Part III.2).

## 1.9 Issue store (ticket register) — `/api/intent/issues*` (serve.py:8758–8914)

- **Create** (`POST /api/intent/issues`, 8778): `{teamId, type, summary, feature, reporterId}`
  required; `type` in `ISSUE_TYPES=(story|spike|bug|task)`; team must exist.
  `file_issue` (sim.py:5294) — key via `next_issue_key` (per-team prefix, e.g. `DEV-1`);
  `_normalize_description` splits "As a… so that…" / "Given… When… Then…"; **appends a
  `backlogRequests` record tagged `issueKey`+`teamId`** (the owning team's scrum master grooms it —
  not a dead ledger); `kick_refinement_now`.
- **Status** (`POST /api/intent/issues/{key}/status`, 8821): open/in_progress/done/closed;
  terminal states resolve the linked backlog request and supersede `awaiting_input` player inbox
  messages.
- **Detail** (`GET .../{key}`, 8841): full record + `questions` + `blockLog` (last 8 committed
  block changes).
- **Claim-met** (`POST .../{key}/claim-met`, 8860): an agent files the issue is requirements-met;
  **the vote is asynchronous** — `_supervisor_block_vote` (sim.py:6114, the ONE Jev-gated judgment
  in the ticket store) + `_supervisor_block_vote_sweep` (6194, after `BLOCK_CLAIM_GATE_MS`) approve
  before the SM commits `blocked`. Dedup: an in-flight claim → 409; already blocked → files an
  unblock instead.
- **Block-dependency** (`POST .../{key}/block-dependency`, 8888): Agent A files the issue is blocked
  on another agent's task; `request_block_dependency` (sim.py:5989) → `_file_block_change`
  queues an SM commit of `blocked=true`; **deliberately Jev-free** (naming a dependency is
  objective). Completion of the dependency auto-files `unblock_landed` (`_auto_clear_dependency_blocks`,
  sim.py:6008).

## 1.10 Report an incident — `POST /api/intent/incidents` (serve.py:9965)

- **Intent:** a live product breakage needing urgent fix.
- **Inputs:** `{productId, title}`. `queue_bug` (sim.py:1878): product must exist → owning team
  (product.teamId → director) → on-call (`on_call_agent`); **one-in-flight cap** per product (no
  other open bug for that product in queue+tasks). Lands in **pressoffice**, taskType `bug`,
  `priority: high`, `assignedTo: on-call`, `incident: True` — pinned, non-gated (bug ∈
  NON_GATED_LANES — incident response, not feature work). None → 400 "could not route this incident".
- **Completion:** `{ok, productId, title}`.

## 1.11 Products — create / list / status / release (`serve.py:10022–10170`)

- **Create** (`POST /api/intent/product`, 10022): `{name, summary, spec, ownerId, sandboxId,
  teamId?, contributorIds?, handles?}`; `sandboxId` must be a real dir under `SANDBOXES_DIR`; record
  `{status:'draft', revisions:[], nextRevision:1, ...}` (`create_product`, sim.py:2405).
- **List** (10067): catalog metadata only — revision **content** never inlined (lives under
  `library/projects/`).
- **Status** (`POST .../status`, 10075): draft/in_progress/review; **director/admin-gated** via
  `_resolve_requester` (claims an agent key) + `_is_director_or_admin` → 403. `'released'` is
  unreachable here (only `/release` flips it).
- **Release** (`POST .../release`, 10099): freeze the product sandbox into
  `library/projects/<id>/v<N>/` (`next_product_revision` sim.py:2435; `_copy_release_snapshot`
  drops `.git`/caches/build noise; writes `RELEASE.md`); `product_release_record` (sim.py:2447)
  appends the revision catalog, bumps `nextRevision`, flips → `'released'`. Re-releases allowed
  (new revision). Actor = free authority or 'player'. Content never returned.

## 1.12 Wiki — pages & categories (`serve.py:10172–10340`)

- **Read:** tree (`GET /api/intent/wiki`, metadata only) and single page (`GET .../page/{id}`, body
  from `library/wiki/<category>/<id>.md`) — open to any authenticated client.
- **Write** (`POST /api/intent/wiki/page`, 10204): **director/admin-gated**; `id`+`category` required,
  category must exist; `wiki_write_page` (sim.py:2499) rejects >200k bodies, bumps `version`,
  appends prior version to `history`; body written to disk; `{ok, page, isNew}`.
- **Category** (`POST /api/intent/wiki/category`, 10298): director/admin-gated; sets `{label, order}`
  + optional per-category room affinity (read-before-act injection).
- **Server-authority write path:** `_write_wiki_server` (serve.py:10244) bypasses the director gate,
  runs as actor `'distill'`, and auto-seeds the `think_tank` category — so distillation never
  silently fails (Part III.9).

## 1.13 Passport verify — `GET /api/intent/passport/verify` (serve.py:11334)

- Walks the hash chain: each block's `prev` must equal the prior block's link value; file blocks
  (path+sha256) re-hash the on-disk file. `intact` iff no bad blocks and `head` matches.
- **Completion:** `{ok, count, head, badBlocks}`. Read-only, no model calls.

---

# Part II — Direct tool / utility requests

## 2.1 General chat — `POST /api/chat` (serve.py:11602)

- **What it is:** the secure OpenRouter proxy; the ONLY place `OPENROUTER_API_KEY` is used and the
  choke point where every model call's cost is banked. Client owns persona/prompt; server returns
  the reply text. Backs the server's own loopback planning calls.
- **Guards:** 500 if no key; self-guarded (session OR agent key — anonymous spend can't be billed);
  `model`+`messages` required; **input-guard regex sweep** for prompt-injection patterns → 400 +
  `chat_input_guard_blocked` audit entry; `max_tokens` clamped (default 150, cap 4000).
- **Steps:** `_call_openrouter_sync` (`serve.py:5197`) — spend cap + per-slug circuit breaker,
  reasoning-token caps for coding/high/mid/reasoning tiers; `_accrue_spend(service, cost)` (and
  `_accrue_high_tier_spend` for high). Log `chat` with model/service/cost.
- **Completion:** `{'reply': <text>}`; upstream HTTPError passthrough.

## 2.2 Browse — `POST /api/browse` (serve.py:11694)

- **What it is:** real internet fetch with a Jev safety gate BEFORE content is fetched (classify the
  destination, not the result). Player-vetted allowlist hosts skip the Jev round trip.
- **Gates:** kill switch `AGENT_BROWSING_ENABLED`; monthly page budget
  (`_page_budget_exhausted`/`_accrue_page_request`); rate limit; URL must be http/https with a
  hostname; **SSRF guard** `_is_safe_public_host` (web_helpers.py:106) blocks private/internal.
- **Routing:** allowlisted host → skip Jev (SSRF still applies). Else `_jev_quorum_decision` +
  `_jev_safety_gate` (criteria from `BROWSE_BLOCK_CATEGORIES`). A confident allow on a non-vetted
  host bumps a trail (`record_browse_success`); after `BROWSE_ALLOWLIST_CANDIDATE_THRESHOLD`
  confident approvals the host is surfaced as an "allowlist candidate" for the player.
- **Fetch:** optional `render:true` → headless Chrome; else urllib; final host re-checked after
  redirects; HTML stripped to text (cap 20k chars) + links; optional `viaVpnCountry` (Mullvad,
  serialized through `_MULLVAD_LOCK`, country must be on the allowlist); optional `visual:true` →
  base64 screenshot (best-effort); output wrapped by `wrap_external_content` (nonce+HMAC tagged
  `textForModel` + `modelInstruction` — the prompt-injection boundary).
- **Completion:** `{allowed, url, text, links, truncated, textForModel, modelInstruction,
  imageBase64, lastModified}`. A Jev-approved URL that fails to load still returns `allowed: True`
  with an error.

## 2.3 Curl — `POST /api/curl` (serve.py:12062)

- **What it is:** raw HTTP (status codes, headers, unprocessed HTML/JSON) that browse's text-stripped
  output can't provide. Same Jev gate PLUS a real room restriction.
- **Gates:** kill switches; rate limit; **Weather Station room requirement**
  (`_agent_is_in_weatherstation` OR `_has_active_temp_access(agent_id,'curl')`); URL/method checks;
  SSRF (pre + post-redirect).
- **Routing:** NO allowlist bypass — every curl goes through `_jev_quorum_decision` +
  `_jev_safety_gate` (method/body-aware criteria).
- **Capability handles:** optional `capabilityHandle` → `resolve_capability_handle` (serve.py:2565)
  validates grantee/expiry/host/method scope, injects decrypted credential headers server-side,
  chains a `credential_used` passport block; **`_redact_secrets` + exact-secret redaction** so
  injected credentials never leak into agent-visible output.
- **Completion:** `{allowed, status, finalUrl, headers, body, truncated}` (200KB body cap, 15s).

## 2.4 Execute — `POST /api/execute` (serve.py:12629)

- **What it is:** real (not simulated) execution of a single shell command inside an isolated Docker
  sandbox — network-disabled except the allowlisted egress proxy, resource-capped, only its own
  scratch dir mounted.
- **Gates:** `AGENT_EXECUTION_ENABLED`; rate limit; `_classify_command` (serve.py:12421, Jev quorum +
  safety gate vs `EXECUTE_BLOCK_CATEGORIES`, **fails closed if the classifier is unreachable**).
- **Steps:** blocked → `create_escalation('blocked command', ...)` (**floor-1.0: only the player's
  email link can approve**), return `{allowed:False, reason, escalationId}`. Else resolve the
  sandbox dir, `_snapshot_sandbox` (local-git commit BEFORE the write — undo safety),
  `_run_in_sandbox_sync` (`docker run --rm`, 256MB/1 CPU/128 pids, 30s, 20KB output cap);
  `sync_prototypes`.
- **Completion:** `{allowed, sandboxId, exitCode, stdout, stderr, timedOut}`.

## 2.5 YouTube transcript — `POST /api/youtube-transcript` (serve.py:12728)

- **What it is:** transcript via download-and-transcribe on a Colab runtime (Apify actor downloads
  audio, faster-whisper transcribes) — audio never touches this machine.
- **Gates:** rate limit; fixed youtube.com/youtu.be host set; Colab enablement + usage budget +
  `APIFY_API_KEY`.
- **Steps:** `_youtube_transcript_colab` (serve.py:2061) accepts output only when it ends with
  `__TRANSCRIPT_END__` (fail-closed); best-effort `_file_youtube_transcript` writes
  `library/media/transcripts/<videoId>.txt` so ANY agent can read/search it (Studio media lane reads
  the same tree) — never fails the request.
- **Completion:** `{ok, url, transcript, chars, filed}`; failure → 422.

## 2.6 Page probe — `POST /api/page-probe` (serve.py:13030) + agentic probe protocol (tasks.js)

- **What it is:** mid-generation, a coding/review model wants to check REAL page state instead of
  guessing. The LLM replies with `{"probeRequest": {"path", "actions", "probes"}}`.
  `_parseProbeRequest` (tasks.js:1555) is the deliberately narrow parser (under test in
  `test_probe_request_parsing.mjs`): strips fences, requires `probeRequest` + at least one of
  `actions`/`probes`, else returns `null` (an ordinary shell command never parses as this shape).
- **Driver loop:** `runCodingTask` (tasks.js:1572), bounded by `MAX_CODE_PROBE_ROUNDS=3`: if the
  reply parses as a probe request (and rounds remain) → `requestPageProbe` (world.js:523, attaches
  the agent key), format via `formatPageProbeResult`, append to messages, continue (does NOT consume
  the continuation-attempt budget). Otherwise accumulate the shell command, check heredoc balance
  (`_heredocBalance`, bounded continuations), execute via `/api/execute`, run mechanical post-checks
  (extract written JS → unlink phantom refs → remove phantom script refs → dangling-selector advisory).
  The review lane mirrors it (`runReviewTask`, tasks.js:2059, `MAX_REVIEW_PROBE_ROUNDS`) and escalates
  unresolvable requirements via `/api/review/escalate`.
- **Server:** `_page_probe_sync` (serve.py:12910) runs headless Chromium with
  `--host-resolver-rules=MAP * 0.0.0.0` (network blackhole), diffs `Object.keys(window)` vs a
  fresh-blank baseline to inventory custom globals (real type/keys/values — surfaces "guard already
  fired" bugs), executes bounded actions (click/keydown/wait/eval), evaluates probes, captures
  console + page errors. Path-traversal guard confines the target inside the sandbox dir.
- **Completion:** `{actionLog, console, pageErrors, customGlobals, results}`.

## 2.7 Phone check-in — `POST /api/device/checkin` (serve.py:8993)

- **What it is:** a phone (iOS Shortcut) reports location/battery/Focus/Wi-Fi. Pure data ingestion —
  no LLM/Jev, **NOT dormancy-gated** (a check-in must land while the tank is asleep). Body fields
  all optional.
- **Auth:** shared `X-Device-Key` matching `DEVICE_API_KEY` (constant-time compare) → else 401;
  `location` must be `{lat, lon}` numeric.
- **Steps:** `record_device_checkin` (sim.py:3102) drops Nones, stamps `receivedAt`, keeps
  `deviceCheckins.last` + a ring buffer capped at 50.
- **Completion:** `{ok, storedAt}`. No consumer wired yet ("clean generic ingestion pipe first").

## 2.8 Pipelines — scheduled runs + the ad-hoc run lane

### Player-scheduled (`POST/GET/DELETE /api/pipelines`, serve.py:9912/9938/9948)
- `{name, cadenceMs, steps:[{title, room, offsetMs, instructions, tool, args}]}`;
  `add_pipeline` (sim.py:3042) fails closed on empty name/steps/steps lacking title+room; clamps
  cadence to ≥ `MIN_PIPELINE_CADENCE_MS` (1h); `lastRunAt:0` → first step fires on the next sweep.
- **Durable scheduler:** `_check_pipelines` (sim.py:3258) inside `_check_schedules` (3167) inside
  `_task_cycle` (server-owned tick). **Strict ordering** — step N+1 fires only after step N's task
  is `done` (matched by the `pipelineStep` marker); `lastRunAt` stamped at run START so the whole
  pipeline gets its cadence window; `runId` increments per run so old done-tasks can't satisfy a
  later run; `offsetMs` is a minimum delay.

### Ad-hoc agent run (`POST /api/pipeline`, serve.py:12677)
- Executes a sequence of `/api/execute`-style steps in the SAME sandbox dir (file state persists
  step-to-step: clone → install → test), stops at first blocked command or non-zero exit. Counted as
  ONE rate-limit call; each step individually Jev-classified. A blocked step →
  `create_escalation('blocked pipeline step', ...)` (**floor-1.0**).
- **Completion:** `{sandboxId, results:[{name, allowed, exitCode, stdout, stderr, timedOut}],
  failedStep}`.

## 2.9 Permission requests routed to the player

### Allowlist request — `POST /api/allowlist/request` (serve.py:11869)
- An agent requests a host join the player-vetted allowlist — a PERMANENT capability (full sandbox
  reachability + skips all future Jev round trips). **Floor-1.0: only the player's email link can
  grant it**; the director may never auto-approve. Normalize hostname; refuse private/internal hosts
  outright; already-allowlisted → early return; dedup pending requests per host; else
  `create_escalation('allowlist request', ..., on_approve_note=host)` → emails the player.
- **Resolution:** `GET /api/escalation/resolve` with approve → `_grant_allowlist` — the ONLY grant
  path, also refreshes the egress proxy.

### Temporary access request — `POST /api/access/request` (serve.py:12372)
- An agent without standing access asks its supervisor for REAL, TEMPORARY access to a
  room/role-gated capability (`curl`, `sandbox-download` — `TEMP_ACCESS_CAPABILITIES`).
- Judged by Jev as "the supervisor's call" — quorum + safety gate; low-confidence allows ESCALATE
  rather than grant. Approved → `_grant_temp_access` (serve.py:11954) with
  `expires_at = now + TEMP_ACCESS_DURATION_S` (1200s). **Never permanent.**
- **Completion:** `{approved, capability, expiresAt, durationS}`; privilege decision chained to the
  passport.

## 2.10 Library — the knowledge base (serve.py:10679–11415)

- **List** (`GET /api/library`): mtime-desc walk, dotfiles hidden (the `.passport.json` leak fix).
- **Search** (`GET /api/library/search`): case-insensitive substring over file contents (not
  embeddings), 400KB cap, 80-char snippet; **trail-ranked** — exponential decay (7-day half-life) of
  read counts (`_library_trail_score`, serve.py:10879); read reinforcement in `record_library_read`.
  Top 50. SAME implementation the clarify router and content.py's `search_library` tool use.
- **Read** (`GET /api/library/file`): traversal-guarded, 200KB, bumps the trail.
- **Write** (`POST /api/library/file`): **the "first-hand findings trusted, imported content
  quarantined" rule** — `source == 'external'` (anything picked up via browse) is FORCED into
  `pending_review/` server-side, ignoring the client path. `_redact_secrets` scrubbed; 200KB cap;
  `working-guide.md` writes require director/admin; per-agent ACL on `downloads/<agent>/`.
- **Download** (`POST /api/library/download`): Jev-gated, SSRF pre+post, sanitized flat filename,
  `scope` personal|shared → always lands in `pending_review/` (as untrusted as a browsed page);
  20MB cap.
- **Ingest** (`POST /api/library/ingest`): **player-only** (an LLM choosing local paths is a
  different risk class) — PDF→text, XLSX→text, text copied scrubbed, images as-is (no eager vision
  call), zip-bomb guard, 300-file walk cap.
- **Promote/Reject** (`POST /api/library/promote|reject`): the vetting decision — quarantine →
  trusted (`promote` chains BOTH the file block and a `library_promote` decision into the passport;
  a promoted file is TRUSTED) / → `rejected/` archive (kept, out of pending_review so a sweep
  doesn't re-judge it).
- **Passport** (`POST /api/library/passport`): reads the immutable ledger; verify lives at
  `/api/intent/passport/verify` (1.13).

## 2.11 Keys — capability credentials & handles (`serve.py:12535–12626`)

- **Credentials** (GET/POST/DELETE `/api/keys/credentials`): **player-only**; stored encrypted
  (Fernet `_seal_secret`); name slug-validated; store/delete chained to the passport.
- **Handles** (POST `/api/keys/handles`, DELETE): **player-only, proven by session cookie** (the
  `_resolve_requester` fallback would be backwards here — an operator action, not an agent absence).
  `mint_capability_handle` (serve.py:2492): refuses unknown credential; **master gate** — DigitalOcean
  handles unmintable while `SANDBOX_EXECUTION != 'digitalocean'`; per-credential spend cap; stores an
  opaque 32-byte nonce with JSON scope + expiry. Handle shown exactly once; never in the passport.
  Handles expire lazily (`_expire_handles`) and are revoked wholesale on firing
  (`revoke_agent_credentials`).

## 2.12 Escalation read + review escalate

- `GET /api/escalation/{id}` (`serve.py:13119`): polled by whatever's waiting; `{status, kind}`.
- `POST /api/review/escalate` (`serve.py:14559`): an agent whose review loop can't confidently
  judge a requirement — or exhausted its revision budget — escalates to the player instead of
  guessing or looping (the "act when confident, escalate when unsure" contract applied to grading).
  `_decide_allowed` throttle (6 calls/5s, 0.4s min interval); `create_escalation` with kind capped at
  60 chars (default `unresolved review requirement`, a routine-risk kind the director may
  auto-approve at the default floor); returns `{queued, escalationId}`.

## 2.13 The decision system — `POST /api/decide` + `/api/jev/model` + `/api/jev/calibration`

- Documented in Part 0.3. `POST /api/decide` is the typed-decision proxy (`_call_openrouter_decision_sync`,
  `_decide_allowed` throttle, spend → `__jev__`, decision tape with `trace_id`). `GET/POST /api/jev/model`
  read/switch the authoritative decision model (POST player-only, persistent). `GET /api/jev/calibration`
  is the DB-only feedback readout.

---

# Part III — Agent-driven processes (the processors)

## 3.1 Backlog Refinement — the per-team ceremony (sim.py:6257–6533)

- **Trigger:** weekly cadence (`REFINEMENT_CADENCE_MS = 7d`) per team; ALSO kicked instantly at
  sprint close and on large requests (`kick_refinement_now`, sim.py:6516 — zeroes
  `teamRefinementAt[team]` so the next pass convenes).
- **Gate:** a team in an ACTIVE sprint refines only at close (`_team_in_active_sprint`, sim.py:2366,
  checked at 6568 — the cadence stamp is deliberately NOT advanced, so the team is still due at
  close). A team needs an effective scrum master (`_refinement_scrum_master_for_team` 6297: the
  designated SM, or the director standing in for a team < `SCRUM_MASTER_MIN_TEAM_SIZE=4`). No
  pending requests → no empty meeting. A busy attendee defers the whole meeting.
- **Attendees:** SM + every agent who filed a request in `reqIds` (non-agent filers are dropped but
  their card is still groomed).
- **Steps:** `_refinement_step` (6536) (a) advances in-flight ceremonies in `pendingRefinements`
  (embark → resolve), (b) convenes a new ceremony for each due team. `_start_refinement` snapshots +
  marks busy at the Command Center; `_resolve_refinement` per request calls the decider with roadmap
  context (`_refinement_context_for_room`), criteria accept/reject. **Accept** → `queue_work` a real
  story (source:'refinement', user story + acceptance criteria + instructions), status 'accepted'.
  **Reject** → status 'rejected' (`selfProposedRejected` signal). Restore attendees; reset stamp;
  log digest.
- **Fallback:** decider outage → deterministic: accept iff `room in VALUED_QUEUE_ROOMS`, else reject
  (ship one well-scoped card rather than drop a filed gap).
- **SM gap 1 — stale in-flight work feeds the SM's re-plan (sim.py:8786):** on a coarse cadence
  (`STALE_WORK_CADENCE_MS=20s`) `_stale_work_step` sweeps NON-bug cards wedged past any legitimate
  budget — 'walking' older than `STALE_WORK_TIMEOUT_MS=3h` (never arrived), or still 'working'
  `STALE_WORK_BUDGET_GRACE_S=900s` past `workUntil`. A first staleness re-queues the card fresh
  (priority bumped to high, holder released). A repeat offender — re-planned
  `STALE_WORK_MAX_REPLANS=2` times for the same (title, room) — is routed to the owning team's
  scrum master as a work-request (`_sm_replan_stale_work`, kick_refinement_now), so refinement's
  accept/reject bounds the loop. Never touches bugs (own restore alarm), shadow dry-runs, or
  review/fix subtasks (stuck-gate watchdog's domain). Runs before the idle gate so a quiet tank
  still re-plans a wedged card.
- **Model tier:** one JEV decision call per request (`_call_openrouter_decision_sync` + `_jev_model`).
- **Tests:** `test_refinement.py`; `test_shared_backlog.py:323` (active-sprint skip).

## 3.2 Sprint lifecycle (sim.py:2049–2398)

- **Trigger (create):** player seeds via 1.8 (scrum-master gate for teams ≥4).
- **Steps:** `queue_sprint` tags each retained item `sprintId`; work flows queue → `_assign_due_item`
  → executor → (deliverable rooms) peer gate.
- **Auto-close:** `_auto_close_completed_sprints` (2169) closes an ACTIVE sprint when
  `done >= total` (`sprint_progress` counts gated stories done only after 2 clean approvals or
  1 + timeout; non-gated after `finish_task`). Sets `closedAt`+`autoClosed`, then
  **`_on_sprint_closed`** (2376):
  1. queue the retrospective (`pendingSprintRetros` append, deduped),
  2. pull `BACKLOG_PULLS_PER_CLOSE=1` shared item per touched team (`_pull_backlog_for_team`),
  3. kick refinement (`kick_refinement_now`).
- **Director gap 1 — team-health review at close (sim.py:2743 → `_team_health_review` 8013):**
  each director reviews the health of the WORKERS under them (never directors/admins — worker-only
  judgment directive). A worker whose REAL trailing grade (`_agent_trailing_grade`, only
  `gradeIsReal` Jev grades count) is below the `DELIVERABLE_GRADE_FLOOR=5.0` delivery floor gets a
  coaching growth-plan note routed to their NEXT task — no Jev spend (the trailing mean IS the
  signal), idempotent per agent (`_write_growth_plan` dedups by kind, `repeat=True` so a repeated
  low close re-coaches).
- **Manual close:** 1.8 — container-only status flip (close ≠ cancel). An incomplete sprint never
  auto-closes; a second close never re-queues a retro; shadow items never count toward progress.
- **Tests:** `test_sprints.py`; `test_shared_backlog.py:224/252`.

## 3.3 Sprint retrospective (sim.py:6607–6797)

- **Trigger:** a sprint id in `pendingSprintRetros` (queued at close).
- **Attendees:** the scrum master (first team with a NON-DIRECTOR effective SM — a small team whose
  only SM is the director waits, `_retro_scrum_master` returns None) + every non-director member of
  the sprint's teams. **The director is EXPLICITLY excluded** (a retro is the team's own reflection).
- **Steps:** standard ceremony shape at the Command Center (`RETRO_MEET_MS=10s`); `_resolve_retrospective`
  calls the decider with sprint goal + landed items, parses START/STOP/CONTINUE JSON, writes
  `state['retrospectives'][sprint_id]` (with `landed`, `attendees`), restores attendees.
- **Fallback:** decider outage → `{}` recorded (an empty retro never blocks sprint close).
- **Model tier:** LOW tier (`serve._low_tier_slug()`) via `_call_openrouter_sync`, accrued to
  `__retrospectives__`. Injectable `_retro_decider` for tests.
- **Tests:** `test_shared_backlog.py:276/305`.

## 3.4 Shared backlog + features (sim.py:2196–2360; serve.py:7951–8042)

- **Trigger:** player delegates a big task while EVERY team is busy in an active sprint (1.1).
- **Steps:** `_breakdown_into_shared_backlog` (high-stakes tier, allow_high) → 2–5 cards under a
  feature (reused by name) → `add_backlog_item` (cap 200) — **cards are room-free on purpose**; the
  ASSIGNED agent resolves the room at assignment. On sprint close, `_pull_backlog_for_team` →
  `pull_backlog_item` (FIFO, first-come) claims: `teamId` + team-scoped `storyKey` + status 'picked',
  then `queue_work` with `featureId`/`backlogItemId` provenance + `taskType` (spike→spike, else code).
  `_assign_due_item` resolves the missing room: `_resolve_assignment_room` (sim.py:2323) returns
  `'observatory'` for spikes, else `'pressoffice'`.
- **Fallbacks:** breakdown fails → `spawn_new_team_for_request` (new Director + team). A blocked
  item (`status=='blocked'`) is not eligible. Empty backlog → no-op.
- **Tests:** `test_shared_backlog.py` (all 13).

## 3.5 On-call escalation (sim.py:1771–1924, 7138–7410)

- **Trigger:** a player reports an incident (1.10) → `queue_bug`; OR an open bug task is abandoned
  at its assignment cap (`_on_work_item_abandoned`, 7585); OR an open bug is unrestored past
  `RESTORE_TIMEOUT_MS=24h` (swept in `_escalation_step`).
- **Steps:** `queue_bug` → owning team (product.teamId → director) → `on_call_agent` (deterministic
  per-sprint rotation over non-SM/non-admin members, prefers an available member, day-shifted when
  sprint-less) → pressoffice, taskType 'bug', priority high, `assignedTo` on-call, pinned non-gated.
  `_escalation_step` (7344) (a) advances the single in-flight `_pendingEscalation`, (b) sweeps
  walking/working bug tasks older than the restore timeout → `_escalate_oncall_failure` (7336) →
  `_start_escalation`: dedup per product (`_escalated_product_pending`), cap `ESCALATION_MAX_OPEN=3`
  per owning team, requires owning SM. `_embark_escalation` convenes the SM alone (Command Center,
  10s); `_resolve_escalation` decider picks **story|spike** — story → `backlogRequests` record
  (origin:'oncall_escalation', so the SM's own refinement ceremony cards it); spike → queued
  directly (non-gated).
- **Fallbacks:** no owning team/on-call/SM → no escalation (stays silent); decider outage → default
  to spike (investigation safer than a guessed story); SM busy/off-duty defers.
- **Model tier:** JEV (`_call_openrouter_decision_sync` + `_jev_model`).
- **Tests:** `test_oncall_escalation.py`.

## 3.6 Peer-approval gate + grading + revision loop (sim.py:3623–4155, 6930; tasks.js, grading.js)

- **Trigger:** a deliverable task's primary content work completes in a deliverable room with a
  normal lane (`_peer_gated_lane` sim.py:3623: room deliverable AND taskType not in
  NON_GATED_LANES={'spike','bug'}; also exempts skillReview/distill/research — housekeeping with no
  real "fix"). Also re-enters on a successful fix subtask.
- **Steps:** `_enter_peer_review` → status 'needs_review', `_peerGate={approvals:0, reviewerIds:
  two, cycleCount, escalated}`; notifies reviewers; queues two `taskType:'review'` subtasks pinned
  `assignedTo` + `reviewOf` + `reviewAuthorId`. Reviewer pick (`_pick_reviewer_ids` 3657): same-team
  first → room-contributors (known craft) → idle → roster; never author/admin/duplicates; a prior
  rejecting pair kept at the head on re-open. **Close when `_peer_gate_should_close`: 2 distinct
  clean approvals, OR 1 clean + `PEER_REVIEW_TIMEOUT_MS=15min` elapsed.** `_close_gated_story` →
  status 'done', email player "Shipped". Author released (`_release_agent_gated`): bumps
  `approvedCount`/`weekApprovals`, notes completed room → fires grading + runbook + follow-up filing.
  Grade (0–10) → `completedDeliverables` with `gradeIsReal`; low grade (< `DELIVERABLE_GRADE_FLOOR`)
  writes a growth plan; roadmap recomputed weekly from trailing grades + demand (`_roadmap_step`).
- **Failures:** a fix whose pipeline failed (`ok=False`) → `_release_agent_after_failure` (NO
  approval/room/grade/follow-up) + `_send_back_after_failure` (status 'failed', fix requeued to
  author, high priority). `_maybe_escalate_stuck_gate` freezes the gate at `MAX_REVIEW_CYCLES=8` +
  escalates to the player (pauses auto-cycling). Grade outage → trailing room mean else 5.0
  (marked `gradeIsReal=False` so fallbacks don't compound). Browser `runGradedReviewLoop`
  (`MAX_REVISION_ROUNDS=2`) grades checklist requirements and escalates unresolved fails.
- **W6 — review-denial rationale lands as coaching (`_sim_notify_author` sim.py:4325):** on an
  'actionable' verdict the gate re-opens AND, when the reviewer supplied a rationale, it is carried
  onto the author's NEXT task as a growth-plan note (`_write_growth_plan`, kind `review_denial`,
  `repeat=True`, rationale truncated to 400 chars) — a peer denial changes the author's next
  execution, not just a one-time mailbox message.
- **Model tier:** grader/runbook JEV; reviewer decisions via content.py ensemble grading
  (GRADE_MEETS/FAILS/UNSURE + confidence bar).
- **Tests:** `test_peer_approval.py`, `test_coding_standards.py`, `test_grading.mjs`,
  `test_revision_loop.mjs`, `test_cut2_processes.py`, `test_judge_gate_calibration.py`.

## 3.7 Spike lane — investigation (sim.py:1771; content.py:2113/3177)

- **Trigger:** player files via the routing lane; an incident escalation falls back to spike; a
  promoted spike→spike follow-up.
- **Steps:** `queue_spike` → taskType 'spike', priority low, time-boxed `budgetMs` (default 60s), no
  deliverable commitment. `_run_spike_content` (content.py:3177) runs **PLAN → EXECUTE → SYNTHESIZE**:
  PLAN (reasoning tier, one call, 3–7 item checklist; first step must be `search_web` or
  `search_library` — internal prior art) → EXECUTE (mid tier, many-iteration tool loop with
  reflection following the plan, forced first tool, one-strike-per-tool) → SYNTHESIZE (reasoning
  tier over the full transcript, findings against the checklist). Finding artifact written to the
  Library; `note` + `libraryPath` stored; `investigated` = any tool-role message.
- **Completion:** never opens a peer gate (spike ∈ NON_GATED_LANES); author released;
  `notifyPlayer spike_done` on success AND failure. **Promotion is player-only** (1.7); the spike
  stays an immutable done record.
- **W3 — spike findings file real issues (`_spike_file_issue_wish` content.py:3177 → `_file_spike_issue`
  sim.py:1606):** a spike whose SYNTHESIZE finding names a concrete problem the think tank OWNS
  (outdated / stale / vulnerable / unmet / incomplete / crashed, `_SPIKE_ISSUE_PROBLEM_SIGNALS`
  content.py:3168) files a JIRA issue via the issue store (1.9) — the owning team is resolved
  (`_resolve_worker_issue_team`, sim.py:1570) so the SM's refinement grooms the follow-up card, not
  a dead ledger. The fileIssue call is fail-safe (`try/except` → no wish on outage) and the sim
  guards on the returned wish.
- **Model tier:** PLAN + SYNTHESIZE = reasoning tier with `allow_high=True` (2 expensive calls per
  investigation); EXECUTE = mid tier.
- **Tests:** `test_spikes.py`, `test_spike_content.py`, `test_shadow_mode.py:308`.

## 3.8 Knowledge Social — weekly cross-team hangout (sim.py:4859–5110)

- **Trigger:** weekly cadence (`SOCIAL_CADENCE_MS=7d`). Stamps `lastSocialAt`, sets `_pendingSocial`.
- **Attendees:** `weekApprovals > 0` (did real work this week), not mid pair/handoff/
  firing-review/onboard. A mid-task agent attends (claim flagged so orphan-reclaim skips it;
  `workUntil` extended so the 30-min meet burns no work budget); off-duty agents woken and returned
  off-duty.
- **Steps:** convene at the Hangout; resolve after `SOCIAL_MEET_MS=30min`: each attendee gets a
  "what should I carry away" JEV decision, `weekApprovals` reset for the next window, attendees
  restored, decision tape recorded.
- **W2 — an 'adopt' carry-away lands (`_resolve_social` sim.py:5755-5761):** a carry-away of choice
  'adopt' with confidence ≥ `SOCIAL_ADOPT_CONFIDENCE=0.6` routes a coaching note through the
  growth-plan loop (`_write_growth_plan`, kind `social_adopt`, `repeat=True` so each fresh weekly
  adopt is a new commitment) — the adoption changes the worker's NEXT task execution, not just a
  digest line.
- **Fallbacks:** no eligible attendees → no-op; JEV outage → `(None, 1.0)` so the event never blocks.
- **Tests:** `test_social.py`.

## 3.9 Distillation — hive-mind wiki merge (sim.py:3144–3333; content.py:374; serve.py:10244)

- **Trigger:** `DISTILL_CADENCE_MS=30min` sweep in `_check_schedules`, content-gated: due AND
  `_distill_has_new_archives(since)` (any plain file in `LIBRARY_ARCHIVE_DIR` newer than the stamp).
  Stamp `lastDistillAt` BEFORE assignment; marker NOT advanced when nothing new.
- **Steps:** task → observatory, taskType 'distill', `distillSince`. `_run_distill_content`
  (content.py:374): collects archive findings newer than `since` (newest first, max 25, excerpt 4k
  chars each); reads the current `state-of-knowledge` wiki page (excerpt 6k); ONE mid-tier `/api/chat`
  synthesis merges/de-dupes/cites; **deterministic safety net** — any real CSV-like block in the
  folded archives missing from the reply is appended back verbatim, cited (structured data is never
  silently lost); persists via `_write_wiki_server` (server-authority, survives autosave).
- **Fallbacks:** nothing new → noop; model call fails → noop; wiki write fails → noop.
- **Tests:** `test_distill.py`.

## 3.10 Self-heal + stuck-at-review watchdog (sim.py:817, 3970–4155; serve.py:3254)

- **Orphan reclaim:** every `_task_cycle` pass — a 'walking'/'working' task whose assignee no longer
  holds it (parked / pointer gone / missing assignee) is re-queued so a fresh assignment re-issues it.
- **Stuck gate:** coarse cadence (`STUCK_GATE_CADENCE_MS=20s`): `_sweep_stuck_gates` — for a gated
  story whose locked reviewer pair can't produce a vote (`_gate_reviewer_reachable` false for both,
  AND no live review subtask), re-pick a fresh REACHABLE pair and re-pin pending `reviewOf`
  subtasks. Bounded: `STUCK_GATE_GRACE_MS=5min` before widening, `STUCK_GATE_RESCUE_REVIEW_TIMEOUT_MS=30min`
  rescue cooldown. Never auto-closes; a lost verdict (`_reenter_gate_review`) re-enqueues fresh
  review subtasks. `_peer_review_tick` (serve.py:3254) runs inside the sim's single read-modify-write
  (no clobber race). Repeated no-verdict rescues freeze after the bound (`_maybe_escalate_stuck_gate`).
- **W4 — worker stuck/help signal (`_worker_stuck_help_signal` sim.py:8949, call site 9216):** tracks
  CONSECUTIVE content-execution failures per worker (a `ok=False` content result — executor crash or
  red pipeline); a clean result resets the streak. Crossing `WORKER_HELP_AFTER_FAILS=2` routes a help
  signal to the owning team's scrum master (`_sm_help_stuck_worker` — a work-request + refinement kick)
  and logs governance, then re-arms. A wedged WORKER is surfaced (vs. SM gap 1's wedged CARD). Never
  trips on reviews (they report `ok=True`).
- **W5 — coaching catch-up loop (`_coaching_loop_step` sim.py:8978, call site 9070):** daily sweep
  (`COACHING_LOOP_CADENCE_MS=24h`) re-checks every agent who HAS a coaching plan (`low_grade` /
  `team_health`): if their real trailing grade is STILL below the delivery floor, re-coach
  (round-aware, `_coach_low_grade` capped at `COACHING_MAX_ROUNDS`) or escalate through the bounded
  SM work-request path (`_escalate_coaching_loop`). Coaching can't be absorbed once and ignored
  forever.
- **Tests:** `test_self_heal.py`, `test_stuck_gate.py`.

## 3.11 Idle park + dormancy (sim.py:7899, 7700–7724; serve.py)

- **Idle park:** `_task_cycle` with an empty/not-yet-due queue + nobody active →
  `_park_idle_wanderers` sends every fully-idle on-duty NON-admin agent off-duty (sprites converge
  to zero; assignment re-wakes on demand). Runs BEFORE the idle gate so a quiet tank still converges.
- **Dormancy:** while `_dormant()`, the sim tick is skipped entirely (no DB read/write, no movement,
  no task cycle, no spend) — `_expire_handles` housekeeping still runs. ANY request wakes the tank;
  `/api/device/checkin` is not dormancy-gated.
- **Tests:** `test_idle_park.py`, `test_dormancy.py`.

## 3.12 Shadow mode — Bot Ops dry run (sim.py:2948; serve.py:8551)

- **Trigger:** a work item queued with `shadow: True`.
- **Steps:** the flag survives the queue whitelist + assignment, lands on the real task. On content
  completion: `_complete_shadow_task` marks done, appends an entry to the append-only
  `shadowLedger` (`promoted=False`, captures title/room/instructions/note/libraryPath/taskType),
  releases the agent — **no peer gate, no approvedCount/weekApprovals bump, no
  completedDeliverables/completedRooms/grade/runbook, no follow-up filing, no dependency unblock**.
  The player reads the ledger (`GET /api/shadow`) and may promote (`POST /api/shadow/{idx}/promote`):
  the dry-run finding becomes the new task's instructions; the new story rides the normal peer-gate
  path; the entry is marked 'promoted' (immutable).
- **Fallbacks:** promote 409 if already promoted; 400/404 for bad/unknown index; 'spike' stays in
  the non-gated lane.
- **Tests:** `test_shadow_mode.py` (incl. sprint_progress excluding shadow items).

## 3.13 The Bank — spend ledger + director teller (serve.py:715–966; content.py:621)

- Documented in Part 0.5. Accrual is a pure choke-point side effect; caps gate every chokepoint;
  the readout (`_run_bank_content`) is the director/admin teller + worker read-only summary.
- **Tests:** `test_bank.py`, `test_spend_accrual.py`.

---

# Appendix — player-ask routing (free-text lane classification)

- Free-text player asks are classified into lanes by `_ROUTING_HANDLERS` (serve.py:9857):
  - **ask** — the Theo-pinned one-off question (1.2)
  - **schedule** — creates a research topic via `_extract_schedule_fields_sync` (1.4)
  - **spike** — `queue_spike`, optionally team-pinned (3.7)
  - **story** — `file_issue` → JIRA issue + backlogRequests → refinement (1.9 + 3.1)
  - **incident** — `queue_bug` → on-call (1.10 + 3.5)
  - **unclear** — senior-most director answers directly.
- These feed processes 3.1 / 3.2 / 3.5 / 3.7.
- **Unclear-lane authority (serve.py:9975–10006):** `_route_lane_unclear` → `_unclear_lane_authority`
  answers with the SENIOR-MOST NON-ADMIN director (admin only as a fallback; None degrades the ask
  to the normal round-robin). Matches the "directors, not the admin, are the free-text authority"
  posture.

# Appendix — request-type → process map

| Request | Entry point | Primary process |
|---|---|---|
| Big task (free teams) | `/api/intent/assign-big-task` | decompose → queue → refinement kick |
| Big task (all teams busy) | `/api/intent/assign-big-task` | shared backlog (3.4) → pull at sprint close |
| One-off question | `/api/intent/ask` | ask tool loop (1.2) |
| Question about completed work | `/api/intent/clarify` | KB-first clarify (1.3) |
| Recurring research sweep | `/api/intent/schedule` | `_check_schedules` (1.4) |
| Story veto | `/api/intent/story/{id}/reject` | peer-gate re-entry (1.5, 3.6) |
| Publish | `/api/intent/publish` | staging + git push (1.6) |
| Spike promote | `/api/intent/spike/{id}/promote` | queue deliverable (1.7) |
| Sprint | `/api/intent/sprint*` | sprint lifecycle (1.8, 3.2) |
| Ticket / issue | `/api/intent/issues*` | issue store (1.9) → refinement |
| Incident | `/api/intent/incidents` | on-call bug (1.10, 3.5) |
| Product | `/api/intent/product*` | product record + release (1.11) |
| Wiki | `/api/intent/wiki*` | wiki (1.12) / distill (3.9) |
| Chat | `/api/chat` | fallback chat (2.1) |
| Web fetch | `/api/browse` | Jev-gated fetch (2.2) |
| Raw HTTP | `/api/curl` | Jev-gated, room-gated (2.3) |
| Sandbox command | `/api/execute` | Jev-classified Docker (2.4) |
| Transcript | `/api/youtube-transcript` | Colab transcribe (2.5) |
| Page facts | `/api/page-probe` | headless probe (2.6) |
| Phone | `/api/device/checkin` | ingestion (2.7) |
| Pipeline | `/api/pipelines` + `/api/pipeline` | scheduled / ad-hoc runs (2.8) |
| Allowlist / temp access | `/api/allowlist/request`, `/api/access/request` | human/Jev approvals (2.9) |
| Knowledge | `/api/library*` | KB + vetting (2.10) |
| Capability keys | `/api/keys/*` | credentials + handles (2.11) |
| Escalations | `/api/escalation*`, `/api/review/escalate` | approval valve (2.12) |
| Decisions | `/api/decide`, `/api/jev/*` | Jev layer (2.13, 0.3) |
| Ceremonies (refine, retro, social, distill, on-call, self-heal, park, shadow, bank) | sim `_task_cycle` | Part III |

---

# Revision log

> This file is a living review artifact. Each revision lists what changed in the
> PROCESSES above (the flows, gates, and ceremonies) so the player can diff the
> spec against the code commit-by-commit. The header line-number pointers are
> refreshed on each commit.

## 2026-10-03 — player-session intent gate + coverage floor (the current commit)
- **Player-only intent surface enforced (`_require_player_session`, serve.py:4079):** the 15
  handlers that hardcode actor `'player'` (sprint create/close, publish, product release, story
  veto, spike promote, issue create/status/detail/claim-met/block-dependency, wiki page/category,
  sprint-close re-open path) now reject even a valid agent key with 401. The middleware's
  session-OR-agent-key rule alone let a server content executor (loopback with only an agent key)
  act as the player; each handler now additionally requires a real player session. §0.1. Tests:
  `test_serve.py` (agent-key rejection across all 15 + the helper's session-only check).
- **Coverage floor raised to ≥95%:** total Python + browser-JS test coverage has a hard floor of
  ≥95% (was 90%), with a 99–100% target; enforcement lands in `test_coding_standards.py` with the
  coverage push. §0.1.
- **`test_jev_model` lifespan made hermetic:** the endpoint tests' `with TestClient(...)` started
  the real app lifespan, whose Telegram poll loop blocked shutdown on a 25s long-poll
  (`TELEGRAM_POLL_TIMEOUT_S`) — ~97s for the file. The tests now patch `TELEGRAM_BOT_TOKEN=None`
  so the loop never starts; the file runs in ~1.3s. No behavior change.

## 2026-10-03 — worker + oversight gaps worklist
These revisions close the worker-level and oversight gaps the player flagged. Each is pinned by
tests listed alongside.

- **SM gap 1 — stale in-flight work sweep (sim.py:8786):** a non-bug card wedged in
  'walking'/'working' past any legitimate budget is re-queued once, then routed to the owning
  scrum master as a work-request (`_sm_replan_stale_work`) so refinement accept/reject bounds the
  loop. §3.1. Tests: `test_self_heal.py` (stale-work cases).
- **Director gap 1 — team-health review at sprint close (sim.py:8013, call site 2743):** each
  director grades the trailing health of the WORKERS under them only (worker-only judgment
  directive) and coaches anyone below the `DELIVERABLE_GRADE_FLOOR`. No Jev spend, idempotent per
  agent. §3.2.
- **Admin gap 1 — periodic health digest (serve.py:2662, 14867):** a readable markdown digest of
  standing signals (aging in-flight work, open escalations, Bank over-cap) is written to the shared
  library every `HEALTH_DIGEST_INTERVAL_S=6h`, so oversight survives a closed browser.
- **W1 — server-owned pairing + handoff (sim.py:3230/3276/751/9491):** pair arrival, handoff
  delivery, navigator release on driver ship, and `pair`-card navigator recruitment all work
  server-side now (the client-only paths froze walkers). §3.10 (stale-busy repair exempts a
  paired navigator).
- **W2 — Knowledge Social adopt lands (sim.py:5755):** an 'adopt' carry-away at confidence ≥ 0.6
  routes a coaching note to the worker's next task. §3.8.
- **W3 — spike findings file real issues (content.py:3177 → sim.py:1606):** a spike that names a
  concrete owned problem files a JIRA issue into the owning team's refinement. §3.7.
- **W4 — worker stuck/help signal (sim.py:8949):** `WORKER_HELP_AFTER_FAILS=2` consecutive
  content failures route a help signal to the owning team's SM. §3.10.
- **W5 — coaching catch-up loop (sim.py:8978):** a daily sweep re-checks agents whose real trailing
  grade is still below the floor and re-coaches (capped) or escalates. §3.10.
- **W6 — review-denial rationale as coaching (sim.py:4325):** an 'actionable' peer verdict with a
  reviewer rationale carries that rationale onto the author's next task as a growth-plan note. §3.6.
- **Unclear-lane authority (serve.py:9975–10006):** the senior-most non-admin director answers
  unclear asks directly; admin only as fallback. Appendix.

## 2026-10-02 — review-change worklist (the current commit)
These revisions implement the review-worklist items the player asked for. Each
one is pinned by tests listed alongside.

- **Todo 1–7, 4-amend, 15, 19 (earlier in the worklist):** peer-review gate
  re-entry (story veto → author rework, §1.5/3.6), gate reviewer re-pick + lost
  verdict re-entry, room-based completion notes, and related gate-lifecycle
  fixes. Tests: `test_peer_approval.py`, `test_serve.py::PlayerIntentEndpoints`.
- **Cross-team borrowing (todo 5):** before a director spends a hire, an
  INACTIVE agent from another team (no active sprint) is borrowed for the
  borrower team's sprint (`_borrow_inactive_agent_for_team`, sim.py:4433).
  Tests: `test_team_borrow.py` (`TeamBorrow`, 8).
- **Sprint staffing + breakdown ceremony (todos 6/7):** refinement staffs a new
  sprint from the backlog and a breakdown ceremony decomposes delegated big
  tasks into story cards. Tests: `test_shared_backlog.py`, `test_sprints.py`.
- **Skill-review stagger (todo 8):** multiple skill-review cards in one queue
  are de-duplicated/held so they never hammer the same reviewer pool at once
  (`SkillReviewStagger`, 5).
- **Sprint rollover + rollover pin (todo 9):** refinement's sprint-rollover
  re-plan pins a carried-over card to a fresh explicit owner
  (`_reassignedTo`, honored by `_assign_due_item`, soft-not-lock); a new sprint
  is started and the old one closed. Tests: `test_sprints.py` (`SprintRollover`,
  7; `RolloverReassignPin`, 2).
- **Feature affinity (todo 10):** `_assign_due_item` pulls a soft preference
  toward the agent who completed similar work before (same room/feature/product),
  layered on the team + fault-aware round-robin — never a lock. Tests:
  `test_fault_aware_routing.py::FeatureAffinity` (5).
- **kbClass whitelist + completion-evidence gate (todos 11/18):** the queue
  round-trips `kbClass`; a completion whose result carries NO evidence note is a
  **DoD gap** — the task is marked `failed` ("missing completion evidence"),
  never `done`, and a rework card is queued. Tests:
  `test_sim.py::SimTaskLifecycle`.
- **Wiki agent-propose lane (todo 12):** ANY authenticated agent can propose a
  wiki page to `pending_review/wiki/`; a director/admin approves it into the
  live wiki (versioned + passport-chained) or rejects it to `rejected/wiki/`.
  The live wiki is untouched until approval. Tests:
  `test_products_wiki.py::WikiProposeLane` (7).
- **Ripple re-review cascade (todo 13):** stories carry `dependsOn`; rejecting a
  story re-opens the peer gate on every done, gated-lane story that depended on
  it (`_cascade_rereview`), so a veto ripples instead of leaving stale
  dependents. Tests: `test_serve.py::PlayerIntentEndpoints` (cascade × 2).
- **Agent onboarding description-affinity (new):** a brand-new hire has no work
  history (feature affinity can't route to them), so when NOBODY has matching
  prior work `_assign_due_item` soft-pulls toward the candidate whose role/
  mission DESCRIPTION fits the task. Tests:
  `test_fault_aware_routing.py::OnboardingAffinity` (3).
- **KB-delete guard (new):** the live wiki tree is the curated, director-gated
  knowledge layer. The generic `/api/library/file` write and
  `/api/library/promote` (which `shutil.move` would let clobber a live page)
  both refuse `wiki/` paths; the propose lane owns wiki promotion. Tests:
  `test_serve.py::WikiDeleteGuard` (3).
- **Work agreement drafted BY agents (todo 17, as re-scoped):** on onboarding
  stage 2→3 the NEW HIRE drafts their own work agreement (their words from
  their own role/mission/access — not a director's instructions) and the ADMIN
  empowers it; it renders into AGENT.md under "Work Agreement". Tests:
  `test_onboard.py::WorkAgreementEmpowered` (4).
- **Per-story capability grants (todo 14):** a temporary access grant records
  the `task_id` (the specific story it was granted for) and is revoked when that
  story ships (`revoke_task_access` at `finish_task` / `_close_gated_story`); the
  access-request endpoint only accepts the agent's own live task. Tests:
  `test_capability_keys.py::PerStoryCapabilityGrant` (3).
- **Decision (todo 16):** change requests are NOT implemented — the current
  flow already lets the player veto closed stories (§1.5) and promote spikes
  (§1.7); an explicit change-request lane was judged unnecessary at this time.
- **Memory audit (decay-scored algorithms):** the think tank's memory is
  deliberately compacted through decay-scored scores, not raw logs. The
  mechanisms: `_agent_failure_score` (fault memory, exponential half-life
  `FAILURE_COOLDOWN_HALF_LIFE_S=1800`, sim.py:804 — feeds `_assign_due_item`),
  `_library_trail_score` (library usage, 7-day half-life), `_render_memory_md`
  (action_log decay + boost, keep top 20, serve.py:1301), and `_room_trailing_grade`
  / `MORALE_DROPPED_DECAY_DAYS` (sim.py:3576). Finding: `MEMORY.md` is written per
  agent by `sync_agent_directories` (serve.py:1386) but is NOT injected into any
  prompt — it is an inspectable record read via the agent-files endpoints and
  referenced by `handoffs.js`. Acceptable as-is; no fix needed.
- **Compaction audit:** there is no explicit "compaction" pass — compaction IS the
  decay-scored consolidation above (each store is bounded and time-decayed). One
  genuine gap was found and fixed: the per-agent player chat `conversationLog`
  grew unbounded in state (persisted every autosave) while only the last 4
  exchanges ever feed the model (index.html:2389). Now capped at
  `CONVERSATION_LOG_CAP = 400` entries (index.html:2271/2352), matching the
  bounded-memory posture of the rest of the system.
- **JEV audit:** every genuine decision point introduced by these changes is
  Jev-gated — temp-access approval runs `_jev_quorum_decision` +
  `_jev_safety_gate`, and hire + breakdown ceremonies are Jev ceremonies. The
  mechanical rules (assignment affinity, ceremony staging, cascade re-entry, ACL
  guards, grant revocation) are deliberately deterministic and spend zero JEV —
  no unbounded non-Jev judgment was introduced.
- **Live weather for the Weather Station (new):** the weather-station task no
  longer browses a hard-coded Wikipedia reference page. It fetches REAL
  Open-Meteo data for a configured location (`WEATHER_LOCATION`, default
  "Charlotte, NC", overridable by env) via serve's own `_weather_fetch`
  (`_run_weather_content`, content.py; `/api/weather/now` for the browser path).
  All hard-coded forecast/reference values are gone. Tests:
  `test_ask.py::WeatherStationLiveReadings` (3).
- **Story-scoped grants ride the story, not a clock (new):** a temp-access
  grant tied to a `taskId` no longer expires on a timer — `expires_at` is NULL
  ("no clock") and the grant dies when its story ships (`revoke_task_access`).
  A one-off grant with no story still rides the finite `TEMP_ACCESS_DURATION_S`
  window (20 min), so non-story one-offs need no story/spike to be created.
  Tests: `test_capability_keys.py::PerStoryCapabilityGrant` (now 5).
- **Scrum master / director duties review (todo 1, closed):** the effective-SM
  rule (`_refinement_scrum_master_for_team`, sim.py:6716) holds across every
  ceremony that needs a facilitator — refinement and breakdown use the same
  resolution (designated SM → own-director stand-in for teams below
  `SCRUM_MASTER_MIN_TEAM_SIZE=4` → None, which correctly defers the ceremony),
  and the retrospective has its own stricter rule (`_retro_scrum_master`,
  sim.py:7329): a NON-director SM is preferred, else the own director stands in,
  and only a busy/off-duty own director falls back to borrowing a director from
  another team. Review outcome: the duty split is coherent and no ceremony lets
  a director be pulled into a worker's seat. Boundary pinned by tests:
  `test_sprints.py::EffectiveScrumMaster` (3).
- **Multi-category requests review (todo 1, closed):** free-text routing
  (`_route_player_request`, serve.py:10084) deliberately classifies each message
  into ONE lane — a multi-intent bundle can't be force-fit without silently
  dropping the rest, so the 'unclear' lane description now explicitly names
  "BUNDLES MULTIPLE UNRELATED REQUESTS" as an unclear signal; a bundled or
  ambiguous message is answered by the senior-most free director
  (`_route_lane_unclear` → `_free_authority`), who can split it. An unknown/
  stale lane id also falls back to 'unclear' (never drops or 500s). Tests:
  `test_ask.py::TelegramBridge` (+2).
- **Todo 1 peer-gate re-entry improvements (new):** the veto endpoint's reason
  must reach the author verbatim so the rework targets what the player flagged
  (and a reasonless veto must NOT fabricate a quoted reason). Tests:
  `test_serve.py::PlayerIntentEndpoints` (+2).

---

*This document is a review artifact. Every flow above is derived from the code at commit
`22ce3d5` (the worker + oversight gaps worklist); the line numbers are best-effort entry
points, not exhaustive spans, and the newest gap bullets are anchored to the current code
while older anchors may lag a commit. Send revisions as comments or
a diff against this file and they will be folded back in.*