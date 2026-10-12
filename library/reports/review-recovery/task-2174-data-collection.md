# Data collection and storage: implementation reuse and actual evidence

Inspected 2026-10-10. The existing implementation is world/historical_data.py, collect. It collects daily Coinbase Exchange OHLCV candles, validates the schema and numeric values, removes duplicate timestamps, sorts records, and publishes a CSV with SHA256/source/missing-day metadata. world/coinbase_spot.py exposes it as coinbase_collect_history. No replacement model-generated collector is needed.

Actual collection used BTC-USD, consistent with the Library's existing datasets. Product selection can be extended if the user specifies more pairs; this is not a claim of coverage for every cryptocurrency.

Range: 2023-10-10 inclusive to 2026-10-10 exclusive, three calendar years. The last complete UTC day is 2026-10-09. Result: 1,096 daily records, zero missing days. CSV: datasets/btc-usd-daily-2023-10-10-to-2026-10-10.csv. Provenance and source URLs: datasets/btc-usd-daily-2023-10-10-to-2026-10-10.json.

Original scope: a script collecting daily Coinbase price/volume data into CSV. Three data sources, real-time updates, fabricated source sampling, backtest-result files and unrelated review findings are excluded. The prior recovery report's assertions of these requirements are unsupported.

The earlier model-call failures have no preserved provider error, so their exact cause cannot be established. New coding requests retain the actual error and use a 120-second timeout. This report and dataset do not substitute for the mandatory code-director validation or independent review. Those checks must run on the reused implementation before this code story is complete.
