"""Behavioral checks for execution timing and cash accounting."""
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from trading_strategy import apply_risk_management, backtest  # noqa: E402


class BacktestTiming(unittest.TestCase):
    def test_entry_uses_previous_signal_exactly_once(self):
        frame = pd.DataFrame({'close': [100., 100., 110.], 'signal': [0, 1, 0]})
        managed = apply_risk_management(frame)
        self.assertEqual(managed['position_size'].tolist(), [0., 0., .1])
        self.assertAlmostEqual(backtest(managed)['total_return'], .0099)

    def test_same_bar_signal_cannot_capture_earlier_return(self):
        frame = pd.DataFrame({'close': [100., 200., 200.], 'signal': [0, 1, 0]})
        self.assertAlmostEqual(backtest(apply_risk_management(frame))['total_return'], -.0001)

    def test_exit_fee_charged_once_and_future_return_not_held(self):
        frame = pd.DataFrame({'close': [100., 100., 100., 200.], 'signal': [1, -1, 0, 0]})
        managed = apply_risk_management(frame)
        self.assertEqual(managed['position_size'].tolist(), [0., .1, 0., 0.])
        result = backtest(managed)
        self.assertAlmostEqual(result['total_return'], .9999 ** 2 - 1)
        self.assertEqual(result['num_trades'], 2)

    def test_no_signal_has_zero_pnl_and_zero_trades(self):
        frame = pd.DataFrame({'close': [100., 200., 50.], 'signal': [0, 0, 0]})
        result = backtest(apply_risk_management(frame))
        self.assertEqual(result['total_return'], 0)
        self.assertEqual(result['num_trades'], 0)

    def test_future_signals_do_not_change_past_exposure(self):
        frame = pd.DataFrame({'close': [100.] * 5, 'signal': [0, 1, 0, 0, -1]})
        original = apply_risk_management(frame)['position_size']
        frame.loc[4, 'signal'] = 1
        changed = apply_risk_management(frame)['position_size']
        pd.testing.assert_series_equal(original.iloc[:4], changed.iloc[:4])
