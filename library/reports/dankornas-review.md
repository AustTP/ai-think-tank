# Review: @dankornas on X (dankornas-review.md)

Reviewer: operator-assisted (the tank's research card kept stalling on free-pool
model timeouts; the operator completed the fetch and analysis using the same
Apify access the card was instructed to use). Source: 10 most recent tweets,
fetched 2026-11-04 ~13:15 UTC via `apidojo/tweet-scraper` (60 requested, 10
returned; his posting cadence is ~1 tweet per 90 minutes, so this covers the
last ~12 hours). Cost: negligible (free-plan budget).

## What his feed is

A tool-radar: each tweet is a one-line pain point followed by a named tool
(name, stack, what it helps with). Roughly one tool per tweet, mostly AI-agent
infrastructure. Useful as a radar, not a roadmap.

## Delta analysis vs the tank's existing machinery

The tank already has: rejection lessons, daily digest, credits ladder with a
free pool, band routing, quality gate with peer repair, weekly voting, pairing
mode, sim watchdog, Library, tool-output gisting, wake-on-mail, durable credits
state.

| # | His tweet | Tool | Delta vs the tank |
|---|-----------|------|-------------------|
| 1 | "Your AI agents shouldn't overwrite each other's work" | Network-AI (multi-agent orchestrator: shared blackboard, token budgets, permissions) | Already covered. The tank's state blob, rooms, per-task `budgetUsd` and the Bank are this, with stricter governance. |
| 2 | "Stop making your coding agent relearn the same project rules" | Eidetic Engine (durable, searchable memory of rules, failures, decisions) | Mostly covered. Rejection lessons + `agent_ltm` store failures; the gap is SEARCH: lessons are flat files; a failure-rule lookup keyed by task-type/room at dispatch time would make them load-bearing. Small, worthwhile. |
| 3 | "Finding skills shouldn't take longer than choosing what to build" | Awesome Claude Code Skills (curated list) | Not applicable. The tank's skill set is purpose-built; a curated index would be ceremony. |
| 4 | "Review a PR for security issues, not just style" | agentgg (SAST scanner for PRs) | Not applicable today: the tank's cards rarely touch PRs, and its quality gate checks evidence, not code security. Revisit only if the tank starts shipping public-facing repos. |
| 5 | "Don't build every AI chat UI component from scratch" | AICSS (React components + CLI) | Irrelevant. The tank's UI is Telegram + minimal HTTP. |
| 6 | "Parallel coding agents shouldn't mean losing track of changes" | Paradev (desktop workspace for parallel CLI agents) | Irrelevant headless; but note his framing: change-tracking between parallel agents. The tank's single-blob lock + room model already serializes writes. |
| 7 | "Turn a vague app idea into a project" | EasyMint (idea-to-scope workspace) | Already covered by the tank's refinement pipeline (vague asks become stories with acceptance criteria). |
| 8 | "Your coding agent changed the code. What did it actually do?" | agentplay (session replay: agent transcripts into replayable views to inspect what happened before an edit or failure) | **The real delta.** Tonight's debugging (a card flailing through echo-completions and refusals) required hand-queried `action_log` rows to reconstruct what the agent attempted. A read-only `/api/replay/:taskId` that renders a task's ordered model calls, tool executions, refusals and failures would make agent failures debuggable in one request. Cheap to build: the evidence already exists in `action_log` and `kv_apicalls`. |
| 9 | (meme) | - | Ignore. |
| 10 | "Your RAG pipeline needs clean content, not raw HTML" | PageSift (Rust crawler -> clean markdown) | Low. The tank's browse lane already caps/cleans text (20k cap, truncation flags). Would matter only if the tank grows a retrieval corpus. |

## Recommendation

Take exactly one thing: **task replay** (his #8, agentplay's idea, not his
tool). A single read-only endpoint that returns, for one task id, the ordered
trail of model calls, executions, classifications, refusals and watchdog
actions from `action_log`. It would have compressed tonight's debugging from
an hour of psql queries to one curl. Everything else in his feed is either
already built here or aimed at a different product shape.

Meta-note: his feed is worth a periodic skim as a radar for agent-infra pain
points, but the tank should keep building to its own incident-driven list.
