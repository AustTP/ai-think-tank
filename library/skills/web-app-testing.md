# Web app testing with Playwright

Source: anthropics/skills @ ca45fc4 (Apache-2.0), skills/webapp-testing; adapted to house format 2026-10-31.

## Purpose
Reach for this whenever a card needs to verify that a web UI actually works -- frontend
behavior, rendered content, forms and flows -- or to debug UI behavior in a real browser.
The repo's CI already runs Playwright chromium for its browser suites; this skill is the
discipline for writing those checks by hand when a card calls for browser verification. It
exists because a UI "works" only when a browser says so: rendered DOM, not markup hopes.

## Key facts
- Write native Python Playwright scripts; launch chromium headless, always.
- Reconnaissance before action on a dynamic app: navigate, wait for network idle (JS must
  finish), screenshot or inspect the DOM, THEN pick selectors from what is actually
  rendered. Never guess selectors from source alone on dynamic pages.
- Static HTML can be read directly to find selectors; if the read does not explain the
  behavior, treat the page as dynamic and use the browser.
- Manage server lifecycle outside the test logic: start the app server(s), wait for the
  port, run the automation, tear down. Upstream ships scripts/with_server.py for exactly
  this (single or multiple servers); fetch it from the source repo rather than rewriting
  process management per card.
- Verify by evidence: screenshots and DOM assertions, plus browser console logs for errors.
  A clean screenshot with a console full of errors is not a pass.

## Policy for this think tank
- Browser-verification cards must state what will be asserted (selector, expected state)
  in the acceptance criteria, so the reviewer can check the assertion, not just the run.
- Never claim a UI fix is verified from code reading alone. The gate evidence is the
  Playwright run output (or its screenshot/DOM capture) saved with the card.
- Do not paste third-party pages' content into reports as if verified; a rendered page is
  evidence of that render only, at that time.

## Sources
- anthropics/skills skills/webapp-testing/SKILL.md @ ca45fc4 (Apache-2.0; full terms in
  the repo's LICENSE.txt); scripts/with_server.py lives in the same directory upstream.
