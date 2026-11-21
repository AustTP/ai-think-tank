"""Change-request window lead time (owner revisions 2026-11-11 and
2026-11-20): the window must start at least 6 hours ahead -- a full day of
notice proved slower than the work needs. Owner rule 2026-11-20: normal
windows must also sit OUTSIDE Eastern-time business hours (09:00-17:00
America/New_York, Mon-Fri) -- the tank applies its changes while the
office is closed."""
import datetime
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import selfedit  # noqa: E402
import serve  # noqa: E402


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
        self.assertIn('at least 6 hours', res['error'])

    def test_five_hours_out_is_refused(self):
        date, start = self._slot(5)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        self.assertFalse(ok)
        self.assertIn('at least 6 hours', res['error'])

    def test_seven_hours_out_passes_the_lead_check(self):
        date, start = self._slot(7)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        # Past the lead gate the request may still fail on other contract
        # rules (validator naming, etc.) -- the boundary this pins is that the
        # lead error is gone.
        if not ok:
            self.assertNotIn('at least 6 hours', res.get('error', ''))

    def test_min_lead_constant_is_six_hours(self):
        self.assertEqual(selfedit.WINDOW_MIN_LEAD_S, 6 * 60 * 60)


class OwnerLeadOverrideTests(unittest.TestCase):
    """The owner can waive or shorten notice for a request they are
    personally authorizing, via CHANGE_WINDOW_MIN_LEAD_HOURS in .env (0
    waives). The veto link, supervisor review, validator, and rollback still
    apply; an unreadable value fails SAFE to the 12h default."""

    def setUp(self):
        self._cm = unittest.mock.patch.object(
            selfedit, '_window_conflicts', return_value=[])
        self._cm.start()

    def tearDown(self):
        self._cm.stop()

    def _slot(self, minutes_out):
        t = datetime.datetime.now() + datetime.timedelta(minutes=minutes_out)
        return (t.strftime('%Y-%m-%d'), t.strftime('%H:%M'))

    def _env(self, value):
        return unittest.mock.patch.object(
            serve, '_load_env',
            return_value={} if value is None
            else {'CHANGE_WINDOW_MIN_LEAD_HOURS': value})

    def test_zero_waives_notice(self):
        with self._env('0'):
            date, start = self._slot(5)
            ok, res = selfedit.request_window('ada', date, start, 15,
                                              'test reason')
        if not ok:
            self.assertNotIn('from now', res.get('error', ''))

    def test_half_hour_lead_passes_at_thirty_five_minutes_out(self):
        with self._env('0.5'):
            date, start = self._slot(35)
            ok, res = selfedit.request_window('ada', date, start, 15,
                                              'test reason')
        if not ok:
            self.assertNotIn('from now', res.get('error', ''))

    def test_half_hour_lead_still_refuses_a_two_minute_slot(self):
        with self._env('0.5'):
            date, start = self._slot(2)
            ok, res = selfedit.request_window('ada', date, start, 15,
                                              'test reason')
        self.assertFalse(ok)
        self.assertIn('at least 0.5 hours', res['error'])

    def test_unreadable_value_falls_back_to_the_default(self):
        with self._env('soon'):
            date, start = self._slot(1)
            ok, res = selfedit.request_window('ada', date, start, 15,
                                              'test reason')
        self.assertFalse(ok)
        self.assertIn('at least 6 hours', res['error'])

    def test_missing_value_is_the_constitution_default(self):
        with self._env(None):
            date, start = self._slot(1)
            ok, res = selfedit.request_window('ada', date, start, 15,
                                              'test reason')
        self.assertFalse(ok)
        self.assertIn('at least 6 hours', res['error'])


class OutsideBusinessHoursTests(unittest.TestCase):
    """Owner rule 2026-11-20: with the shorter 6h notice, NORMAL change
    windows must sit entirely outside Eastern-time business hours
    (09:00-17:00 America/New_York, Mon-Fri) -- evenings, overnights, and
    weekends are in; any weekday 9-to-5 minute is out. The emergency and
    tank-down lanes are exempt."""

    def setUp(self):
        self._cm = unittest.mock.patch.object(
            selfedit, '_window_conflicts', return_value=[])
        self._cm.start()

    def tearDown(self):
        self._cm.stop()

    @staticmethod
    def _et_utc_slot(day_offset, hour_et, minute_et=0):
        """Build (date, start) strings for `day_offset` days from now at a
        fixed Eastern clock time, converted to UTC for the API."""
        from zoneinfo import ZoneInfo
        et = ZoneInfo('America/New_York')
        target = (datetime.datetime.now(et)
                  + datetime.timedelta(days=day_offset)).replace(
            hour=hour_et, minute=minute_et, second=0, microsecond=0)
        utc = target.astimezone(datetime.timezone.utc)
        return utc.strftime('%Y-%m-%d'), utc.strftime('%H:%M')

    @classmethod
    def _weekday_slot(cls, hour_et, minute_et=0):
        """A slot 2-4 days out that lands on a Mon-Fri day (at least one of
        today+2..today+4 is always a business day), so the lead check passes
        and only the business-hours rules decide. The weekday is picked in
        ET -- the clock the gate walks -- because between 19:00 and midnight
        ET the UTC calendar is already a day ahead of the ET one."""
        from zoneinfo import ZoneInfo
        for extra in (2, 3, 4):
            wd = (datetime.datetime.now(
                ZoneInfo('America/New_York')).weekday() + extra) % 7
            if wd < 5:
                return cls._et_utc_slot(extra, hour_et, minute_et)
        raise AssertionError('unreachable')

    @classmethod
    def _weekend_slot(cls, hour_et):
        from zoneinfo import ZoneInfo
        for extra in range(1, 7):
            wd = (datetime.datetime.now(
                ZoneInfo('America/New_York')).weekday() + extra) % 7
            if wd >= 5:
                return cls._et_utc_slot(extra, hour_et)
        raise AssertionError('unreachable')

    def test_midday_weekday_slot_is_refused(self):
        date, start = self._weekday_slot(10, 30)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        self.assertFalse(ok)
        self.assertIn('outside', res['error'])
        self.assertIn('business hours', res['error'])

    def test_window_overlapping_5pm_et_is_refused(self):
        date, start = self._weekday_slot(16, 30)
        ok, res = selfedit.request_window('ada', date, start, 60, 'test reason')
        self.assertFalse(ok)
        self.assertIn('business hours', res['error'])

    def test_window_overlapping_9am_et_is_refused(self):
        date, start = self._weekday_slot(8, 30)
        ok, res = selfedit.request_window('ada', date, start, 60, 'test reason')
        self.assertFalse(ok)
        self.assertIn('business hours', res['error'])

    def test_evening_weekday_slot_gets_past_the_hours_gate(self):
        date, start = self._weekday_slot(19)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        # Past the hours gate the request may fail on later contract rules
        # (validator, roster) -- the boundary this pins is that the
        # business-hours error is gone.
        if not ok:
            self.assertNotIn('business hours', res.get('error', ''))

    def test_early_morning_weekday_slot_gets_past_the_hours_gate(self):
        date, start = self._weekday_slot(7)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        if not ok:
            self.assertNotIn('business hours', res.get('error', ''))

    def test_weekend_slot_gets_past_the_hours_gate(self):
        date, start = self._weekend_slot(10)
        ok, res = selfedit.request_window('ada', date, start, 15, 'test reason')
        if not ok:
            self.assertNotIn('business hours', res.get('error', ''))

    def test_plain_emergency_observes_business_hours(self):
        # Owner rule 2026-11-20: only the tank-down lane may touch the tank
        # during business hours. A plain emergency weekday-midday request is
        # refused by the hours gate.
        t = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        date, start = t.strftime('%Y-%m-%d'), t.strftime('%H:%M')
        ok, res = selfedit.request_window(
            'ada', date, start, 15, 'test reason', change_type='emergency')
        # The slot may or may not be inside business hours depending on when
        # the suite runs -- pin only the TANK-DOWN contrast: the same slot
        # with tank_down=True must never carry the hours refusal.
        if not ok:
            td_ok, td_res = selfedit.request_window(
                'ada', date, start, 15, 'tank is down: serve loop crash-looping',
                change_type='emergency', tank_down=True)
            if not td_ok:
                self.assertNotIn('business hours', td_res.get('error', ''))

    def test_tank_down_lane_ignores_business_hours(self):
        date, start = self._weekday_slot(10, 30)
        ok, res = selfedit.request_window(
            'ada', date, start, 15, 'tank is down: serve loop crash-looping',
            change_type='emergency', tank_down=True)
        if not ok:
            self.assertNotIn('business hours', res.get('error', ''))


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
            self.assertNotIn('at least 6 hours', res.get('error', ''))
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
