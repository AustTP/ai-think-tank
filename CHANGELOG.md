# Changelog

## Unreleased

### Fixes

- Directors and the chief of staff can now use read-only `git_status`, `git_diff`, `git_log`, a team-scoped `task_trace`, and chief-only `find_artifact` recovery search from `live_read`.
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
