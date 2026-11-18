# Review assessment: "knowledge-case coherence" review, 2026-11-18

Reviewer's core claim: the tank has the foundations of epistemic control but
lacks integration, and the next phase should be knowledge-case coherence and
measurable outcomes, not more autonomous activity.

Verdict: the reviewer is right about the destination and wrong about how far
away it is. Roughly half of what the review asks for already exists in
core/epistemics.py and the action_log/tooling around it, including most of
Priority 2. The genuine gaps are the case-level composite object, a release-
gated reachability/invariant suite, and a unified briefing. Several of the
reviewer's factual claims are outdated or understated.

## Claim-by-claim verification

| Reviewer claim | Verified reality |
|---|---|
| serve.py ~15,700 lines, sim.py ~10,200 | Understated. world/serve.py is a 166-line loader; the concatenated `server/serve_parts/*.py` total **62,646 lines**. sim_parts total **23,361**. The size concern is real and larger than stated. |
| audit_reachability.py and _gapmap.py exist, "direction has begun" | Correct: `world/tools/audit_reachability.py` (80 lines) and `world/tools/_gapmap.py`. But **neither is referenced in ci.yml** -- it is a tool, not a release gate. This is the cheapest high-value adoption in the review. |
| Test quantity vs system properties | 9,750 test functions. Keyword proxies across test files: auth 178, cap 243, budget 125, stale 86, village 110, retry 68, persist 48, restart 38, recover 41, **cross-village 4, contradict 8**. Volume exists; the thin spots are cross-village memory isolation and contradiction flows. No first-class invariant suite naming/CI gate. |
| Missing: knowledge-case layer | **Mostly exists, wrongly framed.** core/epistemics.py (1,524 lines) already models: ESTABLISHED vs ASSERTED, evidence attach with a supports flag, independent-corroboration check (same source never counts twice), contradict + resolve-with-winner-claim, supersedes on refresh, stale-but-readable, validations as costed unknowns through the Bank, a review gate (a different agent than the completer must approve), benchmark grades (Known / Hypothesis / Unsupported Assumption / Contradiction / Stale Evidence / Unknown), and both HTTP (`/api/epistemics`, `/api/epistemics/brief`) and agent-tool surfaces. What is genuinely missing: the **case-level composite** binding question -> ledger items -> tasks -> wiki -> owner -> deadline -> disposition in one durable object. |
| Missing: contradiction semantics | **Largely implemented.** supports/contradicts/supersedes exist; a conclusion cannot be validated past an open contradiction (review gate + disputed grade). Missing only: partial_supports, duplicates, context_only relations and an explicit E0-E4 strength scale (the grade classes approximate strength already). |
| Missing: daily intelligence briefing | Partial. `/api/epistemics/brief` returns the epistemic state; telegram alert digests exist in library/analysis. Missing: ONE briefing joining material changes, newly validated claims, contradictions, expiring knowledge, blocked work, decisions requiring the player, next actions, spend. |
| Missing: runtime validation as first-class work type | The ledger's validation objects already carry procedure, cost, blocked status, result, review, disposition. The reviewer's richer schema (expected vs actual telemetry, correlation ids) is a reasonable extension of that existing type, not a new layer. |
| Missing: user-level synthesis | Real gap. The state is scattered across rooms/tasks/ledger/digests. `/api/epistemics/brief` covers beliefs only. |
| Missing: outcome evaluation | Real gap, cheap to start: most proposed metrics are derivable from action_log + the ledger (reopen rate, contradiction-check rate, time-to-validated, cost per validation via Bank records). |

## Adoption order (grounded)

1. **Release-gate the reachability + invariant suites (Priority: highest, cost: low).**
   Wire audit_reachability.py into CI as a blocking step, and add an
   explicit invariant suite with the reviewer's named properties (no
   consequential tool execution without a decision record; no village
   retrieves another village's scoped memory; no dependency-blocked task
   reaches execution; no completed task disappears after restart; no retry
   duplicates an external side effect). The thin spots (4 cross-village
   files, 8 contradict files) get targeted additions here, not a mass of new
   tests everywhere.
2. **Investigations composite (Priority 1, scoped down).**
   Do NOT rebuild the ledger. Add a thin `investigations` entity that
   references ledger itemIds, task ids, wiki page ids, owner, deadline,
   reassessment date, and final disposition. The ledger stays the truth
   store; the case is the index the human reads.
3. **Unified daily briefing endpoint.**
   Join epistemics brief + action_log material changes + open decisions +
   Bank spend into one `/api/briefing` (and the Telegram morning push).
   Score: what changed / what we now believe / what is unproven / what next.
4. **Evidence-strength scale E0-E4 as a field** on ledger evidence entries,
   defaulting from the existing grade classes; contradiction promotion rule
   (an E3+ contradiction blocks validation) falls out naturally.
5. **Outcome metrics endpoint** computed from action_log + ledger + Bank.
6. Standing background work: continued part extraction (the route-shadow
   pilot this week showed the pattern), e2e scenario evals last.

## Pushback

- "Authorization checks may differ between endpoints": TheoPolicy
  centralizes lane actions and the operator gates (`_operator_gate`,
  `_require_player_or_admin`) are shared. The risk is real but the remedy
  is exactly the reviewer's own ask (prove it with the reachability gate),
  not more controls.
- The review undercounts what exists (15.7k vs 62.6k lines; epistemic
  ledger not credited). Its five "missing" items are really two missing
  (case composite, unified briefing), one partial (briefing), one mostly
  built (contradiction semantics), one extension of an existing type
  (runtime validation).
- Priority 5's scenario harness is expensive to stabilize; do it after the
  metrics endpoint says which scenarios matter.

-- Assessment prepared 2026-11-18 against main @ a7b4ea9.
