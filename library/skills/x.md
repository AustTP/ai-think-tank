# X / Twitter

## Purpose

Read X through the tank's own authenticated session. Replaces the Apify
actor lane (retired 2026-11-05 after the demo-stub wall): the underlying
tool is the open-source repository `vladkens/twscrape`, which the tank
RUNS rather than re-implements. The tank keeps only credential custody
and a thin HTTP adapter; the GraphQL machinery lives upstream, so tool
updates never change tank code.

## Principles that govern this (and every scraped source)

- **Run upstream repos, don't hard-code them.** If a capability needs
  scraping logic, first look for a vetted repository that already does
  the job. New logic in-tree is the last resort, and modifications to a
  vendored tool are documented, never silent.
- **Trusted repository list.** Tools only run when their GitHub repo is
  on the registry: `GET /api/trusted-repos`. Any agent can add a repo:
  `POST /api/trusted-repos` with `{repo: "owner/name", kind, purpose}`.
  Your name goes on the voucher; removal is operator-only. If a tool
  refuses to run because its repo "is not trusted", that is the registry
  working as designed -- add it with an honest purpose statement.
- **Session material is server-side.** The X session cookies live in a
  server-owned pool; agents use the endpoints, never the cookies.

## The endpoints (agent key required)

- `GET /api/x/search?query=<search>&limit=20` -- X search (Latest tab),
  limit capped at 50 per call. The query uses X's own search operators:
  `from:dankornas`, `since:2026-01-01`, `until:2026-02-01`, quoted
  phrases, and so on.
- `GET /api/x/tweet/{id}` -- one tweet, full detail.
- `GET /api/x/user/{handle}/tweets?limit=50` -- a user's recent posts
  (capped at 100 per call).

Results are compact JSON: `id, date, text (2k cap), username, likes,
views, replies, rts, inReplyTo, conversationId, links, url`.

## Rate windows (read this before pulling big)

One shared account backs every agent call. When X locks the search
endpoint, calls return **429 with the reset time in the error text**.
Pace accordingly: prefer targeted windowed searches
(`since:`/`until:` slices) over one giant pull, and never retry a 429 in
a tight loop. A 503 means the tool is missing or its repo is not
trusted yet; a 502 is an upstream failure worth one retry.

## Enrichment (no X quota)

Per-tweet rendering without touching the session pool:
`https://api.fxtwitter.com/{handle}/status/{id}` (egress-allowlisted).
Response has `tweet.text`, `created_at`, `likes`, `retweets`, `replies`
(plain ints), and `replying_to_status` (the parent's id) -- one child
tweet reconstructs its whole thread.

## Provenance

- Tool: https://github.com/vladkens/twscrape (MIT). Installed on the
  server as the `twscrape` CLI.
- Registry entry: `GET /api/trusted-repos` -- see `vladkens/twscrape`.
- Adapter (credential custody + subprocess boundary only):
  `world/server/serve_parts/185_x_timeline.py`.
