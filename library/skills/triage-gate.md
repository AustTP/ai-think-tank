# Triage Gate

## Purpose
Reach for this before any agent promotes a candidate onto a permanent
surface: a deliverable handed to the player or a client, a new skill added to
the library, an artifact published, or a claim written to the wiki. It is the
think tank's policy-as-code quality gate: a fixed question set, a fixed
verdict set, and an explicit conservative default, so that promotion is a
decision the gate explains instead of a vibe the agent had. Generalized from
a security-research triage gate whose job was separating payable findings
from noise; the discipline transfers to anything this think tank decides to
keep.

## Key facts
- The gate makes "no" cheap and respectable. The correct, professional
  outcome after an honest pass is often "nothing to promote" -- not another
  token spent polishing something that was never good enough. Closing a
  candidate as not-good-enough IS the successful use of this gate.
- This gate overrides the "keep going" instinct. Conclude, log, move on.
- Answer each question with a hard YES or NO. No "maybe", no "probably".
- One NO on a structural question is a KILL. Q3 and Q5 route to REVISE or
  HOLD instead of an outright kill (see the verdict table).
- The FINAL verdict you state is the authoritative one. If your reasoning
  discusses several verdicts before concluding, only the last one counts.
- Ambiguity or low confidence never resolves upward: it routes to the
  escalation skill, never to ACCEPT.
- Novelty dedup counts only candidates that were actually kept (ACCEPT /
  REVISE) earlier in the session. A held primitive must not suppress the
  later candidate that completes it.

## The 7 questions

1. **Real request?** Does a concrete person gain something from this
   candidate existing, or is it a theoretical exercise dressed as work?
2. **In scope?** Is it inside the think tank's mandate and the current
   project or client brief, and does it cross no red line (see red-lines
   skill)?
3. **Accepted standard?** Does it meet the bar the brief or house-format
   actually pays for, rather than landing in an "informative" bucket?
4. **No unearned assumptions?** Does it work without pretending the think
   tank holds capabilities, data, access, or victim behavior it does not
   have?
5. **Provable in this environment?** Is there concrete evidence available
   now: a file, a trace, a citation, a rendered result, a reproducible
   observation? "This would work if I had X" is a NO.
6. **Novel?** Is this distinct from everything already delivered or kept
   this session, or a reworded duplicate of the same underlying idea?
7. **Not on the never-submit list?** Not on whatever this think tank's
   current never-promote list is (fill the list in below; examples: trivial
   fragments, unverifiable claims, "a developer could theoretically...").

## The verdicts

| Verdict | When | Action |
|---|---|---|
| **ACCEPT** | All 7 are YES | Promote. Deliver, file, or publish. |
| **REVISE** | Real work, fixable problems | Return with specific fixes. Fix, then re-run the gate. |
| **HOLD** | Real primitive, not reportable alone | Keep as a building block. Go find the other half (data access, channel, second proof). |
| **KILL** | Any NO on Q1, Q2, Q4, Q6, or Q7 | Drop it. Log one line ("KILLed: <what>, reason: <Q#>") so it is never re-tested, then move on. |

Q3 or Q5 NO is what separates REVISE and HOLD from KILL: the candidate has a
sound core that fails only on polish or on an incomplete chain.

## Policy for this think tank
1. The gate is mandatory as the last step before any promotion surface. No
   deliverable, skill, artifact, or wiki claim ships without a verdict.
2. Structural NOs are terminal: never write it up "anyway" and never soften a
   KILL into a REVISE by arguing with Q1, Q2, Q4, Q6, or Q7.
3. Log every verdict where the work is tracked (project memory.md or the
   project log), one line per decision, so promotion patterns stay auditable
   and repeatable.
4. When the judge (you) is unsure which verdict fits, that is not ACCEPT:
   raise a concise escalation per the escalation skill and let the human
   decide.

## Sources
- The player's adversarial-ai-swarm project, `skills/triage-gate.md`: the
  7-question kill switch for security findings, generalized here to every
  promotion decision the think tank makes.
- Pairs with the escalation skill (human-decided outcomes) and the
  red-lines skill (scope enforcement inside Q2).

## Lessons learned
- A held item must never KILL its own completion. Semantic duplicates that
  differ only in wording are real duplicates; a finding that finishes a held
  chain is not one. Count only kept priors in Q6.
- The most common gate failure is severity inflation: writing at the verdict
  the agent hoped for instead of the one the answers produced. Score first,
  deliver at the answered severity, never the hoped-for one.

## Never-promote list

(Placeholder. Each think tank keeps its own list here and updates it as
lessons accumulate. Start empty; add items only with a logged reason.)