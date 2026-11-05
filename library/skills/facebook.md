# Facebook / Instagram research (teams own this)

The tank hardcodes nothing for Facebook or Instagram beyond session plumbing
(keeping an approved logged-in profile alive across calls). Everything else
is your recipe.

## What the tank provides

- `/api/browse` and `/api/execute` with the persistent browser profile —
  the neutral headed-browser primitive.
- The credential ask flow: if a Facebook/Instagram session needs to be
  established, the owner logs in once through the approved flow; you use the
  session, never the password.

## Recipe: read a public page or group

1. Open the target URL with `/api/browse` (op `goto`), then `read` the
   rendered content. Facebook serves logged-out visitors a degraded page —
   expect partial content for groups/marketplace.
2. For anything requiring a session, file the card explaining WHAT you need
   to read and WHY; the owner decides whether a logged-in profile is
   provisioned for it.
3. Screenshot evidence via `screenshot` op; save under `library/research/`.

## Guardrails

- Never create accounts, never message, never interact (likes/comments) —
  read-only research unless a card explicitly says otherwise and the owner
  approved it.
- Facebook actively bot-detects: slow, human-ish pacing; back off on
  checkpoints rather than retrying aggressively.
- No credential harvesting; if a page asks you to log in, stop and file.
