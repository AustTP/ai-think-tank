"""Allowance-exhaustion notifications: the operator learns an allowance is
GONE through the standing health-alert pipeline, the same channel as a
failing credential or the Jev outage -- not by discovering that every model
call or browse has been quietly refusing. One alert per trip (a False->True
transition latch), re-armed by the next month or a raised cap. Never raises:
a notification failure must never turn into a rejection of the real call."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402

_TMP_DIR = None
_PATCHER = None


def setUpModule():
    global _TMP_DIR, _PATCHER
    _TMP_DIR = tempfile.mkdtemp(prefix='think-tank-allowance-alerts-')
    _PATCHER = unittest.mock.patch.multiple(
        serve,
        DB_PATH=os.path.join(_TMP_DIR, 'test.db'),
    )
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


class AllowanceNotifyLatch(unittest.TestCase):
    def setUp(self):
        serve._ALLOWANCE_NOTIFIED.clear()
        self.persisted = []
        self.pushed = []
        p1 = mock.patch.object(serve, '_persist_new_health_alerts',
                               side_effect=lambda a: self.persisted.extend(a) or a)
        p2 = mock.patch.object(serve, '_push_new_health_alerts',
                               side_effect=self.pushed.extend)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_fires_once_on_the_false_to_true_transition(self):
        serve._note_allowance_exhausted('monthlySpend', 'S', 'B', False)
        self.assertEqual([], self.persisted)  # not tripped: silent
        serve._note_allowance_exhausted('monthlySpend', 'S', 'B', True)
        self.assertEqual(1, len(self.persisted))
        self.assertEqual('allowance', self.persisted[0]['category'])
        self.assertEqual('critical', self.persisted[0]['severity'])
        self.assertEqual(1, len(self.pushed))
        # tripped again: the latch holds, no spam at every chokepoint call
        serve._note_allowance_exhausted('monthlySpend', 'S', 'B', True)
        serve._note_allowance_exhausted('monthlySpend', 'S', 'B', True)
        self.assertEqual(1, len(self.persisted))

    def test_release_rearms_silently(self):
        serve._note_allowance_exhausted('pageRequests', 'S', 'B', True)
        self.assertEqual(1, len(self.persisted))
        serve._note_allowance_exhausted('pageRequests', 'S', 'B', False)
        self.assertEqual(1, len(self.persisted))  # release: no "restored" ping
        serve._note_allowance_exhausted('pageRequests', 'S', 'B', True)
        self.assertEqual(2, len(self.persisted))  # next trip alerts again

    def test_kinds_are_independent(self):
        serve._note_allowance_exhausted('monthlySpend', 'S', 'B', True)
        serve._note_allowance_exhausted('pageRequests', 'S', 'B', True)
        serve._note_allowance_exhausted('highTier', 'S', 'B', True)
        self.assertEqual(3, len(self.persisted))

    def test_notification_failure_never_raises(self):
        with mock.patch.object(serve, '_persist_new_health_alerts',
                               side_effect=RuntimeError('db down')):
            serve._note_allowance_exhausted('monthlySpend', 'S', 'B', True)
        self.assertEqual([], self.pushed)

    def test_spend_cap_check_notifies_when_tripped(self):
        # The gate itself is the notify point: a tripped cap reaches the
        # operator the first time any chokepoint consults it.
        ledger = {serve._SPEND_CAP_BASELINE_KEY: {
            'period': serve._spend_cap_period(), 'baseline': 0.0}}
        with mock.patch.object(serve, '_spend_ledger_read', lambda: ledger), \
                mock.patch.object(serve, '_spend_ledger_write', lambda l: None), \
                mock.patch.object(serve, 'SPEND_CAP_USD', 50.0):
            for svc in range(60):
                ledger[f'svc{svc}'] = {'used': 1.0}
            self.assertTrue(serve._think_tank_spend_cap_exceeded())
        self.assertEqual(1, len(self.persisted))
        self.assertIn('Monthly USD spend cap', self.persisted[0]['message'])

    def test_spend_cap_below_cap_is_silent(self):
        ledger = {serve._SPEND_CAP_BASELINE_KEY: {
            'period': serve._spend_cap_period(), 'baseline': 0.0}}
        with mock.patch.object(serve, '_spend_ledger_read', lambda: ledger), \
                mock.patch.object(serve, '_spend_ledger_write', lambda l: None), \
                mock.patch.object(serve, 'SPEND_CAP_USD', 50.0):
            ledger['one'] = {'used': 5.0}
            self.assertFalse(serve._think_tank_spend_cap_exceeded())
        self.assertEqual([], self.persisted)

    def test_page_budget_check_notifies_when_tripped(self):
        with mock.patch.object(serve, 'PAGE_REQUEST_MONTHLY_BUDGET', 100), \
                mock.patch.object(serve, '_page_budget_used', lambda: 100):
            self.assertTrue(serve._page_budget_exhausted())
        self.assertEqual(1, len(self.persisted))
        self.assertIn('page-request', self.persisted[0]['message'])

    def test_high_tier_check_notifies_when_tripped(self):
        with mock.patch.object(serve, 'HIGH_TIER_MONTHLY_BUDGET_USD', 5.0), \
                mock.patch.object(serve, '_high_tier_spend_this_month',
                                  lambda: 5.0):
            self.assertTrue(serve._high_tier_budget_exceeded())
        self.assertEqual(1, len(self.persisted))
        self.assertIn('High-tier', self.persisted[0]['message'])


if __name__ == '__main__':
    unittest.main()
