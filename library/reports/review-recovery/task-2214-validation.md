# Strategy test and validation report

Implementation: world/trading_strategy.py. Isolated validation workspace: project-wrq-197-734e4b58a4. All five actual quality checks passed: Black, flake8, mypy, Bandit and pytest-cov. Existing interface tests passed: 24 tests, 100% total coverage. No test suite was removed or its coverage threshold lowered. Validation output is recorded in the workspace quality JSON saved alongside this report.

Tests cover trailing SMA behavior, RSI length/range and constant prices, signal values, bounded exposure and summary metrics. Backtest data: datasets/btc-usd-daily-2023-10-10-to-2026-10-10.csv, 1096 actual Coinbase daily observations.

Actual simulation result: {"total_return": 0.028875622791604938, "sharpe_ratio": 0.3149275595533023, "max_drawdown": -0.045977498589920285, "num_trades": 14}

Total return is a fraction of initial capital, not dollars. The result is not $100/day and does not establish a future return. Exposure-changing events (num_trades) are not necessarily complete round-trip trades. Results include assumed 10bp turnover fees, lagged exposure and daily bars; they exclude slippage, intraday stops, live fills and out-of-sample parameter selection. These limitations are disclosed instead of inventing successful live testing.

The earlier truncated strategy file and its mismatched API were repaired; the existing tests now run against matching compute_sma, compute_rsi, generate_signals, apply_risk_management and backtest functions. Live order submission remains outside this module.

Timing correction: risk management shifts exposure once. Backtest consumes that exposure directly, without a second shift. Five new behavioral tests check entry timing, exit fees, no-signal accounting and future-signal independence. All five quality gates passed after this correction.
