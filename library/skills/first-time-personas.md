# First-Time Personas: three strangers who test differently

**When this loads**: you are doing a first-time-user walkthrough (see
`first-time-user-walkthrough.md`) and need to test beyond your own
temperament. Walk the artifact THREE times, once as each persona below.
Do not average them -- each persona finds defects the other two walk
past, and you report what EACH found.

## The Impatient One

Assumes everything is slow and that waiting is a personal insult.

- Will not read instructions, tooltips, or helper text. If a screen
  cannot be operated by its visible affordances alone, the impatient one
  fails it.
- Clicks the primary action IMMEDIATELY, before forms are "ready". Submits
  half-filled forms on purpose (accidentally, from their perspective).
- Gives up at the first spinner longer than ~3 seconds and tries something
  else -- often the back button, often mid-write.
- **What they find**: slow paths that lose work, dead first clicks, forms
  that need reading before they make sense, missing loading feedback.

## The Confused One

Assumes nothing and understands less. This is the majority of real users.

- Cannot tell headings from buttons; clicks labels expecting them to act.
- Misreads terminology: your "workspace" is their "folder"; your "deploy"
  is their "publish?"...??".
- Fills fields with plausible-but-wrong values (a name in a date field)
  and then cannot understand the validation error.
- Asks, at EVERY step: "what just happened?" and "is this done?" If the
  screen cannot answer in its own text, it fails.
- **What they find**: jargon, silent state changes, error messages that
  blame the user, success states indistinguishable from failure.

## The On-A-Phone One

Has one hand, half a screen, and an interruption always imminent.

- Everything must work with a thumb: the action they need is reachable,
  not floating in desktop-only whitespace or a hover menu.
- Views collapse into scroll -- they WILL scroll, but the thing they came
  for must not require 4 screens of it.
- Context switches constantly: a phone call arrives mid-form. When they
  come back, is their input still there? Is it obvious where they were?
- Touches everything: small targets, accidental taps on adjacent items,
  no hover anywhere.
- **What they find**: hover-only affordances, cramped hit areas,
  desktop-only layouts, flows that die on rotation/interruption,
  autoscroll jumps.

## Reporting

Report per persona: the step where the persona FAILED or got stuck, what
they expected, what happened. A finding counts only if it is one the
personas' BEHAVIOR produced (not a code review of the artifact). If a
persona found nothing, write `impatient: clean` (etc.) -- a persona that
finds nothing is a result, not a skipped step.
