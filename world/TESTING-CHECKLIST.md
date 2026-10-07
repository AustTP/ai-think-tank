# AI Think Tank — Manual Test Checklist

Hands-on checklist for testing the live think tank. Run the server first, then walk
these in order. Mark each **PASS / FAIL / BLOCKED(why)**.

## Setup
- [ ] Start server: `python serve.py` (port 8936) — or `python serve.py <port>`.
- [ ] Confirm startup prints the admin account / "dev server ... on http://127.0.0.1:8936".
- [ ] Open http://127.0.0.1:8936 in a browser; the think tank map renders with agents.
- [ ] Confirm agents are moving between rooms (not all frozen at spawn).

---

## 1. Work lifecycle (a story actually gets done)
- [ ] File a story via `POST /api/intent/issues` (teamId, type, summary, feature, reporterId) → returns key **DEV-1** (no leading zeros), a `title` defaults to summary.
- [ ] Verify a `title` field is returned; pass a distinct `title` → it is kept, `summary` unchanged.
- [ ] The story appears in the owning team's backlog-request pipe (`backlogRequests`), status `pending`.
- [ ] The scrum master grooms it into a sprint (Command Center) on cadence.
- [ ] An agent is assigned the card, walks to the room, does the work.
- [ ] Deliverable work enters the **peer gate** (`needs_review`), two reviewers are picked.
- [ ] On two clean approvals → story `done`. On a rejection → a fix returns to the original author.
- [ ] Agent completes → goes off duty (sprite **vanishes**), story is gradable.

### Regression — the "shipped → roadmap → new work" loop
- [ ] A completed, approved story is graded (Cut-2 grading/roadmap).
- [ ] Roadmap recomputes and surfaces follow-up work for next grooming.

---

## 2. The `blocked` field (SM-committed)
- [ ] `blocked` is a **bool field**, not a status — `POST .../status` with `"blocked"` is rejected.
- [ ] **Requirements-met**: agent claims met → supervisor (director) approves → scrum master commits `blocked=true`; card shows blocked + reason (`blockedKind`).
- [ ] **Stuck-on-player**: director approves an `ask_player` verdict → a player-inbox question is delivered AND the SM commits `blocked=true`; the agent is parked (locked, not abandoned).
- [ ] **Player answers** → `blocked=false` committed, the agent is unblocked and resumes with the answer as context.
- [ ] **Self-resolve** (`resolve_internally`) → no inbox question, `blocked=false`.

### Dependency blocks
- [ ] Agent A files `block-dependency` on another agent's task → SM commits `blocked=true` (kind `stuck_on_agent`), `dependsOnTask` recorded.
- [ ] When the dependency task **lands**, the card auto-unblocks (`unblock_landed`) → SM commits `blocked=false`.
- [ ] The wake mail to A **names the blocking story** (the landed task's title, not a bare id).

### Loop guard (blocked cycles)
- [ ] If A is still blocked on a **different** story's unmet dependency when one dependency lands, A is **NOT** woken (no churn).
- [ ] No repeated wake/email storm when multiple cards block at once.

---

## 3. Wake-on-mail
- [ ] **Off-duty agent** who receives an action-needed mail item (player answer / dependency landed) **comes online** (`offDuty=false`, visible), routes to the work, acts, then returns offline.
- [ ] A woken-but-unacted agent is **not** parked-idle in the same tick (visible until she acts).
- [ ] On-duty recipient: mail is filed, agent resumes through the normal locked-task path (no spurious wake).
- [ ] Pinned resume: the woken agent gets HER card, not a round-robin stranger.

---

## 4. Player email (vault-backed SMTP)
Prereq: provision the credential once —
`curl -X POST localhost:8936/api/player-email/credential -d '{"appPassword":"<16-char>"}'`
→ expect a **self-test email** back immediately (verified, not assumed).
- [ ] **Agent asks a question** → you get an email with the issue key + question.
- [ ] **Card gets blocked** → you get an email naming the card + reason.
- [ ] **Story lands for review** → NO player email (by design: peer review is
      agent-to-agent, the two picked reviewers handle it via their own mailbox;
      the player only hears about things that actually need player input).
- [ ] Email arrives at austtp25@gmail.com (check spam/junk too).
- [ ] `POST /api/player-email/test` re-sends a test.
- [ ] Wrong-format password (not 16 chars) is rejected with a clear error.
- [ ] Agent-key call to the credential endpoint is **rejected** (player-only).
- [ ] With the credential **absent/undecryptable**, the think tank keeps running (fail-closed; no crash, no Wedge) — check serve stdout for a logged skip.

---

## 5. Issue / story title ergonomics
- [ ] Hundreds of cards are scannable by one-line `title` without opening each.
- [ ] A card with no explicit title defaults to its summary.
- [ ] Issue keys are honest — `DEV-1`, `DEV-12` — no `DEV-0001`.
- [ ] Listing issues is newest-first; the `blocked`/`title` fields surface on pre-existing cards too.

---

## 6. Player-inbox (questions to the player)
- [ ] `GET /api/player-inbox` lists awaiting + answered questions, newest first.
- [ ] `POST /api/player-inbox/{id}/respond` answers → status `answered`, agent resumes, `blocked=false` queued.
- [ ] `GET /api/intent/issues/{key}` shows one card's full detail (story, criteria, blocked state + provenance).
- [ ] Answering a **closed/terminal** card clears `needsInput`.

---

## 6b. The spine + attention lanes (2026-10-06)
- [ ] `POST /api/intent/charter` sets the player's charter (goal required); `GET` reads it back. PLAYER-only, agent key rejected.
- [ ] Roadmap recompute (`_roadmap_step`) gives a charter-aligned room a priority boost; a FRESH aligned room defaults to priority 1, an off-spine one to 0.
- [ ] The consensus relay names the charter goal; every task's instructions open with `Tank charter: <goal>`.
- [ ] A `lane` on a queued card survives queue_work → refinement → assign onto the real task.
- [ ] `parking-lot` cards are never auto-assigned and do NOT count as work for the idle gate (they wait with a bookmark, never dropped).
- [ ] At equal priority, build > open > reading; urgency still beats a lane.
- [ ] The reading lane is rate-limited to ONE active card per room (`pick_next_due_index` skips a second one until the first completes).
- [ ] `POST /api/intent/schedule-once` accepts `lane` and rejects an unknown lane with a 400.

---

## 7. Clarify / Ask (player → agent)
- [ ] `POST /api/intent/clarify` → the on-call agent answers **knowledge-base-first** (library search), falling back to the completing agent.
- [ ] Fail-closed on auth (no agent key / bad key → rejected).

---

## 8. Governance — firing / hiring / review loops
- [ ] A consistently underperforming agent builds toward a **firing consultation**; a blocked-but-legitimate command → escalate to admin (email), and a resolve link works.
- [ ] Auto-hire engages when a team is genuinely underutilized (not a low bar).
- [ ] **Stage-3 readiness**: a new hire only finalizes once they hold a real task OR idle-timeout — no ghost onboarding.
- [ ] A fired agent's standing credentials + capability handles are **revoked** (nothing survives the fire).

---

## 9. Artifacts publish (Phase F)
- [ ] Released work can be published from The House (`Publish to GitHub`).
- [ ] Publish writes to the configured private repo (`AI_THINK_TANK_PUBLISH_REPO` in `.env`, a NEW repo, not the think tank source).

---

## 10. Self-heal & watchdog
- [ ] A task stranded at "working" with no assignee is **reclaimed** / re-issued.
- [ ] The skill-review sentinel clears a far-future stamp (the 1e18 "year 33658" wedge).
- [ ] Off-duty agents `_reconcile_stranded_agents` re-wake anywhere free/reachable (no outskirts stranding).
- [ ] Passport `verify_passport()` watchdog runs; a bad passport is caught.

---

## 11. Security / sandbox
- [ ] Sandbox coding work is **quality-gated**: work is not approved unless flake8/mypy/bandit/pytest --cov-fail-under=90 is green on the agent's code.
- [ ] The sandbox container has **no internet route** to the real Internet (only the egress proxy is reachable, and only when the agent holds a capability).
- [ ] External-credential vault: an agent never holds a real key — only an opaque handle; the server decrypts in-process.
- [ ] `/api/keys/*` endpoints are **player-only** (an agent key is rejected).
- [ ] `.env` and `.secret_keys/` are committed nowhere (gitignore + guard script block them).

---

## 12. Reliability surfaces
- [ ] **Reload persistence**: refresh the browser mid-lifecycle → agents/state restore from DB (no double-spawn, no wedged busy).
- [ ] **Dormancy**: with `--max-idle-minutes N` set, the think tank pauses after N min of no requests and any request wakes it (state intact).
- [ ] **Concurrent meetings**: multiple rooms can hold meetings at once; a busy agent rebuffs a second invite; busy-state collision is safe.
- [ ] **Room overflow**: a room at capacity doesn't hard-fail — agents reassign.

---

## Known baseline
The full pytest suite has **23 pre-existing failures** (DB-ordering pollution) that
**pass in isolation** — unrelated to the blocked-field / mail / email / issue work.
They live in products/wiki, sprints, teams, seed. If you want a clean green bar,
run those specific test files alone; none of the flows above depend on them.

## If a check FAILS
Capture: the exact step, the observed vs expected state, and the relevant serve
stdout / DB rows. Don't "fix" by deleting runtime state — diagnose the root cause
(per the project's standing rule: never declare a hard limit without confirming
the actual failure first).