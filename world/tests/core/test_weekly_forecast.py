"""Weekly forecast tests (425): the awake/asleep world clock, the DST-correct
Friday 20:00 ET scheduler, exchange sessions, and the digest body (spend
forecast from the real ledger shape, dormant revenue section). The Telegram
push and the live think_tank DB are patched or redirected into a temp dir.

Run:
  cd /Users/poole86/ai-village-template/world
  python3 -m pytest tests/core/test_weekly_forecast.py
"""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import serve  # noqa: E402

_TMP = None
_PATCHER = None


def setUpModule():
    global _TMP, _PATCHER
    _TMP = tempfile.mkdtemp(prefix='think-tank-weekly-forecast-')
    _PATCHER = unittest.mock.patch.object(serve, 'DB_PATH', os.path.join(_TMP, 'test.db'))
    _PATCHER.start()
    serve.init_db()


def tearDownModule():
    _PATCHER.stop()
    shutil.rmtree(_TMP, ignore_errors=True)


def _fri_evening():
    # 2026-11-13 was a Friday; 20:00 ET = 2026-11-14 01:00 UTC (EST).
    return datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)


class AwakeView(unittest.TestCase):
    def setUp(self):
        self.view = serve._awake_view(_fri_evening())

    def test_rows_cover_all_continents(self):
        continents = {r['continent'] for r in self.view['rows']}
        self.assertEqual(continents,
                         {'Asia', 'Europe', 'Africa', 'Americas', 'Oceania'})

    def test_specific_classification(self):
        rows = {r['country']: r for r in self.view['rows']}
        # 8pm ET Friday: Europe is in its small hours, Shanghai is 9am
        # Saturday (awake, weekend), Sydney is noon Saturday, LA 5pm Friday.
        self.assertFalse(rows['Germany']['awake'])
        self.assertTrue(rows['China']['awake'])
        self.assertTrue(rows['China']['weekend'])
        self.assertTrue(rows['Australia']['awake'])
        self.assertTrue(rows['Australia']['weekend'])
        self.assertTrue(rows['US East']['awake'])
        self.assertFalse(rows['US East']['weekend'])

    def test_weighted_share_is_sane(self):
        self.assertIsNotNone(self.view['awakeShare'])
        self.assertGreater(self.view['awakeShare'], 0.0)
        self.assertLess(self.view['awakeShare'], 1.0)
        self.assertEqual(self.view['trackedPopM'],
                         sum(r['popM'] for r in self.view['rows']))


class NextWeeklyRun(unittest.TestCase):
    def test_wednesday_afternoon_targets_same_friday(self):
        now = datetime(2026, 11, 11, 17, 0, tzinfo=timezone.utc)  # Wed 12pm ET
        target = serve._next_weekly_run(now).astimezone(ZoneInfo(serve.FORECAST_TZ))
        self.assertEqual((target.weekday(), target.hour), (4, 20))
        self.assertEqual(target.strftime('%Y-%m-%d'), '2026-11-13')

    def test_friday_after_the_hour_targets_next_week(self):
        now = datetime(2026, 11, 14, 1, 30, tzinfo=timezone.utc)  # Fri 8:30pm ET
        target = serve._next_weekly_run(now).astimezone(ZoneInfo(serve.FORECAST_TZ))
        self.assertEqual(target.strftime('%Y-%m-%d'), '2026-11-20')
        self.assertEqual((target.weekday(), target.hour), (4, 20))

    def test_friday_before_the_hour_targets_same_day(self):
        now = datetime(2026, 11, 14, 0, 30, tzinfo=timezone.utc)  # Fri 7:30pm ET
        target = serve._next_weekly_run(now).astimezone(ZoneInfo(serve.FORECAST_TZ))
        self.assertEqual(target.strftime('%Y-%m-%d'), '2026-11-13')

    def test_dst_keeps_local_20(self):
        # July: EDT (UTC-4); December: EST (UTC-5). The LOCAL fire hour must
        # be 20:00 in both, which means the UTC instant shifts by an hour.
        july = serve._next_weekly_run(datetime(2026, 7, 15, 12, 0,
                                               tzinfo=timezone.utc))
        december = serve._next_weekly_run(datetime(2026, 12, 15, 12, 0,
                                                   tzinfo=timezone.utc))
        for target in (july, december):
            local = target.astimezone(ZoneInfo(serve.FORECAST_TZ))
            self.assertEqual((local.weekday(), local.hour), (4, 20))
        self.assertNotEqual(july.astimezone(timezone.utc).hour,
                            december.astimezone(timezone.utc).hour)


class SessionsView(unittest.TestCase):
    def test_friday_evening_sessions(self):
        view = serve._sessions_view(_fri_evening())
        by_name = {v['name']: v for v in view['venues']}
        # 8pm ET Friday: NYSE closed (after 4pm), London/XETRA/Tokyo/Shanghai
        # in their weekend. Crypto never closes.
        self.assertFalse(by_name['NYSE']['open'])
        self.assertFalse(by_name['LSE']['open'])
        self.assertTrue(view['crypto'])

    def test_midweek_session_opens(self):
        # Wednesday 14:30 UTC: NYSE (9:30 ET) and LSE (14:30 London) open.
        now = datetime(2026, 11, 11, 14, 30, tzinfo=timezone.utc)
        by_name = {v['name']: v for v in serve._sessions_view(now)['venues']}
        self.assertTrue(by_name['NYSE']['open'])
        self.assertTrue(by_name['LSE']['open'])


class DigestBody(unittest.TestCase):
    def test_digest_contains_spend_clock_revenue(self):
        with unittest.mock.patch.object(serve, '_telegram_send_text_sync',
                                        return_value=1) as push, \
             unittest.mock.patch.object(serve, 'log_action'):
            text = serve._send_weekly_forecast(_fri_evening())
        push.assert_called_once()
        self.assertIn('WEEKLY FORECAST', text)
        self.assertIn('SPEND', text)
        self.assertIn('WORLD CLOCK', text)
        self.assertIn('awake', text)
        self.assertIn('crypto open 24/7', text)
        self.assertIn('REVENUE', text)
        self.assertIn('No inbound ledger yet', text)
        # Persisted for a UI surface to read later.
        self.assertEqual(serve._get_setting('last_weekly_forecast'), text)

    def test_digest_numbers_come_from_the_ledger(self):
        serve._accrue_spend('example_service', 1.25)
        text = serve._weekly_forecast_text(_fri_evening())
        self.assertIn('example_service: $1.25 this month', text)


if __name__ == '__main__':
    unittest.main()
