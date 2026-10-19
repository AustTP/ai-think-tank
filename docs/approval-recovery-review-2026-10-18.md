# Review of the latest 100-action approval email

Source: the application's persisted, sent email report, ID 170 (100 actions).
This is the saved email body and its event records, not an inbox search.

The report contains 71 command approvals and 29 recovery attempts. All 29
recovery attempts failed to apply. Task-2863 (portfolio formatting) accounts for
16 attempts; task-2864 (trading integration) accounts for 13. Command approval
does not establish successful execution, a passing test, or story completion.

| Recurring pattern | Observed count | Resolution and status |
| --- | ---: | --- |
| Recovery steps rejected by the response contract | 17 | Added a concrete JSON example, explicit string-only step instructions, and the invalid proposal in corrective feedback. Independent review and evidence validation remain mandatory. Needs production monitoring. |
| Recovery model HTTP 400 | 6 | Earlier diagnosis identified unsupported JSON grammar on the local Transformers backend. Recovery JSON calls already route to the remote reviewer. Verify subsequent events stay clear of this transport failure. |
| Recovery JSON could not be parsed | 5 | Existing parser handles fenced JSON and corrective regeneration. The email alone does not establish the cause of every parse failure; do not assume every one was a Markdown fence. Monitor future failures with response-shape diagnostics. |
| Recovery evidence did not exactly match findings | 1 | Keep the rejection. Corrective feedback now includes the invalid proposal; exact source quotation is still required. A future improvement is stable evidence IDs resolved to stored text. |
| Prose or Library context approved as a command | 3 | Entries 60, 63 and 65 are analysis/reference text. Added pre-execution rejection for those observed response forms, including fenced responses, with regeneration through the existing correction path. Approval mode does not make prose executable. |
| Repeated quality-pipeline commands | 42 approvals | Many are legitimate author/director/independent-review checks. Do not remove quality gates. Attribute each run to task, source digest and result before deciding whether any runs are redundant. |
| Repeated directory listing | 10 approvals | Gather file inventory once per workspace revision and reuse it while unchanged. This is a proposed optimization, not evidence that these commands all failed. |
| Web-specific probes on Python portfolio work | 6 approvals | Three script-reference checks and three selector checks. Proposed change: select checks using inspected project assets; retain them for mixed web/Python projects. No blanket removal of web validation. |

## Underlying delivery gaps

Reviews found that passing checks on an API client did not prove delivery of a
portfolio report formatter. The recovery plan must retrieve the earlier `q1`
format decision, inspect the actual input schema, implement the formatter,
generate a report artifact, and test its contents against the original contract.
If the format decision is unavailable, investigate it and record the dependency;
do not silently invent a preferred format or relabel sample data as verified data.

Trading integration similarly needs inspection of the agent entry point,
strategy module, P&L tracking and the integration path. A standalone wrapper
does not satisfy the original integration requirement. Require a test exercising
that integration and independent review of the original criteria.

## Reporting and follow-up

Future email summaries now show recoveries queued, awaiting inputs, failed to
apply and other outcomes, while preserving one line per action. Existing sent
reports remain unchanged. Queued recovery is not successful recovery or completion.

Next useful measurements are recovery application rate, recurring failure
fingerprints, verified artifact changes, source-digest changes, elapsed time and
budget used per root story. Any evidence cache must be invalidated by changed
source, environment or relevant inputs; independent reviewers still assess the
deliverable and all original acceptance criteria.

Continuous recovery remains exclusive to Always approve. Budget limits, quality
checks, exact evidence requirements and independent acceptance review remain
enforced. These changes do not raise spending limits.

Validation for the new command guards, recovery prompts and email summary:
103 focused tests passed (file-tool adapter, heredoc continuation, approval
automation and strict acceptance suites). This does not establish that all
historical model/service failures have been eliminated in production.
