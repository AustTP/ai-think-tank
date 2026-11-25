# Merge Risk Assessment

Source: warp.dev/merge-risk skill (Ben Holmes, Warp Factories, 2026-11-24); adapted to house format 2026-11-25.

## Purpose
Reach for this BEFORE opening a pull request or marking a coding card ready
for review. Assess then mitigate: run the five checks, fix what a low cost
fix can fix (add the missing log line, collect the missing screenshot, split
the one-way-door change), and put the verdicts in the PR so the reviewer can
triage at a glance which PRs need the closest reading. A PR without risk
sections is unreviewable at scale.

## Key facts
Score five fixed categories. For each: a **Verdict** (low / medium / high),
a one-paragraph summary, and the evidence you inspected.

1. **Alignment** -- Does the diff match the stated request, and nothing else?
   Undocumented drive-by changes are their own finding even when harmless:
   a reviewer cannot approve what they cannot scope. List anything outside
   the stated scope as an explicit finding.
2. **Impact** -- Blast radius. Isolated view, shared component, or core
   library? Any touch on auth, permissions, payments, database schemas,
   persistence formats, or concurrent-system invariants raises the floor to
   medium at minimum.
3. **Verification** -- What evidence exists: computer-use walkthrough, end-to-
   end run, unit tests, static checks? Name the command and its result. "The
   tests pass" without the command and its output is not evidence
   (verification-before-completion applies).
4. **Rollback cost** -- Two-way door (commit revert, feature flag) or
   one-way door (destructive schema change, persisted-state format change,
   API contract break)? One-way doors raise the floor to high.
5. **Error detection** -- If this breaks in production, does anything notice?
   Monitoring, logs, alerts, client-visible error text. Silent failure modes
   raise the floor to medium at minimum; silent failure of one-way-door news
   raises it to high.

Run the assessment BEFORE opening the PR and mitigate what a cheap fix can
fix (add the missing log line, collect the screenshot, split the one-way-
door commit); re-grade the categories that changed. A PR without the five
verdicts is unreviewable at scale.

Overall risk = the highest category verdict. State the risk-increasing gaps
you could NOT mitigate as the residual, so the reviewer sees what is left.

## Policy for this think tank
- Carry the five verdicts into the PR description as five `## Impact`-style
  sections with a bolded **Verdict** line each, plus the residual-gaps list.
- One-way-door findings and unmitigated high verdicts belong in the PR's
  first screen, not buried under a collapsible.
- If a mitigation was rejected or impossible, say why in the residual list
  rather than silently keeping the gap.
- This assessment never replaces verification evidence; it grades it.

## Sources
- warp.dev/merge-risk (Warp Factories, @BHolmesDev article 2026-11-24,
  "How to build a software factory you'll actually use").
- `library/skills/verification-before-completion.md` for the evidence gate
  this assessment grades.
- `library/skills/triage-gate.md` for hard-verdict discipline (no "maybe").
