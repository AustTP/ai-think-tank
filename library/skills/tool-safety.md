# Tool safety: replay and trust tiers

## Purpose
Every tool call has two labels: whether it is safe to replay, and how much
trust it needs. This is the takeaway from the Pi Durable review applied to the
think tank's own tool surface. The labels live in `world/content.py`
(`tool_policy`, `tool_replay_safety`, `tool_trust_tier`).

## Tiers
| Tier | Meaning | Examples |
|---|---|---|
| `research` | Read-only. Safe to repeat. | `read_library_file`, `browse_page`, `github_get_repo`, `read_ais_feed`, `list_calendar_events` |
| `prepare` | Creates a reversible artifact or draft. | `create_gmail_draft`, `create_google_doc`, `append_google_sheet_row`, `write_spike_report` |
| `commit` | Moves money, sends, grants access, or starts an external run. | `apify_run_actor`, `create_calendar_event`, `generate_agent_avatar`, `assign_task`, `grant_allowlist` |

Unknown tools default to `commit`. A new tool cannot silently become
replayable or low-trust.

## Replay safety
- `safe`: repeating the call cannot create a second side effect. Read-only
  tools are safe.
- `unsafe`: repeating the call can duplicate a side effect. These tools need a
  stable `requestId`.

## Request ids
`apify_run_actor` and `create_calendar_event` accept an optional `requestId`.
Use the task id when the call is part of a task; use a UUID otherwise. A retry
with the same `requestId` returns the original result instead of starting a
second meter-billed run or creating a second calendar event. The server keeps
the idempotency ledger in the `external_writes` table for one hour for Apify
runs and thirty days for calendar events.

## Current enforcement
- Metadata: every tool can be labeled with `tool_policy(name)`.
- Enforced now: request-id replay protection on `apify_run_actor` and
  `create_calendar_event`.
- Enforced now: every known commit-tier tool is wrapped in the same
  `external_writes` ledger. An explicit `requestId` is used when present;
  otherwise the key is derived from the conversation, tool, and arguments.
  Error outputs are not cached, so a transient failure can be retried.
- Browser actions are prepare-tier at the tool-policy layer, but the real
  approval is the server-side Jev gate and the player-vetted host allowlist.
  `browser_step` adds verification and a session checkpoint, not a bypass.
- Pulse watches are prepare-tier local configuration. `read_pulse` is
  read-only. The matched feed text is external data and is wrapped before it
  reaches a model.
- `audit_agent_readiness` and `project_board` are read-only. The audit is a
  surface scan, not a checkout test; the board never changes a card.
- Enforced now: a per-conversation allowlist. Research and prepare tools are
  always allowed. A commit tool is refused unless the conversation approves it
  with `approvedTools`, sets `allowCommit`, or is a trusted code/review/QA
  pipeline task. The refusal is a hard policy message, not a retryable error.
- Supervisor cards are server-authored and approve their own tools
  (`delegate`, `schedules`, `supervisor_grant_budget`, `direct_backlog`,
  `generate_agent_avatar`, `execute_script`).
- Player asks: send `approvedTools` with the ask to approve a commit action,
  for example `{"question": "Book it", "approvedTools": ["create_calendar_event"]}`.
- The Red Team Auditor role is trusted with commit tools for its live security
  self-tests.

## Executor checkpoints
The spike executor saves its plan at the plan/execute boundary and saves the
tool transcript before and after every tool batch. After a crash or restart, a
retry resumes from the last completed batch instead of re-running the whole
investigation.

A call that was in flight when the process died is handled by its replay
label. A `safe` read-only call is reported to the model as safe to retry; the
model can re-issue it. An `unsafe` side-effecting call is reported as
interrupted and is not replayed, so the model must verify the external state
before trying again. This is the Pi Durable replay rule applied to our tools.

The checkpoint is cleared on a successful close; a failed run keeps it for the
retry. AIS feed cursors are checkpointed separately in
`collector_checkpoints`.

## Rules
1. Read before you write. Prefer a `research` tool that answers the question.
2. A `prepare` tool is for a draft the player can review. Do not treat a draft
   as sent.
3. A `commit` tool needs a clear reason and, where supported, a stable
   `requestId`.
4. Do not invent a tool result when a `commit` call fails. Report the failure
   and the original request id.
5. Do not loop a metered tool. A retry is for a failure, not for a new answer.

## Sources
- Pi Durable review: `library/archive/1792619807984-spike-task-3254.md`
- Transcript addendum: `library/archive/pi-durable-transcript-addendum.md`
