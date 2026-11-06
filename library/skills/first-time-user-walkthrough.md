# First-Time-User Walkthrough: click every flow before review

**When this loads**: you built something a human will CLICK -- a page, a
form, a preview, a flow with buttons. Before you send it for review, you
walk it the way a stranger would: cold, skeptical, on their terms. A
builder clicks their own work the way they expect it to be used; that is
exactly how the broken paths survive.

## The walkthrough

Open the artifact fresh (new page, no state, nothing prefilled). Then:

1. **Land as a stranger.** First impression in five seconds: can you tell
   what this IS and what you can DO here? If you need the builder's
   context to understand the screen, the screen has failed a stranger.
2. **Take the golden path without reading anything.** Click the most
   obvious thing at each step. If the golden path dead-ends, mislabels
   itself, or asks a question a first-time user cannot answer, that is a
   defect even if the code "works".
3. **Click EVERYTHING once.** Every button, link, tab, toggle, and empty
   state. Not because each is important -- because the one you skip is
   where the bug lives.
4. **Do the forbidden things.** Submit twice fast. Go back mid-flow.
   Refresh at the worst moment. Enter nonsense. Delete the thing you just
   made. The first-time user does all of this by accident.
5. **Try to get lost.** Can you tell where you are at every moment? Is
   there always an obvious way back? A stranger who feels lost leaves.
6. **Check the phone-shaped failure.** Narrow the window. Does it become
   unusable, overlap, or hide its own actions? A flow that only works at
   desktop width is half-built.
7. **Ask the three-persona questions** (see `first-time-personas.md`):
   the impatient one, the confused one, the on-a-phone one.

## The report

- Every defect you found, with the exact step that triggered it -- even
  the ones you suspect are "known".
- The step where you, knowing the code, still hesitated. That hesitation
  is the stranger's confusion, amplified.
- An explicit verdict: `walkthrough clean` or the defect list. A review
  without the walkthrough verdict is not a finished review.

## Rules

- The walkthrough uses the DEPLOYED artifact, not the localhost of your
  in-progress edits -- reviewers verify what will actually run.
- Fresh state every attempt; a walkthrough against your own cookies is a
  walkthrough of your own assumptions.
- If the flow needs credentials or data to exist, set them up as a
  first-time user would encounter them, and say so in the report.
- Time-box: a walkthrough that takes longer than the build probably
  means the flow is too complicated -- say that in the report too.
