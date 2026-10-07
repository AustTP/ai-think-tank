import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coinbase_spot as spot


class PaperSpot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'paper.db')
        self.quote = {'fetchedAt': time.time(), 'product': {'product_id': 'BTC-USD', 'base_increment': '0.00001'},
                      'ticker': {'best_ask': '100', 'best_bid': '99'}}
        self.order = {'productId': 'BTC-USD', 'side': 'BUY', 'baseSize': '0.5', 'clientOrderId': 'order-1'}

    @contextlib.contextmanager
    def db(self):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def test_duplicate_order_does_not_spend_twice(self):
        first = spot.paper_order(self.db, self.order, self.quote)
        self.assertEqual(first, spot.paper_order(self.db, self.order, self.quote))
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM coinbase_paper_orders').fetchone()[0], 1)
            balance = json.loads(conn.execute('SELECT blob FROM coinbase_paper_account').fetchone()[0])
        self.assertEqual(balance['holdings']['BTC-USD'], '0.5')

    def test_conflicting_retry_is_rejected(self):
        spot.paper_order(self.db, self.order, self.quote)
        with self.assertRaises(ValueError):
            spot.paper_order(self.db, {**self.order, 'baseSize': '0.6'}, self.quote)

    def test_live_shorting_stale_quotes_and_invalid_sizes_are_rejected(self):
        variants = [{'mode': 'live'}, {'side': 'SELL'}, {'baseSize': 'NaN'},
                    {'baseSize': '-1'}, {'baseSize': '2'}, {'baseSize': '0.000001'}]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                spot.paper_order(self.db, {**self.order, **variant}, self.quote)
        with self.assertRaises(ValueError):
            spot.paper_order(self.db, self.order, {**self.quote, 'fetchedAt': 0})

    def test_loss_limit_blocks_new_exposure_but_allows_spot_exit(self):
        spot.paper_order(self.db, self.order, self.quote)
        with self.db() as conn:
            conn.execute('INSERT INTO coinbase_paper_orders VALUES (?, ?, ?)',
                         ('loss', 'loss', json.dumps({'createdAt': time.time(), 'realizedPnlUsd': '-50'})))
        with self.assertRaisesRegex(ValueError, 'loss limit'):
            spot.paper_order(self.db, {**self.order, 'clientOrderId': 'buy-2'}, self.quote)
        exit_order = spot.paper_order(self.db, {**self.order, 'side': 'SELL', 'clientOrderId': 'exit'}, self.quote)
        self.assertEqual(exit_order['status'], 'simulated_fill')

    def test_pair_path_injection_and_derivatives_rejected(self):
        for value in ['BTC-PERP', '../orders', 'BTC-USD/../../orders', None]:
            with self.assertRaises(ValueError):
                spot.product_id(value)

    def test_researched_policy_limits_pairs_allocation_and_unrealized_drawdown(self):
        policy = {'pairs': ['BTC-USD'], 'allocationUsd': 60, 'maxOrderUsd': 60}
        spot.paper_order(self.db, self.order, self.quote, policy)
        with self.assertRaisesRegex(ValueError, 'allocation'):
            spot.paper_order(self.db, {**self.order, 'clientOrderId': 'extra'},
                             {**self.quote, 'portfolioMarks': {'BTC-USD': '99'}}, policy)
        with self.db() as conn:
            conn.execute('INSERT INTO coinbase_paper_orders VALUES (?, ?, ?)', ('peak', 'peak',
                json.dumps({'createdAt': time.time(), 'equityBeforeUsd': '10100'})))
        with self.assertRaisesRegex(ValueError, 'drawdown'):
            spot.paper_order(self.db, {**self.order, 'baseSize': '0.01', 'clientOrderId': 'drawdown'},
                             {**self.quote, 'portfolioMarks': {'BTC-USD': '99'}}, policy)


if __name__ == '__main__':
    unittest.main()
