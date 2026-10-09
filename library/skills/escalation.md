# Escalation

## Purpose
Reach for this whenever an agent hits something it must not decide alone: a
possible red line (see red-lines skill), a request needing a capability or
credential it lacks, an ambiguous instruction that risks the wrong outcome, a
spike/sprint decision, or an operation whose failure would be expensive.
Escalation is how a bounded AI agent hands a decision back to the human
instead of guessing. Mirrors the handover ladder from birmeya's operational
safety plan.

## Key facts
- Escalate when the cost of being wrong on your own is higher than the cost of
  interrupting the player: red-line risk, credential/capability gaps, unclear
  scope on irreversible work, or any "I can't verify this is right" moment.
- A good escalation states, in this order: WHAT (the decision or blockage),
  WHY now (why you can't safely proceed), the OPTIONS you see with their
  trade-offs, and a RECOMMENDATION. Never dump a vague "can you help?" -- give
  the player a decision to make, not a puzzle to solve.
- Escalations are for decisions, not for delegation of thinking: do the
  analysis first, present the recommendation, let the human confirm.
- The handover is a ladder, not a wall: escalate with enough context that the
  player (or another agent on resume) can continue without re-deriving
  everything. Include what's done, what's pending, and what's at risk.
- Distinguish escalation from error: a failure you can retry safely is not an
  escalation; a failure you must not retry without approval is.

## Policy for this think tank
1. Use the existing escalation mechanism (e.g. `create_escalation` for
  approvals like API service proposals); for general decisions, raise a
  concise escalation with WHAT / WHY / OPTIONS / RECOMMENDATION.
2. Never proceed past a red line "because the player will probably approve" --
  that is exactly what must go to a human first.
3. Every escalation is logged with the decision it led to, so the pattern is
  auditable and repeatable.
4. When in doubt between "try once more" and "escalate," escalate on anything
  irreversible or anything that crosses a red line.

## Sources
- Adapted from birmeya's "run an AI company with this safety plan" post
  (five-rung handover ladder) on X.

## Lessons learned
- The most common failure is escalating too late or too vaguely. Escalate at
  the first red-line risk, and always arrive with options plus a
  recommendation.
