# Evidence

## Purpose
Reach for this whenever an agent makes a factual claim, reports a result, or
draws a conclusion the player or a client will rely on -- research summaries,
status updates, competitor profiles, test results, product claims. It is the
rule that every claim carries its source and every number is verifiable.
Mirrors the audit-window discipline from birmeya's operational safety plan and
the claim-verification discipline from the "AI research machine" post by rari
(@0xwhrrari).

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
- Bind every research task to a short contract BEFORE the first query:
  QUESTION (what exactly), DECISION (what the answer will be used for), AS OF
  (the date/freshness window that matters), SCOPE (what is in and out),
  COMPARE (which alternatives/axes), OUTPUT (what the deliverable must be),
  and STOP WHEN (the evidence that makes the answer good enough). A question
  without a contract is the #1 cause of broad-but-wrong research.
- State claim status explicitly, one of: supported (the linked passage
  directly says it), contested (credible sources disagree or limit it),
  unverified (no adequate direct evidence found), stale (once true, past its
  freshness window), not applicable. Never let a claim sit as an implicit
  "supported" because it sounds right.
- Independence check: "two sources confirm it" is weak if both copied the
  same press release or trace to one original. For any conclusion that
  matters, verify the path back to a primary source and, when one exists,
  at least one genuinely independent source. Name the publisher and the date,
  not just the URL.

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
5. For any time-sensitive fact (pricing, availability, leadership, live data),
  record the "as of" date you checked it, and treat the claim as stale the
  moment its freshness window passes -- a freshness date turns silent drift
  into a visible maintenance task.
6. When sources disagree, do not silently average them. Record the
  disagreement, inspect their dates and methods, and explain which one answers
  the player's question. Sometimes the honest result is "the evidence does not
  settle this."

## Sources
- Adapted from birmeya's "run an AI company with this safety plan" post
  (audit windows / evidence discipline) on X.
- Research-contract, claim-status, and independence-check ideas from rari
  (@0xwhrrari)'s "Research Engineering: Build an AI Research Machine" post on
  X, 2026-10-07.

## Lessons learned
- The cheapest way to lose trust is one confident unsourced number. The
  evidence rule exists so the player can always trace a claim to its origin.
- A research contract is the cheapest fix for "researched a lot, answered the
  wrong question": binding scope, as-of, and stop-when before the first query
  beats re-searching after a vague first pass.
