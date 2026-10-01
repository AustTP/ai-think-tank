# Tavily

## Purpose
Reach for this before any real web-search work through the think tank's
`search_web` agent tool -- the first-stop tool for "I don't know a specific
URL, find where the answer lives," feeding browse_page afterward.

## Key facts
- Endpoint: `POST https://api.tavily.com/search`. Auth: `Authorization:
  Bearer <key>` (TAVILY_API_KEY in .env; `search_web` is only offered to agents
  when the key is present).
- Request fields:
  - `query` (required), `max_results` (1-20, default 10).
  - `search_depth`: `basic`/`advanced`/`fast`/`ultra-fast` (basic/fast/ultra-
    fast = 1 credit, advanced = 2).
  - `include_answer`: `true`/`basic`/`advanced`/`false` (an LLM-written quick
    answer; `basic`/`advanced` control its depth).
  - `topic`: `general` | `news` | `finance`. **There is NO `topic=social`** --
    for X/Twitter or LinkedIn content, use the Treg tools or Apify actors
    instead, not a Tavily search.
  - `include_raw_content`: `true`/`markdown`/`text`/`false`.
  - `time_range`, `include_domains`/`exclude_domains` (max 300/150),
    `country`, `language`, `safe_search`.
- Response: `query`, `answer` (when requested), `results[]` (title, url,
  content, score, optional published_date/raw_content), `usage.credits`,
  `request_id`.
- Other endpoints exist but are NOT wired: `/extract` (pull clean content from
  a list of URLs), `/crawl`, `/map`, `/research`. Don't assume they're
  reachable from an agent tool until wired.

## Budget reality
- Credit-based. Free plan: **1,000 credits/month**, no card needed. Rate
  limit 100 RPM (dev). One `basic` search = 1 credit; `advanced` depth = 2.
- 1,000 credits/month is a real ceiling for the whole think tank -- a handful
  of agent-heavy days of `search_web` can exhaust it. Not currently metered
  into the Bank ledger; treat it as a shared budget and prefer `browse_page`
  on known URLs when a specific target is already known.

## Policy for this think tank
1. `search_web` is the discovery tool, not the workhorse -- after it returns
   candidate URLs, switch to browse_page on the specific page. This also keeps
   credit burn low (1 search -> 1 page vs repeated searches).
2. Don't request `include_answer`/`advanced` depth unless the agent genuinely
   needs the synthesized answer or deeper content; default to 1-credit basic
   depth.
3. If search_web starts returning quota errors (`429`/credit-limit), treat it
   as a shared-budget exhaustion signal and prefer direct browse_page calls.

## Sources
- https://docs.tavily.com/documentation/api-reference/endpoint/search
- https://docs.tavily.com/documentation/api-credits (1,000 free credits/mo,
  PAYGO $0.008/credit)
- https://docs.tavily.com/documentation/rate-limits (100 RPM dev)

## Lessons learned
- A natural "what's trending on X" question gets routed to the ask lane with
  the Treg X tool available, yet the model still reached for search_web -- a
  plain Tavily search does NOT cover social/trending content (no social topic),
  so an agent that picks search_web for that will come back empty. This is why
  the ask lane force-starts x_trending_topics for such questions.
