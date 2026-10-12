# Implementation plan: offline trading-strategy hypothesis

The local app now contains world/trading_strategy.py. The implementation is an offline, long-only RSI hypothesis with a trailing SMA diagnostic, 10% maximum portfolio exposure and lagged exposure to avoid same-bar execution. RSI below 35 opens candidate exposure; RSI above 65 closes exposure. It never opens shorts or submits exchange orders. These parameters are experimental choices for a bounded simulation, not an assertion of an optimal strategy or achievable daily income.

Implementation steps:
1. Reuse world/historical_data.py for actual Coinbase daily OHLCV rather than generating another collector. Saved input: datasets/btc-usd-daily-2023-10-10-to-2026-10-10.csv.
2. Load time-sorted data; use generate_signals, apply_risk_management and backtest from world/trading_strategy.py. These functions are implemented and independently testable.
3. Validate interface compatibility, warm-up handling, exposure bounds and lagged returns. Run formatting, lint, mypy, Bandit and tests with minimum 90% coverage. The current implementation passed all five gates.
4. Inspect actual results in the validation report. Keep execution offline/paper while limitations remain; do not claim the $100/day objective is met.
5. Before any later live-execution story: separately define capital, fees/slippage assumptions, approved risk limits, out-of-sample validation and operational controls. This plan does not authorize live trading.

Known gaps: strategy parameters are not optimized; only one pair and daily resolution were evaluated; 10bp turnover fees are a simulation assumption and not a verified account fee; no slippage, execution latency or intrabar stops are simulated. Measuring these is future work, not evidence of profitability.

Completion criteria for this planning story are a written implementation plan. Its scope excludes executing all future enhancements described above.
