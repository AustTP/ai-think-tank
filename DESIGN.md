# AI Village — Architecture & Systems Design

## 1. Overview

A server-owned AI-agent village simulation. Agents autonomously file work
requests, refine backlogs, execute tasks, peer-review deliverables, escalate
blockers, and self-govern through ceremonies — all driven by real LLM calls
through OpenRouter, gated by a JEV policy classifier where decisions have
consequences. The server owns all state (`village.db`); the browser is a
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
| `_health_check_loop` | 300s | Check model tiers, circuit breaker recovery |
| `_peer_review_loop` | 90s | Senior director files peer reports on low-activity workers |
| `_director_approval_loop` | 30s | Resolve pending escalations via delegated JEV approvals |
| `_telegram_poll_loop` | 25s | Poll Telegram for player messages, route into village |
| `_mail_audit_loop` | 3-12h | Admin reviews agent mailboxes for unusual activity |
| `_backup_loop` | 5min | Snapshot village.db (keeps newest 24) |
| `_log_prune_loop` | 6h | Delete decision_tape/action_log rows older than retention (7d) |
| `_model_tier_refresh_loop` | daily | Re-pick each band's best-value model from live catalog + prices |

Each loop runs as an independent asyncio task; the sim loop runs movement
on a 2s tick and the task cycle on a 6s tick, both off-thread to avoid
blocking HTTP.

**Data model:** A `kv_state` blob in SQLite holds the authoritative village
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

### Idle gate

`village_has_work()` gates spend: if no due queue items AND no agent is
mid-task/handoff/pair/busy, the task cycle returns early with zero mutations.
Ceremonies (refinement, social, roadmap) run ungated but no-op without
requests/eligible attendees — they never spend on an empty village.

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
And `HIGH_TIER_MONTHLY_BUDGET_USD` (default $2/mo) — once the month's high-tier
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

### High-tier monthly budget ($2/month)

`HIGH_TIER_MONTHLY_BUDGET_USD` caps total high-tier model spend per calendar
month. The JEV gate checks this before returning the high slug — once spent,
high requests are routed to mid. Accrual happens at the `/api/chat` choke
point.

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
| `kv_state` | Single-row village state blob (agents, teams, tasks, queue, rooms) | Forever |
| `kv_spend` | Single-row spend ledger (per-service used/cap, byDay series) | Forever |
| `model_tiers` | Per-band model slug, name, price, chosen_at | Active bands only |
| `model_benchmark_scores` | Cited benchmark scores (model_id, benchmark, score, source_url) | Preserved |
| `action_log` | Per-agent activity feed (agent_id, action, details JSON, ts) | 7-day rolling prune |
| `decision_tape` | Raw JEV/LLM decisions (prompt, criteria, choice, confidence, cost, raw) | 7-day rolling prune |
| `agent_keys` | Agent attribution secrets (agent_id, secret_key) | Forever |
| `sessions` | Player login sessions | 7-day expiry |

---

## 11. Key Design Decisions

1. **Server-owned simulation** — the browser is a viewport, not a state
   machine. The server's `_task_cycle` drives everything; the client only
   renders what the server says.
2. **DB as source of truth** — `village.db` is authoritative. Agents/*.json
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
7. **Idle is free** — `village_has_work()` gates spend before any ceremony
   fires. An idle village spends nothing.
8. **High tier is bounded twice** — per-model price ceiling ($5/M) keeps the
   daily refresh from picking a $200/M monster; monthly spend cap ($2/mo)
   keeps the gate from escalating into the expensive tier more than a few
   times per month.
9. **Teams are derived, not stored** — membership comes from walking the
   `director` graph, so restructuring a team is repointing a pointer. No
   membership list to drift.
10. **Ceremonies run concurrently** — refinement has per-team slots. One
    team's scrum master never blocks another's from grooming their backlog.