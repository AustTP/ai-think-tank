# Systematic debugging

Source: obra/superpowers @ 2ce8aa1 (MIT), skills/systematic-debugging; adapted to house format 2026-10-31.

## Purpose
Reach for this whenever you hit a bug, test failure, build failure, or any unexpected
behavior -- BEFORE proposing a fix. It exists because this tank repeatedly pays for
symptom-fixing: cards that "fix" a flake by retrying it, coaching cards that blame agent
quality when the real cause was a transport outage, and fixes attempted before anyone read
the actual error.

## Key facts
- The iron law: NO FIXES WITHOUT ROOT-CAUSE INVESTIGATION FIRST. Symptom fixes are failure,
  not savings.
- Four phases, each complete before the next begins:
  1. ROOT CAUSE: read the error completely (stack traces carry their own answers);
     reproduce consistently; check what recently changed; in multi-component systems,
     instrument each boundary once to see exactly where the failure enters; trace bad
     values backwards to their origin.
  2. PATTERN: find working examples of similar working code or flows; compare what
     differs.
  3. HYPOTHESIS: one concrete, testable cause with a prediction.
  4. TEST: the smallest change that tests the hypothesis; prove it before the full fix.
- Pressure is not an exception. Under time pressure, guessing is MOST tempting and LEAST
  reliable; systematic is faster than thrash-and-revert.
- "It seems simple" is not an exemption. Simple bugs have root causes too.

## Policy for this think tank
- Distinguish transport failure from quality failure before assigning blame. "Model
  connection failed" is infrastructure; "candidate change is empty" is work. The coaching
- card misclassification incident (six transport flakes filed as quality problems) happened
  because nobody completed Phase 1 before filing.
- For CI reds: read the failing step's log first, reproduce locally where possible, and
  name the failing thing in the card. The 2026-10-31 env-template red was root-caused in
  minutes by exactly this: fetch the log, run the one failing test locally, fix the cause.
- If you cannot reproduce, gather more evidence -- do not ship a fix for an unreproduced
  failure and call it done.
- Upstream ships deeper references (root-cause-tracing, defense-in-depth,
  condition-based-waiting, find-polluter.sh); fetch them from the source repo when a bug
  outlives the quick version of Phase 1.

## Sources
- obra/superpowers skills/systematic-debugging/SKILL.md @ 2ce8aa1 (MIT license), plus
  referenced root-cause-tracing.md / defense-in-depth.md / condition-based-waiting.md in
  the same directory.
