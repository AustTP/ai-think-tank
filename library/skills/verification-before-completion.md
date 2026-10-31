# Verification before completion

Source: obra/superpowers @ 2ce8aa1 (MIT), skills/verification-before-completion; adapted to house format 2026-10-31.

## Purpose
Reach for this whenever you are about to claim a card is done, a bug is fixed, or a test
passes -- before you write the completion note, before the artifact hand-off, and before
any report that says something succeeded. It exists because this tank's most expensive
repeated failure is completion without evidence: cards closed with no deliverable at the
promised path, "done" reports describing work that exists only as chat text, and review
packets quoting a file that was never written.

## Key facts
- The iron law: NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE. If you did not
  run the verification in this task session, you cannot claim it passes. Evidence before
  assertions, always.
- The gate, in order: (1) NAME the command or check that proves the claim. (2) RUN it,
  fresh and complete. (3) READ the full output including the exit code. (4) Confirm the
  output actually supports the claim. (5) Only then make the claim, WITH the evidence in it.
- What a claim requires (not sufficient: anything less):
  - "Tests pass" -> the test command's own output showing zero failures.
  - "Deliverable saved" -> the artifact exists at the promised Library path; list it.
  - "Bug fixed" -> the original failing behavior now passes.
  - "Agent/subagent finished" -> the diff or artifact checked directly, not the report.
- Red flags that mean stop: the words "should", "probably", "seems"; satisfaction before
  verification; trusting another agent's success report; partial checks standing in for the
  full one. Skip any gate step and you are not verifying -- you are guessing in writing.

## Policy for this think tank
- The completion gate already refuses empty candidate changes ("no file was added or
  modified") and provenance-gaps plain research cards. Do not try to talk past it: if your
  work cannot pass the gate, the work is not done. Say so plainly instead of re-attempting
  a completion with the same empty change.
- An artifact must be saved through the Library write path and its reportPath cited in the
  completion. "It is in my chat message" is not a deliverable and never was.
- When you delegate part of a card to another agent, verify its output yourself before
  folding it into your completion. A fabricated or empty sub-result becomes YOUR empty
  deliverable at the gate.
- If verification genuinely cannot run (environment down, dependency missing), the
  completion says exactly that, with the error text -- an honest blocked report. A false
  "done" is worse than a slow card.

## Sources
- obra/superpowers skills/verification-before-completion/SKILL.md @ 2ce8aa1 (MIT license).
