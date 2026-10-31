# Test-driven development

Source: obra/superpowers @ 2ce8aa1 (MIT), skills/test-driven-development; adapted to house format 2026-10-31.

## Purpose
Reach for this whenever a code card asks you to implement a feature, fix a bug, or change
behavior. The dev lane's quality pipeline (pytest, flake8, bandit) can only judge what
exists; this skill is about writing the proof FIRST so the pipeline judges the right thing.
It exists because code written before its test tends to be "verified" by reading it, which
is how bugs and empty changes reach the gate.

## Key facts
- The iron law: NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST. Code written before the
  test gets deleted, not kept as "reference". Implement fresh from the tests.
- The cycle is RED -> GREEN -> REFACTOR, and the RED step has a verification of its own:
  1. RED: write one minimal failing test that names the missing behavior.
  2. WATCH IT FAIL. If it passes immediately, the test tests nothing -- rewrite it.
  3. GREEN: write the smallest code that makes it pass. No extra features.
  4. REFACTOR with the test staying green.
  5. Commit at green, then next cycle.
- If you did not watch the test fail, you do not know whether it tests the right thing.
- Exceptions are rare and never self-granted: throwaway prototypes, generated files,
  config-only changes. When in doubt, it is not an exception.

## Policy for this think tank
- Bug-fix cards: the regression test IS the first deliverable. Write the test that
  reproduces the reported failure, watch it fail against the current code, then fix. The
  repo's own history follows this pattern (see the queue->task vanish guard in
  world/tests/village/test_repro_vanish.py).
- The "no deliverable published" refusal at the completion gate usually means the work was
  narrated instead of coded. If your change is real, the diff and the test run are real;
  show both.
- Do not weaken or delete existing tests to make a card pass. If a test is genuinely wrong,
  say so in the completion with the reason -- that is a reviewer decision, not an author one.

## Sources
- obra/superpowers skills/test-driven-development/SKILL.md @ 2ce8aa1 (MIT license); the
  upstream skill also ships a writing-good-tests.md reference worth fetching when authoring
  test-heavy cards.
