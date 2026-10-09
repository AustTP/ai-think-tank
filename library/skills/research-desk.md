# Research Desk

## Purpose
Reach for this whenever the village runs a scheduled research lane -- the
standing watchlist topics the sim queues as "Scheduled research" work -- or
any time a task asks you to monitor a set of sources and report what
materially changed. It is the operating contract for a recurring research
operation: a defined pipeline (INTAKE to DELIVERY), an evidence ledger for
every claim, a structured before/after record for every change, and an
exception rule that says when NOT to answer. Mirrors the "Intelligence
Factory" research-infrastructure model: the value is the accumulated
evidence and the consistency of the loop, not one clever prompt.

## Key facts
- Run the seven stages in order. Each one exists because the previous one
  cannot safely replace it:
  INTAKE (what decision the work serves) -> DISCOVERY (candidate items) ->
  RETRIEVAL (open the original, capture real passages) -> EVIDENCE (record
  each claim with source, date, status) -> VERIFICATION (does the source
  actually support it; is there contrary or newer evidence) -> ANALYSIS
  (business implication) -> DELIVERY (report, alert, or briefing).
- A search snippet is not evidence. Retrieval opens the original material;
  the evidence record carries the exact passage and the URL.
- More information is not better intelligence. 200 articles repeating one
  wrong claim are worth less than two independent primary sources. The goal
  is fewer unsupported conclusions, not more pages.
- Claim status is explicit, one of: supported, contested, unverified, stale,
  not applicable (the same status language as the evidence skill).
- A repost is not a second independent source. Two outlets quoting the same
  press release confirm nothing independently.
- Record changes as before/after observations, not just prose. "The vendor
  changed pricing" is a summary; `price: $100 -> $95, at <date>, from <URL>`
  is a reusable record. A history of observations lets a later run answer
  "how did X change over the quarter" without re-researching.

## Policy for this think tank
1. Record every claim through the evidence ledger (POST /api/evidence) with
   a resolved source URL, then record each detected change through
   POST /api/evidence/observation with the fields `claimId`, `field`,
   `before`, `after`, `sourceUrl`, and a short `note`. Call it once per
   change, not once per mention; the ledger dedupes reposts and reuses the
   claim's observation history.
2. Check the coverage rule before re-reporting: an identity that is already
   covered (reviewed) is skipped unless the source shows a material update
   after the covered date. Recording an observation on an existing claim is
   still correct when a change is real.
3. Verification is a separate stage, not an afterthought. Before a finding
   enters delivery, re-open the source and confirm the passage supports it;
   name the publisher and the date. When no independent source exists, say
   so and mark the claim unverified rather than presenting it as fact.
4. Exception engine: route these to the player/escalation queue instead of
   delivering a confident answer -- contradictory claims between credible
   sources, an inaccessible original source, an unusual numerical change
   (price/down 40% overnight), or any conclusion with significant financial
   consequences. A completed run is one that knows when an answer would be
   unjustified.
5. End every run with the operating report: fetch GET /api/research-desk/ops
   and fold the numbers into your run summary -- claims recorded, claims
   blocked vs pending, observations logged, covered identities, watchlist
   size, and seven-day spend. Report what you could NOT verify alongside what
   you delivered. The report is how the operator measures whether the loop is
   earning its autonomy.
6. Keep the output contract explicit before the first query: QUESTION (what
   exactly), DECISION (what the answer is used for), AS OF (freshness window),
   SCOPE (in/out), COMPARE (alternatives/axes), OUTPUT (deliverable shape),
   STOP WHEN (the evidence that makes the answer good enough). A question
   without a contract is the #1 cause of broad-but-wrong research.
7. Never manufacture a finding. If nothing material changed in the window,
   say so plainly; an empty result is a valid result when the sources were
   checked and nothing moved.
