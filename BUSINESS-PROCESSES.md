# Think Tank Business Processes

Status: review draft
Date: 2026-10-17
Owner: operator (the player)
Scope: the operating processes of the AI think tank as implemented in this repository


## Contents

- 0. How to use this document
- 1. Executive summary
- 2. Operating model
- 3. Process inventory
- 4. Core delivery processes (BP-01 to BP-12)
- 5. Planning and governance (BP-13 to BP-25)
- 6. Scheduling and standing operations (BP-26 to BP-33)
- 7. Knowledge and evidence (BP-34 to BP-41)
- 8. Money and resources (BP-42 to BP-46)
- 9. Security, risk, and human oversight (BP-47 to BP-56)
- 10. Communication and lifecycle (BP-57 to BP-60)
- 11. Player and operator processes
- 12. Control framework
- 13. State and record model
- 14. Cadence and SLA table
- 15. Roles and permissions matrix
- 16. Improvement candidates
- 17. Appendix A: glossary
- 18. Appendix B: configuration and policy files
- 19. Appendix C: process to code map
- 20. Appendix D: review checklist

## 0. How to use this document

This document describes what the think tank does, in what order, who owns each
step, which control gates each step, and which records each step leaves behind.
It is written for process review, not for code review. The code remains the
authoritative source. Where this document and the code disagree, the code wins
and this document should be corrected.

Each process has an ID (`BP-nn`). Use the IDs when proposing changes so a
review comment points at one process, not at a whole subsystem.

Three source documents were used:

- `README.md` for purpose and operator setup.
- `DESIGN.md` for architecture and design intent.
- The implementation: `world/serve.py`, `world/sim.py`, `world/content.py`,
  and the extracted modules (`bank.py`, `evidence.py`, `memory.py`,
  `theo_policy.py`, `config.py`, `fs.py`, `auth.py`, `notify.py`).

## 1. Executive summary

The think tank is a persistent, server-owned organization of LLM agents. The
human owner gives direction, approves gated actions, and reviews outcomes. The
agents do the work: they take in requests, groom a backlog, plan sprints, walk
to rooms, produce deliverables, review each other, hire and fire, and keep a
shared memory.

The operating model has five properties:

1. **The server is the state machine.** The browser is a viewport. Every
   position, task, decision, and record lives in `think_tank.db`.
2. **Work is gated before it costs money.** A JEV classifier decides every
   consequence-bearing choice. A budget gate sits in front of every model call.
3. **Idle is free.** If no work is due and no agent is active, the task cycle
   returns without spending.
4. **Deliverable work is reviewed.** Deliverable work passes a two-reviewer
   peer gate. Research passes a claim-level evidence review. The gates cover
   deliverables only: bugs, spikes, and meeting cards close with no review
   gate. Failures become rule proposals.
5. **Authority is structural.** The `director` pointer defines teams and
   escalation. The owner is the only human. Theo is the admin agent, not the
   owner.

### Key operating numbers

| Item | Value | Source |
|---|---|---|
| Simulation tick | 1s | `sim.SIM_TICK_S` |
| Task cycle | 6s | `_task_cycle` cadence |
| Idle gate | No due work and no active agent -> no spend | `think_tank_has_work` |
| Peer review gate | 2 approvals, or 1 approval plus 15 min | `sim._peer_gate_should_close` |
| Review cycle cap | 8 cycles, then freeze and escalate | `sim.MAX_REVIEW_CYCLES` |
| Refinement cadence | Weekly, plus immediate kick on a large request | `sim.REFINEMENT_CADENCE_MS` |
| Social cadence | Weekly | `sim.SOCIAL_CADENCE_MS` |
| Roadmap and mining | Weekly, silent | `sim.ROADMAP_CADENCE_MS`, `RULE_MINE_CADENCE_MS` |
| Model tier refresh | Every 2 hours | `serve.MODEL_TIER_REFRESH_INTERVAL_S` |
| Tier price watch | Every 30 minutes | `serve.TIER_PRICE_WATCH_INTERVAL_S` |
| Idle dormancy poll | 30s when armed by `--max-idle-minutes` | `serve._idle_shutdown_loop` |
| Health snapshot | Every 300s | `serve.HEALTH_CHECK_INTERVAL_S` |
| Peer help-signal pass | Every 90s | `serve.PEER_REVIEW_INTERVAL_S` |
| Director escalation loop | Every 30s | `serve.DIRECTOR_APPROVAL_INTERVAL_S` |
| Log prune | Every 6h, retention 7 days, 50k rows per run | `serve.LOG_PRUNE_INTERVAL_S` |
| Database backup | Hourly, newest 1 kept | `serve.DB_BACKUP_INTERVAL_S`, `DB_BACKUP_KEEP` |
| Page request budget | 1,000 per month by default | `serve.PAGE_REQUEST_MONTHLY_BUDGET` |
| Spend cap | $50 by default, 0 disables | `serve.SPEND_CAP_USD` |
| High-tier monthly budget | 0 by default, disabled | `serve.HIGH_TIER_MONTHLY_BUDGET_USD` |

## 2. Operating model

### 2.1 Actors

| Actor | What it is | Authority |
|---|---|---|
| Owner / player | The human who owns the installation | Full authority. Only the owner may change policy, grant or revoke Theo keys, and approve gated Theo actions. |
| Theo | The admin agent | Director-level agent. Decomposes big tasks, answers the owner, stands at the top of the agent chain. Never the owner. |
| Senior-most director | A director with no director above them, excluding the admin | Stands in for the owner on routine escalation approvals. |
| Director | Any agent with direct reports | Owns a team, a product area, or both. Can write team and agent state. |
| Scrum master | A designated team facilitator | Runs refinement, breakdown, retrospective, and escalation ceremonies. Required at team size 4 or more. |
| Worker | A non-admin, non-director agent | Does room work, reviews peers, files requests and reports. |
| Critic reviewer | The first reviewer on a peer gate | Instructed to prove the work wrong, not to rubber-stamp it. |
| Verifier | A director, admin, or the player acting on evidence claims | Clears or blocks claims and flips coverage. Rate capped when acting as an agent. |
| Winter team | A second team created by the adversarial village | Works the same goal as main, independently. |
| Operator | The human running the deployment | Same person as the owner in normal use. Configures policy JSON, budgets, and integrations. |

### 2.2 Authority and reporting

Authority is derived from the `director` pointer on each roster entry. Walk the
chain from any agent and you reach the admin. Consequences:

- Team membership is derived, never stored as a separate list.
- Promotion is automatic: an agent with a direct report becomes a director.
- Team records are materialized one per director, keyed by the director id.
- The senior-most non-admin director is the routine stand-in for the owner.

Theo's role policy adds a second authority model for conversation:

| Requester | Role | May ask Theo to |
|---|---|---|
| Player session | owner | Everything, including policy changes and key grants |
| Admin agent | director | Read and shape work, run it, report, message the team, manage schedules |
| Agent with reports | director | Same as admin agent |
| Other agent | worker | Report status, read own artifacts, request sandboxed tool calls, message the team |
| Granted key holder | Role carried by the key | The key's own action set, rate limit, and budget |

Actions that spend money, reach outside, change the fleet, or modify the repo
create a pending approval for the owner. They are not denied outright. The
owner answers with `approve apr_<id>` or `deny apr_<id>`.

### 2.3 Work hierarchy

```
Feature
  -> Backlog item or issue (story / bug / spike)
    -> Work-queue item (the durable card)
      -> Task (one agent, one room, one work budget)
        -> Deliverable or review result
          -> Peer gate -> done -> task_archive
            -> Sprint progress -> velocity -> retrospective
```

Products sit beside the hierarchy. A product is a catalog entry with a sandbox,
a spec, revisions, and a release status. A product build task produces a
revision. A release copies a sandbox snapshot into the Library and flips the
product to `released`.

### 2.4 Time model

Three clocks drive the organization:

- **Wall clock.** Schedules, one-off times, escalation cooldowns, retention.
- **Simulation clock.** Movement, arrival, work duration, ceremony durations.
- **Cadence stamps.** Weekly and daily work uses state stamps (`lastRoadmapAt`,
  `lastRuleMineAt`, and similar) rather than wall-clock jobs. A restart does not
  replay a missed cadence; the next tick sees the stamp and runs.

The task cycle is one read-modify-write pass. It performs completion,
assignment, ceremonies, governance, and sweeps in a fixed order. No process
writes the database mid-cycle. One save happens after the pass.

### 2.5 End-to-end value stream

```
Owner request
  -> intake and routing (BP-01)
  -> issue or work request (BP-02)
  -> refinement (BP-03) or breakdown (BP-04)
  -> sprint or direct assignment (BP-06, BP-07)
  -> execution in a room (BP-08)
  -> peer review (BP-09) or evidence review (BP-36)
  -> completion, report, feedback (BP-11)
  -> retrospective and velocity (BP-15)
  -> failure and success mining (BP-22, BP-23)
  -> roadmap and consensus update (BP-13, BP-14)
  -> next cycle
```

## 3. Process inventory

| ID | Process | Trigger | Cadence | Owner | Primary output |
|---|---|---|---|---|---|
| BP-01 | Player request intake and routing | Owner message | On demand | Theo | Routed lane and record |
| BP-02 | Agent work requests | Agent files a request | On demand | Requesting agent | Pending backlog request |
| BP-03 | Backlog refinement | Pending requests or weekly | Weekly plus kick | Scrum master | Stories in the queue |
| BP-04 | Large-request breakdown | Staffable large ask | On demand | Scrum master | Sized stories and spikes |
| BP-05 | New-team spawn | Large ask, all teams busy | On demand | Admin | New director, team, workers |
| BP-06 | Sprint lifecycle | Sprint intent or staffing | Continuous | Director, scrum master | Sprint record and velocity |
| BP-07 | Assignment and on-call | Due work | Every task cycle | Sim | Assigned task |
| BP-08 | Task execution | Agent arrives | Per task | Assigned agent | Deliverable or failure |
| BP-09 | Peer review | Deliverable completes | Per task | Two peers | Approvals or fix request |
| BP-10 | Failure handling and send-back | Rejection or failed check | Per event | Reviewer, author | Classified failure, fix task |
| BP-11 | Completion, report, feedback | Task done | Per task | Sim, owner | Archive row, card report, rating |
| BP-12 | Product build and release | Product task | Per release | Product owner | Revision in Library |
| BP-13 | Roadmap recompute | Weekly stamp | Weekly | Sim | Room priorities |
| BP-14 | Consensus relay | Weekly stamp | Weekly | Sim | Current direction line |
| BP-15 | Retrospective and velocity | Sprint close | Per sprint | Scrum master | START/STOP/CONTINUE notes |
| BP-16 | Knowledge social | Weekly stamp | Weekly | Senior director | Carryaway notes |
| BP-17 | Meetings | Queued meeting | On demand | Facilitator | Meeting record |
| BP-18 | Charter | Owner or director sets it | On demand | Admin | Goal, interests, notes |
| BP-19 | Hiring and onboarding | Idle capacity and need | Cooldown 45s | Team director | New agent, work agreement |
| BP-20 | Firing review | Corroborated signal | Cooldown 60s | Admin, senior director | Fire or keep verdict |
| BP-21 | Coaching and growth | Low trailing grade | Daily sweep | Director | Coaching plan, growth plan |
| BP-22 | Rule mining | Weekly stamp | Weekly | Sim | Operator rule proposals |
| BP-23 | Success mining | Weekly stamp | Weekly | Sim | Operator success proposals |
| BP-24 | Archive distillation | Weekly stamp | Weekly | Sim | Decision archive wiki page |
| BP-25 | Adversarial village | Owner launches | On demand | Admin, winter director | Two independent attempts |
| BP-26 | Research topics and watchlist | Owner schedule or seed | Per topic cadence | Sim, researcher | Research card and skill update |
| BP-27 | Standing tasks | Declaration tick | Per declaration | Sim | Crewed standing card |
| BP-28 | Pipelines | Cadence due | Per pipeline cadence | Sim | Ordered step tasks |
| BP-29 | One-off schedules | Owner names a time | Once | Sim | One card at the named time |
| BP-30 | Skill review and distillation | 30-minute cadence | 30 minutes | Sim | Reviewed skills, distilled wiki |
| BP-31 | Incidents and bugs | Owner or monitor report | On demand | On-call, team | Incident card and restore |
| BP-32 | Page probes, screenshots, browser act | Tool call | On demand | Agent, sandbox | Page result, screenshot, trace |
| BP-33 | Device check-in | Phone post | On demand | Player device | Bounded check-in history |
| BP-34 | Wiki lifecycle | Write or proposal | On demand | Director, admin | Versioned page, provenance |
| BP-35 | Memory provenance | Every shared-memory write | Per write | Sim | Ledger row |
| BP-36 | Evidence and coverage review | Research run or review | Per claim | Verifier, owner | Claim status, coverage status |
| BP-37 | Research desk passes | Research run | Per run | Researcher | Three-pass synthesis |
| BP-38 | Library and file access | Agent or owner request | On demand | Agent, owner | Files, grants, archive |
| BP-39 | Skill publishing | Spike or review output | On demand | Agent | Skill card in the Library |
| BP-40 | Conversation memory | Telegram ask | Per turn | Theo, memory module | STM turns, LTM memories |
| BP-41 | Decision cache and tape | Every JEV call | Per call | Serve | Cache entry, chained tape row |
| BP-42 | Bank ledger and budget gates | Every model call | Per call | Bank | Spend row, cap verdict |
| BP-43 | Model tier selection | Every model call, refresh | 2 hours | Serve | Tier slug |
| BP-44 | Experiment grants | Owner mints a grant | On demand | Owner, grant module | Token, spend cap |
| BP-45 | Task budgets and overruns | Task execution | Per task | Director, Bank | Budget stop, reopen |
| BP-46 | Page request budget | Browse or search | Monthly | Bank | Allow or refuse |
| BP-47 | JEV decision gates | Consequence-bearing choice | Per choice | JEV | Typed choice, confidence |
| BP-48 | Escalation and delegated approval | Gate cannot decide | 30s loop | Director, owner | Approve, deny, or human-only |
| BP-49 | Player input asks | Agent blocked on owner | Per ask | Director gate | Inbox question, unblock |
| BP-50 | Credential vault and handles | Agent needs a service | On demand | Owner, director | Encrypted credential, handle |
| BP-51 | API service proposals | Agent proposes a service | On demand | Owner, director | Registered service |
| BP-52 | Browse, command, egress, VPN | Agent reaches outside | Per call | JEV, proxy | Allow, block, escalate |
| BP-53 | File and app grants | Owner grants or revokes | On demand | Owner | Scoped file and app access |
| BP-54 | Prompt-injection boundaries | External content enters | Per call | Serve | Wrapped content, verification |
| BP-55 | Audit, retention, backups | Continuous | Hourly and 6-hourly | Serve | Logs, tape, snapshots |
| BP-56 | Health monitoring | Continuous | 300s | Serve | Alerts, digest |
| BP-57 | Player feedback | Owner rates a report | On demand | Owner, sim | Rating, agent feedback |
| BP-58 | Peer help signals | Peer help-signal pass | 90s | Serve, director | Help signal to a director |
| BP-59 | Dormancy and wake | Idle with the flag armed | 30s poll | Serve | Dormant flag, wake on request |
| BP-60 | Code self-edits and dormancy windows | Armed edit or agent window request | Per edit or window | JEV, player | Armed edit, applied module, scheduled window |

## 4. Core delivery processes

### BP-01 Player request intake and routing

**Purpose.** Turn one free-text owner message into one correctly owned piece of
work.

**Trigger.** The owner sends a message through the Telegram bridge, or calls one
of the structured intent endpoints.

**Steps.**

1. Trim the text. Empty text returns 400.
2. If the message is `approve apr_<id>` or `deny apr_<id>`, decide the pending
   Theo approval before any classification.
3. If a granted Theo key is presented, verify it. An invalid, revoked, or
   expired key returns 401 before classification.
4. Classify the message into one lane with a quorum-sampled JEV call:
   `ask`, `schedule`, `schedule_once`, `spike`, `story`, `incident`, `unclear`.
   An outage or unrecognized choice becomes `unclear`, never a guess.
5. Resolve the requester role. Check the role against the lane's action.
6. Denied requests return a reason. Gated requests create a pending approval
   and tell the requester the approval id.
7. Dispatch:
   - `ask` -> `_ask_core` with the admin pinned. Telegram passes the chat id as
     the memory session.
   - `unclear` -> the senior-most free non-admin director answers; the admin is
     the fallback; then `_ask_core` may park or round-robin.
   - `schedule` -> extract topic, start URL, cadence, optional dependency.
     `add_research_topic` rejects a non-URL or empty topic and clamps the
     cadence to 5 minutes. The topic is reflected into `watchlist.json`.
   - `schedule_once` -> extract title and day/time. Reject past times. Reject
     more than 366 days ahead. `queue_once` creates one card with `notBefore`.
   - `spike` -> classify a team and room. `queue_spike` files a time-boxed
     investigation. If a team is identified, a free worker is direct-routed.
   - `story` -> classify a team. `file_issue` files a story against that team.
     If no team can be identified, fall back to `unclear`.
   - `incident` -> classify a product. `queue_bug` assigns the on-call. If the
     product cannot be identified or the bug cannot be routed, fall back to
     `spike` rather than pinning an incident to the wrong product.

**Controls.** Role policy, approval gates, quorum classification, fail-closed
fallbacks, action log, passport decision.

**Records.** `action_log` rows (`route_classified`, `schedule_created`,
`issue_created`, `incident_filed`), research topic record, work item, issue.

**Review note.** Two intake surfaces exist. The Telegram bridge has conversation
memory. The public `/api/intent/ask` endpoint is stateless by design. The
operator should know which surface the owner is expected to use for follow-up
questions.

### BP-02 Agent work requests

**Purpose.** Let an agent propose work without letting it write straight into a
sprint.

**Trigger.** An agent files a work request during or after a task.

**Steps.**

1. The agent supplies a title, a room, and a reason.
2. `file_work_request` appends a pending request to the team's queue.
3. The request waits for the team's refinement ceremony.
4. Refinement accepts or rejects it. Acceptance turns it into a queued story.
5. A linked issue, if any, is mirrored as the request moves.

**Controls.** Refinement is the only acceptance path. Requests are capped per
ceremony (`REFINEMENT_MAX_REQUESTS = 20`).

**Records.** `backlogRequests`, issue link, refinement outcome.

### BP-03 Backlog refinement

**Purpose.** Groom raw requests into stories a team can actually execute.

**Trigger.** Pending requests for a team, or the weekly cadence, or a large
request kick.

**Steps.**

1. Select the team's scrum master. If the team is below
   `SCRUM_MASTER_MIN_TEAM_SIZE` (4), the director stands in.
2. Convene the refinement at the Command Center. Each team has its own pending
   slot, so teams do not block each other.
3. For each request, a JEV accept/reject decision decides whether it becomes a
   story.
4. Accepted requests become work-queue stories. Rejected requests are resolved
   with a reason.
5. A refined meeting may be queued for follow-up.
6. Rolled-over cards are reassigned so they do not strand.
7. The ceremony marker advances.

**Controls.** JEV decision, request cap, facilitator availability, active-sprint
guard, per-team slot.

**Records.** `pendingRefinements`, `backlogRequests`, `workQueue`, action log,
decision tape.

### BP-04 Large-request breakdown

**Purpose.** Turn a large, staffable ask into sized stories and spikes.

**Trigger.** An authority files a large request and the receiving team is free,
or the request arrives through the big-task path.

**Steps.**

1. `file_large_request` records the goal, constraints, and requesting authority.
2. The team's scrum master and workers convene a breakdown ceremony.
3. The ceremony produces stories and spikes with room, title, and estimate.
4. Items are queued. A team in an active sprint defers the breakdown until free.
5. Up to 3 requests are handled per pass (`BREAKDOWN_MAX_REQUESTS`).

**Controls.** Scrum master gate, active-sprint guard, request cap, JEV decider.

**Records.** `_pendingBreakdown`, work items, decision tape.

### BP-05 New-team spawn

**Purpose.** Add capacity when a large request cannot be staffed by existing
teams.

**Trigger.** `intent_assign_big_task` arrives while every existing team is busy
in an active sprint.

**Steps.**

1. Estimate headcount from request word count and scope signals.
2. Choose a new director name and team name from the name pool.
3. Create the director, team record, and employees. Respect the total agent cap
   (`MAX_TOTAL_AGENTS = 500`).
4. File the goal to the new team and kick refinement immediately.
5. Run onboarding for each new agent.

**Controls.** Agent cap, name uniqueness, idle gate, admin authority.

**Records.** Roster, teams, pending onboarding, action log.

### BP-06 Sprint lifecycle

**Purpose.** Give a team a bounded delivery window with measured output.

**Create.**

- `intent_sprint` requires a scrum master for teams at or above size 4. A small
  team may use the director as stand-in.
- Items carry title, room, and an optional size estimate (S=1, M=2, L=3).
- `queue_sprint` writes the sprint record and queues its items.

**Execute.**

- Assignment pulls items for the team's active sprint.
- Progress is derived by matching completed task (title, room) pairs against
  sprint items. No separate progress counter can drift.

**Close.**

- The sprint auto-closes when every item is done and every gated item is
  peer-approved.
- `close_sprint` records landed, total, percent, rolled-over count, points
  landed, points total, and elapsed time.
- The velocity record is appended to a rolling window capped at 20.
- Unfinished items roll into the next sprint.
- A retrospective is scheduled for a later pass.
- Re-closing a closed sprint is idempotent.

**Controls.** Scrum master gate, item cap, auto-close derivation, velocity
window, idempotent close.

**Records.** `sprints`, `teamVelocity`, task archive, retrospective pending
record.

### BP-07 Assignment and on-call

**Purpose.** Match due work to a free agent without double-booking or spending
on gated work.

**Due checks.**

- `notBefore` must have passed.
- `dependsOn` / `depends_on_task` must be `done` in the durable task mirror.
- Tier throttling may defer low-value work.
- Reading-lane work waits for its lane to be free.
- A team in an active sprint gets its own items first.
- Direct-routed items go to the named agent.
- A bug goes to the on-call.

**Assignment.**

1. Pick the next due item with priority normalization.
2. Prefer an on-duty, free agent. Wake an off-duty agent only when the policy
   allows it.
3. Create the task with room, instructions, project label, task type, budget,
   and access.
4. The agent walks to the room. Arrival marks `working` and sets `workUntil`.

**On-call.**

- Deterministic rotation per team, seeded by sprint id and a sprint counter.
- Without a sprint, the slot shifts by UTC day.
- The pager prefers an available member, then the next backup in rotation.
- A backup who actually served a sprint is rotated to the end at sprint close.
- If the whole team is unavailable, the primary slot is named anyway; assignment
  re-derives or falls back.

**Controls.** Idle gate, dependency gate, tier throttle, availability, on-call
rotation, direct route.

**Records.** `workQueue`, `tasks`, agent state, action log.

### BP-08 Task execution

**Purpose.** Produce the deliverable in the room that owns the work.

**Trigger.** Agent arrival at the task room.

**Steps.**

1. The dispatcher selects an executor by room and task flags.
2. The executor builds context: task instructions, perception, wiki pages for
   the room and village, design taste, library search, sandbox state, charter
   notes, coaching notes, runbook notes, refocus notes.
3. The executor calls a model through the tier gate and the Bank.
4. Tool use runs inside the sandbox or through a gated server tool.
5. The result is applied:
   - success -> deliverable or review result
   - failure -> send-back, retry, or failed card
   - budget exhaustion -> failed card plus director notice
   - timeout -> grace, then failure
6. Completion may file a follow-up, free a spike week, emit signals, update
   morale, and note completed rooms.

**Executor families.**

| Executor | Work |
|---|---|
| Research | Crawl, search, synthesize, update a skill file, record evidence |
| Coding | Write code in a sandbox, run the quality pipeline |
| Review | Review a deliverable against requirements and evidence |
| Media | Feed and media digest work |
| Observatory | Weather and observatory work |
| Bank | Teller and budget review |
| Distill | Fold archived findings into the wiki |
| Skill review | Review pending skill files |
| Pipeline step | Run one ordered pipeline step |
| Product build | Build and release a product revision |
| Spike | Time-boxed investigation with a tool loop |

**Controls.** Content timeout and grace, work budget, task budget, tier gate,
sandbox isolation, tool allowlist, JEV command gate.

**Records.** Task result, content run state, action log, spend ledger, decision
tape.

### BP-09 Peer review

**Purpose.** Make a deliverable survive hostile review before it ships.

**Scope.** Deliverable rooms only. Exempt lanes: `spike`, `bug`, `research`,
`meeting`, plus `skillReview` and `distill` sweeps.

**Steps.**

1. The primary work completes. The task enters `needs_review`.
2. Pick two reviewers. Prefer same-team members or agents who have completed
   work in the target room. Widen to all non-admin agents if needed. Never the
   author, never a duplicate.
3. Reviewer 1 is the critic. Reviewer 2 keeps the standard skeptical frame.
4. Each reviewer gets a mailbox request and a review task.
5. The gate closes at 2 approvals, or 1 approval plus a 15-minute timeout.
6. A rejection sends the work back to the author as a fix task. The prior
   reviewer pair is preferred for the re-review.
7. The cycle count carries across re-entries. At 8 cycles the gate freezes and
   escalates.
8. A stuck gate sweep runs every 20 seconds. It re-picks a reachable reviewer
   pair. Re-picks are spaced by a 30-minute rescue cooldown.

**Controls.** Reviewer selection rules, critic framing, approval threshold,
timeout, cycle cap, stuck-gate sweep, reachability checks.

**Records.** `_peerGate`, review tasks, mailbox entries, action log.

**Review note.** The timeout means one clean vote is enough after 15 minutes.
On a quiet tank, that may become the normal path rather than the exception.
Confirm this matches the intended quality bar.

### BP-10 Failure handling and send-back

**Purpose.** Turn a rejection into a fix and a reusable rule.

**Steps.**

1. A reviewer or quality check rejects the work.
2. Classify the failure into one bucket:
   - `factual_error`: a number or claim is wrong or unsourced.
   - `client_preference`: contradicts a stated owner or client preference.
   - `missing_information`: a required fact or field is missing.
   - `style`: style, tone, or wording.
3. Classification runs deterministic rules first, then JEV. A failed or
   low-confidence call falls back to `style`.
4. Append the classified failure to the bounded failure ledger.
5. Send the work back with a note. The note enters the author's feedback buffer.
6. Re-queue the fix. Re-arm the gate.
7. Repeated cycles escalate rather than loop forever.

**Controls.** Four-bucket taxonomy, deterministic-first classification,
fail-closed label, bounded ledger, review cycle cap, escalation.

**Records.** `failures.json`, feedback buffer, fix task, decision tape.

### BP-11 Completion, report, and feedback

**Purpose.** Close the card, tell the owner, and feed the result back into the
next task.

**Steps.**

1. `finish_task` clears the agent, increments approved and weekly counts, marks
   the task `done`, and clears dependency blocks keyed on it.
2. Per-story capability grants are revoked.
3. The completed room is noted for future reviewer selection.
4. The task is archived. The state blob keeps its shape.
5. A card report goes to the player inbox and out through email.
6. The owner may rate 1 to 5 and/or comment, only on a `card_report`.
7. The rating and comment are stamped on the inbox message and appended to the
   completing agent's feedback buffer.
8. The agent carries the feedback into its next task.

**Controls.** Player-only feedback, card-report-only feedback, one report per
card, bounded feedback buffer.

**Records.** `task_archive`, `playerInbox`, email outbox, feedback buffer.

### BP-12 Product build and release

**Purpose.** Turn work into a versioned, releasable product.

**Steps.**

1. Create a product record with id, name, summary, spec, owner, sandbox, team,
   contributors, and handles. Status starts `draft`.
2. A product build task runs in the product sandbox.
3. The quality pipeline and review gate run.
4. On success, `next_product_revision` allocates the next version path.
5. The sandbox snapshot is written to `library/projects/<id>/v<n>`.
6. `product_release_record` appends the revision and flips status to
   `released`.
7. Status may move between `draft`, `in_progress`, and `review` before release.
   `released` is only reachable through the release record.

**Controls.** Sandbox validation, quality pipeline, peer review, release record
after the filesystem write, status transition rules.

**Records.** `products`, Library project snapshot, action log.

## 5. Planning and governance

### BP-13 Roadmap recompute

**Trigger.** Weekly stamp.

**Steps.**

1. For each valued queue room, compute the trailing grade, recent delivery
   demand, and charter alignment.
2. Raise priority when the grade is below the floor, when the room is starved,
   or when the room is on the charter spine.
3. A fresh room with no history defaults to aligned or zero.
4. Write the roadmap map. No model call.

**Output.** `roadmap` per room with priority, owner, last grade, demand, and
charter alignment.

### BP-14 Consensus relay

**Trigger.** Weekly stamp, after the roadmap.

**Steps.**

1. Derive the one-line current direction from the freshly recomputed roadmap.
2. Store it as the consensus relay.
3. The next cycle reads it first.

**Output.** `consensusRelay` line.

### BP-15 Retrospective and velocity

**Trigger.** A sprint closes.

**Steps.**

1. Select the scrum master and attendees. The director is excluded.
2. Convene the retrospective on a later pass.
3. Present the measured velocity snapshot.
4. Capture START, STOP, and CONTINUE notes.
5. Record the outcome.

**Controls.** Scrum master gate, director exclusion, velocity snapshot, JEV or
injectable decider.

**Output.** Retrospective record, notes.

### BP-16 Knowledge social

**Trigger.** Weekly stamp.

**Steps.**

1. Select attendees.
2. Convene the social.
3. Each agent produces a carryaway: what it will take from the session.
4. Restore agents to their prior state after the social.
5. A busy agent may be suspended for a scheduled social and resumed afterward.

**Controls.** Attendee selection, duration, restore path, JEV carryaway
decider.

**Output.** Social record, carryaway notes.

### BP-17 Meetings

**Trigger.** A queued meeting.

**Steps.**

1. Resolve attendees from the request, the team, and the facilitator.
2. Walk attendees to the room.
3. Run the meeting for its duration.
4. Record the meeting and any outcome.
5. Release attendees.

**Controls.** Attendee resolution, room capacity, duration, facilitator.

**Output.** Meeting record.

### BP-18 Charter

**Trigger.** The owner or a director sets a charter.

**Steps.**

1. Store the goal, interests, and notes.
2. Derive room alignment from the charter.
3. Inject the charter note into task instructions.
4. Use alignment in the roadmap.

**Controls.** Authority check, state-only write.

**Output.** `charter`, charter note, roadmap alignment.

### BP-19 Hiring and onboarding

**Purpose.** Add an agent when the tank has work and a team needs capacity.

**Trigger.** Governance pass, cooldown 45 seconds, idle gate passed.

**Steps.**

1. Stop at the total agent cap (`MAX_TOTAL_AGENTS = 500`) or the active-agent
   ceiling (`MAX_ACTIVE_AGENTS = 25`). A dormant worker who will be woken by due
   work still counts against the ceiling.
2. Try cross-team borrowing first. If an inactive agent can be borrowed for a
   team's sprint, use that before hiring a new agent.
3. For each free, on-duty team director, build a candidate list from that
   director's own reports.
4. Ask JEV to pick which member needs overflow help. A failed call falls back
   to the governance pick.
5. Create a pending hire with an 8-second deferral.
6. Complete the hire: the admin names the new director, or the team director
   names the employee, using the all-time unique name pool. Spawn the agent and
   start onboarding.
7. Onboarding convenes a Town Hall meeting at the Command Center with the
   director and intended coworkers. Everyone must be free and on duty. If not,
   defer and retry.
8. Stage `AGENTS.md` progressively (stages 1 to 3) after the 10-second meeting.
9. The new agent signs a work agreement.
10. Readiness times out after 15 minutes and completes onboarding on the
    fallback path.

**Controls.** Total cap, active ceiling, dormant-wake check, borrowing first,
director availability, cooldown, idle gate, name pool, JEV decider, onboarding
readiness timeout.

**Records.** `_pendingHire`, `_pendingOnboard`, roster, agents, action log.

**Review note.** Hiring is single-slot. Only one pending hire and one pending
onboarding exist at a time. That is a deliberate pacing choice, but it also
means a burst of need is served one agent at a time.

### BP-20 Firing review

**Purpose.** Remove an agent only when a corroborated problem exists.

**Trigger.** Governance pass, cooldown 60 seconds, idle gate passed.

**Signal.**

- Two serious or severe negative reports inside the 14-day strike window, or
  one severe report.
- Or a true drop-off: dropped work greater than 30 percent of approved work.

A single negative report is not a signal. Low morale alone is not a signal.

**Steps.**

1. Pick the worker with the strongest signal. Never pick an agent mid-task,
   mid-pair, or mid-handoff.
2. Confirm both reviewers exist, are on duty, and are free: the admin and the
   senior-most director.
3. Convene the review at the Command Center.
4. Gather consultation: reporters and coworkers with firsthand knowledge.
5. Ask JEV to decide fire or keep. A failed call falls back to the deterministic
   rule: a corroborated report plus a real drop-off means fire; otherwise keep.
6. On fire, revoke the agent's credentials, grants, and experiment tokens.
7. Record the outcome.

**Controls.** Strike window, severity weights, drop-off threshold, idle guard,
two-reviewer gate, consultation, JEV with deterministic fallback.

**Records.** `_pendingFiringReview`, reports, governance log, action log.

### BP-21 Coaching and growth

**Trigger.** A task's trailing grade falls below the floor.

**Steps.**

1. Grade the completed task.
2. If below the floor, write a coaching plan for the agent and room.
3. Inject the coaching note into the agent's next task in that room.
4. Run a daily catch-up sweep. Re-check the trailing grade.
5. Re-coach or escalate. The loop is capped at 3 rounds.
6. Repeated problems produce a growth plan and an escalation.

**Controls.** Grade floor, round cap, daily cadence, bounded notes.

**Records.** `growthPlans`, coaching notes, action log.

### BP-22 Rule mining

**Trigger.** Weekly stamp.

**Steps.**

1. Group the failure ledger by type and rule hint.
2. Any pattern recurring at least twice becomes a proposed rule.
3. The proposal carries rule text and a test fixture: the offending input and
   the requirement it missed.
4. Proposals are capped.
5. Nothing is auto-applied. The operator encodes the rule and pastes the fixture
   into a conformance test.

**Controls.** Recurrence threshold, proposal cap, operator-only application.

**Records.** `failures.json`, rule proposals, governance log.

### BP-23 Success mining

**Trigger.** Weekly stamp.

**Steps.**

1. Group recurring high-graded deliverables from the success ledger.
2. Emit operator success-lesson proposals.
3. The operator decides whether to encode the lesson as a standard.

**Controls.** Weekly cadence, proposal surface, operator-only application.

**Records.** Success ledger, proposals, governance log.

### BP-24 Archive distillation

**Trigger.** Weekly stamp.

**Steps.**

1. Read the archived decision tape.
2. Fold the decisions into a short decision-archive wiki page.
3. Write through the server-authority path as actor `distill`.
4. A database or write failure skips the week without blocking the cycle.

**Output.** Wiki page, provenance row.

### BP-25 Adversarial village

**Purpose.** Run two independent attempts at the same goal and compare.

**Trigger.** The owner launches it.

**Steps.**

1. Spawn a winter team with its own director.
2. File the same goal to the main team and the winter director, tagged with one
   shared adversarial task id.
3. Both sides work independently.
4. When both sides have delivered, mark the run complete and disable the winter
   village.
5. The owner may disable it early.

**Controls.** Player-only launch, agent cap, name pool, shared task id,
completion check.

**Records.** `adversarialVillage`, roster, tasks, action log.

## 6. Scheduling and standing operations

### BP-26 Research topics and watchlist

**Trigger.** Owner schedule lane, watchlist seed, or a long-horizon topic.

**Steps.**

1. Sync `researchTopics` from `watchlist.json`.
2. Seed the long-horizon topic for a village.
3. On cadence, check the dependency gate. A gated topic does not advance its
   cadence marker.
4. Stamp `lastRunAt` before assignment.
5. Queue a research card in the observatory with a 24-hour overlap window.
6. The research executor runs three passes, records evidence, updates the skill
   file, and records coverage as `pending_review`.
7. A failed run records a failure. It never suppresses the next attempt.

**Controls.** URL validation, 5-minute cadence floor, dependency gate, 24-hour
overlap, review-gated coverage, bounded seen-URL list.

**Records.** `researchTopics`, work item, evidence ledger, coverage ledger.

### BP-27 Standing tasks

**Trigger.** A declaration in `state['standingTasks']` becomes due.

**Steps.**

1. Build a `StandingTask` from the declaration: mode, agent count, duration,
   window, days, day of month, yearly date, occurrence caps, consecutive caps,
   total caps, team.
2. A malformed declaration is skipped. One bad record never wedges the sweep.
3. On `start`, queue a real card with title, room, instructions, standing task
   id, team, and knowledge class.
4. On `stop`, log the stop. Continue and idle are silent.

**Controls.** Declaration validation, per-task runtime state, bounded caps.

**Records.** `standingTasks`, work items, governance log.

### BP-28 Pipelines

**Trigger.** Pipeline cadence due.

**Steps.**

1. A pipeline is a named, ordered list of steps.
2. Each step has title, room, optional offset, instructions, tool, and args.
3. The run starts only when its dependency task is done.
4. Steps fire in strict order. A step waits for its predecessor to complete,
   then applies its own minimum offset.
5. `runId` disambiguates the same step index across runs.
6. Cadence floor is 1 hour.

**Controls.** Ordered execution, dependency gate, cadence floor, step
validation.

**Records.** `pipelines`, work items, run state.

### BP-29 One-off schedules

**Trigger.** Owner names a day and time.

**Steps.**

1. Parse ISO 8601 with an offset, or an epoch timestamp.
2. Naive times are treated as UTC.
3. Reject past times.
4. Reject more than 366 days ahead.
5. Queue one item with `notBefore`.
6. The item enters the normal lifecycle exactly once.
7. An optional dependency delays assignment until the dependency lands.

**Controls.** Parse validation, past-time rejection, horizon cap, dependency
gate, idle gate.

**Records.** Work item, action log, passport decision.

### BP-30 Skill review and distillation

**Trigger.** 30-minute cadence, content-gated.

**Steps.**

1. Skill review queues only when `pending_review/skills/` has content and no
   review is in flight.
2. The observatory reviews each pending skill file for accuracy and value.
3. Distillation queues only when new archives exist since the last pass.
4. Distillation folds findings into the wiki.
5. Neither sweep enters the peer gate. A rejected sweep would only spawn a fix
   for work that is not a normal authored deliverable.

**Controls.** Content gate, in-flight gate, cadence marker held when empty.

**Records.** Work items, wiki pages, action log.

### BP-31 Incidents and bugs

**Trigger.** The owner reports a live failure through the Telegram lane or the
intent endpoint.

**Steps.**

1. Resolve the product. If it cannot be resolved, fall back to a spike.
2. `queue_bug` checks the owning team, on-call availability, and duplicate open
   bugs.
3. The bug is assigned to the on-call.
4. The team restores service.
5. An open incident past the restore window escalates to the product team's
   scrum master.
6. Bugs are not peer-gated. They complete directly.

**Controls.** Product resolution, on-call rotation, duplicate check, restore
window, escalation.

**Records.** Issue or task, on-call state, escalation record, action log.

**Review note.** Incident fixes skip the peer gate by design. Confirm whether a
restored incident should require a post-fix review before closure.

### BP-32 Page probes, screenshots, and browser act

**Trigger.** Agent tool call.

**Steps.**

1. Screenshot: fetch a URL through the SSRF-gated path and capture an image.
2. Page probe: open a page in a controlled browser, apply wait and action
   options, capture the result.
3. Browser act: run a scripted browser action inside a sandbox. Inputs and
   credentials are validated. The result and screenshot stay in the sandbox
   mount.
4. External content is wrapped before it reaches a model.

**Controls.** SSRF host check, redirect re-check, allowlist, credential
validation, sandbox isolation, timeout, prompt-injection wrapper.

**Records.** Result file, screenshot path, action log, decision tape.

### BP-33 Device check-in

**Purpose.** Give the owner's phone a durable ingestion pipe for ambient
context, with no fixed schema, so a future signal never needs the endpoint
redesigned.

**Trigger.** An iOS Shortcut posts to `/api/device/checkin`.

**Steps.**

1. The endpoint authenticates itself: the request must carry `X-Device-Key`
   equal to `DEVICE_API_KEY`, compared in constant time. It accepts no player
   session and no agent key, so it is exempt from the deny-by-default
   middleware and guards itself instead.
2. The body is an open-ended dict. Every field is optional. `location`, when
   present, must be `{lat, lon}` with numeric values. An empty body is
   rejected.
3. `record_device_checkin` stores the entry: `None` values are dropped, the
   entry is stamped with `receivedAt`, `last` is replaced, and the entry is
   appended to a bounded history. The cap is `deviceCheckinHistoryMax`
   (default 50, operator-settable 1 to 10,000) because the bucket lives in
   the hot state blob and must never grow unbounded.
4. The endpoint is not gated by dormancy, and it does not wake a dormant
   tank either: it is pure telemetry, in the non-waking write set (BP-59). A
   check-in lands while the tank sleeps and the tank stays asleep.
5. An action log row records the check-in trigger and whether a location was
   present. No consumer reads the history yet. The ingestion pipe is all
   that exists today.

**Controls.** Device key, constant-time compare, field validation, bounded
history, dormancy exemption.

**Records.** `deviceCheckins` (`last` plus bounded `history`), action log.

**Review note.** There is no consumer yet. Decide what the first consumer
should be before the buffer fills with data nobody reads.

## 7. Knowledge and evidence

### BP-34 Wiki lifecycle

**Purpose.** Keep shared memory versioned, scoped, and reviewable.

**Write paths.**

- A director or admin writes directly.
- An agent proposes a page. A director or admin approves it. Approval promotes
  the proposal into the live wiki with a version bump and history.
- Server processes write through the same path with a system actor.

**Page metadata.**

| Field | Meaning |
|---|---|
| `id`, `title`, `category` | Identity and category. Unknown categories are rejected. |
| `villageId` | Identity boundary. Readers only see their own village. |
| `owner` | Who is accountable for the claim. |
| `clock` | `slow` or `fast`. Fast pages must be re-fetched, not trusted. |
| `scope` | Who may reuse it. |
| `source` | Where the claim can be verified. |
| `reviewAfterMs` | Expiry. Expired pages are not injected. |
| `version`, `history` | Append-only edit history. |

**Read path.**

1. Select pages by category-room affinity.
2. Filter to the reader's village.
3. Skip expired pages.
4. Inject at most 3 pages into the task context.
5. A fast-clock page is flagged for re-fetch rather than trusted.

**Controls.** Category validation, body size cap, village filter, expiry,
provenance, approval gate for proposals.

**Records.** Wiki metadata in state, body on disk, history, provenance ledger.

### BP-35 Memory provenance

**Trigger.** Every shared-memory write.

**Steps.**

1. Resolve owner, clock, scope, source, and review-after.
2. Record a ledger row with actor, page, claim, source, owner, scope, and review
   date.
3. Expose the ledger to the operator.

**Output.** `memoryLedger` row.

### BP-36 Evidence and coverage review

**Purpose.** Separate "we found something" from "we can say it is true."

**Claim lifecycle.**

```
needs_review -> cleared
             -> blocked
```

**Coverage lifecycle.**

```
pending_review -> covered
               -> blocked
failed (never suppresses the next attempt)
```

**Steps.**

1. A research run records each claim with the source URL as found, the resolved
   landing URL, claim type, scope, and status `needs_review`.
2. Identity comes from the resolved landing URL. A short link is never an
   identity.
3. A second record on the same identity is a repost: `independent=false`,
   `repost_of=<first id>`. A repost never counts as a second source.
4. A short link with no resolved URL becomes `unresolved:<hash>`, marked not
   resolved, with an open question. It is never a verified source.
5. Coverage is marked `pending_review`, not covered.
6. A Verifier reviews the claim:
   - `usable_as_written` or `usable_with_narrower_wording` clears the claim and
     flips its identity to covered.
   - `blocked_until_checked` blocks the claim and never marks coverage.
7. `reviewed=True` on the coverage endpoint is player-session-only. An agent
   key can only record pending or failed.
8. Evidence review accepts the player or a roster director/admin. An ordinary
   agent gets 403 even with a valid key.
9. Agent verifiers are capped at 12 covered flips per 6 hours.
10. Every mutation appends an immutable audit row.
11. Recurring runs search from the last successful run minus 24 hours, so a
    late discovery is not missed.

**Controls.** Resolved identity, repost dedup, review gate, role gate, rate cap,
immutable audit, overlap window, failure never suppresses.

**Records.** `evidence_records`, `coverage_ledger`, `evidence_audit`.

### BP-37 Research desk passes

**Trigger.** Every scheduled research run.

**Steps.**

1. Run three search passes:
   - announcements: what changed
   - practical examples: someone actually using it, with inspectable material
   - limitations: documented restrictions, corrections, availability problems
2. Separate what a source supports from the think tank's inference.
3. Record before/after observations for each detected change.
4. Fold the research desk operations numbers into the summary.
5. A repost is not treated as a second independent source.
6. Do not manufacture a finding when there is none.

**Output.** Synthesis, skill file update, evidence rows, observation rows.

### BP-38 Library and file access

**Library.**

- A shared tree of skills, archive, wiki, projects, design references, and
  downloads.
- Writes are namespaced by village. Main stays flat. A non-main agent's commons
  write lands under `villages/<vid>/`.
- Admin and player cross the boundary.
- Reads are filtered by the requester's village scope.
- List, read, write, search, download, and promote are separate operations.
- Uploads and archive promotion are gated.

**Player files.**

- The owner issues a grant: scope root, label, and capabilities (read, write,
  delete).
- Agents get a `local_file` tool. Every call is checked against a grant.
- Containment: realpath must stay inside the scope root. `..`, symlink escapes,
  and absolute outside paths are refused.
- Trash: `~/.Trash` is never a target. Deletes move files into the trash.
  Emptying the trash is never grantable.
- Notes: existing notes are read-only. An agent may create a note, or modify
  and delete a note it created. Ownership is tracked per note.

**Controls.** Village scope, grant capability, containment, trash rule, Notes
ownership, player-only grant API.

**Records.** Library files, `fileGrants`, `noteOwnership`, action log.

### BP-39 Skill publishing

**Trigger.** A spike or review produces a reusable method.

**Steps.**

1. Build a skill card: slug, title, source task, team, interface, body.
2. Write the card to `pending_review/skills/`.
3. The skill review sweep checks accuracy and value.
4. A publishable skill is moved into the Library.
5. The skill becomes available to later tasks.

**Controls.** Pending review, review sweep, Library scope.

**Records.** Skill file, review outcome, action log.

### BP-40 Conversation memory

**Scope.** The Telegram bridge only. The public ask endpoint stays stateless.

**STM.**

1. Read memory context before storing the current turn, so the current question
   is never in its own history.
2. Inject recent turns and a rolling summary into the prompt.
3. After the reply, store both turns.
4. When turn or token budgets are exceeded, prune the oldest turns and fold them
   into the rolling summary.
5. A rule-based recap is written immediately. An LLM summary may upgrade it in
   the background.

**LTM.**

1. Distill typed memories at a bounded cadence: preference, fact, decision,
   unresolved question.
2. Each memory carries confidence and source type.
3. Recall scores keyword overlap, then applies trust and staleness.
4. A bad extraction parses to an empty list. Never fabricate.
5. Deduplicate by content.
6. Cap memories per session.

**Safety.**

- Every persisted memory write is scrubbed for secrets. A secret becomes
  `[REDACTED:TYPE]`.
- Recall drops low-confidence and old memories.
- User-stated memories outrank inferred ones.
- Near-duplicate memories are periodically consolidated.

**Controls.** Turn and token budgets, extraction cadence, confidence filter, age
filter, redaction, consolidation cap, TTL.

**Records.** `stm_sessions`, `stm_messages`, `ltm_memories`.

### BP-41 Decision cache and tape

**Cache.**

- Key: model, configured chain, state, questions.
- An exact repeat is served with no network call and no spend.
- TTL is bounded. Zero disables the cache.
- A cached decision is still taped, marked cached.

**Tape.**

- Every JEV decision is recorded with prompt, candidates, choice, confidence,
  cost, and raw response.
- Each row is hash-chained with HMAC-SHA256 over the previous hash and the
  canonical row.
- `init_db` backfills legacy rows in id order and never re-signs a hashed row.
- Verification reports hash mismatch, chain break, or unhashed row.
- The first row anchors to the pruned past and is exempt.
- Retention pruning distills old rows into `decision_archive` with the source
  tape hash.

**Controls.** TTL, exact key, hash chain, verify endpoint, archive cross-check.

**Records.** `decision_cache`, `decision_tape`, `decision_archive`,
`decision_key_stats`.

## 8. Money and resources

### BP-42 Bank ledger and budget gates

**Purpose.** Attribute every dollar to a service and stop spend at the cap.

**Steps.**

1. Every real model call accrues cost once, at the choke point.
2. Cost lands in a service bucket. Optional dimensions: village, agent, task.
3. Per-service caps come from a product record when present, otherwise the
   default cap.
4. The Bank view shows used, cap, calls, last activity, trailing 7-day burn, and
   days to cap.
5. Before a call, the gates check:
   - page request budget
   - dollar spend cap
   - agent monthly cap, when configured
   - per-task budget
   - tank cap
   - service cap
6. A refused call fails closed. There is no bypass from inside the tank.
7. The spend cap baseline is stored in the ledger, so spend from before the cap
   was set does not count.

**Controls.** Single accrual point, per-service cap, baseline, fail-closed gates,
forecast.

**Records.** `kv_spend`, per-day series, task spend.

### BP-43 Model tier selection

**Bands.**

| Band | Benchmark | Purpose |
|---|---|---|
| Low | MMLU | Default for everything |
| Sub-low | MMLU intelligence index | Short bounded test scenarios, hard price cap |
| Mid | MMLU-Pro | Lightly gated reasoning |
| High | HLE | Heavily gated planning, price ceiling and monthly cap |
| Coding | SWE-bench | Deterministic for code, review, QA |
| Vision | MMMU | Deterministic for image reading |

**Selection logic.**

1. Keep only models with a cited benchmark score.
2. Keep models within 6 points of the best score.
3. Pick the cheapest qualifying model.
4. Verify with a real test call before writing the tier.
5. The high band also enforces a price ceiling and a monthly budget.
6. The sub-low band enforces a hard price ceiling.

**Per-call gate.**

- Coding task type -> coding tier, no JEV.
- Vision work -> vision tier.
- Everything else -> JEV chooses low, mid, or high, with confidence floors.
- High requires both confidence and budget headroom.
- An outage or low confidence fails closed to low.

**Refresh.** A full re-pick of every band every 2 hours, as a governance task.
A cheaper price watch runs every 30 minutes and checks only the selected
tiers' catalog prices; a moved or vanished price triggers a full re-pick
within about 30 minutes instead of at the next scheduled run. A real
availability error on a live call queues an out-of-band re-pick that drains
within a minute, so the band lands on a working model without waiting for
either cadence.

**Controls.** Cited scores, quality floor, cheapest pick, test call, price
ceilings, monthly cap, JEV gate, fail-closed.

**Records.** `model_tiers`, `model_benchmark_scores`, decision tape.

### BP-44 Experiment grants

**Purpose.** Let a notebook spend on a fixed model without holding the
OpenRouter key.

**Lanes.** Sub-low lane and JEV lane. They are separate and never cross.

**Steps.**

1. Mint: owner or proven director/admin. Require a positive estimate. Refuse
   when the model is unavailable, the shared monthly cap is hit, or the estimate
   exceeds the agent's remaining headroom. Return the token once. Store only its
   SHA-256.
2. Quote: show the model, blended price, per-call and total estimate, agent
   spend, headroom, and tank cap.
3. Inference: authenticate with the grant token only. The model is fixed
   server-side. Re-check the burn-down estimate and every Bank gate. Refuse with
   429 when actual cost reaches the estimate.
4. Revoke: delete by id or token.
5. Expired grants are swept. Grants die with their agent on a fire.

**Controls.** Estimate, TTL, per-call token cap, shared cap, agent headroom,
actual-cost cutoff, token hash storage, revocation.

**Records.** Grant rows, spend ledger buckets, action log.

### BP-45 Task budgets and overruns

**Steps.**

1. Every task gets a model-spend ceiling from its type and band.
2. Every call checks the task budget.
3. When the ceiling is crossed, the card closes as failed and is marked
   `budgetExhausted`.
4. The owning director gets a non-disruptive mailbox note with budget, actual
   spend, attempts, cause, work landed, and the future budget factor.
5. The director may grant more budget and reopen the card. The new ceiling must
   exceed what was already spent.
6. The failed record is kept and stamped with the raised budget. The reopened
   card is a fresh task pinned to the original author.
7. Overrun history raises future budgets for similar work.

**Controls.** Per-task ceiling, one-time notice, reopen validation, overrun
factor.

**Records.** Task budget fields, spend rows, mailbox note, governance log.

### BP-46 Page request budget

**Steps.**

1. Each browse fetch and each Tavily search counts as one page request.
2. Track monthly usage.
3. When the allowance is spent, browsing and search are refused with a clear
   message.

**Controls.** Monthly cap, counted at the call site, fail-closed.

**Records.** Page request ledger.

## 9. Security, risk, and human oversight

This section covers the controls that bound what agents may do, and the
oversight loops that catch what slips past.

### BP-47 JEV decision gates

**Purpose.** Put every consequence-bearing choice through one typed classifier.

**Steps.**

1. Build instructions and typed criteria.
2. Sample up to 3 independent decisions. Majority vote.
3. Parse the choice and calibrated confidence.
4. Apply the decision-specific floor.
5. On outage, low confidence, or a non-binary answer, take the safe path.

**Used by.** Browse allow/block, command allow/block, peer review clean or
actionable, escalation approve/deny, refinement accept/reject, firing
keep/fire, social carryaway, governance picks, model tier gate, Colab shard
gate, request routing, failure classification, player-ask gate.

**Controls.** Quorum, typed choice, confidence floor, fail-closed, decision tape,
cache.

**Records.** Decision tape, action log, spend ledger.

### BP-48 Escalation and delegated approval

**Purpose.** Let a director resolve routine approvals while keeping high-risk
decisions human-only.

**Creation.** A gate that cannot decide, or an action that needs approval,
creates an escalation with kind, question, and note.

**Routing.**

- Risk floor 1.0: human-only. The director loop skips it. The owner gets an
  email link.
- Delegable kinds: the senior-most director decides on the 30-second loop.

**Delegated decision.**

1. Skip kinds whose judge cross-check has tripped the drift circuit.
2. Respect the re-ask cooldown: 15 minutes.
3. Stop after 6 asks and notify the human once.
4. Ask JEV for approve or deny.
5. Compute a composite trust score: confidence minus a per-kind error penalty.
6. Require the per-kind floor and the effective safety confidence.
7. Run a heterogeneous judge cross-check with a different model. The judge sees
   only the original question and criteria, never the primary answer.
8. A judge failure, disagreement, or suspected collusion sends the case to the
   human and bumps drift.
9. On approve, run the kind's side effect. Examples: allowlist grant, API service
   registration.
10. Record the decision.

**Kinds and floors.**

| Kind | Floor | Meaning |
|---|---|---|
| `unsure safety decision` | 0.85 | Above routine |
| `blocked command` | 1.0 | Human only |
| `blocked pipeline step` | 1.0 | Human only |
| `blocked browser action` | 1.0 | Human only |
| `unresolved review requirement` | default | Delegable |
| `allowlist request` | default | Delegable |

**Controls.** Human-only floors, cooldown, attempt cap, composite score,
per-kind floor, judge cross-check, drift circuit (4 hits), fail toward pending.

**Records.** `escalations.json`, action log, decision tape, notification outbox.

**Review note.** Human-only escalations have no time-based SLA. They wait for
the owner. That is deliberate for safety, but the operational backlog should be
visible.

### BP-49 Player input asks

**Purpose.** Let an agent ask the owner only when internal recovery has failed.

**Steps.**

1. The agent files a needs-input request on an issue.
2. The owning team's director reviews it after a short gate (8 seconds).
3. Verdict:
   - `resolve_internally`: drop the block, unblock the agent, file `blocked=false`.
   - `ask_player`: deliver a player-inbox message, block the agent, file
     `blocked=true`.
4. The agent waits. It is not abandoned.
5. The owner answers. The agent unblocks.
6. A terminal issue status supersedes the ask and frees the worker.

**Controls.** Director gate, one ask per issue, terminal-status supersede.

**Records.** `_pendingPlayerAsk`, `playerInbox`, issue `blocked` and
`needsInput`.

**Review note.** `_pendingPlayerAsk` is a single slot. A second issue's ask
replaces the first pending ask. Confirm that serialization is intended, or add a
queue.

### BP-50 Credential vault and capability handles

**Purpose.** Let an agent use a service without ever holding its secret.

**Credential lifecycle.**

1. An agent proposes a credential with name, service, kind, and value.
2. A director, admin, or the player approves or denies.
3. Approved credentials are encrypted with Fernet and stored.
4. Removed credentials become unusable immediately.

**Handle lifecycle.**

1. A handle is minted for one agent, one credential, one purpose, allowed hosts,
   allowed methods, and an expiry.
2. The agent presents the handle.
3. The server resolves it, checks host and method, decrypts the secret in
   process, and injects it into the outbound request.
4. Expired handles are swept. Handles are revoked when the agent is fired or the
   story closes.

**Controls.** Approval gate, encryption, scoped handle, host and method
allowlist, expiry, revocation.

**Records.** `external_credentials`, `capability_handles`, action log.

### BP-51 API service proposals

**Steps.**

1. An agent proposes a service spec.
2. The spec is validated for shape, host, base URL, and path rules.
3. The proposal becomes an escalation.
4. On approval, the note is parsed and re-validated. A forged or edited note
   cannot grant more than the validator allows.
5. The service is upserted as active. This is the only registration path.

**Controls.** Validation, re-validation on approval, escalation, operator
approval.

**Records.** `api_services.json`, escalation, action log.

### BP-52 Browse, command, egress, and VPN

**Browse.**

- SSRF gate before the request.
- Re-check after every redirect.
- Always-block categories from policy.
- Allowlist for agent hosts.
- External content wrapped before it reaches a model.

**Command.**

- JEV allow/block with a safety floor.
- Blocked commands escalate. Human-only.

**Sandbox egress.**

- Dual-homed network: one isolated net, one egress net.
- Egress passes through a proxy restricted to allowlisted hosts.
- Per-task egress grants are written for the sandbox.

**VPN.**

- Exits can be started and stopped.
- A browse call conflicts with an active VPN exit and is refused.
- Status polling and exit inventory are operator-visible.

**Controls.** SSRF, redirect re-check, category policy, allowlist, JEV, isolated
network, egress proxy, VPN conflict check.

**Records.** `browse_policy.json`, egress grants, action log, decision tape.

### BP-53 File and app grants

**Purpose.** Give the owner a working control surface for what agents may
touch on the local machine. BP-38 describes enforcement during tool use; this
process is the grant lifecycle.

**Trigger.** The owner grants, lists, or revokes access.

**Grant families.**

| Family | Shape | Tool it unlocks |
|---|---|---|
| File grant | `scope` (absolute existing directory), `label`, `caps` (`read`, `write`, `delete`) | `local_file`, scoped `read_store` |
| App grant | `bundleId` (dotted macOS bundle id, e.g. `com.apple.Notes`), `label`, `caps` (`use`) | `app_script` against that app |

**Steps.**

1. The owner issues a grant through the player-only `/api/file-grants` or
   `/api/app-grants` endpoints. A file scope must be an absolute path to an
   existing directory, and a duplicate scope is refused (409 by realpath).
2. `emptyTrash` is never accepted. It is not a capability the owner can
   grant, not one the code can lose, and not one the operator JSON can add.
3. Effective grants are the union of live state grants and the
   operator-edited `world/grants.json`, re-read whenever the file changes. An
   entry present means granted; removed means revoked. JSON edits apply
   without a restart or a reload call.
4. Revocation is player-only, by grant id, through `DELETE`. The next tool
   call re-checks the grant, so revocation is immediate.
5. The `local_file` tool is offered to agents only when `FILE_ACCESS_ENABLED`
   and at least one file grant exists. Notes is not special-cased: agents
   drive the Notes app through `app_script` once `com.apple.Notes` is
   granted, and read the Notes SQLite store through `read_store`.
6. Notes ownership is tracked per note in state: existing notes are
   read-only, and an agent may create a note, or modify and delete a note it
   created.

**Controls.** Player-only endpoints, realpath dedup, containment at the call
site, absolute trash rule, per-note ownership, grant union with the live
operator file.

**Records.** `fileGrants` and `appGrants` in state, `world/grants.json`,
`noteOwnership`, action log.

**Trust root.** The owner-issued grant, never the agent. An agent cannot
argue its way into a scope; it can only ask the owner for a wider grant.

### BP-54 Prompt-injection boundaries

**Steps.**

1. Wrap all external content with a nonce and HMAC boundary marker.
2. Mark whether the work context is trusted.
3. Verify the boundary downstream.
4. A broken boundary is treated as untrusted input.

**Controls.** Nonce, HMAC, verification, trust flag.

**Records.** Wrapped content, verification result.

### BP-55 Audit, retention, and backups

**Records.**

- `action_log`: human-readable activity feed, per agent.
- `decision_tape`: raw decisions, hash-chained.
- `decision_archive`: pruned tape rows with their source hash.
- `evidence_audit`: immutable claim and coverage mutations.
- Passport chains: story-level provenance for decisions.

**Retention.**

- Action log and decision tape are pruned every 6 hours.
- Default retention is 7 days.
- At most 50,000 rows are pruned per run.
- Tape rows are distilled into the archive before deletion.
- Sessions expire after 7 days.
- STM sessions expire by TTL. LTM survives.

**Backups.**

- SQLite checkpoint to a backup file.
- Hourly by default.
- Newest 1 kept by default.
- A failed backup never crashes the loop.

**Controls.** Retention window, prune cap, hash chain, archive distillation,
backup prune, session expiry.

**Records.** Log tables, archive, backup files.

**Review note.** The design document says backups run every 5 minutes and keep
24. The code runs hourly and keeps 1. Confirm which policy is intended before
relying on the backup window.

### BP-56 Health monitoring

**Trigger.** Every 300 seconds.

**Steps.**

1. Build a snapshot from existing data. No network call and no model spend.
2. Check the signal groups:
   - work queue: depth, due count, and aging in-flight work
   - security: failed logins, 5 or more in an hour
   - capability: blocked or failed browse, curl, or execute calls, 10 or more
     per action in an hour
   - behavior: one agent making an abnormal volume of tool calls in 15
     minutes, a likely runaway loop or injection-driven churn
   - model tiers: a band with no chosen model
   - JEV: decision failure rate above the health threshold
   - API keys: repeated auth failures for an external service, with the
     service blocked after the streak threshold
   - coordination: ceremony weight versus shipped work, decayed over a week,
     plus self-proposed requests rejected at refinement
   - rework: repeated review loops and a low straight-through rate
   - quality gate: red-pipeline results caught, and any red result that
     escaped to done
   - throughput: assignment-to-finish reliability within the horizon
   - escalations: open or aging escalations
   - bank and budget: services over cap, spend-rate warnings, budget headroom
   - OpenRouter credits, from a separately warmed cache
3. Persist new alerts. Deduplicate by category and severity within the window.
   A severity escalation still pushes.
4. Push warning and critical alerts to email and Telegram. Info stays on the
   dashboard.
5. Write a periodic digest.

**Controls.** Dedup window, severity filter, no network in the snapshot, best
effort delivery.

**Records.** `health_alerts`, digest, action log.

## 10. Communication and lifecycle

This section holds the owner-facing feedback loop, the peer help-signal pass,
and the dormancy lifecycle.

### BP-57 Player feedback

**Purpose.** Let the owner rate delivered work so the rating reaches the exact
agent that produced it.

**Trigger.** The owner rates a completed card report in the player inbox.

**Steps.**

1. The endpoint accepts a rating (integer 1 to 5) and/or a comment. At least
   one is required.
2. It serves only a player session, and only a `card_report` message. Feedback
   on a chat reply or a pending question is rejected.
3. The feedback is stamped onto the inbox message, so the owner sees it in
   place, and appended to the completing agent's feedback buffer as
   `player_feedback`. The agent carries it into its next task.
4. Re-submitting overwrites the previous feedback on the same work item
   instead of stacking a second copy.
5. Player feedback is excluded from the feedback dedup rule. A new comment on
   the same card is new information, not a duplicate notice.
6. An action log row records the rating, the agent, and the task.

**Controls.** Player session gate, card-report-only target, rating range
check, overwrite on resubmit.

**Records.** Inbox message feedback, agent `_feedback` buffer, action log.

### BP-58 Peer help signals

**Purpose.** Notice a worker whose recent activity is out of line and tell that
worker's director, without writing a performance report.

**Trigger.** Peer help-signal pass, every 90 seconds.

**Steps.**

1. Read the last hour of the real action log for every non-director worker.
2. Compare recent activity against the worker's structural record.
3. If the evidence shows a real divergence, raise a help signal to the worker's
   director. The signal asks the director to check in, not to punish.
4. Apply a 40 percent filing chance so signals arrive organically rather than
   every pass.
5. Do not re-signal the same worker inside the 6-hour staleness window.
6. Never write a report from this pass. Help signals do not feed the firing
   review.

**Controls.** One-hour lookback, evidence comparison, filing probability,
staleness dedup, no report write, no firing weight.

**Records.** `helpSignals`, action log, director mailbox.

**Review note.** The pass is a help mechanism, not a performance ledger. Keep
that distinction clear if the signal data is ever surfaced in a review.

### BP-59 Dormancy and wake

**Purpose.** Stop all spend and churn when nobody is around, without giving up
the port, the wake path, or the security sweeps.

**Trigger.** The operator starts the server with `--max-idle-minutes N`. The
flag arms the mechanism. The default is 0, which leaves dormancy off.

**Steps.**

1. Every request touches the idle clock in the no-store middleware, whether
   authenticated or not.
2. An idle-watch loop polls every 30 seconds. When no request has arrived for
   `N` minutes, the flag flips to dormant exactly once per idle streak and the
   transition is logged.
3. While dormant, the sim tick is skipped: no movement, no task cycle, no
   content executors, no model spend. The process stays alive and the port
   stays bound.
4. Housekeeping keeps running while paused: the database checkpoint, and the
   expiry sweeps for capability handles and sub-low and JEV grants. An expired
   capability does not wait for the wake. The sim watchdog also holds its
   staleness escalations while dormant, so idleness is not misread as a dead
   loop.
5. Any non-GET request wakes the tank in the middleware, before the handler
   runs, so the request lands on an already-warm tank.
6. Read-only board and status GETs do not wake, and neither does the device
   check-in poster. `_NON_WAKING_READS` holds `/api/state`, `/api/health`,
   `/api/activity`, `/api/decisions`, `/api/escalation/*` except resolve, and
   similar. `_NON_WAKING_WRITES` holds `/api/device/checkin`, checked before
   the method test. An open dashboard tab cannot defeat dormancy, and neither
   can an automated check-in.
7. The health snapshot reports the dormant flag, so the operator can see the
   state from the outside.

**Controls.** Explicit opt-in flag, single transition per streak, wake
exemption list for read-only traffic, housekeeping independent of dormancy,
watchdog aware of dormancy.

**Records.** Idle and wake log lines, health snapshot `dormant` field,
snapshot files, device check-ins stored around a pause.

**Review note.** A device check-in is a POST. The wake rule checks the
telemetry sets before the method test, so a check-in lands while the tank
sleeps and does not wake it. That matches the endpoint's contract: pure
telemetry never produces work (BP-33).

### BP-60 Code self-edits and dormancy windows

**Purpose.** Give the think tank a governed path to improve its OWN code:
propose a precise splice, pass independent review and sandboxed validation,
and land it only while the tank is asleep, through an operator-approved
change request for a scheduled sleep window.

**Trigger.** An agent calls the `self_edit` tool (propose an edit) or
`schedule_dormant_apply` (file a dormancy-window change request); the player
reverts a landed edit or cancels a window; the idle watcher arms dormancy
with `--max-idle-minutes`.

**Steps.**

1. Proposal contract (everything in `world/selfedit.py`, with its docstring
   as the contract). The agent proposes a find/replace splice on one file
   under `world/`. Protected names are refused outright: `serve.py` and
   every `BUSINESS-PROCESSES.md` in the repo, and the audit tiers below
   add a hard test-gate refusal plus a player-signature tier. No path
   escapes the repository, and `config.py` is refused in v1 (see step 3).
2. Mechanical checks come first: the result must compile, and the find text
   must appear in the file exactly once. A rejected proposal never touches
   the file tree.
3. Binding scan: selfedit raises the module's top-level importers and
   refuses a target imported by any consumer that is not on its tolerated
   list (`jev` tolerates `serve.py` because serve re-reads jev's exports
   after an apply; `sublow` tolerates `jev_grants.py`, reloaded after;
   `config` tolerates `content.py`, `sim.py`, `sublow.py`, `serve.py` --
   the apply chain reloads the three consumer modules and refreshes serve's
   config-import block, so a config.py edit IS allowed, Phase 2,
   2026-10-18).
4. Review gate: the same three-judge JEV quorum the tank uses for
   consequential choices decides the proposal from its summary. A reject,
   or a review transport failure, fails closed.
5. Validator gate: a subprocess quarantine copies the tree and runs
   compileall, flake8 on the changed file, and an import smoke of the
   changed module. A broken candidate cannot reach this step's copy.
6. A proposal that passes all gates is ARMED, not applied: the candidate
   byte file and the original's digest are recorded under
   `world/.selfedit_staging/`, and the row lands in the `self_edit_log`
   table with status `armed`.
7. Apply is DORMANT ONLY. One always-running serve loop
   (`serve._dormancy_window_loop`) polls 30 seconds, and while the tank is
   dormant it calls `selfedit.apply_pass`: it writes the candidate bytes,
   records a backup under `world/.selfedit_backups/`, re-imports the
   module, and marks the row `applied`. If the file drifted since arming,
   the row becomes `conflict` and nothing is written. `jev.py` edits also
   refresh all ten re-exported names in serve after the reload.
8. The tank sleeps through TWO paths: operator idle (`--max-idle-minutes`,
   BP-59), or an approved one-shot dormancy window (step 9). Both end with
   the same wake rule: the next request flips the tank awake.

**Dormancy windows (the change-request process).** An agent requests a
one-shot slot -- a date, an `HH:MM` start at least 5 minutes ahead, and a
duration of 5-240 minutes (default 15). The chain is strict:

1. Conflict scan: the slot is refused with a LIST if any standing-task
   schedule window covers a minute of it, or another pending, approved, or
   fired dormancy window overlaps. The refusal names every colliding item,
   so the agent can pick a different time.
2. Supervisor pre-clearance: the server resolves the agent's supervisor
   (`_supervisor_of`: roster director, else admin, else the player) and a
   JEV quorum judges the request as the supervisor's call -- vague reasons,
   prime-work-hours slots, or oversized durations are rejected.
3. Player approval: the request escalates as a floor-1.0 'dormancy window
   request' kind, which the director loop is barred from; the player's
   email or telegram link is the final, mandatory signature. Approve
   schedules the window; deny schedules nothing.
4. When a window fires, the loop forces the tank dormant so EVERY armed
   edit present applies inside it (each edit already passed its own review
   and validator), and keeps it dormant to the end of the slot. A window
   missed while the server was down is recorded `missed`. The player or
   admin agent can cancel a window before it fires.

**Change windows test what they land (operator decision, 2026-10-18).**

Immediately after applying, the window runs the repo's OWN test gate (the
same `tests/run_all.sh` bar every contributed change must meet) against the
LIVE tree. The outcome splits the window in two:

1. Tests pass: every landed edit stays applied; the window proceeds
   normally and closes `done` when its slot ends.
2. Tests fail: the window opens an in-window REMEDIATION session. Every
   responsible proposer is mailed the failing tail plus the deadline. Until
   the slot ends, they may submit corrective splices (`selfedit_fix`, the
   player's window approval already covers them, so only the mechanical
   checks apply) and rerun the gate on demand (`selfedit_recheck`). This
   remediation traffic reaches the server WITHOUT waking the tank -- the
   wake exemption is narrow: only `/api/selfedit/*` paths, only while a
   session is actually open, and only with a verified agent key. A passing
   rerun recovers the failed edits to `applied` and closes the session.
3. Failures unresolved at the end of the slot: the window ROLLS BACK
   automatically. Each rejected candidate is copied, under ANOTHER FILE
   NAME, to `world/.selfedit_failed/<window>/<edit>-<module>.py`; every
   edit applied in that window is reverted to its pre-window bytes; the
   modules reload; the rows land as `rolled_back`; and each responsible
   agent is mailed to resolve the issues OFF-window and schedule a fresh
   window. Nothing else about the tank is disturbed, and a fresh window
   means a fresh conflict scan and fresh approvals.
4. Applies that land OUTSIDE any window (the idle path) get the same
    post-apply tests. With no window to remediate in, a failure there rolls
    back immediately and mails the agents.

**Surface audit (operator decision, 2026-10-18).** Three tiers now bound
what agents may edit and read, layered on the constant protected names
(`serve.py`, every `BUSINESS-PROCESSES.md`):

1. Tier A -- the test gate (`world/tests/`, `conftest.py`, the run
   scripts): refused outright for edits AND fixes, with no approval path.
   An edit that could rewrite its own acceptance bar proves nothing, so no
   quorum review can legitimize it.
2. Tier B -- oversight-enforcement files (`jev.py`, `selfedit.py`,
   `notify.py`, `theo_policy.py`, `sublow.py`, `jev_grants.py`, `bank.py`,
   `budget.py`): they pass the normal gates, but arm only after a
   floor-1.0 'oversight self-edit' escalation the PLAYER signs. Approve
   arms the waiting edit; deny refuses it. Enforcement code is exactly
   what a corner-cutting agent would want to weaken quietly.
3. Reads -- `selfedit_read` gives bounded read access mirroring the write
   rule, with a SMALLER surface: the full listing and one-file heads (up
   to 48 KB with sha256) for the editable world modules, but `serve.py`,
   `BUSINESS-PROCESSES.md`, the Tier B files, and the test gate stay
   closed. Exact anchors make proposals better; reading the gates or the
   enforcement internals is what gaming would target first.

**Revert.** Only the player session or the admin agent may revert an
APPLIED edit: the pre-edit bytes are restored, the module is re-imported,
and the row is marked `reverted`.

**Controls.** Every gate fails closed; protected names are hard-refused
with no carve-out; the anchor must be unique; the reviewer and validator
each run on untrusted-free paths (quorum, subprocess on a copy); applies
and window firing run only while dormant; both approval signatures are
recorded in the action log; spans of idempotency (two loops never apply the
same row twice).

**Records.** `self_edit_log` and `dormancy_windows` tables,
`world/.selfedit_staging/` candidates, `world/.selfedit_backups/` pre-edit
bytes, decision-tape rows for every model call, action-log rows for
propose/validate/apply/revert/request/resolve/cancel, escalations.json for
window approvals.

**Review note.** The floor-1.0 kind lives in `jev.Escalation floors`
(`jev.ESCALATION_KIND_RISK`), which is itself inside the self-editable
surface: the same review gates apply to it, and since the 2026-10-18
surface audit, edits to `jev.py` additionally need the player's signature
(Tier B). The Phase 2 topological reload for `config.py` (reload
config, then sublow, content, sim, then refresh serve's config-import
names) is LIVE -- it runs only while the tank is dormant, between
simulation ticks, since the engine tick is skipped entirely during
dormancy. The superset design lives in the selfedit.py docstring.

### BP-61 Governed policy knob edits

One of the 107 operator-policy knobs is DATA, not code: the player and the
agents share it, and changing it never needs a restart or a dormancy
window.

1. Scopes. `world/operator_policy.json` is the single source of truth.
   The player edits it or the settings UI; agents read it through the
   selfedit_read surface and change it only through the `policy_edit` tool.
   Secrets stay in `.env`, never in the JSON.
2. Keys. `config._OPERATOR_SCHEMA` owns every key's type and range; the
   in-code defaults keep a fresh clone booting identical. Since Phase 3
   (2026-10-18), all 24 former serve.py statics are policy keys, and the
   loops that consume them read the live value every iteration, so a
   rebind lands on the next sleep.
3. Allowlist. `config.AGENT_EDITABLE_POLICY_KEYS` names the 19 keys an
   agent may propose (tier/mine/memory/browse/peer-review cadences and
   thresholds). The DB/log hygiene keys stay operator-only to keep the
   audit trail's retention shape out of agent hands; the allowlist lives
   in self-editable code, so the tank can widen it through the normal
   pipeline.
4. Review. One `policy_edit` call proposes one key, one value, one reason.
   The same JEV quorum that reviews code splices judges the change
   (approve / reject / escalate). Refuse, unknown key, out-of-range
   value, non-editable key, or transport failure: nothing changes.
5. Apply. On approve, a locked read-merge-write lands the validated value
   in the JSON atomically (temp file + rename), then
   `config.reload_policy_config()` re-applies every binding: serve, sim,
   content, sublow rebind live. The write preserves `_notes` and any keys
   the player added.
6. Records. Every attempt is action-logged (`policy_edit`,
   `policy_edit_rejected`, `policy_edit_review_failed`), the review runs
   through the decision tape like every other JEV call, and the player
   keeps final authority: reverting is editing the JSON back.

**Why no window.** Code changes need quiescence because they re-import
modules mid-tick. A policy value is a number the next loop iteration reads;
the tick boundary is irrelevant and the change is one JSON write.

## 11. Player and operator processes

| Process | Surface | What the owner does |
|---|---|---|
| Login and session | Web UI, `auth.py` | Create the first admin account, log in, hold a session |
| Ask Theo | Telegram, web chat | Ask questions, give direction |
| Approve Theo actions | Telegram | `approve apr_<id>` or `deny apr_<id>` |
| Review escalations | Email link | Approve or deny human-only escalations |
| Answer agent asks | Player inbox | Answer a blocked agent's question |
| Rate delivered work | Player inbox | 1 to 5 stars and a comment on a card report |
| Set policy | `world/*.json` | Edit plain writing, browse policy, research desk passes, watchlist |
| Set operator policy | `world/operator_policy.json`, settings | Tune the 107 policy knobs, including every former serve.py static (cadences, thresholds, caps) |
| Reload policy | `POST /api/policy/reload` | Reload operator policy without a restart |
| Set budgets | `.env`, settings | Spend cap, high-tier cap, page budget, agent cap |
| Switch models | Settings | JEV model and judge model |
| Manage villages | Villages UI | Create a village, assign agents |
| Issue file grants | File grants UI | Grant scoped read, write, delete |
| Review evidence | Evidence UI | Clear or block claims, review coverage |
| Review rule and success proposals | Proposals UI | Accept a proposal by encoding it |
| Launch adversarial village | Adversarial UI | Start or stop a two-team run |
| Inspect health | Health UI, digest | Read alerts and the periodic digest |
| Inspect decisions | Decision verify endpoint | Verify the tape chain |
| Probe pages | Page probe | Inspect a live page before trusting it |
| Manage experiments | Grant endpoints | Mint, quote, revoke sub-low or JEV grants |
| Back up and restore | Backup files | Use the newest snapshot |

## 12. Control framework

| Control | What it protects | Failure behavior | Evidence |
|---|---|---|---|
| Idle gate | Model spend | No work -> no spend | Task cycle early return |
| JEV quorum | Consequence decisions | Outage or low confidence -> safe path | Decision tape |
| Approval gates | Owner authority | Gated action -> pending approval | Theo approval record |
| Spend cap | Total budget | Cap hit -> calls fail closed | Spend ledger |
| Page budget | External fetch cost | Budget spent -> browse and search refused | Page ledger |
| Per-task budget | Runaway task | Ceiling hit -> failed card and director note | Task record |
| Per-service cap | Service overspend | Cap hit -> service gate refuses | Bank view |
| Tier gate | Expensive model use | Low confidence -> low tier | Decision tape |
| High-tier monthly cap | Expensive model use | Cap hit -> mid tier | Spend ledger |
| Peer gate | Deliverable quality | Reject -> fix task and re-review | Peer gate record |
| Review cycle cap | Infinite review loops | 8 cycles -> freeze and escalate | Gate record |
| Evidence review | False claims | Unreviewed -> not covered | Evidence and coverage ledgers |
| Verifier cap | Compromised reviewer | Cap hit -> player must review | Audit table |
| Claim audit | History tampering | Insert-only rows | Evidence audit |
| Decision tape chain | Decision tampering | Mismatch or break detected | Verify endpoint |
| Prompt boundary | Prompt injection | Broken boundary -> untrusted | Wrapper result |
| SSRF and redirect gate | Network abuse | Unsafe host -> blocked | Action log |
| Command gate | Host damage | Unsafe command -> block and escalate | Decision tape |
| Egress proxy | Sandbox escape | Host not allowlisted -> blocked | Egress grants |
| File containment | Local file abuse | Escape -> refused | Action log |
| Trash rule | Data loss | Delete -> move to trash, never unlink | Action log |
| Notes ownership | Data loss | Other owner's note -> refused | Note registry |
| Credential vault | Secret leakage | Agent never sees the secret | Handle record |
| Capability scope | Confused deputy | Wrong host or method -> refused | Handle record |
| Backup | Data loss | Backup failure -> logged, loop continues | Backup files |
| Retention prune | Unbounded growth | Old rows pruned, tape archived first | Log tables |
| Health alerts | Silent failure | New alert -> push | Health alerts |

## 13. State and record model

### 13.1 Primary tables

| Table | Purpose | Lifetime |
|---|---|---|
| `kv_state` | Authoritative state blob: agents, teams, queue, tasks, sprints, research, rooms | Forever |
| `kv_spend` | Spend ledger and cap baseline | Forever |
| `task_archive` | One row per done task | Forever |
| `model_tiers` | Active band to model mapping | Active bands |
| `model_benchmark_scores` | Cited benchmark scores | Forever |
| `action_log` | Human-readable activity feed | 7 days |
| `decision_tape` | Hash-chained decisions | 7 days |
| `decision_archive` | Distilled pruned decisions | Forever |
| `decision_cache` | Exact-repeat decision cache | TTL |
| `decision_key_stats` | Repeat-rate measurement | Rolling |
| `agent_keys` | Agent attribution secrets | Forever |
| `sessions` | Player login sessions | 7 days |
| `evidence_records` | Claim-level evidence | Forever |
| `coverage_ledger` | Source coverage | Forever |
| `evidence_audit` | Immutable claim and coverage mutations | Forever |
| `external_credentials` | Encrypted service credentials | Until removed |
| `capability_handles` | Scoped handles for credentials | Until expiry or revoke |
| `sms_messages` | Inbound SMS inbox | Until consumed |
| `health_alerts` | Persisted health alerts | Dedup window |
| `stm_sessions`, `stm_messages` | Conversation short-term memory | TTL |
| `ltm_memories` | Long-term memories | Forever |

### 13.2 Task states

```
queued (work item)
  -> walking
    -> working
      -> needs_review -> done
      -> done (non-gated lane)
      -> failed
      -> cancelled
```

A task in `needs_review` may return to `working` through a fix task, or freeze
after the cycle cap.

### 13.3 Issue states

```
open -> in_progress -> done
                    -> closed
```

`blocked` is a separate boolean committed by the scrum master. It is not a
status.

### 13.4 Sprint and product states

| Record | States |
|---|---|
| Sprint | `active` -> `closed` |
| Product | `draft` -> `in_progress` -> `review` -> `released` |
| Backlog request | `pending` -> `accepted` -> `resolved` |
| Player inbox ask | `awaiting_input` -> answered or `superseded` |
| Escalation | `pending` -> `approved` or `denied` |
| Theo approval | `pending` -> `approved` or `denied` |
| Claim | `needs_review` -> `cleared` or `blocked` |
| Coverage | `pending_review` -> `covered` or `blocked`; `failed` is separate |

### 13.5 Review gate state

| Field | Meaning |
|---|---|
| `approvals` | Clean votes so far |
| `approvers` | Who voted clean |
| `reviewerIds` | Locked reviewer pair |
| `enteredMs` | When the gate opened |
| `cycleCount` | Total review and fix cycles across re-entries |
| `escalated` | Frozen after the cap |

## 14. Cadence and SLA table

| Activity | Cadence | Clock | Notes |
|---|---|---|---|
| Movement | 1s | Simulation | Walk and arrive |
| Task cycle | 6s | Simulation | One read-modify-write pass |
| Peer help signals | 90s | Wall | Signal to the worker's director; no report |
| Director approvals | 30s | Wall | Delegable escalations |
| Health snapshot | 300s | Wall | Alerts and digest |
| OpenRouter credits probe | 300s | Wall | Warms the cache |
| Telegram poll | 25s timeout | Wall | Long poll |
| Backup | 1h | Wall | Keep newest 1 |
| Log prune | 6h | Wall | 7-day retention, 50k rows per run |
| Model tier refresh | 2h | Wall | Governance task |
| Refinement | Weekly plus kick | Cadence stamp | Per-team slot |
| Social | Weekly | Cadence stamp | Knowledge sharing |
| Roadmap | Weekly | Cadence stamp | Silent |
| Consensus relay | Weekly | Cadence stamp | After roadmap |
| Rule mining | Weekly | Cadence stamp | Silent |
| Success mining | Weekly | Cadence stamp | Silent |
| Archive distill | Weekly | Cadence stamp | Silent |
| Skill review | 30 min | Cadence stamp | Content-gated |
| Distill | 30 min | Cadence stamp | Content-gated |
| Research topic | Per topic, floor 5 min | Cadence stamp | 24h overlap |
| Pipeline | Per pipeline, floor 1h | Cadence stamp | Strict order |
| Standing task | Per declaration | Scheduler tick | Window, weekly, monthly, yearly |
| One-off schedule | Named time | Wall | 366-day horizon |
| Hiring | 45s cooldown | Wall | Single pending hire |
| Firing review | 60s cooldown | Wall | Single pending review |
| Stale work sweep | 20s | Wall | Requeue, then scrum master replan |
| Stuck gate sweep | 20s | Wall | Re-pick reachable reviewers |
| Coaching catch-up | Daily | Wall | 3-round cap |
| Peer review timeout | 15 min after 1 vote | Simulation | Gate closes |
| Escalation re-ask | 15 min cooldown, 6 asks | Wall | Then human |
| Verifier coverage cap | 12 flips per 6h | Wall | Agent verifiers |

## 15. Roles and permissions matrix

| Capability | Owner | Theo / director | Worker | Key holder |
|---|---|---|---|---|
| Converse | Yes | Yes | Yes | Per key |
| Create story | Yes | Yes | No | Per key |
| Assign work | Yes | Yes | No | Per key |
| Start run | Yes | Yes | No | Per key |
| Report status | Yes | Yes | Yes | Per key |
| Read artifact | Yes | Yes | Own only | Per key |
| Request tool call | Yes | Yes | Sandboxed | Per key |
| Execute or shut down container | Yes | Yes | No | Per key |
| Process image or video | Yes | Yes | No | Per key |
| Report spend or review budget | Yes | Yes | No | Per key |
| Send or read message | Yes | Yes | Yes | Per key |
| Create or cancel schedule | Yes | Yes | No | Per key |
| Change policy | Yes | No | No | No |
| Grant or revoke Theo key | Yes | No | No | No |
| Spend, external comm, repo change, add agent, provision container | Yes | Approval required | Approval required | Per key |
| Write wiki | Yes | Yes | Proposal only | Per key |
| Review evidence | Yes | Director/admin | No | No |
| Mark coverage reviewed | Yes | No | No | No |

## 16. Improvement candidates

These are observations from reading the implementation. They are not defects.
Each one is a question for the process review.

### Start here

If the review reads only three items, read these. They decide whether the
quality gates mean what this document says they mean:

1. Item 5: the one-vote peer-gate timeout.
2. Item 4: bugs and spikes close with no review.
3. Item 2: the single-slot player ask.

The remaining items are real but lower stakes, or are operator preferences
rather than gate-integrity questions.

1. **Action item: correct `DESIGN.md`.** The code is the source of truth, so
   `DESIGN.md` moves, not the code. Fix four numbers: backups are hourly and
   keep 1 snapshot, not every 5 minutes keeping 24; the model refresh runs
   every 2 hours, not daily; the high-tier monthly budget defaults to 0
   (disabled), not $5; the spend cap defaults to $50, not $5.
2. **Single-slot player asks.** `_pendingPlayerAsk` holds one issue. A second
   issue's ask replaces the first. If concurrent blockers are possible, this
   drops a question. Consider a queue keyed by issue.
3. **Single-slot hiring and firing.** Only one pending hire, one onboarding, and
   one firing review exist at a time. This paces governance, but it also means a
   burst of need waits. Decide whether throughput or pacing is the goal.
4. **Peer gate exclusions.** Research, skill review, distill, spike, bug, and
   meeting lanes skip the peer gate. Research has the evidence review instead.
   Bugs and spikes have no review. Confirm whether incident fixes should close
   without a post-fix check.
5. **One-vote timeout.** After 15 minutes, one clean vote closes the gate. On a
   quiet tank this may be the normal path. If the quality bar needs two votes,
   the timeout policy should say so.
6. **Human-only escalation backlog.** Floor-1.0 kinds wait for the owner with no
   time-based SLA. The queue is visible in the escalation file, but not in a
   dedicated aging report. Add an aging view if these can sit.
7. **Telegram-only memory.** Follow-up context exists only on the Telegram
   bridge. The public ask endpoint is stateless. If the owner uses the web chat,
   a follow-up loses context. Decide which surface is canonical.
8. **Verifier cap.** Agent verifiers can flip 12 claims per 6 hours. A large,
   legitimate review batch needs the owner. That is a deliberate abuse control,
   but it can become an operational bottleneck.
9. **Firing reviewer availability.** A firing review needs both the admin and
   the senior director free. On a small or busy roster, the review can starve.
   Consider a fallback reviewer or a queue.
10. **Task budget stop.** A budget-exhausted card closes as failed and waits for
    a director to reopen it. There is no automatic top-up. Confirm that manual
    intervention is intended for every overrun.
11. **Cross-record traceability.** Tasks, issues, escalations, approvals, and
    claims use different ids. There is no single process-instance id that ties
    one owner request to its issue, tasks, reviews, and final report. A shared
    correlation id would make end-to-end review and audit easier.
12. **Backup window.** Keeping one hourly snapshot gives a one-hour recovery
    point objective and no history. If that is too thin, raise the keep count
    and document the storage cost.
13. **Model refresh cadence.** Re-picking models every 2 hours can change cost
    and behavior mid-day. If stability matters more than price, lengthen the
    interval or pin bands during a sprint.
14. **Policy surface.** Six operator-edited JSON files sit beside the code
    (`plain_writing`, `browse_policy`, `research_desk`, `operator_policy`,
    `grants`, `watchlist`). Only the watchlist writes back. `grants.json`
    applies live on file change; the rest apply on policy reload. A small
    operator UI would reduce edit mistakes, but it also adds a write path to
    policy. Decide which risk matters more.

15. **Quality-floor knob.** The value rule keeps only candidates within 6
    benchmark points of the best scored model in a band (2 points for the
    high band), then picks the cheapest. The gap is a hard-coded constant
    (`BENCHMARK_QUALITY_FLOOR_GAP`), and it silently sets the cost-versus-
    quality trade for every band: under current catalog prices it leaves the
    mid tier about 3 points and the coding tier about 5 points of benchmark
    score on the table in exchange for a 2x to 20x price cut. That trade is
    the owner's call per band, not a global constant. Expose the gap as an
    operator-settable knob, the same way the price ceilings already are.

## 17. Appendix A: glossary

| Term | Meaning |
|---|---|
| Action log | Human-readable activity feed |
| Adversarial village | Two independent teams working one goal |
| Backlog request | An agent's proposal for work, waiting for refinement |
| Bank | Spend ledger and budget gates |
| Breakdown | Ceremony that turns a large ask into sized work |
| Card | A work-queue item or task, in UI language |
| Charter | Owner-set goal and interests that steer room priority |
| Claim | One evidence record with a source and status |
| Coverage | Whether a source has been reviewed and counted as covered |
| Critic | First peer reviewer, instructed to find the flaw |
| Decision tape | Hash-chained record of JEV decisions |
| Dormancy | Sleep-not-die: sim paused, port bound, wake on any request |
| Deliverable room | A room whose work is peer-gated |
| Escalation | A decision the normal gate could not make |
| Idle gate | No due work and no active agent means no spend |
| JEV | Typed-choice classifier used for consequence decisions |
| LTM | Long-term conversation memory |
| Passport | Story-level provenance chain |
| Peer gate | Two-reviewer approval gate for deliverables |
| Player inbox | Owner-facing messages, including card reports and asks |
| Refinement | Grooming requests into stories |
| Sprint | Bounded delivery window with measured velocity |
| Standing task | Declared recurring work with schedule rules |
| STM | Short-term conversation memory |
| Task cycle | The 6-second read-modify-write pass |
| Tier | Model band: low, sub-low, mid, high, coding, vision |
| Verifier | Actor who clears or blocks evidence claims |
| Village | Identity and memory boundary for agents and pages |

## 18. Appendix B: configuration and policy files

| Setting | Where | Default or note |
|---|---|---|
| `OPENROUTER_API_KEY` | `.env` | Required |
| `SPEND_CAP_USD` | `.env` | 50 |
| `AGENT_SPEND_CAP_USD` | `.env` | 0, disabled |
| `HIGH_TIER_MONTHLY_BUDGET_USD` | `.env` | 0, disabled |
| `HIGH_TIER_MAX_PRICE_USD` | `.env` | Price ceiling |
| `SUBLOW_TIER_MAX_PRICE_USD` | `.env` | Hard cap |
| `PAGE_REQUEST_MONTHLY_BUDGET` | `.env` | 1000 |
| `LOG_RETENTION_DAYS` | `.env` | 7 |
| `DB_BACKUP_INTERVAL_S` | `world/operator_policy.json` (`dbBackupIntervalS`) | 3600 |
| `DB_BACKUP_KEEP` | `world/operator_policy.json` (`dbBackupKeep`) | 1 |
| `MODEL_TIER_REFRESH_INTERVAL_S` | `world/operator_policy.json` (`modelTierRefreshIntervalS`) | 7200 |
| `TIER_PRICE_WATCH_INTERVAL_S` | `world/operator_policy.json` (`tierPriceWatchIntervalS`) | 1800 |
| `TIER_RECHECK_DRAIN_INTERVAL_S` | `world/serve.py` | 60 |
| `SCRUM_MASTER_MIN_TEAM_SIZE` | `world/operator_policy.json` | 4 |
| `MAX_TOTAL_AGENTS` | `world/sim.py` | 500 |
| `MAX_REVIEW_CYCLES` | `world/operator_policy.json` | 8 |
| `PEER_REVIEW_TIMEOUT_MS` | `world/sim.py` | 15 minutes |
| `REPORT_STRIKE_WINDOW_MS` | `world/operator_policy.json` | 14 days |
| `HIRE_COOLDOWN_MS` | `world/operator_policy.json` | 45 seconds |
| `FIRING_COOLDOWN_MS` | `world/operator_policy.json` | 60 seconds |
| `REFINEMENT_CADENCE_MS` | `world/operator_policy.json` | 7 days |
| `SOCIAL_CADENCE_MS` | `world/operator_policy.json` | 7 days |
| `ROADMAP_CADENCE_MS` | `world/operator_policy.json` | 7 days |
| `RULE_MINE_CADENCE_MS` | `world/operator_policy.json` | 7 days |
| `MIN_RESEARCH_CADENCE_MS` | `world/operator_policy.json` | 5 minutes |
| `MIN_PIPELINE_CADENCE_MS` | `world/sim.py` | 1 hour |
| `VERIFIER_COVERED_MAX` | `world/evidence.py` | 12 |
| `VERIFIER_COVERED_WINDOW_S` | `world/evidence.py` | 6 hours |
| `DIRECTOR_ESCALATION_REASK_COOLDOWN_S` | `world/operator_policy.json` | 15 minutes |
| `DIRECTOR_ESCALATION_MAX_ASKS` | `world/operator_policy.json` | 6 |
| `MEMORY_ENABLED` | `.env` | Conversation memory |
| `STM_MAX_TURNS`, `STM_MAX_TOKENS` | `.env` | Memory budgets |
| `LTM_MAX_RECALL`, `LTM_MAX_PER_SESSION` | `.env` | Memory caps |
| `FILE_ACCESS_ENABLED` | `.env` | Local file tool |
| `BROWSE_BLOCK_CATEGORIES` | `world/browse_policy.json` | Always-block policy |
| `PLAIN_WRITING_BAN_LIST` | `world/plain_writing.json` | Anti-slop words |
| `RESEARCH_DESK_PASSES` | `world/research_desk.json` | Three-pass questions |
| `RESEARCH_WATCHLIST` | `world/watchlist.json` | Standing topics |
| Operator policy knobs | `world/operator_policy.json` | 107 knobs: cadences, cooldowns, team rules, memory and device knobs, and every former serve.py static (Phase 3, 2026-10-18) |
| Agent policy edits | `policy_edit` tool | Agents tune the 19 agent-editable keys through a JEV-reviewed key/value change; effective immediately, no restart; the player reverts by editing the JSON |
| `GRANTS_CONFIG` | `world/grants.json` | App and file grants; applies live on file change |
| `DEVICE_API_KEY` | `.env` | Device check-in bearer; auto-generated on first boot if unset |
| `--max-idle-minutes` | launch flag | Dormancy arm; 0 or unset disables |

## 19. Appendix C: process to code map

| Process | Primary code |
|---|---|
| BP-01 | `serve._route_player_request`, `serve._route_lane_*`, `theo_policy` |
| BP-02 | `sim.file_work_request` |
| BP-03 | `sim._refinement_step`, `_resolve_refinement` |
| BP-04 | `sim._breakdown_step`, `_resolve_breakdown` |
| BP-05 | `sim.spawn_new_team_for_request` |
| BP-06 | `sim.queue_sprint`, `sprint_progress`, `close_sprint` |
| BP-07 | `sim.pick_next_due_index`, `_assign_due_item`, `on_call_agent` |
| BP-08 | `sim._task_cycle`, `content._server_content_dispatcher` |
| BP-09 | `sim._enter_peer_review`, `_peer_gate_should_close`, `_sweep_stuck_gates` |
| BP-10 | `serve._classify_failure`, `_record_failure`, `sim._send_back_after_failure` |
| BP-11 | `sim.finish_task`, `_deliver_card_report`, `serve` feedback endpoint |
| BP-12 | `sim.create_product`, `product_release_record`, `content._run_product_build_content` |
| BP-13 | `sim._roadmap_step` |
| BP-14 | `sim._consensus_relay_step` |
| BP-15 | `sim._retro_step`, `close_sprint`, `_record_sprint_velocity` |
| BP-16 | `sim._social_step`, `_resolve_social` |
| BP-17 | `sim.queue_meeting` |
| BP-18 | `sim.set_charter`, `get_charter`, `_charter_alignment` |
| BP-19 | `sim._start_auto_hire`, `_complete_auto_hire`, `_resolve_onboard_meeting` |
| BP-20 | `sim._start_auto_firing_review`, `_resolve_firing_review`, `_fire_decision` |
| BP-21 | `sim._grade_completed_task`, `_coach_low_grade`, `_coaching_loop_step` |
| BP-22 | `sim._rule_mine_step`, `serve._mine_rule_proposals` |
| BP-23 | `sim._success_mine_step` |
| BP-24 | `sim._archive_distill_step` |
| BP-25 | `serve._launch_adversarial_village`, `_adversarial_village_tick` |
| BP-26 | `sim.add_research_topic`, `_check_schedules` |
| BP-27 | `sim._standing_task_sweep`, `schedule.StandingTask` |
| BP-28 | `sim.add_pipeline`, `_check_pipelines` |
| BP-29 | `sim.queue_once`, `serve.intent_schedule_once` |
| BP-30 | `sim._check_schedules`, `content._run_skill_review_content`, `_run_distill_content` |
| BP-31 | `sim.queue_bug`, `on_call_agent`, `_escalation_step` |
| BP-32 | `serve.page_probe`, `_screenshot_url_sync`, `_run_browser_act_sync` |
| BP-33 | `serve` device check-in endpoint, `sim.record_device_checkin` |
| BP-34 | `sim.wiki_write_page`, `wiki_read_pages`, `serve.propose_wiki_page`, `approve_wiki_proposal` |
| BP-35 | `sim.record_memory_provenance` |
| BP-36 | `evidence.py`, `serve` evidence endpoints |
| BP-37 | `content._run_research_content`, `config.research_desk_passes` |
| BP-38 | `serve` library endpoints, `fs.py` |
| BP-39 | `content._make_skill_publisher`, `_build_skill_card` |
| BP-40 | `memory.py`, `serve._ask_core` |
| BP-41 | `serve._decision_cache_*`, `_append_decision_tape`, `_verify_decision_tape` |
| BP-42 | `bank.py`, `serve._accrue_spend`, `_spend_ledger_*` |
| BP-43 | `serve.refresh_model_tiers`, `_resolve_model_tier`, `_jev_quorum_choice_sync` |
| BP-44 | `sublow.py`, `jev_grants.py` |
| BP-45 | `bank._task_budget_*`, `sim._notify_task_budget_exhausted`, `_director_grant_budget_reopen` |
| BP-46 | `serve._page_request_*` |
| BP-47 | `serve._jev_quorum_choice_sync`, `_jev_choice` |
| BP-48 | `serve._resolve_pending_escalations_sync`, `_escalation_floor` |
| BP-49 | `sim.request_player_input`, `request_player_input_verdict`, `resolve_player_ask` |
| BP-50 | `serve` credential endpoints, `mint_capability_handle`, `resolve_capability_handle` |
| BP-51 | `serve._validate_api_service_spec`, `_approve_api_service_proposal` |
| BP-52 | `serve` browse, curl, execute, egress, VPN code |
| BP-53 | `serve` grant endpoints, `config.get_grants_config`, `world/grants.json` |
| BP-54 | `serve.wrap_external_content`, `verify_boundary_intact` |
| BP-55 | `serve._prune_logs`, `_backup_loop`, `_verify_decision_tape` |
| BP-56 | `serve.compute_health_snapshot`, `_health_check_loop` |
| BP-57 | `serve` player feedback endpoint, `sim._append_feedback` |
| BP-58 | `serve._peer_review_pass`, `_peer_review_tick` |
| BP-59 | `serve._idle_shutdown_loop`, `no_store` wake, `_NON_WAKING_READS`, `_NON_WAKING_WRITES` |
| BP-60 | `world/selfedit.py` (propose, apply_pass, request_window), `serve._dormancy_window_loop`, selfedit endpoints |

## 20. Appendix D: review checklist

Use this list when reviewing the document and the process.

- [ ] Every process has a named owner.
- [ ] Every process has a trigger and an end state.
- [ ] Every consequence-bearing decision has a gate.
- [ ] Every gate has a defined failure path.
- [ ] Every process leaves a record that can be audited.
- [ ] Every cadence is documented with its actual code value.
- [ ] Every owner-facing approval has a visible queue.
- [ ] Every single-slot queue is intentional.
- [ ] Every lane without peer review has another quality control.
- [ ] Every budget stop has a recovery path.
- [ ] Every doc statement matches the code or is marked as intent.
- [ ] Every process has a measurable outcome.
