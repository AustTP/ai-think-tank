# PixelLab

## Purpose
Reach for this before generating any real character sprite art via the PixelLab
API -- e.g. the per-agent unique sprite project (see the "Per-agent sprites"
product).

## Key facts
- Base URL: `https://api.pixellab.ai/v2`. Auth: `Authorization: Bearer <key>`.
  The key is vault-held under credential name `pixellab` -- request a scoped
  capability handle (host `api.pixellab.ai`, methods GET+POST) rather than
  asking for the raw key.
- **Account status verified**: $7.41 in pay-as-you-go credits,
  plus an active "Tier 1: Pixel Apprentice" subscription (2000 generations/mo
  included, 21 used so far this period). Tier 1 also unlocks the
  skeleton-driven animation endpoint (see below), which is gated to tier-1+.
- **Character generation**: `POST /create-character-with-4-directions` with a
  text `description`, `image_size` ({width,height}), `view` (e.g.
  "high top-down"), `template_id`. Returns `character_id` + a
  `background_job_id`; poll `GET /background-jobs/{id}` until
  `status: "completed"`, then `GET /characters/{character_id}` for
  `rotation_urls` keyed by direction. This is the ONLY endpoint the think tank's
  existing spike script (`scripts/pixellab_spike.py`) has actually exercised
  and confirmed working -- read that file for a real, tested call shape before
  writing a new one from scratch.
- **Walk-cycle / animation frames**: `POST /animate-with-text-v3` takes one
  reference frame image plus a text description of the motion (e.g.
  "walking") and generates the in-between animation frames from it. This is
  the natural way to turn one static direction into that direction's walk
  frames.
- A higher-fidelity alternative is `POST /animate-with-skeleton-v3` (tier-1+,
  which this account has): draws every frame from one reference image, posed
  by an 18-joint skeleton supplied per frame (3-15 frames). More control, more
  setup work than the text-prompt version -- worth trying if
  `animate-with-text-v3`'s output quality isn't good enough, not required for
  a first working version.
- Jobs are async (background job + poll pattern) for every generation call, not
  synchronous request/response -- budget real wall-clock time per sprite set,
  and don't block a single task's whole time budget waiting on one job; the
  existing `poll_job()` helper in the spike script is a reasonable pattern
  (3s interval, generous timeout).

## Policy for this think tank
1. Same vault pattern as everything else external: agents get a scoped
   capability handle (`api.pixellab.ai`, GET+POST), never the raw key.
2. This is a real, metered paid API -- log every generation call's real cost
   (PixelLab's job responses should carry cost info; if not, at minimum log
   call counts) so this shows up in the Bank's ledger like every other real
   spend, the same way `digitalocean`/`treg` caps now do.
3. Don't regenerate a full sprite set speculatively for every agent at once --
   this is remaining scope/cost discipline, not a hard technical limit; start
   with the mechanism working end-to-end for one agent before scaling out.

## Sources
- `scripts/pixellab_spike.py` (this repo) -- the one already-tested, working
  call shape for character generation + tileset generation.
- https://www.pixellab.ai/pixellab-api (API overview)
- https://www.pixellab.ai/docs/options/animation and
  https://www.pixellab.ai/docs/tools/animation-to-animation (animation
  endpoints/options)
- `GET /balance` on the account itself, checked live by the
  developer (not a spike) -- $7.41 credits, Tier 1 subscription active.

## Lessons learned
- Written from a REAL account balance check and the project's own
  already-working spike script, not an agent spike -- unlike several other
  skill files in this directory, there was no ungrounded-guess version of this
  one to correct.
