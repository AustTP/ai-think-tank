# AI Think Tank — Architecture & Systems Design

## 1. Overview

A server-owned AI-agent think tank simulation. Agents autonomously file work
requests, refine backlogs, execute tasks, peer-review deliverables, escalate
blockers, and self-govern through ceremonies — all driven by real LLM calls
through OpenRouter, gated by a JEV policy classifier where decisions have
consequences. The server owns all state (`think_tank.db`); the browser is a
read-view into it.

**Codebase:**
- `world/serve.py` (~11k lines) — FastAPI server, simulation driver,
  model-tier management, the Bank (spend ledger), JEV gates, team/director
  hierarchy, authentication, all HTTP endpoints.
- `world/sim.py` (~6.8k lines) — Server-owned state machine: movement,
  task assignment, ceremonies (refinement, social, governance, escalation),
  peer review gates, sprint lifecycle, agent off-duty/position management.
- `world/content.py` (~2.7k lines) — Content executors that run when an
  agent arrives at a task room: research, coding, review, distill, weather,
  media, spikes, and the Bank teller.
- `world/web_helpers.py` — Pure HTML stripping, link extraction, HTTP-date
  parsing, SSRF host check, filename sanitization (extracted from serve.py).
- `world/sim_helpers.py` — Pure priority normalization, room derivation,
  team lookup, sprint/product id generation (extracted from sim.py).

---

## 2. Server Architecture

The server is a FastAPI application (`_lifespan` startup) that runs several
background loops:

| Loop | Cadence | What it does |
|------|---------|-------------|
| `_sim_loop` | 6s task cycle | Drive the simulation: movement, task lifecycle, ceremonies |
| `_health_check_loop` | 300s | Compute health signals, persist new alerts (model tiers, coordination, runaway-tool churn) |
| `_peer_review_loop` | 90s | A random peer observes the action log and files a note on an out-of-line worker (probabilistic) |
| `_director_approval_loop` | 30s | Resolve pending escalations via delegated JEV approvals |
| `_telegram_poll_loop` | 25s | Poll Telegram for player messages, route into think tank |
| `_backup_loop` | 5min | Snapshot think_tank.db (keeps newest 24) |
| `_log_prune_loop` | 6h | Delete decision_tape/action_log rows older than retention (7d) |
| `_model_tier_refresh_loop` | daily | Re-pick each band's best-value model from live catalog + prices |

Each loop runs as an independent asyncio task; the sim loop runs movement
on a 2s tick and the task cycle on a 6s tick, both off-thread to avoid
blocking HTTP.

**Data model:** A `kv_state` blob in SQLite holds the authoritative think tank
state (agents, teams, work queue, tasks, sprints, research topics, room
definitions). Decision audit logs live in two append-only tables:
`action_log` (human-readable, per-agent activity feed) and `decision_tape`
(raw JEV/LLM decisions with full prompt+response for debugging). Both are
rolled by the prune loop.

---

## 3. Agent Model & Hierarchy

### Roster and identity

Agents are defined by an `agentRoster` array (metadata: id, name, role, color,
model tier) and a parallel `agents` dict (live state: position, busy/off-duty,
task assignment, mailbox, conversation log, approved/dropped work counts).
Both live in `kv_state`, seeded from `.env` on a cold DB and authoritative
thereafter. Per-agent identity files (`agents/*/agent.json`, `AGENTS.md`,
`MEMORY.md`, `state.json`) are materialized mirrors regenerated on every DB
save — never the source of truth.

### Director graph

Authority is derived from the `director` pointer: every agent has a director
they report up to. Walk the chain and you always reach the admin. This makes
team membership derived (any agent whose `director` chain resolves to a given
director id is on that team), and promotion automatic — an agent with a
direct report becomes a director for free.

- **Admin** (`isAdmin`) — one agent (Theo). Approves/denies escalations,
  decomposes big tasks, can write any team or agent state.
- **Senior-most director** — a director with no `director` of their own who
  is NOT the admin (Nora). Stands in for the admin on routine approvals.
- **Mid-directors** — agents with direct reports (e.g., Dev leads a support
  team). Get their own team records automatically.
- **Workers** — non-admin, non-director agents who do the actual room work.

### Teams

Teams are materialized records (`state['teams']`) keyed by the director id.
Team membership is DERIVED from the `director` graph at backfill time —
never stored separately. Scrum masters are designated per-team and required
only when the team reaches `SCRUM_MASTER_MIN_TEAM_SIZE` (default 4); smaller
teams run with the director standing in as facilitator.

---

## 4. Simulation and Task Lifecycle

### Task lifecycle (`_task_cycle`, sim.py)

1. **Queue**: Work items land in `state['workQueue']` via refinement grooming,
   big-task decomposition, or standing schedules (research, distill).
2. **Arrival**: Movement tick emits `('arrive','task',agent_id)` when the
   agent reaches the task's room door → marked `status: 'working'` with a
   work budget (`workUntil`).
3. **Execution**: `_task_cycle` dispatches content executors per room type
   (research, coding, media, weather, bank, spike) or falls back to a
   placeholder work-duration sleep (`TASK_WORK_DURATION_S = 8s`).
4. **Peer gate**: Deliverable tasks (pressoffice, observatory) enter a peer
   gate — two same-or-other-team reviewers must approve (2 clean votes, or 1
   + timeout). Reviewers are agent-to-agent (mailbox notification), not
   player emails; the player only hears about shipped stories.
5. **Completion**: `finish_task` bumps approvedCount, marks task `done`,
   clears the agent, sends them off-duty.
6. **Sprint progress**: Derivation-based — matches completed tasks' (title,room)
   pairs against sprint item records. Sprints auto-close when all items done.

### One-off scheduled tasks

A player can ask for a single task to run at a specific future day+time
(`queue_once` in sim.py; exposed via the `schedule_once` routing lane and
`POST /api/intent/schedule-once`). The item lands in the work queue with a
`notBefore` (epoch-ms) gate instead of a cadence: `is_work_item_due` keeps it
untouched until `at_ms`, so the idle gate treats a far-future item as "no work"
and the think tank spends nothing waiting. When the clock passes `at_ms` the
item enters the normal assign → work → complete lifecycle exactly once (never
recurring). The gate accepts ISO 8601 day+time (trailing `Z` or explicit
offset; naive times are treated as UTC so scheduling is unambiguous) or an
epoch timestamp, fails closed on anything unparseable, rejects past times, and
caps the horizon at 366 days so a misparsed date can't sit in the queue for
years.

### Dependency-gated scheduling

A queued item (or a scheduled research topic / one-off) can carry a
`dependsOn` / `depends_on_task` task id: even once its `notBefore` time
passes, the item is NOT assigned until the referenced task reaches `done`
in the durable task mirror (`_work_item_dependency_met`). The dependency
not landing is "not work yet" — the idle gate treats a gated item as not
due, so the think tank spends nothing waiting, and the assignment loop never
hands a gated card to an agent. Landing a task auto-clears any
dependency-blocks keyed on it, so a composed "run X once the dependency
ships" pipeline resolves itself without a manual kick.

### Idle gate

`think_tank_has_work()` gates spend: if no due queue items AND no agent is
mid-task/handoff/pair/busy, the task cycle returns early with zero mutations.
Ceremonies (refinement, social, roadmap) run ungated but no-op without
requests/eligible attendees — they never spend on an empty think tank.

---

## 5. Policy-as-Code (JEV)

JEV is a classifier (`typesafe/jev-1.13`) that returns a typed choice +
calibrated confidence, backed by quorum sampling (up to 3 independent samples,
majority vote). Used for every decision that has consequences:

| Decision | What JEV decides | Confidence floor | Fallback |
|----------|-----------------|-----------------|----------|
| Browse allow/block | Whether an agent may visit a URL | 0.6 (safety) | Escalate to human |
| Command allow/block | Whether an agent may execute a shell command | 0.6 (safety) | Block + escalate |
| Peer review clean/actionable | Whether a deliverable passes code review | n/a | Re-queue fix |
| Escalation approve/deny | Director delegated approval | Per-kind floor (1.0 = human only) | Leave pending |
| Refinement accept/reject | Scrum master grooms backlog into stories | n/a | Fallback accept |
| Firing review keep/fire | Whether a low-performing agent should be fired | n/a | Default keep |
| Social carryaway | What an agent takes from the weekly knowledge social | n/a | Note skip |
| Governance decider | Who needs help most (hiring) or needs review (firing) | n/a | Fallback pick |
| Model tier gate | Whether a call deserves mid or high tier over cheap default | 0.55 | Fail closed to low |
| Colab shard band | Highest runtime count one `run_on_colab` computation may shard across (single/double/shard) | 0.55 | Fail closed to a single runtime |

### Colab shard gate

A spike agent proposes a runtime count (up to 5) when it shards one computation
across Google Colab runtimes. The count is a consequence-bearing spend choice —
each runtime is a metered compute-unit cost and an extra fan-out of the think
tank's decision gates — so JEV independently caps it: the agent's proposal is
matched to the smallest band its stated purpose earns (`single`=1, `double`=2,
`shard`=3-5), the run uses `min(requested, band max)` runtimes, and a
low-confidence or unreachable classifier degrades to a single runtime (the same
fail-to-cheap move as the tier gate). `runtimes=1` skips the gate entirely, and
the degraded-Jev refusal still stands (a sharded run must not multiply work
that depends on a decision path already failing). The band decision is logged
as `colab_shard_gate`; the run itself logs requested vs approved vs granted
counts.

Provisioning failures are retried a bounded number of times with backoff
(`COLAB_PROVISION_RETRY_DELAYS_S`, 3 attempts total) before the honest
"provision failed" error is returned: free-tier availability, GPU cooldowns,
and transient CLI/API errors recover quickly, and a code-level retry is far
cheaper than punting the agent into a whole re-investigation. A genuine
quota/capacity refusal still gives up and says so after the bounded retries
are spent — there is no unbounded retry and no unbounded agent-side loop.

### Escalation processing

The director loop resolves pending escalations every 30s. Escalation kinds
with a risk floor of 1.0 (`blocked command`, `blocked pipeline step`) are
HUMAN-ONLY — skipped before any JEV call. Resolvable kinds get JEV with a
15-minute cooldown between re-asks and a 6-attempt cap before they join the
human-only pool. Fixes the prior 204,000-request escalation storm.

---

## 6. Model Tier System

### Tier structure

| Band | Benchmark | Model | Price/M | Purpose |
|------|-----------|-------|---------|---------|
| Low | MMLU | deepseek-v4-flash | ~$0.26 | Default: everything starts here. Cheap above all else |
| Mid | MMLU-Pro | deepseek-v3.2-exp | ~$0.68 | Lightly gated by JEV. Needs real reasoning |
| High | HLE | deepseek-v4-pro-0813 | ~$4.60 | Heavily gated by JEV + monthly budget. Only for high-stakes planning |
| Coding | SWE-bench | qwen3-coder | $1.30 | Deterministic: any code/review/qa task uses this tier, no JEV |
| Vision | MMMU | qwen3-vl | $0.75 | Deterministic capability axis: screenshot/image reading |

### Daily refresh

`refresh_model_tiers()` re-picks every band's best-value model from the live
OpenRouter catalog once a day. Selection within each band:
1. Keep only models with a cited benchmark score in `model_benchmark_scores`
2. Keep models within `BENCHMARK_QUALITY_FLOOR_GAP` (6 points) of the best score
3. Pick the cheapest qualifying model
4. Verify the model works with a real test call before writing to `model_tiers`

The high tier additionally enforces `HIGH_TIER_MAX_PRICE_USD` (default $5/M) —
no model over the ceiling is even offered to the pool, regardless of its score.
And `HIGH_TIER_MONTHLY_BUDGET_USD` (default $5/mo) — once the month's high-tier
spend hits the cap, the JEV gate fails closed to mid.

### JEV-gated tier escalation

`_resolve_model_tier(purpose, task_type, allow_high)`:
- **Coding** (code/review/qa task type) → deterministic coding tier, no JEV
- **Everything else** → JEV decides low/mid/high, fails closed to low
- **Mid** = lightly gated (`TIER_GATE_MIN_CONFIDENCE=0.55`)
- **High** = confidence-gated + budget-gated (monthly spend cap). Only
  `intent_assign_big_task` opts in via `allow_high=True`.
- All content executors, ask lane, clarify, mail audit, web monitor, runbook
  writer, and tool loops go through the gate.

---

## 7. Ceremonies & Business Processes

### Backlog refinement (per-team, concurrent)

Each team has its own ceremony slot (`pendingRefinements[team_id]`), so one
team no longer blocks another. The scrum master (or the director standing in
for teams below the size threshold) grooms pending work-requests into
`queue_work` stories via a JEV accept/reject decision. Runs on a weekly
cadence — also triggered immediately on large-request arrival via
`kick_refinement_now()`.

### Sprint lifecycle

- **Create** via `intent_sprint` — requires scrum master for teams at/above
  `SCRUM_MASTER_MIN_TEAM_SIZE` (4 workers). Small teams pass with director
  as stand-in.
- **Progress** derives from matching (title,room) pairs against done tasks.
- **Auto-close** — when every sprint item is done and (for gated stories)
  peer-approved, the sprint closes automatically.
- **Velocity** — `close_sprint` records a velocity snapshot on the
  active→closed transition: landed / total / pct / rolledOver /
  `pointsLanded` / `pointsTotal` / `elapsedMs`. Points are the item size
  estimates (S=1 / M=2 / L=3), folded in as `state['teamVelocity'][teamId]`
  as a rolling window capped at `TEAM_VELOCITY_MAX` (20). A sprint with no
  numeric estimates records `None` points rather than a fake 0 — the honest
  "not estimated" signal. Re-closing an already-closed sprint recomputes
  nothing (idempotent). The retrospective prompt surfaces the snapshot, so
  the retro is grounded in measured delivery, not recollection.

### Peer review gate

Two reviewers are picked per deliverable, preferring same-team members but
widening to every non-admin agent if the team can't supply two. Reviewers
receive a mailbox notification (`peer_review_request`) and must submit a
verdict (clean = approval counted, actionable = fix re-queued to the author).
The gate closes at 2 approvals or 1 + review timeout.

### New-team spawn on large request

When `intent_assign_big_task` is called and every existing team is busy in an
active sprint, `spawn_new_team_for_request()` creates a new director + team +
employee(s). Team headcount scales with request size (heuristic: word count +
scope signal). The new team's refinement is kicked immediately.

---

## 8. Budget Controls

### Page-request budget (1,000/month)

Each `browse_page` fetch and `search_web` (Tavily) call counts as one page
request. Tracked monthly; when the month's allowance is spent, both browsing
and search are refused with a clear message. Set `PAGE_REQUEST_MONTHLY_BUDGET`
in `.env`.

### Dollar spend cap ($5 default)

`SPEND_CAP_USD` is a hard ceiling on total new model spend from the moment
it's set. Once hit every real model call (JEV, chat, decide) fails closed.
Baseline is stored in the spend ledger itself so pre-existing spend never
counts.

### High-tier monthly budget ($5/month)

`HIGH_TIER_MONTHLY_BUDGET_USD` caps total high-tier model spend per calendar
month. The JEV gate checks this before returning the high slug — once spent,
high requests are routed to mid. Accrual happens at the `/api/chat` choke
point.

### Test-time compute (deliberation on low/mid tiers)

The low and mid tiers are the cheap, quality-limited models the sim leans on
most (chat, daily logs, gathering), and they can't "think harder" the way the
expensive tier can. So `/api/chat` runs a lightweight deliberation loop for
them: sample the prompt `TTC_BEST_OF` (default 2, capped at `TTC_MAX_BEST_OF`
= 3) times and fold the drafts into one answer — exact-match JSON majority
vote for structured responses, a same-model self-verification judge pass
(pick the best draft verbatim) for open-ended prose. The reported reply is
therefore far more consistent than any single cheap draft, at the cost of
best_of (prose: best_of + 1) cheap calls. Controls:

- `TTC_ENABLED` (default on) kills the whole mechanism in one env var.
- `deliberate: false` in a `/api/chat` body opts a single request out; `best_of`
  overrides the sample count per request.
- Expensive/reasoning/coding tiers never deliberate — they're already the
  spend-heavy path. High-tier escalation already covers the rare deep-think
  case.
- Every sample and the judge call are real billed calls accrued to the spend
  ledger, so the caps above bound them exactly like any other model call.

### Plain-writing directive (token saver)

The village's prose replies (chat, ask lane, clarify lane) carry an
anti-AI-slop directive: "write plainly, short sentences, no filler, avoid
these words" with a word-ban list mirrored from the coupon scanner
(`leverage`, `utilize`, `seamless`, `moreover`, `it is worth noting`, ...).
`_apply_plain_writing` folds the directive into the first system message at
the model-call boundary, so every prose-producing path inherits it by default.
It is a TOKEN saver, not a style law: cutting filler and recap shrinks both
input and output tokens, and the directive is intentionally worded not to
demand clipped speech. Controls:

- On by default in `/api/chat`; `{"plain": false}` opts a single request out.
- JSON-structured prompts (those containing `JSON`) are exempt so a schema is
  never truncated mid-object.
- The ask lane and clarify lane inherit it automatically.

### The Bank

The spend ledger (`_spend_ledger_read`/`_spend_ledger_write`) is a per-service
accounting system. Each model call is attributed to a service (product id,
`spike`, `__general__`, `__jev__`, `__high_tier__`). Directors see:
used / cap / calls / lastAt / trailing-7-day burn rate / days until cap
per service and cumulatively.

---

## 9. Security Architecture

### Network isolation

Sandboxed container execution uses a dual-homed Docker network: one fully
isolated net (no route to the internet) and one egress net that passes through
a proxy restricted to allowlisted hosts. Browser fetch (`/api/browse`) uses
SSRF-gated urllib with re-check after redirects.

### Prompt-injection boundaries

All external content (web pages, tool outputs, search results) is wrapped
with `wrap_external_content()` which adds a nonce+HMAC boundary marker.
`verify_boundary_intact()` is called downstream to detect injection.

### Authentication

Session-based auth (PBKDF2 password hash, HttpOnly cookie) for the player.
Agent-key HMAC for agent-server calls. `/api/chat` requires either a valid
session OR a valid agent key (so server-to-server loopback works).

---

## 10. Data Model (key tables)

| Table | Purpose | Retention |
|-------|---------|-----------|
| `kv_state` | Single-row think tank state blob (agents, teams, tasks, queue, rooms) | Forever |
| `kv_spend` | Single-row spend ledger (per-service used/cap, byDay series) | Forever |
| `model_tiers` | Per-band model slug, name, price, chosen_at | Active bands only |
| `model_benchmark_scores` | Cited benchmark scores (model_id, benchmark, score, source_url) | Preserved |
| `action_log` | Per-agent activity feed (agent_id, action, details JSON, ts) | Rolling prune at `LOG_RETENTION_DAYS` (default 7, override in .env) |
| `decision_tape` | Raw JEV/LLM decisions (prompt, criteria, choice, confidence, cost, raw) | Rolling prune at `LOG_RETENTION_DAYS` (default 7, override in .env) |
| `agent_keys` | Agent attribution secrets (agent_id, secret_key) | Forever |
| `sessions` | Player login sessions | 7-day expiry |

---

## 11. Key Design Decisions

1. **Server-owned simulation** — the browser is a viewport, not a state
   machine. The server's `_task_cycle` drives everything; the client only
   renders what the server says.
2. **DB as source of truth** — `think_tank.db` is authoritative. Agents/*.json
   files are regenerated mirrors, never independently mutated.
3. **Failed closed, not open** — every JEV gate, budget cap, and circuit
   breaker defaults to the safe/conservative/cheap side. An outage never
   causes spend-up or unauthorized access.
4. **Price-dominant model selection** — models are re-picked daily from the
   live OpenRouter catalog against cited benchmark scores, cheapest within
   a quality floor.
5. **Single read-modify-write pass** — `_task_cycle` does all completion,
   assignment, and governance in one atomic pass on `state`, never the DB
   directly.
6. **Everything is a JEV gate** — from browsing to firing to model-tier
   selection, every consequential decision runs through the same typed-choice
   classifier with quorum sampling.
7. **Idle is free** — `think_tank_has_work()` gates spend before any ceremony
   fires. An idle think tank spends nothing.
8. **High tier is bounded twice** — per-model price ceiling ($5/M) keeps the
   daily refresh from picking a $200/M monster; monthly spend cap ($5/mo)
   keeps the gate from escalating into the expensive tier more than a few
   times per month.
9. **Teams are derived, not stored** — membership comes from walking the
   `director` graph, so restructuring a team is repointing a pointer. No
   membership list to drift.
10. **Ceremonies run concurrently** — refinement has per-team slots. One
    team's scrum master never blocks another's from grooming their backlog.

---

## 12. Failure Taxonomy and Rule Mining

The draft-review loop is more than "send it back" — it sorts WHY a deliverable
failed and turns recurring reasons into operator-visible rule proposals.

### The failure ledger

Every review/QA send-back records one classified failure
(`_record_failure` → `failures.json`, kept outside `world/` so it is never
statically served). Classification (`_classify_failure`) runs cheap
deterministic rules first, then a Jev classifier as fallback, into four
buckets:

| Type | Meaning |
|------|---------|
| `factual_error` | A number or claim is wrong or has no source |
| `client_preference` | Contradicts a stated client/operator preference |
| `missing_information` | A required fact or field is missing |
| `style` | Style, tone, or wording |

The classifier fails closed to `style` on a failed call or low confidence — a
mislabeled fact landing in `style` only yields a softer proposed rule, never a
wrong safety gate (and the deterministic money rule already pins the
safety-critical bucket without any model call). The ledger is bounded to the
tail (`FAILURE_MAX_RECORDS`).

### Weekly rule mining

On the same silent weekly cadence as the roadmap recompute, `_rule_mine_step`
(sim.py) calls serve's `_mine_rule_proposals`: group the ledger by
(type, ruleHint) and any pattern recurring at least `RULE_MIN_RECURRENCE` (2)
times becomes a PROPOSED rule carrying the rule text plus a test fixture (the
offending input and the requirement it missed). Nothing is auto-applied — a
proposal surfaces to the operator and becomes a real rule only when the
operator encodes it (ban list, Jev criteria) and pastes the fixture into a
conformance test, exactly like every other operator-applied governance change.
Proposals are capped (`RULE_PROPOSALS_MAX`), so an aging think tank cannot
grow the file without bound.

---

## 13. Video-Takeaway Mapping

Three videos were reviewed and their transcripts kept under
`world/library/media/transcripts/` (`fde-masterclass`, `ai-roll-ups`,
`fde-explained`). Their lessons were checked against the codebase and are
already operationalized:

| Video lesson | Where it lives |
|--------------|----------------|
| Run agents in the background before they touch anything real | Bot Ops shadow/dry-run mode — `shadow` queue items capture to `shadowLedger` and never ship, plus the weekly diff-against-expectation review |
| A shared layer of rules that gets better every week | §12 failure taxonomy + weekly rule mining |
| People shift from doing the work to checking it | Review checklists, verifier-ensemble grading, the peer review gate |
| Measure constantly, prove the before/after delta | Weekly review (ground-truth week-over-week deltas) + sprint velocity (§7) |
| Baseline before you build | Delivery-grade floor + trailing-grade coaching in the sim |

No further code was added for the videos — the transcripts were ingested and
the mapping recorded here so the review is auditable, not re-done.