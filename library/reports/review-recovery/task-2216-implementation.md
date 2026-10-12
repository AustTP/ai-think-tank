# Implemented trading strategy and risk management

The offline implementation is installed at world/trading_strategy.py and validated in project-wrq-197-734e4b58a4. It implements trailing SMA and RSI diagnostics, long-only candidate signals, 10% capped exposure, lagged execution assumptions and a fee-adjusted daily simulation. It never shorts or submits live orders.

The actual five-step code pipeline passed, including 24 tests with 100% coverage. Source interfaces match the existing test suite. Truncated Python and accidentally executed model prose were corrected. The server shell parser now rejects multiple code blocks and introductory prose before execution.

Read reports/review-recovery/task-2210-implementation-plan.md for design and limitations, and reports/review-recovery/task-2214-validation.md for actual results. The strategy is an experimental offline hypothesis. Profit targets, live trading, tuning and further execution controls are not asserted as delivered.
