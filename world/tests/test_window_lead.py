"""Change-request window lead time (owner revision 2026-11-11): the window
must start at least 12 hours ahead -- the owner keeps the veto link, but a
full day of notice proved slower than the work needs."""
import datetime
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import selfedit


class WindowLeadTests(unittest.TestCase):
    def setUp(self):
        # request_window reads the DB for conflict scans; the lead check runs
        # BEFORE any of that, so a surface stub is enough for the boundary.
        self._cm = unittest.mock.patch.object(
            selfedit, '_window_conflicts', return_value=[])
        self._cm.start()

    def tearDown(self):
        self._cm.stop()

    def _slot(self, hours_out):
        t = datetime.datetime.now() + datetime.timedelta(hours=hours_out)
        return (t.strftime('%Y-%m-%d'), t.strftime('%H:%M'))

    def test_one_hour_out_is_refused(self):
        date, start = self._slot(1)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        self.assertFalse(ok)
        self.assertIn('at least 12 hours', res['error'])

    def test_thirteen_hours_out_passes_the_lead_check(self):
        date, start = self._slot(13)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        # Past the lead gate the request may still fail on other contract
        # rules (validator naming, etc.) -- the boundary this pins is that the
        # lead error is gone.
        if not ok:
            self.assertNotIn('at least 12 hours', res.get('error', ''))

    def test_min_lead_constant_is_twelve_hours(self):
        self.assertEqual(selfedit.WINDOW_MIN_LEAD_S, 12 * 60 * 60)


class TankDownLaneTests(unittest.TestCase):
    """Owner direction 2026-11-11: a bug that prevents the think tank from
    running may file an emergency change request with tankDown=True and no
    minimum lead -- same pretense of activity (review, validator, veto link,
    rollback), none of the notice period."""

    def setUp(self):
        self._cm = unittest.mock.patch.object(
            selfedit, '_window_conflicts', return_value=[])
        self._cm.start()

    def tearDown(self):
        self._cm.stop()

    def _slot(self, minutes_out):
        t = datetime.datetime.now() + datetime.timedelta(minutes=minutes_out)
        return (t.strftime('%Y-%m-%d'), t.strftime('%H:%M'))

    def test_tank_down_allows_an_immediate_window(self):
        date, start = self._slot(5)
        ok, res = selfedit.request_window(
            'ada', date, start, 15, 'tank is down: serve loop crash-looping',
            change_type='emergency', tank_down=True)
        # Past the lead gate the request may fail on other contract rules --
        # the boundary this pins is that the lead refusals are gone.
        if not ok:
            self.assertNotIn('at least 12 hours', res.get('error', ''))
            self.assertNotIn('at least 1 hour', res.get('error', ''))

    def test_tank_down_requires_the_emergency_lane(self):
        date, start = self._slot(5)
        ok, res = selfedit.request_window(
            'ada', date, start, 15, 'test reason', tank_down=True)
        self.assertFalse(ok)
        self.assertIn('tank_down only applies to emergency', res['error'])

    def test_plain_emergency_keeps_its_one_hour_lead(self):
        date, start = self._slot(10)
        ok, res = selfedit.request_window(
            'ada', date, start, 15, 'test reason', change_type='emergency')
        self.assertFalse(ok)
        self.assertIn('at least 1 hour', res['error'])


if __name__ == '__main__':
    unittest.main()
