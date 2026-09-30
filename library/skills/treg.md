# Treg

## Purpose
Reach for this before any real Treg work: posting/reading via the X (Twitter) or
LinkedIn accounts connected through Treg, or evaluating whether some other
platform Treg catalogs is worth using.

## Key facts
- Treg (treg.to) is a unified API catalog/proxy for AI agents -- "OpenRouter for
  tools." It fronts thousands of third-party API endpoints (3,684+ across SEO,
  social, enrichment, ads data) behind one credential and one billing account.
- **Real pricing: ~$0.001 per call**, both for X (Twitter) endpoints (201 of them,
  sourced from providers like Bright Data/Just One API/ScrapeCreators) and LinkedIn
  endpoints (62 of them, sourced from Apify/Aviato/Bright Data). Price is shown
  before the call. No subscription -- pay per call only.
- Every new team gets **$1.00 in free credit**. The developer's account carries a
  separate $10 balance on top of that.
- Treg's own architecture already matches this think tank's confused-deputy vault
  design almost exactly: the caller never holds the upstream provider's key --
  Treg's proxy injects credentials server-side and relays the request. A think tank
  integration would layer the think tank's OWN vault/capability-handle on top of
  Treg's key (agent -> think tank vault -> Treg -> X/LinkedIn), not replace it.
- Specific per-endpoint prices vary by provider even within one platform (e.g.
  different X endpoints from different scrapers may not all be exactly $0.001) --
  the catalog shows the exact price for a given endpoint before it's called; don't
  assume every call costs the same without checking.

## Policy for this think tank
1. Same vault pattern as everything else external: the Treg API key lives
   encrypted server-side; an agent gets a scoped capability handle, never the raw
   key.
2. Since this is billed real money against a real $10 balance (not unlimited),
   log every call's cost the same way OpenRouter calls already accrue to the
   Bank's per-service ledger -- `treg` should show up there once it's wired in,
   the same way `digitalocean` now does with its cap.
3. Confirm the exact per-endpoint price shown by Treg's catalog before a real
   call, don't assume the ~$0.001 figure above applies to every single endpoint.

## Sources
- https://treg.to/catalog/x (X/Twitter: 201 endpoints, priced per call)
- https://treg.to/catalog/linkedin (LinkedIn: 62 endpoints, priced per call)
- https://treg.to/catalog (3,684 total endpoints, free-credit + no-subscription
  pricing model)
- https://orangebot.ai/product/treg ("OpenRouter for tools", 0% markup framing)
- Verified by the developer with real web search, correcting an
  earlier agent spike (see Lessons learned below).

## Lessons learned
- An earlier agent "spike" on Treg invented specific per-post prices
  ($0.03 for an X post, $0.10 for a LinkedIn post) that turned out to be roughly
  30-100x too high compared to the real ~$0.001/call figure found via an actual
  web search. The spike had no real internet access -- it was guessing a
  plausible-sounding number, not reporting a real one. Same lesson as
  library/skills/digitalocean.md: never trust a spike's specific dollar figures
  for a real integration decision without verifying them first.
