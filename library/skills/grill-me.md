# Grill-Me: interrogate the idea before anything gets built

**When this loads**: you are about to turn an idea, request, or ambition
into a BUILD -- a card, a story, a repo, a purchase, an integration. Before
a single line is written, the idea must survive an interrogation. A build
that cannot answer these questions is not ready to be built; it is ready to
be questioned.

## The interrogation (run it as a real interview, out loud in the room)

Ask each question and WAIT for the answer. Do not answer for the idea's
author. Do not soften. If the answer is "we'd figure it out later", the
grill is not done.

1. **Who exactly suffers without this?** A name or a role, not "users".
   Nobody identifiable means nobody is waiting.
2. **What do they do today instead?** If the answer is "nothing, they just
   don't have this", find out what ACTUALLY happens today. A problem that
   has no workaround is usually a problem nobody has.
3. **What is the smallest thing that would make this real?** One card, one
   afternoon. If nothing qualifies, the idea is a program, not a task --
   decompose it before grilling further.
4. **What does this REPLACE?** Every build displaces something: a manual
   step, an old tool, an agent's hours. Name what dies. If nothing dies,
   you are adding maintenance load for life.
5. **What does "done" look like in one sentence, checkable by a stranger?**
   A stranger must be able to verify it from the artifact alone.
6. **What breaks if this is wrong?** Data loss, wrong numbers, a broken
   flow? That determines review depth and who reviews.
7. **What does this cost to KEEP?** Not to build -- to keep. On-call,
   dependencies, a new external service with keys, a thing that rots.
8. **Why now?** If the answer is "it would be neat", park it in the backlog
   with a revisit date instead of building it.

## Verdicts

- **BUILD** -- every question has a concrete answer; the smallest version
  is named; what dies is named. Write the card with the grill's answers in
  the instructions.
- **SPIKE** -- one or more answers are guesses that code could settle
  cheaply. The spike's deliverable is ANSWERS, not the build.
- **PARK** -- the idea survives but now is wrong. Backlog with a revisit
  date and the unanswered questions attached.
- **KILL** -- no sufferer, no replacement, no "done". Say so plainly and
  move on. Killing an idea in five minutes is the cheapest win there is.

## Rules

- The grill happens BEFORE the card is filed, not during the build.
- The answers go in the card's instructions verbatim -- the builder should
  not have to re-derive them.
- A reviewer who finds the grill's answers missing sends the card back;
  "we'll figure it out later" is a review failure, not a style choice.
- Grilling your OWN idea is allowed and encouraged; grilling it out loud
  with someone else is better.
