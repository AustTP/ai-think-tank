# Evidence

## Purpose
Reach for this whenever an agent makes a factual claim, reports a result, or
draws a conclusion the player or a client will rely on -- research summaries,
status updates, competitor profiles, test results, product claims. It is the
rule that every claim carries its source and every number is verifiable.
Mirrors the audit-window discipline from birmeya's operational safety plan.

## Key facts
- Every claim = claim + source. "X is true" is not a report; "X, from [URL /
  file:line / test run], as of [date]" is.
- Distinguish fact from inference: facts carry a source; inferences carry
  `[inferred]` and the reasoning. Never let an inference wear a fact's
  clothes.
- Numbers must be traceable: cite the run, the dataset, the page, or the
  measurement. An uncited number is a guess and gets flagged as
  `[assumption]` or `[NEED: ...]`, never padded.
- For AI agents, this matters doubly: a confidently wrong number is worse than
  a confessed unknown, because it's harder to catch. Say "I don't know"
  plainly and propose the cheapest way to find out.
- Verification before delivery: cross-check claims against their source, note
  the date (sources rot), and flag anything you could not verify.
- Evidence has a lifecycle: record it in the project log so a later reader or
  a resumed session can audit where the conclusion came from.

## Policy for this think tank
1. No deliverable ships with a factual claim that lacks a source or an
  `[inferred]` / `[assumption]` / `[NEED: ...]` marker. This is a hard rule,
  not a style preference.
2. Before delivering, re-open the source and confirm the claim matches; if the
  source is gone or changed, say so.
3. Prefer primary sources (the service's own docs, the actual page) over
  secondary summaries; when using a secondary source, name it.
4. When evidence is missing and hard to get, escalate the decision (see the
  escalation skill) rather than guessing.

## Sources
- Adapted from birmeya's "run an AI company with this safety plan" post
  (audit windows / evidence discipline) on X.

## Lessons learned
- The cheapest way to lose trust is one confident unsourced number. The
  evidence rule exists so the player can always trace a claim to its origin.
