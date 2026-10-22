# Feed tools: choosing the right one

## Purpose
Pick the cheapest, freshest, most direct source for the question. A live feed
is not a search engine, and a page fetch is not a feed. This is the
"choosing the right tool" guidance the Scrapling review recommended, written
for the tools this think tank actually has.

## Decision table

| Need | Tool | Cost | Freshness | Notes |
|---|---|---|---|---|
| Current ship positions | `read_ais_feed` | free | live, buffered ~30 min | AISStream; vessel name, lat/lon, time, MMSI. Do not use it for history. |
| Current crypto quotes | `read_market_feed` | free | live, latest per pair | Kraken ticker; use a pair, not a whole market crawl. |
| Any other registered live feed | `read_live_feed` | free | live, buffered | Use the service id and optional topic. Check `list_services` if unsure. |
| News headlines | `read_rss_feed` | free | minutes | Headline and link only; use `browse_page` for the article text. |
| One known URL | `browse_page` | 1 page request | live page | Allowlisted hosts only. Use for the full text of one article or page. |
| Unknown URL or discovery | `search_web` | 1 page request | index | Use a narrow query, then `browse_page` the best result. |
| GitHub repo facts | `github_get_repo` | free | live metadata | Stars, forks, language, default branch, push time. |
| GitHub code or symbols | `github_search_code` | free | live index | Search one repo or org; do not treat zero matches as proof the feature is absent. |
| Multi-page scrape or actor output | `apify_run_actor` then `apify_get_dataset_items` | metered, monthly cap | run time | Last resort. Prefer a feed or one page. Always send a stable `requestId`. |
| Video transcript | `youtube_transcript` | free | cached | Use the cached transcript when present; do not invent timestamps. |

`read_ais_feed` also returns `status.checkpoint` and `status.resumed_gap_s`.
AISStream is live-only: the checkpoint records the last ingested frame and the
gap after a reconnect, not a replay buffer.

## Rules
1. Prefer the structured feed for continuous data. Do not scrape a page that
   already has a feed.
2. Prefer one `browse_page` for one known page. Do not start an Apify actor for
   a single article.
3. Use `search_web` only when the URL is unknown. Read the best result, not ten.
4. Cite the tool and the source path or URL for every factual claim.
5. State freshness. A cached transcript, a buffered feed, and a live page have
   different ages.
6. Do not add a stealth or evasion fetcher. The allowlist and page-request
   budget are the governance boundary; the Scrapling review rejected bypassing
   them.
7. If a feed is empty or stale, say so. Do not fill the gap with a plausible
   guess.

## Budget notes
- `browse_page` and `search_web` draw from the monthly page-request budget.
- `apify_run_actor` draws from the Apify monthly budget and fails closed when
  it is exhausted.
- The live feeds and GitHub reads are free at the margin, but they are still
  bounded. Do not loop them.

## Sources
- `library/skills/research-desk.md`
- `library/skills/apify.md`
- `library/skills/github.md`
- The Scrapling review: `library/archive/1792619807984-spike-task-3254.md`
