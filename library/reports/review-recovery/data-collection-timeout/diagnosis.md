# Data collection recovery diagnosis

The pipeline loopback client used a 30-second deadline for a sequence of checks, each of which can itself run for 30 seconds plus command classification. Code tasks had a 120-second overall watchdog. These deadlines could abandon a still-running pipeline. Pipeline deadlines are now proportional to step count, and coding-class tasks have a separate bounded 1200-second watchdog.

A generated test file expected a different collector: candles_to_dataframe, compute_sha256, save_provenance, coinbase_collect_history and collect returning a pandas DataFrame with date-object inputs and output-path arguments. The production API is collect(product, start ISO date, end ISO date, resolve, getter), returning metadata for Library CSV/JSON outputs. coinbase_spot.research_tool depends on that existing API. The incompatible generated test file is preserved alongside this diagnosis as text, not treated as a requirement to rewrite the collector. Its applicable complete suite remains tests/test_history.py and the app historical-data tests. The coverage threshold remains 90%.

Incomplete pipeline responses no longer count as successful validation. Timeout failures preserve the working workspace and pause for infrastructure recovery.
