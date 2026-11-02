# Changelog

## Unreleased

### Features

- Outbound webhooks: the tank can notify external systems. A subscribed URL
  with an optional shared secret receives signed POSTs (`X-Tank-Signature`:
  sha256 HMAC over the exact body) when an action-log kind matches its event
  patterns; delivery is one bounded POST with SSRF checks at registration and
  every delivery.
- The GitHub MCP server is registered (official remote server, http
  transport, bearer auth from the existing `GITHUB_TOKEN`).
- Ops page now has a per-agent trace feed: actions, tool calls, model replies, JEV decisions, and task plan checkpoints, with an agent selector and auto-refresh.

### Fixes

- Meeting scribes now read the real documents: issue filings carry
  `sourceFiles` (normalized, capped at 5), the groomed agenda emits a
  `READ:` line per file, and the meeting executor inlines the actual file
  text (repository-root anchored, containment-checked, head+tail with a
  full heading outline when a file exceeds the excerpt) into the scribe
  prompt. A review panel was inventing tables, endpoints, and retry
  policies no code implements; three review cycles later the panel quotes
  line numbers and marks the elided boundary instead of fabricating.
- A stuck-walk cancel no longer destroys the card: the requeue carries
  `_unreachableBy`, assignment drops a pin to a failed walker and the
  round-robin skips failed ones, and after three distinct failed agents
  the card parks for operator promotion. A stranded agent (parked at the
  map's south edge) had three cards die in seconds each, including a
  lost meeting card. `queue_work` also stopped silently dropping
  `reissueOf`/`_unreachableBy`/`_reassignedTo` from re-issued cards.
- Meeting agendas now carry the filing's acceptance criteria, not just
  the user story and never-block: the agenda builder dropped them, so
  panels received "the agenda items to walk" without the items.
- The refinement groom judge no longer bounces meeting cards: the instruction
  claimed every card type "says story, spike, or research" while the criteria
  correctly described a real meeting card. Three player-filed meeting cards
  were rejected on that contradiction.
- A task dependency is now a small DAG: `dependsOn` takes one task id or a
  list with join semantics, cycles are pruned where the durable task record
  is born, and the loop guard walks each surviving edge per edge so an agent
  still blocked elsewhere is not woken by one landing edge.
- `/api/villages/chief` returns 400 on a malformed JSON body instead of a 500.
- `.env` values may be quoted; the server's loader strips one matching quote
  pair, so `source .env` no longer trips on unquoted pipe characters.
- Directors and the chief of staff can now use read-only `git_status`, `git_diff`, `git_log`, a team-scoped `task_trace`, and chief-only `find_artifact` recovery search from `live_read`.
- Agent prose now follows the no AI slop rules: lead with the point, active voice, concrete facts, no binary contrasts, throat-clearing, importance puffery, fake endings, or recap. Outbound text strips em dashes and en dashes.
- A director's low-confidence deny on a JEV safety gate now continues up the chain instead of ending it. Nora is tried once (immediate director and senior director dedupe), then Theo decides; only a confident deny is final.
- Sim-watchdog escalations now queue a research card, not the default code lane, so a senior director such as Nora can actually take the investigation. A director was previously filtered out of code work and the alert could sit unowned.
- Repair cards now inherit the original room, source paths, issue/request provenance, user story, and NEVER constraints, not just the acceptance text.
- Scoped cards (player-filed, JIRA-linked, or a repair/review of an existing task) no longer receive the global "focus next work on <room>" line. That line was overriding a card's own room and deliverable.
- Repair tasks now need a saved Library artifact like their original lane. A repair report that says "not completed" can no longer close as done.

### Breaking changes

- **Unpriced model usage holds the task budget by default.** A task whose
  provider response carries no reliable price now records an unresolved
  `usage_receipts` row. Further `/api/chat` calls for that task return 429
  until an operator resolves the receipt or sets
  `UNPRICED_USAGE_POLICY=allow`.
- **Wake-on-mail now uses a durable queue.** Off-duty wakes are queued,
  coalesced, claimed, completed, and restored from state after a restart. A
  claimed wake with no action tick is requeued after 30 minutes.
- **Prompt-contract evals are now a runnable harness.** `evals/run_evals.py`
  exercises assignment pickup, blocked reporting, approval refusal, village
  boundaries, denied tools, idle exit, and restart recovery against a running
  tank.
- **Player large requests now go through a plan approval gate.** The board
  proposes a durable, versioned plan instead of filing work immediately.
  Approval binds the revision hash; rejection requires a reason; revision
  creates a new version with a diff.
- **Package boundaries are checked in CI.** `scripts/check_module_boundaries.py`
  records the current legacy cross-package edges and fails when a new one
  appears. New code must use a package entry point or a port.

### Upgrade guide

- The `usage_receipts` table and its indexes are created automatically by
  `serve.init_db()` and by the first Bank write. No manual migration is
  required.
- Set `UNPRICED_USAGE_POLICY=allow` only if you intentionally accept
  unpriced spend from providers that omit `usage.cost`.
- Resolve unpriced receipts with `bank._resolve_unpriced_usage()` or by
  setting the policy before restarting.
- `plan_revisions` is created automatically by `core.plan_approvals` on first
  use. No manual migration is required. Webhook and direct API callers can keep
  using `/api/intent/assign-big-task`; the player UI uses
  `/api/intent/plan-big-task` and `/api/plans/*`.
