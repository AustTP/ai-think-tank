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
- Enforced now: request-id replay protection on the two external write tools.
- Not yet enforced: a per-conversation allowlist that refuses a `commit` tool
  without an approval. That waits for a per-tool review, as the review
  recommended. Until then, the tier labels are guidance and audit data, not a
  hard gate.

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
