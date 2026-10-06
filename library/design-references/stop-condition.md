# Stop Conditions: "Done" Is a Claim, Proof Is the Exit

Source: "Your AI Agent Doesn't Need More Tools. It Needs a Stop Condition"
(Sep 2026). An agent should stop because three things are true: the result
exists, the result works, and the proof is attached. Every loop gets an exit:
a retry limit, and "do not silently lower the standard." If the check still
fails after N attempts, stop and report the exact blocker.

## What the think tank already does

- **The result exists.** Every task carries `acceptanceCriteria` at intake
  (sim.py `_spawn_work_items`, `assign_task`), and the story instructions
  embed them into the agent's brief before work starts.
- **The result works.** A review's clean verdict only counts as an approval
  when the quality pipeline objectively passed (`pipelineOk`). A clean
  verdict with a red or missing pipeline is downgraded to actionable and
  sent back to the author (sim.py `_apply_content_result`).
- **The proof is attached.** The evidence-based done gate: coding-class work
  (pressoffice room or product-backed) cannot be approved on a "looks solid"
  alone. The executor files the real per-step pipeline output (coverage
  line, flake8/mypy/bandit tails) in `result['evidence']`; a clean vote with
  no evidence is downgraded the same way a red pipeline is.
- **Retry limit / don't silently lower the standard.** Per-task `budgetUsd`
  and `workUntil` bound how long a task may keep trying; the stale-work
  sweep re-plans wedged work. The red-pipeline gate never lowers the bar:
  a rejected card goes back to the author, and every reject is recorded via
  `_log_quality_gate_reject`, with `redPipelineEscaped` counting any result
  that completes anyway (the fail-closed invariant).
- **When another attempt is waste / when a human decides.** Repeated gate
  failures escalate to the player (`_maybe_escalate_stuck_gate`), and the
  rework/loop metrics in the health digest (loops, straight-through rate,
  loop cost) measure the churn the stop condition is meant to prevent.

## The deliberate scope decision

The evidence requirement is scoped to coding-class work only. Non-coding
lanes (observatory, pure research) rest on the `pipelineOk` check alone.

This is intentional. Non-coding work legitimately concludes with a negative
result (a research task with no findings has no pipeline output to attach).
Forcing an evidence field there would create false "no evidence" rejections,
which turn a task into the exact endless tool-call loop the article warns
about, only in review form. The backstop for those lanes is the rework
metrics: if a lane loops, the loop shows up in the health digest and the
rework alert, and repeated failures escalate.

Revisit this if a non-coding lane starts shipping deliverables that are
always observable (a report, a data file). Then require the deliverable
pointer (`libraryPath` or an artifact) on the review result instead of a
free-text evidence field.