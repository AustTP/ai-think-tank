"""The daily quota budgets for the keyed URL-reputation sources.

Operator direction (2026-11-01): the free-tier rate limits for VirusTotal
(500 lookups/day, 4/min) and Google Safe Browsing (~10k/day) belong in
the Bank's budget accounting, not just in a comment. These tests pin:
- the day-keyed quota ledger accrues per UTC day and resets at midnight;
- a spent daily quota makes the wrapper SKIP the source (score degrades,
  the verdict notes it) and further accrual is refused;
- cap 0 = unlimited -- a disabled cap never blocks;
- the VirusTotal 4/min community limit is enforced as >=15s spacing
  between real calls (pessimistic mark-before-attempt);
- the budgets are visible through the pricing report's _service_budget.
The score itself is mocked throughout -- these tests never touch the
network.
"""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import serve  # noqa: E402


def _clean_ledger():
    with mock.patch.object(serve, '_page_budget_ledger_read', return_value={}), \
         mock.patch.object(serve, '_page_budget_ledger_write',
                           side_effect=lambda ledger: None):
        yield


class QuotaLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = {}
        p_read = mock.patch.object(serve, '_page_budget_ledger_read',
                                   side_effect=lambda: dict(self.ledger))
        p_write = mock.patch.object(
            serve, '_page_budget_ledger_write',
            side_effect=lambda ledger: self.ledger.update(ledger))
        p_read.start(); p_write.start()
        self.addCleanup(p_read.stop); self.addCleanup(p_write.stop)
        # _mark_vt_call writes its spacing stamp through the settings
        # table: keep that in memory too (never touch the live DB).
        self.settings = {}
        p_get = mock.patch.object(serve, '_get_setting',
                                  side_effect=lambda key, default='': self.settings.get(key, default))
        p_set = mock.patch.object(serve, '_set_setting',
                                  side_effect=lambda key, val: self.settings.__setitem__(key, val))
        p_get.start(); p_set.start()
        self.addCleanup(p_get.stop); self.addCleanup(p_set.stop)

    def test_accrual_counts_per_day(self):
        now = datetime.datetime(2026, 11, 2, 12, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(serve._accrue_quota_call('virustotal', now=now))
        self.assertTrue(serve._accrue_quota_call('virustotal', now=now))
        self.assertEqual(serve._quota_used_today('virustotal', now=now), 2)

    def test_day_rollover_resets_the_allowance(self):
        late = datetime.datetime(2026, 11, 2, 23, 59, tzinfo=datetime.timezone.utc)
        early = datetime.datetime(2026, 11, 3, 0, 1, tzinfo=datetime.timezone.utc)
        for _ in range(5):
            serve._accrue_quota_call('safebrowsing', now=late)
        self.assertEqual(serve._quota_used_today('safebrowsing', now=late), 5)
        self.assertEqual(serve._quota_used_today('safebrowsing', now=early), 0)

    def test_exhausted_refuses_to_record_and_reports_exhausted(self):
        with mock.patch.object(serve, 'VIRUSTOTAL_DAILY_BUDGET', 2):
            for _ in range(2):
                self.assertTrue(serve._accrue_quota_call('virustotal'))
            self.assertFalse(serve._accrue_quota_call('virustotal'))
            self.assertTrue(serve._quota_exhausted('virustotal'))
            self.assertEqual(serve._quota_used_today('virustotal'), 2)

    def test_cap_zero_is_unlimited(self):
        with mock.patch.object(serve, 'SAFEBROWSING_DAILY_BUDGET', 0):
            self.assertFalse(serve._quota_exhausted('safebrowsing'))
            for _ in range(20001):  # past the free tier, still fine
                serve._accrue_quota_call('safebrowsing')
            self.assertFalse(serve._quota_exhausted('safebrowsing'))

    def test_vt_spacing_and_mark(self):
        with mock.patch.object(serve, 'VT_MIN_CALL_SPACING_S', 15):
            self.assertTrue(serve._vt_spacing_ok(now=1000.0))
            serve._mark_vt_call(now=1000.0)
            self.assertFalse(serve._vt_spacing_ok(now=1005.0))
            self.assertTrue(serve._vt_spacing_ok(now=1015.0))


class GateChokepoint(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = {}
        p_read = mock.patch.object(serve, '_page_budget_ledger_read',
                                   side_effect=lambda: dict(self.ledger))
        p_write = mock.patch.object(
            serve, '_page_budget_ledger_write',
            side_effect=lambda ledger: self.ledger.update(ledger))
        p_read.start(); p_write.start()
        self.addCleanup(p_read.stop); self.addCleanup(p_write.stop)
        # The wrapper's host cache and the VT spacing stamp both live in
        # the settings table: keep them in memory so tests neither pollute
        # the live DB nor read a cache entry another test left behind.
        self.settings = {}
        p_get = mock.patch.object(serve, '_get_setting',
                                  side_effect=lambda key, default='': self.settings.get(key, default))
        p_set = mock.patch.object(serve, '_set_setting',
                                  side_effect=lambda key, val: self.settings.__setitem__(key, val))
        p_get.start(); p_set.start()
        self.addCleanup(p_get.stop); self.addCleanup(p_set.stop)
        # And make sure no test inherits a spacing stamp from another.
        self.settings.pop('__vt_last_call__', None)

    def _verdict_with(self, vt_key=None, sb_key=None, cap_vt=400, cap_sb=10000):
        captured = {}

        def fake_score(url, virustotal_api_key=None, safebrowsing_api_key=None):
            captured['vt'] = virustotal_api_key
            captured['sb'] = safebrowsing_api_key
            return {'url': url, 'host': 'x.example', 'domain': 'x.example',
                    'band': 'unknown', 'score': 50,
                    'signals': {'ageDays': None, 'urlhaus': None,
                                'safebrowsing': None, 'virustotal': None,
                                'heuristics': [], 'errors': []},
                    'checkedAt': '2026-11-02T00:00:00Z'}

        env = {}
        if vt_key:
            env['VIRUSTOTAL_API_KEY'] = vt_key
        if sb_key:
            env['SAFEBROWSING_API_KEY'] = sb_key
        with mock.patch.dict('os.environ', env, clear=False), \
             mock.patch.object(serve, 'VIRUSTOTAL_DAILY_BUDGET', cap_vt), \
             mock.patch.object(serve, 'SAFEBROWSING_DAILY_BUDGET', cap_sb), \
             mock.patch.object(serve._url_trust, 'score_url', side_effect=fake_score):
            out = serve._url_trust_verdict('https://x.example/a')
        return out, captured

    def test_within_budget_both_sources_run_and_accrue(self):
        out, captured = self._verdict_with(vt_key='k', sb_key='k')
        self.assertEqual(captured['vt'], 'k')
        self.assertEqual(captured['sb'], 'k')
        self.assertEqual(serve._quota_used_today('virustotal'), 1)
        self.assertEqual(serve._quota_used_today('safebrowsing'), 1)

    def test_spent_vt_budget_skips_the_source_with_a_note(self):
        with mock.patch.object(serve, 'VIRUSTOTAL_DAILY_BUDGET', 0):
            for _ in range(3):
                serve._accrue_quota_call('virustotal')
        out, captured = self._verdict_with(vt_key='k', cap_vt=2)
        self.assertIsNone(captured['vt'])
        self.assertTrue(any('virustotal skipped: daily budget spent' in e
                            for e in out['signals']['errors']))

    def test_rate_spacing_skips_vt_with_a_note(self):
        serve._mark_vt_call()  # a call just happened
        out, captured = self._verdict_with(vt_key='k', cap_vt=0)
        self.assertIsNone(captured['vt'])
        self.assertTrue(any('rate spacing' in e for e in out['signals']['errors']))

    def test_no_key_means_no_accrual_and_no_note(self):
        out, captured = self._verdict_with()
        self.assertIsNone(captured['vt'])
        self.assertIsNone(captured['sb'])
        self.assertEqual(serve._quota_used_today('virustotal'), 0)
        self.assertEqual(out['signals']['errors'], [])


class PricingSurface(unittest.TestCase):
    def test_service_budget_reports_virustotal(self):
        with mock.patch.object(serve, '_quota_used_today',
                               return_value=37), \
             mock.patch.object(serve, 'VIRUSTOTAL_DAILY_BUDGET', 400):
            b = serve._service_budget('virustotal')
        self.assertEqual(b['unit'], 'url_lookups')
        self.assertEqual(b['used'], 37)
        self.assertEqual(b['left'], 363)
        self.assertEqual(b['enforcedBy'], 'virustotal_daily_budget')

    def test_service_budget_reports_safebrowsing_unlimited(self):
        with mock.patch.object(serve, '_quota_used_today',
                               return_value=5), \
             mock.patch.object(serve, 'SAFEBROWSING_DAILY_BUDGET', 0):
            b = serve._service_budget('safebrowsing')
        self.assertEqual(b['unit'], 'requests')
        self.assertIsNone(b['cap'])
        self.assertIsNone(b['left'])
        self.assertEqual(b['enforcedBy'], 'safebrowsing_daily_budget')


if __name__ == '__main__':
    unittest.main()
