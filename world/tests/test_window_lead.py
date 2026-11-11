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


if __name__ == '__main__':
    unittest.main()
