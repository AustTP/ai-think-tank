# Progress and spending assessment

Snapshot: 2026-10-09T17:30:05.784930-04:00. Source: local think_tank.db state and spend ledger. No configuration changes made.

Recorded service spending: $21.36. Per-task and village buckets were excluded to avoid counting the same charge twice. These are local accounting records, not a reconciled provider invoice. Daily records use UTC dates.

| UTC date | Recorded spend |
|---|---:|
| 2026-10-06 | $1.58 |
| 2026-10-07 | $5.04 |
| 2026-10-08 | $5.11 |
| 2026-10-09 | $9.63 |

The spike execution service records $9.36 (43.8% of recorded services). This service includes research/repair execution; it is not limited to taskType spike. Task-level attribution records roughly $5.04 for code preflight and $2.15 for reviews. Those figures overlap the service totals and must not be added to them.

Root tasks recorded done: {'code': 14, 'spike': 99, 'research': 13}. All 14 completed code-labelled root tasks are knowledge distillations, not trading software delivery. The dedicated trading workflow is blocked at research, in paper mode, with no reports or accepted evidence recorded. No completed end-to-end implementation is established by these counters.

Repeated repairs include 24 of the trading strategy/risk report, 20 of the project-status audit, and 19 of real-time-price research. Provider account monthly-limit failures now block several stories. The storage-options comparison completed, but the remaining unblocked stories have not all reached completion.

## Recommended cost controls

1. Apply one shared dollar/call budget to an original story and every descendant repair, review, investigation and code check. Current per-task budgets can be renewed unintentionally by creating child tasks.
2. After two identical failures, stop repeating the same approach; require changed evidence, a specific input or a different repair plan. Limit all recovery generators, including Librarian jobs, by original story plus artifact/content fingerprint.
3. Validate actual artifacts mechanically before spending on model reviews. Save tables directly instead of repeatedly generating prose that says a file exists. Cache passing code checks by code digest and tool configuration; rerun after relevant code changes.
4. Gate new exploratory spikes on a specific unresolved milestone. Focus on one usable dataset, one executable downloader/storage path and one independently checked backtest before broad research.
5. Introduce a hard daily spending ceiling, an alert below it, and one scheduled daily digest. A monthly ceiling alone permits a large one-day burst.
6. Cache official documentation and existing dataset fetches; send only changed evidence and in-scope acceptance criteria to repair/review calls. Keep independent review and budget gates.

Illustrative saving: halving the spike service spend would save about $4.68 over a comparable activity window. That is a scenario, not measured future savings. Do not increase the provider quota until the repeat-work controls are effective.
