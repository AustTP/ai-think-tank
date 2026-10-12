# Source review: ai-think-tank and Coinbase integration

Scope: the local C:/Users/Khalil/Projects/ai-think-tank repository, inspected on 2026-10-10. This is the available existing Python app. If another application was intended, its repository must be supplied; this report makes no claims about an unavailable app.

## Functionality and architecture

world/serve.py is the FastAPI entry point, HTTP API and SQLite persistence layer. Its get_state_from_db/save_state_to_db functions manage task and agent state. The browser interface lives in world/index.html and supporting JavaScript. world/sim.py coordinates task assignment, execution and independent review. world/content.py runs agent research and coding work through the server's model proxy. world/story_tracking.py associates subtasks and spending with original stories and scrum masters.

world/coinbase_spot.py provides public spot-market research and isolated paper execution. Its router_for registers market, candle, account and paper-order routes. market calls Coinbase product metadata and ticker endpoints; candles requests historical candles. public_get sends HTTPS GET requests to https://api.coinbase.com/api/v3/brokerage/market with a 15-second timeout. No Coinbase API secrets are required for these public market requests. paper_order updates a local simulated account and order ledger in SQLite; it does not submit live exchange orders.

world/historical_data.py implements collect: validated product and UTC date range -> public Coinbase Exchange daily candles in 299-day batches -> schema/OHLCV checks -> timestamp filtering, deduplication and sorting -> Library CSV and JSON provenance. Its source endpoint is https://api.exchange.coinbase.com/products/{product}/candles with granularity 86400. CSV columns are timestamp, unix_time, product_id, open, high, low, close and volume. The collector uses Python standard-library CSV, datetime, JSON, hashing, filesystem and HTTP modules. Product validation is imported from coinbase_spot. Default Library resolution comes from serve. Source URLs, retrieval time, SHA256 and missing-day counts are recorded.

world/coinbase_spot.py research_tool exposes coinbase_collect_history to research agents. It delegates to historical_data.collect, so a new collector need not be generated for each request. Data flows from public exchange responses through validation into Library artifacts, then through story artifact checks and independent review. Paper-order state is separate from market-data files and from live exchange accounts.

## Verified gaps and improvements

1. historical_data.fetch has a 25-second timeout but no retry/backoff. Add bounded retry for transient failures; retain schema validation and never invent missing prices.
2. historical_data.collect records missing days but can still save an incomplete dataset. Consumers should explicitly verify missingDays against their acceptance criteria.
3. Dataset publication uses atomic replacement per file, but CSV and metadata are separate writes. An interrupted metadata write can leave a CSV without its provenance; a joint publication mechanism would improve consistency.
4. The latest previous report did not inspect this source and substituted estimated issues. Those estimates are not findings about this application and are excluded here.
5. Coding-model failures previously discarded provider errors behind a generic message. The coding executor now retains the error and allows 120 seconds for a coding reply instead of its former 30-second helper default. This improves diagnosis; it does not prove a historical timeout caused the earlier failure.
6. Profitability and strategy efficiency are not measured by a source review. No profit figures are invented, and no live-order verification is claimed. The request is an app architecture and integration review, not a profitability benchmark.

## Verification and boundaries

Direct source inspection of the named modules supports the architecture and endpoint descriptions. A real historical collection produced 1,096 BTC-USD daily records from 2023-10-10 through 2026-10-09, with no missing days. Dataset: datasets/btc-usd-daily-2023-10-10-to-2026-10-10.csv. Provenance: datasets/btc-usd-daily-2023-10-10-to-2026-10-10.json.

No credentials, live trading, profitability estimates, or inspection of another repository are included. Independent review remains required.
